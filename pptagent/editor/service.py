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
from copy import deepcopy
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
    """Conversational, versioned editor for one slide.

    Supports two modes:

    - ``mode="template"``: operates on a ``SlidePage`` / ``Presentation`` pair,
      using content-action / style / regenerate pipelines.
    - ``mode="html"``: operates directly on an HTML file, using an LLM to
      rewrite the page and a preview renderer to regenerate thumbnails.
    """

    def __init__(
        self,
        slide: SlidePage | None = None,
        presentation: Presentation | None = None,
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
        # ── HTML-mode only ────────────────────────────────────
        html_source_path: str | None = None,
        workspace: Path | None = None,
        preview_renderer: Callable | None = None,
    ):
        self.mode = mode
        self.llm = llm
        self.task_id = task_id
        self.layout_name = layout_name
        self.source_path = source_path
        self.outline = outline
        self.event_bus = event_bus or get_default_bus()
        self.store = store
        self.regenerate_fn = regenerate_fn
        self.max_revisions = max_revisions
        self._html_source_path = html_source_path
        self._workspace = workspace
        self._preview_renderer_fn = preview_renderer
        self.dialogue: list[dict[str, Any]] = []
        self.lock = asyncio.Lock()
        self._last_instruction: str | None = None
        self._last_element_id: int | None = None

        if mode == "html":
            self.slide = None
            self.prs = None
            self.doc = doc
            self._total = 1
            if slide_id is None:
                slide_id = f"sld-{uuid.uuid4().hex[:12]}"
            self.slide_id = slide_id
            self.baseline = None
            self.revisions = None  # type: ignore[assignment]
            self._preview_renderer = None  # type: ignore[assignment]
            self._revision = 1
            self.artifact = SlideArtifact(
                slide_id=self.slide_id,
                index=0,
                mode=mode,
                task_id=task_id,
                status="ready",
                layout_name=layout_name,
                structured_data={"title": "", "subtitle": "", "body": []},
                source_path=source_path or html_source_path,
                revision=1,
            )
        else:
            if slide is None or presentation is None:
                raise ValueError("template mode requires slide and presentation")
            self.slide = slide
            self.prs = presentation
            self.doc = doc
            self.slide_id = slide_id or f"s{slide.slide_idx}_{uuid.uuid4().hex[:8]}"
            self.layout_name = layout_name or slide.slide_layout_name
            self._total = len(presentation.slides)
            self.baseline = SlideSnapshot.capture(slide)
            self.revisions = RevisionManager(self.baseline, max_revisions=max_revisions)
            self._preview_renderer = PreviewRenderer()
            self._revision = 0
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
        if self.mode == "html":
            return await self._execute_html(instruction)
        result = await self._execute(instruction, element_id)
        self.dialogue.append({"role": "assistant", "content": result.message or ""})
        return result

    async def undo(self) -> EditResult:
        """Undo the last revision (pointer move + replay, no model call)."""
        if self.mode == "html":
            return await self._undo_html()
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
        if self.mode == "html":
            return await self._apply_revision_html(number)
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
        if self.mode == "html":
            if self._last_instruction is None:
                return EditResult("failed", self.slide_id, None, "没有可重试的失败指令")
            return await self._execute_html(self._last_instruction)
        async with self.lock:
            if self._last_instruction is None:
                return EditResult("failed", self.slide_id, None, "没有可重试的失败指令")
            return await self._execute(self._last_instruction, self._last_element_id)

    def get_revisions(self) -> dict[str, Any]:
        """Return the revision list and the pruned (dropped) revision log."""
        if self.mode == "html":
            return self._get_revisions_html()
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
        """Apply a batch of API calls, or leave the slide exactly as it was.

        The calls mutate the slide one at a time, so a batch that fails
        halfway leaves it half-edited -- the user sees a broken page and the
        next retry compounds it. Generation avoids that by editing a copy;
        this path now does the same and only publishes a slide that survived
        every call.
        """

        if not actions:
            return False, "未生成任何编辑操作"
        draft = deepcopy(self.slide)
        try:
            executor = CodeExecutor(retry_times=1)
            text = "\n".join(actions)
            res = executor.execute_actions(text, draft, self.doc, found_code=True)
            if res is not None:  # (api_lines, traceback)
                return False, f"编辑执行失败: {res[1][:500]}"
        except SlideEditError as e:
            return False, f"编辑执行失败: {e}"
        except Exception as e:  # noqa: BLE001
            return False, f"编辑执行异常: {e}"
        self._commit(draft)
        return True, None

    def _commit(self, draft: SlidePage) -> None:
        """Adopt a successfully edited draft in place of the live slide.

        Every edit API works through ``slide.shapes``, so that is the whole of
        what a successful batch produced.
        """

        self.slide.shapes = draft.shapes

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

    # ── HTML-mode editing ──────────────────────────────────────

    async def _execute_html(self, instruction: str) -> EditResult:
        """HTML mode: read HTML, send to LLM, write back, re-render."""
        if self._html_source_path is None or self._workspace is None:
            return EditResult("failed", self.slide_id, None, "HTML 源文件路径未配置")
        html_path = self._workspace / self._html_source_path
        if not html_path.exists():
            return EditResult("failed", self.slide_id, None, f"HTML 文件不存在: {html_path}")

        html_content = html_path.read_text(encoding="utf-8")
        self.event_bus.publish(
            "edit.started", self.slide_id, self.task_id,
            status="running", message=instruction,
        )

        new_html = await self._call_llm_html(html_content, instruction)
        if new_html is None:
            self._last_instruction = instruction
            self.event_bus.publish(
                "edit.failed", self.slide_id, self.task_id,
                status="failed", message="模型调用失败",
            )
            return EditResult("failed", self.slide_id, None, "模型调用失败")

        changed = new_html.strip() != html_content.strip()
        if not changed:
            self.event_bus.publish(
                "edit.applied", self.slide_id, self.task_id,
                status="succeeded", revision=self._revision,
                message="内容未发生变化",
            )
            return EditResult("no_change", self.slide_id, self._revision, "内容未发生变化")

        # Save revision on disk
        rev_dir = self._workspace / "slides" / self.slide_id / "revisions"
        existing = sorted([int(p.name) for p in rev_dir.iterdir() if p.is_dir() and p.name.isdigit()]) if rev_dir.exists() else []
        new_rev = max(existing, default=self._revision) + 1
        new_rev_dir = rev_dir / str(new_rev)
        new_rev_dir.mkdir(parents=True, exist_ok=True)
        (new_rev_dir / "previous.html").write_text(html_content, encoding="utf-8")
        (new_rev_dir / "slide.html").write_text(new_html, encoding="utf-8")
        (new_rev_dir / "instruction.txt").write_text(instruction, encoding="utf-8")
        html_path.write_text(new_html, encoding="utf-8")

        self._revision = new_rev
        self.artifact.revision = new_rev
        self._last_instruction = instruction

        # Re-render preview
        preview = None
        if self._preview_renderer_fn is not None:
            try:
                preview = await self._preview_renderer_fn(html_path, new_rev)
            except Exception:
                pass

        self.event_bus.publish(
            "edit.preview_ready", self.slide_id, self.task_id,
            status="succeeded", revision=new_rev, preview_url=preview,
            message="预览已更新",
        )
        self.event_bus.publish(
            "edit.applied", self.slide_id, self.task_id,
            status="succeeded", revision=new_rev, preview_url=preview,
            message=f"已应用修改: {instruction[:60]}",
        )
        return EditResult(
            "success", self.slide_id, new_rev,
            f"已应用修改: {instruction[:60]}", preview,
        )

    async def _call_llm_html(self, html_content: str, instruction: str) -> str | None:
        """Call an OpenAI-compatible LLM to edit HTML."""
        if self.llm is None:
            return None
        try:
            from openai import AsyncOpenAI
            client = AsyncOpenAI(api_key=self.llm.api_key, base_url=self.llm.base_url)
            prompt = (
                "你是一个 PPT 幻灯片编辑助手。请根据用户要求修改 HTML 代码。\n\n"
                "## 当前 HTML\n```html\n" + html_content + "\n```\n\n"
                "## 修改要求\n" + instruction + "\n\n"
                "直接返回完整的修改后 HTML 代码。保持原有 CSS 样式结构不变。"
            )
            response = await client.chat.completions.create(
                model=self.llm.model,
                messages=[{"role": "user", "content": prompt}],
                temperature=0.7, max_tokens=16384,
                **self.llm.sampling_parameters,
            )
            reply = response.choices[0].message.content or ""
        except Exception:
            return None

        import re
        match = re.search(r'```(?:html)?\s*\n?(.*?)```', reply, re.DOTALL | re.IGNORECASE)
        if match:
            return match.group(1).strip()
        if reply.strip().startswith('<!DOCTYPE') or reply.strip().startswith('<'):
            return reply.strip()
        html_match = re.search(r'(<!DOCTYPE[^<]*<html.*?</html>)', reply, re.DOTALL | re.IGNORECASE)
        return html_match.group(1).strip() if html_match else html_content

    async def _undo_html(self) -> EditResult:
        """Undo for HTML mode: restore previous version."""
        if self._workspace is None or self._html_source_path is None:
            return EditResult("failed", self.slide_id, None, "工作区未配置")
        rev_dir = self._workspace / "slides" / self.slide_id / "revisions"
        existing = sorted([int(p.name) for p in rev_dir.iterdir() if p.is_dir() and p.name.isdigit()]) if rev_dir.exists() else []
        html_path = self._workspace / self._html_source_path
        current = html_path.read_text(encoding="utf-8")

        if len(existing) < 1:
            return EditResult("noop", self.slide_id, None, "没有可撤销的版本")

        if self._revision <= 1:
            return EditResult("noop", self.slide_id, None, "已是最早版本")

        # Target: go to previous revision (current - 1)
        target_rev = self._revision - 1
        if target_rev < 1:
            target_rev = 1

        prev_html = self._find_revision_content(target_rev, rev_dir)
        if prev_html is None:
            return EditResult("failed", self.slide_id, None,
                              f"版本 {target_rev} 内容缺失，无法撤销")

        # Save current for redo
        redo_dir = rev_dir / "_undo"
        redo_dir.mkdir(parents=True, exist_ok=True)
        (redo_dir / "previous.html").write_text(current, encoding="utf-8")
        (redo_dir / "previous_rev.txt").write_text(str(self._revision), encoding="utf-8")

        html_path.write_text(prev_html, encoding="utf-8")
        prev_url = await self._call_renderer(new_rev=target_rev)

        self._revision = target_rev
        self.artifact.revision = target_rev

        self.event_bus.publish(
            "edit.reverted", self.slide_id, self.task_id,
            status="succeeded", revision=target_rev,
            message=f"已撤销到版本 {target_rev}",
            preview_url=prev_url,
        )
        return EditResult("success", self.slide_id, target_rev,
                          f"已撤销到版本 {target_rev}", prev_url)

    async def _apply_revision_html(self, number: int) -> EditResult:
        """Apply a specific revision in HTML mode — no new version created."""
        if self._workspace is None or self._html_source_path is None:
            return EditResult("failed", self.slide_id, None, "工作区未配置")

        if number == self._revision:
            return EditResult("no_change", self.slide_id, number, "已是当前版本")

        html_path = self._workspace / self._html_source_path
        rev_dir = self._workspace / "slides" / self.slide_id / "revisions"
        restored = self._find_revision_content(number, rev_dir)

        if restored is None:
            # Fallback: read the original slide HTML
            if html_path.exists():
                restored = html_path.read_text(encoding="utf-8")
            else:
                return EditResult("failed", self.slide_id, None, f"版本 {number} 内容缺失")

        # Save current for undo
        current = html_path.read_text(encoding="utf-8")
        redo_dir = rev_dir / "_undo"
        redo_dir.mkdir(parents=True, exist_ok=True)
        (redo_dir / "previous.html").write_text(current, encoding="utf-8")
        (redo_dir / "previous_rev.txt").write_text(str(self._revision), encoding="utf-8")

        # Restore target revision
        html_path.write_text(restored, encoding="utf-8")

        # Re-render preview
        prev_url = await self._call_renderer(new_rev=number)

        self._revision = number
        self.artifact.revision = number

        self.event_bus.publish(
            "edit.reverted", self.slide_id, self.task_id,
            status="succeeded", revision=number,
            message=f"已切换到版本 {number}",
            preview_url=prev_url,
        )
        return EditResult("success", self.slide_id, number,
                          f"已切换到版本 {number}", prev_url)

    def _find_revision_content(self, number: int, rev_dir: Path) -> str | None:
        """Try multiple strategies to find the HTML content for a given revision.

        1. revisions/{number}/slide.html        — new content after edit N
        2. revisions/{number}/previous.html     — content BEFORE edit N (which IS v{N-1})
        3. revisions/{number+1}/previous.html   — content BEFORE edit N+1 (which IS v{N})
        4. revisions/{number}/preview.png exists but no HTML — generated initial revision
        """
        candidates = [
            rev_dir / str(number) / "slide.html",
            rev_dir / str(number) / "previous.html",
            rev_dir / str(number + 1) / "previous.html",
        ]
        for p in candidates:
            if p.exists():
                return p.read_text(encoding="utf-8")
        # For initial revision (v1), look for any earlier revision's previous.html
        if number == 1:
            # v1's content IS the original HTML at the time of first edit
            # Check if there's any revision dir
            all_dirs = sorted([int(d.name) for d in rev_dir.iterdir() if d.is_dir() and d.name.isdigit()])
            if all_dirs and min(all_dirs) > 1:
                # If v1 doesn't exist on disk but v2+ does, v1 is the original
                # which is in v2's previous.html
                for d in sorted(all_dirs):
                    p = rev_dir / str(d) / "previous.html"
                    if p.exists():
                        return p.read_text(encoding="utf-8")
        return None

    async def _call_renderer(self, new_rev: int) -> str | None:
        """Re-render the HTML preview, returning the artifact URL if successful."""
        if self._preview_renderer_fn is None or self._html_source_path is None or self._workspace is None:
            return None
        try:
            html_path = self._workspace / self._html_source_path
            return await self._preview_renderer_fn(html_path, new_rev)
        except Exception:
            return None

    def _get_revisions_html(self) -> dict[str, Any]:
        """List revisions for HTML mode by scanning disk."""
        if self._workspace is None:
            return {"slide_id": self.slide_id, "current": self._revision, "revisions": [], "pruned": []}
        rev_dir = self._workspace / "slides" / self.slide_id / "revisions"
        revs = []
        if rev_dir.exists():
            for p in sorted(rev_dir.iterdir(), reverse=True):
                if p.is_dir() and p.name.isdigit():
                    inst = ""
                    if (p / "instruction.txt").exists():
                        inst = (p / "instruction.txt").read_text(encoding="utf-8")[:100]
                    revs.append({"number": int(p.name), "instruction": inst, "status": "success"})
        return {"slide_id": self.slide_id, "current": self._revision, "revisions": revs, "pruned": []}


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
