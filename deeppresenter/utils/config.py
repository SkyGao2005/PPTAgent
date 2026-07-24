import asyncio
import json
import math
import random
import re
from collections.abc import Mapping
from copy import deepcopy
from itertools import cycle, product
from pathlib import Path
from typing import Any, Literal

import json_repair
import yaml
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion
from openai.types.images_response import ImagesResponse
from pydantic import BaseModel, Field, PrivateAttr, ValidationError, model_validator

from deeppresenter.utils.constants import (
    CONTEXT_LENGTH_LIMIT,
    MCP_CALL_TIMEOUT,
    PACKAGE_DIR,
    PIXEL_MULTIPLE,
    RETRY_TIMES,
    T2I_RETRY_TIMES,
)
from deeppresenter.utils.log import debug, logging_openai_exceptions


class ContextWindowExceededError(RuntimeError):
    """Raised before a request, or immediately after a provider rejects its size."""


def _estimate_text_tokens(text: str) -> int:
    """Conservative tokenizer-independent estimate for mixed Latin/CJK text."""
    ascii_chars = sum(ord(char) < 128 for char in text)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, math.ceil(ascii_chars / 4 + non_ascii_chars * 1.25))


# Worst case for :func:`_estimate_text_tokens` is all-CJK text at 1.25 tokens
# per character. Sizing character bounds against that keeps a projection
# inside its token budget for any language.
_CHARS_PER_TOKEN = 1 / 1.25


def chars_for_tokens(tokens: int) -> int:
    """Convert a token budget into the character bound a projection may use.

    Template projections are bounded while serializing JSON, which is measured
    in characters. Deriving that bound here keeps one number -- the token
    budget -- authoritative instead of pairing it with a second hardcoded
    character limit that silently disagrees.
    """

    return max(256, int(tokens * _CHARS_PER_TOKEN))


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return value
    return {}


