"""Layout scaffold and inherited-style extraction contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE
from pptx.dml.color import RGBColor

from deeppresenter.templates.extractor import _DRAWING_NS, PptxExtractor
from deeppresenter.templates.models import (
    Asset,
    AssetOccurrence,
    AssetRole,
    BoundingBox,
    Canvas,
    Density,
    LayoutFamily,
    LayoutPattern,
    MessagePattern,
    NormalizedBox,
    PageSemantics,
    PageStage,
    Region,
    RegionKind,
    RegionRole,
    Semantic,
    ShapeKind,
    ShapeNode,
    ShapeScope,
    ShapeText,
    SourceGraph,
    ReusePolicy,
    TextParagraph,
    TextRun,
    TextStyle,
    Theme,
)
from deeppresenter.templates.scaffold import (
    _chrome_rules,
    asset_url_name,
    build_layout_css,
    chrome_signature,
)
from deeppresenter.templates.styles import (
    StyleResolver,
    ThemePalette,
    theme_for_master,
)

BUNDLED = Path(__file__).resolve().parents[3] / "pptagent" / "templates"


def _canvas() -> Canvas:
    return Canvas(
        width_emu=12192000,
        height_emu=6858000,
        width_inches=13.3333,
        height_inches=7.5,
        aspect_ratio="16:9",
    )


def _graph() -> SourceGraph:
    shape = ShapeNode(
        shape_id="s001-sh0001",
        name="Title",
        scope=ShapeScope.SLIDE,
        kind=ShapeKind.PLACEHOLDER,
        z_index=0,
        bbox=BoundingBox(x=0, y=0, width=100, height=50),
        normalized_bbox=NormalizedBox(x=0.05, y=0.28, width=0.9, height=0.29),
        text=ShapeText(
            text="Title",
            paragraphs=[
                TextParagraph(
                    text="Title",
                    # Alignment lives on the paragraph, never on a run.
                    style=TextStyle(alignment="center"),
                    runs=[
                        TextRun(
                            text="Title",
                            style=TextStyle(
                                size_pt=36.0,
                                color="#FFFFFF",
                                font_family="Gill Sans MT",
                            ),
                        )
                    ],
                )
            ],
        ),
    )
    return SourceGraph(
        slide_id="s001", page_number=1, canvas=_canvas(), shapes=[shape]
    )


def _semantic() -> Semantic:
    region = Region(
        region_id="r001",
        source_shape_ids=["s001-sh0001"],
        kind=RegionKind.TEXT,
        role=RegionRole.TITLE,
        bbox=NormalizedBox(x=0.05, y=0.28, width=0.9, height=0.29),
    )
    return Semantic(
        slide_id="s001",
        page_number=1,
        page_semantics=PageSemantics(
            stage=PageStage.COVER,
            layout_pattern=LayoutPattern.HERO,
            message_pattern=MessagePattern.STATEMENT,
            density=Density.SPARSE,
        ),
        regions=[region],
        reading_order=["r001"],
        annotator_id="test",
    )


def test_scaffold_encodes_geometry_and_resolved_typography() -> None:
    theme = Theme(
        canvas=_canvas(),
        css_variables={"--template-accent1": "#660874"},
    )

    css = build_layout_css(_semantic(), _graph(), theme)

    assert ".r-r001{" in css
    assert "left:5%" in css
    assert "width:90%" in css
    assert "top:28%" in css
    # 36pt on a 7.5in canvas is 6.667% of slide height.
    assert "font-size:6.667vh" in css
    assert 'font-family:"Gill Sans MT"' in css
    assert "color:#FFFFFF" in css
    assert "text-align:center" in css
    assert "--template-accent1:#660874;" in css
    assert ".safe-area{" in css


def test_theme_and_styles_resolve_through_the_inheritance_chain() -> None:
    """A template that styles via master/theme must not extract as empty."""

    source = BUNDLED / "thu" / "source.pptx"
    result = PptxExtractor().extract(source)

    palette = {color.token: color.value for color in result.theme.colors}
    assert palette["accent1"] == "#660874"
    assert palette["accent3"] == "#E6C46D"
    assert [font.family for font in result.theme.fonts][:1] == ["Gill Sans MT"]
    assert result.theme.css_variables["--template-accent1"] == "#660874"

    graph = result.source_graphs[0]
    content = [s for s in graph.shapes if s.scope is ShapeScope.SLIDE]
    title = content[0]
    style = title.text.paragraphs[0].runs[0].style
    assert style.size_pt == 36.0
    assert style.color == "#FFFFFF"
    assert style.font_family == "Gill Sans MT"


def test_layout_chrome_and_background_are_captured() -> None:
    """Colour blocks, rules, and logo placement live on the layout/master."""

    result = PptxExtractor().extract(BUNDLED / "thu" / "source.pptx")
    graph = result.source_graphs[0]
    chrome = [s for s in graph.shapes if s.scope is not ShapeScope.SLIDE]

    assert graph.background_fill == "#FFFFFF"
    # The purple banner behind the cover title is a layout rectangle sitting
    # directly above the title region at y=0.28.
    banner = next(
        s
        for s in chrome
        if s.scope is ShapeScope.LAYOUT
        and s.fill == "#660874"
        and s.normalized_bbox.width > 0.9
    )
    assert banner.normalized_bbox.y == pytest.approx(0.255, abs=0.01)
    # The seal is placed by the layout, not by the slide.
    assert any(s.asset_ids and s.scope is ShapeScope.LAYOUT for s in chrome)

    css = build_layout_css(_semantic(), graph, result.theme)
    assert "background-color:#660874" in css
    assert "background:#FFFFFF" in css
    assert ".slide .tpl-01{" in css


def test_resolver_ignores_non_placeholder_shapes() -> None:
    presentation = Presentation(str(BUNDLED / "default" / "source.pptx"))
    resolver = StyleResolver(presentation.slides[0])

    for shape in presentation.slides[0].shapes:
        # Must not raise for ordinary (non-placeholder) shapes.
        assert resolver.inherited_style(shape) is not None


def test_layered_overview_keeps_every_family_in_the_index() -> None:
    """The index tier must scale with family count instead of truncating."""

    from deeppresenter.templates.overview import build_family_detail, build_index

    result = PptxExtractor().extract(BUNDLED / "thu" / "source.pptx")
    families = [
        LayoutFamily(
            family_id=f"family_{index:02d}",
            name=f"name-{index}",
            signature=f"sig-{index}",
            stage=PageStage.CONTENT,
            layout_pattern=LayoutPattern.TITLE_BODY,
            slide_ids=["s001"],
            representative_slide_id="s001",
            digest=f"digest {index}",
            selection_hints=["long hint " * 20],
            avoid_when=["long avoid " * 20],
        )
        for index in range(12)
    ]

    index = build_index(
        template_id="thu",
        revision_id="rev_test",
        schema_version="2.0",
        name="thu",
        canvas=result.canvas,
        theme=result.theme,
        families=families,
        slides=[],
        assets=[value.asset for value in result.assets],
    )

    # Every family survives: the index carries digests, not prose.
    assert len(index["families"]) == 12
    assert all(entry["digest"] for entry in index["families"])
    assert "selection_hints" not in index["families"][0]
    assert "detail_hint" in index

    detail = build_family_detail(families[0], {"s001": _semantic()})
    assert detail["selection_hints"] == families[0].selection_hints
    assert detail["avoid_when"] == families[0].avoid_when
    assert detail["slides"][0]["slide_id"] == "s001"


def test_shape_fill_is_not_read_from_descendant_text_or_outline() -> None:
    """A descendant search reports a run's colour as the shape's fill."""

    from lxml import etree

    from deeppresenter.templates.extractor import _DRAWING_NS

    presentation = Presentation(str(BUNDLED / "beamer" / "source.pptx"))
    palette = theme_for_master(presentation.slide_masters[0])
    checked = 0
    for slide in presentation.slides:
        for shape in slide.shapes:
            loose = shape.element.find(f".//{{{_DRAWING_NS}}}solidFill")
            scoped = PptxExtractor._shape_properties(shape)  # noqa: SLF001
            resolved = PptxExtractor._resolved_fill(shape, palette)  # noqa: SLF001
            if loose is None or scoped is None:
                continue
            own = scoped.find(f"{{{_DRAWING_NS}}}solidFill")
            if own is None:
                # The shape has no fill of its own, so neither may the IR.
                assert resolved is None, (
                    f"{shape.name!r} took its fill from "
                    f"{etree.QName(loose.getparent()).localname}"
                )
                checked += 1
    assert checked, "expected at least one fill-less shape carrying styled text"


def test_master_chrome_is_dropped_when_the_layout_hides_it() -> None:
    """thu's cover layout sets showMasterSp="0"."""

    result = PptxExtractor().extract(BUNDLED / "thu" / "source.pptx")
    cover = result.source_graphs[0]

    assert not [s for s in cover.shapes if s.scope is ShapeScope.MASTER]
    assert [s for s in cover.shapes if s.scope is ShapeScope.LAYOUT]


