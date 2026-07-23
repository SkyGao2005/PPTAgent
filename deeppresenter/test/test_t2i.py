from __future__ import annotations

import base64
from io import BytesIO
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock

import pytest
from openai.types.image import Image as OpenAIImage
from openai.types.images_response import ImagesResponse
from PIL import Image as PILImage

from deeppresenter.utils import image_generation as image_io
from deeppresenter.utils.config import LLM
from deeppresenter.utils.image_generation import (
    image_bytes_from_response,
    image_generation_result,
    save_image_payload,
)


def _llm(**kwargs: Any) -> LLM:
    return LLM(
        base_url="http://localhost.invalid/v1",
        model="test-image-model",
        api_key="test-key",
        **kwargs,
    )


def _png_bytes(size: tuple[int, int] = (17, 9)) -> bytes:
    buffer = BytesIO()
    PILImage.new("RGBA", size, (0, 112, 255, 128)).save(buffer, format="PNG")
    return buffer.getvalue()


def _response(payload: bytes | None = None) -> ImagesResponse:
    encoded = base64.b64encode(payload or _png_bytes()).decode("ascii")
    return ImagesResponse(
        created=0,
        data=[OpenAIImage(b64_json=encoded)],
    )


class _StatusError(RuntimeError):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        super().__init__(f"HTTP {status_code}")


def test_image_request_size_uses_the_supplied_pixel_multiple() -> None:
    llm = _llm()

    assert llm.image_request_size(30, 30, pixel_multiple=10) == (30, 30)
    with pytest.raises(ValueError, match="multiple of 10"):
        llm.image_request_size(31, 30, pixel_multiple=10)


def test_minimum_image_size_scales_up_and_stays_aligned() -> None:
    llm = _llm(min_image_size=1_000_000)

    width, height = llm.image_request_size(512, 256)

    assert width % 16 == 0
    assert height % 16 == 0
    assert width * height >= 1_000_000
    assert width / height == pytest.approx(2, abs=0.03)


def test_gpt_image_2_size_constraints_are_enforced() -> None:
    llm = LLM(
        base_url="http://localhost.invalid/v1",
        model="gpt-image-2",
        api_key="test-key",
    )

    assert llm.image_request_size(1280, 720) == (1280, 720)
    width, height = llm.image_request_size(512, 512)
    assert width % 16 == 0 and height % 16 == 0
    assert width * height >= 655_360

    with pytest.raises(ValueError, match="aspect ratio"):
        llm.image_request_size(2560, 640)
    with pytest.raises(ValueError, match="edge length"):
        llm.image_request_size(3856, 2048)
    with pytest.raises(ValueError, match="8,294,400"):
        llm.image_request_size(3840, 2176)


@pytest.mark.asyncio
async def test_non_retryable_image_error_is_not_repeated() -> None:
    llm = _llm()
    generate = AsyncMock(side_effect=_StatusError(401))
    llm._endpoints[0]._client = SimpleNamespace(
        images=SimpleNamespace(generate=generate)
    )

    with pytest.raises(ValueError, match=r"after 1 attempt"):
        await llm.generate_image("prompt", 32, 32, retry_times=10)

    assert generate.await_count == 1


@pytest.mark.asyncio
async def test_retryable_image_error_honors_the_retry_limit() -> None:
    llm = _llm()
    generate = AsyncMock(side_effect=_StatusError(500))
    llm._endpoints[0]._client = SimpleNamespace(
        images=SimpleNamespace(generate=generate)
    )

    with pytest.raises(ValueError, match=r"after 3 attempt"):
        await llm.generate_image("prompt", 32, 32, retry_times=3)

    assert generate.await_count == 3


