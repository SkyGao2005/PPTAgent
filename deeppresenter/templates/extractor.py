"""Deterministic PPTX-to-Template-IR extraction based on python-pptx.

Only factual information is extracted here. Semantic page interpretation is a
separate annotator step so it can be replaced without changing source facts.
"""

from __future__ import annotations

import io
import math
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
    ShapeOutline,
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


EXTRACTOR_VERSION = "pptx-ir-16"
_DRAWING_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_PRESENTATION_NS = "http://schemas.openxmlformats.org/presentationml/2006/main"
_EMU_PER_INCH = 914400
_BACKGROUND_NAMES = re.compile(r"(^|[\s_-])(background|backdrop|bg)([\s_-]|$)", re.I)
_LOGO_NAMES = re.compile(r"(^|[\s_-])(logo|brand|mark)([\s_-]|$)", re.I)
_DECORATION_NAMES = re.compile(
    r"(^|[\s_-])(decor|decoration|ornament|accent|texture|watermark)([\s_-]|$)",
    re.I,
)


# Presets with a fixed silhouette, normalized against the shape's own box.
_FIXED_PRESET_POLYGONS: dict[str, tuple[tuple[float, float], ...]] = {
    "triangle": ((0.5, 0.0), (1.0, 1.0), (0.0, 1.0)),
    "rtTriangle": ((0.0, 0.0), (0.0, 1.0), (1.0, 1.0)),
    "diamond": ((0.5, 0.0), (1.0, 0.5), (0.5, 1.0), (0.0, 0.5)),
}
# Presets whose outline is driven by an adjust handle: (default adj, max adj
# as a multiple of w/ss). ECMA-376 measures every adjustment against the
# shape's shorter side, which is why the canvas proportions matter. Presets
# whose formula is not implemented here are left alone -- a polygon built on a
# guessed adjustment is a wrong shape, which is worse than a plain box.
_ADJUSTED_PRESETS = {
    "parallelogram": (25000, 100000),
    "trapezoid": (25000, 50000),
    "homePlate": (16667, 50000),
    "chevron": (50000, 100000),
}
_ROUND_RECT_PRESETS = {"roundRect", "round1Rect", "round2SameRect", "round2DiagRect"}
_DEFAULT_ROUND_RECT_ADJUST = 16667


def _adjust_value(preset: Any, name: str, default: int) -> float:
    """Read one ``<a:gd name=... fmla="val N"/>`` adjustment."""

    node = preset.find(
        f"{{{_DRAWING_NS}}}avLst/{{{_DRAWING_NS}}}gd[@name='{name}']"
    )
    if node is None:
        return float(default)
    formula = str(node.get("fmla", ""))
    if not formula.startswith("val "):
        return float(default)
    try:
        return float(formula[4:])
    except ValueError:
        return float(default)


def _adjusted_polygon(
    name: str,
    preset: Any,
    box: BoundingBox,
) -> list[tuple[float, float]] | None:
    """Build an adjustable preset's outline from its actual handle value."""

    if box.width <= 0 or box.height <= 0:
        return None
    default, max_factor = _ADJUSTED_PRESETS[name]
    shorter = min(box.width, box.height)
    limit = max_factor * box.width / shorter
    adjust = max(0.0, min(_adjust_value(preset, "adj", default), limit))
    # The handle is a fraction of the shorter side, expressed in the box's
    # own width so it can be written as a percentage.
    offset = shorter * adjust / 100000 / box.width
    far = 1.0 - offset
    if name == "parallelogram":
        return [(offset, 0.0), (1.0, 0.0), (far, 1.0), (0.0, 1.0)]
    if name == "trapezoid":
        return [(0.0, 1.0), (offset, 0.0), (far, 0.0), (1.0, 1.0)]
    if name == "homePlate":
        return [(0.0, 0.0), (far, 0.0), (1.0, 0.5), (far, 1.0), (0.0, 1.0)]
    return [
        (0.0, 0.0),
        (far, 0.0),
        (1.0, 0.5),
        (far, 1.0),
        (0.0, 1.0),
        (offset, 0.5),
    ]


