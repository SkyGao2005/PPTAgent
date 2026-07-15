"""Single-slide editor with automatic checkpointing."""

from pptagent.apis import (
    clone_paragraph,
    del_image,
    del_paragraph,
    replace_image,
    replace_paragraph,
    SlideEditError,
)
from pptagent.document import Document
from pptagent.presentation.presentation import Presentation, SlidePage
from pptagent.editor.snapshot import SlideSnapshot
from pptagent.editor.history import EditHistory, EditRecord


class SlideEditor:
    """Edit a single slide with automatic snapshot-based checkpointing.

    Wraps the low-level edit APIs in ``pptagent.apis`` and records
    every mutation so it can be undone / redone.
    """

    def __init__(
        self,
        slide: SlidePage,
        presentation: Presentation,
        doc: Document | None = None,
    ):
        self.slide = slide
        self.prs = presentation
        self.doc = doc
        self.history = EditHistory()

    # ── text editing ──────────────────────────────────────────

    def edit_text(self, div_id: int, paragraph_id: int, new_text: str) -> SlideSnapshot:
        """Replace the text of a paragraph."""
        before = SlideSnapshot.capture(self.slide)
        replace_paragraph(self.slide, div_id, paragraph_id, new_text)
        after = SlideSnapshot.capture(self.slide)
        record = EditRecord(
            operation="edit_text",
            params={"div_id": div_id, "paragraph_id": paragraph_id, "new_text": new_text},
            before=before,
            after=after,
        )
        self.history.push(record)
        return after

    def clone_paragraph(self, div_id: int, paragraph_id: int) -> SlideSnapshot:
        """Clone (duplicate) a paragraph."""
        before = SlideSnapshot.capture(self.slide)
        clone_paragraph(self.slide, div_id, paragraph_id)
        after = SlideSnapshot.capture(self.slide)
        record = EditRecord(
            operation="clone_paragraph",
            params={"div_id": div_id, "paragraph_id": paragraph_id},
            before=before,
            after=after,
        )
        self.history.push(record)
        return after

    def delete_paragraph(self, div_id: int, paragraph_id: int) -> SlideSnapshot:
        """Delete a paragraph."""
        before = SlideSnapshot.capture(self.slide)
        del_paragraph(self.slide, div_id, paragraph_id)
        after = SlideSnapshot.capture(self.slide)
        record = EditRecord(
            operation="delete_paragraph",
            params={"div_id": div_id, "paragraph_id": paragraph_id},
            before=before,
            after=after,
        )
        self.history.push(record)
        return after

    # ── image editing ─────────────────────────────────────────

    def edit_image(self, img_id: int, new_image_path: str) -> SlideSnapshot:
        """Replace an image."""
        if self.doc is None:
            raise ValueError("Document is required for image replacement")
        before = SlideSnapshot.capture(self.slide)
        replace_image(self.slide, self.doc, img_id, new_image_path)
        after = SlideSnapshot.capture(self.slide)
        record = EditRecord(
            operation="edit_image",
            params={"img_id": img_id, "new_image_path": new_image_path},
            before=before,
            after=after,
        )
        self.history.push(record)
        return after

    def delete_image(self, img_id: int) -> SlideSnapshot:
        """Delete an image."""
        before = SlideSnapshot.capture(self.slide)
        del_image(self.slide, img_id)
        after = SlideSnapshot.capture(self.slide)
        record = EditRecord(
            operation="delete_image",
            params={"img_id": img_id},
            before=before,
            after=after,
        )
        self.history.push(record)
        return after

    # ── style restyling ────────────────────────────────────────

    def restyle(self, strategy: "StyleStrategy") -> SlideSnapshot:
        """Apply a style strategy (color + font) to the entire slide.

        ``strategy`` is a ``StyleStrategy`` from ``pptagent.editor.styles``.
        The operation is automatically checkpointed and undo-able.
        """
        from pptagent.editor.styles import StyleStrategy

        before = SlideSnapshot.capture(self.slide)

        for shape in self.slide.shapes:
            el_name = getattr(shape, "name", "").lower()
            is_title = "title" in el_name

            # ── font preferences ──
            if hasattr(shape, "font"):
                if is_title:
                    if strategy.fonts.title_family:
                        shape.font.name = strategy.fonts.title_family
                    if strategy.fonts.title_size:
                        shape.font.size = strategy.fonts.title_size
                    if strategy.fonts.bold_titles:
                        shape.font.bold = True
                    if strategy.palette.title_color:
                        shape.font.color = strategy.palette.title_color
                else:
                    if strategy.fonts.body_family:
                        shape.font.name = strategy.fonts.body_family
                    if strategy.fonts.body_size:
                        shape.font.size = strategy.fonts.body_size
                    if strategy.palette.body_color:
                        shape.font.color = strategy.palette.body_color

        after = SlideSnapshot.capture(self.slide)
        record = EditRecord(
            operation="restyle",
            params={
                "style": strategy.name,
                "palette": strategy.palette.to_dict(),
                "fonts": strategy.fonts.to_dict(),
            },
            before=before,
            after=after,
        )
        self.history.push(record)
        return after

    # ── undo / redo ───────────────────────────────────────────

    def undo(self) -> SlideSnapshot | None:
        """Undo the last edit. Returns the restored snapshot or None."""
        record = self.history.undo()
        if record is None:
            return None
        record.before.restore(self.slide)
        # Re-validate with presentation
        self.prs.validate(self.slide)
        return SlideSnapshot.capture(self.slide)

    def redo(self) -> SlideSnapshot | None:
        """Redo the last undone edit. Returns the restored snapshot or None."""
        record = self.history.redo()
        if record is None:
            return None
        record.after.restore(self.slide)
        self.prs.validate(self.slide)
        return SlideSnapshot.capture(self.slide)

    @property
    def can_undo(self) -> bool:
        return self.history.can_undo

    @property
    def can_redo(self) -> bool:
        return self.history.can_redo
