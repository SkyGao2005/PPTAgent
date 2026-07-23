"""任务相关 FastAPI 路由 —— 创建、查询、取消、SSE 订阅。

所有路由均挂在 ``/api/tasks`` 下。
"""

from __future__ import annotations

import json
import asyncio
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from deeppresenter.server.models.artifacts import SlideArtifact, is_path_safe, task_dir
from deeppresenter.server.models.templates import TemplateStatus
from deeppresenter.server.routes.attachments import resolve_attachment_paths
from deeppresenter.server.services.preview import artifact_url
from deeppresenter.server.services.task_manager import TaskManager, TaskSnapshot
from deeppresenter.server.services.template_catalog import is_bundled_template
from deeppresenter.server.services.template_catalog import bundled_template_summary
from deeppresenter.templates.store import (
    LegacyTemplateError,
    TemplateAspectRatioMismatchError,
    TemplateNotFoundError,
    TemplateRevisionNotReadyError,
)

TASK_HEARTBEAT_INTERVAL = 30

router = APIRouter(prefix="/api/tasks")

# ── 请求/响应模型 ────────────────────────────────────────────


class CreateTaskRequest(BaseModel):
    """创建任务请求体。"""

    instruction: Optional[str] = Field(default=None, description="PPT 主题/要求")
    topic: Optional[str] = Field(default=None, description="前端工作台主题字段")
    attachments: list[str] = Field(default_factory=list, description="附件文件路径列表")
    attachment_ids: list[str] = Field(
        default_factory=list, description="前端工作台附件 ID 字段"
    )
    num_pages: Optional[str] = Field(default=None, description="页数，如 '8' 或 '5-10'")
    page_count: Optional[int] = Field(default=None, description="前端工作台页数字段")
    powerpoint_type: Optional[str] = Field(default=None, description="画面比例")
    ratio: Optional[str] = Field(default=None, description="前端工作台画面比例字段")
    template: Optional[str] = Field(default=None, description="模板 ID")
    template_id: Optional[str] = Field(
        default=None, description="前端工作台模板 ID 字段"
    )
    template_revision_id: Optional[str] = Field(
        default=None,
        description="可选的 Template IR revision；未提供时固定当前 active revision",
    )
    convert_type: Optional[str] = Field(
        default=None, description="转换模式；仅支持 deeppresenter"
    )
    enable_planner: bool = Field(default=False, description="是否启用大纲规划")
    language: str = Field(default="en", description="语言 en/zh")


class CreateTaskResponse(BaseModel):
    """创建任务响应体。"""

    task_id: str
    status: str
    created_at: str


class TaskHistoryItem(BaseModel):
    """主页“最近对话”所需的轻量任务摘要。"""

    task_id: str
    topic: str
    instruction: str
    status: str
    stage: Optional[str] = None
    progress: float
    template_id: str
    ratio: str
    total_slides: int
    completed_slides: int
    failed_slides: int
    preview_url: Optional[str] = None
    created_at: str
    updated_at: str


class TaskHistoryResponse(BaseModel):
    """按最近更新时间倒序返回的任务历史。"""

    tasks: list[TaskHistoryItem]
    total: int


class ErrorResponse(BaseModel):
    """通用错误响应。"""

    error: dict


# ── 依赖注入：从 app.state 获取 TaskManager ──────────────────


def _get_manager(request: Request) -> TaskManager:
    """从 FastAPI app state 中获取 TaskManager 实例。"""
    manager = request.app.state.task_manager
    if manager is None:
        raise HTTPException(status_code=500, detail="TaskManager 未初始化")
    return manager


def _resolve_template(
    body: CreateTaskRequest, request: Request
) -> tuple[str | None, str | None, str | None]:
    """Resolve a frontend template id against templates owned by the backend."""
    if body.template:
        summary = bundled_template_summary(body.template)
        return (
            body.template,
            body.template_id or body.template,
            str(summary["ratio"]) if summary is not None else None,
        )
    if not body.template_id:
        return None, None, None
    if is_bundled_template(body.template_id):
        summary = bundled_template_summary(body.template_id)
        assert summary is not None
        return body.template_id, body.template_id, str(summary["ratio"])

    registry = request.app.state.template_registry
    manifest = registry.get(body.template_id)
    if manifest is None:
        raise HTTPException(status_code=422, detail=f"模板 {body.template_id} 不存在")
    if manifest.status != TemplateStatus.READY:
        raise HTTPException(
            status_code=409,
            detail=f"模板 {body.template_id} 尚未解析完成",
        )
    return body.template_id, body.template_id, manifest.aspect_ratio