def test_scaffold_paints_chrome_behind_content() -> None:
    """Positive z-index chrome would otherwise hide the content regions."""

    result = PptxExtractor().extract(BUNDLED / "thu" / "source.pptx")
    css = build_layout_css(_semantic(), result.source_graphs[0], result.theme)

    import re

    def z_values(prefix: str) -> list[int]:
        return [
            int(match.group(1))
            for line in css.splitlines()
            if line.startswith(prefix)
            if (match := re.search(r"z-index:(\d+)", line))
        ]

    chrome_z = z_values(".slide .tpl-")
    content_z = z_values(".r-")
    assert chrome_z and content_z
    assert max(chrome_z) < min(content_z)


def test_chrome_descends_into_grouped_decoration() -> None:
    """default's entire visual identity sits inside one layout group."""

    result = PptxExtractor().extract(BUNDLED / "default" / "source.pptx")
    chrome = [
        shape
        for shape in result.source_graphs[0].shapes
        if shape.scope is not ShapeScope.SLIDE
    ]

    # The group itself carries no fill; only its children are painted. Several
    # of them are tinted, so the value carries an alpha suffix.
    fills = {shape.fill for shape in chrome if shape.fill}
    assert any(value.startswith("#90C226") for value in fills)
    assert len(chrome) >= 8


def test_background_resolves_a_theme_fill_reference() -> None:
    """Templates commonly set the page background with <p:bgRef>."""

    result = PptxExtractor().extract(BUNDLED / "default" / "source.pptx")

    # Every page inherits the master's referenced background.
    assert all(graph.background_fill for graph in result.source_graphs)


def test_scaffold_neutralises_the_browser_heading_scale() -> None:
    """An <h1> inherits the region size instead of doubling it.

    Without this the UA's em-based h1 rule silently renders at 2x the size
    the region was measured for, and the text overflows its own box.
    """

    css = build_layout_css(_semantic(), _graph(), Theme(canvas=_canvas()))

    assert ":where(.slide :is(h1,h2,h3,h4,h5,h6))" in css
    assert "font-size:inherit" in css


