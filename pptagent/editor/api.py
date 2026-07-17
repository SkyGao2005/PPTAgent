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

from pptagent.editor.events import get_default_bus
from pptagent.editor.service import DEFAULT_REGISTRY, EditServiceRegistry, SlideEditService

edit_router = APIRouter()


# ── request / response models ──────────────────────────────────


class ChatRequest(BaseModel):
    instruction: str
    element_id: int | None = None


class RetryRequest(BaseModel):
    pass


# ── helpers ────────────────────────────────────────────────────


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
    svc = _get_service(task_id, slide_id)
    result = await svc.chat(body.instruction, element_id=body.element_id)
    return result.to_dict()


@edit_router.get("/api/tasks/{task_id}/slides/{slide_id}/revisions")
async def revisions(task_id: str, slide_id: str):
    """List all kept revisions and the pruned-revision log."""
    svc = _get_service(task_id, slide_id)
    return svc.get_revisions()


@edit_router.post("/api/tasks/{task_id}/slides/{slide_id}/revisions/{revision}/apply")
async def apply_revision(task_id: str, slide_id: str, revision: int):
    """Switch the page to a specific historical revision."""
    svc = _get_service(task_id, slide_id)
    return (await svc.apply_revision(revision)).to_dict()


@edit_router.post("/api/tasks/{task_id}/slides/{slide_id}/undo")
async def undo(task_id: str, slide_id: str):
    """Undo the last edit (pointer move + replay, no model call)."""
    svc = _get_service(task_id, slide_id)
    return (await svc.undo()).to_dict()


@edit_router.post("/api/tasks/{task_id}/slides/{slide_id}/retry")
async def retry(task_id: str, slide_id: str):
    """Retry the last failed modification."""
    svc = _get_service(task_id, slide_id)
    return (await svc.retry()).to_dict()


@edit_router.get("/api/tasks/{task_id}/slides/{slide_id}/events")
async def events(task_id: str, slide_id: str):
    """Server-Sent-Events stream of edit events for this page."""

    bus = get_default_bus()

    async def event_stream():
        async for event in bus.subscribe(task_id=task_id, slide_id=slide_id):
            yield f"data: {__import__('json').dumps(event.to_dict(), ensure_ascii=False)}\n\n"

    return StreamingResponse(event_stream(), media_type="text/event-stream")


__all__ = ["edit_router", "DEFAULT_REGISTRY", "EditServiceRegistry", "ChatRequest"]
