"""Per-slide semantic annotation for Template IR.

The VLM boundary accepts exactly one slide at a time and only answers the
question a model is good at: grouping the extracted shapes into semantic
regions and describing the page. Geometry, capacity, and asset bindings are
always derived from the extracted source shapes, never authored by the model,
so an annotator cannot invent coordinates or assets.
"""

from __future__ import annotations

import json
from typing import (
    Annotated,
    Any,
    Awaitable,
    Callable,
    Mapping,
    Protocol,
    Sequence,
    runtime_checkable,
)

from pydantic import BaseModel, Field, ValidationError

from .ids import sha256_bytes
from .models import (
    Asset,
    AssetRole,
    Density,
    LayoutPattern,
    MessagePattern,
    NormalizedBox,
    PageSemantics,
    PageStage,
    Region,
    RegionBehavior,
    RegionCapacity,
    RegionKind,
    RegionRole,
    Semantic,
    ShapeKind,
    ShapeNode,
    ShapeScope,
    SourceGraph,
    StrictModel,
)
from .rendering import overlay_labels


class SlideAnnotationInput(StrictModel):
    source_graph: SourceGraph
    assets: list[Asset] = Field(default_factory=list)
    reference_image_path: str | None = None
    overlay_image_path: str | None = None


@runtime_checkable
class SemanticAnnotator(Protocol):
    @property
    def annotator_id(self) -> str:
        """Stable implementation/prompt identifier used in revision IDs."""

    async def annotate(self, request: SlideAnnotationInput) -> Semantic:
        """Annotate exactly one slide."""


@runtime_checkable
class StructuredVLMClient(Protocol):
    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        image_paths: Sequence[str],
        response_model: type[BaseModel],
    ) -> Mapping[str, Any]:
        """Return one decoded JSON object matching ``response_model``."""


class CallableStructuredVLMClient:
    """Small adapter for dependency-injected SDK/model functions."""

    def __init__(
        self,
        callback: Callable[..., Awaitable[Mapping[str, Any]]],
    ) -> None:
        self._callback = callback

    async def complete_json(
        self,
        *,
        system_prompt: str,
        user_prompt: str,
        image_paths: Sequence[str],
        response_model: type[BaseModel],
    ) -> Mapping[str, Any]:
        return await self._callback(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            image_paths=image_paths,
            response_model=response_model,
        )


ShapeLabel = Annotated[str, Field(pattern=r"^#[0-9]{1,4}$")]


class AnnotationRegion(StrictModel):
    """One model-proposed region: a grouping of shape labels plus meaning."""

    shape_labels: list[ShapeLabel] = Field(min_length=1)
    kind: RegionKind
    role: RegionRole
    description: str | None = None
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0


class SlideAnnotationResponse(StrictModel):
    """The only structure a VLM is asked to produce, listed in reading order."""

    stage: PageStage
    layout_pattern: LayoutPattern
    message_pattern: MessagePattern
    density: Density
    title: str | None = None
    summary: str
    regions: list[AnnotationRegion] = Field(min_length=1)
    selection_hints: list[str] = Field(default_factory=list)
    avoid_when: list[str] = Field(default_factory=list)


def union_bbox(shapes: Sequence[ShapeNode]) -> NormalizedBox:
    """Bounding box of a shape group, derived from extracted geometry."""

    if len(shapes) == 1:
        return shapes[0].normalized_bbox
    x1 = min(shape.normalized_bbox.x for shape in shapes)
    y1 = min(shape.normalized_bbox.y for shape in shapes)
    x2 = max(
        shape.normalized_bbox.x + shape.normalized_bbox.width for shape in shapes
    )
    y2 = max(
        shape.normalized_bbox.y + shape.normalized_bbox.height for shape in shapes
    )
    return NormalizedBox(
        x=round(x1, 6),
        y=round(y1, 6),
        width=round(x2 - x1, 6),
        height=round(y2 - y1, 6),
    )


