"""幻灯片预览路由测试。"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deeppresenter.server.models.artifacts import task_dir
from deeppresenter.server.models.events import TaskStatus


@pytest.fixture
def tmp_workspace() -> Path:
    tmp = tempfile.mkdtemp(prefix="routes_slides_test_")
    yield Path(tmp)
    import shutil

    shutil.rmtree(tmp, ignore_errors=True)


async def fake_renderer(html_path: Path, output_path: Path, aspect_ratio: str) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_bytes(b"fake-png")


def _create_test_app(tmp_workspace: Path):
    import os

    os.environ["DEEPPRESENTER_WORKSPACE_BASE"] = str(tmp_workspace)
    from deeppresenter.server.app import create_app

    return create_app(tmp_workspace, use_placeholder=True)


def test_list_and_get_slide_preview_routes(tmp_workspace):
    app = _create_test_app(tmp_workspace)
    client = TestClient(app)

    response = client.post(
        "/api/tasks",
        json={
            "instruction": "测试预览路由",
            "num_pages": "1",
            "powerpoint_type": "16:9",
            "language": "zh",
        },
    )
    assert response.status_code == 201
    task_id = response.json()["task_id"]

    service = app.state.task_manager.get_preview_service(task_id)
    service.renderer = fake_renderer
    root = task_dir(tmp_workspace, task_id)
    html_dir = root / "slides"
    html_dir.mkdir(parents=True, exist_ok=True)
    html = html_dir / "slide_01.html"
    html.write_text("<html><body>slide</body></html>", encoding="utf-8")

    import anyio

    artifact = anyio.run(service.render_html_slide, task_id, html)

    list_response = client.get(f"/api/tasks/{task_id}/slides")
    assert list_response.status_code == 200
    list_body = list_response.json()
    assert list_body["total"] == 1
    assert list_body["slides"][0]["slide_id"] == artifact.slide_id
    assert list_body["slides"][0]["current_revision"] == 1
    assert list_body["slides"][0]["preview_url"].endswith("/preview.png")

    detail_response = client.get(f"/api/tasks/{task_id}/slides/{artifact.slide_id}")
    assert detail_response.status_code == 200
    detail_body = detail_response.json()
    assert detail_body["slide_id"] == artifact.slide_id
    assert detail_body["revision_count"] == 1
    assert detail_body["source_path"] == "slides/slide_01.html"

    task_response = client.get(f"/api/tasks/{task_id}")
    assert task_response.status_code == 200
    task_body = task_response.json()
    assert task_body["slides"][0]["slide_id"] == artifact.slide_id
    assert task_body["slides"][0]["preview_url"].endswith("/preview.png")


def test_template_slide_preview_is_visible_via_routes(tmp_workspace):
    app = _create_test_app(tmp_workspace)
    client = TestClient(app)

    response = client.post(
        "/api/tasks",
        json={
            "instruction": "测试模板预览路由",
            "num_pages": "1",
            "template": "template-1",
            "convert_type": "pptagent",
            "language": "zh",
        },
    )
    assert response.status_code == 201
    task_id = response.json()["task_id"]

    service = app.state.task_manager.get_preview_service(task_id)
    service.renderer = fake_renderer

    import anyio

    artifact = anyio.run(
        service.render_template_slide,
        task_id,
        1,
        {
            "layout_name": "Title and body",
            "title": "模板页标题",
            "body": ["第一点", "第二点"],
        },
    )

    list_response = client.get(f"/api/tasks/{task_id}/slides")
    assert list_response.status_code == 200
    slide = list_response.json()["slides"][0]
    assert slide["mode"] == "template"
    assert slide["preview_url"].endswith("/preview.png")

    preview_response = client.get(
        f"/api/tasks/{task_id}/artifacts/{artifact.preview_path}"
    )
    assert preview_response.status_code == 200
    assert preview_response.content == b"fake-png"


def test_slide_routes_return_404_for_unknown_task(tmp_workspace):
    app = _create_test_app(tmp_workspace)
    client = TestClient(app)

    response = client.get("/api/tasks/missing/slides")
    assert response.status_code == 404


def test_get_unknown_slide_returns_404(tmp_workspace):
    app = _create_test_app(tmp_workspace)
    client = TestClient(app)

    response = client.post("/api/tasks", json={"instruction": "测试未知页面"})
    assert response.status_code == 201
    task_id = response.json()["task_id"]

    response = client.get(f"/api/tasks/{task_id}/slides/sld-missing")

    assert response.status_code == 404


def test_artifact_route_rejects_path_traversal(tmp_workspace):
    app = _create_test_app(tmp_workspace)
    client = TestClient(app)

    response = client.post("/api/tasks", json={"instruction": "测试路径安全"})
    assert response.status_code == 201
    task_id = response.json()["task_id"]

    secret = tmp_workspace / "secret.txt"
    secret.write_text("secret", encoding="utf-8")

    response = client.get(f"/api/tasks/{task_id}/artifacts/%2E%2E/secret.txt")

    assert response.status_code == 403


def test_artifact_route_returns_404_for_missing_file(tmp_workspace):
    app = _create_test_app(tmp_workspace)
    client = TestClient(app)

    response = client.post("/api/tasks", json={"instruction": "测试缺失产物"})
    assert response.status_code == 201
    task_id = response.json()["task_id"]

    response = client.get(f"/api/tasks/{task_id}/artifacts/slides/missing.png")

    assert response.status_code == 404


def test_export_route_returns_download_url(tmp_workspace):
    app = _create_test_app(tmp_workspace)
    client = TestClient(app)

    response = client.post("/api/tasks", json={"instruction": "测试导出下载"})
    assert response.status_code == 201
    task_id = response.json()["task_id"]

    root = task_dir(tmp_workspace, task_id)
    exports = root / "exports"
    exports.mkdir(parents=True, exist_ok=True)
    (exports / "latest.pptx").write_bytes(b"pptx")

    snapshot = app.state.task_manager.get_snapshot(task_id)
    snapshot.status = TaskStatus.COMPLETED
    snapshot.result_artifact = "exports/latest.pptx"

    response = client.post(f"/api/tasks/{task_id}/export")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "completed"
    assert body["format"] == "pptx"
    assert body["filename"] == "latest.pptx"
    assert body["artifact_path"] == "exports/latest.pptx"
    assert body["download_url"].endswith(
        f"/api/tasks/{task_id}/artifacts/exports/latest.pptx"
    )


def test_export_route_returns_pdf_when_requested(tmp_workspace):
    app = _create_test_app(tmp_workspace)
    client = TestClient(app)

    response = client.post("/api/tasks", json={"instruction": "测试 PDF 导出"})
    assert response.status_code == 201
    task_id = response.json()["task_id"]

    root = task_dir(tmp_workspace, task_id)
    (root / "manuscript.pptx").write_bytes(b"pptx")
    (root / "manuscript.pdf").write_bytes(b"%PDF")

    snapshot = app.state.task_manager.get_snapshot(task_id)
    snapshot.status = TaskStatus.COMPLETED
    snapshot.result_artifact = "manuscript.pptx"

    response = client.post(f"/api/tasks/{task_id}/export", json={"format": "pdf"})

    assert response.status_code == 200
    body = response.json()
    assert body["format"] == "pdf"
    assert body["filename"] == "manuscript.pdf"
    assert body["artifact_path"] == "manuscript.pdf"
    assert body["download_url"].endswith(
        f"/api/tasks/{task_id}/artifacts/manuscript.pdf"
    )


def test_create_task_requires_instruction(tmp_workspace):
    app = _create_test_app(tmp_workspace)
    client = TestClient(app)

    response = client.post("/api/tasks", json={"num_pages": "1"})

    assert response.status_code == 422
