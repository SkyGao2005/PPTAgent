"""Outline review gate API tests."""

from __future__ import annotations

import os
from pathlib import Path

from fastapi.testclient import TestClient

from deeppresenter.server.models.artifacts import task_dir
from deeppresenter.server.models.outlines import OutlineDraftRecord, OutlineStatus
from deeppresenter.server.services.outline_service import OutlineService


def _create_test_app(tmp_path: Path):
    os.environ["DEEPPRESENTER_WORKSPACE_BASE"] = str(tmp_path)
    from deeppresenter.server.app import create_app

    return create_app(tmp_path, use_placeholder=True)


def test_outline_review_does_not_create_task_until_approved(tmp_path: Path) -> None:
    app = _create_test_app(tmp_path)
    client = TestClient(app)

    created = client.post(
        "/api/outlines",
        json={
            "topic": "季度经营复盘",
            "template_id": "default",
            "page_count": 8,
            "ratio": "16:9",
            "language": "zh-CN",
            "attachment_ids": [],
        },
    )

    assert created.status_code == 201
    outline = created.json()
    assert outline["status"] == "ready"
    assert outline["revision"] == 1
    assert outline["last_seq"] >= 2
    assert outline["manuscript"].startswith("# 季度经营复盘")
    assert outline["manuscript"].count("\n---\n") == 7
    assert app.state.task_manager.list_tasks() == []
    review_workspace = task_dir(tmp_path, outline["outline_id"])
    assert (review_workspace / "outline.json").is_file()
    assert (review_workspace / "manuscript-v1.md").is_file()
    assert not (tmp_path / "_outlines").exists()
    research_artifact = review_workspace / "research-artifact.txt"
    research_artifact.write_text("keep me for Design", encoding="utf-8")
    review_bus = app.state.task_manager.get_event_bus(outline["outline_id"])
    assert review_bus is not None

    regenerated = client.post(
        f"/api/outlines/{outline['outline_id']}/regenerate",
        json={"comment": "先讲核心结论，再补充背景"},
    )

    assert regenerated.status_code == 202
    revised = regenerated.json()
    assert revised["status"] == "ready"
    assert revised["revision"] == 2
    assert revised["last_seq"] > outline["last_seq"]
    assert revised["comments"][0]["text"] == "先讲核心结论，再补充背景"
    assert "先讲核心结论，再补充背景" in revised["manuscript"]
    assert app.state.task_manager.list_tasks() == []

    approved = client.post(f"/api/outlines/{outline['outline_id']}/approve")

    assert approved.status_code == 201
    task_id = approved.json()["task_id"]
    assert task_id == outline["outline_id"]
    snapshot = app.state.task_manager.get_snapshot(task_id)
    assert snapshot is not None
    assert app.state.task_manager.get_event_bus(task_id) is review_bus
    assert research_artifact.read_text(encoding="utf-8") == "keep me for Design"
    assert snapshot.generation_params["enable_planner"] is False
    manuscript_path = Path(snapshot.generation_params["manuscript_path"])
    assert manuscript_path == task_dir(tmp_path, task_id) / "approved_manuscript.md"
    assert manuscript_path.read_text(encoding="utf-8").startswith("# 季度经营复盘")
    event_types = [event["type"] for event in review_bus._replay(0)]
    assert "outline.generating" in event_types
    assert "outline.ready" in event_types
    assert "outline.approved" in event_types
    assert "task.created" in event_types


def test_outline_approval_is_idempotent(tmp_path: Path) -> None:
    app = _create_test_app(tmp_path)
    client = TestClient(app)
    outline_id = client.post(
        "/api/outlines",
        json={"topic": "产品发布", "page_count": 6},
    ).json()["outline_id"]

    first = client.post(f"/api/outlines/{outline_id}/approve")
    second = client.post(f"/api/outlines/{outline_id}/approve")

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["task_id"] == second.json()["task_id"]
    assert len(app.state.task_manager.list_tasks()) == 1


def test_manuscript_local_images_are_served_from_review_workspace(
    tmp_path: Path,
) -> None:
    app = _create_test_app(tmp_path)
    client = TestClient(app)
    outline_id = client.post(
        "/api/outlines",
        json={"topic": "视觉内容", "page_count": 5},
    ).json()["outline_id"]
    draft = app.state.outline_service._drafts[outline_id]
    manuscript_path = Path(draft.manuscript_path)
    image_path = manuscript_path.parent / "chart.png"
    image_path.write_bytes(b"png")
    manuscript_path.write_text(
        "# 视觉内容\n\n![趋势图](chart.png)",
        encoding="utf-8",
    )

    body = client.get(f"/api/outlines/{outline_id}").json()
    image_url = f"/api/outlines/{outline_id}/assets/chart.png"

    assert image_url in body["manuscript"]
    assert client.get(image_url).content == b"png"


def test_unknown_outline_returns_404(tmp_path: Path) -> None:
    app = _create_test_app(tmp_path)
    client = TestClient(app)

    assert client.get("/api/outlines/missing").status_code == 404
    assert client.post("/api/outlines/missing/approve").status_code == 404


def test_outline_events_endpoint_replays_persisted_statuses(tmp_path: Path) -> None:
    app = _create_test_app(tmp_path)
    client = TestClient(app)
    outline = client.post(
        "/api/outlines",
        json={"topic": "事件流", "page_count": 5},
    ).json()
    bus = app.state.task_manager.get_event_bus(outline["outline_id"])
    assert bus is not None

    import asyncio

    asyncio.run(bus.close())
    response = client.get(
        f"/api/outlines/{outline['outline_id']}/events",
        params={"last_seq": 0},
    )

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert '"type": "outline.generating"' in response.text
    assert '"type": "outline.ready"' in response.text


def test_restart_failure_is_pushed_to_reconnecting_outline_stream(
    tmp_path: Path,
) -> None:
    outline_id = "deadbeef"
    workspace = task_dir(tmp_path, outline_id)
    workspace.mkdir(parents=True)
    draft = OutlineDraftRecord(
        outline_id=outline_id,
        status=OutlineStatus.GENERATING,
        topic="中断恢复",
        requested_page_count=5,
        ratio="16:9",
        language="zh",
        created_at="2026-01-01T00:00:00+00:00",
        updated_at="2026-01-01T00:00:00+00:00",
    )
    (workspace / "outline.json").write_text(
        draft.model_dump_json(indent=2),
        encoding="utf-8",
    )
    app = _create_test_app(tmp_path)

    with TestClient(app) as client:
        body = client.get(f"/api/outlines/{outline_id}").json()
        bus = app.state.task_manager.get_event_bus(outline_id)

    assert body["status"] == "failed"
    assert bus is not None
    assert any(
        event["type"] == "outline.failed"
        for event in bus._replay(0)
    )


def test_outline_research_requires_explicit_feedback() -> None:
    assert not OutlineService._requests_new_research(None)
    assert not OutlineService._requests_new_research("调整结构，不要重新调研")
    assert OutlineService._requests_new_research("请补充最新数据并标注来源")
    assert OutlineService._requests_new_research("Please research new market evidence")
