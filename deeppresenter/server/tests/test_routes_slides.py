"""幻灯片预览路由测试。"""

import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deeppresenter.server.models.artifacts import task_dir


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


def test_slide_routes_return_404_for_unknown_task(tmp_workspace):
    app = _create_test_app(tmp_workspace)
    client = TestClient(app)

    response = client.get("/api/tasks/missing/slides")
    assert response.status_code == 404
