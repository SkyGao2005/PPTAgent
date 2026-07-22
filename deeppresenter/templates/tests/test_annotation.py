from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from PIL import Image
from pydantic import BaseModel

from deeppresenter.templates.annotation import (
    PROMPT_VERSION,
    CallableStructuredVLMClient,
    SlideAnnotationInput,
    SlideAnnotationResponse,
    VLMAnnotator,
)
from deeppresenter.templates.deeppresenter_client import (
    DeepPresenterStructuredVLMClient,
)
from deeppresenter.templates.models import (
    BoundingBox,
    Canvas,
    Density,
    LayoutPattern,
    MessagePattern,
    NormalizedBox,
    PageSemantics,
    PageStage,
    RegionRole,
    Semantic,
    ShapeKind,
    ShapeNode,
    ShapeScope,
    ShapeText,
    SourceGraph,
    TextParagraph,
    TextRun,
    TextStyle,
)
from deeppresenter.utils.config import Endpoint


def _canvas() -> Canvas:
    return Canvas(
        width_emu=12192000,
        height_emu=6858000,
        width_inches=13.333,
        height_inches=7.5,
        aspect_ratio="16:9",
    )


def _text_shape(
    shape_id: str,
    *,
    text: str,
    z_index: int,
    bbox: NormalizedBox,
    size_pt: float,
) -> ShapeNode:
    return ShapeNode(
        shape_id=shape_id,
        name=shape_id,
        scope=ShapeScope.SLIDE,
        kind=ShapeKind.TEXT,
        z_index=z_index,
        bbox=BoundingBox(x=0, y=0, width=100, height=50),
        normalized_bbox=bbox,
        text=ShapeText(
            text=text,
            paragraphs=[
                TextParagraph(
                    text=text,
                    runs=[TextRun(text=text, style=TextStyle(size_pt=size_pt))],
                )
            ],
        ),
    )


def _graph() -> SourceGraph:
    return SourceGraph(
        slide_id="s001",
        page_number=1,
        canvas=_canvas(),
        shapes=[
            _text_shape(
                "s001-slide-sh0001",
                text="Template page",
                z_index=0,
                bbox=NormalizedBox(x=0.06, y=0.08, width=0.7, height=0.12),
                size_pt=30.0,
            ),
            _text_shape(
                "s001-slide-sh0002",
                text="Body copy",
                z_index=1,
                bbox=NormalizedBox(x=0.06, y=0.3, width=0.5, height=0.4),
                size_pt=16.0,
            ),
        ],
    )


def _response(regions: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "stage": "content",
        "layout_pattern": "title_body",
        "message_pattern": "explanation",
        "density": "sparse",
        "title": "Template page",
        "digest": "cover with centred title",
        "summary": "Heading over one body block",
        "regions": regions,
        "selection_hints": ["Use for content slides."],
        "avoid_when": [],
    }


@pytest.mark.asyncio
async def test_vlm_annotator_derives_geometry_from_bound_shapes() -> None:
    captured: dict[str, Any] = {}

    async def complete(**kwargs: Any) -> dict[str, Any]:
        captured.update(kwargs)
        return _response(
            [
                {"shape_labels": ["#1"], "kind": "text", "role": "title"},
                {"shape_labels": ["#2"], "kind": "text", "role": "body"},
            ]
        )

    graph = _graph()
    annotator = VLMAnnotator(
        CallableStructuredVLMClient(complete),
        model_id="vision-test",
    )

    result = await annotator.annotate(SlideAnnotationInput(source_graph=graph))

    assert captured["response_model"] is SlideAnnotationResponse
    assert "#1" in captured["user_prompt"]
    assert result.annotator_id == f"vlm:vision-test:{PROMPT_VERSION}"
    assert [region.region_id for region in result.regions] == ["r001", "r002"]
    assert result.reading_order == ["r001", "r002"]
    title = result.regions[0]
    assert title.source_shape_ids == ["s001-slide-sh0001"]
    assert title.bbox == graph.shapes[0].normalized_bbox
    assert title.role is RegionRole.TITLE
    assert title.behavior.required is True
    assert title.capacity.min_chars is not None
    assert title.capacity.max_chars is not None


