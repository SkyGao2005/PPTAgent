"""Generate a CSS layout scaffold from one annotated template page.

The scaffold carries two kinds of rules with two different contracts:

- ``.tpl-*`` chrome reproduces the template's identity: decoration the
  layout and master put on every page (logos, colour bands, the page
  background), plus slide-drawn artwork that recurs across the revision's
  pages and that no annotator judged content-coupled. It is deterministic,
  exact, and deliberately written at ``.slide .tpl-*`` specificity so a
  page's utility classes cannot disturb it.
- ``.dec-*`` rules reproduce the rest of the sample slide's own decoration --
  artwork the designer placed around the sample's content. Included by
  default, but single-class and droppable, because it follows the content it
  was drawn around.
- ``.r-*`` regions describe how the *sample* page arranged its content. They
  are a starting point, not a cage: the sample's box was sized around sample
  text, its colour was chosen for the sample's own ground, and neither fact
  necessarily survives new content. Region rules are therefore single-class,
  everything-in-one-rule defaults that a page can retune by redeclaring the
  class after the import.
"""

from __future__ import annotations

import re
from typing import Mapping, Sequence
from urllib.parse import quote

from .models import (
    Asset,
    Canvas,
    NormalizedBox,
    Region,
    ReusePolicy,
    Semantic,
    ShapeKind,
    ShapeNode,
    ShapeOutline,
    ShapeScope,
    SourceGraph,
    TextStyle,
    Theme,
)

# A revision stores the scaffold at ``slides/<id>/layout.css`` and a task pack
# stores it at ``refs/<id>/layout.css``. Both sit two levels above ``assets/``,
# so one relative prefix is correct wherever the file is rendered from.
ASSET_URL_PREFIX = "../../assets"

# python-pptx reports an enum name ("center"); the inheritance chain reports
# the raw DrawingML token ("ctr"). Both reach here, so both are mapped, and
# the lookup is lowercased because ``_enum_name`` already is.
_ALIGNMENT_CSS = {
    "center": "center",
    "right": "right",
    "left": "left",
    "justify": "justify",
    "justify_low": "justify",
    "distribute": "justify",
    "thai_distribute": "justify",
    "ctr": "center",
    "r": "right",
    "l": "left",
    "just": "justify",
    "justlow": "justify",
    "dist": "justify",
    "thaidist": "justify",
}
# PowerPoint's own default for left-to-right text. Writing it out means a page
# never has to guess, and never inherits an alignment from its own stylesheet.
_DEFAULT_ALIGNMENT = "left"


def _percent(value: float) -> str:
    return f"{round(value * 100, 3):g}%"


def _dominant_style(
    region: Region,
    shapes: dict[str, ShapeNode],
) -> TextStyle | None:
    """Pick the style of the largest run backing this region.

    Alignment comes from the paragraph that owns that run, because that is
    where PowerPoint keeps it. Reading it off the run yields whatever the
    master's list style says -- "left" under a centred paragraph.
    """

    best: TextStyle | None = None
    alignment: str | None = None
    for shape_id in region.source_shape_ids:
        shape = shapes.get(shape_id)
        if shape is None or shape.text is None:
            continue
        for paragraph in shape.text.paragraphs:
            candidates = [run.style for run in paragraph.runs] or [paragraph.style]
            for style in candidates:
                if best is None or (style.size_pt or 0) > (best.size_pt or 0):
                    best = style
                    alignment = paragraph.style.alignment
    if best is None:
        return None
    return best.model_copy(update={"alignment": alignment})


def _paint_order(graph: SourceGraph) -> dict[str, int]:
    """Rank every shape by the order the template paints it.

    Chrome and content share one stacking order -- the template's own. Giving
    content a separate band above the decoration looks safe but inverts pages
    where a photo sits at the bottom of the pile: it then covers the title it
    was meant to sit behind. ``graph.shapes`` is already in paint order, with
    inherited chrome ahead of the slide's own shapes.
    """

    # Positioned elements with z-index:auto never paint above a positive one,
    # so every rank starts at 1 rather than 0.
    return {shape.shape_id: index + 1 for index, shape in enumerate(graph.shapes)}