def test_region_rules_are_single_class_defaults() -> None:
    """A page must be able to retune a region by redeclaring its class.

    Regions describe the sample's arrangement, not a mandate: writing them at
    `.slide .r-x` specificity silently defeated the page's own `.r-x{width}`
    override, so every long title stayed wrapped inside a box measured around
    six characters of placeholder text.
    """

    css = build_layout_css(_semantic(), _graph(), Theme(canvas=_canvas()))

    assert ".slide .r-r001" not in css
    region = next(
        line for line in css.splitlines() if line.startswith(".r-r001{")
    )
    # Geometry, typography, and colour live in one overridable rule.
    assert "width:90%" in region
    assert "font-size" in region
    assert "color:#FFFFFF" in region

def _chrome_css(template: str, page: int = 0) -> str:
    """Chrome rules a real template compiles to, with browser asset paths."""

    result = PptxExtractor().extract(BUNDLED / template / "source.pptx")
    assets = {value.asset.asset_id: value.asset for value in result.assets}
    return "\n".join(_chrome_rules(result.source_graphs[page], assets))


def test_non_rectangular_chrome_keeps_its_silhouette() -> None:
    """default's identity is triangles and diagonal bands, not rectangles.

    A box with the right colour and the wrong shape contradicts the reference
    image, and generation resolves that contradiction by improvising.
    """

    css = _chrome_css("default")

    # An isosceles triangle preset and a custom diagonal band.
    assert "clip-path:polygon(50% 0%, 100% 100%, 0% 100%)" in css
    assert "clip-path:polygon(68.02% 0%, 100% 0%, 100% 100%, 0% 100%)" in css
    assert css.count("clip-path:") >= 8


def test_slide_decoration_is_soft_while_layout_chrome_stays_hard() -> None:
    """Sample-page artwork follows the content it was drawn around.

    Layout chrome (corner bands, logos) is the template's identity and must
    stay pinned. Slide-level decoration -- a capsule around a sample card, an
    arrow at a sample caption -- is not: forcing it onto a rearranged page
    painted sample artwork across new content.
    """

    result = PptxExtractor().extract(BUNDLED / "default" / "source.pptx")
    css = "\n".join(_chrome_rules(result.source_graphs[0], {}))

    # default's identity is master-level triangles and diagonal bands.
    assert ".slide .tpl-" in css
    assert ".dec-" not in css

    slide_shape = next(
        shape
        for shape in result.source_graphs[0].shapes
        if shape.scope is ShapeScope.LAYOUT and shape.fill
    )
    fake = slide_shape.model_copy(
        update={"shape_id": "sample-deco", "scope": ShapeScope.SLIDE, "name": "Capsule"}
    )
    soft = "\n".join(
        _chrome_rules(
            result.source_graphs[0].model_copy(
                update={"shapes": [*result.source_graphs[0].shapes, fake]}
            ),
            {},
        )
    )
    assert ".dec-01{" in soft
    assert "sample decoration" in soft
    assert "move or drop" in soft
    assert ".slide .tpl-01{" in soft


def test_recurring_slide_decoration_is_promoted_unless_vetoed() -> None:
    """Scope records where a shape was drawn, not what it is.

    Brand wedges hand-copied onto the cover and the closing page are
    slide-scope yet template identity: recurrence across pages promotes them
    to hard chrome. An annotator that looked at the page can veto the
    promotion -- a capsule drawn identically behind two sibling pages' cards
    still recurs -- but can never force one.
    """

    result = PptxExtractor().extract(BUNDLED / "default" / "source.pptx")
    graph = result.source_graphs[0]
    template_shape = next(
        shape
        for shape in graph.shapes
        if shape.scope is ShapeScope.LAYOUT and shape.fill
    )
    fake = template_shape.model_copy(
        update={"shape_id": "wedge", "scope": ShapeScope.SLIDE, "name": "Wedge"}
    )
    extended = graph.model_copy(update={"shapes": [*graph.shapes, fake]})
    recurring = frozenset({chrome_signature(fake)})

    def rules(**kwargs: object) -> str:
        return "\n".join(_chrome_rules(extended, {}, **kwargs))

    # Recurs, nobody looked: mechanical evidence alone promotes.
    promoted = rules(recurring_chrome=recurring)
    assert "recurring decoration / Wedge" in promoted
    assert ".dec-" not in promoted

    # Recurs, the annotator looked and did not mark it fixed: veto.
    vetoed = rules(
        recurring_chrome=recurring,
        judged_decorations=frozenset({"wedge"}),
    )
    assert ".dec-01{" in vetoed
    assert "recurring decoration" not in vetoed

    # Recurs, the annotator confirmed page furniture: promoted.
    confirmed = rules(
        recurring_chrome=recurring,
        judged_decorations=frozenset({"wedge"}),
        fixed_decorations=frozenset({"wedge"}),
    )
    assert "recurring decoration / Wedge" in confirmed

    # One-off artwork stays soft even when the annotator calls it fixed:
    # the verdict restricts, it never expands.
    one_off = rules(
        judged_decorations=frozenset({"wedge"}),
        fixed_decorations=frozenset({"wedge"}),
    )
    assert ".dec-01{" in one_off


def test_layout_css_threads_verdicts_from_the_semantic() -> None:
    """The compiler's recurrence set and the annotator's veto meet here."""

    graph = _graph()
    deco = ShapeNode(
        shape_id="s001-deco",
        name="Capsule",
        scope=ShapeScope.SLIDE,
        kind=ShapeKind.AUTO_SHAPE,
        z_index=1,
        bbox=BoundingBox(x=0, y=0, width=40, height=20),
        normalized_bbox=NormalizedBox(x=0.1, y=0.6, width=0.3, height=0.2),
        fill="#005EA4",
    )
    extended = graph.model_copy(update={"shapes": [*graph.shapes, deco]})
    recurring = frozenset({chrome_signature(deco)})
    semantic = _semantic().model_copy(
        update={
            "judged_decoration_shape_ids": ["s001-deco"],
            "fixed_decoration_shape_ids": [],
        }
    )

    vetoed = build_layout_css(
        semantic,
        extended,
        Theme(canvas=_canvas()),
        recurring_chrome=recurring,
    )
    assert ".dec-01{" in vetoed

    silent = build_layout_css(
        _semantic(),
        extended,
        Theme(canvas=_canvas()),
        recurring_chrome=recurring,
    )
    assert "recurring decoration / Capsule" in silent


