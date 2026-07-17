from __future__ import annotations
"""前端工作台附件上传路由。"""

import re
import shutil
import uuid
from pathlib import Path

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import Response

router = APIRouter(prefix="/api/attachments")


def attachments_root(workspace_base: Path) -> Path:
    return Path(workspace_base) / "_attachments"


def _safe_name(filename: str) -> str:
    name = Path(filename).name.strip() or "attachment"
    return re.sub(r"[^A-Za-z0-9._\-\u4e00-\u9fff]", "_", name)


def resolve_attachment_paths(workspace_base: Path, ids: list[str]) -> list[str]:
    """把前端 attachment_id 解析为真实文件路径；未知 ID 原样保留。"""
    root = attachments_root(workspace_base)
    resolved: list[str] = []
    for attachment_id in ids:
        attachment_dir = root / attachment_id
        if attachment_dir.is_dir():
            files = [p for p in attachment_dir.iterdir() if p.is_file()]
            if files:
                resolved.append(str(files[0].resolve()))
                continue
        resolved.append(attachment_id)
    return resolved


@router.post("")
async def upload_attachment(request: Request, file: UploadFile = File(...)):
    manager = request.app.state.task_manager
    if manager is None:
        raise HTTPException(status_code=500, detail="TaskManager 未初始化")

    attachment_id = uuid.uuid4().hex
    target_dir = attachments_root(manager.workspace_base) / attachment_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / _safe_name(file.filename or "attachment")

    try:
        with target.open("wb") as fp:
            shutil.copyfileobj(file.file, fp)
    finally:
        await file.close()

    return {"attachment_id": attachment_id}


@router.delete("/{attachment_id}", status_code=204)
async def delete_attachment(attachment_id: str, request: Request):
    manager = request.app.state.task_manager
    if manager is None:
        raise HTTPException(status_code=500, detail="TaskManager 未初始化")

    target_dir = attachments_root(manager.workspace_base) / attachment_id
    if target_dir.exists():
        shutil.rmtree(target_dir, ignore_errors=True)
    return Response(status_code=204)
