"""Per-slide revision store — the heart of version management.

Implements the version strategy from direction D:

- Each **successful** edit forms a new ``revision``; a failed attempt is
  parked in the drafts dir and never promotes the current pointer.
- A page keeps the most recent 10 revisions; older ones are pruned.
- ``undo()`` just moves the current pointer back to the previous
  successful revision — **no model call**, so it is instant.
- ``apply_revision()`` switches the current pointer to any past
  successful revision, again without regenerating.

The store bridges the in-memory ``SlidePage`` (which the editor mutates
and the renderer previews) and the on-disk ``SlideWorkspace`` (which
holds one ``SlideArtifact`` + preview per revision). In-memory snapshots
make undo/apply instant; on-disk artifacts survive restarts.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from pptagent.editor.artifact import (
    SlideArtifact, SlideWorkspace,
    STATUS_READY, STATUS_FAILED,
)
from pptagent.editor.snapshot import SlideSnapshot
from pptagent.presentation.presentation import SlidePage


# ── revision info (lightweight, for listing) ──────────────────

class RevisionInfo:
    """A flat, JSON-friendly view of one revision for the API layer."""

    def __init__(self, artifact: SlideArtifact, is_current: bool):
        self.revision = artifact.revision
        self.slide_id = artifact.slide_id
        self.message = artifact.message
        self.edit_kind = artifact.edit_kind
        self.failed = artifact.failed
        self.created_at = artifact.created_at
        self.is_current = is_current
        self.preview_path = artifact.preview_path
        self.layout_name = artifact.layout_name

    def to_dict(self) -> dict[str, Any]:
        return {
            "revision": self.revision,
            "slide_id": self.slide_id,
            "message": self.message,
            "edit_kind": self.edit_kind,
            "failed": self.failed,
            "created_at": self.created_at,
            "is_current": self.is_current,
            "preview_path": self.preview_path,
            "layout_name": self.layout_name,
        }


# ── revision store ────────────────────────────────────────────

class RevisionStore:
    """Version control for a single slide, backed by a ``SlideWorkspace``.

    One store per ``(task_id, slide_id)``. The store owns the in-memory
    ``SlidePage`` it versions, so callers always edit through the store
    to keep revisions consistent.

    Usage::

        store = RevisionStore(workspace, slide_id, slide)
        store.create_revision(slide, message="初始生成")      # rev 1
        # ... edit slide via SlideEditor or regenerate ...
        store.create_revision(slide, message="精简标题")       # rev 2
        store.undo()                                          # back to rev 1
        store.apply_revision(2)                               # forward to rev 2
    """

    def __init__(
        self,
        workspace: SlideWorkspace,
        slide_id: str,
        slide: SlidePage,
        index: int = 0,
        mode: str = "template",
        layout_name: str | None = None,
        keep: int = 10,
    ):
        self.workspace = workspace
        self.slide_id = slide_id
        self.slide = slide
        self.index = index
        self.mode = mode
        self.layout_name = layout_name
        self.keep = keep
        # in-memory snapshot cache: revision -> SlideSnapshot (with shapes_copy)
        self._snapshots: dict[int, SlideSnapshot] = {}

    # ── properties ────────────────────────────────────────────

    @property
    def current_revision(self) -> int:
        return self.workspace.current_revision(self.slide_id)

    def get_current_artifact(self) -> SlideArtifact | None:
        return self.workspace.get_current(self.slide_id)

    # ── create ────────────────────────────────────────────────

    def create_revision(
        self,
        slide: SlidePage | None = None,
        message: str = "",
        edit_kind: str = "edit",
        success: bool = True,
        source_path: str | None = None,
        preview_path: str | None = None,
        structured_data: dict[str, Any] | None = None,
        layout_name: str | None = None,
    ) -> SlideArtifact:
        """Capture the slide's current state as a new revision.

        On ``success`` the new revision becomes current. On failure the
        draft is saved to ``drafts/`` and the current pointer is left
        untouched, so the previously-good version stays live.
        """
        target = slide if slide is not None else self.slide
        snap = SlideSnapshot.capture(target)

        # next revision number = max(existing, current) + 1
        revs = self.workspace.list_revisions(self.slide_id)
        next_rev = (max(revs) if revs else 0) + 1

        artifact = SlideArtifact(
            slide_id=self.slide_id,
            task_id=self.workspace.task_id,
            index=self.index,
            status=STATUS_READY if success else STATUS_FAILED,
            mode=self.mode,
            layout_name=layout_name or self.layout_name,
            structured_data=structured_data or snap.to_dict(),
            source_path=source_path,
            preview_path=preview_path,
            revision=next_rev,
            edit_kind=edit_kind,
            message=message or ("编辑" if success else "失败草稿"),
            failed=not success,
        )

        if success:
            # cache the in-memory snapshot for instant undo/apply
            self._snapshots[next_rev] = snap
            self.workspace.save_artifact(artifact)
            # promote current + prune
            self.workspace.set_current(self.slide_id, artifact)
            self.workspace.prune_revisions(self.slide_id, keep=self.keep)
            # also drop pruned snapshots from the in-memory cache
            live = set(self.workspace.list_revisions(self.slide_id))
            for r in list(self._snapshots):
                if r not in live:
                    self._snapshots.pop(r, None)
        else:
            # failed draft: persist under drafts/, never promote current
            self._save_draft(artifact, snap)
        return artifact

    def _save_draft(self, artifact: SlideArtifact, snap: SlideSnapshot) -> Path:
        """Persist a failed draft without promoting current."""
        d = self.workspace.drafts_dir(self.slide_id)
        path = d / f"draft_{artifact.revision}.json"
        payload = artifact.to_dict()
        payload["snapshot"] = snap.to_dict()
        with open(path, "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)
        return path

    # ── list / get ────────────────────────────────────────────

    def list_revisions(self) -> list[RevisionInfo]:
        """Return revision info, newest first."""
        cur = self.current_revision
        out: list[RevisionInfo] = []
        for rev in sorted(self.workspace.list_revisions(self.slide_id),
                          reverse=True):
            art = self.workspace.load_artifact(self.slide_id, rev)
            if art is None:
                continue
            out.append(RevisionInfo(art, is_current=(rev == cur)))
        return out

    def get_revision(self, revision: int) -> SlideArtifact | None:
        return self.workspace.load_artifact(self.slide_id, revision)

    def set_preview(self, revision: int, preview_path: str) -> None:
        """Attach a preview path to an existing revision's artifact.

        Called after the revision is created and the preview rendered,
        since the revision number (and thus the on-disk path) is only
        known once ``create_revision`` has assigned it.
        """
        art = self.workspace.load_artifact(self.slide_id, revision)
        if art is None:
            return
        art.preview_path = preview_path
        self.workspace.save_artifact(art)  # re-saves + re-sets current

    # ── apply / undo (no model call) ───────────────────────────

    def apply_revision(self, revision: int) -> SlideArtifact:
        """Switch the current pointer to ``revision`` and restore the slide.

        This is the primitive behind ``undo`` and the ``/revisions/{r}/apply``
        endpoint. It does **not** call the model.
        """
        art = self.workspace.load_artifact(self.slide_id, revision)
        if art is None:
            raise KeyError(
                f"Revision {revision} not found for slide {self.slide_id}")
        if art.failed:
            raise ValueError(
                f"Revision {revision} is a failed draft and cannot be applied")
        self._restore_into_live(art)
        self.workspace.set_current(self.slide_id, art)
        return art

    def undo(self) -> SlideArtifact | None:
        """Move the current pointer to the previous successful revision.

        Returns the now-current artifact, or ``None`` if already at the
        earliest revision. No model call.
        """
        revs = sorted(r for r in self.workspace.list_revisions(self.slide_id))
        cur = self.current_revision
        idx = revs.index(cur) if cur in revs else len(revs) - 1
        if idx <= 0:
            return None
        return self.apply_revision(revs[idx - 1])

    def redo(self) -> SlideArtifact | None:
        """Move the current pointer forward to the next revision.

        Symmetric counterpart to ``undo``. Also a pointer switch.
        """
        revs = sorted(r for r in self.workspace.list_revisions(self.slide_id))
        cur = self.current_revision
        idx = revs.index(cur) if cur in revs else -1
        if idx < 0 or idx >= len(revs) - 1:
            return None
        return self.apply_revision(revs[idx + 1])

    def can_undo(self) -> bool:
        revs = sorted(r for r in self.workspace.list_revisions(self.slide_id))
        cur = self.current_revision
        return cur in revs and revs.index(cur) > 0

    def can_redo(self) -> bool:
        revs = sorted(r for r in self.workspace.list_revisions(self.slide_id))
        cur = self.current_revision
        return cur in revs and revs.index(cur) < len(revs) - 1

    # ── internal: restore a revision into the live slide ─────

    def _restore_into_live(self, artifact: SlideArtifact) -> None:
        """Make the in-memory ``self.slide`` reflect ``artifact``'s state.

        Fast path: a cached ``SlideSnapshot`` (same session) deep-copies
        shapes back. Fallback (e.g. after a restart, when the cache is
        empty): best-effort in-place text restore from the revision's
        ``structured_data`` — matching shapes by ``shape_idx`` and
        rewriting paragraph text. Geometry/images may differ, but the
        text content aligns with the target revision so preview/export
        stay consistent with ``current.json``.
        """
        snap = self._snapshots.get(artifact.revision)
        if snap is not None:
            snap.restore(self.slide)
            return
        data = artifact.structured_data or {}
        shapes_data = (data.get("shapes_data", [])
                       if isinstance(data, dict) else [])
        by_idx: dict[Any, dict] = {
            s.get("shape_idx"): s
            for s in shapes_data
            if s.get("shape_idx") is not None
        }
        for shape in getattr(self.slide, "shapes", []):
            sid = getattr(shape, "shape_idx", None)
            stored = by_idx.get(sid)
            if not stored or not hasattr(shape, "text_frame"):
                continue
            if not shape.text_frame.is_textframe:
                continue
            stored_paras = {
                p.get("idx"): p.get("text", "")
                for p in stored.get("paragraphs", [])
                if p.get("idx") is not None
            }
            for p in shape.text_frame.paragraphs:
                pid = getattr(p, "idx", -1)
                if pid in stored_paras:
                    try:
                        p.text = stored_paras[pid]
                    except Exception:
                        pass  # best-effort; skip unwritable paragraphs
        if isinstance(data, dict):
            if data.get("layout_name"):
                self.slide.slide_layout_name = data["layout_name"]
            if data.get("slide_title"):
                self.slide.slide_title = data["slide_title"]