@dataclass(frozen=True, slots=True)
class _GroupTransform:
    """Maps a grouped shape's own bounds onto the slide.

    python-pptx reports a grouped shape's offset in its group's child
    coordinate space (``a:chOff``/``a:chExt``), which is not the slide's. Two
    identical groups placed side by side therefore extract to exactly the same
    coordinates, and every card but one disappears under the others.

    A negative scale carries a group's ``flipH``/``flipV`` down to its
    children, which is how a mirrored decoration keeps its real orientation.
    """

    scale_x: float = 1.0
    scale_y: float = 1.0
    translate_x: float = 0.0
    translate_y: float = 0.0

    @property
    def mirrors_x(self) -> bool:
        return self.scale_x < 0

    @property
    def mirrors_y(self) -> bool:
        return self.scale_y < 0

    def apply(self, box: BoundingBox) -> BoundingBox:
        # A mirroring transform swaps which edge is the left one, so the box is
        # rebuilt from both mapped corners rather than from its origin.
        left = box.x * self.scale_x + self.translate_x
        right = (box.x + box.width) * self.scale_x + self.translate_x
        top = box.y * self.scale_y + self.translate_y
        bottom = (box.y + box.height) * self.scale_y + self.translate_y
        return BoundingBox(
            x=round(min(left, right)),
            y=round(min(top, bottom)),
            width=max(0, round(abs(right - left))),
            height=max(0, round(abs(bottom - top))),
        )

    def descend(self, group: Any, placed: BoundingBox) -> "_GroupTransform":
        """Transform for the children of ``group``, already placed on the slide."""

        child = _child_frame(group)
        if child is None:
            return self
        (child_x, child_y, child_width, child_height) = child
        if child_width <= 0 or child_height <= 0:
            return self
        flip_x, flip_y = _flips(group)
        scale_x = placed.width / child_width
        scale_y = placed.height / child_height
        if flip_x:
            scale_x = -scale_x
        if flip_y:
            scale_y = -scale_y
        # The group's own box is the mirror axis: the child frame's far edge
        # has to land on the group's near edge and vice versa.
        anchor_x = placed.x + placed.width if flip_x else placed.x
        anchor_y = placed.y + placed.height if flip_y else placed.y
        return _GroupTransform(
            scale_x=scale_x,
            scale_y=scale_y,
            translate_x=anchor_x - child_x * scale_x,
            translate_y=anchor_y - child_y * scale_y,
        )


def _shape_frame(shape: Any) -> Any | None:
    """A shape's own ``a:xfrm``, not one belonging to a descendant."""

    properties = PptxExtractor._shape_properties(shape)
    if properties is None:
        return None
    return properties.find(f"{{{_DRAWING_NS}}}xfrm")


def _flips(shape: Any) -> tuple[bool, bool]:
    """Read ``flipH``/``flipV`` from a shape's own transform."""

    frame = _shape_frame(shape)
    if frame is None:
        return False, False
    return (
        frame.get("flipH") in {"1", "true"},
        frame.get("flipV") in {"1", "true"},
    )


def _child_frame(group: Any) -> tuple[int, int, int, int] | None:
    """Read a group's ``chOff``/``chExt`` child coordinate frame."""

    frame = _shape_frame(group)
    if frame is None:
        return None
    offset = frame.find(f"{{{_DRAWING_NS}}}chOff")
    extent = frame.find(f"{{{_DRAWING_NS}}}chExt")
    if offset is None or extent is None:
        return None
    return (
        int(offset.get("x", 0)),
        int(offset.get("y", 0)),
        int(extent.get("cx", 0)),
        int(extent.get("cy", 0)),
    )


