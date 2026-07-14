"""任务管理器 —— 任务生命周期、状态机、快照持久化、取消与恢复。

每个任务被包装为 ``asyncio.Task``，拥有独立工作区、EventBus 和状态快照。
AgentLoop 集成将在 Day 4-5 通过可选回调完成；当前提供占位执行器。
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from deeppresenter.server.models.artifacts import task_dir
from deeppresenter.server.models.events import (
    EventType,
    GenerationEvent,
    StageName,
    TaskStatus,
    validate_task_transition,
)
from deeppresenter.server.services.event_bus import EventBus

# 任务快照文件名
SNAPSHOT_FILE = "task.json"


class TaskSnapshot:
    """任务快照，记录任务的完整可恢复状态。

    每次状态变更时写入 ``workspace/<task_id>/task.json``。
    """

    def __init__(
        self,
        task_id: str,
        status: TaskStatus = TaskStatus.QUEUED,
        progress: float = 0.0,
        current_stage: Optional[StageName] = None,
        instruction: str = "",
        total_slides: int = 0,
        completed_slides: int = 0,
        failed_slides: int = 0,
        result_artifact: Optional[str] = None,
        error_message: Optional[str] = None,
        created_at: Optional[str] = None,
        updated_at: Optional[str] = None,
    ):
        self.task_id = task_id
        self.status = status
        self.progress = progress
        self.current_stage = current_stage
        self.instruction = instruction
        self.total_slides = total_slides
        self.completed_slides = completed_slides
        self.failed_slides = failed_slides
        self.result_artifact = result_artifact
        self.error_message = error_message
        now = datetime.now(timezone.utc).isoformat()
        self.created_at = created_at or now
        self.updated_at = updated_at or now

    def to_dict(self) -> Dict[str, Any]:
        """转为可 JSON 序列化的字典。"""
        return {
            "task_id": self.task_id,
            "status": self.status.value if isinstance(self.status, TaskStatus) else self.status,
            "progress": self.progress,
            "current_stage": self.current_stage.value if self.current_stage else None,
            "instruction": self.instruction,
            "total_slides": self.total_slides,
            "completed_slides": self.completed_slides,
            "failed_slides": self.failed_slides,
            "result_artifact": self.result_artifact,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskSnapshot":
        """从字典恢复快照。"""
        status = data.get("status", "queued")
        if isinstance(status, str):
            status = TaskStatus(status)
        stage = data.get("current_stage")
        if isinstance(stage, str) and stage:
            stage = StageName(stage)
        return cls(
            task_id=data["task_id"],
            status=status,
            progress=data.get("progress", 0.0),
            current_stage=stage,
            instruction=data.get("instruction", ""),
            total_slides=data.get("total_slides", 0),
            completed_slides=data.get("completed_slides", 0),
            failed_slides=data.get("failed_slides", 0),
            result_artifact=data.get("result_artifact"),
            error_message=data.get("error_message"),
            created_at=data.get("created_at"),
            updated_at=data.get("updated_at"),
        )


class TaskManager:
    """管理所有生成任务的生命周期。

    用法::

        manager = TaskManager(workspace_base)
        task_id = await manager.create(instruction="...")
        # ...
        await manager.cancel(task_id)
    """

    def __init__(self, workspace_base: Path) -> None:
        self.workspace_base = Path(workspace_base)
        self.workspace_base.mkdir(parents=True, exist_ok=True)
        # task_id → asyncio.Task（包装 AgentLoop 的协程）
        self._runners: Dict[str, asyncio.Task] = {}
        # task_id → EventBus
        self._buses: Dict[str, EventBus] = {}
        # task_id → TaskSnapshot
        self._snapshots: Dict[str, TaskSnapshot] = {}
        # task_id → asyncio.Event（取消信号）
        self._cancel_events: Dict[str, asyncio.Event] = {}

    # ── 任务创建 ──────────────────────────────────────────────

    async def create(
        self,
        instruction: str,
        attachments: Optional[list[str]] = None,
        num_pages: Optional[str] = None,
        enable_planner: bool = False,
        language: str = "zh",
    ) -> str:
        """创建新任务，返回 task_id。

        立即写入初始快照、创建 EventBus、发出 task.created 事件，
        然后异步启动任务执行器。
        """
        task_id = uuid.uuid4().hex[:8]
        workspace = task_dir(self.workspace_base, task_id)
        workspace.mkdir(parents=True, exist_ok=True)

        # 创建事件总线
        bus = EventBus(workspace)
        self._buses[task_id] = bus

        # 创建取消信号
        cancel_evt = asyncio.Event()
        self._cancel_events[task_id] = cancel_evt

        # 写入初始快照
        snapshot = TaskSnapshot(
            task_id=task_id,
            status=TaskStatus.QUEUED,
            instruction=instruction,
            total_slides=0,
        )
        self._snapshots[task_id] = snapshot
        self._save_snapshot(workspace, snapshot)

        # 发出 task.created
        await bus.publish(
            GenerationEvent(
                task_id=task_id,
                seq=1,  # EventBus.publish() 会覆写为真实值
                type=EventType.TASK_CREATED,
                status=TaskStatus.QUEUED,
                progress=0.0,
                message=f"任务已创建：{instruction[:50]}",
                payload={
                    "instruction": instruction,
                    "language": language,
                    "num_pages": num_pages,
                    "attachments": attachments or [],
                },
            )
        )

        # 异步启动任务执行（当前为占位，Day 4-5 对接 AgentLoop）
        runner = asyncio.create_task(
            self._run_placeholder(
                task_id=task_id,
                instruction=instruction,
            )
        )
        self._runners[task_id] = runner

        return task_id

    # ── 任务查询 ──────────────────────────────────────────────

    def get_snapshot(self, task_id: str) -> Optional[TaskSnapshot]:
        """返回任务当前快照，不存在时返回 None。"""
        return self._snapshots.get(task_id)

    def get_event_bus(self, task_id: str) -> Optional[EventBus]:
        """返回任务的事件总线。"""
        return self._buses.get(task_id)

    def get_cancel_event(self, task_id: str) -> Optional[asyncio.Event]:
        """返回任务的取消信号。"""
        return self._cancel_events.get(task_id)

    def list_tasks(self) -> list[TaskSnapshot]:
        """列出所有已知任务的快照。"""
        return list(self._snapshots.values())

    # ── 任务取消 ──────────────────────────────────────────────

    async def cancel(self, task_id: str) -> bool:
        """取消任务。返回 True 表示成功，False 表示任务不存在或已处于终态。"""
        snapshot = self._snapshots.get(task_id)
        if snapshot is None:
            return False

        # 终态不可取消
        if snapshot.status in (TaskStatus.SUCCEEDED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            return False

        if not validate_task_transition(snapshot.status, TaskStatus.CANCELLED):
            return False

        # 更新状态
        self._transition_to(task_id, TaskStatus.CANCELLED, message="任务已取消，已完成页面保留")

        # 通知取消
        cancel_evt = self._cancel_events.get(task_id)
        if cancel_evt:
            cancel_evt.set()

        # 取消 asyncio 任务
        runner = self._runners.get(task_id)
        if runner and not runner.done():
            runner.cancel()

        # 关闭事件总线
        bus = self._buses.get(task_id)
        if bus:
            await bus.close()

        return True

    # ── 快照持久化 ────────────────────────────────────────────

    def _save_snapshot(self, workspace: Path, snapshot: TaskSnapshot) -> None:
        """将快照写入磁盘。"""
        path = workspace / SNAPSHOT_FILE
        with open(path, "w", encoding="utf-8") as f:
            json.dump(snapshot.to_dict(), f, ensure_ascii=False, indent=2)

    def _transition_to(
        self,
        task_id: str,
        new_status: TaskStatus,
        **kwargs,
    ) -> None:
        """更新内存快照并落盘。"""
        snapshot = self._snapshots.get(task_id)
        if snapshot is None:
            return
        snapshot.status = new_status
        snapshot.updated_at = datetime.now(timezone.utc).isoformat()
        for key, value in kwargs.items():
            if hasattr(snapshot, key):
                setattr(snapshot, key, value)
        workspace = task_dir(self.workspace_base, task_id)
        self._save_snapshot(workspace, snapshot)

    # ── 占位执行器（Day 4-5 替换为真实 AgentLoop）────────────

    async def _run_placeholder(self, task_id: str, instruction: str) -> None:
        """占位任务执行器 —— 模拟阶段推进，后续替换为 AgentLoop。

        当前仅用于验证 TaskManager 和 EventBus 的集成链路。
        """
        bus = self._buses.get(task_id)
        cancel_evt = self._cancel_events.get(task_id)
        if bus is None:
            return

        try:
            # task.started
            self._transition_to(task_id, TaskStatus.RUNNING)
            await bus.publish(
                GenerationEvent(
                    task_id=task_id,
                    seq=1,
                    type=EventType.TASK_STARTED,
                    status=TaskStatus.RUNNING,
                    progress=0.0,
                    message="任务开始执行（占位模式）",
                )
            )

            # 模拟各个阶段
            stages = [
                (StageName.PREPARE, 0.0, 5.0, "准备任务环境"),
                (StageName.PLAN, 5.0, 15.0, "规划大纲"),
                (StageName.RESEARCH, 15.0, 40.0, "收集资料并生成稿件"),
                (StageName.GENERATE, 40.0, 90.0, "逐页生成幻灯片"),
                (StageName.EXPORT, 90.0, 100.0, "导出最终文件"),
            ]

            for stage, progress_start, progress_end, msg in stages:
                if cancel_evt and cancel_evt.is_set():
                    return

                # stage.started
                self._snapshots[task_id].current_stage = stage
                await bus.publish(
                    GenerationEvent(
                        task_id=task_id,
                        seq=1,
                        type=EventType.STAGE_STARTED,
                        stage=stage,
                        progress=progress_start,
                        message=msg,
                    )
                )

                # 模拟该阶段的执行耗时
                await asyncio.sleep(0.1)

                if cancel_evt and cancel_evt.is_set():
                    return

                # stage.completed
                await bus.publish(
                    GenerationEvent(
                        task_id=task_id,
                        seq=1,
                        type=EventType.STAGE_COMPLETED,
                        stage=stage,
                        progress=progress_end,
                        message=f"{msg}——完成",
                    )
                )

            # task.completed
            self._transition_to(
                task_id,
                TaskStatus.SUCCEEDED,
                progress=100.0,
                result_artifact="exports/latest.pptx",
            )
            await bus.publish(
                GenerationEvent(
                    task_id=task_id,
                    seq=1,
                    type=EventType.TASK_COMPLETED,
                    status=TaskStatus.SUCCEEDED,
                    progress=100.0,
                    message="任务完成",
                    artifact_url="exports/latest.pptx",
                )
            )

        except asyncio.CancelledError:
            # 被取消 —— 状态已在 cancel() 中设置
            pass
        except Exception as exc:
            # 执行失败
            self._transition_to(
                task_id,
                TaskStatus.FAILED,
                error_message=str(exc),
            )
            await bus.publish(
                GenerationEvent(
                    task_id=task_id,
                    seq=1,
                    type=EventType.TASK_FAILED,
                    status=TaskStatus.FAILED,
                    message=f"任务执行失败：{exc}",
                )
            )
        finally:
            if bus and not bus._closed:
                await bus.close()
