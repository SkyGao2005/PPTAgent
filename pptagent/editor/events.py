"""Unified event model and in-process event bus.

Implements the ``GenerationEvent`` schema and a lightweight event bus
backed by an ``asyncio.Queue`` plus an append-only ``events.jsonl``
log, as described in the two-week development plan (section 4.1 / 4.2).

The bus is intentionally process-local — no Redis/Celery — so the
two-week scope stays within a single machine. Swapping it for a
distributed bus later does not change the event protocol.
"""

import asyncio
import json
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterator, Callable


# ── event vocabulary ──────────────────────────────────────────

# Stages (section 4.2 field ``stage``)
STAGE_TEMPLATE = "template"
STAGE_PLAN = "plan"
STAGE_RESEARCH = "research"
STAGE_GENERATE = "generate"
STAGE_EDIT = "edit"
STAGE_EXPORT = "export"

# Statuses (section 4.2 field ``status``)
STATUS_QUEUED = "queued"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"

# Event types — first phase only (section 4.2)
EVENT_TYPES = {
    # task lifecycle
    "task.created", "task.started", "task.completed",
    "task.failed", "task.cancelled",
    # template parsing
    "template.parse_started", "template.parse_progress",
    "template.ready", "template.failed",
    # stage progress
    "stage.started", "stage.progress", "stage.completed",
    # slide generation
    "slide.started", "slide.preview_ready",
    "slide.completed", "slide.failed",
    # local editing (direction D)
    "edit.started", "edit.preview_ready",
    "edit.applied", "edit.failed", "edit.reverted",
    # export
    "export.started", "export.completed", "export.failed",
}


# ── event payload ─────────────────────────────────────────────

@dataclass
class GenerationEvent:
    """A single structured progress/edit event (section 4.2).

    Field order and names match the contract so the frontend and the
    on-disk ``events.jsonl`` stay in sync.
    """

    task_id: str
    seq: int                    # per-task increasing sequence number
    type: str                   # one of EVENT_TYPES
    stage: str = ""             # template/plan/research/generate/edit/export
    status: str = ""            # queued/running/succeeded/failed/cancelled
    progress: int | None = None  # 0–100, may be empty
    message: str = ""           # short user-facing note
    slide_id: str | None = None
    slide_index: int | None = None
    total_slides: int | None = None
    artifact_url: str | None = None  # thumbnail / pptx path
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    payload: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        # drop empty optional fields to keep the log compact
        for k in ("progress", "slide_id", "slide_index",
                  "total_slides", "artifact_url"):
            if d.get(k) is None:
                d.pop(k, None)
        if not d.get("payload"):
            d.pop("payload", None)
        if not d.get("stage"):
            d.pop("stage", None)
        if not d.get("status"):
            d.pop("status", None)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "GenerationEvent":
        return cls(
            task_id=data["task_id"],
            seq=data["seq"],
            type=data["type"],
            stage=data.get("stage", ""),
            status=data.get("status", ""),
            progress=data.get("progress"),
            message=data.get("message", ""),
            slide_id=data.get("slide_id"),
            slide_index=data.get("slide_index"),
            total_slides=data.get("total_slides"),
            artifact_url=data.get("artifact_url"),
            created_at=data.get("created_at", datetime.now().isoformat()),
            payload=data.get("payload", {}),
        )

    # ── convenience constructors for direction D ─────────────

    @classmethod
    def edit_started(cls, task_id: str, seq: int, slide_id: str,
                     instruction: str, **kw) -> "GenerationEvent":
        return cls(task_id=task_id, seq=seq, type="edit.started",
                   stage=STAGE_EDIT, status=STATUS_RUNNING,
                   slide_id=slide_id, message=instruction[:200], **kw)

    @classmethod
    def edit_preview_ready(cls, task_id: str, seq: int, slide_id: str,
                           artifact_url: str, revision: int, **kw) -> "GenerationEvent":
        return cls(task_id=task_id, seq=seq, type="edit.preview_ready",
                   stage=STAGE_EDIT, status=STATUS_RUNNING,
                   slide_id=slide_id, artifact_url=artifact_url,
                   payload={"revision": revision, **kw})

    @classmethod
    def edit_applied(cls, task_id: str, seq: int, slide_id: str,
                     revision: int, **kw) -> "GenerationEvent":
        return cls(task_id=task_id, seq=seq, type="edit.applied",
                   stage=STAGE_EDIT, status=STATUS_SUCCEEDED,
                   slide_id=slide_id, payload={"revision": revision, **kw})

    @classmethod
    def edit_failed(cls, task_id: str, seq: int, slide_id: str,
                    reason: str, **kw) -> "GenerationEvent":
        return cls(task_id=task_id, seq=seq, type="edit.failed",
                   stage=STAGE_EDIT, status=STATUS_FAILED,
                   slide_id=slide_id, message=reason[:200],
                   payload={"reason": reason, **kw})

    @classmethod
    def edit_reverted(cls, task_id: str, seq: int, slide_id: str,
                      revision: int, **kw) -> "GenerationEvent":
        return cls(task_id=task_id, seq=seq, type="edit.reverted",
                   stage=STAGE_EDIT, status=STATUS_SUCCEEDED,
                   slide_id=slide_id, payload={"revision": revision, **kw})


# ── event bus ─────────────────────────────────────────────────