def test_ellipse_chrome_is_round() -> None:
    css = _chrome_css("cip", page=1)

    assert "border-radius:50%" in css


def test_divider_rules_survive_and_draw_one_edge() -> None:
    """A rule paints with a stroke and no fill, so a fill cannot gate it."""

    css = _chrome_css("cip", page=1)

    assert "border-top:2px solid #385AA4" in css
    # Boxing a zero-height rule on four sides would double it.
    assert "border:2px solid #385AA4" not in css


def test_diagonal_connectors_are_not_reproduced_as_boxes() -> None:
    """A div cannot draw a corner-to-corner stroke; a bordered box is worse.

    default's hairlines run corner to corner across a tall box, so a border
    would outline a large rectangle the template never had.
    """

    result = PptxExtractor().extract(BUNDLED / "default" / "source.pptx")
    graph = result.source_graphs[0]
    diagonals = [
        shape
        for shape in graph.shapes
        if shape.kind is ShapeKind.LINE
        and min(shape.normalized_bbox.width, shape.normalized_bbox.height) > 0.002
    ]
    css = "\n".join(_chrome_rules(graph, {}))

    # The shapes stay in the IR as facts; only the scaffold declines to draw.
    assert diagonals
    assert "#262626" not in css
    assert css.count(".slide .tpl-") == 8


def test_chrome_imagery_resolves_without_the_model_guessing() -> None:
    """The rule carries the URL, so an empty div is all a page must emit."""

    result = PptxExtractor().extract(BUNDLED / "cip" / "source.pptx")
    assets = {value.asset.asset_id: value.asset for value in result.assets}
    css = "\n".join(_chrome_rules(result.source_graphs[1], assets))

    logo = next(
        asset for asset in assets.values() if asset.role is AssetRole.LOGO
    )
    assert f"background-image:url(../../assets/{asset_url_name(logo)})" in css
    assert "background-size:100% 100%" in css


def test_layout_imagery_is_template_chrome_not_replaceable_content() -> None:
    """A cover logo appears once and is often named "Picture 2".

    Size, name, and recurrence heuristics all miss it, and a reference_only
    asset is never staged into a task pack -- so the scaffold pointed at a
    file that did not exist and generation substituted its own image.
    """

    result = PptxExtractor().extract(BUNDLED / "thu" / "source.pptx")
    seal = next(
        value.asset
        for value in result.assets
        if any(
            occurrence.scope is ShapeScope.LAYOUT
            for occurrence in value.asset.occurrences
        )
        and not any(
            occurrence.scope is ShapeScope.MASTER
            for occurrence in value.asset.occurrences
        )
    )

    assert seal.role in {AssetRole.LOGO, AssetRole.DECORATION, AssetRole.BACKGROUND}
    assert seal.reuse_policy is not ReusePolicy.REFERENCE_ONLY


def test_gradient_page_background_is_not_dropped() -> None:
    """hit paints its page with <a:gradFill>, which has no single colour."""

    result = PptxExtractor().extract(BUNDLED / "hit" / "source.pptx")

    background = result.source_graphs[0].background_fill
    assert background is not None
    assert background.startswith("linear-gradient(")
    assert "#F2F2F2" in background
    # The old descendant search would have returned the first stop's colour.
    assert background != "#E1DFE2"


def test_dense_freeform_outlines_are_simplified() -> None:
    """A flattened curve must not outweigh the scaffold the model reads."""

    dense = [(0.0, 0.0), (1.0, 0.0)]
    # A straight run sampled at 200 points, as PowerPoint flattens a curve.
    dense.extend((1.0, index / 200) for index in range(1, 200))
    dense.append((0.0, 1.0))

    simplified = PptxExtractor._drop_collinear(dense)  # noqa: SLF001

    # The straight run collapses; only its corners and last sample survive.
    assert len(simplified) == 4
    assert simplified[:2] == [(0.0, 0.0), (1.0, 0.0)]
    assert simplified[-1] == (0.0, 1.0)


def test_a_gradient_fill_is_not_treated_as_artwork() -> None:
    """Fitting properties belong to images; a gradient already fills its box."""

    result = PptxExtractor().extract(BUNDLED / "hit" / "source.pptx")
    graph = result.source_graphs[0]
    gradient = ShapeNode(
        shape_id="deco",
        name="Band",
        scope=ShapeScope.LAYOUT,
        kind=ShapeKind.AUTO_SHAPE,
        z_index=0,
        bbox=BoundingBox(x=0, y=0, width=10, height=10),
        normalized_bbox=NormalizedBox(x=0.0, y=0.0, width=1.0, height=0.2),
        fill="linear-gradient(90deg, #000000 0%, #FFFFFF 100%)",
    )
    css = "\n".join(
        _chrome_rules(graph.model_copy(update={"shapes": [gradient]}), {})
    )

    assert "background-image:linear-gradient(90deg" in css
    assert "background-size" not in css


