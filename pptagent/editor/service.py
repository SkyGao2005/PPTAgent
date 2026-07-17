"""Conversational local editing & version-management service (D).

:class:`SlideEditService` is the heart of the D feature.  For a single slide it:

* turns a natural-language instruction into concrete edits via an LLM
  (content edits → low-level edit APIs; style edits → restyle; page-level
  regeneration → an optional ``regenerate_fn`` hook or a content fallback);
* records every successful edit as a :class:`~pptagent.editor.revision.SlideRevision`
  and keeps the most recent ``max_revisions`` (default 10);
* supports undo / version-switch purely by replaying stored actions from the
  original baseline — no extra model call;
* serialises edits with a page-level :class:`asyncio.Lock` so concurrent
  requests for the same page are serialised while different pages run in parallel;
* publishes :class:`~pptagent.editor.event_bus.EditEvent` for the front-end.

Two special action directives are stored inside a revision so it can be
replayed exactly:

* ``RESTYLE::<json>``   – apply a colour / font style strategy.
* ``REGEN::<text>``     – regenerate the page (custom hook or fallback).
"""

from __future__ import annotations

import asyncio
import inspect
import json
import tempfile
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from pptagent.apis import CodeExecutor, SlideEditError
from pptagent.document import Document
from pptagent.presentation import Presentation, SlidePage

from pptagent.editor.artifact import (
    ArtifactStore,
    SlideArtifact,
    extract_structured_data,
)
from pptagent.editor.editor import SlideEditor
from pptagent.editor.event_bus import EditEventBus, get_default_bus
from pptagent.editor.preview import PreviewRenderer
from pptagent.editor.prompts import (
    SYSTEM_PROMPT,
    EditActionPlan,
    build_user_prompt,
)
from pptagent.editor.revision import RevisionManager
from pptagent.editor.snapshot import SlideSnapshot
from pptagent.editor.styles import ColorPalette, FontPrefs, StyleRegistry, StyleStrategy


# ── result type ────────────────────────────────────────────────


@dataclass
class EditResult:
    """Outcome of an edit / undo / apply / retry operation."""

    status: str  # success | failed | no_change | noop
    slide_id: str
    revision: int | None
    message: str
    preview_path: str | None = None
    checksum: str | None = None
    diff: str | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


# ── service ────────────────────────────────────────────────────


