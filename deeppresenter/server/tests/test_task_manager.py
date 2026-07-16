"""TaskManager 单元测试 —— 任务创建、状态转换、快照、取消。

运行方式:
    DEEPPRESENTER_SKIP_POSIX=1 pytest deeppresenter/server/tests/test_task_manager.py -v --asyncio-mode=auto
"""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from deeppresenter.server.models.artifacts import task_dir
from deeppresenter.server.models.events import (
    EventType,
    GenerationEvent,
    StageName,
    TaskStatus,
)
from deeppresenter.server.services.event_bus import EventBus
from deeppresenter.server.services.event_reporter import EventReporter
from deeppresenter.server.services.task_manager import (
    SNAPSHOT_FILE,
    TaskManager,
    TaskSnapshot,
)
from deeppresenter.server.services.event_reporter import EventReporter
from deeppresenter.server.services.preview import PreviewService


@pytest.fixture
def tmp_workspace() -> Path:
    tmp = tempfile.mkdtemp(prefix="taskmgr_test_")
    yield Path(tmp)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


class RecordingRetryManager(TaskManager):
    """TaskManager test double that records retry runner parameters."""

    def __init__(self, workspace_base: Path):
        super().__init__(workspace_base, use_placeholder=True)
        self.retry_calls = []

    async def _run_agent_loop(self, **kwargs):
        self.retry_calls.append(kwargs)


# ═══════════════════════════════════════════════════════════════════════
# TaskSnapshot
# ═══════════════════════════════════════════════════════════════════════


class TestTaskSnapshot:
    def test_defaults(self):
        s = TaskSnapshot(task_id="abc", instruction="test")
        assert s.task_id == "abc"
        assert s.status == TaskStatus.QUEUED
        assert s.progress == 0.0
        assert s.total_slides == 0

    def test_to_dict_and_back(self):
        s = TaskSnapshot(
            task_id="abc",
            status=TaskStatus.RUNNING,
            progress=42.0,
            current_stage=StageName.RESEARCH,
            instruction="测试任务",
            total_slides=8,
            completed_slides=3,
            failed_slides=1,
        )
        d = s.to_dict()
        restored = TaskSnapshot.from_dict(d)
        assert restored.task_id == "abc"
        assert restored.status == TaskStatus.RUNNING
        assert restored.progress == 42.0
        assert restored.current_stage == StageName.RESEARCH
        assert restored.completed_slides == 3

    def test_from_dict_handles_string_status(self):
        """从 JSON 反序列化时 status 和 stage 可能是字符串。"""
        d = {
            "task_id": "abc",
            "status": "running",
            "current_stage": "generate",
            "instruction": "test",
        }
        s = TaskSnapshot.from_dict(d)
        assert s.status == TaskStatus.RUNNING
        assert s.current_stage == StageName.GENERATE

    def test_from_dict_accepts_legacy_succeeded_status(self):
        """旧工作区中的 succeeded 快照应恢复为对外统一的 completed。"""
        s = TaskSnapshot.from_dict({"task_id": "abc", "status": "succeeded"})

        assert s.status == TaskStatus.COMPLETED
        assert s.to_dict()["status"] == "completed"


# ═══════════════════════════════════════════════════════════════════════
# TaskManager 创建
# ═══════════════════════════════════════════════════════════════════════


