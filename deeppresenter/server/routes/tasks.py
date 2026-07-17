from __future__ import annotations
"""任务相关 FastAPI 路由 —— 创建、查询、取消、SSE 订阅。

所有路由均挂在 ``/api/tasks`` 下。
"""

import json
import asyncio
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from deeppresenter.server.models.artifacts import SlideArtifact, is_path_safe, task_dir
from deeppresenter.server.routes.attachments import resolve_attachment_paths
from deeppresenter.server.services.preview import artifact_url
from deeppresenter.server.services.task_manager import TaskManager

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
    powerpoint_type: str = Field(default="16:9", description="画面比例")
    ratio: Optional[str] = Field(default=None, description="前端工作台画面比例字段")
    template: Optional[str] = Field(default=None, description="模板 ID")
    template_id: Optional[str] = Field(default=None, description="前端工作台模板 ID 字段")
    convert_type: Optional[str] = Field(
        default=None, description="转换模式 deeppresenter/pptagent"
    )
    enable_planner: bool = Field(default=False, description="是否启用大纲规划")
    language: str = Field(default="en", description="语言 en/zh")


class CreateTaskResponse(BaseModel):
    """创建任务响应体。"""

    task_id: str
    status: str
    created_at: str


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
            artifact_url(task_id, slide.preview_path)
            if slide.preview_path
            else None
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
    num_pages = body.num_pages or (str(body.page_count) if body.page_count else None)
    powerpoint_type = body.ratio or body.powerpoint_type
    attachments = (
        body.attachments
        if body.attachments
        else resolve_attachment_paths(manager.workspace_base, body.attachment_ids)
    )
    language = "zh" if body.language.lower().startswith("zh") else body.language
    task_id = await manager.create(
        instruction=instruction,
        attachments=attachments,
        num_pages=num_pages,
        powerpoint_type=powerpoint_type,
        # 前端 main 的 template_id 是 UI 模板库 ID；C2 后端当前只在
        # 显式 template 字段下启用 PPTAgent 模板模式，避免误入模板链路。
        template=body.template,
        template_id=body.template_id,
        convert_type=body.convert_type,
        enable_planner=body.enable_planner,
        language=language,
    )
    snapshot = manager.get_snapshot(task_id)
    if snapshot is None:
        return CreateTaskResponse(task_id=task_id, status="queued", created_at="")
    return _task_response(manager, snapshot)


@router.get("")
async def list_tasks(request: Request):
    """列出所有任务。"""
    manager = _get_manager(request)
    tasks = manager.list_tasks()
    return {
        "tasks": [
            {
                "task_id": t.task_id,
                "status": t.status.value if hasattr(t.status, "value") else t.status,
                "instruction": t.instruction,
                "created_at": t.created_at,
            }
            for t in tasks
        ]
    }


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
    return {"task_id": task_id, "status": "cancelled", "message": "任务已取消，已完成页面保留"}


class RetryRequest(BaseModel):
    """重试请求体。"""

    retry_failed_slides_only: bool = Field(default=True, description="是否仅重试失败页面")


@router.post("/{task_id}/retry")
async def retry_task(task_id: str, body: RetryRequest, request: Request):
    """重试失败的任务。"""
    manager = _get_manager(request)
    success = await manager.retry(task_id, retry_failed_slides_only=body.retry_failed_slides_only)
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
async def export_task(task_id: str, request: Request, body: ExportRequest | None = None):
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
