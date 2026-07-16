import asyncio
import inspect
import json
import logging
import os
import sys
import time
import uuid
from collections import defaultdict
from collections.abc import Callable
from pathlib import Path

import docker
import jsonschema
from docker.errors import DockerException, NotFound
from fastmcp.utilities.json_schema import compress_schema
from fastmcp.utilities.types import get_cached_typeadapter
from mcp.types import CallToolResult, ImageContent, TextContent
from openai.types.chat.chat_completion_message_function_tool_call import (
    ChatCompletionMessageFunctionToolCall as ToolCall,
)
from pydantic import BaseModel

from deeppresenter.utils.config import DeepPresenterConfig
from deeppresenter.utils.constants import (
    ASYNC_TOOL_TIMEOUT,
    CUTOFF_WARNING,
    LOGGING_LEVEL,
    MCP_CALL_TIMEOUT,
    PACKAGE_DIR,
    TOOL_CACHE,
    TOOL_CUTOFF_LEN,
    WORKSPACE_BASE,
)
from deeppresenter.utils.log import (
    debug,
    error,
    timer,
    warning,
)
from deeppresenter.utils.mcp_client import MCPClient
from deeppresenter.utils.typings import ChatMessage, MCPServer, Role

LOCAL_TOOL_SERVER = "local"


class ToolTiming(BaseModel):
    total_time: float = 0
    success_count: int = 0
    error_count: int = 0


