"""模板 API 路由 —— 6 个端点 + SSE。

POST   /api/templates                     上传模板并创建解析任务
GET    /api/templates                     获取可用及解析中的模板
GET    /api/templates/{template_id}        获取模板详情和解析状态
GET    /api/templates/{template_id}/events 订阅模板解析 SSE 事件
POST   /api/templates/{template_id}/retry  重试失败解析
DELETE /api/templates/{template_id}        删除用户模板及缓存
"""

import asyncio
import hashlib
import json
import shutil
import threading
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, BackgroundTasks, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse, StreamingResponse

from deeppresenter.server.models.templates import (
    GenerationEvent,
    TemplateErrorCode,
    TemplateErrorResponse,
    TemplateListResponse,
    TemplateManifest,
    TemplateSettings,
    TemplateStatus,
)
from deeppresenter.server.services.template_service import (
    TemplateInductionService,
    sanitize_filename,
    validate_pptx,
)
from deeppresenter.utils.log import debug, error, get_logger

logger = get_logger()

router = APIRouter(prefix="/api/templates", tags=["templates"])

# 常量
ALLOWED_EXTENSIONS = {".pptx"}
ALLOWED_MIME = {
    "application/vnd.openxmlformats-officedocument.presentationml.presentation"
}

# 全局 SSE 事件队列
_event_queues: dict[str, asyncio.Queue] = {}
_queue_lock = threading.Lock()


def get_event_queue(template_id: str) -> asyncio.Queue:
    """获取或创建 SSE 事件队列"""
    with _queue_lock:
        if template_id not in _event_queues:
            _event_queues[template_id] = asyncio.Queue(maxsize=100)
        return _event_queues[template_id]


def cleanup_event_queue(template_id: str) -> None:
    """清理 SSE 事件队列"""
    with _queue_lock:
        _event_queues.pop(template_id, None)


async def _push_event(event: GenerationEvent) -> None:
    """推送事件到 SSE 队列"""
    queue = get_event_queue(event.template_id)
    await queue.put(event.model_dump())


# =============================================================================
# 依赖注入辅助（实际应用通过 app.state 注入）
# =============================================================================


def _get_registry(request: Request):
    """从 app.state 获取 TemplateRegistry"""
    registry = getattr(request.app.state, "template_registry", None)
    if registry is None:
        raise HTTPException(500, detail="TemplateRegistry not initialized")
    return registry


def _get_induction_service(request: Request) -> TemplateInductionService:
    """从 app.state 获取 TemplateInductionService"""
    service = getattr(request.app.state, "induction_service", None)
    if service is None:
        raise HTTPException(500, detail="TemplateInductionService not initialized")
    return service


def _get_settings(request: Request) -> TemplateSettings:
    """从 app.state 获取 TemplateSettings"""
    settings = getattr(request.app.state, "template_settings", None)
    return settings or TemplateSettings()


# =============================================================================
# 端点
# =============================================================================


@router.post("")
async def upload_template(
    request: Request,
    file: UploadFile = File(...),
    background_tasks: BackgroundTasks = None,
):
    """上传模板并创建解析任务 (P0-1)"""
    registry = _get_registry(request)
    service = _get_induction_service(request)
    settings = _get_settings(request)

    # 1. 校验扩展名
    if file.filename is None:
        raise HTTPException(400, detail={
            "code": TemplateErrorCode.UNSUPPORTED_FORMAT,
            "message": "未提供文件名",
        })

    suffix = Path(file.filename).suffix.lower()
    if suffix not in ALLOWED_EXTENSIONS:
        raise HTTPException(400, detail=TemplateErrorResponse(
            code=TemplateErrorCode.UNSUPPORTED_FORMAT,
            message=f"仅支持 {ALLOWED_EXTENSIONS} 格式，收到: {suffix}",
        ).model_dump())

    # 2. 安全文件名
    safe_name = sanitize_filename(Path(file.filename).stem)

    # 3. 读取内容
    content = await file.read()
    if len(content) > settings.max_file_size:
        max_mb = settings.max_file_size // 1024 // 1024
        raise HTTPException(400, detail=TemplateErrorResponse(
            code=TemplateErrorCode.FILE_TOO_LARGE,
            message=f"文件大小超过 {max_mb}MB 限制",
        ).model_dump())

    # 4. 校验 PPTX 结构
    validation = validate_pptx(content)
    if not validation.valid and validation.error:
        status_code = 400
        return JSONResponse(
            status_code=status_code,
            content=validation.error,
        )

    # 5. Hash 去重
    file_hash = hashlib.sha256(content).hexdigest()
    existing = registry.find_by_hash(file_hash)
    if existing:
        if existing.status == TemplateStatus.READY:
            debug(f"Duplicate template detected: {existing.template_id}")
            return existing
        elif existing.status == TemplateStatus.PARSING:
            return JSONResponse(
                status_code=409,
                content={
                    "code": TemplateErrorCode.ALREADY_PARSING,
                    "message": "相同文件正在解析中",
                    "template_id": existing.template_id,
                },
            )

    # 6. 创建模板目录
    template_id = existing.template_id if existing else safe_name
    # 防止 template_id 冲突
    counter = 1
    base_id = template_id
    while (registry.templates_dir / template_id).exists():
        # 检查是否是失败的残留
        existing_manifest_path = registry.templates_dir / template_id / "manifest.json"
        if existing_manifest_path.exists():
            try:
                existing_manifest = TemplateManifest.load(
                    registry.templates_dir / template_id
                )
                if existing_manifest.status == TemplateStatus.FAILED:
                    # 覆盖失败的模板
                    registry.unregister(template_id)
                    shutil.rmtree(registry.templates_dir / template_id, ignore_errors=True)
                    break
            except Exception:
                pass
        template_id = f"{base_id}_{counter}"
        counter += 1

    template_dir = registry.templates_dir / template_id
    template_dir.mkdir(parents=True, exist_ok=True)

    # 7. 保存原始文件
    original_path = template_dir / "original.pptx"
    original_path.write_bytes(content)

    # 8. 创建 manifest
    manifest = TemplateManifest(
        template_id=template_id,
        name=safe_name,
        status=TemplateStatus.PARSING,
        source_hash=file_hash,
        slide_count=validation.slide_count,
    )
    manifest.save(template_dir)
    registry.register(manifest)

    # 9. 注册 service 进度回调
    original_callback = service.progress_callback

    async def sse_callback(event_data: dict) -> None:
        """桥接 service 进度事件到 SSE 队列"""
        gen_event = GenerationEvent(**event_data)
        await _push_event(gen_event)

        # 终态时清理事件队列
        if gen_event.event in ("template.ready", "template.failed"):
            # 更新 registry 中的 manifest
            fresh_manifest = TemplateManifest.load(template_dir)
            registry.update_manifest(fresh_manifest)

            # 终态时清理 SSE 队列（延迟 5s 让消费者读取完）
            async def delayed_cleanup():
                await asyncio.sleep(5)
                cleanup_event_queue(template_id)

            asyncio.create_task(delayed_cleanup())

        # 也调用原始回调
        if original_callback:
            try:
                result = original_callback(event_data)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass

    service.progress_callback = sse_callback

    # 10. 后台启动解析
    background_tasks.add_task(service.run_induction, template_id)

    logger.info(f"Template upload accepted: {template_id} (hash={file_hash[:12]}...)")
    return manifest