def _geometry(box: NormalizedBox, z_index: int) -> list[str]:
    return [
        "position:absolute",
        f"left:{_percent(box.x)}",
        f"top:{_percent(box.y)}",
        f"width:{_percent(box.width)}",
        f"height:{_percent(box.height)}",
        f"z-index:{z_index}",
    ]


def _typography(
    style: TextStyle | None,
    canvas_height_inches: float,
) -> list[str]:
    """Fitting properties: inherited, low specificity, meant to be adjusted."""

    declarations: list[str] = []
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
    if style.bold is not None:
        declarations.append(f"font-weight:{700 if style.bold else 400}")
    if style.italic:
        declarations.append("font-style:italic")
    declarations.append(f"text-align:{alignment_css(style.alignment)}")
    return declarations


def alignment_css(alignment: str | None) -> str:
    """The template's horizontal alignment, always stated."""

    return _ALIGNMENT_CSS.get(str(alignment).lower(), _DEFAULT_ALIGNMENT)


def _sample_ground_dropped(
    region: Region,
    shapes: Mapping[str, ShapeNode],
    assets: Mapping[str, Asset],
) -> bool:
    """Whether the sample's text sat on paint this scaffold does not emit.

    A card fill or a photo inside a content region is sample material, so the
    scaffold deliberately leaves it out -- but the sample's text colour was
    chosen against exactly that ground. Handing the colour over without the
    ground produced white text on a white page. When the ground is gone, the
    colour is not a fact worth transferring.
    """

    if _region_asset(region, assets) is not None:
        # The rule paints the template's own artwork, so the ground survives.
        return False
    if region.asset_ids:
        # The region binds imagery the rule does not paint -- sample
        # photography stays reference-only, so its ground is gone.
        return True
    stack = list(region.source_shape_ids)
    while stack:
        shape = shapes.get(stack.pop())
        if shape is None:
            continue
        if shape.fill or shape.asset_ids:
            return True
        stack.extend(shape.child_shape_ids)
    return False


def asset_url_name(asset: Asset) -> str:
    """Filename a chrome rule points at, shared with task-pack staging.

    A revision may keep an unrenderable original (WMF, EMF) next to a browser
    variant. The scaffold must name the file a browser can actually load, and
    staging must write it under exactly that name.
    """

    return (asset.browser_path or asset.path).rpartition("/")[2]


def _with_holes(
    outer: list[tuple[float, float]],
    holes: list[list[tuple[float, float]]],
) -> list[tuple[float, float]]:
    """Thread holes into one contour that ``polygon()`` can express.

    CSS has a fill rule but no notion of subpaths, so each hole is reached by
    a bridge from the outer contour's first point and left along the very same
    line. Coincident bridges enclose nothing; a naive concatenation instead
    leaves two edges a hair apart, and that sliver renders as a white slit
    down the middle of the shape.
    """

    if not holes:
        return outer
    points = list(outer)
    for hole in holes:
        points.append(outer[0])
        points.extend(hole)
        points.append(hole[0])
    return points


def _outline_css(
    outline: ShapeOutline | None,
    box: NormalizedBox,
    canvas: Canvas,
) -> list[str]:
    """Turn a recorded silhouette into the one CSS property that draws it."""

    if outline is None:
        return []
    if outline.polygon:
        points = _with_holes(outline.polygon, outline.holes)
        rendered = ", ".join(f"{_percent(x)} {_percent(y)}" for x, y in points)
        rule = "evenodd, " if outline.holes else ""
        return [f"clip-path:polygon({rule}{rendered})"]
    if outline.ellipse:
        return ["border-radius:50%"]
    if outline.corner_radius:
        # The radius is a fraction of the box's shorter side measured on the
        # slide, so the canvas proportions decide which side that is. A
        # percentage radius then resolves against each axis separately, and
        # splitting it keeps the arc circular on a box of any shape.
        width = box.width * canvas.width_emu
        height = box.height * canvas.height_emu
        if width <= 0 or height <= 0:
            return []
        radius = outline.corner_radius * min(width, height)
        return [
            f"border-radius:{_percent(radius / width)} / "
            f"{_percent(radius / height)}"
        ]
    return []


