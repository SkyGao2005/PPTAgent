import asyncio
import json
import uuid
from abc import abstractmethod
from collections.abc import AsyncGenerator
from datetime import datetime
from pathlib import Path
from typing import Any, Literal

import jsonlines
import yaml
from jinja2 import Template
from jinja2.runtime import StrictUndefined
from openai.types.chat.chat_completion_message import ChatCompletionMessage
from openai.types.chat.chat_completion_message_function_tool_call import (
    ChatCompletionMessageFunctionToolCall as ToolCall,
)
from pydantic import BaseModel

from deeppresenter.agents.env import AgentEnv
from deeppresenter.utils.config import (
    LLM,
    ContextWindowExceededError,
    DeepPresenterConfig,
    estimate_chat_tokens,
    get_json_from_response,
)
from deeppresenter.utils.constants import (
    AGENT_PROMPT,
    CONTEXT_MODE_PROMPT,
    CONTINUE_MSG,
    HALF_BUDGET_NOTICE_MSG,
    LAST_ITER_MSG,
    MA_RESEACHER_PROMPT,
    MA_RRESENTER_PROMPT,
    MAX_LOGGING_LENGTH,
    MAX_TOOLCALL_PER_TURN,
    OFFLINE_PROMPT,
    PACKAGE_DIR,
    URGENT_BUDGET_NOTICE_MSG,
)
from deeppresenter.utils.log import (
    debug,
    info,
    timer,
)
from deeppresenter.utils.typings import (
    ChatMessage,
    ContextLayer,
    Cost,
    InputRequest,
    Role,
    RoleConfig,
)


