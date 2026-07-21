"""幻灯片预览路由测试。"""

import json
import re
import shutil
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from deeppresenter.server.models.artifacts import task_dir
from deeppresenter.server.models.events import TaskStatus
from deeppresenter.server.models.templates import TemplateManifest, TemplateStatus
from deeppresenter.server.services.template_catalog import bundled_templates_root


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
    assert len(list_body) == 1
    assert list_body[0]["slide_id"] == artifact.slide_id
    assert list_body[0]["task_id"] == task_id
    assert list_body[0]["revision"] == 1
    assert list_body[0]["current_revision"] == 1
    assert list_body[0]["preview_url"].endswith("/preview.png")

    detail_response = client.get(f"/api/tasks/{task_id}/slides/{artifact.slide_id}")
    assert detail_response.status_code == 200
    detail_body = detail_response.json()
    assert detail_body["slide_id"] == artifact.slide_id
    assert detail_body["revision_count"] == 1
    assert detail_body["source_path"] == "slides/slide_01.html"

    task_response = client.get(f"/api/tasks/{task_id}")
    assert task_response.status_code == 200
    task_body = task_response.json()
    assert task_body["topic"] == "测试预览路由"
    assert task_body["ratio"] == "16:9"
    assert "last_seq" in task_body
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
    slide = list_response.json()[0]
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


def test_create_task_accepts_frontend_payload(tmp_workspace):
    app = _create_test_app(tmp_workspace)
    client = TestClient(app)

    response = client.post(
        "/api/tasks",
        json={
            "topic": "前端字段兼容测试",
            "template_id": "default",
            "page_count": 5,
            "ratio": "16:9",
            "language": "zh-CN",
            "attachment_ids": [],
        },
    )

    assert response.status_code == 201
    body = response.json()
    task_id = body["task_id"]
    assert body["topic"] == "前端字段兼容测试"
    assert body["template_id"] == "default"
    assert body["ratio"] == "16:9"
    snapshot = app.state.task_manager.get_snapshot(task_id)
    assert snapshot.instruction == "前端字段兼容测试"
    assert snapshot.generation_params["num_pages"] == "5"
    assert snapshot.generation_params["powerpoint_type"] == "16:9"
    assert snapshot.generation_params["language"] == "zh"
    assert snapshot.generation_params["template_id"] == "default"
    assert snapshot.generation_params["template"] == "default"


def test_create_task_inherits_ratio_from_template_when_omitted(tmp_workspace):
    app = _create_test_app(tmp_workspace)
    client = TestClient(app)

    response = client.post(
        "/api/tasks",
        json={"topic": "四比三模板", "template_id": "beamer"},
    )

    assert response.status_code == 201
    assert response.json()["ratio"] == "4:3"
    snapshot = app.state.task_manager.get_snapshot(response.json()["task_id"])
    assert snapshot.generation_params["powerpoint_type"] == "4:3"


def test_create_task_rejects_ratio_that_conflicts_with_template(tmp_workspace):
    app = _create_test_app(tmp_workspace)
    client = TestClient(app)

    response = client.post(
        "/api/tasks",
        json={
            "topic": "错误比例",
            "template_id": "beamer",
            "ratio": "16:9",
        },
    )

    assert response.status_code == 422
    assert "4:3" in response.json()["detail"]


def test_slide_edit_revisions_fallback_to_current_preview(tmp_workspace):
    app = _create_test_app(tmp_workspace)
    client = TestClient(app)

    response = client.post("/api/tasks", json={"instruction": "测试编辑版本 fallback"})
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

    response = client.get(f"/api/tasks/{task_id}/slides/{artifact.slide_id}/revisions")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["revision"] == 1
    assert body[0]["label"] == "初始版本"
    assert body[0]["created_at"]
    assert (
        body[0]["preview_url"]
        == f"/api/tasks/{task_id}/artifacts/{artifact.preview_path}"
    )


