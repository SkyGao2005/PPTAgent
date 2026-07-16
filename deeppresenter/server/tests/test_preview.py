"""PreviewService 单元测试。"""

import os
import tempfile
from pathlib import Path

import pytest

from deeppresenter.server.models.artifacts import revision_dir, slide_dir, slides_dir, task_dir
from deeppresenter.server.services.preview import (
    PreviewService,
    SLIDES_INDEX_FILE,
    artifact_url,
    build_template_preview_html,
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


async def failing_renderer(html_path: Path, output_path: Path, aspect_ratio: str) -> None:
    raise RuntimeError("render failed")


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


def test_build_template_preview_html_escapes_content():
    html = build_template_preview_html(
        {
            "layout_name": "<layout>",
            "title": "<Title>",
            "body": ["<point>"],
            "extras": {"note": "<script>"},
        }
    )

    assert "&lt;Title&gt;" in html
    assert "&lt;point&gt;" in html
    assert "<script>" not in html


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
    assert service.revision_count(task_id, first.slide_id) == 2


@pytest.mark.asyncio
async def test_render_html_slide_maintains_task_slide_index(tmp_workspace):
    task_id = "abc12345"
    root = task_dir(tmp_workspace, task_id)
    html_dir = root / "slides"
    html_dir.mkdir(parents=True)
    slide_2 = html_dir / "slide_02.html"
    slide_1 = html_dir / "slide_01.html"
    slide_2.write_text("<html><body>two</body></html>", encoding="utf-8")
    slide_1.write_text("<html><body>one</body></html>", encoding="utf-8")

    service = PreviewService(tmp_workspace, renderer=fake_renderer)
    second = await service.render_html_slide(task_id, slide_2)
    first = await service.render_html_slide(task_id, slide_1)

    index_path = slides_dir(tmp_workspace, task_id) / SLIDES_INDEX_FILE
    assert index_path.exists()

    slides = service.list_slides(task_id)
    assert [slide.slide_id for slide in slides] == [first.slide_id, second.slide_id]
    assert [slide.index for slide in slides] == [1, 2]
    assert service.get_slide(task_id, first.slide_id).preview_path == first.preview_path


def test_list_slides_returns_empty_for_task_without_previews(tmp_workspace):
    service = PreviewService(tmp_workspace, renderer=fake_renderer)
    assert service.list_slides("abc12345") == []
    assert service.get_slide("abc12345", "sld-missing") is None
    assert service.revision_count("abc12345", "sld-missing") == 0


@pytest.mark.asyncio
async def test_render_html_slide_rejects_outside_workspace(tmp_workspace):
    html = tmp_workspace / "outside.html"
    html.write_text("<html></html>", encoding="utf-8")
    service = PreviewService(tmp_workspace, renderer=fake_renderer)

    with pytest.raises(ValueError):
        await service.render_html_slide("abc12345", html)


@pytest.mark.asyncio
async def test_render_html_slide_renderer_failure_preserves_no_current(tmp_workspace):
    task_id = "abc12345"
    root = task_dir(tmp_workspace, task_id)
    html_dir = root / "slides"
    html_dir.mkdir(parents=True)
    html = html_dir / "slide_01.html"
    html.write_text("<html><body>hello</body></html>", encoding="utf-8")

    service = PreviewService(tmp_workspace, renderer=failing_renderer)

    with pytest.raises(RuntimeError, match="render failed"):
        await service.render_html_slide(task_id, html)

    sid = stable_slide_id(task_id, 1)
    assert not (slide_dir(tmp_workspace, task_id, sid) / "current.json").exists()
    assert not (slides_dir(tmp_workspace, task_id) / SLIDES_INDEX_FILE).exists()


@pytest.mark.asyncio
async def test_list_slides_falls_back_when_index_is_corrupt(tmp_workspace):
    task_id = "abc12345"
    root = task_dir(tmp_workspace, task_id)
    html_dir = root / "slides"
    html_dir.mkdir(parents=True)
    html = html_dir / "slide_01.html"
    html.write_text("<html><body>hello</body></html>", encoding="utf-8")

    service = PreviewService(tmp_workspace, renderer=fake_renderer)
    artifact = await service.render_html_slide(task_id, html)
    (slides_dir(tmp_workspace, task_id) / SLIDES_INDEX_FILE).write_text(
        "{not-json", encoding="utf-8"
    )

    slides = service.list_slides(task_id)

    assert len(slides) == 1
    assert slides[0].slide_id == artifact.slide_id


@pytest.mark.asyncio
async def test_render_html_preview_real_playwright_opt_in(tmp_workspace):
    """真实 Playwright 截图冒烟测试，默认跳过以避免 CI 强依赖浏览器。"""
    if os.getenv("DEEPPRESENTER_RUN_REAL_PREVIEW_TEST") != "1":
        pytest.skip("Set DEEPPRESENTER_RUN_REAL_PREVIEW_TEST=1 to run real preview rendering")

    try:
        from playwright.async_api import async_playwright  # noqa: F401
    except ImportError:
        pytest.skip("playwright is not installed")

    from deeppresenter.server.services.preview import render_html_preview

    task_id = "abc12345"
    root = task_dir(tmp_workspace, task_id)
    html_dir = root / "slides"
    html_dir.mkdir(parents=True)
    html = html_dir / "slide_01.html"
    html.write_text(
        """
        <!doctype html>
        <html>
          <head>
            <style>
              html, body { margin: 0; width: 100%; height: 100%; }
              body { display: grid; place-items: center; background: #f8fafc; }
              h1 { color: #0f172a; font-family: Arial, sans-serif; }
            </style>
          </head>
          <body><h1>Preview smoke test</h1></body>
        </html>
        """,
        encoding="utf-8",
    )
    output = root / "preview.png"

    await render_html_preview(html, output, "16:9")

    assert output.exists()
    assert output.read_bytes().startswith(b"\x89PNG\r\n\x1a\n")


@pytest.mark.asyncio
async def test_render_template_slide_generates_preview_and_source(tmp_workspace):
    task_id = "abc12345"
    service = PreviewService(tmp_workspace, renderer=fake_renderer)

    artifact = await service.render_template_slide(
        task_id,
        1,
        {
            "layout_name": "Title and body",
            "title": "季度总结",
            "subtitle": "Q2",
            "body": ["营收增长", "成本下降"],
            "images": [{"path": "chart.png", "caption": "增长趋势"}],
            "extras": {"footer": "confidential"},
        },
    )

    root = task_dir(tmp_workspace, task_id)
    rev = revision_dir(tmp_workspace, task_id, artifact.slide_id, 1)
    assert artifact.mode == "template"
    assert artifact.layout_name == "Title and body"
    assert artifact.structured_data.body == ["营收增长", "成本下降"]
    assert artifact.source_path.endswith("/source.json")
    assert artifact.preview_path.endswith("/preview.png")
    assert (rev / "source.json").exists()
    assert (rev / "template_preview.html").exists()
    assert (root / artifact.preview_path).read_bytes() == b"fake-png"


@pytest.mark.asyncio
async def test_render_template_slide_accepts_string_body(tmp_workspace):
    service = PreviewService(tmp_workspace, renderer=fake_renderer)

    artifact = await service.render_template_slide(
        "abc12345",
        2,
        {
            "title": "单段正文",
            "body": "这里是正文",
        },
    )

    assert artifact.structured_data.body == ["这里是正文"]
