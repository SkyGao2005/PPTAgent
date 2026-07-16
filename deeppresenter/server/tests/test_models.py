"""服务层数据模型单元测试 —— 不依赖 LLM 或外部服务。

运行方式:
    DEEPPRESENTER_SKIP_POSIX=1 pytest deeppresenter/server/tests/test_models.py -v
"""

import json
import tempfile
from pathlib import Path

import pytest

from deeppresenter.server.models.artifacts import (
    MAX_REVISIONS,
    SlideArtifact,
    StructuredContent,
    is_path_safe,
    revision_dir,
    slide_dir,
    slides_dir,
    task_dir,
)
from deeppresenter.server.models.events import (
    STAGE_WEIGHTS,
    VALID_TASK_TRANSITIONS,
    EventType,
    GenerationEvent,
    StageName,
    TaskStatus,
    parse_events_from_jsonl,
    stage_end_progress,
    stage_start_progress,
    validate_task_transition,
)


# ═══════════════════════════════════════════════════════════════════════
# 任务状态机
# ═══════════════════════════════════════════════════════════════════════


class TestTaskStateMachine:
    def test_legal_transitions(self):
        """所有已文档化的合法转移必须通过校验。"""
        legal = [
            (TaskStatus.QUEUED, TaskStatus.RUNNING),
            (TaskStatus.QUEUED, TaskStatus.CANCELLED),
            (TaskStatus.RUNNING, TaskStatus.COMPLETED),
            (TaskStatus.RUNNING, TaskStatus.FAILED),
            (TaskStatus.RUNNING, TaskStatus.CANCELLED),
            (TaskStatus.FAILED, TaskStatus.RUNNING),  # 重试
        ]
        for current, target in legal:
            ok = validate_task_transition(current, target)
            assert ok, f"{current.value} -> {target.value} 应为合法转移"

    def test_illegal_transitions(self):
        """终态不能跳出，逆向转移必须拒绝。"""
        illegal = [
            (TaskStatus.COMPLETED, TaskStatus.RUNNING),
            (TaskStatus.CANCELLED, TaskStatus.RUNNING),
            (TaskStatus.COMPLETED, TaskStatus.FAILED),
            (TaskStatus.CANCELLED, TaskStatus.QUEUED),
            (TaskStatus.FAILED, TaskStatus.CANCELLED),
        ]
        for current, target in illegal:
            ok = validate_task_transition(current, target)
            assert not ok, f"{current.value} -> {target.value} 应为非法转移"

    def test_terminal_states_have_no_exits(self):
        """终态不应有任何出边。"""
        for state in (TaskStatus.COMPLETED, TaskStatus.CANCELLED):
            assert VALID_TASK_TRANSITIONS.get(state, set()) == set()


# ═══════════════════════════════════════════════════════════════════════
# 阶段进度计算
# ═══════════════════════════════════════════════════════════════════════


class TestStageProgress:
    def test_weights_sum_to_100(self):
        """各阶段权重总和必须为 100（EDIT 除外）。"""
        total = sum(STAGE_WEIGHTS[s] for s in StageName if s != StageName.EDIT)
        assert total == 100.0, f"阶段权重总和应为 100，实际为 {total}"

    def test_stage_boundaries_are_contiguous(self):
        """相邻阶段边界必须连续，不能有间隙。"""
        prev_end = 0.0
        for stage in (
            StageName.PREPARE,
            StageName.PLAN,
            StageName.RESEARCH,
            StageName.GENERATE,
            StageName.EXPORT,
        ):
            start = stage_start_progress(stage)
            end = stage_end_progress(stage)
            assert start == prev_end, (
                f"{stage.value}: start={start} 应等于前一阶段 end={prev_end}"
            )
            assert end == start + STAGE_WEIGHTS[stage], (
                f"{stage.value}: end={end} != start+weight={start + STAGE_WEIGHTS[stage]}"
            )
            prev_end = end

    def test_generate_progress_midpoint(self):
        """生成阶段基础 40%，完成 3/6 页 → 40 + 25 = 65%。"""
        base = stage_start_progress(StageName.GENERATE)  # 40
        weight = STAGE_WEIGHTS[StageName.GENERATE]        # 50
        done, total = 3, 6
        progress = base + weight * (done / total)
        assert progress == 65.0, f"期望 65.0，得到 {progress}"