def _anthropic_json(value: Any) -> Any:
    """Serialize Anthropic SDK models without dropping opaque response fields."""

    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_unset=True)
    model_dump = getattr(value, "model_dump", None)
    if callable(model_dump):
        return model_dump(mode="json", exclude_unset=True)
    if isinstance(value, Mapping):
        return {key: _anthropic_json(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_anthropic_json(item) for item in value]
    if hasattr(value, "__dict__"):
        return {
            key: _anthropic_json(item)
            for key, item in vars(value).items()
            if not key.startswith("_")
        }
    return value


def _usage_token(usage: Mapping[str, Any], key: str) -> int:
    value = usage.get(key, 0)
    try:
        return int(value or 0)
    except (TypeError, ValueError):
        return 0


def _content_token_estimate(content: Any, image_token_estimate: int) -> int:
    if content is None:
        return 0
    if isinstance(content, str):
        return _estimate_text_tokens(content)
    if not isinstance(content, list):
        return _estimate_text_tokens(str(content))

    tokens = 0
    for raw_block in content:
        block = _as_mapping(raw_block)
        block_type = block.get("type")
        if block_type in {"image", "image_url", "input_image"}:
            tokens += image_token_estimate
            continue
        text = block.get("text") or block.get("content")
        if text:
            tokens += _estimate_text_tokens(str(text))
    return tokens


def estimate_chat_tokens(
    messages: list[Any] | str,
    tools: list[dict[str, Any]] | None = None,
    image_token_estimate: int = 1_200,
    response_format: type[BaseModel] | None = None,
) -> int:
    """Estimate text, tool schema/calls and image tokens before an API call."""
    if isinstance(messages, str):
        messages = [{"role": "user", "content": messages}]

    tokens = 3
    for raw_message in messages:
        message = _as_mapping(raw_message)
        tokens += 6
        anthropic_blocks = message.get("anthropic_content")
        if anthropic_blocks:
            # Native blocks are the authoritative payload for an Anthropic
            # assistant turn. Counting the OpenAI compatibility fields too
            # would double-count its text and tool calls.
            tokens += _estimate_text_tokens(
                json.dumps(anthropic_blocks, ensure_ascii=False, default=str)
            )
        else:
            tokens += _content_token_estimate(
                message.get("content"), image_token_estimate
            )
            if message.get("reasoning"):
                tokens += _estimate_text_tokens(str(message["reasoning"]))
            for tool_call in message.get("tool_calls") or []:
                tokens += _estimate_text_tokens(
                    json.dumps(_as_mapping(tool_call), ensure_ascii=False)
                )

    if tools:
        tokens += 8 + _estimate_text_tokens(
            json.dumps(tools, ensure_ascii=False, default=str)
        )
    if response_format is not None:
        tokens += 8 + _estimate_text_tokens(
            json.dumps(response_format.model_json_schema(), ensure_ascii=False)
        )
    return tokens


def is_context_overflow_error(error: BaseException) -> bool:
    """Recognize provider-specific context overflow errors without retrying them."""
    code = str(getattr(error, "code", "")).lower()
    body = str(getattr(error, "body", "")).lower()
    message = f"{error} {code} {body}".lower()
    markers = (
        "context_length_exceeded",
        "context window",
        "context length",
        "maximum context",
        "max context",
        "too many tokens",
        "token limit exceeded",
        "prompt is too long",
        "request too large",
    )
    return any(marker in message for marker in markers)


def is_retryable_image_error(error: BaseException) -> bool:
    """Whether retrying an image request can plausibly succeed unchanged."""

    status_code = getattr(error, "status_code", None)
    try:
        status = int(status_code) if status_code is not None else None
    except (TypeError, ValueError):
        status = None
    if status is not None:
        return status in {408, 409, 425, 429} or status >= 500

    error_name = type(error).__name__.lower()
    return any(
        marker in error_name
        for marker in ("connection", "timeout", "temporarilyunavailable")
    ) or isinstance(error, (ConnectionError, TimeoutError))


def _image_model_id(model: str) -> str:
    return model.lower().rsplit("/", 1)[-1]


class ContextBudgetConfig(BaseModel):
    """Shared request-budget settings for a model and each endpoint."""

    context_limit_tokens: int = Field(
        default=CONTEXT_LENGTH_LIMIT,
        gt=0,
        description="Maximum input plus output tokens supported by the model",
    )
    reserved_output_tokens: int = Field(
        default=12_500,
        ge=0,
        description="Tokens kept free for the model response",
    )
    context_safety_margin_tokens: int = Field(
        default=2_500,
        ge=0,
        description="Additional guard band for tokenizer estimation error",
    )
    fold_trigger_ratio: float = Field(
        default=0.70,
        gt=0,
        le=1,
        description="Proactively fold history at this fraction of the input budget",
    )
    template_context_max_tokens: int = Field(
        default=30_000,
        gt=0,
        description="Maximum pinned template context in one request",
    )
    # Template projections are produced as serialized JSON, so these token
    # budgets are converted to character bounds with ``chars_for_tokens``.
    # Keep them in tokens: they are compared against the same estimator that
    # guards every request.
    template_overview_max_tokens: int = Field(
        default=7_500,
        gt=0,
        description="Budget for the pinned template overview projection",
    )
    template_reference_max_tokens: int = Field(
        default=7_500,
        gt=0,
        description="Budget for one retrieved reference page projection",
    )
    template_search_result_max_tokens: int = Field(
        default=3_750,
        gt=0,
        description="Budget for a single search_template_references match",
    )
    max_template_reference_images: int = Field(default=2, ge=0)
    image_token_estimate: int = Field(
        default=1_200,
        gt=0,
        description="Conservative per-image estimate used during preflight",
    )
    compaction_input_max_tokens: int = Field(
        default=15_000,
        gt=0,
        description="Maximum history payload sent to a compaction request",
    )
    compaction_summary_max_tokens: int = Field(
        default=2_500,
        gt=0,
        description="Maximum tokens retained from one context compaction summary",
    )

    @model_validator(mode="after")
    def template_budgets_fit_the_aggregate(self) -> "ContextBudgetConfig":
        """Shrink template sub-budgets so their worst case fits the aggregate.

        The pinned overview and every live reference image share
        ``template_context_max_tokens``. Treating that total as authoritative
        and scaling the parts to fit makes an inconsistent configuration
        impossible, instead of letting one surface as a failed generation.
        """

        images = self.max_template_reference_images * self.image_token_estimate
        text = self.template_overview_max_tokens + (
            self.max_template_reference_images * self.template_reference_max_tokens
        )
        if images + text <= self.template_context_max_tokens:
            return self

        available = max(0, self.template_context_max_tokens - images)
        scale = available / text if text else 0.0
        self.template_overview_max_tokens = max(
            256, int(self.template_overview_max_tokens * scale)
        )
        self.template_reference_max_tokens = max(
            256, int(self.template_reference_max_tokens * scale)
        )
        debug(
            "Template sub-budgets scaled to fit "
            f"template_context_max_tokens={self.template_context_max_tokens}: "
            f"overview={self.template_overview_max_tokens}, "
            f"reference={self.template_reference_max_tokens}"
        )
        return self

    @property
    def effective_reserved_output_tokens(self) -> int:
        sampling = getattr(self, "sampling_parameters", {})
        requested = sampling.get("max_completion_tokens", sampling.get("max_tokens", 0))
        try:
            requested_tokens = int(requested)
        except (TypeError, ValueError):
            requested_tokens = 0
        return max(self.reserved_output_tokens, requested_tokens)

    @property
    def input_token_budget(self) -> int:
        return max(
            1,
            self.context_limit_tokens
            - self.effective_reserved_output_tokens
            - self.context_safety_margin_tokens,
        )

    def context_budget_kwargs(self) -> dict[str, Any]:
        fields = ContextBudgetConfig.model_fields
        return {name: getattr(self, name) for name in fields}


def get_json_from_response(response: str) -> dict | list:
    """
    Extract JSON from a text response.

    Args:
        response (str): The response text.

    Returns:
        Dict|List: The extracted JSON.

    Raises:
        Exception: If JSON cannot be extracted from the response.
    """

    assert isinstance(response, str) and len(response) > 0, (
        "response must be a non-empty string"
    )
    response = response.strip()
    try:
        return json.loads(response)
    except Exception:
        pass

    # Try to find JSON by looking for matching braces
    open_braces = []
    close_braces = []

    for i, char in enumerate(response):
        if char == "{" or char == "[":
            open_braces.append(i)
        elif char == "}" or char == "]":
            close_braces.append(i)

    for i, j in product(open_braces, reversed(close_braces)):
        if i > j:
            continue
        try:
            json_obj = json.loads(response[i : j + 1])
            if isinstance(json_obj, (dict, list)):
                return max(
                    json_obj, json_repair.loads(response), key=lambda x: len(str(x))
                )
        except Exception:
            pass

    return json_repair.loads(response)


_DATA_URL_RE = re.compile(r"^data:(?P<media>[^;]+);base64,(?P<data>.+)$", re.DOTALL)
# Opus 4.7 and later reject these outright; the rest of the family ignores them.
# Dropping them mirrors LiteLLM's `drop_params` so one config can address both
# wire formats without the sampling block having to know which is in use.
_ANTHROPIC_UNSUPPORTED_SAMPLING = frozenset(
    {"temperature", "top_p", "top_k", "frequency_penalty", "presence_penalty", "n"}
)
_ANTHROPIC_DEFAULT_CACHE_CONTROL = {"type": "ephemeral"}


def anthropic_base_url(base_url: str | None) -> str | None:
    """Normalize a custom Anthropic-compatible endpoint.

    The SDK appends ``/v1/messages`` to whatever base URL it is given, while
    every OpenAI-style entry in this config carries its version segment in the
    URL (``.../api/paas/v4/``). Copying that habit here yields
    ``/v1/v1/messages`` and a 404 from the gateway, so a trailing ``/v1`` is
    treated as the same intent rather than a different endpoint.
    """

    if not base_url:
        return base_url
    trimmed = base_url.rstrip("/")
    if trimmed.endswith("/v1"):
        trimmed = trimmed[: -len("/v1")]
    return trimmed or base_url


def anthropic_content(content: Any) -> Any:
    """Rewrite one OpenAI message body as Anthropic content blocks.

    Plain strings pass through. The shapes that differ are images: OpenAI
    carries them as a data URL under ``image_url``, Anthropic as an explicit
    base64 source with its media type split out.
    """

    if not isinstance(content, list):
        return content
    blocks: list[dict[str, Any]] = []
    for part in content:
        if not isinstance(part, Mapping):
            blocks.append({"type": "text", "text": str(part)})
            continue
        if part.get("type") == "image_url":
            url = (part.get("image_url") or {}).get("url", "")
            match = _DATA_URL_RE.match(url)
            if match is None:
                blocks.append({"type": "image", "source": {"type": "url", "url": url}})
                continue
            blocks.append(
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": match.group("media"),
                        "data": match.group("data"),
                    },
                }
            )
        else:
            blocks.append(dict(part))
    return blocks


