"""Slide-lint contracts that do not need a live browser."""

from __future__ import annotations

import io
from pathlib import Path

from PIL import Image

from deeppresenter.utils.slide_lint import analyze_slide, scaffold_expectations


def _png(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def _busy(width: int, height: int) -> Image.Image:
    """A canvas with drawing everywhere, so only real defects can warn."""

    return Image.effect_noise((width, height), 64).convert("L")


def _data(**overrides: object) -> dict:
    data = {
        "width": 200,
        "height": 100,
        "texts": [
            {"x": 10, "y": 20, "w": 60, "h": 30, "size": 24, "snippet": "Title"}
        ],
        "overflows": [],
    }
    data.update(overrides)
    return data


def test_invisible_text_is_named() -> None:
    image = Image.new("L", (200, 100), 255)
    data = _data(
        texts=[
            {"x": 10, "y": 10, "w": 80, "h": 30, "size": 24, "snippet": "兴庆校区"}
        ]
    )

    warnings = analyze_slide(_png(image), data)

    assert any(
        "nearly invisible" in warning and "兴庆校区" in warning
        for warning in warnings
    )


def test_readable_text_and_busy_canvas_are_clean() -> None:
    assert analyze_slide(_png(_busy(200, 100)), _data()) == []


def test_overflow_and_tiny_type_are_reported() -> None:
    data = _data(
        texts=[{"x": 0, "y": 0, "w": 40, "h": 10, "size": 10, "snippet": "tiny"}],
        overflows=[{"label": "body", "dx": 40, "dy": 0}],
    )

    warnings = analyze_slide(_png(_busy(100, 100)), data)

    assert any("extends 40px" in warning for warning in warnings)
    assert any("below 14px" in warning for warning in warnings)


def test_missing_chrome_and_covered_artwork_are_reported() -> None:
    data = _data(
        missing_chrome=["tpl-02"],
        covered_artwork=[{"label": "r-r001", "fraction": 84}],
    )

    warnings = analyze_slide(_png(_busy(200, 100)), data)

    assert any(
        "tpl-02" in warning and "Required template chrome" in warning
        for warning in warnings
    )
    assert any(
        "r-r001" in warning and "covered" in warning for warning in warnings
    )


def test_overlapping_texts_are_reported_as_a_pair() -> None:
    data = _data(text_overlaps=[{"a": "01", "b": "基础层"}])

    warnings = analyze_slide(_png(_busy(200, 100)), data)

    assert any(
        "01" in warning and "基础层" in warning and "overlap" in warning
        for warning in warnings
    )


def test_a_half_empty_canvas_is_reported() -> None:
    image = _busy(200, 100)
    image.paste(240, (100, 0, 200, 100))

    warnings = analyze_slide(_png(image), _data())

    assert any("empty rectangle" in warning for warning in warnings)


def test_scaffold_expectations_read_the_imported_layout(tmp_path: Path) -> None:
    (tmp_path / "layout.css").write_text(
        ".slide .tpl-01{left:0;background-image:url(a.png)} /* layout / logo */\n"
        ".slide .tpl-02{top:0} /* layout / band */\n"
        ".slide .r-r001{left:5%;background-image:url(b.png)}"
        " /* logo / image -- painted from the template asset; emit an empty div */\n"
        ".r-r002{left:10%} /* title / text */\n",
        encoding="utf-8",
    )
    html = tmp_path / "slide_01.html"
    html.write_text(
        "<style>@import url('layout.css');</style><div class='slide'></div>",
        encoding="utf-8",
    )

    expected = scaffold_expectations(html)

    assert expected["required"] == ["tpl-01", "tpl-02", "r-r001"]
    assert expected["artwork"] == ["tpl-01", "r-r001"]
