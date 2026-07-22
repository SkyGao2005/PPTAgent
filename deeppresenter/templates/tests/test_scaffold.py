"""Layout scaffold and inherited-style extraction contracts."""

from __future__ import annotations

from pathlib import Path

import pytest
from pptx import Presentation

from deeppresenter.templates.extractor import PptxExtractor
from deeppresenter.templates.models import (
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
    TextParagraph,
    TextRun,
    TextStyle,
    Theme,
)
from deeppresenter.templates.scaffold import build_layout_css
from deeppresenter.templates.styles import StyleResolver, theme_for_master

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
                    runs=[
                        TextRun(
                            text="Title",
                            style=TextStyle(
                                size_pt=36.0,
                                color="#FFFFFF",
                                font_family="Gill Sans MT",
                                alignment="CENTER",
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
        and s.fill_color == "#660874"
        and s.normalized_bbox.width > 0.9
    )
    assert banner.normalized_bbox.y == pytest.approx(0.255, abs=0.01)
    # The seal is placed by the layout, not by the slide.
    assert any(s.asset_ids and s.scope is ShapeScope.LAYOUT for s in chrome)

    css = build_layout_css(_semantic(), graph, result.theme)
    assert "background:#660874" in css
    assert "background:#FFFFFF" in css
    assert ".tpl-01{" in css


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

    chrome_z = [
        int(line.split("z-index:")[1].split(";")[0])
        for line in css.splitlines()
        if line.startswith(".tpl-")
    ]
    content_z = [
        int(line.split("z-index:")[1].split(";")[0])
        for line in css.splitlines()
        if line.startswith(".r-")
    ]
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

    # The group itself carries no fill; only its children are painted.
    fills = {shape.fill_color for shape in chrome if shape.fill_color}
    assert "#90C226" in fills
    assert len(chrome) >= 8
