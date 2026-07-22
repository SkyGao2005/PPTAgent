"""Deterministic PPTX-to-Template-IR extraction based on python-pptx.

Only factual information is extracted here. Semantic page interpretation is a
separate annotator step so it can be replaced without changing source facts.
"""

from __future__ import annotations

import io
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from PIL import Image
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.presentation import Presentation as PresentationType

from .ids import asset_id, sha256_bytes
from .styles import (
    ResolvedTextStyle,
    StyleResolver,
    ThemePalette,
    resolve_color,
    theme_for_master,
)
from .models import (
    Asset,
    AssetOccurrence,
    AssetRole,
    BoundingBox,
    Canvas,
    NormalizedBox,
    PictureCrop,
    ReusePolicy,
    ShapeKind,
    ShapeNode,
    ShapeScope,
    ShapeText,
    SourceGraph,
    TextParagraph,
    TextRun,
    TextStyle,
    Theme,
    ThemeColor,
    ThemeFont,
)


EXTRACTOR_VERSION = "pptx-ir-6"
_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_EMU_PER_INCH = 914400
_BACKGROUND_NAMES = re.compile(r"(^|[\s_-])(background|backdrop|bg)([\s_-]|$)", re.I)
_LOGO_NAMES = re.compile(r"(^|[\s_-])(logo|brand|mark)([\s_-]|$)", re.I)
_DECORATION_NAMES = re.compile(
    r"(^|[\s_-])(decor|decoration|ornament|accent|texture|watermark)([\s_-]|$)",
    re.I,
)


@dataclass(slots=True)
class ExtractedAsset:
    """An Asset model plus the binary that must be persisted."""

    asset: Asset
    blob: bytes


@dataclass(slots=True)
class ExtractionResult:
    canvas: Canvas
    source_graphs: list[SourceGraph]
    assets: list[ExtractedAsset]
    theme: Theme


@dataclass(slots=True)
class _AssetRecord:
    digest: str
    blob: bytes
    extension: str
    media_type: str
    pixel_width: int | None
    pixel_height: int | None
    occurrences: list[AssetOccurrence] = field(default_factory=list)
    explicit_background: bool = False


