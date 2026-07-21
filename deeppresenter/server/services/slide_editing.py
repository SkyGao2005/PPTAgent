from __future__ import annotations
"""HTML slide editing service used by the workbench integration layer.

D group's structured editor targets ``pptagent.presentation.SlidePage``.  C2's
verified generation path persists HTML slides and PNG previews instead, so this
service provides a bridge: it edits the persisted HTML source, asks
``PreviewService`` to render a new revision, and exposes the same high-level
methods used by the D routes.
"""

import json
import re
from dataclasses import dataclass
from html import escape
from pathlib import Path
from typing import Any

from deeppresenter.server.models.artifacts import revision_dir, task_dir
from deeppresenter.server.services.preview import PreviewService


@dataclass
class HtmlEditResult:
    status: str
    slide_id: str
    revision: int | None
    message: str
    preview_path: str | None = None
    checksum: str | None = None
    diff: str | None = None
    error: str | None = None


class DeepPresenterLLMAdapter:
    """Adapt DeepPresenter ``LLM`` config objects to D's callable interface."""

    def __init__(self, llm):
        self.llm = llm

    async def __call__(
        self,
        content: str,
        *,
        system_message: str | None = None,
        history: list | None = None,
        response_format=None,
        return_json: bool = False,
        **_: Any,
    ):
        messages: list[dict[str, Any]] = []
        if system_message:
            messages.append({"role": "system", "content": system_message})
        messages.extend(history or [])
        if return_json:
            content = self._with_json_contract(content, response_format)
        messages.append({"role": "user", "content": content})
        response = await self.llm.run(
            messages,
            response_format=None if return_json else response_format,
        )
        text = response.choices[0].message.content or ""
        if return_json:
            from deeppresenter.utils.config import get_json_from_response

            parsed = get_json_from_response(text)
            if response_format is not None and hasattr(response_format, "model_validate"):
                return response_format.model_validate(parsed).model_dump()
            return parsed
        return text

    @staticmethod
    def _with_json_contract(content: str, response_format) -> str:
        if response_format is None or not hasattr(response_format, "model_json_schema"):
            return content + "\n\n请只输出一个合法 JSON 对象，不要输出 Markdown 或解释文字。"
        schema = response_format.model_json_schema()
        return (
            f"{content}\n\n"
            "请只输出一个合法 JSON 对象，不要输出 Markdown 或解释文字。"
            "JSON 必须符合以下 schema：\n"
            f"{json.dumps(schema, ensure_ascii=False)}"
        )


