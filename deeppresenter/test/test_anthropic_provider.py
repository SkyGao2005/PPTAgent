"""The Anthropic wire format is translated at the endpoint boundary.

Every caller in this codebase reads `choices[0].message`, so the second format
has to arrive already shaped like a ChatCompletion.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from openai.types.completion_usage import CompletionUsage
from pydantic import BaseModel

from deeppresenter.utils.config import (
    Endpoint,
    anthropic_content,
    from_anthropic_message,
    to_anthropic_messages,
    to_anthropic_tools,
)
from deeppresenter.utils.typings import ChatMessage, Cost, Role


class _Block:
    def __init__(self, **fields: Any) -> None:
        self.__dict__.update(fields)


class _Reply:
    def __init__(
        self,
        content: list[_Block],
        stop_reason: str = "end_turn",
        usage: _Block | None = None,
    ) -> None:
        self.id = "msg_01"
        self.content = content
        self.stop_reason = stop_reason
        self.usage = usage or _Block(input_tokens=11, output_tokens=7)


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

    assert system == [
        {
            "type": "text",
            "text": "You design slides.\n\nKeep it short.",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert [m["role"] for m in messages] == ["user", "assistant"]


def test_a_tool_round_trip_survives_translation() -> None:
    """The agent hands its ChatMessage history through on every turn.

    Anthropic has no tool role: the call rides inside the assistant's own
    content and the result comes back inside a user turn.
    """

    from openai.types.chat import ChatCompletionMessageFunctionToolCall

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
            "cache_control": {"type": "ephemeral"},
        }
    ]


def test_only_the_last_static_tool_gets_an_explicit_cache_breakpoint() -> None:
    converted = to_anthropic_tools(
        [
            {"name": "first", "input_schema": {"type": "object"}},
            {"name": "last", "input_schema": {"type": "object"}},
        ]
    )

    assert "cache_control" not in converted[0]
    assert converted[1]["cache_control"] == {"type": "ephemeral"}


def test_a_reply_becomes_a_chat_completion() -> None:
    completion = from_anthropic_message(
        _Reply([_Block(type="text", text="完成了")]), "claude-opus-4-8"
    )

    message = completion.choices[0].message
    assert message.content == "完成了"
    assert message.tool_calls is None
    assert completion.choices[0].finish_reason == "stop"
    assert completion.usage.prompt_tokens == 11


def test_thinking_and_redacted_thinking_survive_history_round_trip() -> None:
    native_content = [
        _Block(type="thinking", thinking="", signature="opaque-signature"),
        _Block(type="redacted_thinking", data="opaque-redacted-data"),
        _Block(type="text", text="先检查"),
        _Block(
            type="tool_use",
            id="toolu_1",
            name="inspect_slide",
            input={"html_file": "slide_01.html"},
        ),
    ]
    completion = from_anthropic_message(
        _Reply(native_content, stop_reason="tool_use"), "claude-opus-4-8"
    )
    projected = completion.choices[0].message
    stored = ChatMessage(
        role=Role.ASSISTANT,
        content=projected.content,
        reasoning=getattr(projected, "reasoning", None),
        anthropic_content=getattr(projected, "anthropic_content", None),
        tool_calls=projected.tool_calls,
        cost=completion.usage,
    )
    restored = ChatMessage.model_validate(stored.model_dump(mode="json"))

    messages, _ = to_anthropic_messages(
        [
            ChatMessage(role=Role.USER, content="检查这一页"),
            restored,
            ChatMessage(
                role=Role.TOOL,
                content="ok",
                tool_call_id="toolu_1",
            ),
        ]
    )

    assert messages[1]["content"] == [
        {"type": "thinking", "thinking": "", "signature": "opaque-signature"},
        {"type": "redacted_thinking", "data": "opaque-redacted-data"},
        {"type": "text", "text": "先检查"},
        {
            "type": "tool_use",
            "id": "toolu_1",
            "name": "inspect_slide",
            "input": {"html_file": "slide_01.html"},
        },
    ]


def test_anthropic_cache_usage_is_preserved_and_counted_as_input() -> None:
    completion = from_anthropic_message(
        _Reply(
            [_Block(type="text", text="完成了")],
            usage=_Block(
                input_tokens=11,
                cache_creation_input_tokens=5,
                cache_read_input_tokens=20,
                output_tokens=7,
                cache_creation=_Block(
                    ephemeral_5m_input_tokens=3,
                    ephemeral_1h_input_tokens=2,
                ),
                iterations=[{"input_tokens": 4, "output_tokens": 1}],
                service_tier="standard_only",
            ),
        ),
        "claude-opus-4-8",
    )

    usage = completion.usage
    assert usage.prompt_tokens == 36
    assert usage.completion_tokens == 7
    assert usage.total_tokens == 43
    assert usage.total_input_tokens == 36
    assert usage.cache_hit_rate == pytest.approx(20 / 36)
    assert usage.cache_creation_input_tokens == 5
    assert usage.cache_read_input_tokens == 20
    assert usage.cache_creation == {
        "ephemeral_5m_input_tokens": 3,
        "ephemeral_1h_input_tokens": 2,
    }
    assert usage.anthropic_usage["iterations"] == [
        {"input_tokens": 4, "output_tokens": 1}
    ]
    assert usage.anthropic_usage["service_tier"] == "standard_only"

    restored = ChatMessage.model_validate(
        ChatMessage(
            role=Role.ASSISTANT,
            content="完成了",
            cost=usage,
        ).model_dump(mode="json")
    )
    assert restored.cost.anthropic_usage == usage.anthropic_usage
    assert restored.cost.cache_creation_input_tokens == 5


def test_cost_uses_a_token_weighted_cache_hit_rate_across_requests() -> None:
    first = from_anthropic_message(
        _Reply(
            [_Block(type="text", text="one")],
            usage=_Block(
                input_tokens=11,
                cache_creation_input_tokens=5,
                cache_read_input_tokens=20,
                output_tokens=7,
                cache_creation=_Block(
                    ephemeral_5m_input_tokens=3,
                    ephemeral_1h_input_tokens=2,
                ),
            ),
        ),
        "claude-opus-4-8",
    ).usage
    second = from_anthropic_message(
        _Reply(
            [_Block(type="text", text="two")],
            usage=_Block(
                input_tokens=9,
                cache_creation_input_tokens=0,
                cache_read_input_tokens=30,
                output_tokens=4,
                cache_creation=_Block(
                    ephemeral_5m_input_tokens=0,
                    ephemeral_1h_input_tokens=0,
                ),
            ),
        ),
        "claude-opus-4-8",
    ).usage
    cost = Cost()

    cost += first
    cost += second

    assert cost.prompt == 75
    assert cost.completion == 11
    assert cost.total == 86
    assert cost.input_tokens == 20
    assert cost.cache_creation_input_tokens == 5
    assert cost.cache_read_input_tokens == 50
    assert cost.cache_creation == {
        "ephemeral_5m_input_tokens": 3,
        "ephemeral_1h_input_tokens": 2,
    }
    assert cost.cache_creation_ephemeral_5m_input_tokens == 3
    assert cost.cache_creation_ephemeral_1h_input_tokens == 2
    assert cost.cache_hit_rate == pytest.approx(50 / 75)


def test_cost_accepts_top_level_only_anthropic_usage_fields() -> None:
    usage = CompletionUsage.model_validate(
        {
            "prompt_tokens": 36,
            "completion_tokens": 7,
            "total_tokens": 43,
            "input_tokens": 11,
            "cache_creation_input_tokens": 5,
            "cache_read_input_tokens": 20,
            "cache_creation": {
                "ephemeral_5m_input_tokens": 3,
                "ephemeral_1h_input_tokens": 2,
            },
        }
    )
    cost = Cost()

    cost += usage

    assert cost.input_tokens == 11
    assert cost.cache_creation_input_tokens == 5
    assert cost.cache_read_input_tokens == 20
    assert cost.cache_creation == {
        "ephemeral_5m_input_tokens": 3,
        "ephemeral_1h_input_tokens": 2,
    }
    assert cost.cache_hit_rate == pytest.approx(20 / 36)


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
        sampling_parameters={
            "temperature": 0.7,
            "top_p": 0.9,
            "cache_control": None,
        },
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
        tools=[
            {
                "type": "function",
                "function": {
                    "name": "inspect_slide",
                    "description": "Render one slide",
                    "parameters": {"type": "object"},
                },
            }
        ],
    )

    assert "temperature" not in sent and "top_p" not in sent
    assert sent["max_tokens"] == 4096
    assert sent["cache_control"] == {"type": "ephemeral"}
    assert sent["system"] == [
        {
            "type": "text",
            "text": "sys",
            "cache_control": {"type": "ephemeral"},
        }
    ]
    assert sent["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert sent["thinking"] == {"type": "adaptive"}
    assert sent["output_config"]["format"]["type"] == "json_schema"


@pytest.mark.asyncio
async def test_the_sdk_serializes_all_three_cache_control_locations() -> None:
    pytest.importorskip("anthropic")
    sent: dict[str, Any] = {}

    async def handle(request: httpx.Request) -> httpx.Response:
        sent.update(json.loads(request.content))
        return httpx.Response(
            200,
            request=request,
            json={
                "id": "msg_mock",
                "type": "message",
                "role": "assistant",
                "model": "claude-opus-4-8",
                "content": [{"type": "text", "text": "ok", "citations": None}],
                "stop_reason": "end_turn",
                "stop_sequence": None,
                "usage": {
                    "input_tokens": 11,
                    "cache_creation_input_tokens": 5,
                    "cache_read_input_tokens": 20,
                    "output_tokens": 7,
                    "cache_creation": {
                        "ephemeral_5m_input_tokens": 3,
                        "ephemeral_1h_input_tokens": 2,
                    },
                    "server_tool_use": {
                        "web_search_requests": 0,
                        "web_fetch_requests": 0,
                    },
                    "service_tier": "standard",
                },
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http_client:
        endpoint = Endpoint(
            provider="anthropic",
            model="claude-opus-4-8",
            api_key="test",
            client_kwargs={"http_client": http_client},
        )
        completion = await endpoint._call_anthropic(
            [
                {"role": "system", "content": "sys"},
                {"role": "user", "content": "go"},
            ],
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "inspect_slide",
                        "description": "Render one slide",
                        "parameters": {"type": "object"},
                    },
                }
            ],
        )

    assert sent["cache_control"] == {"type": "ephemeral"}
    assert sent["system"][-1]["cache_control"] == {"type": "ephemeral"}
    assert sent["tools"][-1]["cache_control"] == {"type": "ephemeral"}
    assert completion.usage.prompt_tokens == 36
