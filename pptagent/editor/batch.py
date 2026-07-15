"""Batch editor: edit multiple slides and commit as a single version.

Wraps multiple ``SlideEditor`` instances so that a group of edits
can be undone / redone together and committed as one atomic version.
"""

from pptagent.presentation import Presentation
from pptagent.editor.editor import SlideEditor
from pptagent.editor.snapshot import PresentationSnapshot
from pptagent.editor.version import VersionManager


class BatchEditor:
    """Edit multiple slides at once, commit as one version.

    Usage::

        batch = BatchEditor(prs)
        batch.edit_text(slide_idx=2, div_id=1, paragraph_id=0, text="New")
        batch.edit_text(slide_idx=3, div_id=1, paragraph_id=0, text="Also new")
        batch.commit("Edited slides 2 and 3 titles")   # one version
    """

    def __init__(self, presentation: Presentation, version_manager: VersionManager | None = None):
        self.prs = presentation
        self.vm = version_manager
        self._editors: dict[int, SlideEditor] = {}

    # ── get or create editor for a slide ──────────────────────

    def _get(self, slide_idx: int) -> SlideEditor:
        """Lazy-create a SlideEditor for the given slide (1-based)."""
        if slide_idx not in self._editors:
            slide = self.prs.slides[slide_idx - 1]
            self._editors[slide_idx] = SlideEditor(slide, self.prs)
        return self._editors[slide_idx]

    # ── delegated editing operations ──────────────────────────

    def edit_text(self, slide_idx: int, div_id: int, paragraph_id: int, new_text: str):
        """Edit text on a specific slide."""
        return self._get(slide_idx).edit_text(div_id, paragraph_id, new_text)

    def edit_image(self, slide_idx: int, img_id: int, new_image_path: str):
        """Replace an image on a specific slide."""
        return self._get(slide_idx).edit_image(img_id, new_image_path)

    def delete_paragraph(self, slide_idx: int, div_id: int, paragraph_id: int):
        """Delete a paragraph on a specific slide."""
        return self._get(slide_idx).delete_paragraph(div_id, paragraph_id)

    def restyle(self, slide_idx: int, strategy):
        """Apply a style strategy to a specific slide."""
        return self._get(slide_idx).restyle(strategy)

    # ── batch undo / redo (track the most recently edited slide) ──

    def undo_last(self) -> SlideEditor | None:
        """Undo the most recent edit across all slides."""
        # Find the editor with the most recent undo stack
        for editor in reversed(list(self._editors.values())):
            if editor.can_undo:
                editor.undo()
                return editor
        return None

    # ── commit ────────────────────────────────────────────────

    def commit(self, message: str, tags: list[str] | None = None):
        """Commit all pending edits as a single version."""
        if self.vm is not None:
            self.vm.commit(self.prs, message, tags)
            # Clear all editor histories since we committed
            for ed in self._editors.values():
                ed.history.clear()

    # ── snapshot ──────────────────────────────────────────────

    def snapshot(self) -> PresentationSnapshot:
        """Take a snapshot of the entire presentation."""
        return PresentationSnapshot.capture(self.prs)

    # ── helpers ───────────────────────────────────────────────

    @property
    def active_slides(self) -> list[int]:
        """List slide indices that have been edited in this batch."""
        return sorted(self._editors.keys())

    @property
    def total_pending_edits(self) -> int:
        """Total number of undo-able edits across all slides."""
        return sum(ed.history.undo_count for ed in self._editors.values())

    def clear(self):
        """Clear all editors and histories (discard pending changes)."""
        self._editors.clear()