@pytest.mark.asyncio
async def test_gpt_image_omits_legacy_response_format() -> None:
    llm = LLM(
        base_url="http://localhost.invalid/v1",
        model="gpt-image-2",
        api_key="test-key",
        sampling_parameters={
            "response_format": "b64_json",
            "quality": "low",
        },
    )
    generate = AsyncMock(return_value=_response())
    llm._endpoints[0]._client = SimpleNamespace(
        images=SimpleNamespace(generate=generate)
    )

    await llm.generate_image("prompt", 1280, 720, retry_times=1)

    kwargs = generate.await_args.kwargs
    assert kwargs["size"] == "1280x720"
    assert kwargs["quality"] == "low"
    assert "response_format" not in kwargs


@pytest.mark.asyncio
async def test_non_retryable_endpoint_still_allows_a_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    llm = LLM(
        base_url="http://primary.invalid/v1",
        model="primary",
        api_key="primary-key",
        endpoints=[
            {
                "base_url": "http://fallback.invalid/v1",
                "model": "fallback",
                "api_key": "fallback-key",
            }
        ],
    )
    primary = AsyncMock(side_effect=_StatusError(401))
    fallback = AsyncMock(return_value=_response())
    llm._endpoints[0]._client = SimpleNamespace(
        images=SimpleNamespace(generate=primary)
    )
    llm._endpoints[1]._client = SimpleNamespace(
        images=SimpleNamespace(generate=fallback)
    )
    monkeypatch.setattr(
        "deeppresenter.utils.config.random.shuffle",
        lambda endpoints: None,
    )

    response = await llm.generate_image("prompt", 32, 32, retry_times=3)

    assert response.data
    assert primary.await_count == 1
    assert fallback.await_count == 1


@pytest.mark.asyncio
async def test_empty_image_response_is_not_retried() -> None:
    llm = _llm()
    generate = AsyncMock(return_value=ImagesResponse(created=0, data=None))
    llm._endpoints[0]._client = SimpleNamespace(
        images=SimpleNamespace(generate=generate)
    )

    with pytest.raises(ValueError, match=r"after 1 attempt"):
        await llm.generate_image("prompt", 32, 32, retry_times=3)

    assert generate.await_count == 1


@pytest.mark.asyncio
async def test_base64_image_is_validated_and_saved_in_requested_format(
    tmp_path: Path,
) -> None:
    payload = await image_bytes_from_response(_response())
    output = tmp_path / "generated.jpg"

    saved = save_image_payload(payload, output)

    assert saved["source_format"] == "PNG"
    assert saved["format"] == "JPEG"
    assert (saved["width"], saved["height"]) == (17, 9)
    with PILImage.open(output) as image:
        assert image.format == "JPEG"
        assert image.size == (17, 9)


@pytest.mark.asyncio
async def test_url_image_download_follows_redirects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: dict[str, object] = {}
    payload = _png_bytes()

    class _Download:
        content = payload

        def raise_for_status(self) -> None:
            return None

    class _Client:
        def __init__(self, **kwargs: object) -> None:
            observed.update(kwargs)

        async def __aenter__(self) -> "_Client":
            return self

        async def __aexit__(self, *args: object) -> None:
            return None

        async def get(self, url: str) -> _Download:
            observed["url"] = url
            return _Download()

    monkeypatch.setattr(image_io.httpx, "AsyncClient", _Client)
    response = SimpleNamespace(
        data=[
            SimpleNamespace(
                b64_json=None,
                url="https://images.invalid/generated",
            )
        ]
    )

    assert await image_bytes_from_response(response) == payload
    assert observed["follow_redirects"] is True
    assert observed["url"] == "https://images.invalid/generated"


def test_result_exposes_actual_dimensions_to_the_model(tmp_path: Path) -> None:
    saved = save_image_payload(_png_bytes(), tmp_path / "generated.png")

    result = image_generation_result(
        requested_size=(1280, 720),
        submitted_size=(1280, 720),
        saved=saved,
    )

    assert result["actual_size"] == {
        "width": 17,
        "height": 9,
        "aspect_ratio": 1.8889,
    }
    assert result["size_matches_request"] is False
    assert result["size_matches_submission"] is False
    assert "crop, pad, resize" in result["warning"]