def _stroked_outline_svg(shape: ShapeNode) -> str | None:
    """Draw a clipped shape's own stroke, which CSS otherwise cannot.

    ``clip-path`` clips a fill; ``border`` follows the element's box. A shape
    that is a stroked path with no fill -- line-art icons are always built
    that way -- gets neither: the border is drawn round the box and then the
    clip cuts it into fragments. An inline SVG strokes the path itself, scales
    with the box, and rasterizes on export like any other background image.
    """

    outline = shape.outline
    if outline is None or not outline.polygon or not shape.line_color:
        return None
    if shape.fill:
        return None

    def contour(points: list[tuple[float, float]]) -> str:
        head = f"M{points[0][0] * 100:g},{points[0][1] * 100:g}"
        rest = "".join(f"L{x * 100:g},{y * 100:g}" for x, y in points[1:])
        return f"{head}{rest}Z"

    path = "".join(contour(c) for c in [outline.polygon, *outline.holes])
    width = max(1, round(shape.line_width_pt or 1))
    svg = (
        "<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 100 100' "
        "preserveAspectRatio='none'>"
        f"<path d='{path}' fill='none' stroke='{shape.line_color}' "
        # The stroke must not stretch with the viewBox, or a wide box would
        # thicken it horizontally and thin it vertically.
        f"stroke-width='{width}' vector-effect='non-scaling-stroke'/></svg>"
    )
    return quote(svg, safe="/:=';,<>? ")


def _paint_css(shape: ShapeNode, assets: Mapping[str, Asset]) -> list[str]:
    """Background layers and stroke for one decoration."""

    declarations: list[str] = []
    layers: list[str] = []
    for asset_id in shape.asset_ids:
        asset = assets.get(asset_id)
        if asset is not None:
            layers.append(f"url({ASSET_URL_PREFIX}/{asset_url_name(asset)})")
    images = bool(layers)
    fill = shape.fill
    if fill and not fill.startswith("#"):
        # A gradient is a background image, not a colour, and it paints under
        # any artwork the shape also carries.
        layers.append(fill)
        fill = None
    if fill:
        declarations.append(f"background-color:{fill}")
    if layers:
        declarations.append(f"background-image:{','.join(layers)}")
    if images:
        # Only real artwork needs fitting; a gradient already fills its box.
        declarations.extend(
            ["background-size:100% 100%", "background-repeat:no-repeat"]
        )
    stroke = _stroked_outline_svg(shape)
    if stroke is not None:
        declarations.extend(
            [
                f'background-image:url("data:image/svg+xml,{stroke}")',
                "background-size:100% 100%",
                "background-repeat:no-repeat",
            ]
        )
    elif shape.line_color:
        width = max(1, round(shape.line_width_pt or 1))
        # A divider is a box with no thickness. Boxing it on four sides would
        # double the rule and outline empty space, so it draws one edge.
        box = shape.normalized_bbox
        if box.height <= 0:
            edge = "border-top"
        elif box.width <= 0:
            edge = "border-left"
        else:
            edge = "border"
        declarations.append(f"{edge}:{width}px solid {shape.line_color}")
    return declarations


def _paints(shape: ShapeNode) -> bool:
    """Whether a div can reproduce what this decoration puts on the page.

    A connector strokes one corner of its box to the other, which a div can
    draw only while the box is flat -- a diagonal one would become a rectangle
    it never was, and a wrong shape contradicts the reference image.
    """

    if shape.kind is ShapeKind.GROUP:
        # A group is a container. Its fill exists so children can ask for it
        # with <a:grpFill/>; painting the container turns a pair of chevrons
        # into the solid rectangle enclosing them.
        return False
    if not (shape.fill or shape.line_color or shape.asset_ids):
        return False
    if shape.kind is not ShapeKind.LINE:
        return True
    box = shape.normalized_bbox
    return min(box.width, box.height) < 0.002