# ═══════════════════════════════════════════════════════════════════════
# GenerationEvent 序列化 / 反序列化
# ═══════════════════════════════════════════════════════════════════════


class TestGenerationEvent:
    def test_minimal_event(self):
        """最简事件只需 task_id、seq、type。"""
        e = GenerationEvent(task_id="abc12345", seq=1, type=EventType.TASK_CREATED)
        assert e.task_id == "abc12345"
        assert e.seq == 1
        assert e.type == EventType.TASK_CREATED
        assert e.stage is None
        assert e.progress is None
        assert e.payload == {}

    def test_full_event_roundtrip(self):
        """完整事件序列化再反序列化，字段应对齐。"""
        e = GenerationEvent(
            task_id="abc12345",
            seq=42,
            type=EventType.SLIDE_PREVIEW_READY,
            stage=StageName.GENERATE,
            progress=65.0,
            stage_progress=50.0,
            message="第3页预览已就绪",
            slide_id="sld-001",
            slide_index=3,
            total_slides=6,
            artifact_url="slides/sld-001/revisions/1/preview.png",
            payload={"title": "核心技术突破"},
        )
        dumped = e.model_dump_json()
        reloaded = GenerationEvent.model_validate_json(dumped)
        assert reloaded.task_id == e.task_id
        assert reloaded.seq == e.seq
        assert reloaded.type == e.type
        assert reloaded.slide_index == 3
        assert reloaded.payload == {"title": "核心技术突破"}

    def test_seq_must_be_positive(self):
        """seq 必须 >= 1。"""
        with pytest.raises(Exception):
            GenerationEvent(task_id="abc", seq=0, type=EventType.TASK_CREATED)

    def test_progress_clamped_to_100(self):
        """progress 不能超过 100。"""
        with pytest.raises(Exception):
            GenerationEvent(
                task_id="abc", seq=1, type=EventType.TASK_CREATED, progress=101
            )

    def test_all_event_types_are_parseable(self):
        """所有 EventType 枚举值均可在 GenerationEvent 中使用。"""
        for et in EventType:
            e = GenerationEvent(task_id="abc", seq=1, type=et)
            assert e.type == et

    def test_type_discriminates_correctly(self):
        """JSON 往返后 type 和 status 枚举值应保持不变。"""
        e = GenerationEvent(
            task_id="abc",
            seq=5,
            type=EventType.TASK_FAILED,
            status=TaskStatus.FAILED,
            message="任务执行失败",
        )
        raw = json.loads(e.model_dump_json())
        assert raw["type"] == "task.failed"
        assert raw["status"] == "failed"


# ═══════════════════════════════════════════════════════════════════════
# JSONL 解析
# ═══════════════════════════════════════════════════════════════════════


class TestJsonlParsing:
    def test_parse_valid_jsonl(self):
        """标准 JSONL 应正确解析为 GenerationEvent 列表。"""
        lines = [
            '{"task_id":"abc","seq":1,"type":"task.created","status":"queued"}',
            '{"task_id":"abc","seq":2,"type":"task.started","status":"running"}',
            '{"task_id":"abc","seq":3,"type":"stage.started","stage":"plan"}',
        ]
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        ) as f:
            f.write("\n".join(lines))
            tmp = f.name

        try:
            events = parse_events_from_jsonl(tmp)
            assert len(events) == 3
            assert events[0].seq == 1
            assert events[-1].seq == 3
        finally:
            Path(tmp).unlink()

    def test_heartbeat_and_eof_are_skipped(self):
        """心跳和 EOF 伪事件应被过滤。"""
        lines = [
            '{"task_id":"abc","seq":1,"type":"task.created","status":"queued"}',
            '{"type":"heartbeat"}',
            '{"type":"eof","task_id":"abc"}',
            '{"task_id":"abc","seq":2,"type":"task.completed","status":"succeeded"}',
        ]
        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".jsonl", delete=False, encoding="utf-8"
        ) as f:
            f.write("\n".join(lines))
            tmp = f.name

        try:
            events = parse_events_from_jsonl(tmp)
            assert len(events) == 2, f"心跳/EOF 应被跳过，得到了 {len(events)} 条"
            assert events[0].seq == 1
            assert events[1].seq == 2
            assert events[1].status == TaskStatus.COMPLETED
        finally:
            Path(tmp).unlink()