class Agent:
    def __init__(
        self,
        config: DeepPresenterConfig,
        agent_env: AgentEnv,
        workspace: Path,
        language: Literal["zh", "en"],
        config_file: str | Path | None = None,
        keep_reasoning: bool = True,
        max_turns: int | None = None,
    ):
        self.name = self.__class__.__name__
        self.cost = Cost()
        self.context_length = 0
        self.context_warning = 0
        self.workspace = workspace
        self.agent_env = agent_env
        self.language = language
        self.keep_reasoning = keep_reasoning
        self.context_folding = config.context_folding
        self.max_context_turns = config.max_context_folds
        self.max_turns = max_turns
        self.turn_count = 0
        role_config_file = (
            Path(config_file)
            if config_file
            else PACKAGE_DIR / "roles" / f"{self.name}.yaml"
        )
        if not role_config_file.exists():
            raise FileNotFoundError(
                f"Cannot found role config file at: {role_config_file} "
            )

        # Setting basic context
        workspace.mkdir(parents=True, exist_ok=True)
        with open(role_config_file, encoding="utf-8") as f:
            config_data = yaml.safe_load(f)
        self.role_config = RoleConfig(**config_data)
        self.llm: LLM = config[self.role_config.use_model]
        self.model = self.llm.model_name
        explicit_legacy_limit = (
            config.context_window if config.has_legacy_context_window_override else None
        )
        self.context_limit_tokens = (
            explicit_legacy_limit or self.llm.context_limit_tokens
        )
        self.reserved_output_tokens = self.llm.effective_reserved_output_tokens
        self.context_safety_margin_tokens = self.llm.context_safety_margin_tokens
        self.input_token_budget = max(
            1,
            self.context_limit_tokens
            - self.reserved_output_tokens
            - self.context_safety_margin_tokens,
        )
        # Kept as an alias for integrations that inspect the legacy attribute.
        self.context_window = self.input_token_budget
        self.fold_trigger_ratio = self.llm.fold_trigger_ratio
        self.template_context_max_tokens = self.llm.template_context_max_tokens
        self.max_template_reference_images = self.llm.max_template_reference_images
        self.compaction_input_max_tokens = self.llm.compaction_input_max_tokens
        self._setup_toolset()
        if language not in self.role_config.system:
            raise ValueError(f"Language '{language}' not found in system prompts")
        self.error_history: list[ToolCall | ChatMessage] = []
        self.research_iter = 0
        if config.context_folding:
            self.context_warning = -1

        # Setting tools and interative context
        self.system = self.role_config.system[language]
        self.prompt: Template = Template(
            self.role_config.instruction, undefined=StrictUndefined
        )
        # ? for those agents equipped with sandbox only
        if any(t["function"]["name"] == "execute_command" for t in self.tools):
            self.system += AGENT_PROMPT.format(
                workspace=self.workspace,
                cutoff_len=self.agent_env.cutoff_len,
                time=datetime.now().strftime("%Y-%m-%d"),
                max_toolcall_per_turn=MAX_TOOLCALL_PER_TURN,
            )

        if any(t["function"]["name"] == "delegate_subagent" for t in self.tools):
            if self.name == "Research":
                self.system += MA_RESEACHER_PROMPT
            elif self.name == "Design":
                self.system += MA_RRESENTER_PROMPT

        if config.offline_mode:
            self.system += OFFLINE_PROMPT

        if config.context_folding:
            self.system += CONTEXT_MODE_PROMPT

        self.chat_history: list[ChatMessage] = [
            ChatMessage(
                role=Role.SYSTEM,
                content=self.system,
                extra_info={"context_layer": ContextLayer.PINNED.value},
            )
        ]
        self._pinned_context: dict[str, ChatMessage] = {
            self.chat_history[0].id: self.chat_history[0]
        }
        available_tools = [tool["function"]["name"] for tool in self.tools]
        debug(
            f"{self.name} Agent got {len(self.tools)} tools: {', '.join(available_tools)}"
        )

    def _setup_toolset(self):
        toolset = self.role_config.toolset
        if toolset.include_tool_servers == "all":
            toolset.include_tool_servers = list(self.agent_env._server_tools)
        for server in toolset.include_tool_servers:
            assert server in self.agent_env._server_tools, (
                f"Server {server} is not available"
            )
        self.tools = []
        for server in toolset.include_tool_servers:
            if server not in toolset.exclude_tool_servers:
                for tool in self.agent_env._server_tools[server]:
                    if tool not in toolset.exclude_tools:
                        self.tools.append(self.agent_env._tools_dict[tool])

        for tool_name, tool in self.agent_env._tools_dict.items():
            if tool_name in toolset.include_tools:
                self.tools.append(tool)

    async def chat(
        self,
        message: ChatMessage,
        response_format: type[BaseModel] | None = None,
        **chat_kwargs,
    ) -> ChatMessage:
        if len(self.chat_history) == 1:
            self.chat_history.append(
                ChatMessage(
                    role=Role.USER,
                    content=self.prompt.render(**chat_kwargs),
                    extra_info={"context_layer": ContextLayer.PINNED.value},
                )
            )
            self.log_message(self.chat_history[-1])
        self.chat_history.append(message)
        self.log_message(self.chat_history[-1])
        with timer(f"{self.name} Agent LLM chat"):
            response = await self._run_model(
                response_format=response_format,
            )
            self._record_usage(response.usage)
            self.chat_history.append(
                ChatMessage(
                    role=Role.ASSISTANT,
                    content=response.choices[0].message.content,
                    cost=response.usage,
                    reasoning=getattr(response.choices[0].message, "reasoning", None)
                    if self.keep_reasoning
                    else None,
                )
            )
            self._update_context_estimate()
            self.log_message(self.chat_history[-1])
            return self.chat_history[-1]

    async def action(
        self,
        **chat_kwargs,
    ):
        """Tool calling interface"""
        self.turn_count += 1
        if self.max_turns is not None:
            if self.turn_count > self.max_turns:
                raise RuntimeError(
                    f"{self.name} exceeded max turns: {self.turn_count - 1}/{self.max_turns}"
                )
            if self.max_turns - self.turn_count < 2:
                self.chat_history[-1].content.append(
                    {
                        "type": "text",
                        "text": f"You have only {self.max_turns - self.turn_count} turn left. Finish the remaing work soonly and call `finalize` immediately.",
                    }
                )

        if len(self.chat_history) == 1:
            self.chat_history.append(
                ChatMessage(
                    role=Role.USER,
                    content=self.prompt.render(**chat_kwargs),
                    extra_info={"context_layer": ContextLayer.PINNED.value},
                )
            )
            self.log_message(self.chat_history[-1])

        with timer(f"{self.name} Agent LLM call"):
            response = await self._run_model(tools=self.tools)
            self._record_usage(response.usage)
            agent_message: ChatCompletionMessage = response.choices[0].message
        self.chat_history.append(
            ChatMessage(
                role=Role.ASSISTANT,
                content=agent_message.content,
                cost=response.usage,
                tool_calls=agent_message.tool_calls,
                reasoning=getattr(agent_message, "reasoning", None)
                if self.keep_reasoning
                else None,
            )
        )
        self._update_context_estimate(self.tools)
        self.log_message(self.chat_history[-1])
        return self.chat_history[-1]

    def add_context_message(
        self,
        message: ChatMessage,
        layer: ContextLayer,
        *,
        template_context: bool = False,
    ) -> None:
        """Add a message with an explicit lifetime to the conversation context."""
        message.set_context_layer(layer, template_context=template_context)
        self.chat_history.append(message)
        if layer == ContextLayer.PINNED:
            self._pinned_context[message.id] = message

    def _record_usage(self, usage: Any | None) -> None:
        if usage is not None:
            self.cost += usage

    def _estimate_context(
        self,
        tools: list[dict[str, Any]] | None = None,
        messages: list[ChatMessage] | None = None,
        response_format: type[BaseModel] | None = None,
    ) -> int:
        return estimate_chat_tokens(
            self.chat_history if messages is None else messages,
            tools,
            self.llm.image_token_estimate,
            response_format,
        )

    def _update_context_estimate(
        self, tools: list[dict[str, Any]] | None = None
    ) -> int:
        self.context_length = self._estimate_context(tools)
        return self.context_length

    def _capture_pinned_context(self) -> None:
        for message in self.chat_history:
            if message.context_layer == ContextLayer.PINNED:
                self._pinned_context[message.id] = message

    def _reinject_pinned_context(self) -> None:
        """Restore pinned instructions/template context after any history rewrite."""
        self._capture_pinned_context()
        present_ids = {message.id for message in self.chat_history}
        missing = [
            message
            for message_id, message in self._pinned_context.items()
            if message_id not in present_ids
        ]
        if not missing:
            return

        system_end = 0
        while (
            system_end < len(self.chat_history)
            and self.chat_history[system_end].role == Role.SYSTEM
        ):
            system_end += 1
        self.chat_history[system_end:system_end] = missing

    def _validate_template_context(self) -> None:
        if not any(message.is_template_context for message in self.chat_history):
            return

        # Evict first so an over-fetching turn cannot trip the token budget with
        # reference images that are about to be retired anyway. The token check
        # then reports only genuine context-pack bloat.
        self._evict_excess_template_images()

        template_messages = [
            message for message in self.chat_history if message.is_template_context
        ]
        template_tokens = self._estimate_context(messages=template_messages)
        if template_tokens > self.template_context_max_tokens:
            raise ContextWindowExceededError(
                f"Pinned template context needs about {template_tokens} tokens, exceeding "
                f"template_context_max_tokens={self.template_context_max_tokens}. "
                "Build a smaller template context pack instead of truncating it."
            )

        # Eviction above enforces the cap; this remains a defensive invariant.
        image_count = sum(
            self._count_images(message) for message in template_messages
        )
        if image_count > self.max_template_reference_images:
            raise ContextWindowExceededError(
                f"Pinned template context contains {image_count} images, but only "
                f"{self.max_template_reference_images} reference images are allowed"
            )

    async def _preflight_context(
        self,
        tools: list[dict[str, Any]] | None = None,
        response_format: type[BaseModel] | None = None,
    ) -> int:
        """Fit history to the configured input budget before any model request."""
        self._reinject_pinned_context()
        self._validate_template_context()
        estimated_tokens = self._estimate_context(
            tools, response_format=response_format
        )
        self.context_length = estimated_tokens
        fold_target = max(1, int(self.input_token_budget * self.fold_trigger_ratio))

        while (
            self.context_folding
            and estimated_tokens > fold_target
            and self.research_iter < self.max_context_turns
        ):
            changed = await self.compact_history(target_tokens=fold_target)
            if not changed:
                break
            self._reinject_pinned_context()
            self._validate_template_context()
            estimated_tokens = self._estimate_context(
                tools, response_format=response_format
            )
            self.context_length = estimated_tokens

        if estimated_tokens > self.input_token_budget:
            raise ContextWindowExceededError(
                f"{self.name} needs about {estimated_tokens} input tokens, but only "
                f"{self.input_token_budget} remain after output reservation. Pinned or "
                "ephemeral context cannot be discarded."
            )
        return estimated_tokens

    async def _run_model(
        self,
        *,
        tools: list[dict[str, Any]] | None = None,
        response_format: type[BaseModel] | None = None,
    ):
        await self._preflight_context(tools, response_format)
        ephemeral_image_ids = {
            message.id
            for message in self.chat_history
            if message.context_layer == ContextLayer.EPHEMERAL
            and self._message_has_image_payload(message)
        }
        try:
            response = await self.llm.run(
                messages=self.chat_history,
                response_format=response_format,
                tools=tools,
            )
        except ContextWindowExceededError:
            # A provider may have a smaller real window than its configuration.
            # Fold once more and retry once; LLM.run itself never retries overflow.
            if not self.context_folding:
                raise
            recovered = await self.compact_history(
                target_tokens=max(1, self.input_token_budget // 2),
                keep_tail=2,
            )
            if not recovered:
                raise
            await self._preflight_context(tools, response_format)
            response = await self.llm.run(
                messages=self.chat_history,
                response_format=response_format,
                tools=tools,
                retry_times=1,
            )
        self._retire_ephemeral_images(ephemeral_image_ids)
        return response

    @staticmethod
    def _count_images(message: ChatMessage) -> int:
        return sum(
            block.get("type") in {"image", "image_url", "input_image"}
            for block in message.content
        )

    @staticmethod
    def _message_has_image_payload(message: ChatMessage) -> bool:
        return Agent._count_images(message) > 0

    def _evict_excess_template_images(self) -> None:
        """Retire the oldest pinned reference images so the newest survive.

        A single agent turn may fetch more reference pages than
        ``max_template_reference_images`` (e.g. parallel ``get_template_reference``
        calls). Rather than fail the task, downgrade the oldest ephemeral
        reference images to their textual placeholder, matching the ephemeral
        "view once" contract while keeping the most recently requested pages.
        """

        image_messages = [
            message
            for message in self.chat_history
            if message.is_template_context and self._message_has_image_payload(message)
        ]
        total = sum(self._count_images(message) for message in image_messages)
        if total <= self.max_template_reference_images:
            return
        # chat_history is oldest-first; drop from the front until within the cap.
        to_retire: set[str] = set()
        for message in image_messages:
            if total <= self.max_template_reference_images:
                break
            total -= self._count_images(message)
            to_retire.add(message.id)
        if to_retire:
            self._retire_ephemeral_images(to_retire)

    def _retire_ephemeral_images(self, message_ids: set[str]) -> None:
        """Remove inline image bytes after the model has consumed them once."""
        for message in self.chat_history:
            if message.id not in message_ids:
                continue
            remaining_blocks = [
                block
                for block in message.content
                if block.get("type") not in {"image", "image_url", "input_image"}
            ]
            if not any(block.get("type") == "text" for block in remaining_blocks):
                tool_name = message.from_tool.name if message.from_tool else "unknown"
                remaining_blocks.append(
                    {
                        "type": "text",
                        "text": (
                            f"Image payload from tool `{tool_name}` was consumed; "
                            f"reference tool_call_id={message.tool_call_id}."
                        ),
                    }
                )
            message.content = remaining_blocks
            message.set_context_layer(ContextLayer.SUMMARIZABLE)
            message.extra_info.pop("template_context", None)

    @abstractmethod
    def loop(
        self, req: InputRequest, *args, **kwargs
    ) -> AsyncGenerator[str | ChatMessage, None]:
        """
        Loop interface, return the message or the outcome filepath of the agent.
        """

    async def execute(self, tool_calls: list[ToolCall]) -> str | list[ChatMessage]:
        coros = []
        observations: list[ChatMessage] = []
        used_tools = set()
        finish_id = None
        outcome = None
        for t in tool_calls:
            arguments = t.function.arguments
            if len(arguments) == 0:
                arguments = None
            else:
                try:
                    assert len(tool_calls) <= MAX_TOOLCALL_PER_TURN, (
                        f"Too many tool calls ({len(tool_calls)}), max allowed is {MAX_TOOLCALL_PER_TURN}"
                    )
                    arguments = get_json_from_response(t.function.arguments)
                    assert isinstance(arguments, dict), (
                        f"Tool call arguments must be a dict or empty, while {arguments} is given"
                    )
                    if t.function.name == "finalize":
                        arguments["agent_name"] = self.name
                        finish_id = t.id
                        assert "outcome" in arguments, (
                            "Finalize tool call must have an outcome"
                        )
                        outcome = arguments["outcome"]
                    t.function.arguments = json.dumps(arguments, ensure_ascii=False)
                except AssertionError as e:
                    observations.append(
                        ChatMessage(
                            role=Role.TOOL,
                            content=str(e),
                            tool_call_id=t.id,
                            is_error=True,
                        )
                    )
                    info(f"Tool call `{t.function}` encountered error: {e}")
                    continue
            used_tools.add(t.function.name)
            info(f"{self.name} Agent calling tool `{t.function.name}`")
            coros.append(self.agent_env.tool_execute(t))

        observations.extend(await asyncio.gather(*coros))
        for obs in observations:
            if obs.has_image:
                is_template_reference = bool(
                    obs.from_tool and obs.from_tool.name == "get_template_reference"
                )
                obs.set_context_layer(
                    ContextLayer.EPHEMERAL,
                    template_context=is_template_reference,
                )
                if "gemini" in self.model.lower() or "qwen" in self.model.lower():
                    obs.role = Role.USER
                if "claude" in self.model.lower():
                    obs.content = Agent._claude_content_blocks(obs.content)

        self.chat_history.extend(observations)
        self._update_context_estimate(self.tools)

        tool_call_map = {t.id: t for t in tool_calls}
        for o in observations:
            if o.is_error:
                t = tool_call_map[o.tool_call_id]
                self.error_history.append(t)
                self.error_history.append(o)

        if finish_id is not None:
            for obs in observations:
                if obs.tool_call_id == finish_id and obs.text == outcome:
                    info(f"{self.name} Agent finished with result: {obs.text}")
                    return obs.text

        if observations and (
            self.context_warning == 0
            and self.context_length > self.context_window * 0.5
        ):
            self.context_warning += 1
            observations[0].content.insert(0, HALF_BUDGET_NOTICE_MSG)
        elif observations and (
            self.context_warning == 1
            and self.context_length > self.context_window * 0.8
        ):
            observations[0].content.insert(0, URGENT_BUDGET_NOTICE_MSG)
            self.context_warning = 2

        for obs in observations:
            self.log_message(obs)

        return observations

    @staticmethod
    def _claude_content_blocks(content: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Convert every OpenAI image block without dropping sibling text."""

        converted: list[dict[str, Any]] = []
        for block in content:
            if block.get("type") != "image_url":
                converted.append(block)
                continue
            image_url = block.get("image_url")
            data_uri = image_url.get("url") if isinstance(image_url, dict) else None
            if not isinstance(data_uri, str):
                raise ValueError("Claude image block has no data URI")
            header, separator, encoded = data_uri.partition(",")
            if (
                not separator
                or not header.startswith("data:")
                or ";base64" not in header
                or not encoded
            ):
                raise ValueError("Claude image block must contain base64 data URI")
            media_type = header.removeprefix("data:").split(";", 1)[0]
            converted.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": encoded,
                    },
                }
            )
        return converted

    def log_message(self, msg: ChatMessage):
        if len(msg.text) < MAX_LOGGING_LENGTH:
            debug(f"{self.name}: {msg.text}")
        else:
            debug(f"{self.name}: {msg.text[:MAX_LOGGING_LENGTH]}...")

    async def compact_history(
        self,
        keep_head: int = 10,
        keep_tail: int = 4,
        target_tokens: int | None = None,
    ) -> bool:
        """Fold summarizable messages using a bounded source request.

        Pinned, ephemeral, and external-reference messages are never summarized.
        The old implementation sent the already-overflowing full history to the
        summarizer; this implementation selects a bounded payload first.
        """
        del keep_head  # Retention is layer-driven; kept for API compatibility.
        if self.research_iter >= self.max_context_turns:
            return False

        self._reinject_pinned_context()
        summarizable = [
            message
            for message in self.chat_history
            if message.context_layer == ContextLayer.SUMMARIZABLE
        ]
        if not summarizable:
            return False

        target_tokens = target_tokens or max(
            1, int(self.input_token_budget * self.fold_trigger_ratio)
        )
        tail = self._compaction_tail(summarizable, keep_tail, target_tokens)
        tail_ids = {message.id for message in tail}
        candidates = [message for message in summarizable if message.id not in tail_ids]
        if not candidates:
            # A very large recent message must be foldable too.
            candidates = summarizable
            tail = []

        source_budget = min(
            self.compaction_input_max_tokens,
            max(128, self.llm.input_token_budget - 256),
        )
        source = self._bounded_summary_source(candidates, source_budget)
        if not source:
            return False

        self.save_history(message_only=True)
        summary_request = [
            ChatMessage(
                role=Role.SYSTEM,
                content=(
                    "Summarize the supplied conversation history as durable working "
                    "memory. Preserve concrete facts, decisions, artifact paths, errors, "
                    "and remaining work. Return summary text only."
                ),
            ),
            ChatMessage(
                role=Role.USER,
                content=f"Use {self.language} as the primary language.\n\n{source}",
            ),
        ]
        response = await self.llm.run(
            summary_request,
            retry_times=1,
        )
        self._record_usage(response.usage)
        summary_text = response.choices[0].message.content or ""
        summary_limit = max(128, min(2_000, target_tokens // 4))
        summary_text = self._truncate_to_estimated_tokens(summary_text, summary_limit)

        self.research_iter += 1
        summary_message = ChatMessage(
            id=f"context_fold_{uuid.uuid4().hex[:8]}",
            role=Role.USER,
            content=f"<context_summary>\n{summary_text}\n</context_summary>",
            extra_info={
                "context_layer": ContextLayer.SUMMARIZABLE.value,
                "context_fold": self.research_iter,
            },
        )
        summary_message.content.append(CONTINUE_MSG)
        if self.research_iter >= self.max_context_turns:
            summary_message.content.append(LAST_ITER_MSG)

        retained = [
            message
            for message in self.chat_history
            if message.context_layer != ContextLayer.SUMMARIZABLE
        ]
        self.chat_history = retained + [summary_message] + tail
        self._reinject_pinned_context()
        self._update_context_estimate(self.tools)
        debug(
            f"Summary of Context Fold {self.research_iter:02d}:\n"
            + summary_message.text
        )
        return True

    def _compaction_tail(
        self,
        messages: list[ChatMessage],
        keep_tail: int,
        target_tokens: int,
    ) -> list[ChatMessage]:
        if keep_tail <= 0:
            return []
        tail = messages[-keep_tail:]

        if tail and tail[0].role == Role.TOOL:
            tool_call_id = tail[0].tool_call_id
            for index in range(len(messages) - len(tail) - 1, -1, -1):
                candidate = messages[index]
                call_ids = {call.id for call in candidate.tool_calls or []}
                if tool_call_id in call_ids:
                    tail = messages[index:]
                    break

        tail_budget = max(128, target_tokens // 3)
        if self._estimate_context(messages=tail) > tail_budget:
            return []
        return tail

    def _bounded_summary_source(
        self, messages: list[ChatMessage], max_tokens: int
    ) -> str:
        max_chars = max(256, max_tokens * 2)
        blocks: list[str] = []
        remaining = max_chars
        for message in reversed(messages):
            block = f"[{message.role.value}]\n{message.text}\n"
            if len(block) <= remaining:
                blocks.append(block)
                remaining -= len(block)
                continue
            if not blocks and remaining > 64:
                half = max(24, (remaining - 40) // 2)
                blocks.append(
                    block[:half]
                    + "\n...[middle omitted before summarization]...\n"
                    + block[-half:]
                )
            break
        source = "\n".join(reversed(blocks))
        return self._truncate_to_estimated_tokens(source, max_tokens)

    def _truncate_to_estimated_tokens(self, text: str, max_tokens: int) -> str:
        if estimate_chat_tokens(text) <= max_tokens:
            return text
        marker = "\n...[truncated to context budget]...\n"
        low, high = 0, len(text) // 2
        while low < high:
            half = (low + high + 1) // 2
            candidate = text[:half] + marker + text[-half:]
            if estimate_chat_tokens(candidate) <= max_tokens:
                low = half
            else:
                high = half - 1
        if low == 0:
            return marker.strip()
        return text[:low] + marker + text[-low:]

    def save_history(self, hist_dir: Path | None = None, message_only: bool = False):
        hist_dir = hist_dir or self.workspace / ".history"
        hist_dir.mkdir(parents=True, exist_ok=True)

        history_file = hist_dir / f"{self.name}-history.jsonl"
        if self.research_iter >= 0:
            history_file = (
                hist_dir / f"{self.name}-{self.research_iter:02d}-history.jsonl"
            )
        with jsonlines.open(history_file, mode="w") as writer:
            for message in self.chat_history:
                writer.write(self._history_record(message))

        if message_only:
            return

        config_file = hist_dir / f"{self.name}-config.json"
        with open(config_file, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "name": self.name,
                    "model": self.model,
                    "context_window": self.context_length,
                    "cost": self.cost.model_dump(),
                    "tools": self.tools,
                },
                f,
                ensure_ascii=False,
                indent=2,
            )

        if self.error_history:
            error_file = hist_dir / f"{self.name}-errors.jsonl"
            with jsonlines.open(error_file, mode="w") as writer:
                for msg in self.error_history:
                    if isinstance(msg, ChatMessage):
                        writer.write(self._history_record(msg))
                    else:
                        writer.write(msg.model_dump())

        debug(
            f"{self.name} done | cost:{self.cost} ctx:{self.context_length} | history:{history_file.name} config:{config_file.name}"
        )

    def _history_record(self, message: ChatMessage) -> dict[str, Any]:
        """Serialize history without persisting inline base64 image payloads."""
        record = message.model_dump(mode="json")
        content = record.get("content") or []
        if not any(
            block.get("type") in {"image", "image_url", "input_image"}
            for block in content
        ):
            return record
        record["content"] = [
            block
            for block in content
            if block.get("type") not in {"image", "image_url", "input_image"}
        ]
        record["content"].append(
            {
                "type": "text",
                "text": (
                    "[Inline image payload omitted from persisted history; "
                    f"tool_call_id={message.tool_call_id}]"
                ),
            }
        )
        return record
