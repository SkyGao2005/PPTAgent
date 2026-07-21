from pathlib import Path

from PIL import Image

from deeppresenter.templates.models import (
    BoundingBox,
    Canvas,
    NormalizedBox,
    ShapeKind,
    ShapeNode,
    ShapeScope,
    SourceGraph,
)
from deeppresenter.templates.rendering import (
    LibreOfficeRenderer,
    create_shape_overlay,
    overlay_labels,
)


def test_renderer_prefers_stable_libreoffice(monkeypatch, tmp_path: Path) -> None:
    development = tmp_path / "soffice-dev"
    stable = tmp_path / "soffice-stable"
    development.touch()
    stable.touch()
    versions = {
        str(development.resolve()): "LibreOfficeDev 26.8.0.0.alpha0",
        str(stable.resolve()): "LibreOffice 26.2.4.2",
    }

    monkeypatch.delenv("DEEPPRESENTER_SOFFICE", raising=False)
    monkeypatch.setattr(
        LibreOfficeRenderer,
        "_candidate_executables",
        staticmethod(lambda: [str(development), str(stable)]),
    )
    monkeypatch.setattr(
        LibreOfficeRenderer,
        "_inspect_executable",
        staticmethod(lambda candidate: (str(Path(candidate).resolve()), versions[str(Path(candidate).resolve())])),
    )

    renderer = LibreOfficeRenderer()

    assert renderer._executable == str(stable.resolve())
    assert "26.2.4.2" in renderer.renderer_id


def test_renderer_honors_explicit_soffice_override(monkeypatch, tmp_path: Path) -> None:
    configured = tmp_path / "configured-soffice"
    configured.touch()
    resolved = str(configured.resolve())

    monkeypatch.setenv("DEEPPRESENTER_SOFFICE", str(configured))
    monkeypatch.setattr(
        LibreOfficeRenderer,
        "_inspect_executable",
        staticmethod(lambda candidate: (resolved, "LibreOfficeDev custom")),
    )

    renderer = LibreOfficeRenderer()

    assert renderer._executable == resolved
    assert "LibreOfficeDev-custom" in renderer.renderer_id


def _shape(shape_id: str, *, visible: bool = True, width: float = 0.4) -> ShapeNode:
    return ShapeNode(
        shape_id=shape_id,
        name=shape_id,
        scope=ShapeScope.SLIDE,
        kind=ShapeKind.TEXT,
        z_index=0,
        bbox=BoundingBox(x=0, y=0, width=100, height=50),
        normalized_bbox=NormalizedBox(x=0.1, y=0.1, width=width, height=0.2),
        visible=visible,
    )


def _graph(shapes: list[ShapeNode]) -> SourceGraph:
    return SourceGraph(
        slide_id="s001",
        page_number=1,
        canvas=Canvas(
            width_emu=12192000,
            height_emu=6858000,
            width_inches=13.333,
            height_inches=7.5,
            aspect_ratio="16:9",
        ),
        shapes=shapes,
    )


def test_overlay_labels_skip_invisible_and_degenerate_shapes() -> None:
    graph = _graph(
        [
            _shape("a"),
            _shape("b", visible=False),
            _shape("c", width=0.0),
            _shape("d"),
        ]
    )

    labels = overlay_labels(graph)

    assert [shape.shape_id for shape in labels.values()] == ["a", "d"]
    assert list(labels) == ["#1", "#2"]


def test_create_shape_overlay_writes_labeled_image(tmp_path: Path) -> None:
    reference = tmp_path / "reference.webp"
    Image.new("RGB", (1600, 900), "white").save(reference, "WEBP")
    graph = _graph([_shape("a"), _shape("b")])
    output = tmp_path / "overlay.webp"

    result = create_shape_overlay(reference, graph, output)

    assert result == output
    with Image.open(output) as image:
        assert image.size == (1600, 900)
