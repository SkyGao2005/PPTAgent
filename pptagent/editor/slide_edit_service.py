"""SlideEditService — conversational local editing + version management.

This is the direction-D orchestrator. It binds together everything the
PDF section "方向 D" asks for:

- **对话式局部编辑**: ``chat(slide_id, instruction)`` turns a natural-
  language instruction into an ``EditPlan``, applies it to one page
  only, and produces a new preview + revision.
- **版本管理**: each successful edit creates a revision; ``undo`` /
  ``apply_revision`` switch the current pointer without a model call;
  the last 10 revisions are kept; failed drafts never promote current;
  export reads each page's latest successful revision.

It also publishes the ``edit.*`` events (started / preview_ready /
applied / failed / reverted) through the per-task ``EventBus``, and
serializes page edits with an ``asyncio.Lock`` so the same page is
never edited twice in parallel (different pages may edit concurrently).
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pptagent.editor.artifact import (
    SlideArtifact, SlideWorkspace, MODE_TEMPLATE, MODE_HTML,
    STATUS_READY,
)
from pptagent.editor.conversation import ConversationLog
from pptagent.editor.edit_context import build_edit_context, EditContext
from pptagent.editor.edit_executor import EditPlanExecutor, EditResult
from pptagent.editor.edit_plan import EditPlanner, EditPlan, DeterministicPlanner
from pptagent.editor.events import (
    EventBus, GenerationEvent, global_registry, new_task_id,
)
from pptagent.editor.regen_backend import EditBackend, NoopRegenBackend, RegenUnavailableError
from pptagent.editor.revision_store import RevisionStore, RevisionInfo
from pptagent.editor.snapshot import SlideSnapshot
from pptagent.editor.preview import PreviewRenderer
from pptagent.presentation.presentation import Presentation, SlidePage


# ── response shapes (mirror the PDF's REST endpoints) ────────

@dataclass
class ChatResponse:
    """Result of one ``chat`` call (POST .../slides/{id}/chat)."""

    slide_id: str
    success: bool
    changed: bool
    revision: int | None = None
    summary: str = ""
    plan: dict[str, Any] | None = None
    op_results: list[dict[str, Any]] = field(default_factory=list)
    preview_path: str | None = None
    error: str | None = None


# ── pending edit (for not-yet-complete pages) ─────────────────

@dataclass
class _PendingEdit:
    instruction: str
    element_id: str | None = None


# ── per-slide state ────────────────────────────────────────────

@dataclass
class _SlideState:
    slide_id: str
    slide: SlidePage
    index: int
    mode: str
    layout_name: str | None
    revision_store: RevisionStore
    conversation: ConversationLog
    ready: bool = False
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    pending: list[_PendingEdit] = field(default_factory=list)
    last_failed: _PendingEdit | None = None
    last_context: EditContext | None = None


# ── service ───────────────────────────────────────────────────

class SlideEditService:
    """Per-task conversational edit + version service.

    Construct one per task (one ``Presentation``). The PDF's REST verbs
    map onto the methods:

    ================================= ==============================================
    POST   /slides/{id}/chat            ``chat``
    GET    /slides/{id}/revisions       ``list_revisions``
    POST   /slides/{id}/revisions/{r}/apply ``apply_revision``
    POST   /slides/{id}/undo            ``undo``
    POST   /slides/{id}/retry           ``retry``
    ================================= ==============================================
    """

    def __init__(
        self,
        presentation: Presentation,
        task_id: str | None = None,
        workspace_root: str | Path | None = None,
        *,
        planner: EditPlanner | None = None,
        regen_backend: EditBackend | None = None,
        executor: EditPlanExecutor | None = None,
        editor_factory: Callable[[SlidePage], Any] | None = None,
        event_bus: EventBus | None = None,
        outline: dict[str, Any] | None = None,
        preview_renderer: PreviewRenderer | None = None,
        screenshot_provider: Callable[[Any], Any] | None = None,
        element_schema: dict[str, Any] | None = None,
        keep_revisions: int = 10,
    ):
        self.presentation = presentation
        self.task_id = task_id or new_task_id()
        # always back the service with a workspace on disk so revisions
        # survive; default to a temp dir when none is given
        if workspace_root is None:
            import tempfile
            workspace_root = tempfile.mkdtemp(prefix="pptagent_ws_")
        self.workspace = SlideWorkspace(self.task_id, workspace_root)
        self.outline = outline or {}
        self.screenshot_provider = screenshot_provider
        self.element_schema = element_schema
        self.keep_revisions = keep_revisions

        # event bus — share one instance with the global registry so
        # external subscribers (other services/SSE) see the same events
        self.bus = event_bus or global_registry().get_or_create(
            self.task_id, self.workspace.root)

        # planner: default to the deterministic, model-free planner so the
        # service is fully runnable without an API key. Swap in
        # LLMEditPlanner for real conversational editing.
        self.planner = planner or DeterministicPlanner()

        # regen backend: default to noop (regen ops become failed drafts)
        self._regen = regen_backend or NoopRegenBackend()

        # editor factory: builds a checkpointed SlideEditor for a slide
        self._editor_factory = editor_factory or self._default_editor_factory

        # preview renderer (for context HTML fallback + per-revision preview)
        self._preview = preview_renderer or PreviewRenderer()

        # executor
        self.executor = executor or EditPlanExecutor(
            editor_factory=self._editor_factory,
            regen_backend=self._regen,
            style_registry=self._load_style_registry(),
        )

        # per-slide state
        self._slides: dict[str, _SlideState] = {}
        self._id_by_index: dict[int, str] = {}

        # persist task meta + outline
        self.workspace.init_meta(
            title=self.outline.get("title", ""),
            total_slides=len(presentation.slides),
            outline=self.outline)
        # (self.bus is already registered with the global registry above)

    # ── registration ─────────────────────────────────────────

    def _default_editor_factory(self, slide: SlidePage):
        from pptagent.editor.editor import SlideEditor
        return SlideEditor(slide, self.presentation)

    @staticmethod
    def _load_style_registry():
        try:
            from pptagent.editor.styles import StyleRegistry
            return StyleRegistry()
        except Exception:
            return None

    def register_slide(
        self,
        slide: SlidePage,
        slide_id: str | None = None,
        index: int | None = None,
        mode: str = MODE_TEMPLATE,
        layout_name: str | None = None,
        ready: bool = True,
        message: str = "初始生成",
    ) -> str:
        """Register a generated slide and create its first revision.

        ``ready=True`` means the page is fully generated and editable
        right away; ``ready=False`` (still generating) queues incoming
        edits until ``mark_ready`` is called.
        """
        slide_id = slide_id or getattr(slide, "_edit_slide_id", None) or _new_sid()
        # stable id attached to the slide object so re-registration is idempotent
        try:
            slide._edit_slide_id = slide_id  # type: ignore[attr-defined]
        except Exception:
            pass
        idx = index if index is not None else (slide.slide_idx - 1)
        layout_name = layout_name or getattr(slide, "slide_layout_name", None)

        rs = RevisionStore(
            self.workspace, slide_id, slide, index=idx,
            mode=mode, layout_name=layout_name, keep=self.keep_revisions)

        convo = ConversationLog(slide_id)
        state = _SlideState(
            slide_id=slide_id, slide=slide, index=idx, mode=mode,
            layout_name=layout_name, revision_store=rs,
            conversation=convo, ready=ready,
        )
        self._slides[slide_id] = state
        self._id_by_index[idx] = slide_id

        # initial revision (rev 1) = the generated page
        rs.create_revision(slide, message=message, edit_kind="generate",
                           success=True, layout_name=layout_name,
                           structured_data=self._capture_structured(slide))
        if ready:
            self._flush_pending(slide_id)
        return slide_id

    def mark_ready(self, slide_id: str) -> None:
        """Mark a slide as fully generated; run any queued edits."""
        state = self._get(slide_id)
        state.ready = True
        self._flush_pending(slide_id)

    def _capture_structured(self, slide: SlidePage) -> dict[str, Any]:
        return SlideSnapshot.capture(slide).to_dict()

    # ── lookups ────────────────────────────────────────────────

    def _get(self, slide_id: str) -> _SlideState:
        if slide_id not in self._slides:
            raise KeyError(f"未知 slide_id: {slide_id}")
        return self._slides[slide_id]

    def slide_ids(self) -> list[str]:
        """Return slide_ids in page order."""
        return [self._id_by_index[i] for i in sorted(self._id_by_index)]

    def get_slide(self, slide_id: str) -> SlidePage:
        return self._get(slide_id).slide

    # ── the conversational edit (POST .../chat) ───────────────

    async def chat(
        self,
        slide_id: str,
        instruction: str,
        element_id: str | None = None,
    ) -> ChatResponse:
        """Apply a natural-language instruction to one page.

        If the page isn't ready yet, the instruction is queued and
        applied automatically when the page completes (direction D).
        """
        state = self._get(slide_id)
        if not state.ready:
            state.pending.append(_PendingEdit(instruction, element_id))
            return ChatResponse(
                slide_id=slide_id, success=True, changed=False,
                summary="页面尚未生成完成，已排队待执行",
                error=None)

        async with state.lock:
            return await self._do_edit(state, instruction, element_id)

    async def _do_edit(self, state: _SlideState, instruction: str,
                       element_id: str | None) -> ChatResponse:
        slide = state.slide
        slide_id = state.slide_id
        total = len(self.presentation.slides)

        # 1. assemble context
        context = build_edit_context(
            slide, instruction, state.conversation,
            outline=self.outline,
            element_schema=self.element_schema,
            selected_element=element_id,
            screenshot_provider=self.screenshot_provider,
            preview_renderer=self._preview,
            total_slides=total,
        )
        state.last_context = context

        # 2. edit.started
        self._emit(GenerationEvent.edit_started(
            self.task_id, self.bus.last_seq + 1, slide_id,
            instruction, slide_index=slide.slide_idx, total_slides=total,
            payload={"element_id": element_id}))

        # 3. plan
        plan: EditPlan = await self.planner.plan(context)
        if not plan.ops:
            state.conversation.add_user(instruction, element_id)
            state.conversation.add_assistant("无可执行操作", success=True)
            return ChatResponse(slide_id=state.slide_id, success=True,
                                changed=False, summary=plan.rationale,
                                plan=plan.to_dict())

        # 4. execute
        result: EditResult = await self.executor.apply(
            plan, slide, instruction=instruction, context=context)

        # record the user turn regardless of outcome
        state.conversation.add_user(instruction, element_id)

        if not result.success:
            return self._handle_failed(state, instruction, plan, result, context)

        # 5. success: install regen result if any, then create revision
        if result.new_slide is not None:
            self._install_new_slide(state, result.new_slide)
            slide = state.slide
        if result.html is not None:
            self._install_html(state, result.html)

        changed = result.changed or (result.new_slide is not None)
        if not changed:
            # no-op success: don't create a revision
            state.conversation.add_assistant(result.summary or "无变化",
                                              success=True)
            return ChatResponse(slide_id=state.slide_id, success=True,
                                changed=False, summary=result.summary or "无变化",
                                plan=plan.to_dict(),
                                op_results=[r.__dict__ for r in result.op_results])

        # 6. create the successful revision, then render its preview
        artifact = state.revision_store.create_revision(
            slide, message=instruction, edit_kind="edit",
            success=True,
            structured_data=self._capture_structured(slide),
            layout_name=state.layout_name)
        preview_path = self._save_preview(state, slide, artifact.revision)
        if preview_path:
            state.revision_store.set_preview(artifact.revision, preview_path)
        # also store the source HTML alongside the revision
        self._save_source_html(state, slide, artifact.revision)

        # 7. events: preview_ready + applied
        self._emit(GenerationEvent.edit_preview_ready(
            self.task_id, self.bus.last_seq + 1, state.slide_id,
            preview_path or "", artifact.revision,
            slide_index=slide.slide_idx, total_slides=total))
        self._emit(GenerationEvent.edit_applied(
            self.task_id, self.bus.last_seq + 1, state.slide_id,
            artifact.revision, slide_index=slide.slide_idx,
            total_slides=total, message=result.summary))

        state.conversation.add_assistant(
            result.summary or "已修改", revision=artifact.revision, success=True)
        state.last_failed = None

        return ChatResponse(
            slide_id=state.slide_id, success=True, changed=True,
            revision=artifact.revision, summary=result.summary or "已修改",
            plan=plan.to_dict(),
            op_results=[r.__dict__ for r in result.op_results],
            preview_path=preview_path)

    # ── failure handling (failed draft, current untouched) ───

    def _handle_failed(self, state: _SlideState, instruction: str,
                       plan: EditPlan, result: EditResult,
                       context: EditContext) -> ChatResponse:
        # park a failed draft WITHOUT promoting current
        try:
            state.revision_store.create_revision(
                state.slide, message=instruction, edit_kind="edit",
                success=False, structured_data=self._capture_structured(state.slide),
                layout_name=state.layout_name)
        except Exception:
            pass  # a draft failure must never break the conversation
        state.last_failed = _PendingEdit(instruction)
        reason = result.error or "未知错误"
        self._emit(GenerationEvent.edit_failed(
            self.task_id, self.bus.last_seq + 1, state.slide_id,
            reason, slide_index=state.slide.slide_idx))
        state.conversation.add_assistant(f"修改失败: {reason}", success=False)
        return ChatResponse(
            slide_id=state.slide_id, success=False, changed=False,
            summary=f"修改失败: {reason}", plan=plan.to_dict(),
            op_results=[r.__dict__ for r in result.op_results], error=reason)

    # ── undo / apply / redo (pointer switches, no model) ─────

    async def undo(self, slide_id: str) -> ChatResponse:
        """Revert to the previous successful revision. No model call."""
        state = self._get(slide_id)
        async with state.lock:
            prev = state.revision_store.undo()
            if prev is None:
                return ChatResponse(slide_id=slide_id, success=False,
                                    changed=False, summary="已是最早版本，无法撤销",
                                    error="at_earliest")
            self._emit(GenerationEvent.edit_reverted(
                self.task_id, self.bus.last_seq + 1, slide_id,
                prev.revision, slide_index=state.slide.slide_idx))
            state.conversation.add_assistant(
                f"撤销到 rev{prev.revision}", revision=prev.revision, success=True)
            return ChatResponse(slide_id=slide_id, success=True, changed=True,
                                revision=prev.revision,
                                summary=f"撤销到 rev{prev.revision}",
                                preview_path=prev.preview_path)

    async def redo(self, slide_id: str) -> ChatResponse:
        state = self._get(slide_id)
        async with state.lock:
            nxt = state.revision_store.redo()
            if nxt is None:
                return ChatResponse(slide_id=slide_id, success=False,
                                    changed=False, summary="已是最新版本")
            self._emit(GenerationEvent.edit_reverted(
                self.task_id, self.bus.last_seq + 1, slide_id,
                nxt.revision, slide_index=state.slide.slide_idx,
                message="redo"))
            return ChatResponse(slide_id=slide_id, success=True, changed=True,
                                revision=nxt.revision, summary=f"重做到 rev{nxt.revision}",
                                preview_path=nxt.preview_path)

    async def apply_revision(self, slide_id: str, revision: int) -> ChatResponse:
        """Switch the current pointer to ``revision``. No model call."""
        state = self._get(slide_id)
        async with state.lock:
            art = state.revision_store.apply_revision(revision)
            self._emit(GenerationEvent.edit_reverted(
                self.task_id, self.bus.last_seq + 1, slide_id,
                revision, slide_index=state.slide.slide_idx,
                message=f"apply rev{revision}"))
            return ChatResponse(slide_id=slide_id, success=True, changed=True,
                                revision=revision,
                                summary=f"切换到 rev{revision}",
                                preview_path=art.preview_path)

    # ── retry (POST .../retry) ─────────────────────────────────

    async def retry(self, slide_id: str) -> ChatResponse:
        """Re-run the last failed instruction (e.g. a transient model error)."""
        state = self._get(slide_id)
        async with state.lock:
            if state.last_failed is None:
                return ChatResponse(slide_id=slide_id, success=False,
                                    changed=False, summary="没有可重试的失败操作",
                                    error="nothing_to_retry")
            pending = state.last_failed
            state.last_failed = None
            return await self._do_edit(state, pending.instruction, pending.element_id)

    # ── revisions listing (GET .../revisions) ─────────────────

    def list_revisions(self, slide_id: str) -> list[dict[str, Any]]:
        return [r.to_dict() for r in self._get(slide_id).revision_store.list_revisions()]

    def current_revision(self, slide_id: str) -> int:
        return self._get(slide_id).revision_store.current_revision

    # ── pending queue ─────────────────────────────────────────

    def _flush_pending(self, slide_id: str) -> None:
        """Run queued edits for a now-ready slide (fire-and-forget).

        Called from ``register_slide``/``mark_ready``. If we're inside a
        running loop (the normal case — generation pipeline), queued
        edits are scheduled there; otherwise they stay queued and run on
        the next ``mark_ready``/``chat`` from an async context.
        """
        state = self._get(slide_id)
        if not state.pending:
            return
        pending = list(state.pending)
        state.pending.clear()
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            # no running loop — re-queue and wait for an async caller
            state.pending[:0] = pending
            return
        for p in pending:
            loop.create_task(self.chat(slide_id, p.instruction, p.element_id))

    # ── export (reads latest successful revision per page) ────

    async def export(self, output_path: str | Path,
                     options: Any = None) -> dict[str, Any]:
        """Export the deck using each page's latest successful revision.

        Records the per-slide revision in an export manifest so the
        exact version is reproducible.
        """
        from pptagent.editor.exporter import optimized_save, ExportOptions
        opts = options if isinstance(options, ExportOptions) else ExportOptions()

        self._emit(GenerationEvent(
            task_id=self.task_id, seq=self.bus.last_seq + 1,
            type="export.started", stage="export", status="running",
            message="开始导出"))

        revisions: dict[str, int] = {}
        for sid in self.slide_ids():
            revisions[sid] = self.current_revision(sid)

        result = optimized_save(self.presentation, str(output_path), opts)

        if self.workspace is not None:
            self.workspace.save_export_manifest(revisions, str(output_path))

        evt = GenerationEvent(
            task_id=self.task_id, seq=self.bus.last_seq + 1,
            type="export.completed" if result.success else "export.failed",
            stage="export",
            status="succeeded" if result.success else "failed",
            artifact_url=str(output_path),
            message=result.summary())
        self._emit(evt)
        return {
            "success": result.success,
            "output_path": str(output_path),
            "revisions": revisions,
            "summary": result.summary(),
            "warnings": len(result.warnings),
        }

    # ── helpers: install regen result, previews, events ──────

    def _install_new_slide(self, state: _SlideState, new_slide: SlidePage) -> None:
        """Swap a regenerated slide into the presentation + state.

        The live ``SlidePage`` object is replaced in the presentation
        list, and the revision store's slide reference is updated, so
        subsequent edits operate on the new page.
        """
        try:
            idx = state.index
            # copy the new slide's shapes into the existing object so the
            # reference stays stable for the revision store + editor.
            state.slide.shapes = list(new_slide.shapes)
            if getattr(new_slide, "slide_layout_name", None):
                state.slide.slide_layout_name = new_slide.slide_layout_name
            if getattr(new_slide, "slide_title", None):
                state.slide.slide_title = new_slide.slide_title
        except Exception:
            # fall back to replacing the slot entirely
            if 0 <= state.index < len(self.presentation.slides):
                self.presentation.slides[state.index] = new_slide
                state.slide = new_slide
                state.revision_store.slide = new_slide

    def _install_html(self, state: _SlideState, html: str) -> None:
        if hasattr(self._regen, "set_page_html"):
            self._regen.set_page_html(state.slide_id, html)

    def _save_preview(self, state: _SlideState, slide: SlidePage,
                      revision: int) -> str | None:
        """Save a self-contained HTML preview for the given revision."""
        try:
            total = len(self.presentation.slides)
            path = self.workspace.preview_path_for(
                state.slide_id, revision, ext=".html")
            self._preview.save_preview(
                slide, path, slide_idx=slide.slide_idx, total_slides=total)
            return str(path)
        except Exception:
            return None

    def _save_source_html(self, state: _SlideState, slide: SlidePage,
                          revision: int) -> None:
        try:
            path = self.workspace.source_path_for(
                state.slide_id, revision, ext=".html")
            self._preview.save_preview(
                slide, path, slide_idx=slide.slide_idx,
                total_slides=len(self.presentation.slides))
        except Exception:
            pass

    def _emit(self, event: GenerationEvent) -> None:
        self.bus.publish(event)


# ── helpers ───────────────────────────────────────────────────

def _new_sid() -> str:
    import uuid
    return uuid.uuid4().hex