class TestTaskCreate:
    @pytest.mark.asyncio
    async def test_create_returns_task_id(self, tmp_workspace):
        manager = TaskManager(tmp_workspace)
        task_id = await manager.create(instruction="测试任务")
        assert len(task_id) == 8
        assert manager.get_snapshot(task_id) is not None

    @pytest.mark.asyncio
    async def test_create_emits_task_created_event(self, tmp_workspace):
        manager = TaskManager(tmp_workspace)
        task_id = await manager.create(instruction="测试任务")

        bus = manager.get_event_bus(task_id)
        assert bus is not None
        # 回放 events.jsonl 确认有 task.created
        events = list(bus._replay(0))
        assert len(events) >= 1
        assert events[0]["type"] == EventType.TASK_CREATED.value

    @pytest.mark.asyncio
    async def test_create_exposes_c2_services(self, tmp_workspace):
        """任务创建后应能获取 EventReporter 和 PreviewService，供 AgentLoop 对接。"""
        manager = TaskManager(tmp_workspace)
        task_id = await manager.create(instruction="测试任务")

        assert isinstance(manager.get_event_reporter(task_id), EventReporter)
        assert isinstance(manager.get_preview_service(task_id), PreviewService)

    @pytest.mark.asyncio
    async def test_create_persists_snapshot(self, tmp_workspace):
        manager = TaskManager(tmp_workspace)
        task_id = await manager.create(instruction="测试任务")

        snap_path = task_dir(tmp_workspace, task_id) / SNAPSHOT_FILE
        assert snap_path.exists()
        data = json.loads(snap_path.read_text(encoding="utf-8"))
        assert data["task_id"] == task_id
        assert data["status"] == "queued"

    @pytest.mark.asyncio
    async def test_create_starts_runner(self, tmp_workspace):
        """占位执行器应在创建任务后异步启动。"""
        manager = TaskManager(tmp_workspace)
        task_id = await manager.create(instruction="测试任务")
        # 给一点时间让占位执行器跑完
        await asyncio.sleep(0.5)

        snapshot = manager.get_snapshot(task_id)
        assert snapshot is not None
        # 占位执行器最终会走到 completed
        assert snapshot.status in (TaskStatus.RUNNING, TaskStatus.COMPLETED)

    @pytest.mark.asyncio
    async def test_real_runner_receives_generation_options(self, tmp_workspace):
        """真实执行器模式应完整接收 API 传入的生成参数。"""

        class RecordingTaskManager(TaskManager):
            def __init__(self, workspace_base: Path):
                super().__init__(workspace_base, use_placeholder=False)
                self.calls = []

            async def _run_agent_loop(self, **kwargs):
                self.calls.append(kwargs)

        manager = RecordingTaskManager(tmp_workspace)
        task_id = await manager.create(
            instruction="测试任务",
            attachments=["/tmp/input.pdf"],
            num_pages="6",
            powerpoint_type="4:3",
            template="template-1",
            convert_type="pptagent",
            enable_planner=True,
            language="zh",
        )
        await manager._runners[task_id]

        assert manager.calls == [
            {
                "task_id": task_id,
                "instruction": "测试任务",
                "attachments": ["/tmp/input.pdf"],
                "num_pages": "6",
                "powerpoint_type": "4:3",
                "template": "template-1",
                "convert_type": "pptagent",
                "enable_planner": True,
                "language": "zh",
            }
        ]


# ═══════════════════════════════════════════════════════════════════════
# TaskManager 查询
# ═══════════════════════════════════════════════════════════════════════


class TestTaskQuery:
    @pytest.mark.asyncio
    async def test_get_snapshot_returns_none_for_unknown(self, tmp_workspace):
        manager = TaskManager(tmp_workspace)
        assert manager.get_snapshot("nonexistent") is None

    @pytest.mark.asyncio
    async def test_list_tasks(self, tmp_workspace):
        manager = TaskManager(tmp_workspace)
        id1 = await manager.create(instruction="任务一")
        id2 = await manager.create(instruction="任务二")

        tasks = manager.list_tasks()
        assert len(tasks) >= 2
        ids = {t.task_id for t in tasks}
        assert id1 in ids
        assert id2 in ids


# ═══════════════════════════════════════════════════════════════════════
# TaskManager 取消
# ═══════════════════════════════════════════════════════════════════════


