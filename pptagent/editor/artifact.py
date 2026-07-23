"""Per-slide artifact model and on-disk task workspace.

Implements the ``SlideArtifact`` data model (section 4.3) and the
``SlideWorkspace`` directory layout::

    workspace/<task_id>/
    ├── task.json              # task metadata + slide index
    ├── events.jsonl           # append-only event log
    ├── outline.json           # PPT title + outline (global context)
    ├── manuscript.md          # research manuscript
    ├── slides/
    │   └── <slide_id>/
    │       ├── current.json   # current SlideArtifact pointer
    │       └── revisions/
    │           ├── 1/slide.json
    │           ├── 1/preview.png
    │           ├── 2/slide.json
    │           └── 2/preview.png
    └── exports/
        └── latest.pptx

The ``slide_id`` is a stable UUID that survives re-ordering, so a
client can keep talking about "slide 3" even when pages are moved.
"""

import json
import shutil
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any


# ── slide status / mode ───────────────────────────────────────

# status of a slide artifact at a point in time
STATUS_PENDING = "pending"        # not yet generated
STATUS_GENERATING = "generating"   # generation in progress
STATUS_READY = "ready"             # successfully generated, current
STATUS_EDITING = "editing"        # an edit is in progress
STATUS_FAILED = "failed"           # last attempt failed; older rev still valid

# generation mode
MODE_TEMPLATE = "template"         # template-driven (pptagent.PPTGen)
MODE_HTML = "html"                 # HTML slide (deeppresenter / Design Agent)


def new_slide_id() -> str:
    """Return a stable, opaque slide identifier."""
    return uuid.uuid4().hex


# ── slide artifact ─────────────────────────────────────────────

@dataclass
class SlideArtifact:
    """A persistent record of one slide at one revision (section 4.3).

    ``revision`` is the per-page version number; the workspace keeps
    one ``SlideArtifact`` per revision under ``revisions/<rev>/``.
    """

    slide_id: str               # stable UUID, unchanged by re-ordering
    task_id: str
    index: int                  # current 0-based ordering
    status: str = STATUS_PENDING
    mode: str = MODE_TEMPLATE   # "html" | "template"
    layout_name: str | None = None      # template mode only
    structured_data: dict[str, Any] = field(default_factory=dict)
    source_path: str | None = None      # HTML or single-page source file
    preview_path: str | None = None     # PNG/JPG thumbnail
    revision: int = 0                   # current version number
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    # provenance — what produced this revision (for reproducible export)
    edit_kind: str = "generate"        # "generate" | "edit" | "undo" | "apply"
    message: str = ""                  # human-readable edit summary
    failed: bool = False               # True = draft, never promoted to current

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "SlideArtifact":
        return cls(
            slide_id=data["slide_id"],
            task_id=data["task_id"],
            index=data["index"],
            status=data.get("status", STATUS_PENDING),
            mode=data.get("mode", MODE_TEMPLATE),
            layout_name=data.get("layout_name"),
            structured_data=data.get("structured_data", {}),
            source_path=data.get("source_path"),
            preview_path=data.get("preview_path"),
            revision=data.get("revision", 0),
            created_at=data.get("created_at", datetime.now().isoformat()),
            updated_at=data.get("updated_at", datetime.now().isoformat()),
            edit_kind=data.get("edit_kind", "generate"),
            message=data.get("message", ""),
            failed=data.get("failed", False),
        )


# ── task metadata ─────────────────────────────────────────────

@dataclass
class TaskMeta:
    """Lightweight task record persisted as ``task.json``."""

    task_id: str
    title: str = ""
    total_slides: int = 0
    created_at: str = field(default_factory=lambda: datetime.now().isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now().isoformat())
    # ordered list of slide_ids — the page order for export
    slide_order: list[str] = field(default_factory=list)
    # map slide_id -> current revision number (for quick lookup)
    current_revisions: dict[str, int] = field(default_factory=dict)
    # map slide_id -> 0-based index (kept in sync with slide_order)
    slide_indices: dict[str, int] = field(default_factory=dict)
    outline: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["updated_at"] = datetime.now().isoformat()
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "TaskMeta":
        return cls(
            task_id=data["task_id"],
            title=data.get("title", ""),
            total_slides=data.get("total_slides", 0),
            created_at=data.get("created_at", datetime.now().isoformat()),
            updated_at=data.get("updated_at", datetime.now().isoformat()),
            slide_order=data.get("slide_order", []),
            current_revisions=data.get("current_revisions", {}),
            slide_indices=data.get("slide_indices", {}),
            outline=data.get("outline", {}),
        )