def test_grouped_shapes_are_placed_in_slide_coordinates() -> None:
    """A group's children are offset in the group's own coordinate space.

    Reading them raw makes every group with the same internal layout land on
    the same spot, so a row of three cards stacks into one. The bundled
    templates all use an identity child frame, which is why this went unseen.
    """

    from lxml import etree

    from deeppresenter.templates.extractor import (
        _DRAWING_NS,
        _PRESENTATION_NS,
        _GroupTransform,
    )

    group = etree.fromstring(
        f'''<p:grpSp xmlns:p="{_PRESENTATION_NS}" xmlns:a="{_DRAWING_NS}"><p:grpSpPr>
             <a:xfrm>
               <a:off x="4259996" y="1416666"/><a:ext cx="3384851" cy="5060333"/>
               <a:chOff x="4539205" y="1967696"/><a:chExt cx="3384851" cy="5060333"/>
             </a:xfrm></p:grpSpPr></p:grpSp>'''
    )

    class _Group:
        element = group

    placed = BoundingBox(x=4259996, y=1416666, width=3384851, height=5060333)
    transform = _GroupTransform().descend(_Group(), placed)
    # A child sitting at the child frame's origin belongs at the group's own
    # origin, not at the raw offset python-pptx reports.
    child = transform.apply(
        BoundingBox(x=4539205, y=1967696, width=3113589, height=1000)
    )

    assert (child.x, child.y) == (4259996, 1416666)
    assert child.width == 3113589

    # An identity child frame must stay a no-op.
    identity = etree.fromstring(
        f'''<p:grpSp xmlns:p="{_PRESENTATION_NS}" xmlns:a="{_DRAWING_NS}"><p:grpSpPr>
             <a:xfrm>
               <a:off x="10" y="20"/><a:ext cx="100" cy="200"/>
               <a:chOff x="10" y="20"/><a:chExt cx="100" cy="200"/>
             </a:xfrm></p:grpSpPr></p:grpSp>'''
    )
    _Group.element = identity
    unchanged = _GroupTransform().descend(
        _Group(), BoundingBox(x=10, y=20, width=100, height=200)
    )

    assert unchanged.apply(BoundingBox(x=55, y=60, width=7, height=8)) == (
        BoundingBox(x=55, y=60, width=7, height=8)
    )


def test_luminance_modifiers_survive_fill_resolution() -> None:
    """One brand colour plus lumMod/lumOff is how a template builds its tints.

    python-pptx reports only the base colour of an explicit srgbClr, so three
    stacked bands differing solely by luminance all extracted as one flat
    colour and the marker read as a single solid wedge.
    """

    from lxml import etree

    from deeppresenter.templates.extractor import _PRESENTATION_NS as PNS

    element = etree.fromstring(
        f'''<p:sp xmlns:p="{PNS}" xmlns:a="{_DRAWING_NS}"><p:spPr>
             <a:solidFill><a:srgbClr val="039ACF">
               <a:lumMod val="40000"/><a:lumOff val="60000"/>
             </a:srgbClr></a:solidFill></p:spPr></p:sp>'''
    )
    # python-pptx resolves this fill to its base colour, dropping the
    # modifiers; that shortcut is what used to win.
    fore_color = type("_Fore", (), {"rgb": RGBColor.from_string("039ACF")})()
    fill = type("_Fill", (), {"fore_color": fore_color})()
    shape = type("_Shape", (), {"element": element, "fill": fill})()

    assert PptxExtractor._shape_color(fill) == "#039ACF"  # noqa: SLF001

    resolved = PptxExtractor._resolved_fill(shape, ThemePalette())  # noqa: SLF001

    assert resolved is not None
    # The base colour unmodified would be the bug.
    assert resolved != "#039ACF"
    assert resolved == "#9AD7EC"


def test_mirrored_groups_flip_their_children() -> None:
    """A group carrying flipH/flipV draws everything inside it mirrored.

    Ignoring the flip leaves a corner marker pointing the wrong way and
    anchored to the opposite side of its own group box.
    """

    from lxml import etree

    from deeppresenter.templates.extractor import (
        _DRAWING_NS as NS,
        _PRESENTATION_NS as PNS,
        _GroupTransform,
    )

    def group(flip: str) -> object:
        element = etree.fromstring(
            f'''<p:grpSp xmlns:p="{PNS}" xmlns:a="{NS}"><p:grpSpPr>
                  <a:xfrm {flip}>
                    <a:off x="0" y="0"/><a:ext cx="1000" cy="1000"/>
                    <a:chOff x="0" y="0"/><a:chExt cx="1000" cy="1000"/>
                  </a:xfrm></p:grpSpPr></p:grpSp>'''
        )
        return type("_Group", (), {"element": element})()

    placed = BoundingBox(x=0, y=0, width=1000, height=1000)
    child = BoundingBox(x=0, y=0, width=200, height=100)

    upright = _GroupTransform().descend(group(""), placed).apply(child)
    flipped = (
        _GroupTransform()
        .descend(group('flipH="1" flipV="1"'), placed)
        .apply(child)
    )

    assert (upright.x, upright.y) == (0, 0)
    # A child hugging the group's top-left belongs at its bottom-right.
    assert (flipped.x, flipped.y) == (800, 900)
    assert (flipped.width, flipped.height) == (200, 100)