def derive_region(
    region_id: str,
    *,
    role: RegionRole,
    kind: RegionKind,
    shapes: Sequence[ShapeNode],
    confidence: float,
    description: str | None = None,
) -> Region:
    """Build a region whose geometry, capacity, and assets come from shapes."""

    has_text = any(shape.text for shape in shapes)
    text_length = sum(
        len(shape.text.text.strip()) for shape in shapes if shape.text
    )
    line_count = sum(len(shape.text.paragraphs) for shape in shapes if shape.text)
    bbox = union_bbox(shapes)
    capacity = RegionCapacity(
        min_items=1 if role in {RegionRole.TITLE, RegionRole.LOGO} else 0,
        max_items=1
        if role not in {RegionRole.BULLET_GROUP, RegionRole.BODY}
        else max(1, line_count + 2),
        max_lines=max(1, line_count + 1) if has_text else None,
        min_chars=max(0, round(text_length * 0.6)) if has_text else None,
        max_chars=max(12, round(text_length * 1.4)) if has_text else None,
        recommended_aspect_ratio=(
            bbox.width / bbox.height
            if kind is RegionKind.IMAGE and bbox.height > 0
            else None
        ),
    )
    required = role in {RegionRole.TITLE, RegionRole.LOGO, RegionRole.BACKGROUND}
    return Region(
        region_id=region_id,
        source_shape_ids=[shape.shape_id for shape in shapes],
        kind=kind,
        role=role,
        bbox=bbox,
        capacity=capacity,
        behavior=RegionBehavior(
            required=required,
            can_hide=not required,
            can_reflow=role not in {RegionRole.LOGO, RegionRole.BACKGROUND},
            can_resize=role not in {RegionRole.LOGO, RegionRole.BACKGROUND},
            preserve_position=required,
        ),
        style_ref=f"{kind.value}.{role.value}",
        asset_ids=list(
            dict.fromkeys(
                asset_id for shape in shapes for asset_id in shape.asset_ids
            )
        ),
        confidence=confidence,
        description=description,
    )


def empty_slide_semantic(graph: SourceGraph, annotator_id: str) -> Semantic:
    """Semantic record for a page with no annotatable shapes."""

    return Semantic(
        slide_id=graph.slide_id,
        page_number=graph.page_number,
        page_semantics=PageSemantics(
            stage=PageStage.CONTENT,
            layout_pattern=(
                LayoutPattern.FULL_BLEED
                if graph.background_asset_ids
                else LayoutPattern.FREEFORM
            ),
            message_pattern=MessagePattern.STATEMENT,
            modalities=[],
            density=Density.SPARSE,
            summary="Blank page without annotatable shapes",
        ),
        regions=[],
        reading_order=[],
        annotator_id=annotator_id,
        warnings=["Page has no visible shapes; produced an empty semantic page."],
    )


_SYSTEM_PROMPT = """You annotate ONE PowerPoint template slide for a design-retrieval system.

You receive a JSON listing of the shapes on the slide, keyed by short labels
like "#3", plus a rendered image of the slide and an overlay image that draws
each shape's bounding box with the same "#N" label.

Ground rules:
- The shape listing is authoritative for geometry and text; the images give
  visual context and let you match labels to what you see.
- Group the labeled shapes into semantic regions. Every region references at
  least one label from the listing; never invent labels. Geometry, sizes, and
  asset bindings are computed from your grouping afterwards, so your only job
  is grouping and meaning.
- The slide's sample text is placeholder content. Classify what kind of
  content belongs in each region; do not treat the sample wording as required
  content.

Field guide:
- stage: cover (opening page), agenda (table of contents), section (chapter
  divider), content (regular information page), summary (recap of key
  points), closing (thank-you/contact), appendix (supplementary material).
- layout_pattern: hero (one dominant statement), title_body (heading over one
  content block), split (two complementary halves), columns (3+ parallel
  columns), cards (grid of self-contained tiles), comparison (side-by-side
  contrast), timeline (sequential steps), dashboard (several data views),
  gallery (image-driven grid), full_bleed (edge-to-edge visual), freeform
  (none of the above).
- message_pattern: statement, explanation, comparison, process, evidence
  (data-backed), summary, transition.
- density: sparse, medium, or dense - how much content the page is designed
  to hold, not how much sample text it happens to contain.
- regions: list them in natural reading order. One region per coherent
  content slot; group shapes that form one unit (an image with its caption, a
  metric number with its label). role describes function (title, subtitle,
  body, bullet_group, metric, chart, table, diagram, photo, logo, footer,
  decoration, background, other); kind describes medium (text, image, chart,
  table, diagram, metric, group, decoration, background, unknown).
- title: the slide's visible heading text, or null.
- summary: one sentence describing the page's design intent.
- selection_hints: short sentences telling a generator when to pick this
  page. avoid_when: short sentences telling it when not to.

Return only JSON for the provided response schema."""


