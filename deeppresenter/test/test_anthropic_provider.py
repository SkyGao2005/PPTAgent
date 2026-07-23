"""The Anthropic wire format is translated at the endpoint boundary.

Every caller in this codebase reads `choices[0].message`, so the second format
has to arrive already shaped like a ChatCompletion.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from pydantic import BaseModel

from deeppresenter.utils.config import (
    Endpoint,
    anthropic_content,
    from_anthropic_message,
    to_anthropic_messages,
    to_anthropic_tools,
)


class _Block:
    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


class _Reply:
    def __init__(self, content: list[_Block], stop_reason: str = "end_turn") -> None:
        self.id = "msg_01"
        self.content = content
        self.stop_reason = stop_reason
        self.usage = _Block(input_tokens=11, output_tokens=7)


def test_system_turns_are_lifted_out_of_the_conversation() -> None:
    """Anthropic carries the system prompt beside the messages, not inside."""

    messages, system = to_anthropic_messages(
        [
            {"role": "system", "content": "You design slides."},
            {"role": "user", "content": "第一页"},
            {"role": "system", "content": "Keep it short."},
            {"role": "assistant", "content": "好的"},
        ]
    )

    assert system == "You design slides.\n\nKeep it short."
    assert [m["role"] for m in messages] == ["user", "assistant"]


def test_a_tool_round_trip_survives_translation() -> None:
    """The agent hands its ChatMessage history through on every turn.

    Anthropic has no tool role: the call rides inside the assistant's own
    content and the result comes back inside a user turn.
    """

    from openai.types.chat import ChatCompletionMessageFunctionToolCall

    from deeppresenter.utils.typings import ChatMessage, Role

    messages, _ = to_anthropic_messages(
        [
            ChatMessage(role=Role.USER, content="做第一页"),
            ChatMessage(
                role=Role.ASSISTANT,
                content="我看一下",
                tool_calls=[
                    ChatCompletionMessageFunctionToolCall(
                        id="call_1",
                        type="function",
                        function={
                            "name": "inspect_slide",
                            "arguments": '{"html_file": "a.html"}',
                        },
                    )
                ],
            ),
            ChatMessage(role=Role.TOOL, content="ok", tool_call_id="call_1"),
        ]
    )

    assert [m["role"] for m in messages] == ["user", "assistant", "user"]
    call = messages[1]["content"][-1]
    assert call["type"] == "tool_use"
    assert call["id"] == "call_1"
    assert call["input"] == {"html_file": "a.html"}
    result = messages[2]["content"][0]
    assert result["type"] == "tool_result"
    assert result["tool_use_id"] == "call_1"


def test_a_failed_tool_result_is_flagged() -> None:
    from deeppresenter.utils.typings import ChatMessage, Role

    messages, _ = to_anthropic_messages(
        [
            ChatMessage(role=Role.USER, content="go"),
            ChatMessage(
                role=Role.TOOL, content="boom", tool_call_id="c1", is_error=True
            ),
        ]
    )

    assert messages[1]["content"][0]["is_error"] is True


def test_images_move_from_a_data_url_to_an_explicit_source() -> None:
    blocks = anthropic_content(
        [
            {"type": "text", "text": "这一页哪里不对"},
            {
                "type": "image_url",
                "image_url": {"url": "data:image/png;base64,QUJD"},
            },
        ]
    )

    assert blocks[0] == {"type": "text", "text": "这一页哪里不对"}
    assert blocks[1] == {
        "type": "image",
        "source": {"type": "base64", "media_type": "image/png", "data": "QUJD"},
    }


def test_a_plain_string_body_is_left_alone() -> None:
    assert anthropic_content("just text") == "just text"


def test_tools_lose_the_function_envelope() -> None:
    converted = to_anthropic_tools(
        [
            {
                "type": "function",
                "function": {
                    "name": "inspect_slide",
                    "description": "Render one slide",
                    "parameters": {"type": "object", "properties": {"p": {}}},
                },
            }
        ]
    )

    assert converted == [
        {
            "name": "inspect_slide",
            "description": "Render one slide",
            "input_schema": {"type": "object", "properties": {"p": {}}},
        }
    ]


def test_a_reply_becomes_a_chat_completion() -> None:
    completion = from_anthropic_message(
        _Reply([_Block(type="text", text="完成了")]), "claude-opus-4-8"
    )

    message = completion.choices[0].message
    assert message.content == "完成了"
    assert message.tool_calls is None
    assert completion.choices[0].finish_reason == "stop"
    assert completion.usage.prompt_tokens == 11


def test_tool_use_becomes_an_openai_tool_call() -> None:
    """Tool arguments cross as JSON text, which is what the callers parse."""

    completion = from_anthropic_message(
        _Reply(
            [
                _Block(type="text", text="先看一下"),
                _Block(
                    type="tool_use",
                    id="toolu_1",
                    name="inspect_slide",
                    input={"html_file": "slide_01.html"},
                ),
            ],
            stop_reason="tool_use",
        ),
        "claude-opus-4-8",
    )

    call = completion.choices[0].message.tool_calls[0]
    assert call.function.name == "inspect_slide"
    assert json.loads(call.function.arguments) == {"html_file": "slide_01.html"}
    assert completion.choices[0].finish_reason == "tool_calls"


class _Schema(BaseModel):
    title: str


@pytest.mark.asyncio
async def test_the_request_drops_parameters_the_messages_api_rejects() -> None:
    # The SDK is an optional dependency, like litellm.
    pytest.importorskip("anthropic")

    """One sampling block has to serve both wire formats.

    `temperature` is a 400 on Opus 4.7 and later, and `max_tokens` is required
    where the OpenAI route leaves it optional.
    """

    endpoint = Endpoint(
        provider="anthropic",
        model="claude-opus-4-8",
        api_key="k",
        sampling_parameters={"temperature": 0.7, "top_p": 0.9},
        reserved_output_tokens=4096,
    )
    sent: dict[str, Any] = {}

    class _Messages:
        async def create(self, **kwargs: Any) -> _Reply:
            sent.update(kwargs)
            return _Reply([_Block(type="text", text="{}")])

    endpoint._anthropic = _Block(messages=_Messages())

    await endpoint._call_anthropic(
        [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "hi"},
        ],
        response_format=_Schema,
    )

    assert "temperature" not in sent and "top_p" not in sent
    assert sent["max_tokens"] == 4096
    assert sent["system"] == "sys"
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"]["format"]["type"] == "json_schema"