def chrome_signature(shape: ShapeNode) -> tuple[object, ...]:
    """Identity of a decoration independent of the page that drew it.

    Two pages carrying the same wedge drew the same artwork twice; the
    signature is what makes that observable across a revision. Geometry is
    rounded so EMU jitter between hand-copied shapes does not split them.
    """

    box = shape.normalized_bbox
    return (
        round(box.x, 4),
        round(box.y, 4),
        round(box.width, 4),
        round(box.height, 4),
        shape.kind.value,
        shape.fill,
        shape.line_color,
        shape.line_width_pt,
        shape.rotation,
    )


def recurring_chrome_signatures(
    graphs: Sequence[SourceGraph],
) -> frozenset[tuple[object, ...]]:
    """Slide-drawn decoration signatures seen on more than one page.

    Scope records where an author drew a shape, not what it is: brand wedges
    hand-copied onto the cover and the closing page are slide-scope, yet they
    are the template's identity. Recurrence across pages is the mechanical
    evidence for that, and only a whole-revision pass can observe it -- so
    the compiler calls this once and threads the result into every page.
    """

    pages_by_signature: dict[tuple[object, ...], set[str]] = {}
    for graph in graphs:
        for shape in graph.shapes:
            if shape.is_chrome and shape.scope is ShapeScope.SLIDE:
                pages_by_signature.setdefault(
                    chrome_signature(shape), set()
                ).add(graph.slide_id)
    return frozenset(
        signature
        for signature, pages in pages_by_signature.items()
        if len(pages) >= 2
    )


def _is_template_tier(
    shape: ShapeNode,
    recurring_chrome: frozenset[tuple[object, ...]],
    judged: frozenset[str],
    fixed: frozenset[str],
) -> bool:
    """Whether a chrome shape is the template's identity or the sample's.

    Scope answers it for layout and master shapes. A slide-drawn shape starts
    as the sample's, and is promoted only on converging evidence: the same
    artwork recurs on other pages of the revision (mechanical), and no
    annotator that looked at the page judged it content-coupled (semantic).
    The annotator can only veto a promotion, never force one -- a capsule
    drawn identically behind two sibling pages' cards still recurs, and
    pinning it is exactly the failure the veto exists for.
    """

    if shape.scope is not ShapeScope.SLIDE:
        return True
    if chrome_signature(shape) not in recurring_chrome:
        return False
    return shape.shape_id not in judged or shape.shape_id in fixed


