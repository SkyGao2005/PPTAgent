"""统一事件模型 —— GenerationEvent 及所有子类型。

这是四个方向（A/B/C/D）共享的事件协议的唯一来源。
字段变更必须同步更新 docs/api-contract.md。
"""

from datetime import datetime, timezone
from enum import Enum
from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field, field_validator


class EventType(str, Enum):
    """系统中所有事件类型。

    命名规则：``<领域>.<动作>``，领域为 task / template / stage / slide / edit / export。
    """

    # ── 任务生命周期 ────────────────────────────────────────────
    TASK_CREATED = "task.created"        # 任务已创建
    TASK_STARTED = "task.started"        # 任务开始执行
    TASK_COMPLETED = "task.completed"    # 任务全部完成
    TASK_FAILED = "task.failed"          # 任务失败
    TASK_CANCELLED = "task.cancelled"    # 用户取消任务

    # ── 内容审查 ──────────────────────────────────────────────────
    OUTLINE_GENERATING = "outline.generating"
    OUTLINE_READY = "outline.ready"
    OUTLINE_FAILED = "outline.failed"
    OUTLINE_APPROVED = "outline.approved"

    # ── 模板解析（方向 A 发布）──────────────────────────────────
    TEMPLATE_PARSE_STARTED = "template.parse_started"
    TEMPLATE_PARSE_PROGRESS = "template.parse_progress"
    TEMPLATE_READY = "template.ready"
    TEMPLATE_FAILED = "template.failed"

    # ── 阶段进度（方向 C 发布）──────────────────────────────────
    STAGE_STARTED = "stage.started"
    STAGE_PROGRESS = "stage.progress"
    STAGE_COMPLETED = "stage.completed"

    # ── 页面级事件（方向 C 发布）────────────────────────────────
    SLIDE_STARTED = "slide.started"
    SLIDE_PREVIEW_READY = "slide.preview_ready"
    SLIDE_COMPLETED = "slide.completed"
    SLIDE_FAILED = "slide.failed"

    # ── 编辑事件（方向 D 发布）──────────────────────────────────
    EDIT_STARTED = "edit.started"
    EDIT_PREVIEW_READY = "edit.preview_ready"
    EDIT_APPLIED = "edit.applied"
    EDIT_FAILED = "edit.failed"
    EDIT_REVERTED = "edit.reverted"

    # ── 导出事件（方向 C 发布）──────────────────────────────────
    EXPORT_STARTED = "export.started"
    EXPORT_COMPLETED = "export.completed"
    EXPORT_FAILED = "export.failed"


class StageName(str, Enum):
    """生成任务的阶段名称，按执行顺序排列。"""

    PREPARE = "prepare"      # 附件处理、工作区初始化
    PLAN = "plan"            # 大纲规划
    RESEARCH = "research"    # 资料搜索与稿件生成
    GENERATE = "generate"    # 逐页幻灯片生成
    EDIT = "edit"            # 单页对话式修改
    EXPORT = "export"        # 最终合并导出


class TaskStatus(str, Enum):
    """任务级别状态，遵循状态机模型。"""

    QUEUED = "queued"        # 排队等待
    RUNNING = "running"      # 运行中
    COMPLETED = "completed"  # 成功完成
    SUCCEEDED = "completed"  # 兼容旧代码命名；API 对外统一输出 completed
    FAILED = "failed"        # 执行失败
    CANCELLED = "cancelled"  # 用户取消


# ── 状态机 ──────────────────────────────────────────────────────

#: 任务状态合法转移表。终态（completed / cancelled）无出边。
VALID_TASK_TRANSITIONS: Dict[TaskStatus, set] = {
    TaskStatus.QUEUED: {TaskStatus.RUNNING, TaskStatus.CANCELLED},
    TaskStatus.RUNNING: {
        TaskStatus.COMPLETED,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    },
    TaskStatus.COMPLETED: set(),
    TaskStatus.FAILED: {TaskStatus.RUNNING},  # 允许重试
    TaskStatus.CANCELLED: set(),
}

#: 页面级别的状态值集合。
SLIDE_STATUSES = frozenset(
    {"pending", "generating", "completed", "failed", "editing"}
)