# ── workspace ─────────────────────────────────────────────────

class SlideWorkspace:
    """On-disk workspace for one task (section 4.3 layout).

    Handles all path resolution and JSON persistence so the revision
    store and edit service can stay focused on logic.
    """

    def __init__(self, task_id: str, root: Path | str):
        self.task_id = task_id
        self.root = Path(root) / task_id
        self.slides_dir = self.root / "slides"
        self.exports_dir = self.root / "exports"
        self.events_path = self.root / "events.jsonl"
        self.task_path = self.root / "task.json"
        self.outline_path = self.root / "outline.json"
        self.manuscript_path = self.root / "manuscript.md"
        # ensure base dirs exist
        self.root.mkdir(parents=True, exist_ok=True)
        self.slides_dir.mkdir(exist_ok=True)
        self.exports_dir.mkdir(exist_ok=True)

    # ── task meta ─────────────────────────────────────────────

    def load_meta(self) -> TaskMeta | None:
        if not self.task_path.exists():
            return None
        with open(self.task_path, "r", encoding="utf-8") as f:
            return TaskMeta.from_dict(json.load(f))

    def save_meta(self, meta: TaskMeta) -> None:
        meta.task_id = self.task_id
        with open(self.task_path, "w", encoding="utf-8") as f:
            json.dump(meta.to_dict(), f, ensure_ascii=False, indent=2)

    def init_meta(self, title: str = "", total_slides: int = 0,
                  outline: dict[str, Any] | None = None) -> TaskMeta:
        """Create or overwrite the task meta."""
        meta = TaskMeta(
            task_id=self.task_id, title=title, total_slides=total_slides,
            outline=outline or {},
        )
        self.save_meta(meta)
        return meta

    # ── outline / manuscript ───────────────────────────────────

    def save_outline(self, outline: dict[str, Any]) -> None:
        with open(self.outline_path, "w", encoding="utf-8") as f:
            json.dump(outline, f, ensure_ascii=False, indent=2)
        meta = self.load_meta()
        if meta is not None:
            meta.outline = outline
            self.save_meta(meta)

    def load_outline(self) -> dict[str, Any]:
        if not self.outline_path.exists():
            return {}
        with open(self.outline_path, "r", encoding="utf-8") as f:
            return json.load(f)

    def save_manuscript(self, text: str) -> None:
        self.manuscript_path.write_text(text, encoding="utf-8")

    # ── slide directory layout ────────────────────────────────

    def slide_dir(self, slide_id: str) -> Path:
        d = self.slides_dir / slide_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    def revisions_dir(self, slide_id: str) -> Path:
        d = self.slide_dir(slide_id) / "revisions"
        d.mkdir(parents=True, exist_ok=True)
        return d

    def revision_dir(self, slide_id: str, revision: int) -> Path:
        d = self.revisions_dir(slide_id) / str(revision)
        d.mkdir(parents=True, exist_ok=True)
        return d

    def current_path(self, slide_id: str) -> Path:
        return self.slide_dir(slide_id) / "current.json"

    def slide_json_path(self, slide_id: str, revision: int) -> Path:
        return self.revision_dir(slide_id, revision) / "slide.json"

    def preview_path_for(self, slide_id: str, revision: int,
                         ext: str = ".png") -> Path:
        return self.revision_dir(slide_id, revision) / f"preview{ext}"

    def source_path_for(self, slide_id: str, revision: int,
                        ext: str = ".html") -> Path:
        return self.revision_dir(slide_id, revision) / f"source{ext}"

    def drafts_dir(self, slide_id: str) -> Path:
        """Temporary home for failed-draft artifacts (never promoted)."""
        d = self.slide_dir(slide_id) / "drafts"
        d.mkdir(parents=True, exist_ok=True)
        return d

    # ── artifact persistence ──────────────────────────────────

    def save_artifact(self, artifact: SlideArtifact) -> Path:
        """Persist a revision's artifact to ``revisions/<rev>/slide.json``.

        Also writes ``current.json`` when the artifact is not a failed
        draft, so the current pointer is always on disk.
        """
        artifact.task_id = self.task_id
        path = self.slide_json_path(artifact.slide_id, artifact.revision)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(artifact.to_dict(), f, ensure_ascii=False, indent=2)
        if not artifact.failed:
            self.set_current(artifact.slide_id, artifact)
        return path

    def load_artifact(self, slide_id: str, revision: int) -> SlideArtifact | None:
        path = self.slide_json_path(slide_id, revision)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return SlideArtifact.from_dict(json.load(f))

    def list_revisions(self, slide_id: str) -> list[int]:
        """Return sorted revision numbers for a slide (ascending)."""
        rdir = self.revisions_dir(slide_id)
        revs = []
        for p in rdir.iterdir():
            if p.is_dir() and p.name.isdigit():
                revs.append(int(p.name))
        return sorted(revs)

    def set_current(self, slide_id: str, artifact: SlideArtifact) -> None:
        """Write/overwrite ``current.json`` — the live pointer."""
        with open(self.current_path(slide_id), "w", encoding="utf-8") as f:
            json.dump(artifact.to_dict(), f, ensure_ascii=False, indent=2)
        # keep task meta in sync
        meta = self.load_meta()
        if meta is not None:
            meta.current_revisions[slide_id] = artifact.revision
            if slide_id not in meta.slide_indices:
                meta.slide_indices[slide_id] = artifact.index
            self.save_meta(meta)

    def get_current(self, slide_id: str) -> SlideArtifact | None:
        path = self.current_path(slide_id)
        if not path.exists():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return SlideArtifact.from_dict(json.load(f))

    def current_revision(self, slide_id: str) -> int:
        cur = self.get_current(slide_id)
        return cur.revision if cur else 0

    def list_slide_ids(self) -> list[str]:
        if not self.slides_dir.exists():
            return []
        return sorted(
            p.name for p in self.slides_dir.iterdir()
            if p.is_dir() and p.name not in ("revisions",)
        )

    # ── revision cleanup ──────────────────────────────────────

    def prune_revisions(self, slide_id: str, keep: int = 10) -> list[int]:
        """Keep the most recent ``keep`` revisions.

        Per the version strategy: delete the oldest *preview* files
        beyond the limit; structured ``slide.json`` may be retained for
        the audit log. Here we drop the whole oldest revision dir to
        keep the workspace bounded, matching the "keep last 10" rule.
        Returns the list of pruned revision numbers.
        """
        revs = self.list_revisions(slide_id)
        if len(revs) <= keep:
            return []
        current_rev = self.current_revision(slide_id)
        # never prune the current revision
        to_prune = [r for r in revs if r != current_rev]
        to_prune.sort()
        prune_count = len(revs) - keep
        pruned = to_prune[:prune_count]
        for r in pruned:
            rdir = self.revisions_dir(slide_id) / str(r)
            if rdir.exists():
                shutil.rmtree(rdir, ignore_errors=True)
        return pruned

    # ── export helpers ────────────────────────────────────────

    def export_path(self, name: str = "latest.pptx") -> Path:
        return self.exports_dir / name

    def save_export_manifest(self, revisions: dict[str, int],
                             output_path: str) -> Path:
        """Record which revision was used per slide (reproducible export)."""
        path = self.exports_dir / "export_manifest.json"
        with open(path, "w", encoding="utf-8") as f:
            json.dump({
                "task_id": self.task_id,
                "output_path": output_path,
                "revisions": revisions,
                "created_at": datetime.now().isoformat(),
            }, f, ensure_ascii=False, indent=2)
        return path


