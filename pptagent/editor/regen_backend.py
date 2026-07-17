"""Single-page regeneration backends for the two generation modes.

Direction D requires that a whole-page instruction ("换布局" / "重新生成本页")
re-runs **only the target page**, not the whole Research + generation pipeline.

- **Template mode** reuses ``PPTAgent._generate_commands`` + ``_edit_slide``
  (the same primitives the MCP ``generate_slide`` tool uses), so a regen
  stays consistent with the original template-driven generation.
- **HTML mode** keeps each page's HTML source and lets a Design Agent rewrite
  just that HTML from the instruction + screenshot (section "HTML 模式").
- **NoopRegenBackend** makes regen fail gracefully when no model is wired,
  so the version strategy's "failed draft → current revision untouched" path
  can be exercised without an API key.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

from pptagent.presentation.presentation import SlidePage


class RegenUnavailableError(RuntimeError):
    """Raised when no regeneration backend can fulfil a whole-page op."""


@runtime_checkable
class EditBackend(Protocol):
    """Pluggable single-page regeneration backend.

    Element-level edits (text/image/style) are handled by the executor
    via ``SlideEditor`` and do **not** go through here — only whole-page
    regen (``regenerate``, ``relayout``, ``set_raw_html``) does.
    """

    @property
    def available(self) -> bool:
        """Whether this backend can actually regenerate (has a model)."""
        ...

    async def regenerate_page(
        self,
        slide: SlidePage,
        context: Any,
        layout_name: str | None = None,
    ) -> SlidePage:
        """Template mode: rebuild the target page from its content.

        If ``layout_name`` is given, switch to that layout first
        (the "换布局" instruction); otherwise reuse the page's layout.
        """
        ...

    async def regenerate_html(
        self,
        slide: SlidePage,
        context: Any,
        instruction: str,
    ) -> str:
        """HTML mode: return a new HTML string for the target page."""
        ...


# ── noop backend (no model wired) ──────────────────────────────

class NoopRegenBackend:
    """A backend that always refuses to regenerate.

    Useful when no LLM/PPTAgent is available: whole-page regen ops
    become failed drafts, leaving the current revision untouched —
    which is exactly the failure behaviour the version strategy
    requires, and lets the flow be tested without an API key.
    """

    def __init__(self, reason: str = "no regeneration backend configured"):
        self.reason = reason

    @property
    def available(self) -> bool:
        return False

    async def regenerate_page(self, slide, context, layout_name=None):
        raise RegenUnavailableError(self.reason)

    async def regenerate_html(self, slide, context, instruction):
        raise RegenUnavailableError(self.reason)


# ── template-mode backend (PPTAgent) ───────────────────────────

class TemplateRegenBackend:
    """Whole-page regen for template mode via ``PPTAgent``.

    Mirrors the MCP ``generate_slide`` tool: build an ``EditorOutput``
    from the page's current element content, translate it to a command
    list, then run ``_edit_slide``. Only the target page is touched.

    Requires a ``PPTAgent`` (or subclass) that has had ``set_reference``
    called, i.e. ``agent.layouts`` / ``agent.presentation`` /
    ``agent.staffs["coder"]`` are populated.
    """

    def __init__(self, agent, *, layout_fallback: str | None = None):
        self.agent = agent
        self.layout_fallback = layout_fallback

    @property
    def available(self) -> bool:
        return (
            getattr(self.agent, "_initialized", False)
            and bool(getattr(self.agent, "layouts", None))
        )

    # ── helpers ───────────────────────────────────────────────

    def _extract_element_data(self, slide: SlidePage) -> list[dict]:
        """Rebuild ``SlideElement`` dicts from a slide's current content.

        Walks the slide's shapes, turning each text frame into one
        ``{name, data, type}`` entry. Images become ``{type: "image"}``
        entries carrying their path. This is a best-effort inverse of
        generation — enough to re-run ``write_slide``→``generate_slide``
        on the target page.
        """
        from pptagent.presentation.shapes import Picture

        elements: list[dict] = []
        for shape in getattr(slide, "shapes", []):
            # element name: prefer the shape's own name, else the
            # PowerPoint name in its style dict, else a synthetic id
            name = getattr(shape, "name", None)
            if not name and hasattr(shape, "style") and isinstance(shape.style, dict):
                name = shape.style.get("name") or shape.style.get("semantic_name")
            if not isinstance(name, str) or not name:
                name = f"element_{getattr(shape, 'shape_idx', 0)}"
            if isinstance(shape, Picture):
                img = getattr(shape, "img_path", None) or ""
                elements.append({"name": name, "data": [img], "type": "image"})
            elif hasattr(shape, "text_frame") and shape.text_frame.is_textframe:
                paras = [p.text for p in shape.text_frame.paragraphs
                         if getattr(p, "idx", -1) != -1 and p.text]
                if paras:
                    elements.append({"name": name, "data": paras, "type": "text"})
        return elements

    def _resolve_layout(self, slide: SlidePage,
                        layout_name: str | None) -> Any:
        """Pick a ``Layout`` object, falling back to the page's own layout."""
        layouts = getattr(self.agent, "layouts", {}) or {}
        if layout_name and layout_name in layouts:
            return layouts[layout_name]
        # try the slide's recorded layout name
        ln = getattr(slide, "slide_layout_name", None)
        if ln and ln in layouts:
            return layouts[ln]
        # fall back to the first text layout
        text_layouts = getattr(self.agent, "text_layouts", None) or []
        for tl in text_layouts:
            if tl in layouts:
                return layouts[tl]
        if self.layout_fallback and self.layout_fallback in layouts:
            return layouts[self.layout_fallback]
        if layouts:
            return next(iter(layouts.values()))
        raise RegenUnavailableError("no layouts available on agent")

    # ── EditBackend ───────────────────────────────────────────

    async def regenerate_page(self, slide, context, layout_name=None):
        if not self.available:
            raise RegenUnavailableError("PPTAgent not initialized (set_reference?)")
        from pptagent.response.pptgen import EditorOutput, SlideElement

        layout = self._resolve_layout(slide, layout_name)
        element_dicts = self._extract_element_data(slide)
        # ensure every layout element is represented (fill missing with [])
        layout_names = [e.name for e in layout.elements]
        for ln in layout_names:
            if not any(d["name"] == ln for d in element_dicts):
                element_dicts.append({"name": ln, "data": [""]})

        editor_output = EditorOutput(
            elements=[SlideElement(**d) for d in element_dicts]
        )
        command_list, template_id = self.agent._generate_commands(
            editor_output, layout)
        new_slide, _code_executor = await self.agent._edit_slide(
            command_list, template_id)
        # preserve the logical index/title of the target page
        try:
            new_slide.slide_idx = slide.slide_idx
        except Exception:
            pass
        return new_slide

    async def regenerate_html(self, slide, context, instruction):
        # HTML mode is a separate concern (deeppresenter Design Agent);
        # in template mode we don't rewrite raw HTML.
        raise RegenUnavailableError(
            "HTML regen is not supported by TemplateRegenBackend")