def to_anthropic_messages(
    messages: list[Any],
    cache_control: Mapping[str, Any] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]] | None]:
    """Split OpenAI-shaped messages into Anthropic messages plus a system prompt.

    Three shapes differ, and an agent loop hits all of them on its second turn:
    the system prompt travels beside the conversation rather than as its first
    turn; a tool call rides in the assistant's own content as a ``tool_use``
    block instead of a sibling ``tool_calls`` array; and a tool result is a
    ``tool_result`` block inside a *user* turn rather than a ``tool`` role.

    Callers pass ``ChatMessage`` models as readily as dicts -- the agent hands
    its history straight through -- so each entry is normalized first.
    """

    cache_control = (
        _ANTHROPIC_DEFAULT_CACHE_CONTROL
        if cache_control is None
        else dict(cache_control)
    )
    system: list[str] = []
    converted: list[dict[str, Any]] = []
    for entry in messages:
        message = _as_mapping(entry)
        role = message.get("role")
        content = message.get("content")
        if role == "system":
            if isinstance(content, str):
                system.append(content)
            else:
                system.extend(
                    part.get("text", "")
                    for part in content or []
                    if isinstance(part, Mapping) and part.get("type") == "text"
                )
            continue
        if role == "tool":
            # Anthropic has no tool role: the result is content the user turn
            # carries back, keyed to the id of the call it answers.
            converted.append(
                {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": message.get("tool_call_id"),
                            "content": anthropic_content(content) or "",
                            **({"is_error": True} if message.get("is_error") else {}),
                        }
                    ],
                }
            )
            continue
        native_blocks = message.get("anthropic_content")
        if role == "assistant" and isinstance(native_blocks, list):
            # Thinking signatures and redacted payloads are opaque. Anthropic
            # requires the complete assistant block sequence to be replayed
            # field-for-field and in its original order in a following tool
            # turn, so never reconstruct it from the OpenAI-compatible
            # text/tool_call projection.
            blocks = deepcopy(native_blocks)
        else:
            blocks = anthropic_content(content)
            for call in message.get("tool_calls") or []:
                call = _as_mapping(call)
                function = _as_mapping(call.get("function"))
                arguments = function.get("arguments")
                if isinstance(arguments, str):
                    arguments = get_json_from_response(arguments) if arguments else {}
                if not isinstance(blocks, list):
                    blocks = [{"type": "text", "text": blocks}] if blocks else []
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": call.get("id"),
                        "name": function.get("name"),
                        "input": arguments or {},
                    }
                )
        converted.append({"role": role, "content": blocks})
    # A turn whose content is empty is rejected; that happens when an assistant
    # message carried nothing but a now-relocated tool call.
    converted = [
        message for message in converted if message["content"] not in (None, "", [])
    ]
    system_text = "\n\n".join(part for part in system if part)
    system_blocks = (
        [
            {
                "type": "text",
                "text": system_text,
                "cache_control": deepcopy(cache_control),
            }
        ]
        if system_text
        else None
    )
    return converted, system_blocks


