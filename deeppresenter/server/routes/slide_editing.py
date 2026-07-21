from __future__ import annotations
"""Front-end compatible slide editing adapter.

This route layer bridges the workbench API contract to D group's
``pptagent.editor`` services while keeping all edit events on the existing
task-level SSE stream.
"""

import asyncio
import uuid
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request, status
from pydantic import BaseModel, Field

from deeppresenter.server.models.artifacts import task_dir
from deeppresenter.server.models.events import EventType, StageName
from deeppresenter.server.services.slide_editing import (
    DeepPresenterLLMAdapter,
    HtmlSlideEditService,
    PptxSlideEditService,
)
from deeppresenter.server.services.preview import artifact_url
from deeppresenter.server.services.task_manager import TaskManager
from pptagent.editor.artifact import ArtifactStore
from pptagent.editor.service import DEFAULT_REGISTRY, SlideEditService

router = APIRouter(prefix="/api/tasks")


class ChatRequest(BaseModel):
    """Workbench chat payload.

    The current front-end sends ``text``. D group's router uses
    ``instruction``. Accept both during integration.
    """

    text: str | None = Field(default=None)
    instruction: str | None = Field(default=None)
    element_id: int | None = Field(default=None)

    @property
    def normalized_text(self) -> str:
        return (self.text or self.instruction or "").strip()


def _get_manager(request: Request) -> TaskManager:
    manager = request.app.state.task_manager
    if manager is None:
        raise HTTPException(status_code=500, detail="TaskManager 未初始化")
    return manager