class PptxSlideEditService:
    """Bridge D group's ``SlideEditService`` to C2 preview/export artifacts."""

    def __init__(
        self,
        inner,
        presentation,
        preview_service: PreviewService,
        workspace_base: Path,
        task_id: str,
        slide_id: str,
        slide_index: int,
        pptx_path: Path,
    ):
        self.inner = inner
        self.presentation = presentation
        self.preview_service = preview_service
        self.workspace_base = workspace_base
        self.task_id = task_id
        self.slide_id = slide_id
        self.slide_index = slide_index
        self.pptx_path = pptx_path

    async def chat(self, instruction: str, element_id: int | None = None) -> HtmlEditResult:
        result = await self.inner.chat(instruction, element_id=element_id)
        return await self._finalize(result)

    async def apply_revision(self, number: int) -> HtmlEditResult:
        result = await self.inner.apply_revision(number)
        return await self._finalize(result)

    async def undo(self) -> HtmlEditResult:
        result = await self.inner.undo()
        return await self._finalize(result)

    async def retry(self) -> HtmlEditResult:
        result = await self.inner.retry()
        return await self._finalize(result)

    def get_revisions(self) -> dict[str, Any]:
        data = self.inner.get_revisions()
        revisions = []
        for item in data.get("revisions", []):
            mapped = dict(item)
            preview_path = mapped.get("preview_path")
            if preview_path:
                png_path = Path(preview_path).with_name("preview.png")
                if png_path.exists():
                    mapped["preview_path"] = str(png_path)
            revisions.append(mapped)
        if not revisions:
            current = self.preview_service.get_slide(self.task_id, self.slide_id)
            if current is not None and current.preview_path:
                revisions.append(
                    {
                        "number": current.revision or 1,
                        "instruction": "当前版本",
                        "status": "success",
                        "created_at": current.updated_at or current.created_at or "",
                        "message": "当前版本",
                        "preview_path": str(task_dir(self.workspace_base, self.task_id) / current.preview_path),
                        "is_current": True,
                    }
                )
        return {**data, "revisions": revisions}

    async def _finalize(self, result) -> HtmlEditResult:
        if result.status not in {"success", "no_change"}:
            return HtmlEditResult(
                result.status,
                self.slide_id,
                result.revision,
                result.message,
                preview_path=result.preview_path,
                checksum=result.checksum,
                diff=result.diff,
                error=result.error,
            )

        try:
            self.presentation.save(str(self.pptx_path))
        except Exception as exc:  # noqa: BLE001
            return HtmlEditResult(
                "failed",
                self.slide_id,
                result.revision,
                f"D 组编辑已应用，但保存 PPTX 失败：{exc}",
                error=str(exc),
            )

        preview_path = result.preview_path
        if preview_path:
            try:
                artifact = await self.preview_service.render_html_slide(
                    self.task_id,
                    Path(preview_path),
                    slide_index=self.slide_index,
                    slide_id=self.slide_id,
                    revision=result.revision or 1,
                )
                preview_path = artifact.preview_path
            except Exception:
                # Keep the D preview path in the result; callers will still
                # report a useful edit event, even if PNG rendering fails.
                pass

        return HtmlEditResult(
            result.status,
            self.slide_id,
            result.revision,
            result.message,
            preview_path=preview_path,
            checksum=result.checksum,
            diff=result.diff,
            error=result.error,
        )