def to_anthropic_tools(
    tools: list[Any],
    cache_control: Mapping[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Unwrap tools and mark the end of their stable cacheable prefix."""

    cache_control = (
        _ANTHROPIC_DEFAULT_CACHE_CONTROL
        if cache_control is None
        else dict(cache_control)
    )
    converted: list[dict[str, Any]] = []
    for tool in tools:
        tool = _as_mapping(tool)
        function = _as_mapping(tool.get("function")) or tool
        converted.append(
            {
                "name": function["name"],
                "description": function.get("description", ""),
                "input_schema": function.get(
                    "parameters", {"type": "object", "properties": {}}
                ),
            }
        )
    if converted:
        converted[-1]["cache_control"] = deepcopy(cache_control)
    return converted


def from_anthropic_message(message: Any, model: str) -> ChatCompletion:
    """Adapt one Anthropic reply into the ChatCompletion the callers expect.

    Every consumer in this codebase reads `choices[0].message`, so translating
    at the endpoint boundary keeps the second wire format from leaking into
    agents, tools, and tests.
    """

    native_content = _anthropic_json(message.content)
    if not isinstance(native_content, list):
        native_content = []
    text: list[str] = []
    thinking: list[str] = []
    tool_calls: list[dict[str, Any]] = []
    for block in native_content:
        if not isinstance(block, Mapping):
            continue
        kind = block.get("type")
        if kind == "text":
            text.append(str(block.get("text", "")))
        elif kind == "thinking":
            # This is only a human-readable compatibility view. The complete
            # signed block above remains authoritative for replay.
            thinking.append(str(block.get("thinking", "")))
        elif kind == "tool_use":
            tool_calls.append(
                {
                    "id": block.get("id"),
                    "type": "function",
                    "function": {
                        "name": block.get("name"),
                        "arguments": json.dumps(
                            block.get("input", {}), ensure_ascii=False
                        ),
                    },
                }
            )
    # Anthropic reports why it stopped in its own vocabulary; map only what the
    # OpenAI schema has a slot for.
    finish_reason = {
        "end_turn": "stop",
        "stop_sequence": "stop",
        "max_tokens": "length",
        "tool_use": "tool_calls",
        "refusal": "content_filter",
    }.get(getattr(message, "stop_reason", None) or "end_turn", "stop")
    usage = getattr(message, "usage", None)
    usage_payload: dict[str, Any] | None = None
    if usage is not None:
        raw_usage = _anthropic_json(usage)
        if not isinstance(raw_usage, Mapping):
            raw_usage = {}
        raw_usage = dict(raw_usage)
        input_tokens = _usage_token(raw_usage, "input_tokens")
        cache_creation_input_tokens = _usage_token(
            raw_usage, "cache_creation_input_tokens"
        )
        cache_read_input_tokens = _usage_token(raw_usage, "cache_read_input_tokens")
        output_tokens = _usage_token(raw_usage, "output_tokens")
        total_input_tokens = (
            input_tokens + cache_creation_input_tokens + cache_read_input_tokens
        )
        usage_payload = {
            **raw_usage,
            "prompt_tokens": total_input_tokens,
            "completion_tokens": output_tokens,
            "total_tokens": total_input_tokens + output_tokens,
            "prompt_tokens_details": {
                "cached_tokens": cache_read_input_tokens,
                "uncached_tokens": input_tokens,
                "cache_creation_tokens": cache_creation_input_tokens,
            },
            "total_input_tokens": total_input_tokens,
            "cache_hit_rate": (
                cache_read_input_tokens / total_input_tokens
                if total_input_tokens
                else 0.0
            ),
            "usage_source": "anthropic",
            # Keep one untouched nested copy so future Anthropic usage fields
            # survive even if the OpenAI compatibility model evolves.
            "anthropic_usage": deepcopy(raw_usage),
        }
    return ChatCompletion.model_validate(
        {
            "id": getattr(message, "id", "anthropic"),
            "object": "chat.completion",
            "created": 0,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "finish_reason": finish_reason,
                    "message": {
                        "role": "assistant",
                        "content": "".join(text) or None,
                        "tool_calls": tool_calls or None,
                        "reasoning": "".join(thinking) if thinking else None,
                        "anthropic_content": native_content,
                    },
                }
            ],
            "usage": usage_payload,
        }
    )


class Endpoint(ContextBudgetConfig):
    """LLM Endpoint Configuration"""

    base_url: str | None = Field(default=None, description="API base URL")
    model: str = Field(description="Model name")
    api_key: str | None = Field(default=None, description="API key")
    provider: Literal["openai", "anthropic", "litellm"] = Field(
        default="openai",
        description=(
            "Wire format to speak: 'openai' (default), 'anthropic' for the "
            "native Messages API, or 'litellm' to route through LiteLLM"
        ),
    )
    client_kwargs: dict[str, Any] = Field(
        default_factory=dict, description="Client parameters"
    )
    sampling_parameters: dict[str, Any] = Field(
        default_factory=dict, description="Sampling parameters"
    )
    _client: AsyncOpenAI | None = PrivateAttr(default=None)
    _anthropic: Any = PrivateAttr(default=None)

    def model_post_init(self, _) -> None:
        if self.provider == "anthropic":
            from anthropic import AsyncAnthropic

            # A bare client also resolves ANTHROPIC_API_KEY or an `ant auth
            # login` profile, so an unset key here is not an error. A gateway
            # that wants Bearer auth or extra headers takes them through
            # `client_kwargs` (`auth_token`, `default_headers`).
            self._anthropic = AsyncAnthropic(
                api_key=self.api_key,
                base_url=anthropic_base_url(self.base_url),
                **self.client_kwargs,
            )
        elif self.provider != "litellm":
            self._client = AsyncOpenAI(
                api_key=self.api_key,
                base_url=self.base_url,
                **self.client_kwargs,
            )

    async def _call_litellm(
        self,
        messages: list[dict[str, Any]],
        response_format: type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatCompletion:
        import litellm

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "drop_params": True,
            **self.sampling_parameters,
        }
        if self.api_key:
            kwargs["api_key"] = self.api_key
        if self.base_url:
            kwargs["api_base"] = self.base_url
        if tools is not None:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"
        if response_format is not None:
            kwargs["response_format"] = response_format
        return await litellm.acompletion(**kwargs)

    async def _call_anthropic(
        self,
        messages: list[dict[str, Any]],
        response_format: type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatCompletion:
        sampling = {
            key: value
            for key, value in self.sampling_parameters.items()
            if key not in _ANTHROPIC_UNSUPPORTED_SAMPLING
        }
        configured_cache_control = sampling.pop("cache_control", None)
        cache_control = (
            deepcopy(_ANTHROPIC_DEFAULT_CACHE_CONTROL)
            if configured_cache_control is None
            else dict(configured_cache_control)
        )
        converted, system = to_anthropic_messages(messages, cache_control=cache_control)
        # max_tokens is required here, unlike the OpenAI route where it is
        # optional; the endpoint's own reservation is the natural value.
        sampling.setdefault(
            "max_tokens",
            sampling.pop("max_completion_tokens", None)
            or self.effective_reserved_output_tokens,
        )
        sampling.setdefault("thinking", {"type": "adaptive"})
        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": converted,
            "cache_control": deepcopy(cache_control),
            **sampling,
        }
        if system is not None:
            kwargs["system"] = system
        if tools is not None:
            # "auto" matches what the openai and litellm branches send; forcing
            # a call would also collide with thinking on some deployments.
            kwargs["tools"] = to_anthropic_tools(tools, cache_control=cache_control)
            kwargs["tool_choice"] = {"type": "auto"}
        if response_format is not None:
            # The caller re-validates the text against the model afterwards,
            # so this only has to make the reply parseable.
            kwargs.setdefault("output_config", {}).setdefault(
                "format",
                {
                    "type": "json_schema",
                    "schema": response_format.model_json_schema(),
                },
            )
        message = await self._anthropic.messages.create(**kwargs)
        return from_anthropic_message(message, self.model)

    async def call(
        self,
        messages: list[dict[str, Any]],
        soft_response_parsing: bool,
        response_format: type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatCompletion:
        """Execute a chat or tool call using the endpoint client"""
        estimated_tokens = estimate_chat_tokens(
            messages,
            tools,
            self.image_token_estimate,
            response_format,
        )
        if estimated_tokens > self.input_token_budget:
            raise ContextWindowExceededError(
                f"Request needs about {estimated_tokens} input tokens, but endpoint "
                f"{self.model} allows {self.input_token_budget} after reserving output"
            )
        if self.provider == "litellm":
            response = await self._call_litellm(messages, response_format, tools)
        elif self.provider == "anthropic":
            response = await self._call_anthropic(messages, response_format, tools)
        elif tools is not None:
            response = await self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                **self.sampling_parameters,
            )
        elif not soft_response_parsing and response_format is not None:
            response: ChatCompletion = await self._client.chat.completions.parse(
                model=self.model,
                messages=messages,
                response_format=response_format,
                **self.sampling_parameters,
            )
        else:
            response: ChatCompletion = await self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                **self.sampling_parameters,
            )
        assert response.choices is not None and len(response.choices) > 0, (
            f"No choices returned from the model, got {response}"
        )
        message = response.choices[0].message
        debug(f"Response from {self.model}: {message}")
        if response_format is not None:
            parsed_json = json.dumps(get_json_from_response(message.content))
            message.content = response_format.model_validate_json(
                parsed_json
            ).model_dump_json(indent=2)
        assert tools is None or len(message.tool_calls or []), (
            f"No tool call returned from the model, got {message}"
        )
        assert message.tool_calls or message.content, (
            "Empty content returned from the model"
        )
        return response


class LLM(ContextBudgetConfig):
    """LLM Client Manager"""

    base_url: str | None = Field(default=None, description="API base URL")
    model: str | None = Field(default=None, description="Model name")
    api_key: str | None = Field(default=None, description="API key")
    provider: str = Field(
        default="openai",
        description=(
            "Wire format for every endpoint below: 'openai' (default), "
            "'anthropic', or 'litellm'"
        ),
    )
    identifier: str | None = Field(
        default=None,
        description="Optional identifier for the model instance, this will override property `model_name`",
    )
    is_multimodal: bool | None = Field(
        default=None, description="Whether the model is multimodal"
    )
    max_concurrent: int | None = Field(
        default=None, description="Maximum concurrency limit"
    )
    client_kwargs: dict[str, Any] = Field(
        default_factory=dict, description="Client parameters"
    )
    sampling_parameters: dict[str, Any] = Field(
        default_factory=dict, description="Sampling parameters"
    )
    endpoints: list[dict[str, Any]] = Field(
        default_factory=list,
        description="Additional endpoints for alternating retries",
    )
    soft_response_parsing: bool = Field(
        default=False,
        description="Enable soft parsing: parse response content as JSON directly instead of using completion.parse",
    )
    min_image_size: int | None = Field(
        default=None,
        gt=0,
        description="Minimum image size (width * height) for generation, smaller images will be resized proportionally",
    )
    secret_logging: bool = Field(
        default=False, description="Logging detailed endpoint (API key included)"
    )

    _semaphore: asyncio.Semaphore = PrivateAttr()
    _endpoints: list[Endpoint] = PrivateAttr(default_factory=list)

    model_config = {"arbitrary_types_allowed": True}

    @property
    def model_name(self) -> str:
        return self.identifier or self._endpoints[0].model.split("/")[-1].split(":")[0]

    def model_post_init(self, context) -> None:
        """Initialize semaphore and endpoints"""
        self._semaphore = asyncio.Semaphore(self.max_concurrent or 10000)
        if self.model:
            self._endpoints.insert(
                0,
                Endpoint(
                    base_url=self.base_url,
                    model=self.model,
                    api_key=self.api_key,
                    provider=self.provider,
                    client_kwargs=self.client_kwargs,
                    sampling_parameters=self.sampling_parameters,
                    **self.context_budget_kwargs(),
                ),
            )
        for endpoint in self.endpoints:
            endpoint_config = {**self.context_budget_kwargs(), **endpoint}
            self._endpoints.append(Endpoint(**endpoint_config))
        assert len(self._endpoints) >= 1, "At least one endpoint must be configured"

        model_lower = self._endpoints[0].model.lower()
        if self.is_multimodal is None:
            if any(word in model_lower for word in ("gpt", "claude", "gemini", "vl")):
                self.is_multimodal = True
                debug(
                    f"Model {self._endpoints[0].model} is detected as multimodal model, setting `is_multimodal` to True"
                )
            else:
                self.is_multimodal = False

        return super().model_post_init(context)

    def image_request_size(
        self,
        width: int,
        height: int,
        pixel_multiple: int = PIXEL_MULTIPLE,
    ) -> tuple[int, int]:
        """Validate and, when required, enlarge an image request safely."""

        if (
            not isinstance(width, int)
            or isinstance(width, bool)
            or not isinstance(height, int)
            or isinstance(height, bool)
        ):
            raise TypeError("Image width and height must be integers")
        if width <= 0 or height <= 0:
            raise ValueError("Image width and height must be positive")
        if pixel_multiple <= 0:
            raise ValueError("pixel_multiple must be positive")
        if width % pixel_multiple or height % pixel_multiple:
            raise ValueError(
                f"Image width and height must be a multiple of {pixel_multiple}"
            )

        is_gpt_image_2 = _image_model_id(self._endpoints[0].model).startswith(
            "gpt-image-2"
        )
        minimum_pixels = self.min_image_size or 0
        if is_gpt_image_2:
            minimum_pixels = max(minimum_pixels, 655_360)

        if width * height < minimum_pixels:
            ratio = (minimum_pixels / (width * height)) ** 0.5
            width = math.ceil(width * ratio / pixel_multiple) * pixel_multiple
            height = math.ceil(height * ratio / pixel_multiple) * pixel_multiple

        if is_gpt_image_2:
            if max(width, height) > 3_840:
                raise ValueError("GPT Image 2 edge length cannot exceed 3840px")
            if max(width, height) / min(width, height) > 3:
                raise ValueError("GPT Image 2 aspect ratio cannot exceed 3:1")
            if width * height > 8_294_400:
                raise ValueError("GPT Image 2 requests cannot exceed 8,294,400 pixels")
        return width, height

    @staticmethod
    def _image_sampling_parameters(endpoint: Endpoint) -> dict[str, Any]:
        parameters = dict(endpoint.sampling_parameters)
        if _image_model_id(endpoint.model).startswith("gpt-image-"):
            # GPT Image always returns base64. response_format is a legacy
            # DALL-E parameter and some compatibility gateways mishandle it.
            parameters.pop("response_format", None)
        return parameters

    async def run(
        self,
        messages: list[dict[str, Any]] | str,
        response_format: type[BaseModel] | None = None,
        tools: list[dict[str, Any]] | None = None,
        retry_times: int = RETRY_TIMES,
    ) -> ChatCompletion:
        """Unified interface for chat and tool calls with alternating retry"""
        if isinstance(messages, str):
            messages = [{"role": "user", "content": messages}]

        estimated_tokens = estimate_chat_tokens(
            messages,
            tools,
            self.image_token_estimate,
            response_format,
        )
        if estimated_tokens > self.input_token_budget:
            raise ContextWindowExceededError(
                f"Request needs about {estimated_tokens} input tokens, but model "
                f"{self.model_name} allows {self.input_token_budget} after reserving output"
            )

        eligible_endpoints = [
            endpoint
            for endpoint in self._endpoints
            if estimated_tokens <= endpoint.input_token_budget
        ]
        if not eligible_endpoints:
            raise ContextWindowExceededError(
                f"No endpoint can accept the estimated {estimated_tokens} input tokens"
            )

        errors = []
        iter_endpoints = cycle(eligible_endpoints)
        async with self._semaphore:
            for _ in range(retry_times):
                endpoint = next(iter_endpoints)
                try:
                    return await endpoint.call(
                        messages,
                        self.soft_response_parsing,
                        response_format,
                        tools,
                    )
                except (AssertionError, ValidationError) as e:
                    errors.append(f"[{endpoint.model}] {e}")
                except Exception as e:
                    if is_context_overflow_error(e):
                        raise ContextWindowExceededError(
                            f"Endpoint {endpoint.model} rejected the request as too large: {e}"
                        ) from e
                    errors.append(f"[{endpoint.model}] {e}")
                    if self.secret_logging:
                        identifider = endpoint
                    else:
                        identifider = endpoint.model
                    logging_openai_exceptions(identifider, e)
        raise ValueError(f"All models failed after {retry_times} retries:\n{errors}")

    async def generate_image(
        self,
        prompt: str,
        width: int,
        height: int,
        retry_times: int = T2I_RETRY_TIMES,
        pixel_multiple: int = PIXEL_MULTIPLE,
    ) -> ImagesResponse:
        """Unified interface for image generation"""
        width, height = self.image_request_size(width, height, pixel_multiple)
        if retry_times <= 0:
            raise ValueError("retry_times must be positive")

        async with self._semaphore:
            errors: list[str] = []
            endpoints = list(self._endpoints)
            random.shuffle(endpoints)
            attempts = 0
            endpoint_index = 0
            while attempts < retry_times and endpoints:
                # t2i is stateless
                endpoint = endpoints[endpoint_index % len(endpoints)]
                attempts += 1
                try:
                    sampling_parameters = self._image_sampling_parameters(endpoint)
                    if endpoint.provider == "litellm":
                        import litellm

                        response = await litellm.aimage_generation(
                            prompt=prompt,
                            model=endpoint.model,
                            size=f"{width}x{height}",
                            timeout=MCP_CALL_TIMEOUT // 5,
                            drop_params=True,
                            **(
                                {"api_key": endpoint.api_key}
                                if endpoint.api_key
                                else {}
                            ),
                            **(
                                {"api_base": endpoint.base_url}
                                if endpoint.base_url
                                else {}
                            ),
                            **sampling_parameters,
                        )
                    else:
                        response = await endpoint._client.images.generate(
                            prompt=prompt,
                            model=endpoint.model,
                            size=f"{width}x{height}",
                            timeout=MCP_CALL_TIMEOUT // 5,
                            **sampling_parameters,
                        )
                    if not response.data:
                        raise ValueError(
                            f"Expected at least one image from {endpoint.model}"
                        )
                    return response

                except Exception as e:
                    errors.append(f"[{endpoint.model}] {e}")
                    if self.secret_logging:
                        identifider = endpoint
                    else:
                        identifider = endpoint.model
                    logging_openai_exceptions(identifider, e)
                    if is_retryable_image_error(e):
                        endpoint_index += 1
                        continue

                    # A bad key or request will not improve by sending the same
                    # payload again. Disable only that endpoint so configured
                    # fallbacks still get one chance.
                    endpoints.remove(endpoint)
                    if endpoints:
                        endpoint_index %= len(endpoints)

            raise ValueError(
                f"All image models failed after {attempts} attempt(s): {errors}"
            )

    async def validate(self):
        endpoint = self._endpoints[0]
        if endpoint.provider == "litellm":
            import litellm

            try:
                await litellm.acompletion(
                    model=endpoint.model,
                    messages=[{"role": "user", "content": "ping"}],
                    max_tokens=1,
                    drop_params=True,
                    **({"api_key": endpoint.api_key} if endpoint.api_key else {}),
                    **({"api_base": endpoint.base_url} if endpoint.base_url else {}),
                )
            except Exception as e:
                raise Exception(
                    f"LiteLLM validation failed for model {endpoint.model}: {e}\n"
                ) from e
            return
        models = await endpoint._client.models.list()
        if not any(model.id.endswith(endpoint.model) for model in models.data):
            raise Exception(
                f"Model {endpoint.model} is not available at {endpoint.base_url}, please check your apikey or {PACKAGE_DIR / 'config.yaml'}\n"
            )


class DeepPresenterConfig(BaseModel):
    """DeepPresenter Global Configuration"""

    # config
    multiagent_mode: bool = Field(
        default=False, description="Enable multiagent mode (experimental)"
    )
    offline_mode: bool = Field(
        default=False, description="Enable offline mode, disable all network requests"
    )
    async_tool_mode: bool = Field(
        default=False,
        description="Enable async tool mode for slow tool calls",
    )
    file_path: str = Field(description="Configuration file path")
    mcp_config_file: str = Field(
        description="MCP configuration file", default=str(PACKAGE_DIR / "mcp.json")
    )
    context_folding: bool = Field(
        default=True, description="Enable context management and auto summarization"
    )
    context_window: int | None = Field(
        default=None,
        description="Context window for context management, if not set, use the default value",
    )
    max_context_folds: int = Field(
        default=5, description="Maximum number of folds for context management"
    )
    heavy_reflect: bool = Field(
        default=False,
        description="Enable heavy reflection, use rendered slide image for reflective design",
    )

    # llms
    research_agent: LLM = Field(description="Research agent model configuration")
    design_agent: LLM = Field(description="Design agent model configuration")
    long_context_model: LLM = Field(description="Long context model configuration")
    vision_model: LLM | None = Field(
        default=None, description="Vision model configuration"
    )
    t2i_model: LLM | None = Field(
        default=None, description="Text-to-image model configuration"
    )
    _context_window_explicit: bool = PrivateAttr(default=False)

    def model_post_init(self, context):
        self._context_window_explicit = self.context_window is not None
        if self.context_window is None:
            if self.context_folding:
                self.context_window = CONTEXT_LENGTH_LIMIT // self.max_context_folds
            else:
                self.context_window = CONTEXT_LENGTH_LIMIT

        if self.context_folding:
            debug(
                f"Context folding is enabled, context window: {self.context_window}, max folds: {self.max_context_folds}"
            )
        else:
            debug(f"Context folding is disabled, context window: {self.context_window}")

        return super().model_post_init(context)

    @property
    def has_legacy_context_window_override(self) -> bool:
        return self._context_window_explicit

    @classmethod
    def load_from_file(cls, config_path: str | None = None) -> "DeepPresenterConfig":
        """Load configuration from file"""
        if config_path:
            config_file = Path(config_path)
        else:
            config_file = PACKAGE_DIR / "config.yaml"

        if not config_file.exists():
            raise FileNotFoundError(f"Configuration file {config_file} does not exist")
        config_data = {}
        with open(config_file, encoding="utf-8") as f:
            config_data = yaml.safe_load(f) or {}

        config_data["file_path"] = str(config_file.resolve())
        return cls(**config_data)

    async def validate_llms(self):
        # ? t2i endpoints might not support this api
        tasks = [
            self.research_agent.validate(),
            self.design_agent.validate(),
            self.long_context_model.validate(),
        ]
        if self.vision_model is not None:
            tasks.append(self.vision_model.validate())
        await asyncio.gather(*tasks)

    def __getitem__(self, key: str) -> Any:
        return getattr(self, key)
