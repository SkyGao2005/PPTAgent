"""Integration layer: connect editor/version modules to the PPTAgent generation pipeline.

Provides ``VersionedPPTAgent`` — a thin wrapper around ``PPTAgent`` that
automatically creates versions after each generation and exposes per-slide
editors for post-generation editing.
"""

from pathlib import Path

from pptagent.document import Document
from pptagent.pptgen import PPTAgent
from pptagent.presentation import Presentation
from pptagent.editor.artifact import ArtifactStore
from pptagent.editor.editor import SlideEditor
from pptagent.editor.service import (
    DEFAULT_REGISTRY,
    SlideEditService,
)
from pptagent.editor.version import VersionManager
from pptagent.editor.version_store import VersionStore


class VersionedPPTAgent(PPTAgent):
    """PPTAgent subclass with automatic version management.

    Usage::

        agent = VersionedPPTAgent(
            language_model=...,
            vision_model=...,
            workspace="/path/to/workspace",
        )
        agent.set_reference(slide_induction, presentation)
        prs, history = await agent.generate_pres(document)

        # Post-generation editing
        editor = agent.get_editor(2)      # edit slide 2
        editor.edit_text(1, 0, "New title")
        editor.undo()

        # Version management
        agent.commit("Manual edits on slide 2")
        agent.version_manager.log()
        agent.save_versions()             # persist to disk
    """

    def __init__(
        self,
        *args,
        workspace: str | Path | None = None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self._workspace = Path(workspace) if workspace else Path(".")
        self.version_manager = VersionManager()
        self._editors: dict[int, SlideEditor] = {}
        self._store: VersionStore | None = None

    # ── generate with auto-commit ─────────────────────────────

    async def generate_pres(self, *args, **kwargs):
        """Generate presentation and auto-commit the result as a version."""
        prs, history = await super().generate_pres(*args, **kwargs)
        if prs is not None and len(prs.slides) > 0:
            tag = kwargs.get("version_tag", None)
            self.version_manager.commit(prs, "自动生成", tags=[tag] if tag else ["auto"])
            self._editors.clear()  # reset editors for new generation
        return prs, history

    # ── post-generation editing ───────────────────────────────

    def get_editor(self, slide_idx: int) -> SlideEditor:
        """Get a SlideEditor for a specific slide (1-based index)."""
        if not hasattr(self, "empty_prs") or slide_idx > len(self.empty_prs.slides):
            raise IndexError(f"Slide {slide_idx} out of range")
        if slide_idx not in self._editors:
            self._editors[slide_idx] = SlideEditor(
                self.empty_prs.slides[slide_idx - 1],
                self.empty_prs,
            )
        return self._editors[slide_idx]

    def list_editors(self) -> dict[int, SlideEditor]:
        """Return all active editors keyed by slide index."""
        return dict(self._editors)

    # ── direction D: conversational editing & versioning ────────

    def get_edit_service(
        self,
        slide_idx: int,
        task_id: str | None = None,
        llm=None,
        outline: str | None = None,
        regenerate_fn=None,
        workspace=None,
        max_revisions: int = 10,
    ) -> "SlideEditService":
        """Return (creating & registering if needed) a conversational edit service.

        The service targets one slide (1-based ``slide_idx``) and is registered
        in the shared :data:`pptagent.editor.service.DEFAULT_REGISTRY` so the
        FastAPI routes can resolve it by ``(task_id, slide_id)``.
        """
        if not hasattr(self, "empty_prs") or slide_idx > len(self.empty_prs.slides):
            raise IndexError(f"Slide {slide_idx} out of range")
        slide = self.empty_prs.slides[slide_idx - 1]
        slide_id = f"s{slide_idx}"
        existing = DEFAULT_REGISTRY.get(task_id, slide_id)
        if existing is not None:
            return existing

        store = None
        ws = workspace or self._workspace
        if ws is not None:
            store = ArtifactStore(ws, task_id)

        svc = SlideEditService(
            slide=slide,
            presentation=self.empty_prs,
            doc=getattr(self, "document", None),
            llm=llm or getattr(self, "language_model", None),
            task_id=task_id,
            slide_id=slide_id,
            mode="template",
            outline=outline,
            store=store,
            regenerate_fn=regenerate_fn,
            max_revisions=max_revisions,
        )
        DEFAULT_REGISTRY.register(task_id, slide_id, svc)
        return svc

    # ── version commit ────────────────────────────────────────

    def commit(self, message: str, tags: list[str] | None = None):
        """Manually commit the current presentation state as a new version."""
        if not hasattr(self, "empty_prs"):
            raise RuntimeError("No presentation generated yet")
        return self.version_manager.commit(self.empty_prs, message, tags)

    # ── persistence ───────────────────────────────────────────

    def enable_persistence(self, storage_dir: str | Path | None = None):
        """Enable disk persistence for version history."""
        d = storage_dir or self._workspace / ".versions"
        self._store = VersionStore(d)

    def save_versions(self):
        """Save all versions to disk (requires ``enable_persistence()`` first)."""
        if self._store is None:
            raise RuntimeError("Call enable_persistence() first")
        for vid in self.version_manager._versions:
            self._store.save(self.version_manager._versions[vid])

    def load_versions(self) -> "VersionManager":
        """Load versions from disk (requires ``enable_persistence()`` first)."""
        if self._store is None:
            raise RuntimeError("Call enable_persistence() first")
        self.version_manager = self._store.load_manager()
        return self.version_manager