def test_slide_edit_chat_updates_html_preview_and_emits_applied_event(tmp_workspace):
    app = _create_test_app(tmp_workspace)
    client = TestClient(app)

    response = client.post("/api/tasks", json={"instruction": "测试 HTML 编辑事件"})
    assert response.status_code == 201
    task_id = response.json()["task_id"]

    service = app.state.task_manager.get_preview_service(task_id)
    service.renderer = fake_renderer
    root = task_dir(tmp_workspace, task_id)
    html_dir = root / "slides"
    html_dir.mkdir(parents=True, exist_ok=True)
    html = html_dir / "slide_01.html"
    html.write_text(
        "<html><body><h1>原始标题</h1><p>原始正文</p></body></html>",
        encoding="utf-8",
    )

    import anyio

    artifact = anyio.run(service.render_html_slide, task_id, html)

    response = client.post(
        f"/api/tasks/{task_id}/slides/{artifact.slide_id}/chat",
        json={"text": "把标题改成 新标题"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["chat_id"]
    assert body["status"] == "queued"

    updated = service.get_slide(task_id, artifact.slide_id)
    assert updated.revision == 2
    assert updated.preview_path.endswith("/2/preview.png")
    assert (task_dir(tmp_workspace, task_id) / updated.source_path).read_text(
        encoding="utf-8"
    ) == "<html><body><h1>新标题</h1><p>原始正文</p></body></html>"

    bus = app.state.task_manager.get_event_bus(task_id)
    events = list(bus._replay(0))
    applied = [event for event in events if event.get("type") == "edit.applied"]
    assert applied
    assert applied[-1]["slide_id"] == artifact.slide_id
    assert applied[-1]["stage"] == "edit"
    assert applied[-1]["artifact_url"].endswith("/2/preview.png")
    assert applied[-1]["payload"]["chat_id"] == body["chat_id"]
    assert applied[-1]["payload"]["action"] == "把标题改成 新标题"
    assert applied[-1]["payload"]["revision"] == 2


def test_templates_route_returns_real_backend_templates(tmp_workspace):
    app = _create_test_app(tmp_workspace)
    client = TestClient(app)

    response = client.get("/api/templates")

    assert response.status_code == 200
    templates = response.json()
    assert templates
    assert templates[0]["status"] == "ready"
    assert "palette" in templates[0]
    template_ids = {template["id"] for template in templates}
    assert {"beamer", "cip", "default", "hit", "thu", "ucas"} <= template_ids
    assert not {"obsidian", "mist", "azure", "jade"} & template_ids

    by_id = {template["id"]: template for template in templates}
    for template_id in ("beamer", "cip", "default", "hit", "thu", "ucas"):
        template = by_id[template_id]
        assert template["layouts"]
        assert template["revision_id"].startswith("rev_")
        assert set(template["palette"]) == {
            "bg",
            "surface",
            "primary",
            "accent",
            "ink",
            "dark",
        }
        for field in ("bg", "surface", "primary", "accent", "ink"):
            assert re.fullmatch(r"#[0-9A-F]{6}", template["palette"][field])

    # The cover stage maps to a stable label; the remaining families depend on
    # the annotator, so assert diversity rather than specific stage labels.
    assert "封面" in by_id["default"]["layouts"]
    assert len(set(by_id["default"]["layouts"])) >= 2
    assert by_id["thu"]["palette"]["primary"] != "#38506B"


def test_bundled_template_detail_matches_list_contract(tmp_workspace):
    app = _create_test_app(tmp_workspace)
    client = TestClient(app)

    templates = client.get("/api/templates").json()
    listed = next(template for template in templates if template["id"] == "thu")
    response = client.get("/api/templates/thu")

    assert response.status_code == 200
    assert response.json() == listed


def test_bundled_template_summary_serves_first_slide_thumbnail(tmp_workspace):
    app = _create_test_app(tmp_workspace)
    client = TestClient(app)

    summary = client.get("/api/templates/hit").json()
    thumbnail_url = summary["thumbnail_url"]
    response = client.get(thumbnail_url)

    assert summary["revision_id"] in thumbnail_url
    assert response.status_code == 200
    assert response.headers["content-type"] == "image/webp"
    assert response.headers["cache-control"] == (
        "public, max-age=31536000, immutable"
    )
    assert response.content.startswith(b"RIFF")


def test_user_template_statuses_match_frontend_contract(tmp_workspace):
    app = _create_test_app(tmp_workspace)
    registry = app.state.template_registry
    registry.register(
        TemplateManifest(
            template_id="pending_upload",
            name="解析中模板",
            status=TemplateStatus.COMPILING,
            slide_count=7,
        )
    )
    registry.register(
        TemplateManifest(
            template_id="failed_upload",
            name="解析失败模板",
            status=TemplateStatus.FAILED,
            slide_count=3,
            error="VLM annotation failed",
        )
    )
    client = TestClient(app)

    response = client.get("/api/templates")

    assert response.status_code == 200
    by_id = {template["id"]: template for template in response.json()}
    assert by_id["pending_upload"]["status"] == "parsing"
    assert by_id["pending_upload"]["progress"] == 0
    assert by_id["pending_upload"]["layouts"] == []
    assert by_id["failed_upload"]["status"] == "failed"
    assert by_id["failed_upload"]["error"] == "VLM annotation failed"

    detail = client.get("/api/templates/pending_upload")
    assert detail.status_code == 200
    assert detail.json() == by_id["pending_upload"]


def test_ready_user_template_summary_is_derived_from_compiled_ir(tmp_workspace):
    app = _create_test_app(tmp_workspace)
    registry = app.state.template_registry
    template_id = "uploaded_beamer"
    target = registry.templates_dir / template_id
    shutil.copytree(bundled_templates_root() / "beamer", target)

    manifest_path = target / "manifest.json"
    manifest_data = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest_data["template_id"] = template_id
    manifest_data["name"] = "用户 Beamer"
    manifest_path.write_text(
        json.dumps(manifest_data, ensure_ascii=False),
        encoding="utf-8",
    )
    revision_path = (
        target
        / "revisions"
        / manifest_data["active_revision_id"]
        / "revision.json"
    )
    revision_data = json.loads(revision_path.read_text(encoding="utf-8"))
    revision_data["template_id"] = template_id
    revision_path.write_text(
        json.dumps(revision_data, ensure_ascii=False),
        encoding="utf-8",
    )
    registry.register(TemplateManifest.load(target))
    client = TestClient(app)

    response = client.get(f"/api/templates/{template_id}")

    assert response.status_code == 200
    summary = response.json()
    assert summary["owner"] == "user"
    assert summary["status"] == "ready"
    assert summary["revision_id"] == manifest_data["active_revision_id"]
    assert summary["layouts"]
    assert "封面" in summary["layouts"]
    assert summary["palette"]["primary"] != "#38506B"
    assert summary["revision_id"] in summary["thumbnail_url"]

    thumbnail = client.get(summary["thumbnail_url"])
    assert thumbnail.status_code == 200
    assert thumbnail.headers["content-type"] == "image/webp"


def test_create_task_rejects_frontend_only_template(tmp_workspace):
    app = _create_test_app(tmp_workspace)
    client = TestClient(app)

    response = client.post(
        "/api/tasks",
        json={"topic": "不存在的前端模板", "template_id": "obsidian"},
    )

    assert response.status_code == 422


def test_frontend_attachment_upload_can_be_used_for_create(tmp_workspace):
    app = _create_test_app(tmp_workspace)
    client = TestClient(app)

    upload = client.post(
        "/api/attachments",
        files={"file": ("notes.txt", b"hello", "text/plain")},
    )
    assert upload.status_code == 200
    attachment_id = upload.json()["attachment_id"]

    response = client.post(
        "/api/tasks",
        json={
            "topic": "带附件生成",
            "page_count": 5,
            "attachment_ids": [attachment_id],
        },
    )

    assert response.status_code == 201
    task_id = response.json()["task_id"]
    snapshot = app.state.task_manager.get_snapshot(task_id)
    [attachment_path] = snapshot.generation_params["attachments"]
    assert Path(attachment_path).exists()
    assert Path(attachment_path).name == "notes.txt"

    delete = client.delete(f"/api/attachments/{attachment_id}")
    assert delete.status_code == 204