def test_a_region_never_spans_a_gap_the_page_cannot_see() -> None:
    """A caption on a graphic and the paragraph under it are two slots.

    Their union box also covers the graphic between them, so a page fills it
    from the top and leaves the bottom third empty -- which is exactly how the
    body copy ended up sitting on the artwork.
    """

    from deeppresenter.templates.annotation import split_disjoint_bands

    def _text(shape_id: str, top: float, height: float) -> ShapeNode:
        return ShapeNode(
            shape_id=shape_id,
            name=shape_id,
            scope=ShapeScope.SLIDE,
            kind=ShapeKind.TEXT,
            z_index=0,
            bbox=BoundingBox(x=0, y=0, width=1, height=1),
            normalized_bbox=NormalizedBox(x=0.11, y=top, width=0.19, height=height),
            text=ShapeText(text="sample"),
        )

    label = _text("label", 0.523, 0.067)
    body = _text("body", 0.758, 0.115)
    # The arrow the template draws between them, wholly inside the gap.
    arrow = _text("arrow", 0.676, 0.069)
    # A card outline crossing the gap is not what separates them.
    card = _text("card", 0.285, 0.660)

    split = split_disjoint_bands([label, body], [label, body, arrow, card])
    unblocked = split_disjoint_bands([label, body], [label, body, card])
    # Ordinary line spacing between two stacked paragraphs is not a gap.
    kept = split_disjoint_bands(
        [_text("a", 0.20, 0.05), _text("b", 0.26, 0.05)], [arrow, card]
    )

    assert [[shape.shape_id for shape in band] for band in split] == [["label"], ["body"]]
    assert len(unblocked) == 1
    assert len(kept) == 1


def test_a_logo_region_paints_the_template_asset_itself() -> None:
    """Handed only an asset id, a model reaches for its own copy.

    That is how a white logo variant landed on the template's white ground.
    Sample photography keeps reference_only and stays the page's job.
    """

    logo = Asset(
        asset_id="asset_logo",
        sha256="0" * 64,
        media_type="image/png",
        extension="png",
        path="assets/asset_logo.png",
        byte_size=10,
        role=AssetRole.LOGO,
        reuse_policy=ReusePolicy.ALWAYS,
    )
    photo = logo.model_copy(
        update={
            "asset_id": "asset_photo",
            "path": "assets/asset_photo.png",
            "role": AssetRole.CONTENT_IMAGE,
            "reuse_policy": ReusePolicy.REFERENCE_ONLY,
        }
    )
    semantic = _semantic()
    regions = [
        semantic.regions[0].model_copy(
            update={"region_id": "r001", "asset_ids": ["asset_logo"]}
        ),
        semantic.regions[0].model_copy(
            update={"region_id": "r002", "asset_ids": ["asset_photo"]}
        ),
    ]

    css = build_layout_css(
        semantic.model_copy(update={"regions": regions}),
        _graph(),
        Theme(canvas=_canvas()),
        {"asset_logo": logo, "asset_photo": photo},
    )

    assert "url(../../assets/asset_logo.png)" in css
    assert "asset_photo" not in css
    # Painting template material, the rule keeps the guarded specificity a
    # page cannot disturb; free regions stay single-class.
    assert ".slide .r-r001{" in css


def test_a_masked_photo_slot_clips_whatever_replaces_it() -> None:
    """Templates cut photo slots to shape inside the image, not the shape.

    The frame stays a plain rect, so a replacement photo filled the whole
    rectangle and ran under the title the slot was cut to sit beside.
    """

    import io

    from PIL import Image

    # A slanted slot: opaque on the left, cut back further with every row.
    mask = Image.new("RGBA", (200, 100), (0, 0, 0, 0))
    pixels = mask.load()
    for y in range(100):
        for x in range(200 - y):
            pixels[x, y] = (10, 20, 30, 255)
    blob = io.BytesIO()
    mask.save(blob, "PNG")

    outline = PptxExtractor._alpha_outline(blob.getvalue())  # noqa: SLF001

    assert outline is not None and outline.polygon
    xs = [x for x, _ in outline.polygon]
    assert min(xs) == 0.0
    # The right edge slants inwards rather than reaching the frame.
    assert max(xs) < 1.01
    assert any(x < 0.6 for x in xs)


def test_an_opaque_image_is_not_clipped() -> None:
    """Clipping a full-bleed photo would only shave its own edges."""

    import io

    from PIL import Image

    blob = io.BytesIO()
    Image.new("RGBA", (64, 64), (1, 2, 3, 255)).save(blob, "PNG")

    assert PptxExtractor._alpha_outline(blob.getvalue()) is None  # noqa: SLF001


def test_preset_adjust_handles_are_read_not_assumed() -> None:
    """A preset's adjust handle decides its outline; the default is a guess.

    The cover band carries adj=76100, so assuming the 25000 default pointed
    the slant the other way and dragged the shape across the logo.
    """

    from lxml import etree

    from deeppresenter.templates.extractor import (
        _DRAWING_NS as NS,
        _PRESENTATION_NS as PNS,
        _adjusted_polygon,
    )

    def preset(adjust: str) -> object:
        return etree.fromstring(
            f'<a:prstGeom xmlns:a="{NS}" xmlns:p="{PNS}" prst="parallelogram">'
            f"<a:avLst>{adjust}</a:avLst></a:prstGeom>"
        )

    # A box wider than it is tall, so the shorter side is its height.
    box = BoundingBox(x=0, y=0, width=1000, height=1000)
    actual = _adjusted_polygon(
        "parallelogram", preset('<a:gd name="adj" fmla="val 76100"/>'), box
    )
    default = _adjusted_polygon("parallelogram", preset(""), box)

    assert actual is not None and default is not None
    assert actual[0][0] == pytest.approx(0.761, abs=0.001)
    assert default[0][0] == pytest.approx(0.25, abs=0.001)


