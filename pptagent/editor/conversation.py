"""Per-page conversation memory for conversational local editing.

Holds the most recent turns of dialogue for one slide so the model
gets just enough context — not the whole task history — when applying
the next instruction ("再短一点" must be understood relative to the
previous turn, per the direction-D completion criteria).

Kept deliberately small and dependency-free: a bounded ring of turns
plus the user's currently selected element (section "对话上下文").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ── conversation turn ─────────────────────────────────────────

@dataclass
class ConversationTurn:
    """One user instruction + the assistant's resulting action summary."""

    role: str               # "user" | "assistant" | "system"
    content: str            # the instruction (user) or action summary (assistant)
    revision: int | None = None   # revision produced by this turn (assistant)
    element_id: str | None = None # user-selected element, if any
    success: bool = True
    created_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "role": self.role,
            "content": self.content,
            "revision": self.revision,
            "element_id": self.element_id,
            "success": self.success,
            "created_at": self.created_at,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ConversationTurn":
        return cls(
            role=data.get("role", "user"),
            content=data.get("content", ""),
            revision=data.get("revision"),
            element_id=data.get("element_id"),
            success=data.get("success", True),
            created_at=data.get("created_at", ""),
        )


# ── conversation log ──────────────────────────────────────────

class ConversationLog:
    """Bounded per-slide dialogue history.

    Only the most recent ``max_turns`` turns are kept in memory and in
    the model context, matching the plan's "最近若干轮对话" rule.
    """

    def __init__(self, slide_id: str, max_turns: int = 8):
        self.slide_id = slide_id
        self.max_turns = max_turns
        self._turns: list[ConversationTurn] = []

    def add_user(self, instruction: str,
                 element_id: str | None = None) -> ConversationTurn:
        turn = ConversationTurn(role="user", content=instruction,
                                element_id=element_id)
        self._turns.append(turn)
        self._trim()
        return turn

    def add_assistant(self, summary: str, revision: int | None = None,
                      success: bool = True) -> ConversationTurn:
        turn = ConversationTurn(role="assistant", content=summary,
                                revision=revision, success=success)
        self._turns.append(turn)
        self._trim()
        return turn

    def _trim(self) -> None:
        if len(self._turns) > self.max_turns:
            # keep the last max_turns, but never drop a pending user turn
            self._turns = self._turns[-self.max_turns:]

    def recent(self, n: int | None = None) -> list[ConversationTurn]:
        """Return the last ``n`` turns (default: all kept)."""
        if n is None:
            return list(self._turns)
        return list(self._turns[-n:])

    def as_messages(self, n: int | None = None) -> list[dict[str, str]]:
        """Return recent turns as ``{role, content}`` chat messages."""
        return [{"role": t.role, "content": t.content}
                for t in self.recent(n)]

    def clear(self) -> None:
        self._turns.clear()

    def __len__(self) -> int:
        return len(self._turns)