class EventBus:
    """Per-task event bus with seq assignment, jsonl persistence, replay.

    - ``publish()`` assigns the next ``seq`` and appends to
      ``events.jsonl`` so a client can replay after a reconnect.
    - ``subscribe()`` yields events as they are published (async).
    - ``replay(from_seq=)`` replays past events from disk so a
      reconnecting client can fill the gap (section 4.1).

    The bus is safe to use from one event loop. Cross-task isolation
    is achieved by giving each task its own bus (one per workspace).
    """

    def __init__(self, task_id: str, workspace_dir: Path | str | None = None):
        self.task_id = task_id
        self._seq = 0
        self._subscribers: list[asyncio.Queue[GenerationEvent]] = []
        self._closed = False
        if workspace_dir is not None:
            self._log_path = Path(workspace_dir) / "events.jsonl"
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
        else:
            self._log_path = None

    # ── publish ───────────────────────────────────────────────

    def publish(self, event: GenerationEvent) -> GenerationEvent:
        """Assign the next seq, persist, and fan out to subscribers.

        ``event.seq`` is overwritten here so callers never need to
        manage sequence numbers themselves.
        """
        if self._closed:
            raise RuntimeError(f"EventBus for task {self.task_id} is closed")
        if event.task_id != self.task_id:
            event.task_id = self.task_id
        self._seq += 1
        event.seq = self._seq
        # persist (jsonl append)
        if self._log_path is not None:
            with open(self._log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(event.to_dict(), ensure_ascii=False) + "\n")
        # fan out (non-blocking; drop if queue full to avoid blocking producers)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                # subscriber too slow — drop oldest to keep latest flowing
                try:
                    q.get_nowait()
                except asyncio.QueueEmpty:
                    pass
                try:
                    q.put_nowait(event)
                except asyncio.QueueFull:
                    pass
        return event

    async def publish_async(self, event: GenerationEvent) -> GenerationEvent:
        """Async convenience wrapper for ``publish``."""
        return self.publish(event)

    # ── subscribe ─────────────────────────────────────────────

    def subscribe(self, maxsize: int = 256) -> asyncio.Queue[GenerationEvent]:
        """Register a subscriber queue. Call ``unsubscribe`` to remove."""
        q: asyncio.Queue[GenerationEvent] = asyncio.Queue(maxsize=maxsize)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue[GenerationEvent]) -> None:
        if q in self._subscribers:
            self._subscribers.remove(q)

    async def events(self, from_seq: int = 0,
                     max_events: int = 10_000) -> AsyncIterator[GenerationEvent]:
        """Async iterator: replay past events then stream live.

        ``from_seq`` lets a reconnecting client resume from the last
        event it saw (section 4.1). To avoid losing events published in
        the gap between disk replay and queue subscription, we subscribe
        **first**, record the high-water mark, replay only up to that
        mark, then drain the live queue for everything after it.
        """
        # 1. subscribe first so nothing published from here on is missed
        q = self.subscribe()
        sub_seq = self._seq  # high-water mark at subscribe time
        replayed = 0
        # 2. replay past events up to the high-water mark
        if self._log_path is not None and self._log_path.exists():
            with open(self._log_path, "r", encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        evt = GenerationEvent.from_dict(json.loads(line))
                    except (json.JSONDecodeError, KeyError):
                        continue
                    if from_seq < evt.seq <= sub_seq:
                        yield evt
                        replayed += 1
                        if replayed >= max_events:
                            self.unsubscribe(q)
                            return
        # 3. stream live — yield only events newer than the high-water
        #    mark (replayed ones were already yielded above)
        try:
            while True:
                evt = await q.get()
                if evt.seq > sub_seq:
                    yield evt
        finally:
            self.unsubscribe(q)

    # ── query ─────────────────────────────────────────────────

    @property
    def last_seq(self) -> int:
        return self._seq

    def history(self) -> list[GenerationEvent]:
        """Return all persisted events (empty if no log path)."""
        if self._log_path is None or not self._log_path.exists():
            return []
        out: list[GenerationEvent] = []
        with open(self._log_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(GenerationEvent.from_dict(json.loads(line)))
                except (json.JSONDecodeError, KeyError):
                    continue
        return out

    def close(self) -> None:
        """Stop accepting new events. Subscribers keep draining."""
        self._closed = True


# ── task registry (process-wide, in-memory) ───────────────────

class TaskEventRegistry:
    """Holds one ``EventBus`` per task_id for the running process.

    A thin shim so services can look up a task's bus by id without
    threading it through every call. Not persisted — tasks that
    outlive the process are recovered from their ``events.jsonl``.
    """

    def __init__(self):
        self._buses: dict[str, EventBus] = {}

    def get_or_create(self, task_id: str,
                      workspace_dir: Path | str | None = None) -> EventBus:
        if task_id not in self._buses:
            self._buses[task_id] = EventBus(task_id, workspace_dir)
        return self._buses[task_id]

    def get(self, task_id: str) -> EventBus | None:
        return self._buses.get(task_id)

    def drop(self, task_id: str) -> None:
        bus = self._buses.pop(task_id, None)
        if bus is not None:
            bus.close()

    def __contains__(self, task_id: str) -> bool:
        return task_id in self._buses

    def __len__(self) -> int:
        return len(self._buses)


# process-global registry (direction C owns task lifecycle; direction D
# only needs to publish edit events through it)
_GLOBAL_REGISTRY: TaskEventRegistry | None = None


def global_registry() -> TaskEventRegistry:
    global _GLOBAL_REGISTRY
    if _GLOBAL_REGISTRY is None:
        _GLOBAL_REGISTRY = TaskEventRegistry()
    return _GLOBAL_REGISTRY


def new_task_id() -> str:
    """Generate a short, URL-safe task id."""
    return uuid.uuid4().hex[:12]