def test_masked_artwork_is_template_material_not_a_sample() -> None:
    """An image cut to a bespoke silhouette was made for that slot.

    Its mask encodes the layout, so no other picture can take its place --
    unlike a sample photograph, which is always a plain rectangle.
    """

    import io

    from PIL import Image

    from deeppresenter.templates.extractor import _AssetRecord

    def _record(mask: bool) -> _AssetRecord:
        image = Image.new("RGBA", (120, 60), (9, 9, 9, 255))
        if mask:
            pixels = image.load()
            for y in range(60):
                for x in range(120 - y, 120):
                    pixels[x, y] = (0, 0, 0, 0)
        blob = io.BytesIO()
        image.save(blob, "PNG")
        data = blob.getvalue()
        return _AssetRecord(
            digest="a" * 64,
            blob=data,
            extension="png",
            media_type="image/png",
            pixel_width=120,
            pixel_height=60,
            occurrences=[
                AssetOccurrence(
                    slide_id="s001",
                    page_number=1,
                    scope=ShapeScope.SLIDE,
                    source_name="Picture 9",
                    bbox=NormalizedBox(x=0.1, y=0.1, width=0.4, height=0.3),
                )
            ],
            alpha_outline=PptxExtractor._alpha_outline(data),  # noqa: SLF001
        )

    extractor = PptxExtractor()

    assert extractor._asset_role(_record(True), 10) is AssetRole.DECORATION  # noqa: SLF001
    assert extractor._asset_role(_record(False), 10) is AssetRole.CONTENT_IMAGE  # noqa: SLF001


def test_sample_colour_is_dropped_when_its_ground_is_not_reproduced() -> None:
    """A colour chosen for a ground the scaffold omits is not a fact.

    The sample's white card headers sat on colour pills inside the content
    group; the scaffold reproduces neither the pill nor the photo behind it.
    Pinning the white anyway produced white text on a white page -- and the
    model, forbidden to restyle it, shipped the blank page.
    """

    ground = ShapeNode(
        shape_id="s001-sh0001",
        name="Card",
        scope=ShapeScope.SLIDE,
        kind=ShapeKind.AUTO_SHAPE,
        z_index=0,
        bbox=BoundingBox(x=0, y=0, width=100, height=50),
        normalized_bbox=NormalizedBox(x=0.05, y=0.28, width=0.9, height=0.29),
        fill="#005EA4",
        text=ShapeText(
            text="Header",
            paragraphs=[
                TextParagraph(
                    text="Header",
                    style=TextStyle(alignment="center"),
                    runs=[
                        TextRun(
                            text="Header",
                            style=TextStyle(size_pt=20.0, color="#FFFFFF"),
                        )
                    ],
                )
            ],
        ),
    )
    graph = SourceGraph(
        slide_id="s001", page_number=1, canvas=_canvas(), shapes=[ground]
    )

    css = build_layout_css(_semantic(), graph, Theme(canvas=_canvas()))

    region = next(line for line in css.splitlines() if line.startswith(".r-r001{"))
    assert "color:#FFFFFF" not in region
    assert "backing panel" in region
    # The rest of the sample's typography is still a usable default.
    assert "font-size" in region

    # The same text on paint the scaffold does reproduce keeps its colour.
    intact = build_layout_css(_semantic(), _graph(), Theme(canvas=_canvas()))
    assert "color:#FFFFFF" in next(
        line for line in intact.splitlines() if line.startswith(".r-r001{")
    )


def test_a_group_never_paints_itself() -> None:
    """A group's fill exists for its children to ask for, not for the box.

    Painting the container turned a pair of chevrons into the solid rectangle
    that encloses them, and the chevrons themselves vanished for want of a
    fill of their own.
    """

    from deeppresenter.templates.scaffold import _paints

    group = ShapeNode(
        shape_id="grp",
        name="组合 33",
        scope=ShapeScope.SLIDE,
        kind=ShapeKind.GROUP,
        z_index=0,
        bbox=BoundingBox(x=0, y=0, width=10, height=10),
        normalized_bbox=NormalizedBox(x=0.2, y=0.4, width=0.05, height=0.05),
        fill="#005EA4",
    )
    child = group.model_copy(
        update={"shape_id": "chev", "kind": ShapeKind.FREEFORM}
    )

    assert not _paints(group)
    assert _paints(child)


def test_group_fill_is_inherited_by_grpfill_children() -> None:
    """`<a:grpFill/>` means "paint me with my group's fill"."""

    from lxml import etree

    from deeppresenter.templates.extractor import _PRESENTATION_NS as PNS

    element = etree.fromstring(
        f'<p:sp xmlns:p="{PNS}" xmlns:a="{_DRAWING_NS}"><p:spPr>'
        f"<a:grpFill/></p:spPr></p:sp>"
    )
    shape = type("_Shape", (), {"element": element, "fill": None})()

    inherited = PptxExtractor._resolved_fill(  # noqa: SLF001
        shape, ThemePalette(), "#005EA4"
    )
    orphan = PptxExtractor._resolved_fill(shape, ThemePalette())  # noqa: SLF001

    assert inherited == "#005EA4"
    assert orphan is None


def test_holes_are_cut_without_slitting_the_shape() -> None:
    """One `<a:path>` may hold several subpaths; the extra ones are holes.

    CSS has a fill rule but no subpaths, so each hole is bridged from the same
    outer point and left along the same line. Bridges a hair apart enclose a
    sliver, which renders as a white slit down the middle of the capsule.
    """

    from deeppresenter.templates.scaffold import _with_holes

    outer = [(0.0, 0.0), (1.0, 0.0), (1.0, 1.0), (0.0, 1.0)]
    hole = [(0.3, 0.3), (0.7, 0.3), (0.7, 0.7), (0.3, 0.7)]

    threaded = _with_holes(outer, [hole])

    # Out along p0 -> q0 and back along q0 -> p0: exactly coincident.
    assert threaded[:4] == outer
    assert threaded[4] == outer[0]
    assert threaded[5:9] == hole
    assert threaded[9] == hole[0]
    assert _with_holes(outer, []) == outer