class HtmlSlideEditService:
    """Revisioned editor for C2 HTML slide artifacts."""

    def __init__(self, workspace_base: Path, preview_service: PreviewService, task_id: str, slide_id: str):
        self.workspace_base = workspace_base
        self.preview_service = preview_service
        self.task_id = task_id
        self.slide_id = slide_id
        self._last_instruction: str | None = None
        self._last_element_id: int | None = None

    async def chat(self, instruction: str, element_id: int | None = None) -> HtmlEditResult:
        current = self.preview_service.get_slide(self.task_id, self.slide_id)
        if current is None:
            return HtmlEditResult("failed", self.slide_id, None, "页面不存在", error="slide_not_found")
        if current.mode != "html":
            return HtmlEditResult(
                "failed",
                self.slide_id,
                current.revision,
                "当前页面不是 HTML artifact，需使用 D 组结构化 SlideEditService 编辑。",
                error="unsupported_slide_mode",
            )

        source = self._source_path(current.source_path)
        if not source.exists():
            return HtmlEditResult(
                "failed",
                self.slide_id,
                current.revision,
                f"页面源文件不存在：{current.source_path}",
                error="source_missing",
            )

        before = source.read_text(encoding="utf-8")
        after, summary = self._apply_instruction(before, instruction)
        if after == before:
            return HtmlEditResult(
                "no_change",
                self.slide_id,
                current.revision,
                "指令已收到，但页面内容未发生变化",
                preview_path=current.preview_path,
            )

        next_revision = (current.revision or 1) + 1
        edited_rel = Path("slides") / self.slide_id / "revisions" / str(next_revision) / "edited.html"
        edited_abs = task_dir(self.workspace_base, self.task_id) / edited_rel
        edited_abs.parent.mkdir(parents=True, exist_ok=True)
        edited_abs.write_text(after, encoding="utf-8")

        artifact = await self.preview_service.render_html_slide(
            self.task_id,
            edited_abs,
            aspect_ratio=self._aspect_ratio(),
            slide_index=current.index,
            slide_id=self.slide_id,
            revision=next_revision,
        )
        self._write_meta(
            next_revision,
            {
                "number": next_revision,
                "instruction": instruction,
                "message": summary,
                "status": "success",
                "source_path": str(edited_rel),
                "preview_path": artifact.preview_path,
                "created_at": artifact.updated_at,
            },
        )
        self._last_instruction = instruction
        self._last_element_id = element_id
        return HtmlEditResult(
            "success",
            self.slide_id,
            next_revision,
            summary,
            preview_path=artifact.preview_path,
            diff=summary,
        )

    async def apply_revision(self, number: int) -> HtmlEditResult:
        meta = self._read_meta(number)
        if meta is None:
            current = self.preview_service.get_slide(self.task_id, self.slide_id)
            if current and number == current.revision:
                return HtmlEditResult(
                    "success",
                    self.slide_id,
                    current.revision,
                    f"已切换到版本 {number}",
                    preview_path=current.preview_path,
                )
            return HtmlEditResult("failed", self.slide_id, None, f"版本 {number} 不存在", error="revision_not_found")

        source = self._source_path(meta.get("source_path"))
        if not source.exists():
            return HtmlEditResult("failed", self.slide_id, number, "版本源文件不存在", error="source_missing")

        current = self.preview_service.get_slide(self.task_id, self.slide_id)
        slide_index = current.index if current else 1
        artifact = await self.preview_service.render_html_slide(
            self.task_id,
            source,
            aspect_ratio=self._aspect_ratio(),
            slide_index=slide_index,
            slide_id=self.slide_id,
            revision=number,
        )
        return HtmlEditResult(
            "success",
            self.slide_id,
            number,
            f"已切换到版本 {number}",
            preview_path=artifact.preview_path,
        )

    async def undo(self) -> HtmlEditResult:
        current = self.preview_service.get_slide(self.task_id, self.slide_id)
        if current is None or current.revision <= 1:
            return HtmlEditResult("noop", self.slide_id, None, "已是最早版本，无法继续撤销")
        return await self.apply_revision(current.revision - 1)

    async def retry(self) -> HtmlEditResult:
        if self._last_instruction is None:
            return HtmlEditResult("failed", self.slide_id, None, "没有可重试的失败指令", error="no_retry_instruction")
        return await self.chat(self._last_instruction, self._last_element_id)

    def get_revisions(self) -> dict[str, Any]:
        current = self.preview_service.get_slide(self.task_id, self.slide_id)
        revisions = []
        revisions_root = task_dir(self.workspace_base, self.task_id) / "slides" / self.slide_id / "revisions"
        if revisions_root.exists():
            for child in sorted(revisions_root.iterdir(), key=lambda path: int(path.name) if path.name.isdigit() else 0):
                if not child.is_dir() or not child.name.isdigit():
                    continue
                number = int(child.name)
                meta = self._read_meta(number) or {}
                slide_json = child / "slide.json"
                created_at = ""
                preview_path = meta.get("preview_path")
                if slide_json.exists():
                    try:
                        slide_data = json.loads(slide_json.read_text(encoding="utf-8"))
                        created_at = slide_data.get("updated_at") or slide_data.get("created_at") or ""
                        preview_path = preview_path or slide_data.get("preview_path")
                    except (OSError, ValueError, TypeError):
                        pass
                revisions.append(
                    {
                        "number": number,
                        "instruction": meta.get("instruction") or ("初始版本" if number == 1 else f"版本 {number}"),
                        "message": meta.get("message") or "",
                        "status": meta.get("status") or "success",
                        "created_at": meta.get("created_at") or created_at,
                        "preview_path": preview_path,
                        "is_current": bool(current and current.revision == number),
                    }
                )
        return {"slide_id": self.slide_id, "current": current.revision if current else 0, "revisions": revisions, "pruned": []}

    def _source_path(self, rel: str | None) -> Path:
        if not rel:
            return task_dir(self.workspace_base, self.task_id) / "missing.html"
        path = Path(rel)
        if path.is_absolute():
            return path
        return task_dir(self.workspace_base, self.task_id) / path

    def _aspect_ratio(self) -> str:
        # Current create API defaults to 16:9 and PreviewService validates this.
        return "16:9"

    def _meta_path(self, number: int) -> Path:
        return revision_dir(self.workspace_base, self.task_id, self.slide_id, number) / "edit.json"

    def _write_meta(self, number: int, payload: dict[str, Any]) -> None:
        path = self._meta_path(number)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    def _read_meta(self, number: int) -> dict[str, Any] | None:
        path = self._meta_path(number)
        if not path.exists():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError):
            return None

    def _apply_instruction(self, html: str, instruction: str) -> tuple[str, str]:
        target = _extract_target_text(instruction)
        lowered = instruction.lower()
        if target and ("标题" in instruction or "title" in lowered):
            updated = _replace_first_tag_text(html, ("h1", "h2", "h3"), target)
            if updated != html:
                return updated, f"已将标题修改为：{target}"

        if target:
            updated = _replace_first_tag_text(html, ("p", "li", "h1", "h2", "h3"), target)
            if updated != html:
                return updated, f"已将文本修改为：{target}"

        styled = _apply_style_instruction(html, instruction)
        if styled != html:
            return styled, "已根据指令调整页面样式"

        return _append_edit_note(html, instruction), "已把修改说明添加到页面"


