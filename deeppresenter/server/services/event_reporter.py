"""GenerationEvent 发布辅助层。

EventReporter 是 AgentLoop / AgentEnv 与 EventBus 之间的薄适配器：
业务代码只表达“任务开始、阶段完成、工具失败”等动作，不直接关心
GenerationEvent 字段拼装和持久化细节。
"""

from collections.abc import Awaitable, Callable
from typing import Any

from deeppresenter.server.models.events import (
    EventType,
    GenerationEvent,
    StageName,
    TaskStatus,
    stage_end_progress,
    stage_start_progress,
)

EventPublisher = Callable[[GenerationEvent], Awaitable[None]]


class EventReporter:
    """将任务执行动作转换为 GenerationEvent 并发布。"""

    def __init__(self, task_id: str, publish: EventPublisher) -> None:
        self.task_id = task_id
        self._publish = publish

    async def emit(
        self,
        event_type: EventType,
        *,
        stage: StageName | None = None,
        status: TaskStatus | None = None,
        progress: float | None = None,
        stage_progress: float | None = None,
        message: str | None = None,
        slide_id: str | None = None,
        slide_index: int | None = None,
        total_slides: int | None = None,
        artifact_url: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        """发布一条事件。

        ``seq`` 由 EventBus 覆写，这里只提供满足 Pydantic 校验的占位值。
        """
        await self._publish(
            GenerationEvent(
                task_id=self.task_id,
                seq=1,
                type=event_type,
                stage=stage,
                status=status,
                progress=progress,
                stage_progress=stage_progress,
                message=message,
                slide_id=slide_id,
                slide_index=slide_index,
                total_slides=total_slides,
                artifact_url=artifact_url,
                payload=payload or {},
            )
        )

    async def task_created(self, message: str | None = None) -> None:
        await self.emit(
            EventType.TASK_CREATED,
            status=TaskStatus.QUEUED,
            progress=0,
            message=message or "任务已创建",
        )

    async def task_started(self, message: str | None = None) -> None:
        await self.emit(
            EventType.TASK_STARTED,
            status=TaskStatus.RUNNING,
            progress=0,
            message=message or "任务开始执行",
        )

    async def task_completed(
        self, artifact_url: str | None = None, message: str | None = None
    ) -> None:
        await self.emit(
            EventType.TASK_COMPLETED,
            status=TaskStatus.SUCCEEDED,
            progress=100,
            artifact_url=artifact_url,
            message=message or "任务已完成",
        )

    async def task_failed(
        self, message: str, payload: dict[str, Any] | None = None
    ) -> None:
        await self.emit(
            EventType.TASK_FAILED,
            status=TaskStatus.FAILED,
            message=message,
            payload=payload,
        )

    async def task_cancelled(self, message: str | None = None) -> None:
        await self.emit(
            EventType.TASK_CANCELLED,
            status=TaskStatus.CANCELLED,
            message=message or "任务已取消",
        )

    async def stage_started(
        self, stage: StageName, message: str | None = None
    ) -> None:
        await self.emit(
            EventType.STAGE_STARTED,
            stage=stage,
            progress=stage_start_progress(stage),
            stage_progress=0,
            message=message or f"{stage.value} 阶段开始",
        )

    async def stage_progress(
        self,
        stage: StageName,
        stage_progress: float,
        *,
        message: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await self.emit(
            EventType.STAGE_PROGRESS,
            stage=stage,
            stage_progress=stage_progress,
            message=message,
            payload=payload,
        )

    async def stage_completed(
        self, stage: StageName, message: str | None = None
    ) -> None:
        await self.emit(
            EventType.STAGE_COMPLETED,
            stage=stage,
            progress=stage_end_progress(stage),
            stage_progress=100,
            message=message or f"{stage.value} 阶段完成",
        )

    async def stage_failed(
        self, stage: StageName, message: str, payload: dict[str, Any] | None = None
    ) -> None:
        data = {"status": "failed"}
        if payload:
            data.update(payload)
        await self.stage_progress(stage, 100, message=message, payload=data)

    async def slide_started(
        self, slide_id: str, slide_index: int, total_slides: int
    ) -> None:
        await self.emit(
            EventType.SLIDE_STARTED,
            stage=StageName.GENERATE,
            slide_id=slide_id,
            slide_index=slide_index,
            total_slides=total_slides,
            message=f"第 {slide_index} 页开始生成",
        )

    async def slide_preview_ready(
        self,
        slide_id: str,
        slide_index: int,
        artifact_url: str,
        *,
        total_slides: int | None = None,
    ) -> None:
        await self.emit(
            EventType.SLIDE_PREVIEW_READY,
            stage=StageName.GENERATE,
            slide_id=slide_id,
            slide_index=slide_index,
            total_slides=total_slides,
            artifact_url=artifact_url,
            message=f"第 {slide_index} 页预览已就绪",
        )

    async def slide_completed(
        self,
        slide_id: str,
        slide_index: int,
        *,
        total_slides: int | None = None,
    ) -> None:
        await self.emit(
            EventType.SLIDE_COMPLETED,
            stage=StageName.GENERATE,
            slide_id=slide_id,
            slide_index=slide_index,
            total_slides=total_slides,
            message=f"第 {slide_index} 页生成完成",
        )

    async def slide_failed(
        self,
        slide_id: str,
        slide_index: int,
        message: str,
        *,
        total_slides: int | None = None,
    ) -> None:
        await self.emit(
            EventType.SLIDE_FAILED,
            stage=StageName.GENERATE,
            slide_id=slide_id,
            slide_index=slide_index,
            total_slides=total_slides,
            message=message,
        )

    async def export_started(self) -> None:
        await self.emit(
            EventType.EXPORT_STARTED,
            stage=StageName.EXPORT,
            progress=stage_start_progress(StageName.EXPORT),
            stage_progress=0,
            message="开始导出",
        )

    async def export_completed(self, artifact_url: str) -> None:
        await self.emit(
            EventType.EXPORT_COMPLETED,
            stage=StageName.EXPORT,
            progress=stage_end_progress(StageName.EXPORT),
            stage_progress=100,
            artifact_url=artifact_url,
            message="导出完成",
        )

    async def export_failed(self, message: str) -> None:
        await self.emit(
            EventType.EXPORT_FAILED,
            stage=StageName.EXPORT,
            message=message,
        )

    async def tool_started(
        self,
        tool_name: str,
        *,
        stage: StageName | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        await self._tool_event("started", tool_name, stage=stage, payload=payload)

    async def tool_completed(
        self,
        tool_name: str,
        *,
        elapsed: float | None = None,
        stage: StageName | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        data = {"elapsed": elapsed} if elapsed is not None else {}
        if payload:
            data.update(payload)
        await self._tool_event("completed", tool_name, stage=stage, payload=data)

    async def tool_failed(
        self,
        tool_name: str,
        *,
        elapsed: float | None = None,
        stage: StageName | None = None,
        error: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        data = {"elapsed": elapsed, "error": error}
        if payload:
            data.update(payload)
        await self._tool_event("failed", tool_name, stage=stage, payload=data)

    async def _tool_event(
        self,
        status: str,
        tool_name: str,
        *,
        stage: StageName | None = None,
        payload: dict[str, Any] | None = None,
    ) -> None:
        data = {"kind": "tool", "tool_name": tool_name, "status": status}
        if payload:
            data.update({k: v for k, v in payload.items() if v is not None})
        await self.emit(
            EventType.STAGE_PROGRESS,
            stage=stage,
            message=f"工具 {tool_name} {status}",
            payload=data,
        )
