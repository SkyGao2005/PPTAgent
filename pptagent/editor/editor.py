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

    If a ``PreviewRenderer`` is attached via ``set_preview()``,
    the preview HTML is automatically regenerated after every edit,
    enabling real-time WYSIWYG workflows.
    """
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
        self._preview_renderer = None
        self._total_slides = len(presentation.slides)

    # ── preview integration ────────────────────────────────────

    def set_preview(self, renderer: "PreviewRenderer") -> None:
        """Attach a PreviewRenderer for auto-refresh after each edit."""
        self._preview_renderer = renderer

    def set_total_slides(self, count: int) -> None:
        """Set total slide count for preview page indicator."""
        self._total_slides = count

    def preview(self) -> "PreviewResult | None":
        """Render and return a fresh preview of the current slide state."""
        if self._preview_renderer is None:
            return None
        return self._preview_renderer.render(
            self.slide,
            slide_idx=self.slide.slide_idx,
            total_slides=self._total_slides,
        )

    def save_preview(self, output_dir: str) -> "PreviewResult | None":
        """Render and save a preview HTML file."""
        if self._preview_renderer is None:
            return None
        return self._preview_renderer.save_preview(
            self.slide,
            f"{output_dir}/slide_{self.slide.slide_idx:02d}.html",
            slide_idx=self.slide.slide_idx,
            total_slides=self._total_slides,
        )

    # ── internal ───────────────────────────────────────────────

    def _checkpoint(self, operation: str, params: dict,
                    before: "SlideSnapshot", after: "SlideSnapshot"):
        """Record edit history and auto-refresh preview."""
        record = EditRecord(
            operation=operation, params=params,
            before=before, after=after,
        )
        self.history.push(record)
        # Auto-refresh preview
        if self._preview_renderer:
            self._preview_renderer.render(
                self.slide,
                slide_idx=self.slide.slide_idx,
                total_slides=self._total_slides,
            )

    # ── text editing ──────────────────────────────────────────

    def edit_text(self, div_id: int, paragraph_id: int, new_text: str) -> SlideSnapshot:
        """Replace the text of a paragraph."""
        before = SlideSnapshot.capture(self.slide)
        replace_paragraph(self.slide, div_id, paragraph_id, new_text)
        after = SlideSnapshot.capture(self.slide)
        self._checkpoint("edit_text", {
            "div_id": div_id, "paragraph_id": paragraph_id, "new_text": new_text
        }, before, after)
        return after

    def clone_paragraph(self, div_id: int, paragraph_id: int) -> SlideSnapshot:
        """Clone (duplicate) a paragraph."""
        before = SlideSnapshot.capture(self.slide)
        clone_paragraph(self.slide, div_id, paragraph_id)
        after = SlideSnapshot.capture(self.slide)
        self._checkpoint("clone_paragraph", {
            "div_id": div_id, "paragraph_id": paragraph_id
        }, before, after)
        return after

    def delete_paragraph(self, div_id: int, paragraph_id: int) -> SlideSnapshot:
        """Delete a paragraph."""
        before = SlideSnapshot.capture(self.slide)
        del_paragraph(self.slide, div_id, paragraph_id)
        after = SlideSnapshot.capture(self.slide)
        self._checkpoint("delete_paragraph", {
            "div_id": div_id, "paragraph_id": paragraph_id
        }, before, after)
        return after

    # ── image editing ─────────────────────────────────────────

    def edit_image(self, img_id: int, new_image_path: str) -> SlideSnapshot:
        """Replace an image."""
        if self.doc is None:
            raise ValueError("Document is required for image replacement")
        before = SlideSnapshot.capture(self.slide)
        replace_image(self.slide, self.doc, img_id, new_image_path)
        after = SlideSnapshot.capture(self.slide)
        self._checkpoint("edit_image", {
            "img_id": img_id, "new_image_path": new_image_path
        }, before, after)
        return after

    def delete_image(self, img_id: int) -> SlideSnapshot:
        """Delete an image."""
        before = SlideSnapshot.capture(self.slide)
        del_image(self.slide, img_id)
        after = SlideSnapshot.capture(self.slide)
        self._checkpoint("delete_image", {
            "img_id": img_id
        }, before, after)
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

            # ── font preferences (at paragraph level) ──
            if hasattr(shape, "text_frame") and shape.text_frame.is_textframe:
                for para in shape.text_frame.paragraphs:
                    if not hasattr(para, "font"):
                        continue
                    if is_title:
                        if strategy.fonts.title_family:
                            para.font.name = strategy.fonts.title_family
                        if strategy.fonts.title_size:
                            para.font.size = strategy.fonts.title_size
                        if strategy.fonts.bold_titles:
                            para.font.bold = True
                        if strategy.palette.title_color:
                            para.font.color = strategy.palette.title_color
                    else:
                        if strategy.fonts.body_family:
                            para.font.name = strategy.fonts.body_family
                        if strategy.fonts.body_size:
                            para.font.size = strategy.fonts.body_size
                        if strategy.palette.body_color:
                            para.font.color = strategy.palette.body_color

        after = SlideSnapshot.capture(self.slide)
        self._checkpoint("restyle", {
            "style": strategy.name,
            "palette": strategy.palette.to_dict(),
            "fonts": strategy.fonts.to_dict(),
        }, before, after)
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