PROMPT_VERSION = f"semantic-v3.{sha256_bytes(_SYSTEM_PROMPT.encode('utf-8'))[:8]}"


class VLMAnnotator:
    """Strict one-request-per-slide VLM annotator with schema-error retries."""

    def __init__(
        self,
        client: StructuredVLMClient,
        *,
        model_id: str,
        max_attempts: int = 3,
    ) -> None:
        if max_attempts < 1:
            raise ValueError("max_attempts must be positive")
        self.client = client
        self.model_id = model_id
        self.max_attempts = max_attempts

    @property
    def annotator_id(self) -> str:
        return f"vlm:{self.model_id}:{PROMPT_VERSION}"

    async def annotate(self, request: SlideAnnotationInput) -> Semantic:
        graph = request.source_graph
        labels = overlay_labels(graph)
        if not labels:
            return empty_slide_semantic(graph, self.annotator_id)
        payload = self._request_payload(request, labels)
        image_paths = [
            value
            for value in (request.reference_image_path, request.overlay_image_path)
            if value is not None
        ]
        last_error = ""
        for _ in range(self.max_attempts):
            prompt = dict(payload)
            if last_error:
                prompt["previous_attempt_error"] = (
                    "Your previous response was rejected; fix this and answer "
                    f"again: {last_error}"
                )
            raw = await self.client.complete_json(
                system_prompt=_SYSTEM_PROMPT,
                user_prompt=json.dumps(prompt, ensure_ascii=False),
                image_paths=image_paths,
                response_model=SlideAnnotationResponse,
            )
            try:
                response = SlideAnnotationResponse.model_validate_json(
                    json.dumps(raw, ensure_ascii=False)
                )
                return self._assemble(graph, labels, response)
            except (ValidationError, ValueError) as exc:
                last_error = str(exc)
        raise ValueError(
            f"VLM annotation for slide {graph.slide_id} failed after "
            f"{self.max_attempts} attempts: {last_error}"
        )

    def _assemble(
        self,
        graph: SourceGraph,
        labels: Mapping[str, ShapeNode],
        response: SlideAnnotationResponse,
    ) -> Semantic:
        regions: list[Region] = []
        for index, item in enumerate(response.regions):
            shapes: list[ShapeNode] = []
            for label in item.shape_labels:
                shape = labels.get(label)
                if shape is None:
                    raise ValueError(
                        f"unknown shape label {label!r}; valid labels are "
                        f"{list(labels)}"
                    )
                if shape not in shapes:
                    shapes.append(shape)
            regions.append(
                derive_region(
                    f"r{index + 1:03d}",
                    role=item.role,
                    kind=item.kind,
                    shapes=shapes,
                    confidence=item.confidence,
                    description=item.description,
                )
            )
        page_semantics = PageSemantics(
            stage=response.stage,
            layout_pattern=response.layout_pattern,
            message_pattern=response.message_pattern,
            modalities=list(dict.fromkeys(region.kind for region in regions)),
            density=response.density,
            title=response.title,
            summary=response.summary,
        )
        return Semantic(
            slide_id=graph.slide_id,
            page_number=graph.page_number,
            page_semantics=page_semantics,
            regions=regions,
            reading_order=[region.region_id for region in regions],
            selection_hints=response.selection_hints,
            avoid_when=response.avoid_when,
            annotator_id=self.annotator_id,
        )

    @staticmethod
    def _request_payload(
        request: SlideAnnotationInput,
        labels: Mapping[str, ShapeNode],
    ) -> dict[str, Any]:
        graph = request.source_graph
        asset_roles = {asset.asset_id: asset.role for asset in request.assets}
        label_by_shape_id = {
            shape.shape_id: label for label, shape in labels.items()
        }
        payload: dict[str, Any] = {
            "task": "Annotate this single template slide.",
            "page_number": graph.page_number,
            "canvas_aspect_ratio": graph.canvas.aspect_ratio,
            "shapes": {
                label: _shape_summary(shape, asset_roles, label_by_shape_id)
                for label, shape in labels.items()
            },
        }
        if graph.notes:
            payload["speaker_notes"] = graph.notes
        return payload


