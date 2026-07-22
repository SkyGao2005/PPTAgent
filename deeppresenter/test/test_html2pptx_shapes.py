"""Export contracts for shapes PowerPoint has no native equivalent for."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pptx import Presentation
from pptx.enum.shapes import MSO_SHAPE_TYPE

from deeppresenter.utils.webview import convert_html_to_pptx

_SLIDE = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0}
.slide{position:relative;width:1280px;height:720px;background:#fff}
.plain{position:absolute;left:5%;top:5%;width:20%;height:20%;background:#005EA4}
.clipped{position:absolute;left:40%;top:5%;width:20%;height:20%;background:#039ACF;
  clip-path:polygon(50% 0%, 100% 100%, 0% 100%)}
</style></head><body><main class="slide">
<div class="plain"></div><div class="clipped"></div>
</main></body></html>"""


def _export(tmp_path: Path) -> Presentation:
    (tmp_path / "slide_01.html").write_text(_SLIDE, encoding="utf-8")
    output = tmp_path / "deck.pptx"
    asyncio.run(convert_html_to_pptx(tmp_path, output, aspect_ratio="16:9"))
    return Presentation(str(output))


def test_a_clipped_shape_survives_the_pptx_export(tmp_path: Path) -> None:
    """PowerPoint cannot express a clip, so the element must be rasterized.

    Exported as an autoshape it becomes the rectangle enclosing the clip, and
    every triangle, chevron and capsule in a template flattens into a block.
    """

    if not Path(__file__).parents[1].joinpath("html2pptx", "node_modules").is_dir():
        pytest.skip("html2pptx dependencies are not installed")

    slide = _export(tmp_path).slides[0]
    pictures = [s for s in slide.shapes if s.shape_type == MSO_SHAPE_TYPE.PICTURE]
    autoshapes = [s for s in slide.shapes if s.shape_type == MSO_SHAPE_TYPE.AUTO_SHAPE]

    # The unclipped block stays a real autoshape; only the clip is rasterized.
    assert len(autoshapes) >= 1
    assert len(pictures) == 1
    picture = pictures[0]
    assert picture.width > 0 and picture.height > 0


_ROTATED = """<!doctype html><html><head><meta charset="utf-8"><style>
html,body{margin:0}
.slide{position:relative;width:1280px;height:720px;background:#fff}
/* An upward triangle turned upside down, as a template's arrows are. */
.arrow{position:absolute;left:40%;top:30%;width:10%;height:10%;background:#00539E;
  clip-path:polygon(50% 0%, 100% 100%, 0% 100%);transform:rotate(180deg)}
</style></head><body><main class="slide"><div class="arrow"></div></main></body></html>"""


def test_a_rotated_clip_keeps_its_orientation(tmp_path: Path) -> None:
    """getBoundingClientRect reports the rotated box, not the element's own.

    Rasterizing at that size without applying the transform exported every
    arrow unrotated, so a template's downward markers came out as raw
    right-angles pointing the wrong way.
    """

    if not Path(__file__).parents[1].joinpath("html2pptx", "node_modules").is_dir():
        pytest.skip("html2pptx dependencies are not installed")

    import io

    from PIL import Image

    (tmp_path / "slide_01.html").write_text(_ROTATED, encoding="utf-8")
    output = tmp_path / "deck.pptx"
    asyncio.run(convert_html_to_pptx(tmp_path, output, aspect_ratio="16:9"))

    slide = Presentation(str(output)).slides[0]
    picture = next(
        s for s in slide.shapes if s.shape_type == MSO_SHAPE_TYPE.PICTURE
    )
    with Image.open(io.BytesIO(picture.image.blob)).convert("RGBA") as image:
        alpha = image.getchannel("A")
        width, height = alpha.size
        top = sum(
            alpha.getpixel((x, y)) > 128
            for y in range(height // 4)
            for x in range(0, width, 4)
        )
        bottom = sum(
            alpha.getpixel((x, y)) > 128
            for y in range(height - height // 4, height)
            for x in range(0, width, 4)
        )

    # A downward triangle is widest at the top.
    assert top > bottom * 2
