"""Research-manuscript review API used before a formal task is created."""

from __future__ import annotations

import mimetypes

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from deeppresenter.server.models.outlines import (
    OutlineApprovalResponse,
    OutlineDraftResponse,
)
from deeppresenter.server.routes.attachments import resolve_attachment_paths
from deeppresenter.server.routes.tasks import CreateTaskRequest, resolve_template
from deeppresenter.server.services.outline_service import (
    OutlineConflictError,
    OutlineNotFoundError,
    OutlineService,
)
from deeppresenter.templates.store import (
    LegacyTemplateError,
    TemplateAspectRatioMismatchError,
    TemplateNotFoundError,
    TemplateRevisionNotReadyError,
)

router = APIRouter(prefix="/api/outlines")


class RegenerateOutlineRequest(BaseModel):
    """Feedback appended to Research before producing a complete replacement."""

    comment: str = Field(default="", max_length=1000)


def _get_service(request: Request) -> OutlineService:
    service = request.app.state.outline_service
    if service is None:
        raise HTTPException(status_code=500, detail="OutlineService 未初始化")
    return service


def _raise_outline_error(exc: Exception) -> None:
    if isinstance(exc, OutlineNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, OutlineConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, TemplateRevisionNotReadyError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(
        exc,
        (
            LegacyTemplateError,
            TemplateAspectRatioMismatchError,
            TemplateNotFoundError,
        ),
    ):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


@router.post("", response_model=OutlineDraftResponse, status_code=201)
async def create_outline(
    body: CreateTaskRequest,
    request: Request,
) -> OutlineDraftResponse:
    """Run Research and create a reviewable Markdown manuscript."""

    service = _get_service(request)
    manager = request.app.state.task_manager
    topic = (body.instruction or body.topic or "").strip()
    if not topic:
        raise HTTPException(status_code=422, detail="instruction 或 topic 不能为空")
    if body.convert_type not in (None, "deeppresenter"):
        raise HTTPException(
            status_code=422,
            detail="内容审查仅支持 deeppresenter 生成模式",
        )
    page_count = body.page_count
    if page_count is None and body.num_pages:
        first = body.num_pages.split("-", 1)[0].strip()
        page_count = int(first) if first.isdigit() else None
    page_count = max(5, min(30, page_count or 12))
    attachments = (
        body.attachments
        if body.attachments
        else resolve_attachment_paths(manager.workspace_base, body.attachment_ids)
    )
    language = "zh" if body.language.lower().startswith("zh") else body.language
    template, template_id, template_ratio = resolve_template(body, request)
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
        ratio = template_ratio
    else:
        ratio = requested_ratio or "16:9"

    try:
        return await service.create(
            topic=topic,
            attachments=attachments,
            page_count=page_count,
            ratio=ratio,
            language=language,
            template=template,
            template_id=template_id,
            template_revision_id=body.template_revision_id,
            convert_type=body.convert_type,
        )
    except Exception as exc:
        _raise_outline_error(exc)
        raise


@router.get("/{outline_id}", response_model=OutlineDraftResponse)
async def get_outline(
    outline_id: str,
    request: Request,
) -> OutlineDraftResponse:
    """Read the latest persisted manuscript revision and generation status."""

    try:
        return _get_service(request).get(outline_id)
    except Exception as exc:
        _raise_outline_error(exc)
        raise


@router.get("/{outline_id}/assets/{asset_path:path}")
async def get_outline_asset(
    outline_id: str,
    asset_path: str,
    request: Request,
) -> FileResponse:
    """Serve only image files inside this review workspace."""

    service = _get_service(request)
    try:
        path = service.resolve_asset(outline_id, asset_path)
    except OutlineNotFoundError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    except FileNotFoundError as exc:
        raise HTTPException(status_code=404, detail="图片不存在") from exc
    except PermissionError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from exc
    media_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
    return FileResponse(path, media_type=media_type)


@router.post(
    "/{outline_id}/regenerate",
    response_model=OutlineDraftResponse,
    status_code=202,
)
async def regenerate_outline(
    outline_id: str,
    body: RegenerateOutlineRequest,
    request: Request,
) -> OutlineDraftResponse:
    """Continue Research with feedback and replace the Markdown manuscript."""

    try:
        return await _get_service(request).regenerate(outline_id, body.comment)
    except Exception as exc:
        _raise_outline_error(exc)
        raise


@router.post(
    "/{outline_id}/approve",
    response_model=OutlineApprovalResponse,
    status_code=201,
)
async def approve_outline(
    outline_id: str,
    request: Request,
) -> OutlineApprovalResponse:
    """Approve the manuscript and continue directly with slide generation."""

    service = _get_service(request)
    try:
        task_id = await service.approve(outline_id)
    except Exception as exc:
        _raise_outline_error(exc)
        raise
    return OutlineApprovalResponse(
        task_id=task_id,
        outline_id=outline_id,
        status="queued",
    )