def _chrome_rules(
    graph: SourceGraph,
    assets: Mapping[str, Asset],
    *,
    recurring_chrome: frozenset[tuple[object, ...]] = frozenset(),
    judged_decorations: frozenset[str] = frozenset(),
    fixed_decorations: frozenset[str] = frozenset(),
) -> list[str]:
    """Emit the template's own decoration as reproducible CSS.

    Reproducing decoration exactly is deterministic, so generation never has
    to approximate it from a reference screenshot. Every rule paints itself,
    including imagery, so an empty ``<div>`` is all the page has to supply.

    Two tiers with two contracts. The template's identity -- layout and
    master shapes, plus slide-drawn artwork promoted by
    :func:`_is_template_tier` -- becomes mandatory ``.tpl-*`` rules at
    guarded specificity. The rest of a slide's own decoration was placed
    around the *sample's* content -- a capsule per sample card, a pointer at
    a sample caption -- so it is emitted as ``.dec-*``: single-class,
    included by default, but movable and droppable once the page's layout
    departs from the sample. Forcing those onto a rearranged page pinned
    sample artwork across new content.
    """

    order = _paint_order(graph)
    rules: list[str] = []
    counters = {"tpl": 0, "dec": 0}
    for shape in graph.shapes:
        if not (shape.is_chrome and _paints(shape)):
            continue
        declarations = _geometry(shape.normalized_bbox, order[shape.shape_id])
        declarations.extend(_paint_css(shape, assets))
        if _stroked_outline_svg(shape) is None:
            # A stroked outline is drawn by its own SVG; clipping it again
            # would shave the stroke in half along every edge.
            declarations.extend(
                _outline_css(shape.outline, shape.normalized_bbox, graph.canvas)
            )
        if shape.rotation:
            declarations.append(f"transform:rotate({shape.rotation:g}deg)")
        body = ";".join(declarations)
        if not _is_template_tier(
            shape, recurring_chrome, judged_decorations, fixed_decorations
        ):
            counters["dec"] += 1
            rules.append(
                f".dec-{counters['dec']:02d}{{{body}}}"
                f" /* sample decoration / {shape.name} -- include by default;"
                " move or drop it when your layout departs from the sample */"
            )
        else:
            counters["tpl"] += 1
            provenance = (
                "recurring decoration"
                if shape.scope is ShapeScope.SLIDE
                else shape.scope.value
            )
            rules.append(
                f".slide .tpl-{counters['tpl']:02d}{{{body}}}"
                f" /* {provenance} / {shape.name} */"
            )
    return rules


def _region_asset(
    region: Region,
    assets: Mapping[str, Asset],
) -> Asset | None:
    """A template asset this region must reuse rather than replace.

    A logo slot is a content region by shape but not by nature: the deck can
    never supply a better version of the template's own mark. Painting it from
    the rule leaves nothing for generation to get wrong -- handed only an
    asset id, a model reaches for its own copy and lands a white logo on a
    white ground. Sample photography keeps ``reference_only`` and is skipped,
    because replacing that is exactly the point.
    """

    reusable = [
        assets[asset_id]
        for asset_id in region.asset_ids
        if asset_id in assets
        and assets[asset_id].reuse_policy
        in {ReusePolicy.ALWAYS, ReusePolicy.TEMPLATE_ONLY}
    ]
    return reusable[0] if len(reusable) == 1 else None


def _region_outline(
    region: Region,
    shapes: Mapping[str, ShapeNode],
    assets: Mapping[str, Asset],
) -> ShapeOutline | None:
    """The silhouette of a region that is not a plain rectangle.

    A cover photo slotted into a slanted frame is a content region, but it is
    still that shape. Handing over a plain rectangle makes the page bleed a
    photo across the title it was cut to sit beside. The cut may live in the
    shape's geometry or, just as often, in the sample image's alpha channel --
    both describe the same slot.
    """

    outlines = [
        shapes[shape_id].outline
        for shape_id in region.source_shape_ids
        if shape_id in shapes and shapes[shape_id].outline is not None
    ]
    if len(outlines) == 1:
        return outlines[0]
    masks = [
        assets[asset_id].alpha_outline
        for asset_id in region.asset_ids
        if asset_id in assets and assets[asset_id].alpha_outline is not None
    ]
    return masks[0] if len(masks) == 1 else None