class PptxExtractor:
    """Extract source graphs, reusable images, and theme facts from a PPTX."""

    version = EXTRACTOR_VERSION

    def extract(self, source_pptx: Path) -> ExtractionResult:
        """Extract one complete presentation without invoking a model."""

        presentation = Presentation(str(source_pptx))
        if not presentation.slides:
            raise ValueError("A template must contain at least one slide")

        canvas = self._canvas(presentation)
        asset_records: dict[str, _AssetRecord] = {}
        source_graphs: list[SourceGraph] = []
        color_counts: Counter[str] = Counter()
        font_counts: Counter[str] = Counter()
        # Fills and fonts resolve against the theme, so the palette is needed
        # before any shape is read.
        palette = (
            theme_for_master(presentation.slide_masters[0])
            if len(presentation.slide_masters)
            else ThemePalette()
        )

        for page_number, slide in enumerate(presentation.slides, start=1):
            slide_id = f"s{page_number:03d}"
            shapes = self._extract_slide_shapes(
                slide,
                slide_id,
                page_number,
                canvas,
                asset_records,
                color_counts,
                font_counts,
                palette,
            )

            background_asset_ids: list[str] = []
            inherited_asset_ids: list[str] = []
            for scope, owner in self._owners(slide):
                if scope is not ShapeScope.SLIDE:
                    assets, chrome = self._extract_inherited_shapes(
                        owner,
                        scope,
                        slide_id,
                        page_number,
                        canvas,
                        asset_records,
                        color_counts,
                        font_counts,
                        palette,
                    )
                    inherited_asset_ids.extend(assets)
                    # Template chrome (colour blocks, rules, logo placement)
                    # lives on the layout and master; generation cannot honour
                    # it unless the IR records its geometry. A layout may hide
                    # the master's shapes, and then they are not on the page.
                    if scope is ShapeScope.MASTER and not self._shows_master_shapes(
                        slide
                    ):
                        chrome = []
                    shapes = chrome + shapes
                background_asset_ids.extend(
                    self._extract_background_assets(
                        owner,
                        scope,
                        slide_id,
                        page_number,
                        canvas,
                        asset_records,
                    )
                )

            full_bleed = [
                asset_id_value
                for shape in shapes
                if self._area(shape.normalized_bbox) >= 0.82
                for asset_id_value in shape.asset_ids
            ]
            background_asset_ids.extend(full_bleed)
            source_graphs.append(
                SourceGraph(
                    slide_id=slide_id,
                    page_number=page_number,
                    canvas=canvas,
                    shapes=shapes,
                    background_asset_ids=self._unique(background_asset_ids),
                    inherited_asset_ids=self._unique(inherited_asset_ids),
                    background_fill=self._background_fill(slide, palette),
                )
            )

        assets = self._finalize_assets(asset_records, len(source_graphs))
        theme = self._theme(canvas, assets, color_counts, font_counts, palette)
        return ExtractionResult(
            canvas=canvas,
            source_graphs=source_graphs,
            assets=assets,
            theme=theme,
        )

    @staticmethod
    def _canvas(presentation: PresentationType) -> Canvas:
        width = int(presentation.slide_width)
        height = int(presentation.slide_height)
        ratio = width / height
        if abs(ratio - 16 / 9) < 0.02:
            aspect_ratio = "16:9"
        elif abs(ratio - 4 / 3) < 0.02:
            aspect_ratio = "4:3"
        else:
            aspect_ratio = f"{ratio:.3f}:1"
        return Canvas(
            width_emu=width,
            height_emu=height,
            width_inches=round(width / _EMU_PER_INCH, 4),
            height_inches=round(height / _EMU_PER_INCH, 4),
            aspect_ratio=aspect_ratio,
        )

    @staticmethod
    def _owners(slide: Any) -> list[tuple[ShapeScope, Any]]:
        return [
            (ShapeScope.SLIDE, slide),
            (ShapeScope.LAYOUT, slide.slide_layout),
            (ShapeScope.MASTER, slide.slide_layout.slide_master),
        ]

    def _extract_slide_shapes(
        self,
        slide: Any,
        slide_id: str,
        page_number: int,
        canvas: Canvas,
        records: dict[str, _AssetRecord],
        colors: Counter[str],
        fonts: Counter[str],
        palette: ThemePalette | None = None,
    ) -> list[ShapeNode]:
        nodes: list[ShapeNode] = []
        resolver = StyleResolver(slide)
        for z_index, shape in enumerate(slide.shapes):
            nodes.extend(
                self._shape_nodes(
                    shape,
                    id_prefix=f"{slide_id}-sh{z_index + 1:04d}",
                    z_index=z_index,
                    scope=ShapeScope.SLIDE,
                    slide_id=slide_id,
                    page_number=page_number,
                    canvas=canvas,
                    owner_part=slide.part,
                    records=records,
                    colors=colors,
                    fonts=fonts,
                    resolver=resolver,
                    palette=palette,
                )
            )
        return nodes

    def _shape_nodes(
        self,
        shape: Any,
        *,
        id_prefix: str,
        z_index: int,
        scope: ShapeScope,
        slide_id: str,
        page_number: int,
        canvas: Canvas,
        owner_part: Any,
        records: dict[str, _AssetRecord],
        colors: Counter[str],
        fonts: Counter[str],
        resolver: StyleResolver | None = None,
        palette: ThemePalette | None = None,
    ) -> list[ShapeNode]:
        bbox = self._bbox(shape)
        normalized = self._normalize(bbox, canvas)
        text = self._extract_text(shape, colors, fonts, resolver)
        shape_id = id_prefix
        asset_ids = self._extract_shape_assets(
            shape,
            owner_part,
            shape_id,
            scope,
            slide_id,
            page_number,
            normalized,
            records,
        )
        children: list[ShapeNode] = []
        child_ids: list[str] = []
        if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.GROUP:
            for child_index, child in enumerate(shape.shapes):
                child_prefix = f"{id_prefix}-c{child_index + 1:03d}"
                child_nodes = self._shape_nodes(
                    child,
                    id_prefix=child_prefix,
                    z_index=child_index,
                    scope=scope,
                    slide_id=slide_id,
                    page_number=page_number,
                    canvas=canvas,
                    owner_part=owner_part,
                    records=records,
                    colors=colors,
                    fonts=fonts,
                    resolver=resolver,
                    palette=palette,
                )
                child_ids.append(child_prefix)
                children.extend(child_nodes)

        crop = None
        if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE:
            crop = PictureCrop(
                left=max(0.0, min(float(shape.crop_left), 1.0)),
                top=max(0.0, min(float(shape.crop_top), 1.0)),
                right=max(0.0, min(float(shape.crop_right), 1.0)),
                bottom=max(0.0, min(float(shape.crop_bottom), 1.0)),
            )
        fill_color = (
            self._resolved_fill(shape, palette)
            if palette is not None
            else self._shape_color(getattr(shape, "fill", None))
        )
        line_color = (
            self._resolved_line(shape, palette)
            if palette is not None
            else self._shape_line_color(shape)
        )
        if fill_color:
            colors[fill_color] += 1
        if line_color:
            colors[line_color] += 1

        node = ShapeNode(
            shape_id=shape_id,
            source_id=self._int_or_none(getattr(shape, "shape_id", None)),
            name=str(getattr(shape, "name", "")),
            scope=scope,
            kind=self._shape_kind(shape),
            z_index=z_index,
            bbox=bbox,
            normalized_bbox=normalized,
            rotation=float(getattr(shape, "rotation", 0.0) or 0.0),
            visible=not self._is_hidden(shape),
            placeholder_type=self._placeholder_type(shape),
            text=text,
            asset_ids=self._unique(asset_ids),
            child_shape_ids=child_ids,
            crop=crop,
            fill_color=fill_color,
            line_color=line_color,
        )
        return [node, *children]

    def _extract_inherited_shapes(
        self,
        owner: Any,
        scope: ShapeScope,
        slide_id: str,
        page_number: int,
        canvas: Canvas,
        records: dict[str, _AssetRecord],
        colors: Counter[str],
        fonts: Counter[str],
        palette: ThemePalette,
    ) -> tuple[list[str], list[ShapeNode]]:
        """Collect inherited assets and the decorative chrome behind a slide."""

        result: list[str] = []
        chrome: list[ShapeNode] = []
        for z_index, (identifier, shape) in enumerate(self._flatten(owner.shapes)):
            shape_id = f"{slide_id}-{scope.value}-sh{identifier}"
            normalized = self._normalize(self._bbox(shape), canvas)
            self._extract_text(shape, colors, fonts)
            asset_ids = self._extract_shape_assets(
                shape,
                owner.part,
                shape_id,
                scope,
                slide_id,
                page_number,
                normalized,
                records,
            )
            result.extend(asset_ids)
            node = self._chrome_node(
                shape, shape_id, scope, z_index, normalized, asset_ids, palette
            )
            if node is not None:
                chrome.append(node)
                if node.fill_color:
                    colors[node.fill_color] += 1
        return self._unique(result), chrome

    def _chrome_node(
        self,
        shape: Any,
        shape_id: str,
        scope: ShapeScope,
        z_index: int,
        normalized: NormalizedBox,
        asset_ids: list[str],
        palette: ThemePalette,
    ) -> ShapeNode | None:
        """Build a node for a non-placeholder layout/master decoration.

        Placeholders are content slots that the slide already contributes, so
        only genuine decoration -- fills, rules, and staged imagery -- is kept.
        """

        if getattr(shape, "is_placeholder", False):
            return None
        fill_color = self._resolved_fill(shape, palette)
        if fill_color is None and not asset_ids:
            return None
        if self._area(normalized) <= 0:
            return None
        return ShapeNode(
            shape_id=shape_id,
            source_id=self._int_or_none(getattr(shape, "shape_id", None)),
            name=str(getattr(shape, "name", "")),
            scope=scope,
            kind=self._shape_kind(shape),
            z_index=z_index,
            bbox=self._bbox(shape),
            normalized_bbox=normalized,
            rotation=float(getattr(shape, "rotation", 0.0) or 0.0),
            asset_ids=asset_ids,
            fill_color=fill_color,
            line_color=self._resolved_line(shape, palette),
        )

    @classmethod
    def _flatten(cls, shapes: Any, prefix: str = "") -> Iterable[tuple[str, Any]]:
        """Walk a shape tree, descending into groups.

        Template decoration is often a single group; the group itself carries
        no fill, so only its children describe what is painted.
        """

        for index, shape in enumerate(shapes, start=1):
            identifier = f"{prefix}{index:04d}"
            if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.GROUP:
                yield from cls._flatten(shape.shapes, prefix=f"{identifier}-")
                continue
            yield identifier, shape

    @staticmethod
    def _shows_master_shapes(slide: Any) -> bool:
        """Whether the slide's layout inherits the master's decorations."""

        value = slide.slide_layout.element.get("showMasterSp")
        return value not in {"0", "false"}

    @staticmethod
    def _shape_properties(shape: Any) -> Any:
        """Return a shape's own ``spPr``/``grpSpPr`` element.

        Scoping matters: a descendant search would happily return the fill of
        a text run or an outline and report it as the shape's background.
        """

        for tag in ("spPr", "grpSpPr"):
            node = shape.element.find(f"./{{{_PRESENTATION_NS}}}{tag}")
            if node is not None:
                return node
        return None

    @staticmethod
    def _resolved_fill(shape: Any, palette: ThemePalette) -> str | None:
        """Resolve a shape fill, including theme-scheme references."""

        direct = PptxExtractor._shape_color(getattr(shape, "fill", None))
        if direct is not None:
            return direct
        properties = PptxExtractor._shape_properties(shape)
        if properties is None:
            return None
        return resolve_color(
            properties.find(f"{{{_DRAWING_NS}}}solidFill"),
            palette,
        )

    @staticmethod
    def _resolved_line(shape: Any, palette: ThemePalette) -> str | None:
        direct = PptxExtractor._shape_line_color(shape)
        if direct is not None:
            return direct
        properties = PptxExtractor._shape_properties(shape)
        if properties is None:
            return None
        line = properties.find(f"{{{_DRAWING_NS}}}ln")
        if line is None:
            return None
        return resolve_color(line.find(f"{{{_DRAWING_NS}}}solidFill"), palette)

    @staticmethod
    def _background_fill(slide: Any, palette: ThemePalette) -> str | None:
        """Resolve the effective page background from slide, layout, or master."""

        for owner in (slide, slide.slide_layout, slide.slide_layout.slide_master):
            background = owner.element.find(f".//{{{_PRESENTATION_NS}}}bg")
            if background is None:
                continue
            color = resolve_color(
                background.find(f".//{{{_DRAWING_NS}}}solidFill"), palette
            )
            if color is not None:
                return color
        return None

    def _extract_shape_assets(
        self,
        shape: Any,
        owner_part: Any,
        shape_id: str,
        scope: ShapeScope,
        slide_id: str,
        page_number: int,
        bbox: NormalizedBox,
        records: dict[str, _AssetRecord],
    ) -> list[str]:
        blobs: list[tuple[bytes, str, str]] = []
        if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.PICTURE:
            try:
                image = shape.image
                blobs.append((image.blob, image.ext, image.content_type))
            except (AttributeError, KeyError, ValueError):
                pass
        else:
            blobs.extend(self._blip_assets(shape.element, owner_part))

        result: list[str] = []
        for blob, extension, media_type in blobs:
            occurrence = AssetOccurrence(
                slide_id=slide_id,
                page_number=page_number,
                scope=scope,
                shape_id=shape_id,
                source_name=str(getattr(shape, "name", "")) or None,
                bbox=bbox,
            )
            result.append(
                self._record_asset(
                    records,
                    blob,
                    extension,
                    media_type,
                    occurrence,
                    explicit_background=False,
                )
            )
        return result

    def _extract_background_assets(
        self,
        owner: Any,
        scope: ShapeScope,
        slide_id: str,
        page_number: int,
        canvas: Canvas,
        records: dict[str, _AssetRecord],
    ) -> list[str]:
        result: list[str] = []
        try:
            elements = owner._element.xpath("./p:bg//a:blip")
        except (AttributeError, KeyError, ValueError):
            elements = []
        for blip in elements:
            rel_id = blip.get(
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
            )
            if not rel_id:
                continue
            part = self._related_part(owner.part, rel_id)
            if part is None or not hasattr(part, "blob"):
                continue
            extension = Path(str(getattr(part, "partname", ".bin"))).suffix.lstrip(".")
            media_type = str(getattr(part, "content_type", "application/octet-stream"))
            occurrence = AssetOccurrence(
                slide_id=slide_id,
                page_number=page_number,
                scope=scope,
                source_name=f"{scope.value} background",
                bbox=NormalizedBox(x=0.0, y=0.0, width=1.0, height=1.0),
            )
            result.append(
                self._record_asset(
                    records,
                    part.blob,
                    extension,
                    media_type,
                    occurrence,
                    explicit_background=True,
                )
            )
        return result

    @staticmethod
    def _blip_assets(element: Any, owner_part: Any) -> list[tuple[bytes, str, str]]:
        assets: list[tuple[bytes, str, str]] = []
        try:
            blips = element.xpath(".//a:blip")
        except (AttributeError, KeyError, ValueError):
            return assets
        for blip in blips:
            rel_id = blip.get(
                "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}embed"
            )
            if not rel_id:
                continue
            part = PptxExtractor._related_part(owner_part, rel_id)
            if part is None or not hasattr(part, "blob"):
                continue
            extension = Path(str(getattr(part, "partname", ".bin"))).suffix.lstrip(".")
            media_type = str(getattr(part, "content_type", "application/octet-stream"))
            assets.append((part.blob, extension, media_type))
        return assets

    @staticmethod
    def _related_part(owner_part: Any, rel_id: str) -> Any | None:
        try:
            return owner_part.related_part(rel_id)
        except (KeyError, AttributeError):
            return None

    def _record_asset(
        self,
        records: dict[str, _AssetRecord],
        blob: bytes,
        extension: str,
        media_type: str,
        occurrence: AssetOccurrence,
        *,
        explicit_background: bool,
    ) -> str:
        digest = sha256_bytes(blob)
        record = records.get(digest)
        if record is None:
            width, height = self._pixel_size(blob)
            record = _AssetRecord(
                digest=digest,
                blob=blob,
                extension=self._safe_extension(extension, media_type),
                media_type=media_type or "application/octet-stream",
                pixel_width=width,
                pixel_height=height,
            )
            records[digest] = record
        if occurrence not in record.occurrences:
            record.occurrences.append(occurrence)
        record.explicit_background |= explicit_background
        return asset_id(digest)

    def _finalize_assets(
        self,
        records: dict[str, _AssetRecord],
        slide_count: int,
    ) -> list[ExtractedAsset]:
        result: list[ExtractedAsset] = []
        for digest, record in sorted(records.items()):
            role = self._asset_role(record, slide_count)
            policy = {
                AssetRole.LOGO: ReusePolicy.ALWAYS,
                AssetRole.BACKGROUND: ReusePolicy.TEMPLATE_ONLY,
                AssetRole.DECORATION: ReusePolicy.TEMPLATE_ONLY,
                AssetRole.CONTENT_IMAGE: ReusePolicy.REFERENCE_ONLY,
                AssetRole.UNKNOWN: ReusePolicy.NEVER,
            }[role]
            identifier = asset_id(digest)
            result.append(
                ExtractedAsset(
                    asset=Asset(
                        asset_id=identifier,
                        sha256=digest,
                        media_type=record.media_type,
                        extension=record.extension,
                        path=f"assets/{identifier}.{record.extension}",
                        byte_size=len(record.blob),
                        pixel_width=record.pixel_width,
                        pixel_height=record.pixel_height,
                        role=role,
                        reuse_policy=policy,
                        occurrences=record.occurrences,
                        tags=[role.value],
                    ),
                    blob=record.blob,
                )
            )
        return result

    def _asset_role(self, record: _AssetRecord, slide_count: int) -> AssetRole:
        names = " ".join(
            occurrence.source_name or "" for occurrence in record.occurrences
        )
        areas = [self._area(item.bbox) for item in record.occurrences if item.bbox]
        pages = {item.page_number for item in record.occurrences}
        recurring = len(pages) >= max(2, (slide_count + 1) // 2)
        edge_small = any(
            occurrence.bbox is not None
            and self._area(occurrence.bbox) <= 0.12
            and (
                occurrence.bbox.x < 0.15
                or occurrence.bbox.y < 0.15
                or occurrence.bbox.x + occurrence.bbox.width > 0.85
                or occurrence.bbox.y + occurrence.bbox.height > 0.85
            )
            for occurrence in record.occurrences
        )
        if record.explicit_background or _BACKGROUND_NAMES.search(names):
            return AssetRole.BACKGROUND
        if areas and max(areas) >= 0.82:
            return AssetRole.BACKGROUND
        if _LOGO_NAMES.search(names) or (recurring and edge_small):
            return AssetRole.LOGO
        thin_or_tiny = any(
            occurrence.bbox is not None
            and (
                self._area(occurrence.bbox) <= 0.025
                or occurrence.bbox.width <= 0.025
                or occurrence.bbox.height <= 0.025
            )
            for occurrence in record.occurrences
        )
        if _DECORATION_NAMES.search(names) or recurring or thin_or_tiny:
            return AssetRole.DECORATION
        return AssetRole.CONTENT_IMAGE

    def _theme(
        self,
        canvas: Canvas,
        assets: list[ExtractedAsset],
        colors: Counter[str],
        fonts: Counter[str],
        palette: ThemePalette | None = None,
    ) -> Theme:
        # The presentation theme is the design system; usage counts only rank
        # the extra colours a template applies on top of it.
        palette = palette or ThemePalette()
        color_tokens = [
            ThemeColor(token=token, value=value, roles=["theme"])
            for token, value in palette.colors.items()
            if token not in {"dk1", "dk2", "lt1", "lt2"}
        ]
        known = {token.value for token in color_tokens}
        color_tokens.extend(
            ThemeColor(token=f"color_{index}", value=value, usage_count=count)
            for index, (value, count) in enumerate(
                ((value, count) for value, count in colors.most_common(12)
                 if value not in known),
                start=1,
            )
        )
        font_tokens = [
            ThemeFont(
                token=token,
                family=family,
                roles=["theme"],
                fallback=["Arial", "sans-serif"],
            )
            for token, family in (
                ("font_major", palette.major_latin),
                ("font_minor", palette.minor_latin),
            )
            if family
        ]
        named = {token.family for token in font_tokens}
        font_tokens.extend(
            ThemeFont(
                token=f"font_{index}",
                family=family,
                usage_count=count,
                fallback=["Arial", "sans-serif"],
            )
            for index, (family, count) in enumerate(
                ((family, count) for family, count in fonts.most_common(8)
                 if family not in named),
                start=1,
            )
        )
        css: dict[str, str] = {}
        for token in color_tokens:
            css[f"--template-{token.token.replace('_', '-')}"] = token.value
        for token in font_tokens:
            css[f"--template-{token.token.replace('_', '-')}"] = token.family
        return Theme(
            canvas=canvas,
            colors=color_tokens,
            fonts=font_tokens,
            css_variables=css,
            logo_asset_ids=[
                value.asset.asset_id
                for value in assets
                if value.asset.role is AssetRole.LOGO
            ],
            background_asset_ids=[
                value.asset.asset_id
                for value in assets
                if value.asset.role is AssetRole.BACKGROUND
            ],
            decoration_asset_ids=[
                value.asset.asset_id
                for value in assets
                if value.asset.role is AssetRole.DECORATION
            ],
        )

    def _extract_text(
        self,
        shape: Any,
        colors: Counter[str],
        fonts: Counter[str],
        resolver: StyleResolver | None = None,
    ) -> ShapeText | None:
        if not getattr(shape, "has_text_frame", False):
            return None
        paragraphs: list[TextParagraph] = []
        for paragraph in shape.text_frame.paragraphs:
            level = int(paragraph.level)
            # PowerPoint resolves unset run properties through the layout,
            # master, and theme; record what a viewer actually sees.
            inherited = (
                resolver.inherited_style(shape, level + 1)
                if resolver is not None
                else None
            )
            runs: list[TextRun] = []
            for run in paragraph.runs:
                style = self._merge_inherited(self._font_style(run.font), inherited)
                if style.font_family:
                    fonts[style.font_family] += 1
                if style.color:
                    colors[style.color] += 1
                runs.append(TextRun(text=run.text, style=style))
            paragraphs.append(
                TextParagraph(
                    text=paragraph.text,
                    level=level,
                    bullet=self._paragraph_has_bullet(paragraph),
                    style=self._merge_inherited(
                        TextStyle(alignment=self._enum_name(paragraph.alignment)),
                        inherited,
                    ),
                    runs=runs,
                )
            )
        text = str(shape.text or "")
        return ShapeText(text=text, paragraphs=paragraphs)

    @staticmethod
    def _merge_inherited(
        style: TextStyle,
        inherited: ResolvedTextStyle | None,
    ) -> TextStyle:
        """Fill unset run properties from the resolved inheritance chain."""

        if inherited is None:
            return style
        updates = {
            name: getattr(inherited, name)
            for name in ("font_family", "size_pt", "bold", "italic", "underline", "color", "alignment")
            if getattr(style, name) is None and getattr(inherited, name) is not None
        }
        return style.model_copy(update=updates) if updates else style

    @staticmethod
    def _font_style(font: Any) -> TextStyle:
        size = float(font.size.pt) if font.size is not None else None
        return TextStyle(
            font_family=font.name,
            size_pt=size,
            bold=font.bold,
            italic=font.italic,
            underline=font.underline,
            color=PptxExtractor._color_value(getattr(font, "color", None)),
            language=PptxExtractor._font_language(font),
        )

    @staticmethod
    def _font_language(font: Any) -> str | None:
        """Read standard or producer-specific language tags without losing facts."""

        try:
            language_id = font.language_id
        except ValueError:
            run_properties = getattr(font, "_rPr", None)
            raw_language = (
                run_properties.get("lang") if run_properties is not None else None
            )
            return str(raw_language) if raw_language else None
        return str(language_id) if language_id is not None else None

    @staticmethod
    def _paragraph_has_bullet(paragraph: Any) -> bool:
        try:
            p_pr = paragraph._p.pPr
            if p_pr is None:
                return False
            return bool(p_pr.xpath("./a:buChar | ./a:buAutoNum | ./a:buBlip"))
        except (AttributeError, KeyError, ValueError):
            return False

    @staticmethod
    def _shape_color(fill: Any) -> str | None:
        if fill is None:
            return None
        try:
            return PptxExtractor._color_value(fill.fore_color)
        except (AttributeError, TypeError):
            return None

    @staticmethod
    def _shape_line_color(shape: Any) -> str | None:
        try:
            return PptxExtractor._color_value(shape.line.color)
        except (AttributeError, TypeError):
            return None

    @staticmethod
    def _color_value(color: Any) -> str | None:
        if color is None:
            return None
        try:
            rgb = color.rgb
        except (AttributeError, TypeError):
            return None
        return f"#{rgb}" if rgb is not None else None

    @staticmethod
    def _bbox(shape: Any) -> BoundingBox:
        return BoundingBox(
            x=int(getattr(shape, "left", 0) or 0),
            y=int(getattr(shape, "top", 0) or 0),
            width=max(0, int(getattr(shape, "width", 0) or 0)),
            height=max(0, int(getattr(shape, "height", 0) or 0)),
        )

    @staticmethod
    def _normalize(bbox: BoundingBox, canvas: Canvas) -> NormalizedBox:
        return NormalizedBox(
            x=round(bbox.x / canvas.width_emu, 6),
            y=round(bbox.y / canvas.height_emu, 6),
            width=round(bbox.width / canvas.width_emu, 6),
            height=round(bbox.height / canvas.height_emu, 6),
        )

    @staticmethod
    def _shape_kind(shape: Any) -> ShapeKind:
        value = getattr(shape, "shape_type", None)
        mapping = {
            MSO_SHAPE_TYPE.AUTO_SHAPE: ShapeKind.AUTO_SHAPE,
            MSO_SHAPE_TYPE.CHART: ShapeKind.CHART,
            MSO_SHAPE_TYPE.DIAGRAM: ShapeKind.DIAGRAM,
            MSO_SHAPE_TYPE.FREEFORM: ShapeKind.FREEFORM,
            MSO_SHAPE_TYPE.GROUP: ShapeKind.GROUP,
            MSO_SHAPE_TYPE.LINE: ShapeKind.LINE,
            MSO_SHAPE_TYPE.MEDIA: ShapeKind.MEDIA,
            MSO_SHAPE_TYPE.PICTURE: ShapeKind.PICTURE,
            MSO_SHAPE_TYPE.PLACEHOLDER: ShapeKind.PLACEHOLDER,
            MSO_SHAPE_TYPE.TABLE: ShapeKind.TABLE,
            MSO_SHAPE_TYPE.TEXT_BOX: ShapeKind.TEXT,
        }
        return mapping.get(value, ShapeKind.UNKNOWN)

    @staticmethod
    def _placeholder_type(shape: Any) -> str | None:
        if not getattr(shape, "is_placeholder", False):
            return None
        try:
            return PptxExtractor._enum_name(shape.placeholder_format.type)
        except (AttributeError, KeyError, ValueError):
            return None

    @staticmethod
    def _enum_name(value: Any) -> str | None:
        if value is None:
            return None
        return str(getattr(value, "name", value)).lower()

    @staticmethod
    def _is_hidden(shape: Any) -> bool:
        try:
            values = shape.element.xpath("./p:nvSpPr/p:cNvPr/@hidden")
            return bool(values and values[0] in {"1", "true"})
        except (AttributeError, KeyError, ValueError):
            return False

    @staticmethod
    def _pixel_size(blob: bytes) -> tuple[int | None, int | None]:
        try:
            with Image.open(io.BytesIO(blob)) as image:
                return int(image.width), int(image.height)
        except (OSError, ValueError):
            return None, None

    @staticmethod
    def _safe_extension(extension: str, media_type: str) -> str:
        value = re.sub(r"[^a-z0-9]", "", extension.lower())
        if value:
            return value[:10]
        subtype = media_type.rpartition("/")[2].split("+")[0]
        return re.sub(r"[^a-z0-9]", "", subtype.lower())[:10] or "bin"

    @staticmethod
    def _area(bbox: NormalizedBox | None) -> float:
        if bbox is None:
            return 0.0
        return bbox.width * bbox.height

    @staticmethod
    def _unique(values: Iterable[str]) -> list[str]:
        return list(dict.fromkeys(values))

    @staticmethod
    def _int_or_none(value: Any) -> int | None:
        return int(value) if value is not None else None