class TestTaskCancel:
    @pytest.mark.asyncio
    async def test_cancel_running_task(self, tmp_workspace):
        manager = TaskManager(tmp_workspace)
        task_id = await manager.create(instruction="测试任务")
        # 等任务开始执行
        await asyncio.sleep(0.05)

        success = await manager.cancel(task_id)
        assert success

        snapshot = manager.get_snapshot(task_id)
        assert snapshot.status == TaskStatus.CANCELLED

    @pytest.mark.asyncio
    async def test_cancel_nonexistent_returns_false(self, tmp_workspace):
        manager = TaskManager(tmp_workspace)
        success = await manager.cancel("no_such_task")
        assert not success

    @pytest.mark.asyncio
    async def test_cancel_snapshot_persisted(self, tmp_workspace):
        manager = TaskManager(tmp_workspace)
        task_id = await manager.create(instruction="测试任务")
        await asyncio.sleep(0.05)
        await manager.cancel(task_id)

        snap_path = task_dir(tmp_workspace, task_id) / SNAPSHOT_FILE
        data = json.loads(snap_path.read_text(encoding="utf-8"))
        assert data["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_stops_placeholder_runner(self, tmp_workspace):
        """取消后占位执行器应立即停止，不再推进阶段。"""
        manager = TaskManager(tmp_workspace)
        task_id = await manager.create(instruction="测试任务")

        # 立即取消
        await manager.cancel(task_id)
        await asyncio.sleep(0.3)

        # 阶段应停止在 prepare 或之前，不会到 completed
        snapshot = manager.get_snapshot(task_id)
        assert snapshot.status == TaskStatus.CANCELLED


# ═══════════════════════════════════════════════════════════════════════
# TaskManager 并发
# ═══════════════════════════════════════════════════════════════════════


class TestConcurrency:
    @pytest.mark.asyncio
    async def test_two_tasks_independent(self, tmp_workspace):
        """两个任务应使用独立的工作区和 EventBus。"""
        manager = TaskManager(tmp_workspace)

        id1 = await manager.create(instruction="任务一")
        id2 = await manager.create(instruction="任务二")

        assert id1 != id2
        assert task_dir(tmp_workspace, id1) != task_dir(tmp_workspace, id2)

        bus1 = manager.get_event_bus(id1)
        bus2 = manager.get_event_bus(id2)
        assert bus1 is not bus2

    @pytest.mark.asyncio
    async def test_cancel_one_does_not_affect_other(self, tmp_workspace):
        manager = TaskManager(tmp_workspace)
        id1 = await manager.create(instruction="任务一")
        id2 = await manager.create(instruction="任务二")

        await asyncio.sleep(0.05)
        await manager.cancel(id1)

        # 任务二不受影响
        snap2 = manager.get_snapshot(id2)
        assert snap2.status != TaskStatus.CANCELLED


# ═══════════════════════════════════════════════════════════════════════
# TaskSnapshot completed_slide_ids
# ═══════════════════════════════════════════════════════════════════════


class TestTaskSnapshotSlideIds:
    def test_default_empty_list(self):
        s = TaskSnapshot(task_id="abc")
        assert s.completed_slide_ids == []

    def test_roundtrip_with_slide_ids(self):
        s = TaskSnapshot(
            task_id="abc",
            completed_slide_ids=["sld-aaa", "sld-bbb"],
            completed_slides=2,
        )
        d = s.to_dict()
        restored = TaskSnapshot.from_dict(d)
        assert restored.completed_slide_ids == ["sld-aaa", "sld-bbb"]
        assert restored.completed_slides == 2


# ═══════════════════════════════════════════════════════════════════════
# TaskManager 重试
# ═══════════════════════════════════════════════════════════════════════


class TestTaskRetry:
    @pytest.mark.asyncio
    async def test_retry_failed_task(self, tmp_workspace):
        """FAILED 任务可以重试并转为 RUNNING。"""
        manager = RecordingRetryManager(tmp_workspace)
        task_id = await manager.create(instruction="test")

        # 手动设为 FAILED
        manager._transition_to(task_id, TaskStatus.FAILED,
                               error_message="模拟失败")
        # 需要重建 bus（placeholder runner 完成后 bus 已关闭）
        workspace = task_dir(tmp_workspace, task_id)
        manager._buses[task_id] = EventBus(workspace)
        manager._reporters[task_id] = EventReporter(task_id, manager._buses[task_id].publish)

        success = await manager.retry(task_id)
        assert success
        snap = manager.get_snapshot(task_id)
        assert snap.status == TaskStatus.RUNNING
        await manager._runners[task_id]
        assert manager.retry_calls[0]["task_id"] == task_id

    @pytest.mark.asyncio
    async def test_retry_non_failed_returns_false(self, tmp_workspace):
        manager = TaskManager(tmp_workspace, use_placeholder=True)
        task_id = await manager.create(instruction="test")
        await asyncio.sleep(0.5)  # 等待 placeholder 完成

        success = await manager.retry(task_id)
        assert not success  # completed 不可重试

    @pytest.mark.asyncio
    async def test_retry_nonexistent_returns_false(self, tmp_workspace):
        manager = TaskManager(tmp_workspace)
        success = await manager.retry("no_such_task")
        assert not success


# ═══════════════════════════════════════════════════════════════════════
# TaskManager 导出
# ═══════════════════════════════════════════════════════════════════════


class TestTaskExport:
    @pytest.mark.asyncio
    async def test_export_with_artifact(self, tmp_workspace):
        """导出应返回快照中的 result_artifact（文件在磁盘上存在时）。"""
        task_id = "exp00001"
        workspace = task_dir(tmp_workspace, task_id)
        exports_dir = workspace / "exports"
        exports_dir.mkdir(parents=True)
        (exports_dir / "latest.pptx").write_bytes(b"pptx-data")

        manager = TaskManager(tmp_workspace, use_placeholder=True)
        snap = TaskSnapshot(
            task_id=task_id, status=TaskStatus.COMPLETED,
            progress=100.0, result_artifact="exports/latest.pptx",
        )
        manager._snapshots[task_id] = snap
        manager._buses[task_id] = EventBus(workspace)
        manager._reporters[task_id] = EventReporter(task_id, manager._buses[task_id].publish)

        artifact = await manager.export(task_id)
        assert artifact == "exports/latest.pptx"

    @pytest.mark.asyncio
    async def test_export_pdf_prefers_matching_sibling(self, tmp_workspace):
        """请求 PDF 时应返回同 stem 的 PDF，而不是把 PPTX 伪装成 PDF。"""
        task_id = "exp00002"
        workspace = task_dir(tmp_workspace, task_id)
        workspace.mkdir(parents=True)
        (workspace / "manuscript.pptx").write_bytes(b"pptx-data")
        (workspace / "manuscript.pdf").write_bytes(b"%PDF-data")

        manager = TaskManager(tmp_workspace, use_placeholder=True)
        snap = TaskSnapshot(
            task_id=task_id, status=TaskStatus.COMPLETED,
            progress=100.0, result_artifact="manuscript.pptx",
        )
        manager._snapshots[task_id] = snap
        manager._buses[task_id] = EventBus(workspace)
        manager._reporters[task_id] = EventReporter(task_id, manager._buses[task_id].publish)

        artifact = await manager.export(task_id, fmt="pdf")
        assert artifact == "manuscript.pdf"

    @pytest.mark.asyncio
    async def test_export_nonexistent_returns_none(self, tmp_workspace):
        manager = TaskManager(tmp_workspace)
        artifact = await manager.export("no_such_task")
        assert artifact is None


# ═══════════════════════════════════════════════════════════════════════
# TaskManager 启动恢复
# ═══════════════════════════════════════════════════════════════════════


class TestRestoreSnapshots:
    @pytest.mark.asyncio
    async def test_restore_loads_persisted_tasks(self, tmp_workspace):
        """从磁盘恢复已写入的快照。"""
        task_id = "restore01"
        workspace = task_dir(tmp_workspace, task_id)
        workspace.mkdir(parents=True, exist_ok=True)
        snap = TaskSnapshot(task_id=task_id, status=TaskStatus.COMPLETED,
                            instruction="已完成任务", progress=100.0,
                            result_artifact="exports/latest.pptx")
        (workspace / SNAPSHOT_FILE).write_text(
            json.dumps(snap.to_dict(), ensure_ascii=False), encoding="utf-8")

        restored = await TaskManager.restore_snapshots(tmp_workspace)
        assert task_id in restored._snapshots
        assert restored._snapshots[task_id].status == TaskStatus.COMPLETED
        assert restored._snapshots[task_id].instruction == "已完成任务"

    @pytest.mark.asyncio
    async def test_orphan_running_marked_failed(self, tmp_workspace):
        """如果快照 status=running，恢复时应该标记为 failed。"""
        task_id = "orphan01"
        workspace = task_dir(tmp_workspace, task_id)
        workspace.mkdir(parents=True, exist_ok=True)
        snap = TaskSnapshot(task_id=task_id, status=TaskStatus.RUNNING,
                            instruction="orphan task")
        snap_path = workspace / SNAPSHOT_FILE
        snap_path.write_text(json.dumps(snap.to_dict(), ensure_ascii=False),
                             encoding="utf-8")

        restored = await TaskManager.restore_snapshots(tmp_workspace)
        assert task_id in restored._snapshots
        assert restored._snapshots[task_id].status == TaskStatus.FAILED
        assert "异常重启" in (restored._snapshots[task_id].error_message or "")

    @pytest.mark.asyncio
    async def test_empty_workspace(self, tmp_workspace):
        restored = await TaskManager.restore_snapshots(tmp_workspace)
        assert len(restored._snapshots) == 0


# ═══════════════════════════════════════════════════════════════════════
# TaskManager 重试——参数保留
# ═══════════════════════════════════════════════════════════════════════


class TestRetryParams:
    @pytest.mark.asyncio
    async def test_snapshot_stores_generation_params(self, tmp_workspace):
        """create() 时应把全部生成参数写入 generation_params。"""
        manager = TaskManager(tmp_workspace, use_placeholder=True)
        task_id = await manager.create(
            instruction="模板测试",
            num_pages="5",
            powerpoint_type="4:3",
            template="acme",
            convert_type="pptagent",
            enable_planner=True,
            language="zh",
        )
        snap = manager.get_snapshot(task_id)
        assert snap is not None
        params = snap.generation_params
        assert params["instruction"] == "模板测试"
        assert params["num_pages"] == "5"
        assert params["powerpoint_type"] == "4:3"
        assert params["template"] == "acme"
        assert params["convert_type"] == "pptagent"
        assert params["enable_planner"] is True
        assert params["language"] == "zh"

    @pytest.mark.asyncio
    async def test_retry_restores_params(self, tmp_workspace):
        """retry() 应使用 generation_params 而非硬编码默认值。"""
        manager = RecordingRetryManager(tmp_workspace)
        task_id = await manager.create(
            instruction="retry 参数测试",
            num_pages="8",
        )
        # 手动设为 FAILED
        manager._transition_to(task_id, TaskStatus.FAILED, error_message="失败")
        workspace = task_dir(tmp_workspace, task_id)
        manager._buses[task_id] = EventBus(workspace)
        manager._reporters[task_id] = EventReporter(task_id, manager._buses[task_id].publish)
        params_before = manager.get_snapshot(task_id).generation_params

        await manager.retry(task_id)
        params_after = manager.get_snapshot(task_id).generation_params
        assert params_after == params_before
        assert params_after["num_pages"] == "8"
        await manager._runners[task_id]
        assert manager.retry_calls[0]["instruction"] == "retry 参数测试"
        assert manager.retry_calls[0]["num_pages"] == "8"

    @pytest.mark.asyncio
    async def test_retry_clears_error_message(self, tmp_workspace):
        """重试后 error_message 应清空。"""
        manager = RecordingRetryManager(tmp_workspace)
        task_id = await manager.create(instruction="test")
        manager._transition_to(task_id, TaskStatus.FAILED, error_message="旧错误")
        workspace = task_dir(tmp_workspace, task_id)
        manager._buses[task_id] = EventBus(workspace)
        manager._reporters[task_id] = EventReporter(task_id, manager._buses[task_id].publish)

        await manager.retry(task_id)
        snap = manager.get_snapshot(task_id)
        assert snap.error_message is None
        await manager._runners[task_id]


# ═══════════════════════════════════════════════════════════════════════
# TaskManager 导出——文件存在性
# ═══════════════════════════════════════════════════════════════════════


class TestExportRobustness:
    @pytest.mark.asyncio
    async def test_export_artifact_not_on_disk_falls_back(self, tmp_workspace):
        """快照有 artifact 但磁盘上不存在，应扫描 exports/ 目录。"""
        task_id = "exp01"
        workspace = task_dir(tmp_workspace, task_id)
        exports_dir = workspace / "exports"
        exports_dir.mkdir(parents=True)
        fake_pptx = exports_dir / "output.pptx"
        fake_pptx.write_bytes(b"fake-pptx-data")

        manager = TaskManager(tmp_workspace, use_placeholder=True)
        snap = TaskSnapshot(
            task_id=task_id, status=TaskStatus.COMPLETED,
            result_artifact="exports/missing.pptx",  # 不存在
        )
        manager._snapshots[task_id] = snap
        manager._buses[task_id] = EventBus(workspace)
        manager._reporters[task_id] = EventReporter(task_id, manager._buses[task_id].publish)

        artifact = await manager.export(task_id)
        assert artifact is not None
        assert artifact.endswith("output.pptx")

    @pytest.mark.asyncio
    async def test_export_no_artifact_returns_none(self, tmp_workspace):
        """既无 result_artifact 也无 exports/，返回 None。"""
        task_id = "exp02"
        workspace = task_dir(tmp_workspace, task_id)
        workspace.mkdir(parents=True, exist_ok=True)

        manager = TaskManager(tmp_workspace, use_placeholder=True)
        snap = TaskSnapshot(task_id=task_id, status=TaskStatus.COMPLETED)
        manager._snapshots[task_id] = snap
        manager._buses[task_id] = EventBus(workspace)
        manager._reporters[task_id] = EventReporter(task_id, manager._buses[task_id].publish)

        artifact = await manager.export(task_id)
        assert artifact is None


# ═══════════════════════════════════════════════════════════════════════
# TaskManager 并发控制
# ═══════════════════════════════════════════════════════════════════════


class TestConcurrencyLimit:
    @pytest.mark.asyncio
    async def test_semaphore_limits_concurrent_runners(self, tmp_workspace):
        """max_concurrent=1 时第二个任务排队，最终两者都完成。"""
        manager = TaskManager(tmp_workspace, use_placeholder=True, max_concurrent=1)

        id1 = await manager.create(instruction="任务一")
        id2 = await manager.create(instruction="任务二")

        # 两个任务都已创建（QUEUED 状态）
        assert manager.get_snapshot(id1) is not None
        assert manager.get_snapshot(id2) is not None

        # max_concurrent=1 加 Semaphore 排队，两个任务最终都应完成
        await asyncio.sleep(1.2)
        snap1 = manager.get_snapshot(id1)
        snap2 = manager.get_snapshot(id2)
        assert snap1.status == TaskStatus.COMPLETED
        assert snap2.status == TaskStatus.COMPLETED

    @pytest.mark.asyncio
    async def test_max_concurrent_notation(self, tmp_workspace):
        """验证 max_concurrent 通过创建信号量生效。"""
        manager = TaskManager(tmp_workspace, max_concurrent=3)
        assert manager._concurrency_sem._value == 3

        manager2 = TaskManager(tmp_workspace, max_concurrent=1)
        assert manager2._concurrency_sem._value == 1