@router.get("")
async def list_templates(request: Request):
    """获取可用及解析中的模板"""
    registry = _get_registry(request)
    manifests = registry.list_all(include_failed=False)
    return TemplateListResponse(templates=manifests, count=len(manifests))


@router.get("/{template_id}")
async def get_template(template_id: str, request: Request):
    """获取模板详情和解析状态"""
    registry = _get_registry(request)
    manifest = registry.get(template_id)
    if manifest is None:
        raise HTTPException(404, detail=TemplateErrorResponse(
            code=TemplateErrorCode.TEMPLATE_NOT_FOUND,
            message=f"模板 {template_id} 不存在",
        ).model_dump())
    return manifest


@router.get("/{template_id}/events")
async def template_events(template_id: str, request: Request):
    """订阅模板解析 SSE 事件"""
    registry = _get_registry(request)
    manifest = registry.get(template_id)
    if manifest is None:
        raise HTTPException(404, detail=TemplateErrorResponse(
            code=TemplateErrorCode.TEMPLATE_NOT_FOUND,
            message=f"模板 {template_id} 不存在",
        ).model_dump())

    async def event_generator():
        queue = get_event_queue(template_id)

        # 先发送当前状态
        yield f"event: status\ndata: {json.dumps({'template_id': template_id, 'status': manifest.status.value})}\n\n"

        while True:
            try:
                event_data = await asyncio.wait_for(queue.get(), timeout=30)
                yield f"event: {event_data.get('event', 'message')}\ndata: {json.dumps(event_data)}\n\n"

                if event_data.get("event") in ("template.ready", "template.failed"):
                    break
            except asyncio.TimeoutError:
                # 发送心跳，保持连接
                yield f": heartbeat\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.post("/{template_id}/retry")
async def retry_parse(
    template_id: str,
    request: Request,
    background_tasks: BackgroundTasks = None,
):
    """重试失败解析 (P1)"""
    registry = _get_registry(request)
    service = _get_induction_service(request)

    manifest = registry.get(template_id)
    if manifest is None:
        raise HTTPException(404, detail=TemplateErrorResponse(
            code=TemplateErrorCode.TEMPLATE_NOT_FOUND,
            message=f"模板 {template_id} 不存在",
        ).model_dump())

    if manifest.status != TemplateStatus.FAILED:
        raise HTTPException(400, detail=TemplateErrorResponse(
            code=TemplateErrorCode.INVALID_STATE,
            message=f"只有失败的模板可以重试，当前状态: {manifest.status.value}",
        ).model_dump())

    # 重置状态
    template_dir = registry.templates_dir / template_id
    manifest.status = TemplateStatus.PARSING
    manifest.error = None
    manifest.save(template_dir)
    registry.register(manifest)

    # 后台重新解析
    background_tasks.add_task(service.run_induction, template_id)

    return {"message": "重新解析已启动", "template_id": template_id}


@router.delete("/{template_id}")
async def delete_template(template_id: str, request: Request):
    """删除用户模板及缓存"""
    registry = _get_registry(request)
    manifest = registry.get(template_id)
    if manifest is None:
        raise HTTPException(404, detail=TemplateErrorResponse(
            code=TemplateErrorCode.TEMPLATE_NOT_FOUND,
            message=f"模板 {template_id} 不存在",
        ).model_dump())

    # 注销并清理
    registry.unregister(template_id)
    registry.invalidate_cache(template_id)

    template_dir = registry.templates_dir / template_id
    if template_dir.exists():
        shutil.rmtree(template_dir, ignore_errors=True)

    cleanup_event_queue(template_id)

    logger.info(f"Template deleted: {template_id}")
    return {"message": f"模板 {template_id} 已删除"}
