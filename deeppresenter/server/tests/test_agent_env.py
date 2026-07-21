import json
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult, ImageContent, TextContent
from openai.types.chat.chat_completion_message_function_tool_call import (
    ChatCompletionMessageFunctionToolCall as ToolCall,
)

from deeppresenter.agents.env import AgentEnv


def _config(mcp_config_file):
    return SimpleNamespace(
        async_tool_mode=False,
        offline_mode=False,
        mcp_config_file=str(mcp_config_file),
        file_path=str(mcp_config_file.parent / "config.yaml"),
    )


@pytest.mark.asyncio
async def test_agent_env_without_sandbox_does_not_touch_docker(tmp_path, monkeypatch):
    mcp_file = tmp_path / "mcp.json"
    mcp_file.write_text("[]", encoding="utf-8")

    def fail_if_called():
        raise AssertionError("docker.from_env should not be called")

    monkeypatch.setattr("deeppresenter.agents.env.docker.from_env", fail_if_called)

    async with AgentEnv(tmp_path / "workspace", _config(mcp_file)) as env:
        assert env._requires_docker() is False


def test_agent_env_requires_docker_for_sandbox_server(tmp_path):
    mcp_file = tmp_path / "mcp.json"
    mcp_file.write_text(
        json.dumps(
            [
                {
                    "name": "sandbox",
                    "description": "sandbox",
                    "command": "docker",
                    "args": ["run", "deeppresenter-sandbox"],
                }
            ]
        ),
        encoding="utf-8",
    )

    env = AgentEnv(tmp_path / "workspace", _config(mcp_file))

    assert env._requires_docker() is True


@pytest.mark.asyncio
async def test_agent_env_accepts_multi_block_tool_result(tmp_path):
    mcp_file = tmp_path / "mcp.json"
    mcp_file.write_text("[]", encoding="utf-8")

    env = AgentEnv(tmp_path / "workspace", _config(mcp_file))
    env._tools_dict = {
        "read_file": {
            "function": {
                "parameters": {
                    "type": "object",
                    "properties": {"path": {"type": "string"}},
                    "required": ["path"],
                }
            }
        }
    }

    async def fake_execute_tool(tool_name, arguments):
        return CallToolResult(
            content=[
                TextContent(type="text", text="Image file: slide_01.png"),
                ImageContent(
                    type="image",
                    data="data:image/png;base64,abc",
                    mimeType="image/png",
                ),
            ]
        )

    env._execute_tool = fake_execute_tool
    tool_call = ToolCall(
        id="call_1",
        type="function",
        function={"name": "read_file", "arguments": '{"path": "slide_01.png"}'},
    )

    message = await env.tool_execute(tool_call)

    assert message.content == [
        {"type": "text", "text": "Image file: slide_01.png"},
        {
            "type": "image_url",
            "image_url": {"url": "data:image/png;base64,abc"},
        },
    ]
