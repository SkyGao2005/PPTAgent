"""EventBus 单元测试 —— 发布、回放、SSE 订阅、心跳、生命周期。

运行方式:
    DEEPPRESENTER_SKIP_POSIX=1 pytest deeppresenter/server/tests/test_event_bus.py -v --asyncio-mode=auto
"""

import asyncio
import json
import tempfile
from pathlib import Path

import pytest

from deeppresenter.server.models.events import EventType, GenerationEvent, StageName
from deeppresenter.server.services.event_bus import (
    HEARTBEAT_EVENT,
    HEARTBEAT_INTERVAL,
    EventBus,
)


@pytest.fixture
def tmp_workspace() -> Path:
    tmp = tempfile.mkdtemp(prefix="eventbus_test_")
    yield Path(tmp)
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)


def _make_event(**kwargs) -> GenerationEvent:
    """快速构造测试用事件。"""
    defaults: dict = {"task_id": "abc12345", "seq": 1, "type": EventType.TASK_CREATED}
    defaults.update(kwargs)
    return GenerationEvent(**defaults)


# ═══════════════════════════════════════════════════════════════════════
# 发布
# ═══════════════════════════════════════════════════════════════════════


class TestPublish:
    @pytest.mark.asyncio
    async def test_publish_increments_seq(self, tmp_workspace):
        """每次 publish 后 seq 应递增。"""
        bus = EventBus(tmp_workspace)
        assert bus.seq == 0

        await bus.publish(_make_event(type=EventType.TASK_STARTED))
        assert bus.seq == 1

        await bus.publish(
            _make_event(type=EventType.STAGE_STARTED, stage=StageName.PLAN)
        )
        assert bus.seq == 2

        await bus.close()

    @pytest.mark.asyncio
    async def test_publish_auto_assigns_seq(self, tmp_workspace):
        """EventBus 应覆写事件的 seq，调用方无需自行跟踪。"""
        bus = EventBus(tmp_workspace)
        e = _make_event(seq=999)
        await bus.publish(e)
        assert e.seq == 1
        await bus.close()

    @pytest.mark.asyncio
    async def test_publish_to_closed_bus_raises(self, tmp_workspace):
        """已关闭的总线发布事件应抛出 RuntimeError。"""
        bus = EventBus(tmp_workspace)
        await bus.close()
        with pytest.raises(RuntimeError, match="closed"):
            await bus.publish(_make_event())

    @pytest.mark.asyncio
    async def test_publish_persists_to_jsonl(self, tmp_workspace):
        """发布后事件应写入 events.jsonl。"""
        bus = EventBus(tmp_workspace)
        await bus.publish(
            _make_event(type=EventType.STAGE_STARTED, stage=StageName.RESEARCH)
        )
        await bus.publish(
            _make_event(type=EventType.STAGE_COMPLETED, stage=StageName.RESEARCH)
        )
        await bus.close()

        jsonl = tmp_workspace / "events.jsonl"
        assert jsonl.exists()
        lines = jsonl.read_text(encoding="utf-8").strip().split("\n")
        assert len(lines) == 2
        for line in lines:
            obj = json.loads(line)
            assert obj["task_id"] == "abc12345"
            assert "seq" in obj


# ═══════════════════════════════════════════════════════════════════════
# 历史回放
# ═══════════════════════════════════════════════════════════════════════


class TestReplay:
    @pytest.mark.asyncio
    async def test_replay_from_start(self, tmp_workspace):
        """last_seq=0 时回放全部事件。"""
        bus = EventBus(tmp_workspace)
        await bus.publish(_make_event(type=EventType.TASK_CREATED))
        await bus.publish(_make_event(type=EventType.TASK_STARTED))

        replayed = list(bus._replay(0))
        assert len(replayed) == 2
        assert replayed[0]["seq"] == 1
        assert replayed[1]["seq"] == 2

        await bus.close()

    @pytest.mark.asyncio
    async def test_replay_from_midpoint(self, tmp_workspace):
        """last_seq=3 时应只回放 seq > 3 的事件。"""
        bus = EventBus(tmp_workspace)
        for i in range(5):
            await bus.publish(_make_event(type=EventType.STAGE_PROGRESS))

        replayed = list(bus._replay(3))
        assert len(replayed) == 2
        assert replayed[0]["seq"] == 4
        assert replayed[1]["seq"] == 5

        await bus.close()

    @pytest.mark.asyncio
    async def test_replay_empty_when_caught_up(self, tmp_workspace):
        """last_seq 超过所有事件序号时回放为空。"""
        bus = EventBus(tmp_workspace)
        await bus.publish(_make_event(type=EventType.TASK_CREATED))
        replayed = list(bus._replay(99))
        assert len(replayed) == 0
        await bus.close()


