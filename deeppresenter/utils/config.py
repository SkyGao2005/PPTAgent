import asyncio
import json
import math
import random
from itertools import cycle, product
from pathlib import Path
from typing import Any, Literal

import json_repair
import yaml
from openai import AsyncOpenAI
from openai.types.chat import ChatCompletion
from openai.types.images_response import ImagesResponse
from pydantic import BaseModel, Field, PrivateAttr, ValidationError

from deeppresenter.utils.constants import (
    CONTEXT_LENGTH_LIMIT,
    MCP_CALL_TIMEOUT,
    PACKAGE_DIR,
    PIXEL_MULTIPLE,
    RETRY_TIMES,
)
from deeppresenter.utils.log import debug, logging_openai_exceptions


class ContextWindowExceededError(RuntimeError):
    """Raised before a request, or immediately after a provider rejects its size."""


def _estimate_text_tokens(text: str) -> int:
    """Conservative tokenizer-independent estimate for mixed Latin/CJK text."""
    ascii_chars = sum(ord(char) < 128 for char in text)
    non_ascii_chars = len(text) - ascii_chars
    return max(1, math.ceil(ascii_chars / 4 + non_ascii_chars * 1.25))


def _as_mapping(value: Any) -> dict[str, Any]:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json", exclude_none=True)
    if isinstance(value, dict):
        return value
    return {}


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


class ContextBudgetConfig(BaseModel):
    """Shared request-budget settings for a model and each endpoint."""

    context_limit_tokens: int = Field(
        default=CONTEXT_LENGTH_LIMIT,
        gt=0,
        description="Maximum input plus output tokens supported by the model",
    )
    reserved_output_tokens: int = Field(
        default=10_000,
        ge=0,
        description="Tokens kept free for the model response",
    )
    context_safety_margin_tokens: int = Field(
        default=2_000,
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
        default=12_000,
        gt=0,
        description="Maximum pinned template context in one request",
    )
    template_overview_max_tokens: int = Field(default=1_800, gt=0)
    template_reference_max_tokens: int = Field(default=1_200, gt=0)
    max_template_reference_images: int = Field(default=2, ge=0)
    image_token_estimate: int = Field(
        default=1_200,
        gt=0,
        description="Conservative per-image estimate used during preflight",
    )
    compaction_input_max_tokens: int = Field(
        default=12_000,
        gt=0,
        description="Maximum history payload sent to a compaction request",
    )

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


class Endpoint(ContextBudgetConfig):
    """LLM Endpoint Configuration"""

    base_url: str | None = Field(default=None, description="API base URL")
    model: str = Field(description="Model name")
    api_key: str | None = Field(default=None, description="API key")
    provider: Literal["openai", "litellm"] = Field(
        default="openai",
        description="Backend provider: 'openai' (default) or 'litellm'",
    )
    client_kwargs: dict[str, Any] = Field(
        default_factory=dict, description="Client parameters"
    )
    sampling_parameters: dict[str, Any] = Field(
        default_factory=dict, description="Sampling parameters"
    )
    _client: AsyncOpenAI | None = PrivateAttr(default=None)

    def model_post_init(self, _) -> None:
        if self.provider != "litellm":
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
        description="Backend provider: 'openai' (default) or 'litellm'",
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
        retry_times: int = RETRY_TIMES,
        pixel_multiple: int = PIXEL_MULTIPLE,
    ) -> ImagesResponse:
        """Unified interface for image generation"""
        if self.min_image_size is not None and (width * height) < int(
            self.min_image_size
        ):
            ratio = (int(self.min_image_size) / (width * height)) ** 0.5
            width = int(width * ratio)
            height = int(height * ratio)
        assert (width % pixel_multiple == 0) and (height % PIXEL_MULTIPLE == 0), (
            f"Image width and height must be a multiple of {pixel_multiple}"
        )
        async with self._semaphore:
            errors = []
            random.shuffle(self._endpoints)
            for retry_idx in range(retry_times):
                # t2i is stateless
                endpoint = self._endpoints[retry_idx % len(self._endpoints)]
                try:
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
                            **endpoint.sampling_parameters,
                        )
                    else:
                        response = await endpoint._client.images.generate(
                            prompt=prompt,
                            model=endpoint.model,
                            size=f"{width}x{height}",
                            timeout=MCP_CALL_TIMEOUT // 5,
                            **endpoint.sampling_parameters,
                        )
                    assert len(response.data) >= 1, (
                        f"Expected at least an image response, got {response}"
                    )
                    return response

                except (AssertionError, ValidationError) as e:
                    errors.append(f"[{endpoint.model}] {e}")
                except Exception as e:
                    errors.append(f"[{endpoint.model}] {e}")
                    if self.secret_logging:
                        identifider = endpoint
                    else:
                        identifider = endpoint.model
                    logging_openai_exceptions(identifider, e)
            raise ValueError(f"All models failed after {retry_times} retries: {errors}")

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
