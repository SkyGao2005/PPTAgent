from __future__ import annotations
"""任务管理器 —— 任务生命周期、状态机、快照持久化、取消与恢复。

每个任务被包装为 ``asyncio.Task``，拥有独立工作区、EventBus 和状态快照。
FastAPI 服务默认运行真实 AgentLoop；测试可显式启用占位执行器。
"""

import asyncio
import json
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Literal, Optional

from deeppresenter.server.models.artifacts import task_dir
from deeppresenter.server.models.events import (
    StageName,
    TaskStatus,
    validate_task_transition,
)
from deeppresenter.server.services.event_bus import EventBus
from deeppresenter.server.services.event_reporter import EventReporter
from deeppresenter.server.services.preview import PreviewService

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
        completed_slide_ids: Optional[list[str]] = None,
        generation_params: Optional[Dict[str, Any]] = None,
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
        self.completed_slide_ids = completed_slide_ids or []
        self.generation_params = generation_params or {}
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
            "completed_slide_ids": self.completed_slide_ids,
            "generation_params": self.generation_params,
            "result_artifact": self.result_artifact,
            "error_message": self.error_message,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "TaskSnapshot":
        """从字典恢复快照。"""
        status = data.get("status", "queued")
        if status == "succeeded":
            status = "completed"
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
            completed_slide_ids=data.get("completed_slide_ids") or [],
            generation_params=data.get("generation_params") or {},
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

    def __init__(
        self,
        workspace_base: Path,
        *,
        use_placeholder: bool = True,
        config_path: Optional[str] = None,
        max_concurrent: int = 2,
    ) -> None:
        self.workspace_base = Path(workspace_base)
        self.workspace_base.mkdir(parents=True, exist_ok=True)
        self.use_placeholder = use_placeholder
        self.config_path = config_path
        # 并发控制
        self._concurrency_sem = asyncio.Semaphore(max_concurrent)
        # task_id → asyncio.Task（包装 AgentLoop 的协程）
        self._runners: Dict[str, asyncio.Task] = {}
        # task_id → EventBus
        self._buses: Dict[str, EventBus] = {}
        # task_id → EventReporter
        self._reporters: Dict[str, EventReporter] = {}
        # task_id → PreviewService
        self._preview_services: Dict[str, PreviewService] = {}
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
        powerpoint_type: str = "16:9",
        template: Optional[str] = None,
        convert_type: Optional[str] = None,
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
        reporter = EventReporter(task_id, bus.publish)
        self._reporters[task_id] = reporter
        preview_service = PreviewService(self.workspace_base)
        self._preview_services[task_id] = preview_service

        # 创建取消信号
        cancel_evt = asyncio.Event()
        self._cancel_events[task_id] = cancel_evt

        # 写入初始快照
        params = {
            "instruction": instruction,
            "attachments": attachments or [],
            "num_pages": num_pages,
            "powerpoint_type": powerpoint_type,
            "template": template,
            "convert_type": convert_type,
            "enable_planner": enable_planner,
            "language": language,
        }
        snapshot = TaskSnapshot(
            task_id=task_id,
            status=TaskStatus.QUEUED,
            instruction=instruction,
            total_slides=0,
            generation_params=params,
        )
        self._snapshots[task_id] = snapshot
        self._save_snapshot(workspace, snapshot)

        await reporter.task_created(
            message=f"任务已创建：{instruction[:50]}",
            payload={
                "instruction": instruction,
                "language": language,
                "num_pages": num_pages,
                "powerpoint_type": powerpoint_type,
                "template": template,
                "convert_type": convert_type,
                "attachments": attachments or [],
            },
        )

        async def _runner_with_semaphore():
            async with self._concurrency_sem:
                if self.use_placeholder:
                    await self._run_placeholder(
                        task_id=task_id,
                        instruction=instruction,
                    )
                else:
                    await self._run_agent_loop(
                        task_id=task_id,
                        instruction=instruction,
                        attachments=attachments or [],
                        num_pages=num_pages,
                        powerpoint_type=powerpoint_type,
                        template=template,
                        convert_type=convert_type,
                        enable_planner=enable_planner,
                        language=language,
                    )

        runner = asyncio.create_task(_runner_with_semaphore())
        self._runners[task_id] = runner

        return task_id

    # ── 任务查询 ──────────────────────────────────────────────

    def get_snapshot(self, task_id: str) -> Optional[TaskSnapshot]:
        """返回任务当前快照，不存在时返回 None。"""
        return self._snapshots.get(task_id)

    def get_event_bus(self, task_id: str) -> Optional[EventBus]:
        """返回任务的事件总线。"""
        return self._buses.get(task_id)

    def get_event_reporter(self, task_id: str) -> Optional[EventReporter]:
        """返回任务的事件发布辅助器。"""
        return self._reporters.get(task_id)

    def get_preview_service(self, task_id: str) -> Optional[PreviewService]:
        """返回任务的预览服务。"""
        return self._preview_services.get(task_id)

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
        if snapshot.status in (TaskStatus.COMPLETED, TaskStatus.FAILED, TaskStatus.CANCELLED):
            return False

        if not validate_task_transition(snapshot.status, TaskStatus.CANCELLED):
            return False

        # 更新状态
        self._transition_to(task_id, TaskStatus.CANCELLED, message="任务已取消，已完成页面保留")
        reporter = self._reporters.get(task_id)
        if reporter:
            await reporter.task_cancelled("任务已取消，已完成页面保留")

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

    # ── 任务重试 ──────────────────────────────────────────────

    async def retry(self, task_id: str, retry_failed_slides_only: bool = True) -> bool:
        """重试失败的任务。

        Args:
            task_id: 任务 ID
            retry_failed_slides_only: True 时跳过已完成的页面

        Returns:
            True 表示重试已启动，False 表示任务不存在或状态不允许重试
        """
        snapshot = self._snapshots.get(task_id)
        if snapshot is None:
            return False
        if snapshot.status != TaskStatus.FAILED:
            return False
        if not validate_task_transition(TaskStatus.FAILED, TaskStatus.RUNNING):
            return False

        bus = self._buses.get(task_id)
        reporter = self._reporters.get(task_id)
        if bus is None or reporter is None:
            return False

        # 重建 EventBus 和取消信号
        workspace = task_dir(self.workspace_base, task_id)
        if bus._closed:
            bus = EventBus(workspace)
            self._buses[task_id] = bus
            reporter = EventReporter(task_id, bus.publish)
            self._reporters[task_id] = reporter
        cancel_evt = asyncio.Event()
        self._cancel_events[task_id] = cancel_evt

        self._transition_to(task_id, TaskStatus.RUNNING, progress=snapshot.progress,
                            error_message=None)
        await reporter.task_started(
            f"任务重新执行{'（跳过已完成页面）' if retry_failed_slides_only else ''}"
        )

        # 从快照恢复原始生成参数
        params = snapshot.generation_params
        skip_ids = snapshot.completed_slide_ids if retry_failed_slides_only else []
        async def _retry_with_semaphore():
            async with self._concurrency_sem:
                await self._run_agent_loop(
                    task_id=task_id,
                    instruction=params.get("instruction", snapshot.instruction),
                    attachments=params.get("attachments") or [],
                    num_pages=params.get("num_pages"),
                    powerpoint_type=params.get("powerpoint_type", "16:9"),
                    template=params.get("template"),
                    convert_type=params.get("convert_type"),
                    enable_planner=params.get("enable_planner", False),
                    language=params.get("language", "zh"),
                    skip_slide_ids=skip_ids,
                )

        runner = asyncio.create_task(_retry_with_semaphore())
        self._runners[task_id] = runner
        return True

    # ── 任务导出 ──────────────────────────────────────────────

    async def export(self, task_id: str, fmt: Literal["pptx", "pdf"] = "pptx") -> Optional[str]:
        """触发导出，返回产物相对路径。

        先检查快照中记录的 result_artifact 是否在磁盘上存在，
        不存在则扫描任务根目录和 exports/ 目录，仍找不到则返回 None。
        """
        snapshot = self._snapshots.get(task_id)
        if snapshot is None:
            return None
        reporter = self._reporters.get(task_id)
        bus = self._buses.get(task_id)
        if reporter is None or bus is None:
            return None

        await reporter.export_started()
        try:
            root = task_dir(self.workspace_base, task_id)
            suffix = f".{fmt}"

            def _relative_if_exists(path: Path) -> Optional[str]:
                if path.exists() and path.is_file():
                    try:
                        return str(path.resolve().relative_to(root.resolve()))
                    except ValueError:
                        return None
                return None

            # 1. 检查快照记录的产物是否在磁盘上
            artifact = snapshot.result_artifact
            if artifact:
                artifact_path = root / artifact
                if artifact_path.suffix.lower() == suffix:
                    rel = _relative_if_exists(artifact_path)
                    if rel:
                        await reporter.export_completed(rel)
                        return rel

                sibling = artifact_path.with_suffix(suffix)
                rel = _relative_if_exists(sibling)
                if rel:
                    await reporter.export_completed(rel)
                    return rel

            # 2. 扫描任务根目录和 exports 目录
            search_dirs = [root, root / "exports"]
            for export_dir in search_dirs:
                if not export_dir.exists():
                    continue
                files = sorted(
                    export_dir.glob(f"*{suffix}"),
                    key=lambda p: p.stat().st_mtime,
                    reverse=True,
                )
                for file in files:
                    rel = _relative_if_exists(file)
                    if rel:
                        await reporter.export_completed(rel)
                        return rel

            await reporter.export_failed("未找到可导出产物")
            return None
        except Exception as exc:
            await reporter.export_failed(str(exc))
            return None

    # ── 启动恢复 ──────────────────────────────────────────────

    @classmethod
    async def restore_snapshots(cls, workspace_base: Path) -> "TaskManager":
        """扫描工作区目录，恢复已知任务快照。

        对状态为 running 的孤儿任务标记为 failed。
        """
        base = Path(workspace_base)
        manager = cls(base)

        if not base.exists():
            return manager

        for child in base.iterdir():
            if not child.is_dir():
                continue
            snap_path = child / SNAPSHOT_FILE
            if not snap_path.exists():
                continue
            try:
                data = json.loads(snap_path.read_text(encoding="utf-8"))
                snapshot = TaskSnapshot.from_dict(data)
                task_id = snapshot.task_id

                # 孤儿 running → failed
                if snapshot.status == TaskStatus.RUNNING:
                    snapshot.status = TaskStatus.FAILED
                    snapshot.error_message = "服务异常重启，任务标记为失败"
                    snapshot.updated_at = datetime.now(timezone.utc).isoformat()
                    manager._save_snapshot(child, snapshot)

                manager._snapshots[task_id] = snapshot
                # 重建 EventBus（从 events.jsonl 恢复 seq）
                bus = EventBus(child)
                manager._buses[task_id] = bus
                manager._reporters[task_id] = EventReporter(task_id, bus.publish)
                manager._preview_services[task_id] = PreviewService(base)
                manager._cancel_events[task_id] = asyncio.Event()
            except (OSError, json.JSONDecodeError, KeyError):
                continue

        return manager

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

    # ── 真实 AgentLoop 执行器 ───────────────────────────────

    async def _run_agent_loop(
        self,
        *,
        task_id: str,
        instruction: str,
        attachments: list[str],
        num_pages: Optional[str],
        powerpoint_type: str,
        template: Optional[str],
        convert_type: Optional[str],
        enable_planner: bool,
        language: str,
        skip_slide_ids: Optional[list[str]] = None,
    ) -> None:
        """运行真实 AgentLoop，并把最终结果写回任务快照。"""
        bus = self._buses.get(task_id)
        reporter = self._reporters.get(task_id)
        preview_service = self._preview_services.get(task_id)
        cancel_evt = self._cancel_events.get(task_id)
        if bus is None or reporter is None:
            return

        # retry 跳过已完成页：写入 AgentEnv 可读取的 skip 文件
        if skip_slide_ids:
            skip_file = task_dir(self.workspace_base, task_id) / ".retry_skip.json"
            skip_file.write_text(
                json.dumps(skip_slide_ids, ensure_ascii=False),
                encoding="utf-8",
            )

        try:
            from deeppresenter.main import AgentLoop
            from deeppresenter.utils.config import DeepPresenterConfig
            from deeppresenter.utils.typings import (
                ConvertType,
                InputRequest,
                PowerPointType,
            )

            config = DeepPresenterConfig.load_from_file(self.config_path)
            workspace = task_dir(self.workspace_base, task_id)
            request_convert_type = (
                ConvertType(convert_type)
                if convert_type
                else (
                    ConvertType.PPTAGENT
                    if template
                    else ConvertType.DEEPPRESENTER
                )
            )
            request = InputRequest(
                instruction=instruction,
                attachments=attachments,
                num_pages=num_pages,
                template=template,
                powerpoint_type=PowerPointType(powerpoint_type),
                convert_type=request_convert_type,
                enable_planner=enable_planner,
            )

            self._transition_to(task_id, TaskStatus.RUNNING, progress=0.0)
            await reporter.task_started("任务开始执行")

            loop = AgentLoop(
                config=config,
                session_id=task_id,
                workspace=workspace,
                language=language,
                event_reporter=reporter,
                preview_service=preview_service,
            )

            final_artifact: Optional[str] = None
            async for msg in loop.run(request):
                if cancel_evt and cancel_evt.is_set():
                    await reporter.task_cancelled("任务已取消，已完成页面保留")
                    self._transition_to(task_id, TaskStatus.CANCELLED,
                                        result_artifact=final_artifact)
                    return
                if isinstance(msg, (str, Path)):
                    final_artifact = self._artifact_path(task_id, Path(msg))

            # 取消后不再写 completed
            if cancel_evt and cancel_evt.is_set():
                return
            # 扫描已完成页面
            completed_ids = self._collect_completed_slide_ids(task_id)
            self._transition_to(
                task_id,
                TaskStatus.COMPLETED,
                progress=100.0,
                result_artifact=final_artifact,
                completed_slide_ids=completed_ids,
                completed_slides=len(completed_ids),
            )
            await reporter.task_completed(final_artifact, "任务完成")

        except asyncio.CancelledError:
            pass
        except Exception as exc:
            # 扫描已完成的页面，保留不丢失
            completed_ids = self._collect_completed_slide_ids(task_id)
            self._transition_to(
                task_id,
                TaskStatus.FAILED,
                error_message=str(exc),
                completed_slide_ids=completed_ids,
                completed_slides=len(completed_ids),
            )
            await reporter.task_failed(f"任务执行失败：{exc}")
        finally:
            if bus and not bus._closed:
                await bus.close()

    def _collect_completed_slide_ids(self, task_id: str) -> list[str]:
        """从 PreviewService 收集已完成的 slide_id 列表。"""
        preview_service = self._preview_services.get(task_id)
        if preview_service is None:
            return []
        try:
            slides = preview_service.list_slides(task_id)
            return [s.slide_id for s in slides]
        except Exception:
            return []

    def _artifact_path(self, task_id: str, path: Path) -> str:
        """把最终产物路径转换为任务工作区内相对路径。"""
        root = task_dir(self.workspace_base, task_id).resolve()
        try:
            return str(path.resolve().relative_to(root))
        except ValueError:
            return str(path)

    # ── 占位执行器 ────────────────────────────────────────────

    async def _run_placeholder(self, task_id: str, instruction: str) -> None:
        """占位任务执行器 —— 模拟阶段推进，后续替换为 AgentLoop。

        当前仅用于验证 TaskManager 和 EventBus 的集成链路。
        """
        bus = self._buses.get(task_id)
        cancel_evt = self._cancel_events.get(task_id)
        reporter = self._reporters.get(task_id)
        if bus is None or reporter is None:
            return

        try:
            # task.started
            self._transition_to(task_id, TaskStatus.RUNNING, progress=0.0)
            await reporter.task_started("任务开始执行（占位模式）")

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
                self._snapshots[task_id].progress = progress_start
                self._save_snapshot(task_dir(self.workspace_base, task_id), self._snapshots[task_id])
                await reporter.stage_started(stage, msg)

                # 模拟该阶段的执行耗时
                await asyncio.sleep(0.1)

                if cancel_evt and cancel_evt.is_set():
                    return

                # stage.completed
                self._snapshots[task_id].progress = progress_end
                self._save_snapshot(task_dir(self.workspace_base, task_id), self._snapshots[task_id])
                await reporter.stage_completed(stage, f"{msg}——完成")

            # task.completed
            self._transition_to(
                task_id,
                TaskStatus.COMPLETED,
                progress=100.0,
                result_artifact="exports/latest.pptx",
            )
            await reporter.task_completed("exports/latest.pptx", "任务完成")

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
            await reporter.task_failed(f"任务执行失败：{exc}")
        finally:
            if bus and not bus._closed:
                await bus.close()
