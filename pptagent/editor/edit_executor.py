"""Execute a resolved ``EditPlan`` against a single slide.

Translates each ``EditOp`` into either a checkpointed ``SlideEditor``
call (element-level text/image/style edits — no model needed) or a
single-page regeneration via the ``EditBackend`` (whole-page ops:
regenerate / relayout / set_raw_html).

Element addressing follows the repo's own convention: shapes carry a
``shape_idx`` (the ``div_id`` / ``img_id`` the coder LLM uses) and
paragraphs carry an ``idx`` (the ``paragraph_id``). ``element_id`` in
an op may be an int (``shape_idx``), a semantic name, or ``None``
(meaning "the first editable text element" — the natural target for
instructions like "精简" or "把标题改成…").
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pptagent.editor.edit_plan import (
    EditOp, EditPlan,
    OP_REPLACE_TEXT, OP_REPLACE_CONTENT, OP_ADD_PARAGRAPH,
    OP_DELETE_PARAGRAPH, OP_DELETE_ELEMENT,
    OP_REPLACE_IMAGE, OP_RESTYLE, OP_RELAYOUT,
    OP_REGENERATE, OP_SET_RAW_HTML,
)
from pptagent.editor.regen_backend import EditBackend, RegenUnavailableError
from pptagent.presentation.presentation import SlidePage
from pptagent.presentation.shapes import Picture


# ── result ────────────────────────────────────────────────────

@dataclass
class OpResult:
    op: str
    ok: bool
    detail: str = ""


@dataclass
class EditResult:
    """Outcome of executing one ``EditPlan``."""

    success: bool = True
    changed: bool = False                 # did the slide actually change?
    summary: str = ""
    op_results: list[OpResult] = field(default_factory=list)
    new_slide: SlidePage | None = None    # set when a regen produced a new slide
    html: str | None = None                # set for HTML-mode regen
    error: str | None = None


# ── executor ──────────────────────────────────────────────────

class EditPlanExecutor:
    """Apply an ``EditPlan`` to a slide.

    Holds an ``editor_factory`` (a callable ``(slide) -> SlideEditor``)
    so each plan is applied through a fresh, checkpointed editor, and a
    regen backend for whole-page ops.
    """

    def __init__(
        self,
        editor_factory=None,
        regen_backend: EditBackend | None = None,
        style_registry: Any = None,
    ):
        self._editor_factory = editor_factory
        self._regen = regen_backend
        self._styles = style_registry

    # ── public ────────────────────────────────────────────────

    async def apply(
        self,
        plan: EditPlan,
        slide: SlidePage,
        instruction: str = "",
        context: Any = None,
    ) -> EditResult:
        if not plan.ops:
            return EditResult(success=True, changed=False,
                              summary="无可执行操作", error=None)

        result = EditResult(success=True, changed=False)
        editor = self._editor_factory(slide) if self._editor_factory else None
        # side channels for regen ops (set inside _apply_regen_op)
        self._last_new_slide = None
        self._last_html = None
        # snapshot the live slide BEFORE applying any op, so a mid-plan
        # failure can roll the slide back to its pre-plan state — a
        # failed turn must never leave a partially-mutated live slide
        # diverging from the (unchanged) current revision.
        from pptagent.editor.snapshot import SlideSnapshot
        pre_plan = SlideSnapshot.capture(slide)
        mutated = False

        for op in plan.ops:
            try:
                if op.op in (OP_REGENERATE, OP_RELAYOUT, OP_SET_RAW_HTML):
                    ok, detail, changed = await self._apply_regen_op(
                        op, slide, instruction, context)
                    if ok:
                        if self._last_new_slide is not None:
                            result.new_slide = self._last_new_slide
                        if self._last_html is not None:
                            result.html = self._last_html
                else:
                    ok, detail, changed = self._apply_op(
                        op, slide, editor, instruction, context)
                    if ok and changed:
                        mutated = True
                result.op_results.append(OpResult(op=op.op, ok=ok, detail=detail))
                result.changed = result.changed or changed
                if not ok:
                    result.success = False
                    result.error = detail
                    break
            except RegenUnavailableError as e:
                # whole-page regen not available → this turn is a failed draft
                result.op_results.append(OpResult(op=op.op, ok=False,
                                                  detail=str(e)))
                result.success = False
                result.error = str(e)
                break
            except Exception as e:  # noqa: BLE001 — surface any op failure
                result.op_results.append(OpResult(op=op.op, ok=False,
                                                  detail=f"{type(e).__name__}: {e}"))
                result.success = False
                result.error = f"{type(e).__name__}: {e}"
                break

        # on failure with no regen-produced slide, roll the live slide back
        # so a failed draft never diverges from the current revision.
        # (regen ops don't mutate the live slide — they produce a separate
        # new_slide that the service only installs on success.)
        if not result.success and result.new_slide is None and mutated:
            try:
                pre_plan.restore(slide)
            except Exception:
                pass  # best-effort rollback; failure still reported

        # whole-page regen may have produced a fresh slide
        if result.new_slide is None and result.success:
            result.summary = self._summarize(plan, result)
        elif not result.success:
            result.summary = f"编辑失败: {result.error}"
        return result

    # ── op dispatch ────────────────────────────────────────────

    def _apply_op(
        self,
        op: EditOp,
        slide: SlidePage,
        editor,
        instruction: str,
        context: Any,
    ) -> tuple[bool, str, bool]:
        """Apply one element-level op. Returns (ok, detail, changed).

        Whole-page regen ops (regenerate/relayout/set_raw_html) are
        dispatched by ``apply`` via ``_apply_regen_op`` instead.
        """
        if op.op == OP_RESTYLE:
            return self._apply_restyle(op, slide, editor)
        if op.op == OP_REPLACE_IMAGE:
            return self._apply_replace_image(op, slide, editor)
        if op.op == OP_DELETE_ELEMENT:
            return self._apply_delete_element(op, slide, editor)
        # text-family ops need a resolved (shape, paragraph)
        if op.op in (OP_REPLACE_TEXT, OP_REPLACE_CONTENT,
                     OP_ADD_PARAGRAPH, OP_DELETE_PARAGRAPH):
            return self._apply_text_op(op, slide, editor, instruction)
        return False, f"未知操作类型: {op.op}", False

    # ── element resolution ────────────────────────────────────

    def _resolve_shape(self, slide: SlidePage,
                       element_id: Any) -> Any:
        """Find the target shape for an op.

        - ``int``   → shape with matching ``shape_idx``
        - ``str``   → shape whose PowerPoint/semantic name matches
        - ``None``  → the text shape with the most content (default target)

        A **concrete** id that matches nothing raises ``KeyError`` rather
        than silently editing the first element; only ``None`` falls back.
        """
        shapes = list(getattr(slide, "shapes", []))
        if isinstance(element_id, int):
            for s in shapes:
                if getattr(s, "shape_idx", None) == element_id:
                    return s
            raise KeyError(f"未找到 shape_idx={element_id} 的元素")
        if isinstance(element_id, str) and element_id:
            for s in shapes:
                name = getattr(s, "name", "") or ""
                style_name = ""
                if hasattr(s, "style") and isinstance(s.style, dict):
                    style_name = s.style.get("name", "") or s.style.get("semantic_name", "") or ""
                if element_id in (name, style_name) or element_id.lower() in (
                        name.lower(), style_name.lower()):
                    return s
            raise KeyError(f"未找到名为 {element_id!r} 的元素")
        # default: the text shape with the most content — for whole-page
        # instructions like "精简"/"缩短" this targets the body, not a
        # short title, so the edit keeps taking effect across turns.
        best, best_len = None, -1
        for s in shapes:
            if not (hasattr(s, "text_frame") and s.text_frame.is_textframe):
                continue
            total = sum(len(p.text) for p in s.text_frame.paragraphs
                        if getattr(p, "idx", -1) != -1)
            if total > best_len:
                best, best_len = s, total
        if best is not None and best_len > 0:
            return best
        # fall back to first shape at all
        if shapes:
            return shapes[0]
        raise KeyError("幻灯片没有可编辑元素")

    def _resolve_paragraph(self, shape, paragraph_id: Any):
        """Find the target paragraph.

        ``None``/``-1`` → first editable paragraph. A **concrete**
        paragraph_id that matches nothing raises ``ValueError`` rather
        than silently targeting paragraph 0.
        """
        if not hasattr(shape, "text_frame"):
            raise ValueError("该元素不是文本元素")
        paras = [p for p in shape.text_frame.paragraphs
                 if getattr(p, "idx", -1) != -1]
        if not paras:
            raise ValueError("该元素没有可编辑段落")
        if paragraph_id is None or paragraph_id == -1:
            return paras[0]
        for p in paras:
            if getattr(p, "idx", None) == paragraph_id:
                return p
        raise ValueError(
            f"段落 paragraph_id={paragraph_id} 不存在（该元素有 {len(paras)} 段）")

    # ── text ops ─────────────────────────────────────────────

    def _apply_text_op(self, op, slide, editor, instruction):
        try:
            shape = self._resolve_shape(slide, op.element_id)
        except KeyError as e:
            return False, str(e), False
        div_id = getattr(shape, "shape_idx", 0)
        # resolve paragraph (delete supports "last")
        try:
            if op.op == OP_DELETE_PARAGRAPH and op.paragraph_id in ("last", -2):
                paras = [p for p in shape.text_frame.paragraphs
                         if getattr(p, "idx", -1) != -1]
                if not paras:
                    return False, "没有可删除的段落", False
                para = paras[-1]
            else:
                para = self._resolve_paragraph(shape, op.paragraph_id)
        except ValueError as e:
            return False, str(e), False
        pid = getattr(para, "idx", 0)
        # snapshot BEFORE any edit — `para` is a live reference, so its
        # `.text` would otherwise read the post-edit value and mask changes
        original_text = getattr(para, "text", "")

        if op.op == OP_DELETE_PARAGRAPH:
            if editor is not None:
                editor.delete_paragraph(div_id, pid)
            else:
                from pptagent.apis import del_paragraph
                del_paragraph(slide, div_id, pid)
            return True, f"删除段落 div={div_id} para={pid}", True

        # resolve new text (handle sentinels)
        new_text = self._resolve_text(op, para, instruction)

        if op.op == OP_REPLACE_TEXT:
            if editor is not None:
                editor.edit_text(div_id, pid, new_text)
            else:
                from pptagent.apis import replace_paragraph
                replace_paragraph(slide, div_id, pid, new_text)
            return True, f"替换文本 div={div_id} para={pid}", new_text != original_text
        if op.op == OP_ADD_PARAGRAPH:
            # clone then replace, to grow the element by one paragraph
            if editor is not None:
                editor.clone_paragraph(div_id, pid)
                # the cloned paragraph gets a new idx; replace its text
                paras = [p for p in shape.text_frame.paragraphs
                         if getattr(p, "idx", -1) != -1]
                new_para = paras[-1]
                editor.edit_text(div_id, getattr(new_para, "idx", pid), new_text)
            else:
                from pptagent.apis import clone_paragraph, replace_paragraph
                clone_paragraph(slide, div_id, pid)
                paras = [p for p in shape.text_frame.paragraphs
                         if getattr(p, "idx", -1) != -1]
                new_para = paras[-1]
                replace_paragraph(slide, div_id, getattr(new_para, "idx", pid), new_text)
            return True, f"新增段落 div={div_id}", True
        if op.op == OP_REPLACE_CONTENT:
            # rewrite the element's paragraphs to exactly ``texts``:
            # set the first, delete the rest, then append extras.
            paras = [p for p in shape.text_frame.paragraphs
                     if getattr(p, "idx", -1) != -1]
            if not paras:
                return False, "无段落可替换", False
            texts = op.paragraphs or ([new_text] if new_text else [])
            if not texts:
                return False, "无内容", False
            # 1) first paragraph → texts[0]
            first_pid = getattr(paras[0], "idx", 0)
            if editor is not None:
                editor.edit_text(div_id, first_pid, texts[0])
            else:
                from pptagent.apis import replace_paragraph
                replace_paragraph(slide, div_id, first_pid, texts[0])
            # 2) delete the remaining original paragraphs (back-to-front)
            remaining = [p for p in shape.text_frame.paragraphs
                         if getattr(p, "idx", -1) != -1][1:]
            for p in reversed(remaining):
                pid = getattr(p, "idx", 0)
                if editor is not None:
                    editor.delete_paragraph(div_id, pid)
                else:
                    from pptagent.apis import del_paragraph
                    del_paragraph(slide, div_id, pid)
            # 3) append texts[1:] by cloning the (now sole) first paragraph
            if len(texts) > 1:
                cur = [p for p in shape.text_frame.paragraphs
                       if getattr(p, "idx", -1) != -1]
                base_pid = getattr(cur[0], "idx", 0) if cur else first_pid
                for extra_text in texts[1:]:
                    if editor is not None:
                        editor.clone_paragraph(div_id, base_pid)
                        new_last = [p for p in shape.text_frame.paragraphs
                                    if getattr(p, "idx", -1) != -1][-1]
                        editor.edit_text(div_id, getattr(new_last, "idx", base_pid),
                                         extra_text)
                    else:
                        from pptagent.apis import clone_paragraph, replace_paragraph
                        clone_paragraph(slide, div_id, base_pid)
                        new_last = [p for p in shape.text_frame.paragraphs
                                    if getattr(p, "idx", -1) != -1][-1]
                        replace_paragraph(slide, div_id,
                                          getattr(new_last, "idx", base_pid),
                                          extra_text)
            return True, f"重写内容 div={div_id} ({len(texts)}段)", True
        return False, f"未处理的文本操作: {op.op}", False

    def _resolve_text(self, op, para, instruction: str) -> str:
        """Resolve the new text for a text op, expanding sentinels."""
        text = op.text
        if text == "__SHORTEN__":
            return self._shorten(getattr(para, "text", "") or "")
        if text == "__INSTRUCTION__":
            return instruction
        return text if text is not None else ""

    @staticmethod
    def _shorten(text: str) -> str:
        """Heuristic shortening for "精简/再短一点" instructions."""
        if not text:
            return text
        # take the first sentence if there's punctuation, else 60% length
        for sep in ("。", "！", "？", ". ", "! ", "? ", "\n"):
            if sep in text:
                head = text.split(sep, 1)[0].strip()
                if head and len(head) < len(text):
                    return head + sep.strip()
        # 60% length, word/char boundary
        if len(text) <= 6:
            return text
        cut = max(1, int(len(text) * 0.6))
        return text[:cut].rstrip()

    # ── image ops ─────────────────────────────────────────────

    def _apply_replace_image(self, op, slide, editor):
        shapes = [s for s in getattr(slide, "shapes", []) if isinstance(s, Picture)]
        if not shapes:
            return False, "该页没有可替换的图片", False
        # resolve target image
        target = None
        if isinstance(op.element_id, int):
            for s in shapes:
                if getattr(s, "shape_idx", None) == op.element_id:
                    target = s
                    break
        if target is None:
            target = shapes[0]
        img_id = getattr(target, "shape_idx", 0)
        if editor is not None:
            editor.edit_image(img_id, op.image_path or "")
        else:
            from pptagent.apis import replace_image
            replace_image(slide, getattr(editor, "doc", None), img_id,
                          op.image_path or "")
        return True, f"替换图片 img_id={img_id}", True

    def _apply_delete_element(self, op, slide, editor):
        # delete an element's image (text element deletion = delete all its paragraphs)
        shapes = list(getattr(slide, "shapes", []))
        try:
            shape = self._resolve_shape(slide, op.element_id)
        except KeyError as e:
            return False, str(e), False
        if isinstance(shape, Picture):
            figure_id = getattr(shape, "shape_idx", 0)
            if editor is not None:
                editor.delete_image(figure_id)
            else:
                from pptagent.apis import del_image
                del_image(slide, figure_id)
            return True, f"删除图片 figure_id={figure_id}", True
        # text element: delete all its paragraphs one by one (back to front)
        paras = [p for p in shape.text_frame.paragraphs
                 if getattr(p, "idx", -1) != -1]
        div_id = getattr(shape, "shape_idx", 0)
        for p in reversed(paras):
            pid = getattr(p, "idx", 0)
            if editor is not None:
                editor.delete_paragraph(div_id, pid)
            else:
                from pptagent.apis import del_paragraph
                del_paragraph(slide, div_id, pid)
        return True, f"删除元素 div={div_id}", True

    # ── restyle ───────────────────────────────────────────────

    def _apply_restyle(self, op, slide, editor):
        if self._styles is None and op.style is None:
            return False, "未配置风格注册表", False
        strategy = None
        if op.style and self._styles is not None:
            try:
                strategy = self._styles.get(op.style)
            except KeyError:
                return False, f"未知风格: {op.style}", False
        if strategy is None:
            return False, "无法解析风格策略", False
        if editor is not None:
            editor.restyle(strategy)
        return True, f"应用风格 {op.style}", True

    # ── regen ops (async) ─────────────────────────────────────

    async def _apply_regen_op(
        self,
        op: EditOp,
        slide: SlidePage,
        instruction: str,
        context: Any,
    ) -> tuple[bool, str, bool]:
        """Dispatch a whole-page op to the regen backend.

        On success the new slide is stashed on the ``EditResult`` via the
        caller (``apply`` reads ``result.new_slide``); on
        ``RegenUnavailableError`` the op fails and the service treats the
        whole turn as a failed draft (current revision untouched).
        """
        if self._regen is None:
            raise RegenUnavailableError("未配置重生成后端")
        if op.op == OP_SET_RAW_HTML:
            new_html = await self._regen.regenerate_html(
                slide, context, instruction)
            # stash via a side channel read by ``apply``
            self._last_html = new_html
            return True, "HTML 重写", new_html is not None and len(new_html) > 0
        layout_name = op.layout_name if op.op == OP_RELAYOUT else None
        new_slide = await self._regen.regenerate_page(
            slide, context, layout_name=layout_name)
        self._last_new_slide = new_slide
        return True, op.note or ("换布局" if op.op == OP_RELAYOUT else "重新生成"), new_slide is not None

    # ── summary ───────────────────────────────────────────────

    def _summarize(self, plan: EditPlan, result: EditResult) -> str:
        ok = [r for r in result.op_results if r.ok]
        if not ok:
            return "无成功操作"
        names = {r.op for r in ok}
        label = {
            OP_REPLACE_TEXT: "文本修改", OP_REPLACE_CONTENT: "内容重写",
            OP_ADD_PARAGRAPH: "新增段落", OP_DELETE_PARAGRAPH: "删除段落",
            OP_DELETE_ELEMENT: "删除元素", OP_REPLACE_IMAGE: "替换图片",
            OP_RESTYLE: "风格调整", OP_REGENERATE: "重新生成",
            OP_RELAYOUT: "换布局", OP_SET_RAW_HTML: "HTML重写",
        }
        parts = [label.get(n, n) for n in names]
        return "、".join(parts)