# ═══════════════════════════════════════════════════════════════════════
# SSE 订阅
# ═══════════════════════════════════════════════════════════════════════


class TestSubscribe:
    @pytest.mark.asyncio
    async def test_subscribe_replays_history_then_live(self, tmp_workspace):
        """订阅者应先收到历史事件，再收到实时事件。"""
        bus = EventBus(tmp_workspace)
        await bus.publish(_make_event(type=EventType.TASK_CREATED))

        gen = bus.subscribe(last_seq=0)

        # 第一条：历史事件
        first = await gen.__anext__()
        assert first["type"] == "task.created"
        assert first["seq"] == 1

        # 发布新事件，应实时收到
        await bus.publish(_make_event(type=EventType.TASK_STARTED))
        second = await gen.__anext__()
        assert second["type"] == "task.started"
        assert second["seq"] == 2

        await gen.aclose()
        await bus.close()

    @pytest.mark.asyncio
    async def test_subscribe_skips_caught_up_history(self, tmp_workspace):
        """last_seq 追上进度时只收实时事件。"""
        bus = EventBus(tmp_workspace)
        await bus.publish(_make_event(type=EventType.TASK_CREATED))
        await bus.publish(_make_event(type=EventType.TASK_STARTED))

        gen = bus.subscribe(last_seq=2)

        # 延迟发布实时事件
        async def _publish():
            await asyncio.sleep(0.05)
            await bus.publish(
                _make_event(
                    type=EventType.STAGE_STARTED, stage=StageName.RESEARCH
                )
            )

        asyncio.ensure_future(_publish())

        live = await gen.__anext__()
        assert live["seq"] == 3
        assert live["type"] == "stage.started"

        await gen.aclose()
        await bus.close()

    @pytest.mark.asyncio
    async def test_subscribe_receives_heartbeat(self, tmp_workspace):
        """空闲超时后订阅者应收心跳。"""
        bus = EventBus(tmp_workspace)

        # 缩短心跳间隔以加速测试
        import deeppresenter.server.services.event_bus as eb_mod
        old = eb_mod.HEARTBEAT_INTERVAL
        eb_mod.HEARTBEAT_INTERVAL = 0.1

        try:
            gen = bus.subscribe(last_seq=9999)
            hb = await gen.__anext__()
            assert hb == HEARTBEAT_EVENT
            await gen.aclose()
        finally:
            eb_mod.HEARTBEAT_INTERVAL = old
            await bus.close()

    @pytest.mark.asyncio
    async def test_multiple_subscribers(self, tmp_workspace):
        """多个订阅者应同时收到同一事件，subscriber_count 应准确。"""
        bus = EventBus(tmp_workspace)

        gen1 = bus.subscribe(last_seq=9999)
        gen2 = bus.subscribe(last_seq=9999)
        assert bus.subscriber_count == 2

        await bus.publish(_make_event(type=EventType.TASK_STARTED))

        evt1 = await gen1.__anext__()
        evt2 = await gen2.__anext__()
        assert evt1["seq"] == 1
        assert evt2["seq"] == 1

        await gen1.aclose()
        await gen2.aclose()
        await bus.close()

    @pytest.mark.asyncio
    async def test_close_terminates_subscriber(self, tmp_workspace):
        """关闭总线后订阅者应抛出 StopAsyncIteration。"""
        bus = EventBus(tmp_workspace)

        gen = bus.subscribe(last_seq=9999)

        # 关闭总线 —— 向所有订阅者队列发送关闭哨兵
        await bus.close()

        # 生成器应收到哨兵并停止迭代
        with pytest.raises(StopAsyncIteration):
            await gen.__anext__()


# ═══════════════════════════════════════════════════════════════════════
# 生命周期
# ═══════════════════════════════════════════════════════════════════════


class TestLifecycle:
    @pytest.mark.asyncio
    async def test_close_is_idempotent(self, tmp_workspace):
        """重复调用 close() 不应抛出异常。"""
        bus = EventBus(tmp_workspace)
        await bus.close()
        await bus.close()

    @pytest.mark.asyncio
    async def test_close_cleans_up_subscribers(self, tmp_workspace):
        """close() 后 subscriber_count 应为 0。"""
        bus = EventBus(tmp_workspace)

        gen1 = bus.subscribe(last_seq=9999)
        gen2 = bus.subscribe(last_seq=9999)
        assert bus.subscriber_count == 2

        await bus.close()
        assert bus.subscriber_count == 0