# ═══════════════════════════════════════════════════════════════════════
# SlideArtifact 与工作区路径工具
# ═══════════════════════════════════════════════════════════════════════


class TestSlideArtifact:
    def test_defaults(self):
        """默认值检查。"""
        s = SlideArtifact(slide_id="sld-001", task_id="abc", index=1)
        assert s.status == "pending"
        assert s.mode == "html"
        assert s.revision == 1
        assert s.structured_data.title is None
        assert s.structured_data.body == []

    def test_bumped_increments_revision(self):
        """bumped() 应生成 revision+1 的副本，原对象不变。"""
        s = SlideArtifact(slide_id="sld-001", task_id="abc", index=1, revision=3)
        s2 = s.bumped()
        assert s2.revision == 4
        assert s.revision == 3  # 原对象未变
        assert s2.slide_id == s.slide_id

    def test_roundtrip(self):
        """序列化再反序列化后字段应对齐。"""
        s = SlideArtifact(
            slide_id="sld-001",
            task_id="abc",
            index=2,
            status="completed",
            mode="template",
            layout_name="title_and_body",
            structured_data=StructuredContent(
                title="人工智能发展报告",
                body=["要点一", "要点二"],
                images=[{"path": "chart.png", "caption": "增长趋势"}],
            ),
            source_path="slides/sld-001/revisions/1/source.json",
            preview_path="slides/sld-001/revisions/1/preview.png",
            revision=2,
        )
        reloaded = SlideArtifact.model_validate_json(s.model_dump_json())
        assert reloaded.slide_id == "sld-001"
        assert reloaded.layout_name == "title_and_body"
        assert reloaded.structured_data.title == "人工智能发展报告"
        assert reloaded.structured_data.body == ["要点一", "要点二"]

    def test_revision_limit_constant(self):
        """每页最多保留 10 个版本。"""
        assert MAX_REVISIONS == 10


class TestWorkspaceHelpers:
    def test_task_dir(self):
        assert task_dir(Path("/ws"), "abc12345") == Path("/ws/abc12345")

    def test_slides_dir(self):
        assert slides_dir(Path("/ws"), "abc") == Path("/ws/abc/slides")

    def test_slide_dir(self):
        assert slide_dir(Path("/ws"), "abc", "sld-001") == Path(
            "/ws/abc/slides/sld-001"
        )

    def test_revision_dir(self):
        assert revision_dir(Path("/ws"), "abc", "sld-001", 3) == Path(
            "/ws/abc/slides/sld-001/revisions/3"
        )

    def test_path_safe_valid(self):
        """合法路径应通过安全校验。"""
        assert is_path_safe(Path("/ws"), "abc", "slides/sld-001/preview.png")

    def test_path_safe_block_traversal(self):
        """路径穿越攻击应被拦截。"""
        assert not is_path_safe(Path("/ws"), "abc", "../../../etc/passwd")

    def test_path_safe_block_absolute(self):
        """绝对路径应被拦截。"""
        assert not is_path_safe(Path("/ws"), "abc", "/etc/passwd")

    def test_path_safe_block_sibling_prefix(self):
        """同名前缀的相邻目录不能被误判为任务目录内部。"""
        assert not is_path_safe(Path("/ws"), "abc", "../abc2/preview.png")