def _require_task(manager: TaskManager, task_id: str):
    snapshot = manager.get_snapshot(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
    return snapshot


def _slide_index(manager: TaskManager, task_id: str, slide_id: str) -> int | None:
    service = manager.get_preview_service(task_id)
    if service is None:
        return None
    slide = service.get_slide(task_id, slide_id)
    return slide.index if slide else None


def _preview_url(manager: TaskManager, task_id: str, preview_path: str | None) -> str | None:
    if not preview_path:
        return None
    if preview_path.startswith("/api/"):
        return preview_path

    path = Path(preview_path)
    if not path.is_absolute():
        return artifact_url(task_id, preview_path)

    root = task_dir(manager.workspace_base, task_id).resolve()
    try:
        rel = path.resolve().relative_to(root)
    except ValueError:
        return None
    return artifact_url(task_id, str(rel))


async def _emit_edit_event(
    manager: TaskManager,
    task_id: str,
    slide_id: str,
    event_type: EventType,
    *,
    message: str,
    chat_id: str | None = None,
    action: str | None = None,
    revision: int | None = None,
    artifact: str | None = None,
    extra: dict[str, Any] | None = None,
) -> None:
    reporter = manager.get_event_reporter(task_id)
    if reporter is None:
        return

    payload: dict[str, Any] = {}
    if chat_id is not None:
        payload["chat_id"] = chat_id
    if action is not None:
        payload["action"] = action
    if revision is not None:
        payload["revision"] = revision
    if extra:
        payload.update(extra)

    await reporter.emit(
        event_type,
        stage=StageName.EDIT,
        slide_id=slide_id,
        slide_index=_slide_index(manager, task_id, slide_id),
        artifact_url=artifact,
        message=message,
        payload=payload,
    )


_HTML_SERVICES: dict[tuple[int, str, str], HtmlSlideEditService] = {}
_PPTX_PRESENTATIONS: dict[tuple[int, str], Any] = {}
_PPTX_SERVICES: dict[tuple[int, str, str], PptxSlideEditService] = {}


def _service(manager: TaskManager, task_id: str, slide_id: str):
    svc = DEFAULT_REGISTRY.get(task_id, slide_id)
    if svc is not None:
        return svc

    pptx_svc = _pptx_service(manager, task_id, slide_id)
    if pptx_svc is not None:
        return pptx_svc

    preview_service = manager.get_preview_service(task_id)
    if preview_service is None or preview_service.get_slide(task_id, slide_id) is None:
        return None

    key = (id(manager), task_id, slide_id)
    html_svc = _HTML_SERVICES.get(key)
    if html_svc is None:
        html_svc = HtmlSlideEditService(
            manager.workspace_base,
            preview_service,
            task_id,
            slide_id,
        )
        _HTML_SERVICES[key] = html_svc
    return html_svc


def _pptx_service(manager: TaskManager, task_id: str, slide_id: str):
    preview_service = manager.get_preview_service(task_id)
    slide_artifact = preview_service.get_slide(task_id, slide_id) if preview_service else None
    if slide_artifact is None:
        return None

    key = (id(manager), task_id, slide_id)
    existing = _PPTX_SERVICES.get(key)
    if existing is not None:
        return existing

    pptx_path = _task_pptx_path(manager, task_id)
    if pptx_path is None:
        return None

    presentation_key = (id(manager), task_id)
    presentation = _PPTX_PRESENTATIONS.get(presentation_key)
    if presentation is None:
        try:
            from pptagent.presentation import Presentation
            from pptagent.utils import Config

            presentation = Presentation.from_file(str(pptx_path), Config(str(task_dir(manager.workspace_base, task_id) / ".pptagent-edit")))
        except Exception:
            return None
        _PPTX_PRESENTATIONS[presentation_key] = presentation

    slide_index = slide_artifact.index
    if slide_index < 1 or slide_index > len(presentation.slides):
        return None
    try:
        from deeppresenter.utils.config import DeepPresenterConfig

        config = DeepPresenterConfig.load_from_file(manager.config_path)
        llm = DeepPresenterLLMAdapter(config.research_agent)
        inner = SlideEditService(
            slide=presentation.slides[slide_index - 1],
            presentation=presentation,
            llm=llm,
            task_id=task_id,
            slide_id=slide_id,
            mode="pptx",
            source_path=str(pptx_path),
            store=ArtifactStore(manager.workspace_base, task_id),
        )
    except Exception:
        return None

    wrapped = PptxSlideEditService(
        inner,
        presentation,
        preview_service,
        manager.workspace_base,
        task_id,
        slide_id,
        slide_index,
        pptx_path,
    )
    _PPTX_SERVICES[key] = wrapped
    return wrapped


def _task_pptx_path(manager: TaskManager, task_id: str) -> Path | None:
    snapshot = manager.get_snapshot(task_id)
    root = task_dir(manager.workspace_base, task_id)
    candidates: list[Path] = []
    if snapshot and snapshot.result_artifact:
        candidates.append(root / snapshot.result_artifact)
    candidates.extend([root / "manuscript.pptx", root / "exports" / "latest.pptx"])
    candidates.extend(sorted(root.glob("*.pptx")))
    candidates.extend(sorted((root / "exports").glob("*.pptx")) if (root / "exports").exists() else [])
    for path in candidates:
        if path.exists() and path.suffix.lower() == ".pptx":
            return path
    return None


async def _run_chat_edit(
    manager: TaskManager,
    task_id: str,
    slide_id: str,
    svc: SlideEditService,
    *,
    chat_id: str,
    instruction: str,
    element_id: int | None,
) -> None:
    await _emit_edit_event(
        manager,
        task_id,
        slide_id,
        EventType.EDIT_STARTED,
        message=instruction,
        chat_id=chat_id,
        action=instruction,
    )
    try:
        result = await svc.chat(instruction, element_id=element_id)
    except Exception as exc:  # noqa: BLE001
        await _emit_edit_event(
            manager,
            task_id,
            slide_id,
            EventType.EDIT_FAILED,
            message=f"修改失败：{exc}",
            chat_id=chat_id,
            action=instruction,
            extra={"error": str(exc)},
        )
        return

    artifact = _preview_url(manager, task_id, result.preview_path)
    if result.status in {"failed", "noop"}:
        await _emit_edit_event(
            manager,
            task_id,
            slide_id,
            EventType.EDIT_FAILED,
            message=result.message,
            chat_id=chat_id,
            action=instruction,
            revision=result.revision,
            artifact=artifact,
            extra={"error": result.error, "status": result.status},
        )
        return

    await _emit_edit_event(
        manager,
        task_id,
        slide_id,
        EventType.EDIT_APPLIED,
        message=result.message,
        chat_id=chat_id,
        action=instruction,
        revision=result.revision,
        artifact=artifact,
        extra={"status": result.status, "diff": result.diff},
    )


async def _emit_missing_service(
    manager: TaskManager,
    task_id: str,
    slide_id: str,
    *,
    chat_id: str | None,
    action: str,
) -> None:
    await _emit_edit_event(
        manager,
        task_id,
        slide_id,
        EventType.EDIT_FAILED,
        message="该页面尚未注册 D 组编辑服务，请先接入生成后的 SlideEditService 注册逻辑。",
        chat_id=chat_id,
        action=action,
        extra={"error": "edit_service_not_registered"},
    )


def _map_revision(manager: TaskManager, task_id: str, item: dict[str, Any]) -> dict[str, Any]:
    revision = item.get("revision", item.get("number", 0))
    label = item.get("label") or item.get("instruction") or item.get("message") or f"版本 {revision}"
    return {
        "revision": revision,
        "label": label,
        "created_at": item.get("created_at") or "",
        "preview_url": _preview_url(manager, task_id, item.get("preview_url") or item.get("preview_path")),
    }


@router.post("/{task_id}/slides/{slide_id}/chat", status_code=status.HTTP_202_ACCEPTED)
async def chat(task_id: str, slide_id: str, body: ChatRequest, request: Request):
    manager = _get_manager(request)
    _require_task(manager, task_id)
    instruction = body.normalized_text
    if not instruction:
        raise HTTPException(status_code=422, detail="text 或 instruction 不能为空")

    chat_id = uuid.uuid4().hex[:12]
    svc = _service(manager, task_id, slide_id)
    if svc is None:
        await _emit_missing_service(
            manager,
            task_id,
            slide_id,
            chat_id=chat_id,
            action=instruction,
        )
        return {"chat_id": chat_id, "status": "failed"}

    runner = _run_chat_edit(
        manager,
        task_id,
        slide_id,
        svc,
        chat_id=chat_id,
        instruction=instruction,
        element_id=body.element_id,
    )
    if isinstance(svc, HtmlSlideEditService):
        await runner
    else:
        asyncio.create_task(runner)
    return {"chat_id": chat_id, "status": "queued"}


@router.get("/{task_id}/slides/{slide_id}/revisions")
async def revisions(task_id: str, slide_id: str, request: Request):
    manager = _get_manager(request)
    _require_task(manager, task_id)
    svc = _service(manager, task_id, slide_id)
    if svc is not None:
        return [_map_revision(manager, task_id, item) for item in svc.get_revisions().get("revisions", [])]

    service = manager.get_preview_service(task_id)
    slide = service.get_slide(task_id, slide_id) if service else None
    if slide is None:
        raise HTTPException(status_code=404, detail=f"页面 {slide_id} 不存在")
    return [
        {
            "revision": slide.revision or 1,
            "label": "初始版本",
            "created_at": slide.created_at,
            "preview_url": artifact_url(task_id, slide.preview_path) if slide.preview_path else None,
        }
    ]


@router.post("/{task_id}/slides/{slide_id}/revisions/{revision}/apply", status_code=status.HTTP_202_ACCEPTED)
async def apply_revision(task_id: str, slide_id: str, revision: int, request: Request):
    manager = _get_manager(request)
    _require_task(manager, task_id)
    svc = _service(manager, task_id, slide_id)
    if svc is None:
        await _emit_missing_service(
            manager,
            task_id,
            slide_id,
            chat_id=None,
            action=f"切换到版本 {revision}",
        )
        return {"status": "failed"}

    async def _run() -> None:
        result = await svc.apply_revision(revision)
        event_type = EventType.EDIT_REVERTED if result.status == "success" else EventType.EDIT_FAILED
        await _emit_edit_event(
            manager,
            task_id,
            slide_id,
            event_type,
            message=result.message,
            action=f"切换到版本 {revision}",
            revision=result.revision,
            artifact=_preview_url(manager, task_id, result.preview_path),
            extra={"error": result.error} if result.error else None,
        )

    if isinstance(svc, HtmlSlideEditService):
        await _run()
    else:
        asyncio.create_task(_run())
    return {"status": "queued"}


@router.post("/{task_id}/slides/{slide_id}/undo", status_code=status.HTTP_202_ACCEPTED)
async def undo(task_id: str, slide_id: str, request: Request):
    manager = _get_manager(request)
    _require_task(manager, task_id)
    svc = _service(manager, task_id, slide_id)
    if svc is None:
        await _emit_missing_service(
            manager,
            task_id,
            slide_id,
            chat_id=None,
            action="撤销修改",
        )
        return {"status": "failed"}

    async def _run() -> None:
        result = await svc.undo()
        event_type = EventType.EDIT_REVERTED if result.status == "success" else EventType.EDIT_FAILED
        await _emit_edit_event(
            manager,
            task_id,
            slide_id,
            event_type,
            message=result.message,
            action="撤销修改",
            revision=result.revision,
            artifact=_preview_url(manager, task_id, result.preview_path),
            extra={"error": result.error} if result.error else None,
        )

    if isinstance(svc, HtmlSlideEditService):
        await _run()
    else:
        asyncio.create_task(_run())
    return {"status": "queued"}


@router.post("/{task_id}/slides/{slide_id}/retry", status_code=status.HTTP_202_ACCEPTED)
async def retry(task_id: str, slide_id: str, request: Request):
    manager = _get_manager(request)
    _require_task(manager, task_id)
    svc = _service(manager, task_id, slide_id)
    if svc is None:
        await _emit_missing_service(
            manager,
            task_id,
            slide_id,
            chat_id=None,
            action="重试修改",
        )
        return {"status": "failed"}

    runner = _run_chat_edit(
        manager,
        task_id,
        slide_id,
        svc,
        chat_id=uuid.uuid4().hex[:12],
        instruction="重试修改",
        element_id=None,
    )
    if isinstance(svc, HtmlSlideEditService):
        await runner
    else:
        asyncio.create_task(runner)
    return {"status": "queued"}
