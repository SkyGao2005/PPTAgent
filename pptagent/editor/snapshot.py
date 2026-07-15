"""Snapshot system for capturing and restoring PPT slide state.

In-memory snapshots use ``copy.deepcopy`` for fast undo/redo.
Disk serialization uses JSON-compatible ``shapes_data`` dicts.
"""

import hashlib
import json
from copy import deepcopy
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from pptagent.presentation.presentation import Picture, Presentation, SlidePage
from pptagent.presentation.shapes import ClosureType


# ── shape serialization helpers ───────────────────────────────

def _shape_to_dict(shape) -> dict[str, Any]:
    """Extract a JSON-serializable dict from a single shape."""
    data: dict[str, Any] = {
        "shape_idx": getattr(shape, "shape_idx", -1),
        "element_name": getattr(shape, "name", f"shape_{getattr(shape, 'shape_idx', 0)}"),
        "paragraphs": [],
        "images": [],
    }

    if hasattr(shape, "text_frame") and shape.text_frame.is_textframe:
        for para in shape.text_frame.paragraphs:
            if para.idx != -1 and para.text:
                data["paragraphs"].append({
                    "idx": para.idx,
                    "text": para.text,
                })

    if isinstance(shape, Picture):
        data["images"].append({
            "path": getattr(shape, "img_path", getattr(shape, "caption", "")),
            "caption": getattr(shape, "caption", ""),
        })

    return data


def _slide_to_json(slide: SlidePage) -> str:
    data: dict[str, Any] = {
        "slide_idx": slide.slide_idx,
        "layout_name": slide.slide_layout_name,
        "title": slide.slide_title,
        "shapes": [_shape_to_dict(s) for s in slide.shapes],
    }
    return json.dumps(data, ensure_ascii=False, sort_keys=True, default=str)


def _compute_checksum(data: str) -> str:
    return hashlib.md5(data.encode()).hexdigest()[:12]


# ── snapshots ─────────────────────────────────────────────────

@dataclass
class SlideSnapshot:
    """A point-in-time snapshot of a single slide.

    Two representations for two use cases:
    - ``shapes_copy``:  deepcopy of ShapeElement list (for in-memory undo/redo)
    - ``shapes_data``:  JSON-safe dict            (for disk persistence)
    """

    slide_idx: int
    layout_name: str | None
    slide_title: str | None
    shapes_copy: list = field(repr=False)   # deepcopy — undo/redo
    shapes_data: list[dict[str, Any]] = field(default_factory=list)  # JSON — disk
    slide_width: int = 0
    slide_height: int = 0
    checksum: str = ""
    timestamp: datetime = field(default_factory=datetime.now)
    parent_checksum: str | None = None

    @classmethod
    def capture(cls, slide: SlidePage) -> "SlideSnapshot":
        # Deep-copy shapes for in-memory restore
        shapes_copy = deepcopy(slide.shapes)
        for shape in shapes_copy:
            if hasattr(shape, "_closures"):
                shape._closures = ClosureType.to_default_dict()

        # JSON-compatible data for disk
        shapes_data = [_shape_to_dict(s) for s in slide.shapes]

        json_data = _slide_to_json(slide)
        checksum = _compute_checksum(json_data)

        return cls(
            slide_idx=slide.slide_idx,
            layout_name=slide.slide_layout_name,
            slide_title=slide.slide_title,
            shapes_copy=shapes_copy,
            shapes_data=shapes_data,
            slide_width=slide.slide_width,
            slide_height=slide.slide_height,
            checksum=checksum,
        )

    def restore(self, slide: SlidePage) -> None:
        """Restore snapshotted state into an existing SlidePage (in-memory)."""
        slide.shapes = deepcopy(self.shapes_copy)

    def is_same_content(self, other: "SlideSnapshot") -> bool:
        return self.checksum == other.checksum

    # ── disk serialization ────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "slide_idx": self.slide_idx,
            "layout_name": self.layout_name,
            "slide_title": self.slide_title,
            "shapes_data": self.shapes_data,
            "slide_width": self.slide_width,
            "slide_height": self.slide_height,
            "checksum": self.checksum,
            "timestamp": self.timestamp.isoformat(),
            "parent_checksum": self.parent_checksum,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SlideSnapshot":
        return cls(
            slide_idx=data["slide_idx"],
            layout_name=data.get("layout_name"),
            slide_title=data.get("slide_title"),
            shapes_copy=[],  # disk-loaded snapshots don't support undo/redo
            shapes_data=data.get("shapes_data", []),
            slide_width=data.get("slide_width", 0),
            slide_height=data.get("slide_height", 0),
            checksum=data.get("checksum", ""),
            timestamp=datetime.fromisoformat(data["timestamp"]) if data.get("timestamp") else datetime.now(),
            parent_checksum=data.get("parent_checksum"),
        )


@dataclass
class PresentationSnapshot:
    """A point-in-time snapshot of an entire presentation."""

    slides: list[SlideSnapshot]
    metadata: dict[str, str] = field(default_factory=dict)
    version_tag: str | None = None
    created_at: datetime = field(default_factory=datetime.now)
    checksum: str = ""

    @classmethod
    def capture(cls, prs: Presentation, version_tag: str | None = None) -> "PresentationSnapshot":
        slides = [SlideSnapshot.capture(slide) for slide in prs.slides]
        checksums = [s.checksum for s in slides]
        aggregate = _compute_checksum("".join(checksums))

        metadata = {
            "source_file": prs.source_file,
            "slide_width": str(prs.slide_width),
            "slide_height": str(prs.slide_height),
            "num_pages": str(prs.num_pages),
        }

        return cls(
            slides=slides,
            metadata=metadata,
            version_tag=version_tag,
            checksum=aggregate,
        )

    def restore(self, prs: Presentation) -> None:
        for snap in self.slides:
            if snap.slide_idx - 1 < len(prs.slides):
                snap.restore(prs.slides[snap.slide_idx - 1])

    def is_same_content(self, other: "PresentationSnapshot") -> bool:
        return self.checksum == other.checksum

    def changed_slides(self, other: "PresentationSnapshot") -> list[int]:
        changed = []
        for i, (s1, s2) in enumerate(zip(self.slides, other.slides)):
            if not s1.is_same_content(s2):
                changed.append(i + 1)
        return changed

    # ── disk serialization ────────────────────────────────────

    def to_dict(self) -> dict[str, Any]:
        return {
            "slides": [s.to_dict() for s in self.slides],
            "metadata": self.metadata,
            "version_tag": self.version_tag,
            "created_at": self.created_at.isoformat(),
            "checksum": self.checksum,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "PresentationSnapshot":
        return cls(
            slides=[SlideSnapshot.from_dict(s) for s in data["slides"]],
            metadata=data.get("metadata", {}),
            version_tag=data.get("version_tag"),
            created_at=datetime.fromisoformat(data["created_at"]) if data.get("created_at") else datetime.now(),
            checksum=data.get("checksum", ""),
        )