# ── HTML-mode backend ──────────────────────────────────────────

class HTMLEditBackend:
    """Whole-page regen for HTML mode.

    Stores each page's HTML source in the workspace and, when a Design
    Agent is available, rewrites only the target page's HTML from the
    instruction + page context. Without a vision model, the stored HTML
    is returned unchanged so the rest of the pipeline (preview, export)
    keeps working.
    """

    def __init__(self, vision_model=None, workspace=None,
                 html_store: dict[str, str] | None = None):
        self.vision_model = vision_model
        self.workspace = workspace
        self._html: dict[str, str] = html_store or {}

    @property
    def available(self) -> bool:
        return self.vision_model is not None

    def set_page_html(self, slide_id: str, html: str) -> None:
        self._html[slide_id] = html

    def get_page_html(self, slide_id: str) -> str | None:
        return self._html.get(slide_id)

    async def regenerate_page(self, slide, context, layout_name=None):
        raise RegenUnavailableError(
            "HTML mode does not regenerate via template; use regenerate_html")

    async def regenerate_html(self, slide, context, instruction):
        slide_id = getattr(context, "slide_id", None) or str(
            getattr(slide, "slide_idx", 0))
        if self.vision_model is None:
            # no model: keep current HTML, let the caller treat as no-op
            return self._html.get(slide_id, "")
        prompt = (
            "你是PPT单页设计助手。根据用户指令重写这一页的HTML，"
            "只输出完整的单页HTML，不要解释。\n"
            f"【用户指令】{instruction}\n"
            f"【当前HTML】\n{self._html.get(slide_id, '')}"
        )
        new_html = await self.vision_model(prompt)
        if isinstance(new_html, (list, tuple)):
            new_html = new_html[0]
        new_html = str(new_html).strip()
        self._html[slide_id] = new_html
        return new_html
