"""Adapter from DeepPresenter's LLM client to per-slide Template IR annotation."""

from __future__ import annotations

import base64
import mimetypes
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from deeppresenter.utils.config import get_json_from_response


class DeepPresenterStructuredVLMClient:
    """Send exactly one slide and its bounded shape listing to a multimodal LLM.

    The response schema travels only through ``response_format``; the Endpoint
    layer injects it into the request for providers without structured output.
    """

    def __init__(self, llm: Any) -> None:
        self.llm = llm

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        image_paths: Sequence[str],
        response_model: type[BaseModel],
    ) -> Mapping[str, Any]:
        content: list[dict[str, Any]] = [{"type": "text", "text": user_prompt}]
        for raw_path in image_paths:
            image_path = Path(raw_path)
            media_type = mimetypes.guess_type(image_path.name)[0] or "image/webp"
            encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
            content.append(
                {
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:{media_type};base64,{encoded}",
                    },
                }
            )
        response = await self.llm.run(
            [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": content},
            ],
            response_format=response_model,
            retry_times=2,
        )
        parsed = get_json_from_response(response.choices[0].message.content or "")
        if not isinstance(parsed, dict):
            raise ValueError("Template VLM returned a non-object response")
        return parsed
