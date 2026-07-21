"""FastAPI routes for the conversational editing & version-management API (D).

Mount ``edit_router`` under your app (e.g. ``app.include_router(edit_router)``).
The routes follow the plan's suggested API exactly::

    POST /api/tasks/{task_id}/slides/{slide_id}/chat
    GET  /api/tasks/{task_id}/slides/{slide_id}/revisions
    POST /api/tasks/{task_id}/slides/{slide_id}/revisions/{revision}/apply
    POST /api/tasks/{task_id}/slides/{slide_id}/undo
    POST /api/tasks/{task_id}/slides/{slide_id}/retry

An additional SSE endpoint streams edit events for live preview updates.

Services are resolved from the shared :data:`DEFAULT_REGISTRY`; the generation
pipeline / integration layer is responsible for registering a
:class:`~pptagent.editor.service.SlideEditService` for each slide.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from pptagent.editor.event_bus import get_default_bus
from pptagent.editor.service import DEFAULT_REGISTRY, EditServiceRegistry, SlideEditService

edit_router = APIRouter()


# ── request / response models ──────────────────────────────────


class ChatRequest(BaseModel):
    instruction: str = ""
    text: str = ""
    element_id: int | None = None

    def model_post_init(self, __context) -> None:
        # Accept "text" from the frontend as an alias for "instruction".
        if not self.instruction and self.text:
            self.instruction = self.text


class RetryRequest(BaseModel):
    pass


# ── helpers ────────────────────────────────────────────────────

# Per-task store for the C-group EventBus, so edit routes can publish
# events that reach the frontend SSE connection.
_TASK_EVENT_BUSES: dict[str, Any] = {}


def set_task_event_bus(task_id: str, bus: Any) -> None:
    _TASK_EVENT_BUSES[task_id] = bus


def _emit_deeppresenter_event(
    task_id: str, slide_id: str, event_type: str,
    message: str = "", payload: dict | None = None,
    artifact_url: str | None = None,
) -> None:
    """Publish an edit event through the C-group EventBus so the frontend
    SSE stream receives it."""
    bus = _TASK_EVENT_BUSES.get(task_id)
    if bus is None:
        return
    try:
        from deeppresenter.server.models.events import GenerationEvent, EventType
        event = GenerationEvent(
            task_id=task_id,
            seq=1,  # Overridden by EventBus.publish
            type=EventType(event_type),
            slide_id=slide_id,
            message=message,
            payload=payload or {},
        )
        if artifact_url:
            event.artifact_url = artifact_url
        import asyncio
        asyncio.ensure_future(bus.publish(event))
    except Exception:
        pass


def _get_service(task_id: str, slide_id: str) -> SlideEditService:
    svc = DEFAULT_REGISTRY.get(task_id, slide_id)
    if svc is None:
        raise HTTPException(
            status_code=404,
            detail=f"未找到任务 {task_id} 的页面 {slide_id} 编辑器，请先完成该页生成并注册编辑器。",
        )
    return svc


# ── routes ─────────────────────────────────────────────────────


@edit_router.post("/api/tasks/{task_id}/slides/{slide_id}/chat")
async def chat(task_id: str, slide_id: str, body: ChatRequest):
    """Send a page-level natural-language edit instruction."""
    import uuid as _uuid

    svc = _get_service(task_id, slide_id)
    chat_id = f"c{_uuid.uuid4().hex[:10]}"

    # Publish edit.started through the C-group event bus if available
    _emit_deeppresenter_event(task_id, slide_id, "edit.started",
        message=f"正在处理：{body.instruction[:60]}",
        payload={"chat_id": chat_id})

    try:
        result = await svc.chat(body.instruction, element_id=body.element_id)
    except HTTPException:
        raise
    except Exception as e:
        _emit_deeppresenter_event(task_id, slide_id, "edit.failed",
            message=f"修改失败: {e}",
            payload={"chat_id": chat_id})
        raise HTTPException(status_code=502, detail=str(e))

    d = result.to_dict()
    d["chat_id"] = chat_id

    if result.status == "success" or result.status == "no_change":
        _emit_deeppresenter_event(task_id, slide_id,
            "edit.applied" if result.status == "success" else "edit.applied",
            message=result.message,
            payload={"chat_id": chat_id, "revision": result.revision},
            artifact_url=result.preview_path)
    else:
        _emit_deeppresenter_event(task_id, slide_id, "edit.failed",
            message=result.message or "修改失败",
            payload={"chat_id": chat_id})

    return d


@edit_router.get("/api/tasks/{task_id}/slides/{slide_id}/revisions")
async def revisions(task_id: str, slide_id: str):
    """Return revision list as a flat array (frontend-compatible)."""
    svc = _get_service(task_id, slide_id)
    raw = svc.get_revisions()
    # Transform to SlideRevision[] format expected by frontend
    revs = raw.get("revisions", []) if isinstance(raw, dict) else []
    result = []
    for r in revs:
        if isinstance(r, dict):
            result.append({
                "revision": r.get("number", 0),
                "label": r.get("instruction", f"版本 {r.get('number', 0)}")[:100],
                "created_at": r.get("created_at", ""),
                "preview_url": r.get("preview_url"),
            })
    return result


@edit_router.post("/api/tasks/{task_id}/slides/{slide_id}/revisions/{revision}/apply")
async def apply_revision(task_id: str, slide_id: str, revision: int):
    """Switch the page to a specific historical revision."""
    svc = _get_service(task_id, slide_id)
    result = await svc.apply_revision(revision)
    d = result.to_dict()
    _emit_deeppresenter_event(task_id, slide_id, "edit.reverted",
        message=f"已切换到版本 {revision}",
        payload={"revision": revision},
        artifact_url=result.preview_path)
    return d


@edit_router.post("/api/tasks/{task_id}/slides/{slide_id}/undo")
async def undo(task_id: str, slide_id: str):
    """Undo the last edit (pointer move + replay, no model call)."""
    svc = _get_service(task_id, slide_id)
    result = await svc.undo()
    d = result.to_dict()
    _emit_deeppresenter_event(task_id, slide_id, "edit.reverted",
        message=result.message,
        payload={"revision": result.revision},
        artifact_url=result.preview_path)
    return d


@edit_router.post("/api/tasks/{task_id}/slides/{slide_id}/retry")
async def retry(task_id: str, slide_id: str):
    """Retry the last failed modification."""
    svc = _get_service(task_id, slide_id)
    result = await svc.retry()
    d = result.to_dict()
    if result.status == "success":
        _emit_deeppresenter_event(task_id, slide_id, "edit.applied",
            message=result.message,
            payload={"revision": result.revision})
    return d


@edit_router.get("/api/tasks/{task_id}/slides/{slide_id}/events")
async def events(task_id: str, slide_id: str):
    """Server-Sent-Events stream of edit events for this page."""

    bus = get_default_bus()

    async def event_stream():
        async for event in bus.subscribe(task_id=task_id, slide_id=slide_id):
            yield f"data: {__import__('json').dumps(event.to_dict(), ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


__all__ = ["edit_router", "DEFAULT_REGISTRY", "EditServiceRegistry", "ChatRequest"]
