"""Per-slide revision management (D).

Each successful conversational edit produces a new *revision* of the slide.
Revisions are stored as the list of edit actions that produced them plus a
checksum of the resulting slide.  Restoring a revision replays those actions
from the original *baseline* snapshot, which guarantees the rebuilt slide
carries the correct edit closures and therefore exports correctly — without
ever calling the model again (undo is purely a pointer move + replay).

Retention follows the plan: by default the most recent ``max_revisions`` (10)
revisions are kept; pruned revisions are dropped from disk but their metadata
is preserved in a lightweight pruned log.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pptagent.editor.snapshot import SlideSnapshot


@dataclass
class SlideRevision:
    """A single recorded revision of a slide."""

    number: int
    instruction: str
    actions: list[str]  # low-level API call lines, or special directives
    status: str  # "success" | "draft"
    checksum: str
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    dialogue: list[dict[str, Any]] = field(default_factory=list)
    message: str = ""
    preview_path: str | None = None


class RevisionManager:
    """Manage the ordered revisions of a single slide."""

    def __init__(self, baseline: SlideSnapshot, max_revisions: int = 10):
        self.baseline = baseline
        self.max_revisions = max_revisions
        self._revisions: dict[int, SlideRevision] = {}
        self._order: list[int] = []
        self._current: int = 0
        self._pruned: list[dict[str, Any]] = []
        self._last_draft: SlideRevision | None = None

    # ── write ──────────────────────────────────────────────────

    def add(
        self,
        instruction: str,
        actions: list[str],
        status: str,
        checksum: str,
        dialogue: list[dict[str, Any]] | None = None,
        message: str = "",
        preview_path: str | None = None,
    ) -> SlideRevision:
        """Record a revision.  Successful revisions advance the pointer."""
        number = (max(self._order) if self._order else 0) + 1
        rev = SlideRevision(
            number=number,
            instruction=instruction,
            actions=list(actions),
            status=status,
            checksum=checksum,
            dialogue=dialogue or [],
            message=message,
            preview_path=preview_path,
        )
        if status == "success":
            self._revisions[number] = rev
            self._order.append(number)
            self._current = number
            self._trim()
        else:
            self._last_draft = rev
        return rev

    def _trim(self) -> list[int]:
        """Drop oldest revisions beyond ``max_revisions``; return pruned numbers."""
        pruned: list[int] = []
        while len(self._order) > self.max_revisions:
            oldest = self._order.pop(0)
            rev = self._revisions.pop(oldest)
            self._pruned.append(
                {
                    "number": oldest,
                    "instruction": rev.instruction,
                    "created_at": rev.created_at,
                    "checksum": rev.checksum,
                    "message": rev.message,
                }
            )
            pruned.append(oldest)
        return pruned

    # ── read ───────────────────────────────────────────────────

    @property
    def current_number(self) -> int:
        return self._current

    @property
    def current_checksum(self) -> str | None:
        rev = self._revisions.get(self._current)
        return rev.checksum if rev else None

    def get(self, number: int) -> SlideRevision | None:
        return self._revisions.get(number)

    def get_current(self) -> SlideRevision | None:
        return self._revisions.get(self._current)

    def previous_number(self) -> int | None:
        if self._current not in self._order:
            return None
        idx = self._order.index(self._current)
        return self._order[idx - 1] if idx > 0 else None

    def set_current(self, number: int) -> None:
        if number in self._order:
            self._current = number

    def actions_up_to(self, number: int) -> list[str]:
        """All successful actions from revision 1 up to and including ``number``."""
        out: list[str] = []
        for num in self._order:
            if num <= number:
                out.extend(self._revisions[num].actions)
        return out

    def list(self) -> list[dict[str, Any]]:
        """Metadata for every kept revision (oldest → newest)."""
        return [
            {
                "number": num,
                "instruction": self._revisions[num].instruction,
                "status": self._revisions[num].status,
                "checksum": self._revisions[num].checksum,
                "created_at": self._revisions[num].created_at,
                "message": self._revisions[num].message,
                "preview_path": self._revisions[num].preview_path,
                "is_current": num == self._current,
            }
            for num in self._order
        ]

    def pruned_log(self) -> list[dict[str, Any]]:
        return list(self._pruned)

    def __len__(self) -> int:
        return len(self._order)
