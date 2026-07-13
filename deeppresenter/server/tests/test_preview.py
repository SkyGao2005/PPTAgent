"""PreviewService 单元测试。"""

import tempfile
from pathlib import Path

import pytest

from deeppresenter.server.models.artifacts import revision_dir, slide_dir, task_dir
from deeppresenter.server.services.preview import (
    PreviewService,
    artifact_url,
    parse_slide_index,
    stable_slide_id,
)


@pytest.fixture
def tmp_workspace() -> Path:
    tmp = tempfile.mkdtemp(prefix="preview_test_")
    yield Path(tmp)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


async def fake_renderer(html_path: Path, output_path: Path, aspect_ratio: str) -> None:
    assert html_path.exists()
    assert aspect_ratio == "16:9"
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"fake-png")


def test_parse_slide_index():
    assert parse_slide_index(Path("slide_01.html")) == 1
    assert parse_slide_index(Path("slide-12.html")) == 12
    with pytest.raises(ValueError):
        parse_slide_index(Path("intro.html"))


def test_stable_slide_id():
    first = stable_slide_id("abc12345", 1)
    second = stable_slide_id("abc12345", 1)
    other = stable_slide_id("abc12345", 2)
    assert first == second
    assert first.startswith("sld-")
    assert first != other


def test_artifact_url():
    assert (
        artifact_url("abc12345", "slides/sld-1/revisions/1/preview.png")
        == "/api/tasks/abc12345/artifacts/slides/sld-1/revisions/1/preview.png"
    )


@pytest.mark.asyncio
async def test_render_html_slide_persists_artifact(tmp_workspace):
    task_id = "abc12345"
    root = task_dir(tmp_workspace, task_id)
    html_dir = root / "slides"
    html_dir.mkdir(parents=True)
    html = html_dir / "slide_01.html"
    html.write_text("<html><body>hello</body></html>", encoding="utf-8")

    service = PreviewService(tmp_workspace, renderer=fake_renderer)
    artifact = await service.render_html_slide(task_id, html)

    assert artifact.task_id == task_id
    assert artifact.index == 1
    assert artifact.status == "completed"
    assert artifact.source_path == "slides/slide_01.html"
    assert artifact.preview_path.endswith("/preview.png")

    current = slide_dir(tmp_workspace, task_id, artifact.slide_id) / "current.json"
    revision = revision_dir(tmp_workspace, task_id, artifact.slide_id, 1) / "slide.json"
    preview = root / artifact.preview_path
    assert current.exists()
    assert revision.exists()
    assert preview.read_bytes() == b"fake-png"


@pytest.mark.asyncio
async def test_render_html_slide_reuses_existing_slide_id(tmp_workspace):
    task_id = "abc12345"
    root = task_dir(tmp_workspace, task_id)
    html_dir = root / "slides"
    html_dir.mkdir(parents=True)
    html = html_dir / "slide_02.html"
    html.write_text("<html><body>hello</body></html>", encoding="utf-8")

    service = PreviewService(tmp_workspace, renderer=fake_renderer)
    first = await service.render_html_slide(task_id, html)
    second = await service.render_html_slide(task_id, html)

    assert first.slide_id == second.slide_id
    assert second.revision == 1


@pytest.mark.asyncio
async def test_render_html_slide_explicit_revision_updates_current(tmp_workspace):
    task_id = "abc12345"
    root = task_dir(tmp_workspace, task_id)
    html_dir = root / "slides"
    html_dir.mkdir(parents=True)
    html = html_dir / "slide_03.html"
    html.write_text("<html><body>hello</body></html>", encoding="utf-8")

    service = PreviewService(tmp_workspace, renderer=fake_renderer)
    first = await service.render_html_slide(task_id, html)
    second = await service.render_html_slide(task_id, html, revision=2)

    current = slide_dir(tmp_workspace, task_id, first.slide_id) / "current.json"
    rev2 = revision_dir(tmp_workspace, task_id, first.slide_id, 2) / "slide.json"
    assert first.slide_id == second.slide_id
    assert second.revision == 2
    assert rev2.exists()
    assert '"revision": 2' in current.read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_render_html_slide_rejects_outside_workspace(tmp_workspace):
    html = tmp_workspace / "outside.html"
    html.write_text("<html></html>", encoding="utf-8")
    service = PreviewService(tmp_workspace, renderer=fake_renderer)

    with pytest.raises(ValueError):
        await service.render_html_slide("abc12345", html)
