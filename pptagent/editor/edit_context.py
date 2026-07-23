"""Assemble the model context for a single-page conversational edit.

Direction D specifies exactly what to give the model on each edit
(section "对话上下文") — and, just as importantly, what *not* to give:

- the current page screenshot
- the current page ``structured_data``, layout name and element schema
- the current PPT's title and outline (global consistency)
- the most recent few turns of dialogue on this page
- the user's explicitly selected element (if any)

Assembling this in one place keeps the edit service cheap and the
model's attention focused, avoiding the cost and semantic drift the
plan warns about.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from typing import Any, Callable

from pptagent.editor.conversation import ConversationLog
from pptagent.editor.snapshot import SlideSnapshot
from pptagent.presentation.presentation import SlidePage


# type for an optional screenshot provider: slide -> (bytes, mime) or path
ScreenshotProvider = Callable[[SlidePage], tuple[bytes, str] | str | None]


# ── edit context ──────────────────────────────────────────────

@dataclass
class EditContext:
    """Everything the model needs to act on one instruction.

    Use ``to_prompt_dict()`` for a text-only model, or ``to_vision_parts()``
    for a multimodal call that includes the page screenshot.
    """

    instruction: str
    slide_index: int
    layout_name: str | None
    slide_title: str | None
    structured_data: dict[str, Any]            # shapes_data view
    element_schema: dict[str, Any] | None      # template layout schema (optional)
    outline: dict[str, Any]                     # PPT title + outline
    recent_turns: list[dict[str, str]]         # [{role, content}]
    selected_element: str | None = None
    screenshot_b64: str | None = None          # base64 PNG, if available
    screenshot_mime: str = "image/png"
    preview_html: str | None = None            # HTML fallback when no raster

    # ── serialization for the model ──────────────────────────

    def to_prompt_dict(self) -> dict[str, Any]:
        """A plain-dict view suitable for a text-only model prompt."""
        return {
            "instruction": self.instruction,
            "selected_element": self.selected_element,
            "slide": {
                "index": self.slide_index,
                "layout": self.layout_name,
                "title": self.slide_title,
                "elements": self.structured_data.get("shapes_data", []),
                "element_schema": self.element_schema,
            },
            "presentation": {
                "title": self.outline.get("title", ""),
                "outline": self.outline.get("outline", []),
            },
            "recent_turns": self.recent_turns,
        }

    def to_vision_parts(self) -> list[dict[str, Any]]:
        """Multimodal message parts: text prompt + optional image."""
        parts: list[dict[str, Any]] = [{
            "type": "text",
            "text": self._render_text_prompt(),
        }]
        if self.screenshot_b64:
            parts.append({
                "type": "image",
                "source": {
                    "type": "base64",
                    "media_type": self.screenshot_mime,
                    "data": self.screenshot_b64,
                },
            })
        return parts

    def _render_text_prompt(self) -> str:
        import json
        ctx = self.to_prompt_dict()
        lines = [
            "你是一个PPT单页编辑助手。根据用户的自然语言指令，对当前这一页做局部修改。",
            "只修改这一页，不要影响其他页。返回结构化的编辑操作列表（JSON）。",
            "",
            f"【用户指令】{self.instruction}",
        ]
        if self.selected_element:
            lines.append(f"【选中元素】{self.selected_element}")
        lines.append("")
        lines.append("【当前页信息】")
        lines.append(json.dumps(ctx["slide"], ensure_ascii=False, indent=2))
        lines.append("")
        lines.append("【全局标题与大纲】")
        lines.append(json.dumps(ctx["presentation"], ensure_ascii=False, indent=2))
        if ctx["recent_turns"]:
            lines.append("")
            lines.append("【最近对话】")
            for t in ctx["recent_turns"][-6:]:
                lines.append(f"{t['role']}: {t['content']}")
        return "\n".join(lines)


# ── builder ───────────────────────────────────────────────────

def build_edit_context(
    slide: SlidePage,
    instruction: str,
    conversation: ConversationLog,
    *,
    outline: dict[str, Any] | None = None,
    element_schema: dict[str, Any] | None = None,
    selected_element: str | None = None,
    screenshot_provider: ScreenshotProvider | None = None,
    preview_renderer: Any = None,
    total_slides: int | None = None,
) -> EditContext:
    """Capture a snapshot of everything the model needs for one edit.

    Args:
        slide:               the live SlidePage being edited.
        instruction:        the user's natural-language instruction.
        conversation:        this slide's ConversationLog (recent turns).
        outline:             PPT title + outline (global consistency).
        element_schema:      the template layout's element schema, if any.
        selected_element:    an element_id the user clicked (P1).
        screenshot_provider: callable returning (bytes, mime) or a path
                             for the page screenshot. Optional.
        preview_renderer:    a PreviewRenderer used as an HTML fallback
                             when no raster screenshot is available.
        total_slides:        for the preview page indicator.
    """
    snap = SlideSnapshot.capture(slide)

    # screenshot — try the provider, then fall back to HTML preview
    screenshot_b64: str | None = None
    screenshot_mime = "image/png"
    preview_html: str | None = None
    if screenshot_provider is not None:
        try:
            result = screenshot_provider(slide)
            if isinstance(result, tuple) and len(result) == 2:
                raw, mime = result
                screenshot_b64 = base64.b64encode(raw).decode()
                screenshot_mime = mime
            elif isinstance(result, (str, bytes)):
                data = result.encode() if isinstance(result, str) else result
                # treat string as a path if it exists, else raw bytes
                if isinstance(result, str):
                    import os
                    if os.path.exists(result):
                        with open(result, "rb") as f:
                            data = f.read()
                screenshot_b64 = base64.b64encode(data).decode()
        except Exception:
            screenshot_b64 = None
    if screenshot_b64 is None and preview_renderer is not None:
        try:
            total = total_slides if total_slides is not None else 1
            prev = preview_renderer.render(slide, slide_idx=slide.slide_idx,
                                           total_slides=total)
            preview_html = prev.html
        except Exception:
            preview_html = None

    return EditContext(
        instruction=instruction,
        slide_index=slide.slide_idx,
        layout_name=snap.layout_name,
        slide_title=snap.slide_title,
        structured_data=snap.to_dict(),
        element_schema=element_schema,
        outline=outline or {},
        recent_turns=conversation.as_messages(),
        selected_element=selected_element,
        screenshot_b64=screenshot_b64,
        screenshot_mime=screenshot_mime,
        preview_html=preview_html,
    )