# ── structured-data helpers used by SlideEditService ──────────


def extract_structured_data(slide: Any) -> dict[str, Any]:
    """Extract editable text and element identifiers from a ``SlidePage``."""
    try:
        from pptagent.presentation import Picture, SlidePage

        if isinstance(slide, SlidePage):
            body_texts: list[str] = []
            elements: list[dict[str, Any]] = []
            for shape in getattr(slide, "shapes", []):
                text = getattr(shape, "text", "") or ""
                if text.strip():
                    body_texts.append(text.strip())
                shape_idx = getattr(shape, "shape_idx", -1)
                is_text = (
                    hasattr(shape, "text_frame")
                    and shape.text_frame.is_textframe
                )
                element: dict[str, Any] = {
                    "element_id": shape_idx,
                    "name": getattr(shape, "name", "") or f"element_{shape_idx}",
                    "type": "image" if isinstance(shape, Picture) else "text",
                }
                if is_text:
                    element["paragraphs"] = [
                        {"id": paragraph.idx, "text": paragraph.text}
                        for paragraph in shape.text_frame.paragraphs
                        if paragraph.idx != -1 and paragraph.text
                    ]
                if isinstance(shape, Picture):
                    element["images"] = [{
                        "id": shape_idx,
                        "caption": getattr(shape, "caption", ""),
                        "path": getattr(shape, "img_path", ""),
                    }]
                elements.append(element)
            return {
                "title": getattr(slide, "slide_title", "") or "",
                "subtitle": getattr(slide, "subtitle", "") or "",
                "body": body_texts,
                "layout_name": getattr(slide, "slide_layout_name", "") or "",
                "elements": elements,
            }
    except ImportError:
        pass
    return {
        "title": "",
        "subtitle": "",
        "body": [],
        "layout_name": "",
        "elements": [],
    }


