"""Edit history with undo/redo support."""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pptagent.editor.snapshot import SlideSnapshot


@dataclass
class EditRecord:
    """A single edit operation with before/after snapshots."""

    operation: str  # "edit_text" | "edit_image" | "delete_paragraph" | ...
    params: dict[str, Any]
    before: SlideSnapshot
    after: SlideSnapshot
    timestamp: datetime = field(default_factory=datetime.now)


class EditHistory:
    """Two-stack edit history supporting undo and redo.

    - ``_undo_stack``: edits that can be undone.
    - ``_redo_stack``: edits that can be redone (cleared on new edit).
    """

    def __init__(self):
        self._undo_stack: list[EditRecord] = []
        self._redo_stack: list[EditRecord] = []

    def push(self, record: EditRecord) -> None:
        """Record a new edit. Clears redo history."""
        self._undo_stack.append(record)
        self._redo_stack.clear()

    def undo(self) -> EditRecord | None:
        """Pop the most recent edit from undo stack, push to redo stack."""
        if not self._undo_stack:
            return None
        record = self._undo_stack.pop()
        self._redo_stack.append(record)
        return record

    def redo(self) -> EditRecord | None:
        """Pop the most recent edit from redo stack, push to undo stack."""
        if not self._redo_stack:
            return None
        record = self._redo_stack.pop()
        self._undo_stack.append(record)
        return record

    @property
    def can_undo(self) -> bool:
        return len(self._undo_stack) > 0

    @property
    def can_redo(self) -> bool:
        return len(self._redo_stack) > 0

    @property
    def undo_count(self) -> int:
        return len(self._undo_stack)

    @property
    def redo_count(self) -> int:
        return len(self._redo_stack)

    def clear(self) -> None:
        """Clear all history."""
        self._undo_stack.clear()
        self._redo_stack.clear()
