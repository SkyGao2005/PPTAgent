from __future__ import annotations

import base64
import binascii
from io import BytesIO
from pathlib import Path
from typing import Any, TypedDict
from uuid import uuid4

import httpx
from PIL import Image, UnidentifiedImageError

from deeppresenter.utils.constants import MCP_CALL_TIMEOUT


class SavedImageInfo(TypedDict):
    path: str
    width: int
    height: int
    format: str
    source_format: str
    bytes: int


_OUTPUT_FORMATS = {
    ".jpeg": "JPEG",
    ".jpg": "JPEG",
    ".png": "PNG",
    ".webp": "WEBP",
}


async def image_bytes_from_response(response: Any) -> bytes:
    """Read the first image from an OpenAI-compatible image response."""

    data = getattr(response, "data", None)
    if not data:
        raise ValueError("Image response contains no data")

    image = data[0]
    encoded = getattr(image, "b64_json", None)
    url = getattr(image, "url", None)
    if encoded:
        if encoded.startswith("data:"):
            _, separator, encoded = encoded.partition(",")
            if not separator:
                raise ValueError("Malformed image data URL")
        compact = "".join(encoded.split())
        try:
            payload = base64.b64decode(compact, validate=True)
        except (binascii.Error, ValueError) as error:
            raise ValueError("Image response contains invalid base64 data") from error
    elif url:
        async with httpx.AsyncClient(
            follow_redirects=True,
            timeout=MCP_CALL_TIMEOUT // 5,
        ) as client:
            download = await client.get(url)
            download.raise_for_status()
            payload = download.content
    else:
        raise ValueError("Image response contains neither base64 data nor a URL")

    if not payload:
        raise ValueError("Image response contains an empty payload")
    return payload


def _image_for_output(image: Image.Image, output_format: str) -> Image.Image:
    if output_format != "JPEG":
        return image.copy()
    if image.mode in {"RGB", "L"}:
        return image.copy()

    rgba = image.convert("RGBA")
    background = Image.new("RGB", rgba.size, "white")
    background.paste(rgba, mask=rgba.getchannel("A"))
    return background


def save_image_payload(payload: bytes, path: str | Path) -> SavedImageInfo:
    """Validate image bytes and atomically save them in the requested format."""

    output_path = Path(path).expanduser()
    output_format = _OUTPUT_FORMATS.get(output_path.suffix.lower())
    if output_format is None:
        supported = ", ".join(sorted(_OUTPUT_FORMATS))
        raise ValueError(f"Image path must end in one of: {supported}")

    try:
        with Image.open(BytesIO(payload)) as source:
            source.load()
            width, height = source.size
            source_format = (source.format or "UNKNOWN").upper()
            output = _image_for_output(source, output_format)
    except (UnidentifiedImageError, OSError) as error:
        raise ValueError("Generated payload is not a valid image") from error

    encoded = BytesIO()
    save_options: dict[str, Any] = {}
    if output_format in {"JPEG", "WEBP"}:
        save_options["quality"] = 95
    output.save(encoded, format=output_format, **save_options)
    image_bytes = encoded.getvalue()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = output_path.with_name(
        f".{output_path.name}.{uuid4().hex}.tmp"
    )
    try:
        temporary.write_bytes(image_bytes)
        temporary.replace(output_path)
    finally:
        temporary.unlink(missing_ok=True)

    return {
        "path": str(output_path),
        "width": width,
        "height": height,
        "format": output_format,
        "source_format": source_format,
        "bytes": len(image_bytes),
    }


def image_generation_result(
    *,
    requested_size: tuple[int, int],
    submitted_size: tuple[int, int],
    saved: SavedImageInfo,
) -> dict[str, Any]:
    """Build model-facing metadata so it can decide how to use the asset."""

    requested_width, requested_height = requested_size
    submitted_width, submitted_height = submitted_size
    actual_width = saved["width"]
    actual_height = saved["height"]
    matches_request = (actual_width, actual_height) == requested_size
    matches_submission = (actual_width, actual_height) == submitted_size
    warning = None
    if not matches_submission:
        warning = (
            "The provider returned different dimensions from the submitted size. "
            "Inspect the image and decide whether to crop, pad, resize, use as-is, "
            "or regenerate it; do not assume the requested dimensions."
        )

    return {
        "status": "success",
        "path": saved["path"],
        "requested_size": {
            "width": requested_width,
            "height": requested_height,
            "aspect_ratio": round(requested_width / requested_height, 4),
        },
        "submitted_size": {
            "width": submitted_width,
            "height": submitted_height,
            "aspect_ratio": round(submitted_width / submitted_height, 4),
        },
        "actual_size": {
            "width": actual_width,
            "height": actual_height,
            "aspect_ratio": round(actual_width / actual_height, 4),
        },
        "size_matches_request": matches_request,
        "size_matches_submission": matches_submission,
        "format": saved["format"],
        "source_format": saved["source_format"],
        "bytes": saved["bytes"],
        "warning": warning,
    }
