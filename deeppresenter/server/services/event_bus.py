"""进程内事件总线 —— JSONL 持久化 + 多订阅者 SSE 推送。

设计决策（参见 docs/api-contract.md）：
- 使用 ``asyncio.Queue`` 实现进程内广播，两周内不引入 Redis/Celery。
- 事件追加写入 ``events.jsonl``，支持崩溃恢复和历史回放。
- SSE 订阅者先收到历史事件（seq > last_seq），再进入实时推送。
- 每 30 秒发送心跳，防止中间代理超时断开。
"""

import asyncio
import json
import logging
from collections.abc import AsyncGenerator, Iterator
from pathlib import Path

import jsonlines

from deeppresenter.server.models.events import GenerationEvent

logger = logging.getLogger(__name__)

# ── 哨兵常量 ────────────────────────────────────────────────────

HEARTBEAT_EVENT = {"type": "heartbeat"}
EOF_EVENT = {"type": "eof"}
HEARTBEAT_INTERVAL = 30  # 心跳间隔（秒）
_CLOSE_SENTINEL = object()  # 内部哨兵，通知订阅者优雅退出


class EventBus:
    """每个任务一个的事件总线。

    任务创建时生成一个 EventBus 实例，任务到达终态时关闭。
    多个 SSE 订阅者可以同时连接；每条发布的事件会广播给所有订阅者。

    用法::

        bus = EventBus(task_workspace)
        await bus.publish(event)
        # ...
        await bus.close()
    """

    def __init__(self, task_workspace: Path) -> None:
        task_workspace.mkdir(parents=True, exist_ok=True)
        self._events_path = task_workspace / "events.jsonl"
        self._fh = open(str(self._events_path), "a", encoding="utf-8")
        self._seq: int = self._load_latest_seq()
        self._subscribers: list[asyncio.Queue] = []
        self._closed: bool = False

    # ── 发布 ────────────────────────────────────────────────────

    async def publish(self, event: GenerationEvent) -> None:
        """持久化 *event* 到 jsonl 并广播给所有订阅者。

        总线关闭后调用会抛出 ``RuntimeError``。
        """
        if self._closed:
            raise RuntimeError("Cannot publish on a closed EventBus")

        self._seq += 1
        event.seq = self._seq
        raw = event.model_dump()
        line = json.dumps(raw, ensure_ascii=False)

        self._fh.write(line + "\n")
        self._fh.flush()

        for queue in self._subscribers:
            queue.put_nowait(raw)

    # ── 订阅（SSE 数据源）───────────────────────────────────────

    def subscribe(
        self, last_seq: int = 0
    ) -> AsyncGenerator[dict, None]:
        """返回一个异步生成器，逐个产出 SSE 就绪的字典。

        订阅者在调用时立即注册（在首次 ``__anext__()`` 之前），
        因此 ``subscriber_count`` 能实时反映活跃连接数。

        1. 回放 events.jsonl 中 seq > *last_seq* 的历史事件。
        2. 实时推送新发布的事件。
        3. 每隔 ``HEARTBEAT_INTERVAL`` 秒发送心跳。
        4. 总线关闭时退出。
        """
        queue: asyncio.Queue = asyncio.Queue()
        self._subscribers.append(queue)
        return self._iterate(queue, last_seq)

    async def _iterate(
        self, queue: asyncio.Queue, last_seq: int
    ) -> AsyncGenerator[dict, None]:
        """内部异步生成器 —— 仅在首次迭代时开始执行。"""
        try:
            # 第一阶段 —— 回放历史事件
            for event in self._replay(last_seq):
                yield event

            if self._closed:
                yield EOF_EVENT
                return

            # 第二阶段 —— 实时订阅
            while True:
                try:
                    raw = await asyncio.wait_for(
                        queue.get(), timeout=HEARTBEAT_INTERVAL
                    )
                except asyncio.TimeoutError:
                    yield HEARTBEAT_EVENT
                    continue

                if raw is _CLOSE_SENTINEL:
                    yield EOF_EVENT
                    return

                yield raw
        finally:
            if queue in self._subscribers:
                self._subscribers.remove(queue)

    def _replay(self, last_seq: int) -> Iterator[dict]:
        """从 jsonl 中回放 seq > *last_seq* 的事件。"""
        if last_seq < 0:
            last_seq = 0

        if not self._events_path.exists():
            return

        # 先刷新写入缓冲区，确保所有已发布事件可见
        try:
            if not self._fh.closed:
                self._fh.flush()
        except (ValueError, OSError):
            pass

        try:
            with jsonlines.open(str(self._events_path), mode="r") as reader:
                for obj in reader:
                    if obj.get("seq", 0) > last_seq:
                        yield obj
        except Exception:
            # 文件可能为空或损坏，跳过回放
            return

    def _load_latest_seq(self) -> int:
        """从已有 events.jsonl 中恢复最大 seq，避免重启后重复编号。"""
        latest = 0
        if not self._events_path.exists():
            return latest
        try:
            with jsonlines.open(str(self._events_path), mode="r") as reader:
                for obj in reader:
                    latest = max(latest, int(obj.get("seq", 0)))
        except Exception:
            return latest
        return latest

    # ── 生命周期 ────────────────────────────────────────────────

    async def close(self) -> None:
        """通知所有订阅者停止并关闭文件句柄。"""
        if self._closed:
            return
        self._closed = True

        # 先拷贝再清空，避免订阅者的 finally 块在遍历时修改列表
        queues = list(self._subscribers)
        self._subscribers.clear()

        for queue in queues:
            queue.put_nowait(_CLOSE_SENTINEL)

        self._fh.close()

    @property
    def seq(self) -> int:
        """当前事件序号（首次发布前为 0）。"""
        return self._seq

    @property
    def subscriber_count(self) -> int:
        """当前活跃的 SSE 订阅者数量。"""
        return len(self._subscribers)