# Samples per curve segment. High enough that the flattened edge is smooth at
# any projector resolution, and the scaffold costs nothing to carry: it is
# imported by the generated page, never read into a model's context.
_CURVE_SAMPLES = 24

# Sampling grid used to trace an image mask. Enough to follow a slanted or
# rounded photo slot; the polygon is simplified afterwards anyway.
_MASK_ROWS = 33
_MASK_COLUMNS = 65


def _points(command: Any) -> list[tuple[float, float]]:
    return [
        (float(point.get("x", 0)), float(point.get("y", 0)))
        for point in command.findall(f"{{{_DRAWING_NS}}}pt")
    ]


def _bezier(
    start: tuple[float, float],
    controls: list[tuple[float, float]],
) -> list[tuple[float, float]]:
    """Sample a quadratic or cubic Bezier, excluding its start point."""

    nodes = [start, *controls]
    sampled: list[tuple[float, float]] = []
    for step in range(1, _CURVE_SAMPLES + 1):
        t = step / _CURVE_SAMPLES
        # De Casteljau: repeatedly interpolate until one point remains.
        current = nodes
        while len(current) > 1:
            current = [
                (
                    a[0] + (b[0] - a[0]) * t,
                    a[1] + (b[1] - a[1]) * t,
                )
                for a, b in zip(current, current[1:])
            ]
        sampled.append(current[0])
    return sampled


def _arc(
    start: tuple[float, float],
    command: Any,
) -> list[tuple[float, float]]:
    """Sample an ``<a:arcTo>``, which begins at the current point."""

    radius_x = float(command.get("wR", 0) or 0)
    radius_y = float(command.get("hR", 0) or 0)
    # DrawingML angles are 60000ths of a degree, measured clockwise with y
    # pointing down, which is the same sense as the coordinates themselves.
    start_angle = math.radians(float(command.get("stAng", 0) or 0) / 60000.0)
    swing = math.radians(float(command.get("swAng", 0) or 0) / 60000.0)
    if radius_x == 0 or radius_y == 0 or swing == 0:
        return []
    centre_x = start[0] - radius_x * math.cos(start_angle)
    centre_y = start[1] - radius_y * math.sin(start_angle)
    return [
        (
            centre_x + radius_x * math.cos(start_angle + swing * step / _CURVE_SAMPLES),
            centre_y + radius_y * math.sin(start_angle + swing * step / _CURVE_SAMPLES),
        )
        for step in range(1, _CURVE_SAMPLES + 1)
    ]