def _shape_summary(
    shape: ShapeNode,
    asset_roles: Mapping[str, AssetRole],
    label_by_shape_id: Mapping[str, str],
) -> dict[str, Any]:
    """Compact single-shape view sent to the VLM instead of the full graph."""

    box = shape.normalized_bbox
    summary: dict[str, Any] = {
        "name": shape.name,
        "kind": shape.kind.value,
        "scope": shape.scope.value,
        "bbox": [
            round(box.x, 3),
            round(box.y, 3),
            round(box.width, 3),
            round(box.height, 3),
        ],
    }
    if shape.placeholder_type:
        summary["placeholder"] = shape.placeholder_type
    if shape.rotation:
        summary["rotation"] = shape.rotation
    if shape.text and shape.text.text.strip():
        summary["paragraphs"] = [
            {
                "text": paragraph.text,
                "level": paragraph.level,
                "bullet": paragraph.bullet,
                "size_pt": max(
                    (
                        run.style.size_pt
                        for run in paragraph.runs
                        if run.style.size_pt is not None
                    ),
                    default=None,
                ),
            }
            for paragraph in shape.text.paragraphs
        ]
    roles = sorted(
        {
            asset_roles[asset_id].value
            for asset_id in shape.asset_ids
            if asset_id in asset_roles
        }
    )
    if roles:
        summary["asset_roles"] = roles
    child_labels = [
        label_by_shape_id[child_id]
        for child_id in shape.child_shape_ids
        if child_id in label_by_shape_id
    ]
    if child_labels:
        summary["child_labels"] = child_labels
    return summary


