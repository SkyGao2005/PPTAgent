"""Generate a CSS layout scaffold from one annotated template page.

Region geometry only guides generation if it is easy to obey. Emitting the
grid as ready-to-use absolute-positioned classes makes following the template
the least-effort path, instead of asking a model to transcribe coordinates.
"""

from __future__ import annotations

from .models import (
    Region,
    Semantic,
    ShapeNode,
    ShapeScope,
    SourceGraph,
    TextStyle,
    Theme,
)

_ALIGNMENT_CSS = {
    "CENTER": "center",
    "RIGHT": "right",
    "LEFT": "left",
    "JUSTIFY": "justify",
    "ctr": "center",
    "r": "right",
    "l": "left",
    "just": "justify",
}


def _percent(value: float) -> str:
    return f"{round(value * 100, 3):g}%"


def _dominant_style(
    region: Region,
    shapes: dict[str, ShapeNode],
) -> TextStyle | None:
    """Pick the style of the largest run backing this region."""

    best: TextStyle | None = None
    for shape_id in region.source_shape_ids:
        shape = shapes.get(shape_id)
        if shape is None or shape.text is None:
            continue
        for paragraph in shape.text.paragraphs:
            candidates = [run.style for run in paragraph.runs] or [paragraph.style]
            for style in candidates:
                if best is None or (style.size_pt or 0) > (best.size_pt or 0):
                    best = style
    return best


def _declarations(
    region: Region,
    style: TextStyle | None,
    canvas_height_inches: float,
) -> list[str]:
    box = region.bbox
    declarations = [
        "position:absolute",
        f"left:{_percent(box.x)}",
        f"top:{_percent(box.y)}",
        f"width:{_percent(box.width)}",
        f"height:{_percent(box.height)}",
    ]
    if style is None:
        return declarations
    if style.size_pt:
        # Slide points map to viewport height so the scaffold scales with any
        # render size the converter picks.
        declarations.append(
            f"font-size:{round(style.size_pt / (canvas_height_inches * 72) * 100, 3):g}vh"
        )
    if style.font_family:
        declarations.append(f'font-family:"{style.font_family}"')
    if style.color:
        declarations.append(f"color:{style.color}")
    if style.bold is not None:
        declarations.append(f"font-weight:{700 if style.bold else 400}")
    if style.italic:
        declarations.append("font-style:italic")
    alignment = _ALIGNMENT_CSS.get(str(style.alignment))
    if alignment:
        declarations.append(f"text-align:{alignment}")
    return declarations


def _chrome_rules(graph: SourceGraph) -> list[str]:
    """Emit the template's own decoration as reproducible CSS.

    Colour blocks, rules, and logo placement live on the layout and master.
    Reproducing them exactly is deterministic, so generation never has to
    approximate them from a reference screenshot.
    """

    rules: list[str] = []
    chrome = [shape for shape in graph.shapes if shape.scope is not ShapeScope.SLIDE]
    for index, shape in enumerate(chrome, start=1):
        box = shape.normalized_bbox
        declarations = [
            "position:absolute",
            f"left:{_percent(box.x)}",
            f"top:{_percent(box.y)}",
            f"width:{_percent(box.width)}",
            f"height:{_percent(box.height)}",
            f"z-index:{index}",
        ]
        if shape.fill_color:
            declarations.append(f"background:{shape.fill_color}")
        if shape.rotation:
            declarations.append(f"transform:rotate({shape.rotation:g}deg)")
        if shape.asset_ids:
            declarations.append("object-fit:contain")
        note = f"{shape.scope.value} / {shape.name}"
        if shape.asset_ids:
            note += f" -> assets/{shape.asset_ids[0]}"
        rules.append(f".tpl-{index:02d}{{{';'.join(declarations)}}} /* {note} */")
    return rules


def build_layout_css(
    semantic: Semantic,
    graph: SourceGraph,
    theme: Theme,
) -> str:
    """Render one page's regions as absolute-positioned CSS classes."""

    shapes = {shape.shape_id: shape for shape in graph.shapes}
    height_inches = graph.canvas.height_inches or 7.5
    background = (
        f"background:{graph.background_fill}" if graph.background_fill else ""
    )
    lines = [
        f"/* Layout scaffold for {semantic.slide_id} "
        f"({semantic.page_semantics.stage.value}/"
        f"{semantic.page_semantics.layout_pattern.value}).",
        " * Position content with these classes so the generated page keeps the",
        " * template grid. Override a value only when the content demands it.",
        " * .tpl-* classes reproduce the template's own chrome: emit them as",
        " * empty divs (or <img> for the ones naming an asset) before content.",
        " */",
        f".slide{{position:relative;width:100%;height:100%;overflow:hidden;{background}}}",
    ]
    if theme.css_variables:
        lines.append(":root{")
        lines.extend(
            f"  {name}:{value};" for name, value in sorted(theme.css_variables.items())
        )
        lines.append("}")
    lines.extend(_chrome_rules(graph))
    for region in semantic.regions:
        style = _dominant_style(region, shapes)
        selector = f".r-{region.region_id}"
        declarations = ";".join(_declarations(region, style, height_inches))
        lines.append(
            f"{selector}{{{declarations}}}"
            f" /* {region.role.value} / {region.kind.value} */"
        )
    safe = theme.safe_area
    lines.append(
        ".safe-area{"
        f"position:absolute;left:{_percent(safe.x)};top:{_percent(safe.y)};"
        f"width:{_percent(safe.width)};height:{_percent(safe.height)}"
        "}"
    )
    return "\n".join(lines) + "\n"