def _flatten_path(path: Any) -> list[list[tuple[float, float]]] | None:
    """Walk one ``<a:path>`` into its contours, in path coordinates.

    A single ``<a:path>`` may hold several subpaths, each begun by a
    ``moveTo``. The extra ones are usually holes -- the white disc punched out
    of a capsule -- so running them into one polyline draws a shape that never
    existed.
    """

    contours: list[list[tuple[float, float]]] = []
    points: list[tuple[float, float]] = []
    for command in path:
        name = command.tag.rpartition("}")[2]
        if name == "close":
            continue
        if name == "moveTo":
            target = _points(command)
            if not target:
                return None
            if len(points) >= 3:
                contours.append(points)
            points = [target[0]]
        elif name == "lnTo":
            target = _points(command)
            if not target:
                return None
            points.append(target[0])
        elif name in {"cubicBezTo", "quadBezTo"}:
            controls = _points(command)
            if not points or not controls:
                return None
            points.extend(_bezier(points[-1], controls))
        elif name == "arcTo":
            if not points:
                return None
            points.extend(_arc(points[-1], command))
        else:
            return None
    if len(points) >= 3:
        contours.append(points)
    return contours or None


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
    alpha_outline: ShapeOutline | None = None


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
        transform: _GroupTransform = _GroupTransform(),
        group_fill: str | None = None,
    ) -> list[ShapeNode]:
        bbox = transform.apply(self._bbox(shape))
        normalized = self._normalize(bbox, canvas)
        text = self._extract_text(shape, colors, fonts, resolver, palette)
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
        # Resolved before descending: a group's own fill is what its children
        # ask for with <a:grpFill/>.
        fill = (
            self._resolved_fill(shape, palette, group_fill)
            if palette is not None
            else self._shape_color(getattr(shape, "fill", None))
        )
        children: list[ShapeNode] = []
        child_ids: list[str] = []
        if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.GROUP:
            child_transform = transform.descend(shape, bbox)
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
                    transform=child_transform,
                    group_fill=fill,
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
        line_color = (
            self._resolved_line(shape, palette)
            if palette is not None
            else self._shape_line_color(shape)
        )
        for value in self._paint_colors(fill):
            colors[value] += 1
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
            rotation=self._rotation(shape, transform),
            visible=not self._is_hidden(shape),
            placeholder_type=self._placeholder_type(shape),
            text=text,
            asset_ids=self._unique(asset_ids),
            child_shape_ids=child_ids,
            crop=crop,
            fill=fill,
            line_color=line_color,
            line_width_pt=self._line_width_pt(shape),
            outline=self._outline(shape, transform, bbox),
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
        for z_index, (identifier, shape, transform) in enumerate(
            self._flatten(owner.shapes)
        ):
            shape_id = f"{slide_id}-{scope.value}-sh{identifier}"
            bbox = transform.apply(self._bbox(shape))
            normalized = self._normalize(bbox, canvas)
            self._extract_text(shape, colors, fonts, palette=palette)
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
                shape,
                shape_id,
                scope,
                z_index,
                bbox,
                normalized,
                asset_ids,
                palette,
                transform,
            )
            if node is not None:
                chrome.append(node)
                for value in self._paint_colors(node.fill):
                    colors[value] += 1
                if node.line_color:
                    colors[node.line_color] += 1
        return self._unique(result), chrome

    def _chrome_node(
        self,
        shape: Any,
        shape_id: str,
        scope: ShapeScope,
        z_index: int,
        bbox: BoundingBox,
        normalized: NormalizedBox,
        asset_ids: list[str],
        palette: ThemePalette,
        transform: "_GroupTransform",
    ) -> ShapeNode | None:
        """Build a node for a non-placeholder layout/master decoration.

        Placeholders are content slots that the slide already contributes, so
        only genuine decoration -- fills, rules, and staged imagery -- is kept.
        A divider rule paints with a stroke and no fill, so a fill is not what
        makes a shape visible.
        """

        if getattr(shape, "is_placeholder", False):
            return None
        fill = self._resolved_fill(shape, palette)
        line_color = self._resolved_line(shape, palette)
        if fill is None and line_color is None and not asset_ids:
            return None
        # A rule has zero height, so area alone would reject it. What matters
        # is that the shape occupies some space on at least one axis.
        if normalized.width <= 0 and normalized.height <= 0:
            return None
        return ShapeNode(
            shape_id=shape_id,
            source_id=self._int_or_none(getattr(shape, "shape_id", None)),
            name=str(getattr(shape, "name", "")),
            scope=scope,
            kind=self._shape_kind(shape),
            z_index=z_index,
            bbox=bbox,
            normalized_bbox=normalized,
            rotation=self._rotation(shape, transform),
            asset_ids=asset_ids,
            fill=fill,
            line_color=line_color,
            line_width_pt=self._line_width_pt(shape),
            outline=self._outline(shape, transform, bbox),
        )

    @classmethod
    def _flatten(
        cls,
        shapes: Any,
        prefix: str = "",
        transform: _GroupTransform = _GroupTransform(),
    ) -> Iterable[tuple[str, Any, _GroupTransform]]:
        """Walk a shape tree, descending into groups.

        Template decoration is often a single group; the group itself carries
        no fill, so only its children describe what is painted. Each child is
        paired with the transform that places it on the slide.
        """

        for index, shape in enumerate(shapes, start=1):
            identifier = f"{prefix}{index:04d}"
            if getattr(shape, "shape_type", None) == MSO_SHAPE_TYPE.GROUP:
                placed = transform.apply(cls._bbox(shape))
                yield from cls._flatten(
                    shape.shapes,
                    prefix=f"{identifier}-",
                    transform=transform.descend(shape, placed),
                )
                continue
            yield identifier, shape, transform

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
    def _resolved_fill(
        shape: Any,
        palette: ThemePalette,
        group_fill: str | None = None,
    ) -> str | None:
        """Resolve a shape fill to a CSS paint, including gradients.

        The shape's own XML is read first. python-pptx reports only the base
        colour of a fill, dropping the ``lumMod``/``lumOff`` that turn one
        brand colour into the light and dark tints a template is built from.
        """

        properties = PptxExtractor._shape_properties(shape)
        if properties is not None:
            paint = PptxExtractor._fill_css(properties, palette, group_fill)
            if paint is not None:
                return paint
        # No fill of its own: fall back to whatever it inherits.
        return PptxExtractor._shape_color(getattr(shape, "fill", None))

    @staticmethod
    def _fill_css(
        container: Any,
        palette: ThemePalette,
        group_fill: str | None = None,
    ) -> str | None:
        """Read the first solid or gradient fill under ``container`` as CSS.

        ``<a:grpFill/>`` means "paint me with my group's fill". That is why a
        group carries one at all; ignoring it leaves the children colourless
        and tempts the scaffold into painting the container instead.
        """

        if container.find(f"{{{_DRAWING_NS}}}grpFill") is not None:
            return group_fill
        solid = resolve_color(container.find(f"{{{_DRAWING_NS}}}solidFill"), palette)
        if solid is not None:
            return solid
        return PptxExtractor._gradient_css(
            container.find(f"{{{_DRAWING_NS}}}gradFill"),
            palette,
        )

    @staticmethod
    def _gradient_css(gradient: Any, palette: ThemePalette) -> str | None:
        """Translate ``<a:gradFill>`` into a CSS gradient function."""

        if gradient is None:
            return None
        stops: list[tuple[float, str]] = []
        for stop in gradient.findall(
            f"{{{_DRAWING_NS}}}gsLst/{{{_DRAWING_NS}}}gs"
        ):
            color = resolve_color(stop, palette)
            if color is None:
                continue
            position = float(stop.get("pos", 0)) / 1000.0
            stops.append((max(0.0, min(100.0, position)), color))
        if len(stops) < 2:
            return None
        stops.sort(key=lambda item: item[0])
        rendered = ", ".join(f"{color} {position:g}%" for position, color in stops)
        if gradient.find(f"{{{_DRAWING_NS}}}path") is not None:
            return f"radial-gradient({rendered})"
        line = gradient.find(f"{{{_DRAWING_NS}}}lin")
        angle = float(line.get("ang", 0)) / 60000.0 if line is not None else 0.0
        # DrawingML measures clockwise from the positive x-axis; CSS measures
        # clockwise from "to top".
        return f"linear-gradient({(angle + 90) % 360:g}deg, {rendered})"

    @staticmethod
    def _paint_colors(paint: str | None) -> list[str]:
        """Hex colours inside a CSS paint, for theme palette ranking."""

        return re.findall(r"#[0-9A-Fa-f]{6}", paint or "")

    @staticmethod
    def _line_width_pt(shape: Any) -> float | None:
        width = getattr(getattr(shape, "line", None), "width", None)
        return float(width.pt) if width else None

    @staticmethod
    def _outline(
        shape: Any,
        transform: "_GroupTransform" = None,  # type: ignore[assignment]
        box: BoundingBox = BoundingBox(x=0, y=0, width=1, height=1),
    ) -> ShapeOutline | None:
        """Record a non-rectangular silhouette, when the shape has one.

        A mirrored shape keeps its bounding box but not its silhouette, so the
        flip is baked into the polygon. Ellipses and rounded corners are
        symmetric and need no adjustment.
        """

        transform = transform or _GroupTransform()
        properties = PptxExtractor._shape_properties(shape)
        if properties is None:
            return None
        own_flip_x, own_flip_y = _flips(shape)
        flip_x = own_flip_x != transform.mirrors_x
        flip_y = own_flip_y != transform.mirrors_y

        def mirrored(points: Iterable[tuple[float, float]]) -> list[tuple[float, float]]:
            return [
                (
                    round(1.0 - x, 4) if flip_x else x,
                    round(1.0 - y, 4) if flip_y else y,
                )
                for x, y in points
            ]

        custom = properties.find(f"{{{_DRAWING_NS}}}custGeom")
        if custom is not None:
            contours = PptxExtractor._custom_polygon(custom)
            if not contours:
                return None
            return ShapeOutline(
                polygon=mirrored(contours[0]),
                holes=[mirrored(hole) for hole in contours[1:]],
            )
        preset = properties.find(f"{{{_DRAWING_NS}}}prstGeom")
        if preset is None:
            return None
        name = preset.get("prst", "")
        if name in {"ellipse", "circle"}:
            return ShapeOutline(ellipse=True)
        if name in _ROUND_RECT_PRESETS:
            radius = (
                min(50000.0, _adjust_value(preset, "adj", _DEFAULT_ROUND_RECT_ADJUST))
                / 100000
            )
            return ShapeOutline(corner_radius=radius) if radius > 0 else None
        polygon = _FIXED_PRESET_POLYGONS.get(name)
        if polygon is None and name in _ADJUSTED_PRESETS:
            polygon = _adjusted_polygon(name, preset, box)
        return ShapeOutline(polygon=mirrored(polygon)) if polygon else None

    @staticmethod
    def _rotation(shape: Any, transform: "_GroupTransform") -> float:
        """Slide-space rotation, accounting for mirroring.

        Mirroring one axis reverses the sense of a rotation; mirroring both is
        a half turn, which leaves it unchanged.
        """

        angle = float(getattr(shape, "rotation", 0.0) or 0.0)
        own_flip_x, own_flip_y = _flips(shape)
        flip_x = own_flip_x != transform.mirrors_x
        flip_y = own_flip_y != transform.mirrors_y
        return -angle % 360 if angle and flip_x != flip_y else angle

    @staticmethod
    def _custom_polygon(custom: Any) -> list[list[tuple[float, float]]] | None:
        """Normalize a ``<a:custGeom>`` outline to its box.

        Curves are flattened here rather than skipped. ``clip-path: path()``
        would keep them exactly but resolves in absolute pixels, so it cannot
        follow a percentage-sized box; a dense polygon is the only silhouette
        CSS will scale. Skipping them instead left every rounded decoration
        rendered as the rectangle it never was.
        """

        contours: list[list[tuple[float, float]]] = []
        for path in custom.findall(f"{{{_DRAWING_NS}}}pathLst/{{{_DRAWING_NS}}}path"):
            width = float(path.get("w", 0) or 0)
            height = float(path.get("h", 0) or 0)
            if width <= 0 or height <= 0:
                continue
            traced = _flatten_path(path)
            if traced is None:
                return None
            for points in traced:
                scaled = [
                    (round(x / width, 4), round(y / height, 4)) for x, y in points
                ]
                # A closed contour repeats its first point; CSS closes its own.
                if len(scaled) > 3 and scaled[0] == scaled[-1]:
                    scaled.pop()
                if len(scaled) >= 3:
                    contours.append(PptxExtractor._drop_collinear(scaled))
        if not contours:
            return None
        # The outer boundary comes first so the even-odd rule reads the rest as
        # holes. Area is what tells them apart, not order in the file.
        contours.sort(key=PptxExtractor._polygon_area, reverse=True)
        return contours

    @staticmethod
    def _polygon_area(points: list[tuple[float, float]]) -> float:
        """Twice the enclosed area, unsigned -- used only to rank contours."""

        total = 0.0
        for (ax, ay), (bx, by) in zip(points, points[1:] + points[:1]):
            total += ax * by - bx * ay
        return abs(total)

    @staticmethod
    def _drop_collinear(
        points: list[tuple[float, float]],
        tolerance: float = 1e-6,
    ) -> list[tuple[float, float]]:
        """Drop points that lie on the line between their neighbours.

        Curve flattening emits runs of points on the same straight segment.
        The tolerance is small enough to be invisible at any render size --
        this removes redundancy, not detail. The scaffold is never read by a
        model, so there is nothing to gain from simplifying beyond that.
        """

        kept = [points[0]]
        for index in range(1, len(points) - 1):
            ax, ay = kept[-1]
            bx, by = points[index]
            cx, cy = points[index + 1]
            # Twice the triangle's area: zero when the three points are on a
            # line, and it grows with both the detour and its span.
            if abs((bx - ax) * (cy - ay) - (cx - ax) * (by - ay)) > tolerance:
                kept.append((bx, by))
        kept.append(points[-1])
        return kept if len(kept) >= 3 else points

    @staticmethod
    def _resolved_line(shape: Any, palette: ThemePalette) -> str | None:
        properties = PptxExtractor._shape_properties(shape)
        if properties is not None:
            line = properties.find(f"{{{_DRAWING_NS}}}ln")
            if line is not None:
                # As with fills, the XML keeps the luminance modifiers.
                color = resolve_color(
                    line.find(f"{{{_DRAWING_NS}}}solidFill"), palette
                )
                if color is not None:
                    return color
        return PptxExtractor._shape_line_color(shape)

    @staticmethod
    def _background_fill(slide: Any, palette: ThemePalette) -> str | None:
        """Resolve the effective page background from slide, layout, or master."""

        for owner in (slide, slide.slide_layout, slide.slide_layout.slide_master):
            background = owner.element.find(f".//{{{_PRESENTATION_NS}}}bg")
            if background is None:
                continue
            properties = background.find(f"{{{_PRESENTATION_NS}}}bgPr")
            paint = PptxExtractor._fill_css(
                properties if properties is not None else background,
                palette,
            )
            if paint is None:
                # <p:bgRef> names a theme fill style plus its colour. Most
                # templates use the plain style, and the referenced colour is
                # the right answer for the rest too.
                paint = resolve_color(
                    background.find(f"{{{_PRESENTATION_NS}}}bgRef"), palette
                )
            if paint is not None:
                return paint
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
                alpha_outline=self._alpha_outline(blob),
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
                        alpha_outline=record.alpha_outline,
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
        # An image painted by a layout or master is template chrome by
        # definition: the slide never supplied it, so generation can never
        # replace it. Scope settles the question that the name and size
        # heuristics below can only guess at -- a one-off cover logo is not
        # "recurring" and its name is often just "Picture 2".
        chrome = any(
            occurrence.scope is not ShapeScope.SLIDE
            for occurrence in record.occurrences
        )
        if record.explicit_background or _BACKGROUND_NAMES.search(names):
            return AssetRole.BACKGROUND
        if areas and max(areas) >= 0.82:
            return AssetRole.BACKGROUND
        if _LOGO_NAMES.search(names) or (recurring and edge_small):
            return AssetRole.LOGO
        if chrome:
            return AssetRole.DECORATION
        # Artwork cut to a bespoke silhouette was made for this slot at this
        # position: the mask encodes the layout, so no other image can take
        # its place. A sample photograph is always a plain rectangle.
        if record.alpha_outline is not None:
            return AssetRole.DECORATION
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
        palette: ThemePalette | None = None,
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
                style = self._merge_inherited(
                    self._font_style(run.font, palette),
                    inherited,
                    self._RUN_PROPERTIES,
                )
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

    # Alignment belongs to a paragraph, never to a run inside it. Letting a
    # run inherit one records a fact that is not true: the master's list style
    # says "left" while the paragraph itself is centred, and whoever reads the
    # run wins the argument.
    _RUN_PROPERTIES = (
        "font_family",
        "size_pt",
        "bold",
        "italic",
        "underline",
        "color",
    )
    _PARAGRAPH_PROPERTIES = (*_RUN_PROPERTIES, "alignment")

    @staticmethod
    def _merge_inherited(
        style: TextStyle,
        inherited: ResolvedTextStyle | None,
        properties: tuple[str, ...] | None = None,
    ) -> TextStyle:
        """Fill unset properties from the resolved inheritance chain."""

        if inherited is None:
            return style
        updates = {
            name: getattr(inherited, name)
            for name in (properties or PptxExtractor._PARAGRAPH_PROPERTIES)
            if getattr(style, name) is None and getattr(inherited, name) is not None
        }
        return style.model_copy(update=updates) if updates else style

    @staticmethod
    def _font_style(font: Any, palette: ThemePalette | None = None) -> TextStyle:
        size = float(font.size.pt) if font.size is not None else None
        return TextStyle(
            font_family=font.name,
            size_pt=size,
            bold=font.bold,
            italic=font.italic,
            underline=font.underline,
            color=PptxExtractor._run_color(font, palette),
            language=PptxExtractor._font_language(font),
        )

    @staticmethod
    def _run_color(font: Any, palette: ThemePalette | None) -> str | None:
        """Resolve a run's own colour, scheme and preset values included.

        python-pptx exposes ``font.color.rgb`` only for a literal RGB value and
        raises for everything else, so white text written as ``schemeClr bg1``
        read as no colour at all -- and then inherited black from the theme.
        """

        if palette is not None:
            run_properties = getattr(font, "_rPr", None)
            if run_properties is not None:
                color = resolve_color(
                    run_properties.find(f"{{{_DRAWING_NS}}}solidFill"),
                    palette,
                )
                if color is not None:
                    return color
        return PptxExtractor._color_value(getattr(font, "color", None))

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
    def _alpha_outline(blob: bytes) -> ShapeOutline | None:
        """Trace the opaque area of a masked image as a polygon.

        A picture fills its frame, so the mask's normalized coordinates are the
        region's. Scanning rows for the first and last opaque column recovers
        the slanted and rounded photo slots templates are built from; anything
        essentially rectangular is left alone.
        """

        try:
            with Image.open(io.BytesIO(blob)) as image:
                if "A" not in image.getbands():
                    return None
                alpha = image.getchannel("A")
                width, height = alpha.size
                if width < 2 or height < 2:
                    return None
                pixels = alpha.load()
        except (OSError, ValueError):
            return None

        rows = min(_MASK_ROWS, height)
        columns = min(_MASK_COLUMNS, width)
        left: list[tuple[float, float]] = []
        right: list[tuple[float, float]] = []
        opaque = 0
        for step in range(rows):
            y = min(height - 1, round(step * (height - 1) / (rows - 1)))
            spans = [
                column
                for column in range(columns)
                if pixels[min(width - 1, round(column * (width - 1) / (columns - 1))), y]
                > 128
            ]
            if not spans:
                continue
            opaque += len(spans)
            position = y / (height - 1)
            left.append((round(spans[0] / (columns - 1), 4), round(position, 4)))
            right.append((round(spans[-1] / (columns - 1), 4), round(position, 4)))
        if len(left) < 2:
            return None
        # A mask that covers nearly everything is a rectangle with soft edges,
        # and clipping to it would only shave pixels off a full-bleed image.
        if opaque >= rows * columns * 0.98:
            return None
        polygon = left + list(reversed(right))
        return ShapeOutline(polygon=PptxExtractor._drop_collinear(polygon))

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
