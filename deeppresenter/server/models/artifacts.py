"""SlideArtifact 模型与工作区路径工具。

每页生成后立即持久化，确保预览、编辑和导出操作基于稳定的磁盘数据，
而非 MCP 内存中的临时状态。
"""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


class StructuredContent(BaseModel):
    """从幻灯片中提取的可编辑内容字段。

    设计为松散 schema —— 不同模板暴露不同的元素名称。
    常见键：``title``、``subtitle``、``body``（字符串列表）、
    ``images``（``{path, caption}`` 列表）。
    """

    title: Optional[str] = Field(default=None, description="幻灯片标题")
    subtitle: Optional[str] = Field(default=None, description="副标题")
    body: List[str] = Field(default_factory=list, description="正文文本块")
    images: List[Dict[str, Any]] = Field(
        default_factory=list,
        description="图片描述列表，每项含 `path` 和可选 `caption`",
    )
    extras: Dict[str, Any] = Field(
        default_factory=dict,
        description="模板特有元素（如页脚、标注等）",
    )


class SlideArtifact(BaseModel):
    """单页生成产物的磁盘表示。

    存储路径：``workspace/<task_id>/slides/<slide_id>/current.json``
    （为最新 revision 的 slide.json 的副本）。
    """

    slide_id: str = Field(..., description="稳定 UUID；重新排序时不变")
    task_id: str = Field(..., description="所属任务 ID")
    index: int = Field(..., ge=1, description="1-based 页码")
    status: str = Field(
        default="pending", description="当前页面状态"
    )
    mode: str = Field(
        default="html",
        description="生成模式：`html`（Design agent）或 `template`（PPTAgent）",
    )
    layout_name: Optional[str] = Field(
        default=None, description="模板布局名称（仅模板模式）"
    )
    structured_data: StructuredContent = Field(
        default_factory=StructuredContent,
        description="提取的可编辑内容",
    )
    source_path: Optional[str] = Field(
        default=None, description="源文件相对路径（HTML 或结构化 JSON）"
    )
    preview_path: Optional[str] = Field(
        default=None, description="PNG/JPG 缩略图相对路径"
    )
    revision: int = Field(default=1, ge=1, description="当前版本号")
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="UTC ISO-8601 创建时间",
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="UTC ISO-8601 更新时间",
    )

    def bumped(self) -> "SlideArtifact":
        """返回 revision 加 1 并更新时间戳的深拷贝。"""
        import copy

        dup = copy.deepcopy(self)
        dup.revision += 1
        dup.updated_at = datetime.now(timezone.utc).isoformat()
        return dup


# ── 工作区路径工具 ──────────────────────────────────────────────


def task_dir(workspace_base: Path, task_id: str) -> Path:
    """返回任务工作区根目录。"""
    return workspace_base / task_id


def slides_dir(workspace_base: Path, task_id: str) -> Path:
    """返回幻灯片父目录。"""
    return task_dir(workspace_base, task_id) / "slides"


def slide_dir(workspace_base: Path, task_id: str, slide_id: str) -> Path:
    """返回单个幻灯片的目录。"""
    return slides_dir(workspace_base, task_id) / slide_id


def revision_dir(
    workspace_base: Path, task_id: str, slide_id: str, revision: int
) -> Path:
    """返回指定版本的存储目录。"""
    return slide_dir(workspace_base, task_id, slide_id) / "revisions" / str(revision)


def exports_dir(workspace_base: Path, task_id: str) -> Path:
    """返回任务导出目录。"""
    return task_dir(workspace_base, task_id) / "exports"


def is_path_safe(workspace_base: Path, task_id: str, requested: str) -> bool:
    """校验 *requested* 路径是否在任务工作区内。

    用于防止产物文件读取接口的路径穿越攻击。
    """
    root = task_dir(workspace_base, task_id).resolve()
    candidate = (root / requested).resolve()
    return str(candidate).startswith(str(root))


# ── 版本数量限制 ────────────────────────────────────────────────

MAX_REVISIONS = 10
"""每页保留的最大版本数。超出后删除最旧的预览图，结构日志可继续保留。"""