def format_structured_context(data: dict[str, Any]) -> str:
    """Render structured slide data as a human-readable context string."""
    parts: list[str] = []
    title = data.get("title", "")
    subtitle = data.get("subtitle", "")
    body = data.get("body", [])
    if title:
        parts.append(f"标题: {title}")
    if subtitle:
        parts.append(f"副标题: {subtitle}")
    if body:
        parts.append("正文:")
        for i, line in enumerate(body, 1):
            parts.append(f"  {i}. {line}")
    return "\n".join(parts)


# ── ArtifactStore: lightweight persistence used by SlideEditService ──

class ArtifactStore:
    """Minimal on-disk store for slide artifacts, revisions, and previews."""

    def __init__(
        self,
        workspace: Path | str,
        task_id: str | None = None,
    ) -> None:
        self.workspace = Path(workspace) / task_id if task_id else Path(workspace)
        self.workspace.mkdir(parents=True, exist_ok=True)

    def save_preview_file(self, slide_id: str, revision: int, html: str) -> str:
        rev_dir = self.workspace / "slides" / slide_id / "revisions" / str(revision)
        rev_dir.mkdir(parents=True, exist_ok=True)
        path = rev_dir / "preview.html"
        path.write_text(html, encoding="utf-8")
        return str(path)

    def save_artifact(self, artifact: SlideArtifact) -> None:
        slide_dir = self.workspace / "slides" / artifact.slide_id
        slide_dir.mkdir(parents=True, exist_ok=True)
        current = slide_dir / "current.json"
        current.write_text(json.dumps(asdict(artifact), ensure_ascii=False, indent=2), encoding="utf-8")

    def load_artifact(self, slide_id: str) -> SlideArtifact | None:
        """Load the current artifact for compatibility with the legacy service."""
        current = self.workspace / "slides" / slide_id / "current.json"
        if not current.exists():
            return None
        return SlideArtifact.from_dict(json.loads(current.read_text(encoding="utf-8")))

    def save_revision(self, slide_id: str, number: int, instruction: str,
                      actions: list[str], status: str, checksum: str,
                      created_at: str, dialogue: list[dict[str, Any]],
                      message: str = "", shapes_data: list[Any] | None = None,
                      preview_path: str | None = None) -> None:
        rev_dir = self.workspace / "slides" / slide_id / "revisions" / str(number)
        rev_dir.mkdir(parents=True, exist_ok=True)
        rev_json = {
            "number": number, "instruction": instruction,
            "actions": actions, "status": status, "checksum": checksum,
            "created_at": created_at, "message": message,
            "dialogue": dialogue, "shapes_data": shapes_data or [],
            "preview_path": preview_path,
        }
        (rev_dir / "revision.json").write_text(
            json.dumps(rev_json, ensure_ascii=False, indent=2), encoding="utf-8")

    def load_revision(self, slide_id: str, number: int) -> dict[str, Any] | None:
        """Load revision metadata written by :meth:`save_revision`."""
        path = (
            self.workspace
            / "slides"
            / slide_id
            / "revisions"
            / str(number)
            / "revision.json"
        )
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def delete_revision(self, slide_id: str, number: int) -> None:
        rev_dir = self.workspace / "slides" / slide_id / "revisions" / str(number)
        if rev_dir.exists():
            shutil.rmtree(rev_dir, ignore_errors=True)