@pytest.mark.asyncio
async def test_vlm_annotator_retries_with_error_feedback() -> None:
    prompts: list[dict[str, Any]] = []

    async def complete(**kwargs: Any) -> dict[str, Any]:
        prompts.append(json.loads(kwargs["user_prompt"]))
        if len(prompts) == 1:
            return _response(
                [{"shape_labels": ["#9"], "kind": "text", "role": "title"}]
            )
        return _response(
            [{"shape_labels": ["#1", "#2"], "kind": "text", "role": "body"}]
        )

    annotator = VLMAnnotator(
        CallableStructuredVLMClient(complete),
        model_id="vision-test",
    )

    result = await annotator.annotate(SlideAnnotationInput(source_graph=_graph()))

    assert len(prompts) == 2
    assert "previous_attempt_error" not in prompts[0]
    assert "#9" in prompts[1]["previous_attempt_error"]
    assert result.regions[0].source_shape_ids == [
        "s001-slide-sh0001",
        "s001-slide-sh0002",
    ]
    assert result.regions[0].bbox == NormalizedBox(
        x=0.06, y=0.08, width=0.7, height=0.62
    )


@pytest.mark.asyncio
async def test_vlm_annotator_fails_after_max_attempts() -> None:
    calls = 0

    async def complete(**kwargs: Any) -> dict[str, Any]:
        nonlocal calls
        calls += 1
        return {"unexpected": True}

    annotator = VLMAnnotator(
        CallableStructuredVLMClient(complete),
        model_id="vision-test",
        max_attempts=2,
    )

    with pytest.raises(ValueError, match="failed after 2 attempts"):
        await annotator.annotate(SlideAnnotationInput(source_graph=_graph()))
    assert calls == 2


@pytest.mark.asyncio
async def test_vlm_annotator_handles_pages_without_shapes() -> None:
    async def complete(**kwargs: Any) -> dict[str, Any]:
        raise AssertionError("a shapeless page must not reach the model")

    graph = SourceGraph(
        slide_id="s002",
        page_number=2,
        canvas=_canvas(),
        shapes=[],
    )
    annotator = VLMAnnotator(
        CallableStructuredVLMClient(complete),
        model_id="vision-test",
    )

    result = await annotator.annotate(SlideAnnotationInput(source_graph=graph))

    assert result.regions == []
    assert result.reading_order == []
    assert result.warnings


class _RecordingLLM:
    def __init__(self) -> None:
        self.response_format: type[BaseModel] | None = None
        self.messages: list[dict[str, Any]] = []

    async def run(
        self,
        messages: list[dict[str, Any]],
        *,
        response_format: type[BaseModel],
        retry_times: int,
    ) -> SimpleNamespace:
        assert retry_times == 2
        self.messages = messages
        self.response_format = response_format
        message = SimpleNamespace(content=json.dumps(_response([])))
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@pytest.mark.asyncio
async def test_deeppresenter_client_enforces_structured_output(
    tmp_path: Path,
) -> None:
    image_path = tmp_path / "reference.png"
    Image.new("RGB", (2, 2), "white").save(image_path)
    llm = _RecordingLLM()
    client = DeepPresenterStructuredVLMClient(llm)

    result = await client.complete_json(
        system_prompt="system",
        user_prompt="user",
        image_paths=[str(image_path)],
        response_model=SlideAnnotationResponse,
    )

    assert llm.response_format is SlideAnnotationResponse
    assert result["stage"] == "content"
    content = llm.messages[1]["content"]
    assert content[0]["text"] == "user"
    assert content[1]["image_url"]["url"].startswith("data:image/png;base64,")


class _StructuredCompletions:
    async def parse(self, **_: Any) -> SimpleNamespace:
        semantic = Semantic(
            slide_id="s001",
            page_number=1,
            page_semantics=PageSemantics(
                stage=PageStage.CONTENT,
                layout_pattern=LayoutPattern.FREEFORM,
                message_pattern=MessagePattern.EXPLANATION,
                density=Density.SPARSE,
            ),
            regions=[],
            reading_order=[],
            annotator_id="model-value",
        )
        message = SimpleNamespace(
            content=semantic.model_dump_json(),
            tool_calls=None,
        )
        return SimpleNamespace(choices=[SimpleNamespace(message=message)])


@pytest.mark.asyncio
async def test_endpoint_validates_strict_models_from_json() -> None:
    endpoint = Endpoint(
        model="vision-test",
        api_key="test-key",
        base_url="http://localhost.invalid/v1",
    )
    endpoint._client = SimpleNamespace(  # noqa: SLF001
        chat=SimpleNamespace(completions=_StructuredCompletions())
    )

    response = await endpoint.call(
        [{"role": "user", "content": "annotate"}],
        soft_response_parsing=False,
        response_format=Semantic,
    )

    parsed = Semantic.model_validate_json(response.choices[0].message.content)
    assert parsed.page_semantics.stage is PageStage.CONTENT
