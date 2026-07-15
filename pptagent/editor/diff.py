"""Precise slide diff engine.

Compares two ``SlideSnapshot`` objects element by element and produces
a detailed change list: which text changed, which image was replaced, etc.
"""

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pptagent.editor.snapshot import SlideSnapshot, PresentationSnapshot


# ── change primitives ────────────────────────────────────────

@dataclass
class TextChange:
    """A single paragraph text modification."""
    shape_idx: int
    element_name: str
    paragraph_idx: int
    old_text: str
    new_text: str

    def describe(self) -> str:
        return (
            f"元素[{self.shape_idx}]「{self.element_name}」"
            f"第{self.paragraph_idx}段: "
            f'"{self.old_text[:40]}{"..." if len(self.old_text) > 40 else ""}"'
            f" → "
            f'"{self.new_text[:40]}{"..." if len(self.new_text) > 40 else ""}"'
        )


@dataclass
class ImageChange:
    """An image replacement."""
    shape_idx: int
    element_name: str
    old_image: str | None   # None means image was added
    new_image: str | None   # None means image was removed

    def describe(self) -> str:
        if self.old_image is None:
            return f"元素[{self.shape_idx}]「{self.element_name}」: 新增图片「{self.new_image}」"
        if self.new_image is None:
            return f"元素[{self.shape_idx}]「{self.element_name}」: 删除图片「{self.old_image}」"
        return f"元素[{self.shape_idx}]「{self.element_name}」: 图片「{self.old_image}」→「{self.new_image}」"


@dataclass
class ParagraphCountChange:
    """Number of paragraphs changed (added or removed)."""
    shape_idx: int
    element_name: str
    old_count: int
    new_count: int

    def describe(self) -> str:
        diff = self.new_count - self.old_count
        return (
            f"元素[{self.shape_idx}]「{self.element_name}」"
            f"段落数: {self.old_count} → {self.new_count} "
            f"({'新增' if diff > 0 else '减少'}{abs(diff)}段)"
        )


# ── slide-level diff ─────────────────────────────────────────

@dataclass
class SlideDiff:
    """All changes detected between two versions of a slide."""
    slide_idx: int
    text_changes: list[TextChange] = field(default_factory=list)
    image_changes: list[ImageChange] = field(default_factory=list)
    count_changes: list[ParagraphCountChange] = field(default_factory=list)
    timestamp: datetime = field(default_factory=datetime.now)

    @property
    def is_empty(self) -> bool:
        return not any([self.text_changes, self.image_changes, self.count_changes])

    @property
    def total_changes(self) -> int:
        return len(self.text_changes) + len(self.image_changes) + len(self.count_changes)

    def summary(self) -> str:
        """One-line summary of all changes on this slide."""
        if self.is_empty:
            return f"第{self.slide_idx}页: 无变化"
        parts = []
        if self.text_changes:
            parts.append(f"{len(self.text_changes)}处文字修改")
        if self.image_changes:
            parts.append(f"{len(self.image_changes)}处图片变更")
        if self.count_changes:
            parts.append(f"{len(self.count_changes)}处段落增减")
        return f"第{self.slide_idx}页: {', '.join(parts)}"

    def detail(self) -> str:
        """Multi-line detailed change report."""
        lines = [self.summary()]
        for c in self.text_changes:
            lines.append(f"   [文字] {c.describe()}")
        for c in self.image_changes:
            lines.append(f"   [图片] {c.describe()}")
        for c in self.count_changes:
            lines.append(f"   [结构] {c.describe()}")
        return "\n".join(lines)


# ── diff engine ──────────────────────────────────────────────

class DiffEngine:
    """Compare snapshots and produce detailed diffs."""

    @staticmethod
    def _extract_data(snapshot: SlideSnapshot) -> list[dict[str, Any]]:
        """Extract text and image info from every shape in a snapshot.

        ``shapes_data`` is already a list of JSON-compatible dicts from
        ``Snapshot._shape_to_dict()`` — use that directly.
        """
        return snapshot.shapes_data

    @classmethod
    def diff_slides(cls, before: SlideSnapshot, after: SlideSnapshot) -> SlideDiff:
        """Compare two slide snapshots element by element."""
        diff = SlideDiff(slide_idx=after.slide_idx)

        if before.is_same_content(after):
            return diff  # fast path

        old_shapes = cls._extract_data(before)
        new_shapes = cls._extract_data(after)

        # Compare shapes by index
        for i, (old_s, new_s) in enumerate(zip(old_shapes, new_shapes)):
            el_name = new_s["element_name"]
            shape_idx = new_s["shape_idx"]

            # -- text paragraph comparison --
            old_paras = old_s.get("paragraphs", [])
            new_paras = new_s.get("paragraphs", [])
            if old_paras or new_paras:
                # Check paragraph count change
                if len(old_paras) != len(new_paras):
                    diff.count_changes.append(ParagraphCountChange(
                        shape_idx=shape_idx,
                        element_name=el_name,
                        old_count=len(old_paras),
                        new_count=len(new_paras),
                    ))
                # Check paragraph text changes
                for idx in range(min(len(old_paras), len(new_paras))):
                    if old_paras[idx]["text"] != new_paras[idx]["text"]:
                        diff.text_changes.append(TextChange(
                            shape_idx=shape_idx,
                            element_name=el_name,
                            paragraph_idx=idx,
                            old_text=old_paras[idx]["text"],
                            new_text=new_paras[idx]["text"],
                        ))

            # -- image comparison --
            old_imgs = old_s.get("images", [])
            new_imgs = new_s.get("images", [])
            for idx in range(max(len(old_imgs), len(new_imgs))):
                old_img = old_imgs[idx] if idx < len(old_imgs) else None
                new_img = new_imgs[idx] if idx < len(new_imgs) else None
                if old_img != new_img:
                    diff.image_changes.append(ImageChange(
                        shape_idx=shape_idx,
                        element_name=el_name,
                        old_image=old_img,
                        new_image=new_img,
                    ))

        return diff

    @classmethod
    def diff_presentations(
        cls, before: PresentationSnapshot, after: PresentationSnapshot
    ) -> list[SlideDiff]:
        """Compare two presentation snapshots page by page."""
        diffs = []
        for b_snap, a_snap in zip(before.slides, after.slides):
            d = cls.diff_slides(b_snap, a_snap)
            if not d.is_empty:
                diffs.append(d)
        return diffs