def build_layout_css(
    semantic: Semantic,
    graph: SourceGraph,
    theme: Theme,
    assets: Mapping[str, Asset] | None = None,
    *,
    recurring_chrome: frozenset[tuple[object, ...]] = frozenset(),
) -> str:
    """Render one page's regions as absolute-positioned CSS classes.

    ``recurring_chrome`` carries the revision-wide signatures of slide-drawn
    decoration seen on more than one page; the compiler computes it because
    only the compiler sees every page at once.
    """

    shapes = {shape.shape_id: shape for shape in graph.shapes}
    height_inches = graph.canvas.height_inches or 7.5
    background = (
        f"background:{graph.background_fill}" if graph.background_fill else ""
    )
    lines = [
        f"/* Layout scaffold for {semantic.slide_id} "
        f"({semantic.page_semantics.stage.value}/"
        f"{semantic.page_semantics.layout_pattern.value}).",
        " * .tpl-* rules are the template's page chrome. Each one paints itself,",
        " * imagery included: emit every one as an empty <div> before the",
        " * content, add nothing to it, and never restyle or reposition it.",
        " * .dec-* rules are the sample slide's own decoration: include them as",
        " * empty <div>s by default, but move or drop them when your layout",
        " * departs from the sample's.",
        " * .r-* rules record how the sample page arranged its content --",
        " * geometry, type and colour in one low-specificity rule per region.",
        " * They are the default grid, not a constraint: redeclare a region's",
        " * class after this import whenever your content needs a different",
        " * size, position, colour or count of sibling regions.",
        " */",
        f".slide{{position:relative;width:100%;height:100%;overflow:hidden;{background}}}",
        # Decoration geometry is the template's, so a border must not grow the
        # box it was measured for.
        '.slide :is([class^="tpl-"],[class^="dec-"]){box-sizing:border-box}',
        # A region sets its font-size on the container, but the browser's own
        # h1-h6 rules are em-based and would silently multiply it -- an h1
        # renders at 2x and overflows the region it was sized for. :where()
        # carries no specificity, so this only overrides the UA default and
        # still yields to anything the page declares.
        ":where(.slide :is(h1,h2,h3,h4,h5,h6))"
        "{font-size:inherit;font-weight:inherit;margin:0}",
    ]
    if theme.css_variables:
        lines.append(":root{")
        lines.extend(
            f"  {name}:{value};" for name, value in sorted(theme.css_variables.items())
        )
        lines.append("}")
    lines.extend(
        _chrome_rules(
            graph,
            assets or {},
            recurring_chrome=recurring_chrome,
            judged_decorations=frozenset(semantic.judged_decoration_shape_ids),
            fixed_decorations=frozenset(semantic.fixed_decoration_shape_ids),
        )
    )
    order = _paint_order(graph)
    for region in semantic.regions:
        style = _dominant_style(region, shapes)
        note = f" /* {region.role.value} / {region.kind.value}"
        # A region stacks where its own shapes do, not where it happens to
        # fall in reading order.
        depth = min(
            (order[shape_id] for shape_id in region.source_shape_ids if shape_id in order),
            default=len(order) + 1,
        )
        declarations = _geometry(region.bbox, depth)
        asset = _region_asset(region, assets or {})
        if asset is not None:
            # The template's own artwork already carries its transparency, so
            # clipping it again would only trace its letterforms. This rule
            # paints template material, so like chrome it keeps the guarded
            # specificity: nothing a page declares may disturb it.
            declarations.extend(
                [
                    f"background-image:url({ASSET_URL_PREFIX}/{asset_url_name(asset)})",
                    # PowerPoint stretches a picture to its frame, and this
                    # artwork was cut for exactly this box.
                    "background-size:100% 100%",
                    "background-repeat:no-repeat",
                ]
            )
            note += " -- painted from the template asset; emit an empty div"
            lines.append(
                f".slide .r-{region.region_id}{{{';'.join(declarations)}}}{note} */"
            )
            continue
        declarations.extend(
            _outline_css(
                _region_outline(region, shapes, assets or {}),
                region.bbox,
                graph.canvas,
            )
        )
        # One single-class rule per region: the page can retune any of it by
        # redeclaring the class after the import, and the cascade -- not
        # specificity -- decides. Colour rides on the container and reaches
        # the text by inheritance, so it too stays a default, not a mandate.
        declarations.extend(_typography(style, height_inches))
        if style is not None and style.color:
            if _sample_ground_dropped(region, shapes, assets or {}):
                note += (
                    " -- sample text sat on its own card/photo paint, which is"
                    " not reproduced here; pick a readable colour from the"
                    " theme tokens or add a backing panel"
                )
            else:
                declarations.append(f"color:{style.color}")
        lines.append(
            f".r-{region.region_id}{{{';'.join(declarations)}}}{note} */"
        )
    safe = theme.safe_area
    lines.append(
        ".safe-area{"
        f"position:absolute;left:{_percent(safe.x)};top:{_percent(safe.y)};"
        f"width:{_percent(safe.width)};height:{_percent(safe.height)}"
        "}"
    )
    return "\n".join(lines) + "\n"


