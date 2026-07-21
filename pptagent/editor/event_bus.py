"""In-process event bus for the conversational editing feature (D).

Mirrors the ``edit.*`` event family described in the secondary-development
plan: ``edit.started``, ``edit.preview_ready``, ``edit.applied``,
``edit.failed``, ``edit.reverted``.

Events are broadcast to all subscribers (used by the SSE endpoint) and, when a
storage directory is provided, also appended to ``events.jsonl`` so a client
that reconnects can replay missed events by ``seq``.
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections import deque
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator


@dataclass
class EditEvent:
    """A single edit-related event published on the bus."""

    event_id: str
    seq: int
    type: str  # edit.started | edit.preview_ready | edit.applied | edit.failed | edit.reverted
    slide_id: str
    task_id: str | None = None
    status: str | None = None  # running | succeeded | failed
    message: str | None = None
    revision: int | None = None
    preview_url: str | None = None
    artifact_url: str | None = None
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class EditEventBus:
    """Process-wide asynchronous event bus for slide-edit events.

    A single instance is usually shared across all slides of a task via
    :func:`get_default_bus`.  Subscribers receive every event through an
    async generator; the bus also keeps a bounded replay buffer so an SSE
    client can resume from the last ``seq`` it saw.
    """

    def __init__(self, storage_dir: Path | str | None = None, max_history: int = 2000):
        self._storage_dir = Path(storage_dir) if storage_dir else None
        self._events_file = self._storage_dir / "events.jsonl" if self._storage_dir else None
        self._history: deque[EditEvent] = deque(maxlen=max_history)
        self._seq = 0
        self._lock = threading.Lock()
        self._subscribers: list[asyncio.Queue] = []
        if self._storage_dir:
            self._storage_dir.mkdir(parents=True, exist_ok=True)

    # ── publishing ─────────────────────────────────────────────

    def _next_seq(self) -> int:
        with self._lock:
            self._seq += 1
            return self._seq

    def publish(
        self,
        type: str,
        slide_id: str,
        task_id: str | None = None,
        status: str | None = None,
        message: str | None = None,
        revision: int | None = None,
        preview_url: str | None = None,
        artifact_url: str | None = None,
        payload: dict[str, Any] | None = None,
    ) -> EditEvent:
        """Create, persist and broadcast an :class:`EditEvent`."""
        event = EditEvent(
            event_id=uuid.uuid4().hex[:12],
            seq=self._next_seq(),
            type=type,
            slide_id=slide_id,
            task_id=task_id,
            status=status,
            message=message,
            revision=revision,
            preview_url=preview_url,
            artifact_url=artifact_url,
            payload=payload or {},
        )
        self._history.append(event)
        if self._events_file is not None:
            try:
                with open(self._events_file, "a", encoding="utf-8") as f:
                    f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
            except OSError:
                pass
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                pass
        return event

    # ── replay / inspection ────────────────────────────────────

    def recent(self, since_seq: int = 0, task_id: str | None = None) -> list[EditEvent]:
        """Return buffered events with ``seq > since_seq`` (optionally filtered)."""
        events = [e for e in self._history if e.seq > since_seq]
        if task_id is not None:
            events = [e for e in events if e.task_id == task_id]
        return events

    # ── subscription (SSE) ─────────────────────────────────────

    async def subscribe(
        self, task_id: str | None = None, slide_id: str | None = None
    ) -> AsyncIterator[EditEvent]:
        """Async generator yielding edit events as they are published.

        Pass ``task_id`` / ``slide_id`` to filter.  Use ``recent(since_seq=…)``
        first to backfill missed events after a reconnect.
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=512)
        self._subscribers.append(q)
        try:
            while True:
                event = await q.get()
                if task_id is not None and event.task_id != task_id:
                    continue
                if slide_id is not None and event.slide_id != slide_id:
                    continue
                yield event
        finally:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass


# ── process-wide default bus ───────────────────────────────────

_DEFAULT_BUS: EditEventBus | None = None
_BUS_LOCK = threading.Lock()


def get_default_bus(storage_dir: Path | str | None = None) -> EditEventBus:
    """Return the shared :class:`EditEventBus` (creating it on first use)."""
    global _DEFAULT_BUS
    if _DEFAULT_BUS is None:
        with _BUS_LOCK:
            if _DEFAULT_BUS is None:
                _DEFAULT_BUS = EditEventBus(storage_dir)
    return _DEFAULT_BUS
