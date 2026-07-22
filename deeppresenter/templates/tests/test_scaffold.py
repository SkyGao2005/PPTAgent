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
from deeppresenter.templates.styles import StyleResolver

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
