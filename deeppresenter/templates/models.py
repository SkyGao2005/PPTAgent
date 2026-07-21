"""Strict, versioned data models for the DeepPresenter template IR.

The models in this module are the persistence contract of the template compiler.
They deliberately contain no references to the legacy PPTAgent layout engine.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path, PurePosixPath
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SCHEMA_VERSION = "2.0"
SUPPORTED_ASPECT_RATIOS = frozenset({"16:9", "4:3"})


def utc_now() -> datetime:
    """Return a timezone-aware UTC timestamp."""

    return datetime.now(timezone.utc)


class StrictModel(BaseModel):
    """Base class used by every persisted Template IR model."""

    model_config = ConfigDict(
        extra="forbid",
        frozen=True,
        strict=True,
        str_strip_whitespace=True,
        validate_default=True,
    )


Identifier = Annotated[str, Field(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")]
HexColor = Annotated[str, Field(pattern=r"^#[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$")]


class TemplateStatus(str, Enum):
    COMPILING = "compiling"
    READY = "ready"
    FAILED = "failed"


class ShapeKind(str, Enum):
    AUTO_SHAPE = "auto_shape"
    CHART = "chart"
    DIAGRAM = "diagram"
    FREEFORM = "freeform"
    GROUP = "group"
    LINE = "line"
    MEDIA = "media"
    OLE_OBJECT = "ole_object"
    PICTURE = "picture"
    PLACEHOLDER = "placeholder"
    TABLE = "table"
    TEXT = "text"
    UNKNOWN = "unknown"


class ShapeScope(str, Enum):
    SLIDE = "slide"
    LAYOUT = "layout"
    MASTER = "master"


class AssetRole(str, Enum):
    LOGO = "logo"
    BACKGROUND = "background"
    DECORATION = "decoration"
    CONTENT_IMAGE = "content_image"
    UNKNOWN = "unknown"


class ReusePolicy(str, Enum):
    ALWAYS = "always"
    TEMPLATE_ONLY = "template_only"
    REFERENCE_ONLY = "reference_only"
    NEVER = "never"


class PageStage(str, Enum):
    COVER = "cover"
    AGENDA = "agenda"
    SECTION = "section"
    CONTENT = "content"
    SUMMARY = "summary"
    CLOSING = "closing"
    APPENDIX = "appendix"


class LayoutPattern(str, Enum):
    HERO = "hero"
    TITLE_BODY = "title_body"
    SPLIT = "split"
    COLUMNS = "columns"
    CARDS = "cards"
    COMPARISON = "comparison"
    TIMELINE = "timeline"
    DASHBOARD = "dashboard"
    GALLERY = "gallery"
    FULL_BLEED = "full_bleed"
    FREEFORM = "freeform"


class MessagePattern(str, Enum):
    STATEMENT = "statement"
    EXPLANATION = "explanation"
    COMPARISON = "comparison"
    PROCESS = "process"
    EVIDENCE = "evidence"
    SUMMARY = "summary"
    TRANSITION = "transition"


class Density(str, Enum):
    SPARSE = "sparse"
    MEDIUM = "medium"
    DENSE = "dense"


class RegionKind(str, Enum):
    TEXT = "text"
    IMAGE = "image"
    CHART = "chart"
    TABLE = "table"
    DIAGRAM = "diagram"
    METRIC = "metric"
    GROUP = "group"
    DECORATION = "decoration"
    BACKGROUND = "background"
    UNKNOWN = "unknown"


class RegionRole(str, Enum):
    TITLE = "title"
    SUBTITLE = "subtitle"
    BODY = "body"
    BULLET_GROUP = "bullet_group"
    METRIC = "metric"
    CHART = "chart"
    TABLE = "table"
    DIAGRAM = "diagram"
    PHOTO = "photo"
    LOGO = "logo"
    FOOTER = "footer"
    DECORATION = "decoration"
    BACKGROUND = "background"
    OTHER = "other"


class ValidationSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


class Canvas(StrictModel):
    width_emu: Annotated[int, Field(gt=0)]
    height_emu: Annotated[int, Field(gt=0)]
    width_inches: Annotated[float, Field(gt=0)]
    height_inches: Annotated[float, Field(gt=0)]
    aspect_ratio: Annotated[str, Field(min_length=1)]


class BoundingBox(StrictModel):
    """Absolute shape bounds in English Metric Units (EMU)."""

    x: int
    y: int
    width: Annotated[int, Field(ge=0)]
    height: Annotated[int, Field(ge=0)]


class NormalizedBox(StrictModel):
    """Bounds normalized against the slide canvas.

    Coordinates may be slightly outside the canvas because PowerPoint permits
    bleed and off-canvas elements.
    """

    x: Annotated[float, Field(ge=-4.0, le=4.0)]
    y: Annotated[float, Field(ge=-4.0, le=4.0)]
    width: Annotated[float, Field(ge=0.0, le=8.0)]
    height: Annotated[float, Field(ge=0.0, le=8.0)]


class TextStyle(StrictModel):
    font_family: str | None = None
    size_pt: Annotated[float | None, Field(gt=0)] = None
    bold: bool | None = None
    italic: bool | None = None
    underline: bool | None = None
    color: HexColor | None = None
    alignment: str | None = None
    language: str | None = None


class TextRun(StrictModel):
    text: str
    style: TextStyle = Field(default_factory=TextStyle)


class TextParagraph(StrictModel):
    text: str
    level: Annotated[int, Field(ge=0, le=8)] = 0
    bullet: bool = False
    style: TextStyle = Field(default_factory=TextStyle)
    runs: list[TextRun] = Field(default_factory=list)


class ShapeText(StrictModel):
    text: str
    paragraphs: list[TextParagraph] = Field(default_factory=list)


class PictureCrop(StrictModel):
    left: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    top: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    right: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0
    bottom: Annotated[float, Field(ge=0.0, le=1.0)] = 0.0


class ShapeNode(StrictModel):
    shape_id: Identifier
    source_id: int | None = None
    name: str
    scope: ShapeScope
    kind: ShapeKind
    z_index: Annotated[int, Field(ge=0)]
    bbox: BoundingBox
    normalized_bbox: NormalizedBox
    rotation: float = 0.0
    visible: bool = True
    placeholder_type: str | None = None
    text: ShapeText | None = None
    asset_ids: list[Identifier] = Field(default_factory=list)
    child_shape_ids: list[Identifier] = Field(default_factory=list)
    crop: PictureCrop | None = None
    fill_color: HexColor | None = None
    line_color: HexColor | None = None


class SourceGraph(StrictModel):
    """Deterministically extracted source facts for one slide."""

    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    slide_id: Identifier
    page_number: Annotated[int, Field(gt=0)]
    canvas: Canvas
    shapes: list[ShapeNode]
    background_asset_ids: list[Identifier] = Field(default_factory=list)
    inherited_asset_ids: list[Identifier] = Field(default_factory=list)
    notes: str | None = None

    @model_validator(mode="after")
    def shape_ids_are_unique(self) -> "SourceGraph":
        shape_ids = [shape.shape_id for shape in self.shapes]
        if len(shape_ids) != len(set(shape_ids)):
            raise ValueError("shape_id values must be unique within a slide")
        return self


class AssetOccurrence(StrictModel):
    slide_id: Identifier
    page_number: Annotated[int, Field(gt=0)]
    scope: ShapeScope
    shape_id: Identifier | None = None
    source_name: str | None = None
    bbox: NormalizedBox | None = None


class Asset(StrictModel):
    asset_id: Identifier
    sha256: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    media_type: Annotated[str, Field(min_length=1)]
    extension: Annotated[str, Field(pattern=r"^[a-z0-9]{1,10}$")]
    path: str
    browser_path: str | None = None
    browser_media_type: Annotated[str | None, Field(min_length=1)] = None
    browser_sha256: Annotated[
        str | None,
        Field(pattern=r"^[0-9a-f]{64}$"),
    ] = None
    browser_pixel_width: Annotated[int | None, Field(gt=0)] = None
    browser_pixel_height: Annotated[int | None, Field(gt=0)] = None
    byte_size: Annotated[int, Field(gt=0)]
    pixel_width: Annotated[int | None, Field(gt=0)] = None
    pixel_height: Annotated[int | None, Field(gt=0)] = None
    role: AssetRole
    reuse_policy: ReusePolicy
    occurrences: list[AssetOccurrence] = Field(default_factory=list)
    alt_text: str | None = None
    tags: list[str] = Field(default_factory=list)

    @field_validator("path", "browser_path")
    @classmethod
    def path_is_relative_and_safe(cls, value: str | None) -> str | None:
        if value is None:
            return None
        path = PurePosixPath(value)
        if path.is_absolute() or ".." in path.parts:
            raise ValueError("asset path must be relative to the revision directory")
        return value

    @model_validator(mode="after")
    def browser_variant_is_complete(self) -> "Asset":
        identity = (
            self.browser_path,
            self.browser_media_type,
            self.browser_sha256,
        )
        if any(value is not None for value in identity) and not all(
            value is not None for value in identity
        ):
            raise ValueError("browser asset metadata must be complete")
        dimensions = (self.browser_pixel_width, self.browser_pixel_height)
        if any(value is not None for value in dimensions) and not all(
            value is not None for value in dimensions
        ):
            raise ValueError("browser asset dimensions must be a complete pair")
        return self


class AssetIndex(StrictModel):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    assets: list[Asset] = Field(default_factory=list)


class RegionCapacity(StrictModel):
    min_items: Annotated[int, Field(ge=0)] = 0
    max_items: Annotated[int | None, Field(gt=0)] = None
    max_lines: Annotated[int | None, Field(gt=0)] = None
    min_chars: Annotated[int | None, Field(ge=0)] = None
    max_chars: Annotated[int | None, Field(gt=0)] = None
    recommended_aspect_ratio: Annotated[float | None, Field(gt=0)] = None

    @model_validator(mode="after")
    def char_range_is_ordered(self) -> "RegionCapacity":
        if (
            self.min_chars is not None
            and self.max_chars is not None
            and self.min_chars > self.max_chars
        ):
            raise ValueError("min_chars must not exceed max_chars")
        return self


class RegionBehavior(StrictModel):
    required: bool = False
    can_hide: bool = True
    can_reflow: bool = True
    can_resize: bool = True
    preserve_position: bool = False


class Region(StrictModel):
    """One semantic region, always bound to at least one extracted shape.

    Geometry and asset bindings are derived from the bound shapes, never
    authored by an annotator. Reading order lives on ``Semantic`` alone.
    """

    region_id: Identifier
    source_shape_ids: list[Identifier] = Field(min_length=1)
    kind: RegionKind
    role: RegionRole
    bbox: NormalizedBox
    capacity: RegionCapacity = Field(default_factory=RegionCapacity)
    behavior: RegionBehavior = Field(default_factory=RegionBehavior)
    style_ref: str | None = None
    asset_ids: list[Identifier] = Field(default_factory=list)
    confidence: Annotated[float, Field(ge=0.0, le=1.0)] = 1.0
    description: str | None = None


class PageSemantics(StrictModel):
    stage: PageStage
    layout_pattern: LayoutPattern
    message_pattern: MessagePattern
    modalities: list[RegionKind] = Field(default_factory=list)
    density: Density
    title: str | None = None
    summary: str = ""


class Semantic(StrictModel):
    """Model-authored semantics for one slide, bound to source shape IDs."""

    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    slide_id: Identifier
    page_number: Annotated[int, Field(gt=0)]
    page_semantics: PageSemantics
    regions: list[Region]
    reading_order: list[Identifier] = Field(default_factory=list)
    selection_hints: list[str] = Field(default_factory=list)
    avoid_when: list[str] = Field(default_factory=list)
    annotator_id: str
    warnings: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def references_are_consistent(self) -> "Semantic":
        region_ids = [region.region_id for region in self.regions]
        if len(region_ids) != len(set(region_ids)):
            raise ValueError("region_id values must be unique within a slide")
        if sorted(self.reading_order) != sorted(region_ids):
            raise ValueError(
                "reading_order must list every region_id exactly once"
            )
        return self


class ThemeColor(StrictModel):
    token: Identifier
    value: HexColor
    usage_count: Annotated[int, Field(ge=0)] = 0
    roles: list[str] = Field(default_factory=list)


class ThemeFont(StrictModel):
    token: Identifier
    family: str
    usage_count: Annotated[int, Field(ge=0)] = 0
    roles: list[str] = Field(default_factory=list)
    fallback: list[str] = Field(default_factory=list)


class Theme(StrictModel):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    canvas: Canvas
    colors: list[ThemeColor] = Field(default_factory=list)
    fonts: list[ThemeFont] = Field(default_factory=list)
    css_variables: dict[str, str] = Field(default_factory=dict)
    logo_asset_ids: list[Identifier] = Field(default_factory=list)
    background_asset_ids: list[Identifier] = Field(default_factory=list)
    decoration_asset_ids: list[Identifier] = Field(default_factory=list)
    safe_area: NormalizedBox = Field(
        default_factory=lambda: NormalizedBox(
            x=0.04,
            y=0.04,
            width=0.92,
            height=0.92,
        )
    )


class LayoutFamily(StrictModel):
    family_id: Identifier
    name: str
    signature: str
    stage: PageStage
    layout_pattern: LayoutPattern
    modalities: list[RegionKind] = Field(default_factory=list)
    slide_ids: list[Identifier]
    representative_slide_id: Identifier
    description: str = ""
    selection_hints: list[str] = Field(default_factory=list)
    avoid_when: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def representative_is_a_member(self) -> "LayoutFamily":
        if self.representative_slide_id not in self.slide_ids:
            raise ValueError("representative_slide_id must be a family member")
        return self


class LayoutFamilyIndex(StrictModel):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    families: list[LayoutFamily] = Field(default_factory=list)


class CompactRegion(StrictModel):
    region_id: Identifier
    role: RegionRole
    kind: RegionKind
    bbox: NormalizedBox
    required: bool
    capacity: RegionCapacity
    style_ref: str | None = None
    asset_ids: list[Identifier] = Field(default_factory=list)


class CompactSlideContext(StrictModel):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    slide_id: Identifier
    page_number: Annotated[int, Field(gt=0)]
    page_semantics: PageSemantics
    regions: list[CompactRegion]
    family_ids: list[Identifier] = Field(default_factory=list)
    reference_image_path: str | None = None
    overlay_image_path: str | None = None
    reusable_asset_ids: list[Identifier] = Field(default_factory=list)
    theme_tokens: dict[str, str] = Field(default_factory=dict)


class SlideIndexEntry(StrictModel):
    slide_id: Identifier
    page_number: Annotated[int, Field(gt=0)]
    source_graph_path: str
    semantic_path: str
    compact_context_path: str
    reference_image_path: str | None = None
    overlay_image_path: str | None = None
    family_ids: list[Identifier] = Field(default_factory=list)
    stage: PageStage
    layout_pattern: LayoutPattern
    modalities: list[RegionKind] = Field(default_factory=list)


class ValidationIssue(StrictModel):
    severity: ValidationSeverity
    code: Identifier
    message: str
    slide_id: Identifier | None = None
    path: str | None = None


class ValidationReport(StrictModel):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    valid: bool
    issues: list[ValidationIssue] = Field(default_factory=list)
    stats: dict[str, int] = Field(default_factory=dict)
    generated_at: datetime = Field(default_factory=utc_now)


class Revision(StrictModel):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    revision_id: Identifier
    template_id: Identifier
    source_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    compiler_version: str
    extractor_version: str
    annotator_id: str
    renderer_id: str
    status: Literal["ready"] = "ready"
    slide_count: Annotated[int, Field(gt=0)]
    canvas: Canvas
    source_path: str
    theme_path: str
    slide_index_path: str
    asset_index_path: str
    family_index_path: str
    validation_report_path: str
    file_hashes: dict[str, Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]] = Field(
        default_factory=dict
    )
    created_at: datetime = Field(default_factory=utc_now)

    @classmethod
    def load(cls, path: Path | str) -> "Revision":
        return cls.model_validate_json(Path(path).read_text(encoding="utf-8"))


TemplateRevision = Revision


class RevisionRef(StrictModel):
    revision_id: Identifier
    source_hash: Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]
    slide_count: Annotated[int, Field(gt=0)]
    created_at: datetime


class TemplateManifest(StrictModel):
    schema_version: Literal[SCHEMA_VERSION] = SCHEMA_VERSION
    template_id: Identifier
    name: str
    status: TemplateStatus
    latest_revision_id: Identifier | None = None
    active_revision_id: Identifier | None = None
    revisions: list[RevisionRef] = Field(default_factory=list)
    source_hash: str | None = None
    slide_count: Annotated[int, Field(ge=0)] = 0
    aspect_ratio: str | None = None
    thumbnail: str | None = None
    error: str | None = None
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def revision_references_exist(self) -> "TemplateManifest":
        ids = [revision.revision_id for revision in self.revisions]
        if len(ids) != len(set(ids)):
            raise ValueError("manifest revision IDs must be unique")
        if self.latest_revision_id is not None and self.latest_revision_id not in ids:
            raise ValueError("latest_revision_id must be present in revisions")
        if self.active_revision_id is not None and self.active_revision_id not in ids:
            raise ValueError("active_revision_id must be present in revisions")
        return self

    @classmethod
    def load(cls, path: Path | str) -> "TemplateManifest":
        source = Path(path)
        if source.is_dir():
            source = source / "manifest.json"
        return cls.model_validate_json(source.read_text(encoding="utf-8"))

    def save(self, path: Path | str) -> None:
        target = Path(path)
        if target.is_dir() or target.suffix == "":
            target = target / "manifest.json"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.model_dump(mode="json"), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


def dump_model(
    path: Path, value: StrictModel | list[StrictModel] | dict[str, Any]
) -> None:
    """Write a model as stable, human-readable UTF-8 JSON."""

    if isinstance(value, StrictModel):
        payload: Any = value.model_dump(mode="json")
    elif isinstance(value, list):
        payload = [
            item.model_dump(mode="json") if isinstance(item, StrictModel) else item
            for item in value
        ]
    else:
        payload = value
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True),
        encoding="utf-8",
    )