def _extract_target_text(instruction: str) -> str | None:
    quoted = re.search(r"[“\"']([^“”\"']{1,80})[”\"']", instruction)
    if quoted:
        return quoted.group(1).strip()
    match = re.search(r"(?:改成|改为|修改为|换成|设为|变成)(.+)$", instruction)
    if not match:
        return None
    target = re.sub(r"[。！!；;，,]?$", "", match.group(1).strip())
    return target[:80] if target else None


def _replace_first_tag_text(html: str, tags: tuple[str, ...], text: str) -> str:
    escaped = escape(text)
    for tag in tags:
        pattern = re.compile(rf"(<{tag}\b[^>]*>)(.*?)(</{tag}>)", re.IGNORECASE | re.DOTALL)
        if pattern.search(html):
            return pattern.sub(lambda m: f"{m.group(1)}{escaped}{m.group(3)}", html, count=1)
    return html


def _apply_style_instruction(html: str, instruction: str) -> str:
    palette = None
    if "蓝" in instruction or "科技" in instruction:
        palette = ("#0f172a", "#2563eb", "#dbeafe")
    elif "绿" in instruction or "自然" in instruction:
        palette = ("#052e2b", "#059669", "#dcfce7")
    elif "红" in instruction or "醒目" in instruction:
        palette = ("#3f1111", "#dc2626", "#fee2e2")
    elif "黑" in instruction or "深色" in instruction:
        palette = ("#111827", "#f59e0b", "#f9fafb")
    if palette is None:
        return html

    bg, accent, ink = palette
    style = f"""
<style id="c2-html-edit-style">
  body {{ background: {bg} !important; color: {ink} !important; }}
  h1, h2, h3 {{ color: {ink} !important; }}
  h1, h2, h3, .accent {{ border-color: {accent} !important; }}
  a, strong {{ color: {accent} !important; }}
</style>
"""
    if "</head>" in html.lower():
        return re.sub(r"</head>", style + "</head>", html, count=1, flags=re.IGNORECASE)
    return style + html


def _append_edit_note(html: str, instruction: str) -> str:
    note = (
        "<div class=\"c2-edit-note\" style=\"position:absolute;right:40px;bottom:32px;"
        "max-width:45%;padding:12px 16px;border-radius:8px;background:rgba(0,0,0,.72);"
        "color:#fff;font:18px Arial,sans-serif;z-index:9999;\">"
        f"{escape(instruction)}</div>"
    )
    if "</body>" in html.lower():
        return re.sub(r"</body>", note + "</body>", html, count=1, flags=re.IGNORECASE)
    return html + note