def test_fill_transparency_is_kept() -> None:
    """A tinted panel is what lets the artwork beneath it show through.

    Dropping `<a:alpha>` paints it solid, and the background image behind it
    disappears entirely.
    """

    from lxml import etree

    from deeppresenter.templates.extractor import _PRESENTATION_NS as PNS

    element = etree.fromstring(
        f'<p:sp xmlns:p="{PNS}" xmlns:a="{_DRAWING_NS}"><p:spPr><a:solidFill>'
        f'<a:srgbClr val="00467B"><a:alpha val="88000"/></a:srgbClr>'
        f"</a:solidFill></p:spPr></p:sp>"
    )
    shape = type("_Shape", (), {"element": element, "fill": None})()

    fill = PptxExtractor._resolved_fill(shape, ThemePalette())  # noqa: SLF001

    # 88% of 255 is 224 = 0xE0.
    assert fill == "#00467BE0"


def test_run_colour_resolves_scheme_and_preset_values() -> None:
    """python-pptx exposes only literal RGB and raises for everything else.

    White body text written as `schemeClr bg1` therefore read as no colour at
    all, and the inheritance chain filled in black -- black on a navy panel.
    """

    from lxml import etree

    from deeppresenter.templates.extractor import _PRESENTATION_NS as PNS

    def _font(colour: str) -> object:
        rpr = etree.fromstring(
            f'<a:rPr xmlns:a="{_DRAWING_NS}" xmlns:p="{PNS}">'
            f"<a:solidFill>{colour}</a:solidFill></a:rPr>"
        )
        return type("_Font", (), {"_rPr": rpr, "color": None})()

    palette = ThemePalette(colors={"bg1": "#FFFFFF"})

    assert (
        PptxExtractor._run_color(_font('<a:schemeClr val="bg1"/>'), palette)  # noqa: SLF001
        == "#FFFFFF"
    )
    assert (
        PptxExtractor._run_color(_font('<a:prstClr val="black"/>'), palette)  # noqa: SLF001
        == "#000000"
    )


def test_alignment_comes_from_the_paragraph_and_is_always_stated() -> None:
    """PowerPoint keeps alignment on the paragraph; a run never has its own.

    Reading it off a run returns whatever the master's list style says, so a
    deck of centred cards came out uniformly left-aligned. It is also written
    out even when the template leaves it implicit, so a page cannot inherit a
    different alignment from its own stylesheet.
    """

    def _region(alignment: str | None) -> Semantic:
        shape = ShapeNode(
            shape_id="s001-sh0001",
            name="Body",
            scope=ShapeScope.SLIDE,
            kind=ShapeKind.TEXT,
            z_index=0,
            bbox=BoundingBox(x=0, y=0, width=100, height=50),
            normalized_bbox=NormalizedBox(x=0.1, y=0.1, width=0.5, height=0.2),
            text=ShapeText(
                text="Body",
                paragraphs=[
                    TextParagraph(
                        text="Body",
                        style=TextStyle(alignment=alignment),
                        runs=[
                            TextRun(
                                text="Body",
                                # What a run inherits from the master.
                                style=TextStyle(size_pt=18.0, alignment="l"),
                            )
                        ],
                    )
                ],
            ),
        )
        graph = SourceGraph(
            slide_id="s001", page_number=1, canvas=_canvas(), shapes=[shape]
        )
        return graph

    def _css(alignment: str | None) -> str:
        graph = _region(alignment)
        return build_layout_css(_semantic(), graph, Theme(canvas=_canvas()))

    assert "text-align:center" in _css("center")
    assert "text-align:right" in _css("right")
    assert "text-align:justify" in _css("justify")
    # Unstated in the template still means stated in the scaffold.
    assert "text-align:left" in _css(None)


def test_layout_summary_shows_every_module_without_the_polygons() -> None:
    """Serving only a path left generation importing a file it never read.

    The summary is what lets a page see where the template's own modules sit
    and what they look like. Clip polygons are named rather than reproduced:
    a dense page's outlines run to tens of kilobytes and no layout decision
    depends on their vertices.
    """

    from deeppresenter.templates.scaffold import summarize_layout_css

    css = build_layout_css(_semantic(), _graph(), Theme(canvas=_canvas()))
    summary = summarize_layout_css(css)

    region = next(
        line for line in summary.splitlines() if line.startswith(".r-r001")
    )
    assert "left:5%" in region and "color:#FFFFFF" in region
    assert "title / text" in region
    # The page-level helpers are noise for a model placing content.
    assert ".safe-area" not in summary
    assert "polygon(" not in summary


def test_a_dense_summary_keeps_regions_and_chrome_over_sample_decoration() -> None:
    """Sample decoration is the droppable tier, so it yields first.

    Clipping the text instead ended a dense page's summary mid-rule, which
    is worse than a shorter but complete one.
    """

    from deeppresenter.templates.scaffold import summarize_layout_css

    lines = [
        ".slide .tpl-01{left:0%;top:0%;width:10%;height:10%} /* layout / band */",
        *(
            f".dec-{index:02d}{{left:{index}%;top:0%;width:5%;height:5%}}"
            " /* sample decoration / capsule */"
            for index in range(1, 40)
        ),
        ".r-r001{left:5%;top:5%;width:50%;height:10%} /* title / text */",
    ]
    summary = summarize_layout_css("\n".join(lines), max_chars=600)

    assert len(summary) <= 600
    assert ".tpl-01{" in summary
    assert ".r-r001{" in summary
    assert "more .dec-* rules omitted" in summary
    # Every surviving line is whole.
    assert all(
        line.endswith("*/") or line.endswith("}")
        for line in summary.splitlines()
    )
