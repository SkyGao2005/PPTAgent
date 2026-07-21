"""EventReporter 单元测试。"""

import tempfile
from pathlib import Path

import pytest

from deeppresenter.server.models.events import EventType, StageName, TaskStatus
from deeppresenter.server.services.event_bus import EventBus
from deeppresenter.server.services.event_reporter import EventReporter


@pytest.fixture
def tmp_workspace() -> Path:
    tmp = tempfile.mkdtemp(prefix="reporter_test_")
    yield Path(tmp)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


@pytest.mark.asyncio
async def test_task_events(tmp_workspace):
    bus = EventBus(tmp_workspace)
    reporter = EventReporter("abc12345", bus.publish)

    await reporter.task_created()
    await reporter.task_started()
    await reporter.task_completed("exports/latest.pptx")

    events = list(bus._replay(0))
    assert [evt["type"] for evt in events] == [
        EventType.TASK_CREATED.value,
        EventType.TASK_STARTED.value,
        EventType.TASK_COMPLETED.value,
    ]
    assert events[0]["status"] == TaskStatus.QUEUED.value
    assert events[-1]["status"] == "completed"
    assert events[-1]["progress"] == 100
    assert events[-1]["artifact_url"] == "exports/latest.pptx"

    await bus.close()


@pytest.mark.asyncio
async def test_slide_failed_event_includes_preview_payload(tmp_workspace):
    bus = EventBus(tmp_workspace)
    reporter = EventReporter("abc12345", bus.publish)

    await reporter.slide_failed(
        "sld-001",
        1,
        "第 1 页预览生成失败：boom",
        payload={
            "error": "boom",
            "html_file": "slides/slide_01.html",
            "source_preserved": True,
        },
    )

    events = list(bus._replay(0))
    assert events[0]["type"] == EventType.SLIDE_FAILED.value
    assert events[0]["slide_id"] == "sld-001"
    assert events[0]["slide_index"] == 1
    assert events[0]["payload"]["source_preserved"] is True
    assert events[0]["payload"]["html_file"] == "slides/slide_01.html"

    await bus.close()


@pytest.mark.asyncio
async def test_stage_events_use_weighted_progress(tmp_workspace):
    bus = EventBus(tmp_workspace)
    reporter = EventReporter("abc12345", bus.publish)

    await reporter.stage_started(StageName.RESEARCH)
    await reporter.stage_completed(StageName.RESEARCH)

    events = list(bus._replay(0))
    assert events[0]["type"] == EventType.STAGE_STARTED.value
    assert events[0]["progress"] == 15.0
    assert events[1]["type"] == EventType.STAGE_COMPLETED.value
    assert events[1]["progress"] == 40.0
    assert events[1]["stage_progress"] == 100

    await bus.close()


@pytest.mark.asyncio
async def test_tool_events_are_stage_progress_payloads(tmp_workspace):
    bus = EventBus(tmp_workspace)
    reporter = EventReporter("abc12345", bus.publish)

    await reporter.tool_started("write_file", stage=StageName.GENERATE)
    await reporter.tool_failed(
        "write_file", elapsed=0.5, stage=StageName.GENERATE, error="boom"
    )

    events = list(bus._replay(0))
    assert all(evt["type"] == EventType.STAGE_PROGRESS.value for evt in events)
    assert events[0]["payload"] == {
        "kind": "tool",
        "tool_name": "write_file",
        "status": "started",
    }
    assert events[1]["payload"]["status"] == "failed"
    assert events[1]["payload"]["elapsed"] == 0.5
    assert events[1]["payload"]["error"] == "boom"

    await bus.close()


@pytest.mark.asyncio
async def test_slide_and_export_events(tmp_workspace):
    bus = EventBus(tmp_workspace)
    reporter = EventReporter("abc12345", bus.publish)

    await reporter.slide_started("sld-001", 1, 3)
    await reporter.slide_preview_ready("sld-001", 1, "slides/sld-001/preview.png")
    await reporter.slide_completed("sld-001", 1)
    await reporter.export_started()
    await reporter.export_completed("exports/latest.pptx")

    events = list(bus._replay(0))
    assert events[0]["type"] == EventType.SLIDE_STARTED.value
    assert events[1]["artifact_url"] == "slides/sld-001/preview.png"
    assert events[2]["type"] == EventType.SLIDE_COMPLETED.value
    assert events[3]["type"] == EventType.EXPORT_STARTED.value
    assert events[4]["type"] == EventType.EXPORT_COMPLETED.value

    await bus.close()


@pytest.mark.asyncio
async def test_emit_publish_failure_warns_without_raising():
    async def broken_publish(_event):
        raise OSError("disk full")

    reporter = EventReporter("abc12345", broken_publish)

    with pytest.warns(RuntimeWarning, match="Failed to publish event task.created"):
        await reporter.task_created()