def _require_task(manager: TaskManager, task_id: str):
    snapshot = manager.get_snapshot(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
    return snapshot


def _slide_summary(task_id: str, slide: SlideArtifact) -> dict:
    title = slide.structured_data.title or f"第 {slide.index} 页"
    summary = slide.structured_data.subtitle or (
        " / ".join(slide.structured_data.body[:2]) if slide.structured_data.body else ""
    )
    return {
        "slide_id": slide.slide_id,
        "task_id": task_id,
        "index": slide.index,
        "title": title,
        "summary": summary,
        "status": slide.status,
        "mode": slide.mode,
        "layout_name": slide.layout_name,
        "revision": slide.revision,
        "current_revision": slide.revision,
        "preview_url": (
            artifact_url(task_id, slide.preview_path) if slide.preview_path else None
        ),
        "created_at": slide.created_at,
        "updated_at": slide.updated_at,
    }


def _slide_detail(manager: TaskManager, task_id: str, slide: SlideArtifact) -> dict:
    service = manager.get_preview_service(task_id)
    revision_count = service.revision_count(task_id, slide.slide_id) if service else 0
    return {
        **_slide_summary(task_id, slide),
        "structured_data": slide.structured_data.model_dump(),
        "source_path": slide.source_path,
        "revision_count": revision_count,
    }


def _last_seq(manager: TaskManager, task_id: str) -> int:
    bus = manager.get_event_bus(task_id)
    return bus.seq if bus else 0


def _task_response(manager: TaskManager, snapshot) -> dict:
    d = snapshot.to_dict()
    # 确保 status 是字符串
    if hasattr(d["status"], "value"):
        d["status"] = d["status"].value

    params = d.get("generation_params") or {}
    status = d.get("status")
    stage = d.get("current_stage")
    if stage is None:
        stage = "export" if status == "completed" else "generate"

    d.update(
        {
            "topic": d.get("instruction", ""),
            "stage": stage,
            "template_id": params.get("template_id") or params.get("template") or "",
            "ratio": params.get("powerpoint_type", "16:9"),
            "last_seq": _last_seq(manager, snapshot.task_id),
        }
    )

    service = manager.get_preview_service(snapshot.task_id)
    d["slides"] = (
        [
            _slide_summary(snapshot.task_id, slide)
            for slide in service.list_slides(snapshot.task_id)
        ]
        if service
        else []
    )
    return d


def _requested_slide_count(snapshot: TaskSnapshot) -> int:
    """Return the configured page count while a task has no generated slides yet."""
    value = snapshot.generation_params.get("num_pages")
    if isinstance(value, int):
        return max(value, 0)
    if not isinstance(value, str):
        return 0
    first = value.split("-", 1)[0].strip()
    return int(first) if first.isdigit() else 0


def _task_history_item(
    manager: TaskManager,
    snapshot: TaskSnapshot,
) -> TaskHistoryItem:
    """Project a full task snapshot into the homepage history contract."""
    params = snapshot.generation_params
    status = (
        snapshot.status.value
        if hasattr(snapshot.status, "value")
        else str(snapshot.status)
    )
    stage = (
        snapshot.current_stage.value
        if hasattr(snapshot.current_stage, "value")
        else snapshot.current_stage
    )
    total_slides = snapshot.total_slides or _requested_slide_count(snapshot)

    preview_url = None
    service = manager.get_preview_service(snapshot.task_id)
    if service is not None:
        first_preview = next(
            (
                slide
                for slide in service.list_slides(snapshot.task_id)
                if slide.preview_path
            ),
            None,
        )
        if first_preview is not None:
            preview_url = artifact_url(snapshot.task_id, first_preview.preview_path)

    return TaskHistoryItem(
        task_id=snapshot.task_id,
        topic=snapshot.instruction.strip() or "未命名演示",
        instruction=snapshot.instruction,
        status=status,
        stage=stage,
        progress=snapshot.progress,
        template_id=params.get("template_id") or params.get("template") or "",
        ratio=params.get("powerpoint_type") or "16:9",
        total_slides=total_slides,
        completed_slides=snapshot.completed_slides,
        failed_slides=snapshot.failed_slides,
        preview_url=preview_url,
        created_at=snapshot.created_at,
        updated_at=snapshot.updated_at,
    )


def _task_heartbeat(task_id: str, seq: int) -> dict:
    return {
        "task_id": task_id,
        "seq": seq,
        "type": "heartbeat",
        "stage": None,
        "status": None,
        "progress": None,
        "message": "",
        "slide_id": None,
        "slide_index": None,
        "total_slides": None,
        "artifact_url": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "payload": {},
    }


# ── 路由 ──────────────────────────────────────────────────────


@router.post("", status_code=201)
async def create_task(
    body: CreateTaskRequest,
    request: Request,
):
    """创建新任务，返回 task_id。"""
    manager = _get_manager(request)
    instruction = (body.instruction or body.topic or "").strip()
    if not instruction:
        raise HTTPException(status_code=422, detail="instruction 或 topic 不能为空")
    if body.convert_type not in (None, "deeppresenter"):
        raise HTTPException(
            status_code=422,
            detail=(
                "PPTAgent 排版引擎已从 DeepPresenter 生成链移除；"
                "请先编译 Template IR，再使用 deeppresenter 模式生成"
            ),
        )
    num_pages = body.num_pages or (str(body.page_count) if body.page_count else None)
    attachments = (
        body.attachments
        if body.attachments
        else resolve_attachment_paths(manager.workspace_base, body.attachment_ids)
    )
    language = "zh" if body.language.lower().startswith("zh") else body.language
    template, template_id, template_ratio = _resolve_template(body, request)
    requested_ratio = body.ratio or body.powerpoint_type
    if template_ratio in {"16:9", "4:3"}:
        if requested_ratio is not None and requested_ratio != template_ratio:
            raise HTTPException(
                status_code=422,
                detail=(
                    f"模板 {template_id} 的画面比例为 {template_ratio}，"
                    f"不能按 {requested_ratio} 生成"
                ),
            )
        powerpoint_type = template_ratio
    else:
        powerpoint_type = requested_ratio or "16:9"
    try:
        task_id = await manager.create(
            instruction=instruction,
            attachments=attachments,
            num_pages=num_pages,
            powerpoint_type=powerpoint_type,
            template=template,
            template_id=template_id,
            template_revision_id=body.template_revision_id,
            convert_type=body.convert_type,
            enable_planner=body.enable_planner,
            language=language,
        )
    except TemplateRevisionNotReadyError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except (
        LegacyTemplateError,
        TemplateAspectRatioMismatchError,
        TemplateNotFoundError,
    ) as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    snapshot = manager.get_snapshot(task_id)
    if snapshot is None:
        return CreateTaskResponse(task_id=task_id, status="queued", created_at="")
    return _task_response(manager, snapshot)


@router.get("", response_model=TaskHistoryResponse)
async def list_tasks(
    request: Request,
    limit: Optional[int] = Query(
        default=None,
        ge=1,
        le=50,
        description="最多返回的最近任务数；省略时返回全部",
    ),
) -> TaskHistoryResponse:
    """列出最近任务，供主页恢复历史对话。"""
    manager = _get_manager(request)
    tasks = sorted(
        manager.list_tasks(),
        key=lambda task: (task.updated_at or task.created_at, task.created_at),
        reverse=True,
    )
    return TaskHistoryResponse(
        tasks=[
            _task_history_item(manager, task)
            for task in (tasks[:limit] if limit is not None else tasks)
        ],
        total=len(tasks),
    )


@router.get("/{task_id}")
async def get_task(task_id: str, request: Request):
    """获取任务快照。"""
    manager = _get_manager(request)
    snapshot = manager.get_snapshot(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
    return _task_response(manager, snapshot)


@router.get("/{task_id}/events")
async def subscribe_events(
    task_id: str,
    request: Request,
    last_seq: int = Query(default=0, ge=0, description="客户端最后收到的 seq"),
):
    """SSE 订阅任务事件流。

    先回放 seq > last_seq 的历史事件，再推送实时事件。
    """
    manager = _get_manager(request)
    bus = manager.get_event_bus(task_id)
    if bus is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在或已关闭")

    async def _event_stream():
        async for event_dict in bus.subscribe(last_seq=last_seq):
            if event_dict.get("type") == "eof":
                while True:
                    await asyncio.sleep(TASK_HEARTBEAT_INTERVAL)
                    yield f"data: {json.dumps(_task_heartbeat(task_id, bus.seq), ensure_ascii=False)}\n\n"
            yield f"data: {json.dumps(event_dict, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        _event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/{task_id}/slides")
async def list_slides(task_id: str, request: Request):
    """获取任务当前所有页的预览元数据。"""
    manager = _get_manager(request)
    _require_task(manager, task_id)
    service = manager.get_preview_service(task_id)
    if service is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 预览服务不存在")

    slides = service.list_slides(task_id)
    return [_slide_summary(task_id, slide) for slide in slides]


@router.get("/{task_id}/slides/{slide_id}")
async def get_slide(task_id: str, slide_id: str, request: Request):
    """获取单页当前预览元数据。"""
    manager = _get_manager(request)
    _require_task(manager, task_id)
    service = manager.get_preview_service(task_id)
    if service is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 预览服务不存在")

    slide = service.get_slide(task_id, slide_id)
    if slide is None:
        raise HTTPException(status_code=404, detail=f"页面 {slide_id} 不存在")
    return _slide_detail(manager, task_id, slide)


@router.post("/{task_id}/cancel")
async def cancel_task(task_id: str, request: Request):
    """取消正在运行的任务。"""
    manager = _get_manager(request)
    success = await manager.cancel(task_id)
    if not success:
        snapshot = manager.get_snapshot(task_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
        raise HTTPException(
            status_code=409,
            detail=f"任务当前状态为 {snapshot.status.value}，无法取消",
        )
    return {
        "task_id": task_id,
        "status": "cancelled",
        "message": "任务已取消，已完成页面保留",
    }


class RetryRequest(BaseModel):
    """重试请求体。"""

    retry_failed_slides_only: bool = Field(
        default=True, description="是否仅重试失败页面"
    )


@router.post("/{task_id}/retry")
async def retry_task(task_id: str, body: RetryRequest, request: Request):
    """重试失败的任务。"""
    manager = _get_manager(request)
    success = await manager.retry(
        task_id, retry_failed_slides_only=body.retry_failed_slides_only
    )
    if not success:
        snapshot = manager.get_snapshot(task_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
        raise HTTPException(
            status_code=409,
            detail=f"任务当前状态为 {snapshot.status.value}，无法重试（仅失败任务可重试）",
        )
    return {"task_id": task_id, "status": "running", "message": "任务已重新启动"}


class ExportRequest(BaseModel):
    """导出请求体。"""

    format: str = Field(default="pptx", description="导出格式：pptx 或 pdf")


@router.post("/{task_id}/export")
async def export_task(
    task_id: str, request: Request, body: ExportRequest | None = None
):
    """导出任务最终产物。"""
    manager = _get_manager(request)
    fmt = (body.format if body else "pptx").lower()
    if fmt not in {"pptx", "pdf"}:
        raise HTTPException(status_code=422, detail="导出格式仅支持 pptx 或 pdf")

    artifact = await manager.export(task_id, fmt=fmt)  # type: ignore[arg-type]
    if artifact is None:
        snapshot = manager.get_snapshot(task_id)
        if snapshot is None:
            raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")
        raise HTTPException(status_code=409, detail="任务无可导出产物")
    url = artifact_url(task_id, artifact)
    return {
        "task_id": task_id,
        "format": fmt,
        "artifact_path": artifact,
        "artifact_url": url,
        "download_url": url,
        "filename": artifact.rsplit("/", 1)[-1],
        "status": "completed",
    }


@router.get("/{task_id}/artifacts/{artifact_path:path}")
async def get_artifact(
    task_id: str,
    artifact_path: str,
    request: Request,
):
    """安全读取任务产物文件（缩略图、导出文件等）。

    仅允许读取任务工作区内的文件，防止路径穿越攻击。
    """
    manager = _get_manager(request)
    snapshot = manager.get_snapshot(task_id)
    if snapshot is None:
        raise HTTPException(status_code=404, detail=f"任务 {task_id} 不存在")

    workspace = task_dir(manager.workspace_base, task_id)
    if not is_path_safe(manager.workspace_base, task_id, artifact_path):
        raise HTTPException(status_code=403, detail="路径访问被拒绝")

    full_path = workspace / artifact_path
    if not full_path.exists():
        raise HTTPException(status_code=404, detail=f"产物 {artifact_path} 不存在")

    # 根据扩展名设置 MIME 类型
    suffix = full_path.suffix.lower()
    media_type_map = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        ".pdf": "application/pdf",
        ".json": "application/json",
        ".md": "text/markdown",
        ".txt": "text/plain",
    }
    media_type = media_type_map.get(suffix, "application/octet-stream")

    from fastapi.responses import FileResponse

    return FileResponse(full_path, media_type=media_type)