class DeterministicAnnotator:
    """Offline semantic fallback derived entirely from extracted facts."""

    annotator_id = "deterministic-semantic-v2"

    async def annotate(self, request: SlideAnnotationInput) -> Semantic:
        graph = request.source_graph
        assets = {asset.asset_id: asset for asset in request.assets}
        child_ids = {
            child_id for shape in graph.shapes for child_id in shape.child_shape_ids
        }
        candidates = [
            shape
            for shape in graph.shapes
            # Layout/master chrome is reproduced verbatim by the scaffold and
            # must not become a content region.
            if shape.scope is ShapeScope.SLIDE
            and shape.visible
            and shape.shape_id not in child_ids
        ]
        title_shape = self._title_shape(candidates)
        ordered = sorted(
            candidates,
            key=lambda shape: (
                0 if shape is title_shape else 1,
                shape.normalized_bbox.y,
                shape.normalized_bbox.x,
                shape.z_index,
            ),
        )
        regions: list[Region] = []
        for shape in ordered:
            role, kind = self._role_and_kind(shape, title_shape, assets)
            if (
                kind is RegionKind.DECORATION
                and not shape.asset_ids
                and shape.normalized_bbox.width * shape.normalized_bbox.height
                < 0.0005
            ):
                continue
            regions.append(
                derive_region(
                    f"r{len(regions) + 1:03d}",
                    role=role,
                    kind=kind,
                    shapes=[shape],
                    confidence=0.75,
                )
            )

        stage = self._stage(graph, regions, title_shape)
        layout = self._layout(regions)
        modalities = list(dict.fromkeys(region.kind for region in regions))
        char_count = sum(
            len(shape.text.text) for shape in candidates if shape.text is not None
        )
        density = (
            Density.SPARSE
            if len(regions) <= 3 and char_count < 120
            else Density.DENSE
            if len(regions) >= 9 or char_count > 700
            else Density.MEDIUM
        )
        title = (
            title_shape.text.text.strip() if title_shape and title_shape.text else None
        )
        page_semantics = PageSemantics(
            stage=stage,
            layout_pattern=layout,
            message_pattern=self._message_pattern(stage, regions),
            modalities=modalities,
            density=density,
            title=title or None,
            summary=f"{stage.value} slide using a {layout.value} composition",
        )
        return Semantic(
            slide_id=graph.slide_id,
            page_number=graph.page_number,
            page_semantics=page_semantics,
            regions=regions,
            reading_order=[region.region_id for region in regions],
            selection_hints=self._selection_hints(page_semantics),
            avoid_when=self._avoid_when(page_semantics),
            annotator_id=self.annotator_id,
            warnings=[
                "Semantics were inferred deterministically because no VLM annotator was configured."
            ],
        )

    @staticmethod
    def _title_shape(shapes: list[ShapeNode]) -> ShapeNode | None:
        text_shapes = [
            shape for shape in shapes if shape.text and shape.text.text.strip()
        ]
        for shape in text_shapes:
            placeholder = shape.placeholder_type or ""
            if "title" in placeholder and "subtitle" not in placeholder:
                return shape
        upper = [shape for shape in text_shapes if shape.normalized_bbox.y < 0.3]
        if not upper:
            return None

        def score(shape: ShapeNode) -> tuple[float, float]:
            sizes = [
                run.style.size_pt or 0.0
                for paragraph in shape.text.paragraphs
                for run in paragraph.runs
            ]
            return (max(sizes, default=0.0), -shape.normalized_bbox.y)

        return max(upper, key=score)

    @staticmethod
    def _role_and_kind(
        shape: ShapeNode,
        title_shape: ShapeNode | None,
        assets: dict[str, Asset],
    ) -> tuple[RegionRole, RegionKind]:
        roles = {assets[value].role for value in shape.asset_ids if value in assets}
        if AssetRole.LOGO in roles:
            return RegionRole.LOGO, RegionKind.IMAGE
        if AssetRole.BACKGROUND in roles:
            return RegionRole.BACKGROUND, RegionKind.BACKGROUND
        if AssetRole.DECORATION in roles:
            return RegionRole.DECORATION, RegionKind.DECORATION
        if shape is title_shape:
            return RegionRole.TITLE, RegionKind.TEXT
        placeholder = shape.placeholder_type or ""
        if "subtitle" in placeholder:
            return RegionRole.SUBTITLE, RegionKind.TEXT
        if shape.normalized_bbox.y > 0.9 and shape.text:
            return RegionRole.FOOTER, RegionKind.TEXT
        if shape.kind is ShapeKind.CHART:
            return RegionRole.CHART, RegionKind.CHART
        if shape.kind is ShapeKind.TABLE:
            return RegionRole.TABLE, RegionKind.TABLE
        if shape.kind is ShapeKind.DIAGRAM:
            return RegionRole.DIAGRAM, RegionKind.DIAGRAM
        if shape.kind is ShapeKind.PICTURE:
            return RegionRole.PHOTO, RegionKind.IMAGE
        if shape.text and shape.text.text.strip():
            has_bullets = any(paragraph.bullet for paragraph in shape.text.paragraphs)
            return (
                RegionRole.BULLET_GROUP if has_bullets else RegionRole.BODY,
                RegionKind.TEXT,
            )
        if shape.kind is ShapeKind.GROUP:
            return RegionRole.OTHER, RegionKind.GROUP
        return RegionRole.DECORATION, RegionKind.DECORATION

    @staticmethod
    def _stage(
        graph: SourceGraph,
        regions: list[Region],
        title_shape: ShapeNode | None,
    ) -> PageStage:
        title = (
            title_shape.text.text.lower() if title_shape and title_shape.text else ""
        )
        if any(word in title for word in ("agenda", "contents", "目录", "议程")):
            return PageStage.AGENDA
        if any(word in title for word in ("summary", "conclusion", "总结", "结论")):
            return PageStage.SUMMARY
        if any(word in title for word in ("thank", "questions", "谢谢", "致谢")):
            return PageStage.CLOSING
        substantive = [
            region
            for region in regions
            if region.role
            not in {
                RegionRole.LOGO,
                RegionRole.BACKGROUND,
                RegionRole.DECORATION,
                RegionRole.FOOTER,
            }
        ]
        if graph.page_number == 1 and len(substantive) <= 3:
            return PageStage.COVER
        if len(substantive) <= 2 and title_shape is not None:
            return PageStage.SECTION
        return PageStage.CONTENT

    @staticmethod
    def _layout(regions: list[Region]) -> LayoutPattern:
        meaningful = [
            region
            for region in regions
            if region.role
            not in {RegionRole.LOGO, RegionRole.DECORATION, RegionRole.FOOTER}
        ]
        if any(
            region.kind in {RegionKind.IMAGE, RegionKind.BACKGROUND}
            and region.bbox.width * region.bbox.height > 0.75
            for region in meaningful
        ):
            return LayoutPattern.FULL_BLEED
        content = [
            region for region in meaningful if region.role is not RegionRole.TITLE
        ]
        if len(content) >= 4 and all(region.bbox.width < 0.35 for region in content):
            return LayoutPattern.CARDS
        left = [
            region for region in content if region.bbox.x + region.bbox.width / 2 < 0.5
        ]
        right = [
            region for region in content if region.bbox.x + region.bbox.width / 2 >= 0.5
        ]
        if left and right:
            return LayoutPattern.SPLIT if len(content) <= 3 else LayoutPattern.COLUMNS
        if (
            any(region.kind is RegionKind.CHART for region in content)
            and len(content) >= 3
        ):
            return LayoutPattern.DASHBOARD
        if len(content) <= 2:
            return LayoutPattern.TITLE_BODY
        return LayoutPattern.FREEFORM

    @staticmethod
    def _message_pattern(stage: PageStage, regions: list[Region]) -> MessagePattern:
        if stage in {PageStage.SECTION, PageStage.COVER, PageStage.CLOSING}:
            return MessagePattern.TRANSITION
        if stage is PageStage.SUMMARY:
            return MessagePattern.SUMMARY
        if sum(region.kind is RegionKind.CHART for region in regions):
            return MessagePattern.EVIDENCE
        if (
            sum(
                region.kind in {RegionKind.TEXT, RegionKind.IMAGE} for region in regions
            )
            >= 4
        ):
            return MessagePattern.COMPARISON
        return MessagePattern.EXPLANATION

    @staticmethod
    def _selection_hints(semantics: PageSemantics) -> list[str]:
        return [
            f"Use for {semantics.stage.value} slides.",
            f"Works with {semantics.density.value} content density.",
            f"Prefer when a {semantics.layout_pattern.value} composition fits the message.",
        ]

    @staticmethod
    def _avoid_when(semantics: PageSemantics) -> list[str]:
        if semantics.density is Density.SPARSE:
            return ["Avoid for long-form or data-dense content."]
        if semantics.density is Density.DENSE:
            return ["Avoid when the slide should communicate a single visual idea."]
        return []