class SlideEditService:
    """Conversational, versioned editor for one slide."""

    def __init__(
        self,
        slide: SlidePage,
        presentation: Presentation,
        doc: Document | None = None,
        llm: Any | None = None,
        task_id: str | None = None,
        slide_id: str | None = None,
        mode: str = "template",
        layout_name: str | None = None,
        source_path: str | None = None,
        outline: str | None = None,
        event_bus: EditEventBus | None = None,
        store: ArtifactStore | None = None,
        regenerate_fn: Callable | None = None,
        max_revisions: int = 10,
    ):
        self.slide = slide
        self.prs = presentation
        self.doc = doc
        self.llm = llm
        self.task_id = task_id
        self.slide_id = slide_id or f"s{slide.slide_idx}_{uuid.uuid4().hex[:8]}"
        self.mode = mode
        self.layout_name = layout_name or slide.slide_layout_name
        self.source_path = source_path
        self.outline = outline
        self.event_bus = event_bus or get_default_bus()
        self.store = store
        self.regenerate_fn = regenerate_fn
        self.max_revisions = max_revisions

        self._total = len(presentation.slides)
        self.baseline = SlideSnapshot.capture(slide)
        self.revisions = RevisionManager(self.baseline, max_revisions=max_revisions)
        self.dialogue: list[dict[str, Any]] = []
        self.lock = asyncio.Lock()
        self._preview_renderer = PreviewRenderer()
        self.artifact = SlideArtifact(
            slide_id=self.slide_id,
            index=slide.slide_idx,
            mode=mode,
            task_id=task_id,
            status="ready",
            layout_name=self.layout_name,
            structured_data=extract_structured_data(slide),
            source_path=source_path,
            revision=0,
        )
        self._last_instruction: str | None = None
        self._last_element_id: int | None = None

    # ── public API ─────────────────────────────────────────────

    async def chat(self, instruction: str, element_id: int | None = None) -> EditResult:
        """Send a natural-language edit instruction for this page."""
        self.dialogue.append({"role": "user", "content": instruction})
        result = await self._execute(instruction, element_id)
        self.dialogue.append({"role": "assistant", "content": result.message or ""})
        return result

    async def undo(self) -> EditResult:
        """Undo the last revision (pointer move + replay, no model call)."""
        async with self.lock:
            prev = self.revisions.previous_number()
            if prev is None:
                return EditResult("noop", self.slide_id, None, "已是最早版本，无法继续撤销")
            await self._restore_to(prev)
            self.revisions.set_current(prev)
            self.artifact.revision = prev
            self.artifact.structured_data = extract_structured_data(self.slide)
            preview = self._render_and_persist_preview(prev)
            self.event_bus.publish(
                "edit.reverted", self.slide_id, self.task_id,
                status="succeeded", revision=prev, preview_url=preview,
                message=f"已撤销到版本 {prev}",
            )
            self._persist()
            return EditResult(
                "success", self.slide_id, prev, f"已撤销到版本 {prev}",
                preview, checksum=self.revisions.current_checksum,
            )

    async def apply_revision(self, number: int) -> EditResult:
        """Switch the page to a specific historical revision."""
        async with self.lock:
            rev = self.revisions.get(number)
            if rev is None:
                return EditResult("failed", self.slide_id, None, f"版本 {number} 不存在")
            await self._restore_to(number)
            self.revisions.set_current(number)
            self.artifact.revision = number
            self.artifact.structured_data = extract_structured_data(self.slide)
            preview = self._render_and_persist_preview(number)
            self.event_bus.publish(
                "edit.applied", self.slide_id, self.task_id,
                status="succeeded", revision=number, preview_url=preview,
                message=f"已切换到版本 {number}",
            )
            self._persist()
            return EditResult(
                "success", self.slide_id, number, f"已切换到版本 {number}",
                preview, checksum=rev.checksum,
            )

    async def retry(self) -> EditResult:
        """Retry the last failed modification."""
        async with self.lock:
            if self._last_instruction is None:
                return EditResult("failed", self.slide_id, None, "没有可重试的失败指令")
            return await self._execute(self._last_instruction, self._last_element_id)

    def get_revisions(self) -> dict[str, Any]:
        """Return the revision list and the pruned (dropped) revision log."""
        return {
            "slide_id": self.slide_id,
            "current": self.revisions.current_number,
            "revisions": self.revisions.list(),
            "pruned": self.revisions.pruned_log(),
        }

    # ── core execution ─────────────────────────────────────────

    async def _execute(self, instruction: str, element_id: int | None) -> EditResult:
        before = SlideSnapshot.capture(self.slide)
        self.event_bus.publish(
            "edit.started", self.slide_id, self.task_id,
            status="running", message=instruction,
        )

        # 1) ask the model for a structured plan
        try:
            plan = await self._call_llm(
                build_user_prompt(
                    self.slide, instruction, self.outline, element_id,
                    self.dialogue, self.revisions.current_number,
                ),
                self.dialogue,
            )
        except Exception as e:  # model failure → keep previous version
            self._last_instruction = instruction
            self._last_element_id = element_id
            self.event_bus.publish(
                "edit.failed", self.slide_id, self.task_id,
                status="failed", message=f"模型调用失败: {e}",
            )
            return EditResult("failed", self.slide_id, None, f"模型调用失败: {e}", error=str(e))

        if isinstance(plan, EditActionPlan):
            plan = plan.model_dump()
        ptype = (plan.get("type") or "none").lower()
        assistant_msg = plan.get("message") or ""

        if ptype == "none":
            self.event_bus.publish(
                "edit.failed", self.slide_id, self.task_id,
                status="failed", message=assistant_msg or "暂无法执行该指令",
            )
            return EditResult("failed", self.slide_id, None, assistant_msg or "暂无法执行该指令")

        # 2) translate the plan into actions and run them
        actions: list[str] = []
        if ptype == "content":
            actions = [a for a in plan.get("actions", []) if str(a).strip()]
            ok, err = self._run_api_actions(actions)
        elif ptype == "style":
            actions = ["RESTYLE::" + json.dumps(plan.get("style", {}), ensure_ascii=False)]
            ok, err = self._run_style(actions[0])
        elif ptype == "regenerate":
            regen_text = plan.get("message") or instruction
            actions = ["REGEN::" + regen_text]
            ok, err = await self._run_regen(regen_text)
        else:
            ok, err = False, f"未知编辑类型: {ptype}"

        # 3) failure → keep previous version, allow retry
        if not ok:
            self._last_instruction = instruction
            self._last_element_id = element_id
            self.revisions.add(instruction, actions, "draft", before.checksum,
                               self.dialogue, message=err)
            self.event_bus.publish(
                "edit.failed", self.slide_id, self.task_id,
                status="failed", message=err,
            )
            return EditResult("failed", self.slide_id, None, err, error=err)

        # 4) success → record revision, render preview, publish events
        after = SlideSnapshot.capture(self.slide)
        # Style / regenerate edits change fonts/colours/layout but not the
        # (text-only) checksum, so they must always be recorded as revisions.
        # Content edits that leave the text byte-identical are true no-ops.
        if ptype == "content" and after.checksum == before.checksum:
            self.event_bus.publish(
                "edit.applied", self.slide_id, self.task_id,
                status="succeeded", revision=self.revisions.current_number,
                message=assistant_msg or "指令已执行，内容未变化",
            )
            return EditResult(
                "no_change", self.slide_id, self.revisions.current_number,
                assistant_msg or "内容未发生变化",
            )

        rev = self.revisions.add(
            instruction, actions, "success", after.checksum,
            self.dialogue, message=assistant_msg or "",
        )
        preview = self._render_and_persist_preview(rev.number)
        rev.preview_path = preview
        self.artifact.revision = rev.number
        self.artifact.status = "ready"
        self.artifact.structured_data = extract_structured_data(self.slide)

        self.event_bus.publish(
            "edit.preview_ready", self.slide_id, self.task_id,
            status="succeeded", revision=rev.number, preview_url=preview,
            message="预览已更新",
        )
        self.event_bus.publish(
            "edit.applied", self.slide_id, self.task_id,
            status="succeeded", revision=rev.number, preview_url=preview,
            message=assistant_msg or "已应用修改",
            payload={"instruction": instruction},
        )
        diff_text = self._diff(before, after)
        self._persist()
        return EditResult(
            "success", self.slide_id, rev.number,
            assistant_msg or "已应用修改", preview,
            checksum=after.checksum, diff=diff_text,
        )

    # ── LLM helper (sync / async agnostic) ─────────────────────

    async def _call_llm(self, content: str, history: list[dict]) -> Any:
        if self.llm is None:
            raise RuntimeError("SlideEditService 未配置 language_model，无法执行对话式编辑")
        kwargs = dict(
            system_message=SYSTEM_PROMPT,
            history=history,
            response_format=EditActionPlan,
            return_json=True,
        )
        if inspect.iscoroutinefunction(type(self.llm).__call__):
            return await self.llm(content, **kwargs)
        return self.llm(content, **kwargs)

    # ── low-level action runners ───────────────────────────────

    def _run_api_actions(self, actions: list[str]) -> tuple[bool, str | None]:
        if not actions:
            return False, "未生成任何编辑操作"
        try:
            executor = CodeExecutor(retry_times=1)
            text = "\n".join(actions)
            res = executor.execute_actions(text, self.slide, self.doc, found_code=True)
            if res is not None:  # (api_lines, traceback)
                return False, f"编辑执行失败: {res[1][:500]}"
            return True, None
        except SlideEditError as e:
            return False, f"编辑执行失败: {e}"
        except Exception as e:  # noqa: BLE001
            return False, f"编辑执行异常: {e}"

    def _run_style(self, directive: str) -> tuple[bool, str | None]:
        try:
            payload = directive.split("::", 1)[1] if "::" in directive else "{}"
            style_dict = json.loads(payload) if payload.strip() else {}
            strategy = self._resolve_style(style_dict)
            SlideEditor(self.slide, self.prs).restyle(strategy)
            return True, None
        except Exception as e:  # noqa: BLE001
            return False, f"风格调整失败: {e}"

    async def _run_regen(self, instruction: str) -> tuple[bool, str | None]:
        try:
            if self.regenerate_fn is not None:
                if inspect.iscoroutinefunction(self.regenerate_fn):
                    await self.regenerate_fn(self.slide, instruction, layout_name=self.layout_name)
                else:
                    self.regenerate_fn(self.slide, instruction, layout_name=self.layout_name)
                return True, None
            # fallback: rewrite the page's text content via the same edit APIs
            return await self._regen_fallback(instruction)
        except Exception as e:  # noqa: BLE001
            return False, f"重新生成本页失败: {e}"

    async def _regen_fallback(self, instruction: str) -> tuple[bool, str | None]:
        from pptagent.editor.artifact import format_structured_context

        prompt = (
            f"请按以下要求重写本页全部文字内容：{instruction}\n\n"
            "当前页面内容：\n"
            + format_structured_context(extract_structured_data(self.slide))
            + "\n\n给出 replace_paragraph 调用以替换需要修改的段落（保持其余段落不变）。"
        )
        try:
            plan = await self._call_llm(prompt, self.dialogue)
            if isinstance(plan, EditActionPlan):
                plan = plan.model_dump()
            actions = [a for a in plan.get("actions", []) if str(a).strip()]
        except Exception as e:  # noqa: BLE001
            return False, f"重新生成调用模型失败: {e}"
        if not actions:
            return False, "重新生成未产生有效编辑"
        return self._run_api_actions(actions)

    def _resolve_style(self, style_dict: dict) -> StyleStrategy:
        registry = StyleRegistry()
        name = style_dict.get("name")
        if name and name in registry.list_names():
            return registry.get(name)
        palette = ColorPalette(
            primary=style_dict.get("primary") or "1F4E79",
            secondary=style_dict.get("secondary") or "333333",
            background=style_dict.get("background") or "FFFFFF",
            accent=style_dict.get("accent") or "2E75B6",
            title_color=style_dict.get("title_color"),
            body_color=style_dict.get("body_color"),
        )
        fonts = FontPrefs(
            title_family=style_dict.get("title_family"),
            body_family=style_dict.get("body_family"),
            title_size=style_dict.get("title_size"),
            body_size=style_dict.get("body_size"),
            bold_titles=style_dict.get("bold_titles"),
        )
        return StyleStrategy(
            name=name or "custom", description="自定义风格", palette=palette, fonts=fonts
        )

    # ── restore / replay ───────────────────────────────────────

    async def _restore_to(self, number: int) -> None:
        """Reset to baseline then replay all actions up to ``number``."""
        self.baseline.restore(self.slide)
        api_block: list[str] = []
        for line in self.revisions.actions_up_to(number):
            if line.startswith("RESTYLE::"):
                if api_block:
                    self._run_api_actions(api_block)
                    api_block = []
                self._run_style(line)
            elif line.startswith("REGEN::"):
                if api_block:
                    self._run_api_actions(api_block)
                    api_block = []
                await self._run_regen(line.split("::", 1)[1])
            else:
                api_block.append(line)
        if api_block:
            self._run_api_actions(api_block)

    # ── preview & persistence ──────────────────────────────────

    def _render_and_persist_preview(self, number: int) -> str:
        html = self._preview_renderer.render(
            self.slide, slide_idx=self.artifact.index, total_slides=self._total
        ).html
        if self.store is not None:
            path = self.store.save_preview_file(self.slide_id, number, html)
            return str(path)
        tmp = Path(tempfile.gettempdir()) / f"pptagent_edit_{self.slide_id}_{number}.html"
        tmp.write_text(html, encoding="utf-8")
        return str(tmp)

    def _diff(self, before: "SlideSnapshot", after: "SlideSnapshot") -> str | None:
        try:
            from pptagent.editor.diff import DiffEngine

            diff = DiffEngine.diff_slides(before, after)
            return diff.detail() if not diff.is_empty else None
        except Exception:  # noqa: BLE001
            return None

    def _persist(self) -> None:
        if self.store is None:
            return
        self.store.save_artifact(self.artifact)
        for rev in self.revisions._revisions.values():
            self.store.save_revision(
                self.slide_id, rev.number, rev.instruction, rev.actions,
                rev.status, rev.checksum, rev.created_at, rev.dialogue,
                rev.message, shapes_data=None, preview_path=rev.preview_path,
            )
        for pruned in self.revisions.pruned_log():
            self.store.delete_revision(self.slide_id, pruned["number"])


# ── registry ──────────────────────────────────────────────────


class EditServiceRegistry:
    """In-memory registry mapping (task_id, slide_id) → :class:`SlideEditService`."""

    def __init__(self):
        self._services: dict[tuple[str | None, str], SlideEditService] = {}

    @staticmethod
    def _key(task_id: str | None, slide_id: str) -> tuple[str | None, str]:
        return (task_id, slide_id)

    def register(self, task_id: str | None, slide_id: str, service: SlideEditService) -> None:
        self._services[self._key(task_id, slide_id)] = service

    def get(self, task_id: str | None, slide_id: str) -> SlideEditService | None:
        return self._services.get(self._key(task_id, slide_id))

    def get_or_create(
        self, task_id: str | None, slide_id: str, factory: Callable[[], SlideEditService]
    ) -> SlideEditService:
        svc = self.get(task_id, slide_id)
        if svc is None:
            svc = factory()
            self.register(task_id, slide_id, svc)
        return svc

    def list_slides(self, task_id: str | None) -> list[str]:
        return [sid for (t, sid) in self._services if t == task_id]

    def all(self) -> list[SlideEditService]:
        return list(self._services.values())


DEFAULT_REGISTRY = EditServiceRegistry()