# Declarations worth showing a model that has to place content among the
# template's own modules. Long values (clip polygons, data URLs) are named
# rather than reproduced: what a page needs is where a module sits and what
# it looks like, not the vertices of its outline.
_DIGEST_KEYS = (
    "left",
    "top",
    "width",
    "height",
    "font-size",
    "line-height",
    "text-align",
    "color",
    "background",
    "background-color",
    "z-index",
)

_RULE_RE = re.compile(r"^(\.[^{\n]+)\{([^}]*)\}(?:\s*/\*(.*?)\*/)?", re.MULTILINE)
_PERCENT_RE = re.compile(r"(-?\d+\.\d+)%")
# A page dense enough to need more rules than this is described well enough by
# the first ones plus its reference image.
_DIGEST_MAX_RULES = 34


def _round_percent(value: str) -> str:
    """Three decimals of a percentage is noise a model cannot act on."""

    return _PERCENT_RE.sub(lambda m: f"{float(m.group(1)):.1f}%", value)


def summarize_layout_css(css: str, max_chars: int | None = None) -> str:
    """One line per scaffold rule: where a module sits and how it looks.

    Serving the scaffold as a path alone left generation guessing at the
    template's own component design -- it imported a file it had never read
    and invented cards and capsules of its own. Serving the file verbatim is
    not an option either: a dense page's clip polygons run to tens of
    kilobytes. This keeps the geometry, the palette and the role comment,
    which is what a page needs in order to compose with the template rather
    than beside it.
    """

    lines: list[str] = []
    for match in _RULE_RE.finditer(css):
        selector = match.group(1).strip().removeprefix(".slide ")
        if not selector.startswith(".") or selector.startswith((".slide", ".safe")):
            continue
        declarations = dict(
            (part.split(":", 1)[0].strip(), part.split(":", 1)[1].strip())
            for part in match.group(2).split(";")
            if ":" in part
        )
        parts = [
            f"{key}:{_round_percent(declarations[key])}"
            for key in _DIGEST_KEYS
            if key in declarations
        ]
        if "background-image" in declarations:
            parts.append("bg-image:asset")
        clip = declarations.get("clip-path")
        if clip and clip != "none":
            parts.append(f"clip:{clip.count(',') + 1}pt")
        # Only the role survives from the comment. The advice that follows it
        # is identical on every rule of a tier and repeating it per line cost
        # more than the geometry it was attached to.
        note = (match.group(3) or "").split("--")[0].strip()
        lines.append(
            f"{selector}{{{';'.join(parts)}}}" + (f" /* {note} */" if note else "")
        )
    return _fit_digest(lines, max_chars)


def _fit_digest(lines: list[str], max_chars: int | None) -> str:
    """Trim a digest to size by dropping the most droppable tier first.

    Clipping the text itself left a dense page's summary ending mid-rule,
    which is worse than a shorter but complete one. Sample decoration is
    both the most numerous tier and the one a page may discard, so it yields
    before the regions and the chrome that a page has to honour.
    """

    def rendered(items: list[str], hidden: int) -> str:
        if hidden:
            items = items + [f"/* + {hidden} more .dec-* rules omitted */"]
        return "\n".join(items)

    kept = list(lines)
    hidden = 0
    while max_chars is not None and len(rendered(kept, hidden)) > max_chars:
        index = next(
            (
                position
                for position in range(len(kept) - 1, -1, -1)
                if kept[position].startswith(".dec-")
            ),
            None,
        )
        if index is None:
            break
        kept.pop(index)
        hidden += 1
    text = rendered(kept, hidden)
    return text if max_chars is None else text[:max_chars]
