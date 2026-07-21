"""Single-slide artifact model and disk-backed storage (D).

A :class:`SlideArtifact` is the stable, persistent description of one slide
during editing.  It follows the plan's *single-page artifact model*: a stable
``slide_id``, the owning ``task_id``, ordering ``index``, ``status``,
generation ``mode`` (``template`` / ``html``), ``layout_name``, the editable
``structured_data``, the source / preview paths, and the current ``revision``.

Storage layout (under ``<workspace>/<task_id>/slides/<slide_id>/``)::

    current.json            # the SlideArtifact
    revisions/
        <n>/slide.json      # revision metadata + actions + shapes_data
        <n>/preview.html    # rendered preview for that revision
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from pptagent.presentation import Picture, SlidePage


# ── structured data extraction ─────────────────────────────────


def extract_structured_data(slide: SlidePage) -> dict[str, Any]:
    """Extract an editable, JSON-friendly description of a slide's content.

    The result is used both as the ``structured_data`` field of a
    :class:`SlideArtifact` and as the context fed to the editing model, so the
    element / paragraph / image ids match the ids the low-level edit APIs use
    (``div_id`` / ``paragraph_id`` = ``shape_idx`` / ``para.idx``).
    """
    elements: list[dict[str, Any]] = []
    for shape in slide.shapes:
        shape_idx = getattr(shape, "shape_idx", -1)
        name = getattr(shape, "name", "") or f"element_{shape_idx}"
        is_text = hasattr(shape, "text_frame") and shape.text_frame.is_textframe
        is_image = isinstance(shape, Picture)
        element: dict[str, Any] = {
            "element_id": shape_idx,
            "name": name,
            "type": "image" if is_image else "text",
        }
        if is_text:
            element["paragraphs"] = [
                {"id": p.idx, "text": p.text}
                for p in shape.text_frame.paragraphs
                if p.idx != -1 and p.text
            ]
        if is_image:
            element["images"] = [
                {
                    "id": shape_idx,
                    "caption": getattr(shape, "caption", ""),
                    "path": getattr(shape, "img_path", ""),
                }
            ]
        elements.append(element)

    return {
        "title": slide.slide_title,
        "layout_name": slide.slide_layout_name,
        "elements": elements,
    }


def format_structured_context(data: dict[str, Any]) -> str:
    """Render ``structured_data`` as the human/machine readable context block."""
    lines: list[str] = []
    lines.append(f"页面标题: {data.get('title') or '(无)'}")
    lines.append(f"布局名称: {data.get('layout_name') or '(未知)'}")
    for el in data.get("elements", []):
        if el["type"] == "text":
            lines.append(
                f"[文本元素 shape_idx={el['element_id']} name=\"{el['name']}\"]"
            )
            for p in el.get("paragraphs", []):
                lines.append(f"    段落 paragraph_id={p['id']}: \"{p['text']}\"")
        else:
            for img in el.get("images", []):
                lines.append(
                    f"[图片元素 img_id={el['element_id']} name=\"{el['name']}\" "
                    f"caption=\"{img.get('caption', '')}\"]"
                )
    return "\n".join(lines)


# ── artifact model ─────────────────────────────────────────────


@dataclass
class SlideArtifact:
    """Persistent, per-slide artifact for the editing workflow."""

    slide_id: str
    index: int
    mode: str = "template"  # "template" | "html"
    task_id: str | None = None
    status: str = "ready"  # ready | editing | failed
    layout_name: str | None = None
    structured_data: dict[str, Any] = field(default_factory=dict)
    source_path: str | None = None
    preview_path: str | None = None
    revision: int = 0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())

    def touch(self) -> None:
        self.updated_at = datetime.now().isoformat()

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SlideArtifact":
        return cls(**data)


# ── disk storage ───────────────────────────────────────────────


class ArtifactStore:
    """Persist :class:`SlideArtifact` and per-revision data to disk."""

    def __init__(self, workspace: Path | str, task_id: str | None = None):
        self.root = Path(workspace)
        self.task_id = task_id
        self.slides_dir = self.root / (task_id or "_global") / "slides"

    # ── paths ──────────────────────────────────────────────────

    def _slide_dir(self, slide_id: str) -> Path:
        d = self.slides_dir / slide_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def _revision_dir(self, slide_id: str, number: int) -> Path:
        d = self._slide_dir(slide_id) / "revisions" / str(number)
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── artifact ───────────────────────────────────────────────

    def save_artifact(self, art: SlideArtifact) -> None:
        art.touch()
        path = self._slide_dir(art.slide_id) / "current.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump(art.to_dict(), f, ensure_ascii=False, indent=2)

    def load_artifact(self, slide_id: str) -> SlideArtifact | None:
        path = self._slide_dir(slide_id) / "current.json"
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return SlideArtifact.from_dict(json.load(f))

    def list_artifacts(self, task_id: str | None = None) -> list[SlideArtifact]:
        if not self.slides_dir.exists():
            return []
        out: list[SlideArtifact] = []
        for slide_dir in self.slides_dir.iterdir():
            art = self.load_artifact(slide_dir.name)
            if art is not None and (task_id is None or art.task_id == task_id):
                out.append(art)
        out.sort(key=lambda a: a.index)
        return out

    # ── revisions ──────────────────────────────────────────────

    def save_revision(
        self,
        slide_id: str,
        number: int,
        instruction: str,
        actions: list[str],
        status: str,
        checksum: str,
        created_at: str,
        dialogue: list[dict] | None = None,
        message: str = "",
        shapes_data: list | None = None,
        preview_path: str | None = None,
    ) -> None:
        payload = {
            "slide_id": slide_id,
            "number": number,
            "instruction": instruction,
            "actions": actions,
            "status": status,
            "checksum": checksum,
            "created_at": created_at,
            "dialogue": dialogue or [],
            "message": message,
            "shapes_data": shapes_data or [],
            "preview_path": preview_path,
        }
        with open(self._revision_dir(slide_id, number) / "slide.json", "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    def load_revision(self, slide_id: str, number: int) -> dict | None:
        path = self._revision_dir(slide_id, number) / "slide.json"
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)

    def delete_revision(self, slide_id: str, number: int) -> None:
        d = self._revision_dir(slide_id, number)
        if d.exists():
            for child in d.iterdir():
                child.unlink()
            d.rmdir()

    def save_preview_file(self, slide_id: str, number: int, html: str) -> Path:
        path = self._revision_dir(slide_id, number) / "preview.html"
        path.write_text(html, encoding="utf-8")
        return path

    def clear(self) -> None:
        """Remove the whole store (used by tests / reset)."""
        import shutil

        if self.slides_dir.exists():
            shutil.rmtree(self.slides_dir, ignore_errors=True)