def validate_task_transition(
    current: TaskStatus,
    target: TaskStatus,
) -> bool:
    """校验 *current* → *target* 是否为合法状态转移。"""
    return target in VALID_TASK_TRANSITIONS.get(current, set())


# ── 事件模型 ────────────────────────────────────────────────────


class GenerationEvent(BaseModel):
    """SSE 流上发出的单条事件，同时持久化到 events.jsonl。

    所有时间字段使用 UTC ISO-8601 字符串。
    """

    task_id: str = Field(..., description="任务唯一 ID（8 位 hex）")
    seq: int = Field(
        ...,
        ge=1,
        description="单任务内严格递增的事件序号，从 1 开始",
    )
    type: EventType = Field(..., description="事件类型")

    # ── 可选上下文字段 ─────────────────────────────────────────
    stage: Optional[StageName] = Field(
        default=None, description="当前阶段（阶段/页面事件时填写）"
    )
    status: Optional[TaskStatus] = Field(
        default=None, description="任务状态（任务生命周期事件时填写）"
    )
    progress: Optional[float] = Field(
        default=None,
        ge=0,
        le=100,
        description="全局进度 0~100；无法精确计算时为 null",
    )
    stage_progress: Optional[float] = Field(
        default=None,
        ge=0,
        le=100,
        description="当前阶段进度 0~100；无法精确计算时为 null",
    )
    message: Optional[str] = Field(
        default=None, description="用户可读的简短描述"
    )
    slide_id: Optional[str] = Field(
        default=None, description="稳定页面 UUID（页面相关事件时填写）"
    )
    slide_index: Optional[int] = Field(
        default=None, ge=1, description="页码（1-based）"
    )
    total_slides: Optional[int] = Field(
        default=None, ge=1, description="总页数"
    )
    artifact_url: Optional[str] = Field(
        default=None, description="产物相对路径（缩略图、PPTX 等）"
    )
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat(),
        description="UTC ISO-8601 时间戳",
    )
    payload: Dict[str, Any] = Field(
        default_factory=dict,
        description="少量扩展数据；请勿放入大段文本",
    )

    @field_validator("status", mode="before")
    @classmethod
    def _normalize_legacy_status(cls, value):
        if value == "succeeded":
            return "completed"
        return value


# ── 进度计算 ────────────────────────────────────────────────────

# 各阶段权重（参见 docs/api-contract.md 第 6 节）。
STAGE_WEIGHTS: Dict[StageName, float] = {
    StageName.PREPARE: 5.0,
    StageName.PLAN: 10.0,
    StageName.RESEARCH: 25.0,
    StageName.GENERATE: 50.0,
    StageName.EDIT: 0.0,   # 编辑阶段不单独占进度
    StageName.EXPORT: 10.0,
}

# 各阶段起始时的累计进度百分比。
_STAGE_START: Dict[StageName, float] = {}
_accum = 0.0
for _stage, _weight in [
    (StageName.PREPARE, 5.0),
    (StageName.PLAN, 10.0),
    (StageName.RESEARCH, 25.0),
    (StageName.GENERATE, 50.0),
    (StageName.EXPORT, 10.0),
]:
    _STAGE_START[_stage] = _accum
    _accum += _weight
del _accum


def stage_start_progress(stage: StageName) -> float:
    """返回 *stage* 开始时的全局进度值。"""
    return _STAGE_START.get(stage, 0.0)


def stage_end_progress(stage: StageName) -> float:
    """返回 *stage* 完成时的全局进度值。"""
    weight = STAGE_WEIGHTS.get(stage, 0.0)
    return _STAGE_START.get(stage, 0.0) + weight


# ── 持久化工具 ──────────────────────────────────────────────────


def parse_events_from_jsonl(path: str) -> List[GenerationEvent]:
    """读取 JSONL 文件并返回 GenerationEvent 列表。

    自动跳过心跳和 EOF 等伪事件（``{"type": "heartbeat"}`` 等）。
    """
    import json

    events: List[GenerationEvent] = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            obj = json.loads(line)
            if "type" in obj and obj["type"] in ("heartbeat", "eof"):
                continue
            events.append(GenerationEvent(**obj))
    return events