class AgentEnv:
    def __init__(
        self,
        workspace: Path,
        config: DeepPresenterConfig,
        cutoff_len: int = TOOL_CUTOFF_LEN,
        event_reporter=None,
        preview_service=None,
    ):
        if isinstance(workspace, str):
            workspace = Path(workspace)
        self.workspace = workspace.absolute()
        self.cutoff_len = cutoff_len
        self.async_mode = config.async_tool_mode
        self.event_reporter = event_reporter
        self.preview_service = preview_service
        self.current_stage = None
        self.mcp_configs = []
        with open(config.mcp_config_file, encoding="utf-8") as f:
            for s in json.load(f):
                server = MCPServer(**s)
                if server.network and config.offline_mode:
                    continue
                self.mcp_configs.append(server)
        # Pass workspace-specific variables to client to avoid global env pollution
        host_workspace_base = os.environ.get("DEEPPRESENTER_HOST_WORKSPACE_BASE", None)
        if host_workspace_base:
            # calculate HOST_WORKSPACE for docker-in-docker volume mounting
            host_workspace = str(self.workspace).replace(
                str(WORKSPACE_BASE), host_workspace_base
            )
            debug(
                f"HOST WORKSPACE DETECTED: mapping {host_workspace} to {self.workspace}"
            )
        else:
            # assume paths are the same (local development)
            host_workspace = str(self.workspace)

        envs = {
            "WORKSPACE": str(self.workspace),
            "HOST_WORKSPACE": host_workspace,
            "WORKSPACE_ID": self.workspace.stem,
            "CONFIG_FILE": str(config.file_path),
            "FASTMCP_LOG_LEVEL": "CRITICAL",
            "PACKAGE_DIR": str(PACKAGE_DIR),
            "PYTHONWARNINGS": "ignore",
        }
        if config.offline_mode:
            envs["OFFLINE_MODE"] = "1"
        self.client = MCPClient(envs=envs)
        # caching overlong content
        self.timing_dict = defaultdict(ToolTiming)
        self._local_tools: dict[str, Callable] = {}
        self._tools_dict: dict[str, dict] = {}
        self._server_tools = defaultdict(list)
        self._tool_to_server = {}
        self.tool_history: list[tuple[ToolCall, ChatMessage]] = []
        self.tool_history_file = self.workspace / ".history" / "tool_history.jsonl"
        self._async_tool_tasks: dict[str, asyncio.Task[CallToolResult]] = {}
        self._async_tool_counter = 0
        if self.async_mode:
            self._register_async_tools()

    def _requires_docker(self) -> bool:
        return any(
            server.command == "docker" or server.name == "sandbox"
            for server in self.mcp_configs
        )

    async def tool_execute(
        self,
        tool_call: ToolCall,
    ):
        start_time = time.time()
        arguments: dict | None = None
        try:
            if len(tool_call.function.arguments) != 0:
                arguments = json.loads(tool_call.function.arguments)
                try:
                    assert (
                        jsonschema.validate(
                            arguments,
                            self._tools_dict[tool_call.function.name]["function"][
                                "parameters"
                            ],
                        )
                        is None
                    )
                except jsonschema.ValidationError as e:
                    raise ValueError(f"Input validation error: {e.message}") from e

            await self._report_tool_started(tool_call, arguments)
            result = await self._execute_tool(tool_call.function.name, arguments)
        except KeyError:
            result = CallToolResult(
                content=[
                    TextContent(
                        text=f"Tool `{tool_call.function.name}` not found.", type="text"
                    )
                ],
                isError=True,
            )
        except TimeoutError:
            result = CallToolResult(
                content=[
                    TextContent(
                        text=f"Tool `{tool_call.function.name}` execution timed out after {MCP_CALL_TIMEOUT} seconds.",
                        type="text",
                    )
                ],
                isError=True,
            )
        except Exception as e:
            result = CallToolResult(
                content=[
                    TextContent(
                        text=f"Tool `{tool_call.function.name}` execution failed with error: {e}",
                        type="text",
                    )
                ],
                isError=True,
            )
        finally:
            elapsed = time.time() - start_time
            debug(
                f"Tool `{tool_call.function.name}` execution took {elapsed:.2f} seconds"
            )
            self.timing_dict[tool_call.function.name].total_time += elapsed
            await self._report_tool_finished(tool_call, arguments, elapsed, result)
        if result.isError:
            self.timing_dict[tool_call.function.name].error_count += 1
            warning(
                f"Tool `{tool_call.function.name}` with params:`{tool_call.function.arguments}` encountered error: {result.content}"
            )
        else:
            self.timing_dict[tool_call.function.name].success_count += 1

        if not result.content or any(
            c.type not in ["image", "text"] for c in result.content
        ):
            raise ValueError(
                f"Only text/image blocks are supported currently. While getting {result.content} from {tool_call.function.name}"
            )
        content = []
        for block in result.content:
            if block.type == "text":
                assert isinstance(block, TextContent)
                if len(block.text) > self.cutoff_len:
                    truncated = block.text[: self.cutoff_len]
                    truncated = truncated[: truncated.rfind("\n")]

                    # checking if we are reading from local file
                    if tool_call.function.name == "read_file":
                        local_file = arguments["path"]
                    else:
                        hash_id = uuid.uuid4().hex[:4]
                        local_file = (
                            self.workspace / f"{tool_call.function.name}_{hash_id}.txt"
                        )
                        local_file.write_text(block.text)

                    truncated += CUTOFF_WARNING.format(
                        line=truncated.count("\n"), resource_id=str(local_file)
                    )
                    block.text = truncated

                content.append(
                    {
                        "type": "text",
                        "text": block.text,
                    }
                )
            elif block.type == "image":
                assert isinstance(block, ImageContent)
                content.append(
                    {
                        "type": "image_url",
                        "image_url": {"url": block.data},
                    }
                )
        msg = ChatMessage(
            role=Role.TOOL,
            content=content,
            from_tool=tool_call.function,
            tool_call_id=tool_call.id,
            is_error=result.isError,
        )
        self.tool_history.append((tool_call, msg))
        return msg

    async def _report_tool_started(
        self, tool_call: ToolCall, arguments: dict | None
    ) -> None:
        if self.event_reporter is None:
            return
        try:
            await self.event_reporter.tool_started(
                tool_call.function.name,
                stage=self.current_stage,
                payload={
                    "tool_call_id": tool_call.id,
                    "has_arguments": arguments is not None,
                },
            )
        except Exception as e:
            warning(f"Failed to report tool start event: {e}")

    async def _report_tool_finished(
        self,
        tool_call: ToolCall,
        arguments: dict | None,
        elapsed: float,
        result: CallToolResult,
    ) -> None:
        if self.event_reporter is None:
            await self._maybe_render_html_preview(tool_call, arguments, result)
            await self._maybe_capture_template_slide(tool_call, arguments, result)
            return
        try:
            if result.isError:
                await self.event_reporter.tool_failed(
                    tool_call.function.name,
                    elapsed=elapsed,
                    stage=self.current_stage,
                    error=self._tool_result_text(result),
                    payload={"tool_call_id": tool_call.id},
                )
            else:
                await self.event_reporter.tool_completed(
                    tool_call.function.name,
                    elapsed=elapsed,
                    stage=self.current_stage,
                    payload={"tool_call_id": tool_call.id},
                )
            await self._maybe_render_html_preview(tool_call, arguments, result)
            await self._maybe_capture_template_slide(tool_call, arguments, result)
        except Exception as e:
            warning(f"Failed to report tool finish event: {e}")

    async def _maybe_render_html_preview(
        self,
        tool_call: ToolCall,
        arguments: dict | None,
        result: CallToolResult,
    ) -> None:
        if (
            self.preview_service is None
            or result.isError
            or tool_call.function.name != "inspect_slide"
            or not arguments
            or "html_file" not in arguments
        ):
            return
        html_path = None
        slide_id = None
        slide_index = None
        aspect_ratio = arguments.get("aspect_ratio", "16:9")
        try:
            from deeppresenter.server.services.preview import (
                artifact_url,
                parse_slide_index,
                stable_slide_id,
            )

            html_path = Path(arguments["html_file"])
            if not html_path.is_absolute():
                html_path = self.workspace / html_path
            slide_index = parse_slide_index(html_path)
            slide_id = stable_slide_id(self.workspace.stem, slide_index)
            if self.event_reporter is not None:
                await self.event_reporter.slide_started(slide_id, slide_index)
            artifact = await self.preview_service.render_html_slide(
                self.workspace.stem,
                html_path,
                aspect_ratio=aspect_ratio,
                slide_index=slide_index,
                slide_id=slide_id,
            )
            if self.event_reporter is not None:
                await self.event_reporter.slide_preview_ready(
                    artifact.slide_id,
                    artifact.index,
                    artifact_url(artifact.task_id, artifact.preview_path),
                )
                await self.event_reporter.slide_completed(
                    artifact.slide_id,
                    artifact.index,
                )
        except Exception as e:
            if (
                self.event_reporter is not None
                and slide_id is not None
                and slide_index is not None
            ):
                try:
                    await self.event_reporter.slide_failed(
                        slide_id,
                        slide_index,
                        f"第 {slide_index} 页预览生成失败：{e}",
                        payload={
                            "error": str(e),
                            "html_file": str(html_path) if html_path else None,
                            "aspect_ratio": aspect_ratio,
                            "source_preserved": True,
                        },
                    )
                except Exception as report_error:
                    warning(f"Failed to report slide preview failure: {report_error}")
            warning(f"Failed to render slide preview: {e}")

    async def _maybe_capture_template_slide(
        self,
        tool_call: ToolCall,
        arguments: dict | None,
        result: CallToolResult,
    ) -> None:
        """当 ``generate_slide`` 工具成功后，持久化模板模式 SlideArtifact。

        与 ``_maybe_render_html_preview`` 对称：HTML 模式通过 inspect_slide
        获取预览，模板模式通过 generate_slide 获取结构化页面数据。
        """
        if (
            self.preview_service is None
            or result.isError
            or tool_call.function.name != "generate_slide"
        ):
            return
        slide_id = None
        slide_index = None
        try:
            from deeppresenter.server.services.preview import (
                artifact_url,
                stable_slide_id,
            )

            # 从工具参数和结果中提取 slide_data
            slide_data: dict = {}
            if arguments:
                slide_data.update(arguments)
            # 结果文本可能包含 JSON，尝试解析
            result_text = self._tool_result_text(result)
            if result_text:
                try:
                    import json as _json
                    parsed = _json.loads(result_text)
                    if isinstance(parsed, dict):
                        slide_data.update(parsed)
                except (_json.JSONDecodeError, TypeError):
                    pass

            # 确定页码——从参数或上下文计数
            slide_index = slide_data.get("slide_index") or 1
            if isinstance(slide_index, str):
                slide_index = int(slide_index)
            slide_id = stable_slide_id(self.workspace.stem, slide_index)
            if self.event_reporter is not None:
                await self.event_reporter.slide_started(slide_id, slide_index)

            artifact = await self.preview_service.render_template_slide(
                self.workspace.stem,
                slide_index,
                slide_data,
                slide_id=slide_id,
                aspect_ratio=slide_data.get("aspect_ratio", "16:9"),
            )

            if self.event_reporter is not None:
                await self.event_reporter.slide_preview_ready(
                    artifact.slide_id,
                    artifact.index,
                    artifact_url(artifact.task_id, artifact.preview_path),
                )
                await self.event_reporter.slide_completed(
                    artifact.slide_id,
                    artifact.index,
                )
        except Exception as e:
            if (
                self.event_reporter is not None
                and slide_id is not None
                and slide_index is not None
            ):
                try:
                    await self.event_reporter.slide_failed(
                        slide_id,
                        slide_index,
                        f"模板模式第 {slide_index} 页持久化失败：{e}",
                        payload={
                            "error": str(e),
                            "source_preserved": True,
                        },
                    )
                except Exception as report_error:
                    warning(f"Failed to report template slide failure: {report_error}")
            warning(f"Failed to capture template slide: {e}")

    @staticmethod
    def _tool_result_text(result: CallToolResult) -> str:
        texts = []
        for block in result.content:
            if getattr(block, "type", None) == "text":
                texts.append(block.text)
        return "\n".join(texts)[:500]

    async def __aenter__(self):
        if self._requires_docker():
            try:
                client = docker.from_env()
                container = client.containers.get(self.workspace.stem)
                warning(
                    f"Found duplicated sandbox container id={self.workspace.stem}, killed."
                )
                container.remove(force=True)
            # happend if cannot find the container
            except NotFound:
                pass
            except DockerException as e:
                error(f"Docker is not accessible: {e}.")
                sys.exit(1)
            except Exception as e:
                error(f"Unexpected error when launching docker containers: {e}.")
                sys.exit(1)

        with timer("Connecting MCP servers"):
            await asyncio.gather(
                *[self.connect_server(server) for server in self.mcp_configs]
            )

        if LOGGING_LEVEL <= logging.INFO:
            debug(
                f"Found {len(self._tools_dict)} tools, writing to {TOOL_CACHE}\nTools: {', '.join(self._tools_dict.keys())}"
            )
            with open(TOOL_CACHE, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "server_tools": self._server_tools,
                        "tool_specs": list(self._tools_dict.values()),
                    },
                    f,
                    ensure_ascii=False,
                    indent=2,
                )
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Clean up all MCP connections and resources"""
        for server_name in list(self._server_tools.keys()):
            await self.disconnect_server(server_name)
        self.tool_history_file.parent.mkdir(parents=True, exist_ok=True)
        with open(self.tool_history_file, "a", encoding="utf-8") as f:
            for tool_call, msg in self.tool_history:
                f.write(
                    json.dumps(
                        [tool_call.model_dump(), msg.model_dump()], ensure_ascii=False
                    )
                    + "\n"
                )
        with (self.workspace / ".history" / "tools_time_cost.json").open(
            "w", encoding="utf-8"
        ) as f:
            timing_data = {
                name: timing.model_dump()
                for name, timing in sorted(
                    self.timing_dict.items(),
                    key=lambda x: x[1].total_time,
                    reverse=True,
                )
            }
            json.dump(
                timing_data,
                f,
                ensure_ascii=False,
                indent=2,
            )
        debug(
            f"Agent Environment exited successfully, interaction history saved to: {self.tool_history_file}."
        )

    async def connect_server(self, server: MCPServer):
        """Connect to a single MCP server and register its tools."""
        name = server.name
        await self.client.connect_server(name, server)
        debug(f"Connected to server {name}")

        keep_tools = server.keep_tools
        exclude_tools = set(server.exclude_tools)

        tools_dict = await self.client.list_tools(name)
        for tool_name, tool_info in tools_dict.items():
            if (
                keep_tools is None or tool_name in keep_tools
            ) and tool_name not in exclude_tools:
                tool = {
                    "type": "function",
                    "function": {
                        "name": tool_name,
                        "description": tool_info.description,
                        "parameters": tool_info.inputSchema,
                    },
                }
                self._tools_dict[tool_name] = tool
                self._server_tools[name].append(tool_name)
                self._tool_to_server[tool_name] = name

    async def disconnect_server(self, server_name: str):
        """Disconnect a single MCP server and clean up its tools."""
        if server_name not in self._server_tools:
            return
        for tool_name in self._server_tools[server_name]:
            self._tools_dict.pop(tool_name, None)
            self._tool_to_server.pop(tool_name, None)
        del self._server_tools[server_name]
        if server_name == LOCAL_TOOL_SERVER:
            return
        await self.client._close_server(server_name)
        debug(f"Disconnected from server {server_name}")

    def get_server_tools(self, server_name: str) -> list[dict]:
        tools = []
        for tool_name in self._server_tools[server_name]:
            tools.append(self._tools_dict[tool_name])
        return tools

    def register_tool(
        self,
        func: Callable,
    ) -> None:
        """Register a callable (function or bound method) as a tool.

        The JSON Schema for parameters is auto-generated from type hints.
        Supports both sync and async callables.
        """
        tool_name = func.__name__
        tool_desc = inspect.getdoc(func) or ""
        schema = get_cached_typeadapter(func).json_schema()
        schema = compress_schema(schema, prune_titles=True)
        self._tools_dict[tool_name] = {
            "type": "function",
            "function": {
                "name": tool_name,
                "description": tool_desc,
                "parameters": schema,
            },
        }
        self._local_tools[tool_name] = func
        self._tool_to_server[tool_name] = LOCAL_TOOL_SERVER
        if tool_name in self._server_tools[LOCAL_TOOL_SERVER]:
            raise ValueError(f"Tool {tool_name} is already registered.")
        self._server_tools[LOCAL_TOOL_SERVER].append(tool_name)

    async def _call_local_tool(
        self, name: str, arguments: dict | None
    ) -> CallToolResult:
        """Execute a locally registered tool and wrap the result."""
        func = self._local_tools[name]
        kwargs = arguments or {}
        try:
            raw = (
                await func(**kwargs)
                if inspect.iscoroutinefunction(func)
                else func(**kwargs)
            )
            if isinstance(raw, CallToolResult):
                return raw
        except Exception as e:
            return CallToolResult(
                content=[
                    TextContent(
                        text=f"Tool `{name}` execution failed with error: {e}",
                        type="text",
                    )
                ],
                isError=True,
            )
        return CallToolResult(
            content=[TextContent(text=str(raw), type="text")],
            isError=False,
        )

    async def _execute_tool(
        self, tool_name: str, arguments: dict | None
    ) -> CallToolResult:
        if not self.async_mode or tool_name in {"gather", "finalize", "inspect_slide"}:
            return await self._execute_tool_once(tool_name, arguments)
        task = asyncio.create_task(self._execute_tool_once(tool_name, arguments))
        done, _ = await asyncio.wait({task}, timeout=ASYNC_TOOL_TIMEOUT)
        if task in done:
            return task.result()
        self._async_tool_counter += 1
        task_id = f"task_{self._async_tool_counter:04d}"
        self._async_tool_tasks[task_id] = task
        return CallToolResult(
            content=[
                TextContent(
                    text=f"Tool `{tool_name}` is still running after {ASYNC_TOOL_TIMEOUT} seconds as `{task_id}`. Call `gather` later to get the results.",
                    type="text",
                )
            ],
            isError=False,
        )

    def _register_async_tools(self) -> None:
        async def gather() -> CallToolResult:
            """
            Wait for all background tasks to finish and gather the results.
            """
            if not self._async_tool_tasks:
                return CallToolResult(
                    content=[TextContent(text="No background tasks.", type="text")],
                    isError=False,
                )
            tasks = self._async_tool_tasks
            self._async_tool_tasks = {}
            results = await asyncio.gather(*tasks.values(), return_exceptions=True)
            texts = [
                f"`{task_id}` result:\n" + "\n".join(c.text for c in result.content)
                for task_id, result in zip(tasks, results, strict=True)
            ]
            return CallToolResult(
                content=[TextContent(text="\n\n".join(texts), type="text")],
            )

        self.register_tool(gather)

    async def _execute_tool_once(
        self, tool_name: str, arguments: dict | None
    ) -> CallToolResult:
        if tool_name in self._local_tools:
            return await self._call_local_tool(tool_name, arguments)
        server_id = self._tool_to_server[tool_name]
        return await self.client.tool_execute(server_id, tool_name, arguments)
