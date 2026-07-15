from __future__ import annotations
"""逐页预览与 SlideArtifact 持久化服务。"""

import json
import re
import uuid
from collections.abc import Awaitable, Callable
from html import escape
from pathlib import Path

from deeppresenter.server.models.artifacts import (
    SlideArtifact,
    revision_dir,
    slide_dir,
    slides_dir,
    task_dir,
)

HtmlPreviewRenderer = Callable[[Path, Path, str], Awaitable[None]]

_SLIDE_HTML_RE = re.compile(r"^slide[_-](\d+)\.html$", re.IGNORECASE)
SLIDES_INDEX_FILE = "index.json"
_VIEWPORTS = {
    "16:9": (1280, 720),
    "4:3": (960, 720),
    "A1": (2244, 3178),
    "A2": (1587, 2244),
    "A3": (1122, 1587),
    "A4": (794, 1123),
}


def parse_slide_index(path: Path) -> int:
    """从 ``slide_01.html`` / ``slide-01.html`` 文件名解析 1-based 页码。"""
    match = _SLIDE_HTML_RE.match(path.name)
    if not match:
        raise ValueError(f"Cannot parse slide index from HTML file name: {path.name}")
    return int(match.group(1))


def stable_slide_id(task_id: str, slide_index: int) -> str:
    """生成稳定 slide_id；同一任务同一页反复渲染保持不变。"""
    digest = uuid.uuid5(uuid.NAMESPACE_URL, f"{task_id}:{slide_index}").hex[:12]
    return f"sld-{digest}"


def artifact_url(task_id: str, relative_path: str) -> str:
    """生成前端访问产物的 API 路径。"""
    return f"/api/tasks/{task_id}/artifacts/{relative_path}"


class PreviewService:
    """负责把单页 HTML 渲染为预览图并写入 SlideArtifact。"""

    def __init__(
        self,
        workspace_base: Path,
        renderer: HtmlPreviewRenderer | None = None,
    ) -> None:
        self.workspace_base = workspace_base
        self.renderer = renderer or render_html_preview

    async def render_html_slide(
        self,
        task_id: str,
        html_path: Path | str,
        *,
        aspect_ratio: str = "16:9",
        slide_index: int | None = None,
        slide_id: str | None = None,
        revision: int | None = None,
    ) -> SlideArtifact:
        """渲染单页 HTML，并持久化 revision 与 current.json。

        ``html_path`` 必须位于 ``workspace_base/task_id`` 内，产物路径以任务
        工作区为根记录相对路径，供 artifact API 安全读取。
        """
        html = Path(html_path).resolve()
        root = task_dir(self.workspace_base, task_id).resolve()
        try:
            source_rel = html.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"HTML file must be inside task workspace: {html}") from exc
        if not html.exists():
            raise FileNotFoundError(f"HTML slide does not exist: {html}")

        index = slide_index or parse_slide_index(html)
        sid = slide_id or stable_slide_id(task_id, index)
        current_path = slide_dir(self.workspace_base, task_id, sid) / "current.json"
        current = self._load_current(current_path)
        rev = revision or (current.revision if current else 1)

        rev_dir = revision_dir(self.workspace_base, task_id, sid, rev)
        rev_dir.mkdir(parents=True, exist_ok=True)
        preview_rel = Path("slides") / sid / "revisions" / str(rev) / "preview.png"
        preview_abs = root / preview_rel
        await self.renderer(html, preview_abs, aspect_ratio)

        artifact_kwargs = {
            "slide_id": sid,
            "task_id": task_id,
            "index": index,
            "status": "completed",
            "mode": "html",
            "source_path": str(source_rel),
            "preview_path": str(preview_rel),
            "revision": rev,
        }
        if current:
            artifact_kwargs["created_at"] = current.created_at
        artifact = SlideArtifact(**artifact_kwargs)

        self._write_json(rev_dir / "slide.json", artifact)
        self._write_json(current_path, artifact)
        self._write_index(task_id)
        return artifact

    async def render_template_slide(
        self,
        task_id: str,
        slide_index: int,
        slide_data: dict,
        *,
        slide_id: str | None = None,
        revision: int | None = None,
        aspect_ratio: str = "16:9",
    ) -> SlideArtifact:
        """持久化模板模式单页 SlideArtifact，并生成保底 PNG 预览。

        ``slide_data`` 应包含 ``layout_name``、``title``、``subtitle``、``body``、
        ``images``、``extras`` 等字段，由 ``generate_slide`` 工具返回。
        当前先用结构化数据生成轻量 HTML，再复用 HTML 预览渲染器截图；后续可替换
        为更高保真的单页 PPTX/图片渲染器。
        """
        sid = slide_id or stable_slide_id(task_id, slide_index)
        current_path = slide_dir(self.workspace_base, task_id, sid) / "current.json"
        current = self._load_current(current_path)
        rev = revision or (current.revision if current else 1)

        rev_dir = revision_dir(self.workspace_base, task_id, sid, rev)
        rev_dir.mkdir(parents=True, exist_ok=True)

        # 持久化原始结构化数据供后续预览渲染
        source_rel = Path("slides") / sid / "revisions" / str(rev) / "source.json"
        rev_dir.mkdir(parents=True, exist_ok=True)
        source_path = rev_dir / "source.json"
        source_path.write_text(
            json.dumps(slide_data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        preview_html = rev_dir / "template_preview.html"
        preview_html.write_text(
            build_template_preview_html(slide_data),
            encoding="utf-8",
        )
        preview_rel = Path("slides") / sid / "revisions" / str(rev) / "preview.png"
        preview_abs = task_dir(self.workspace_base, task_id) / preview_rel
        await self.renderer(preview_html, preview_abs, aspect_ratio)

        from deeppresenter.server.models.artifacts import StructuredContent
        structured = StructuredContent(
            title=slide_data.get("title"),
            subtitle=slide_data.get("subtitle"),
            body=_normalize_text_list(slide_data.get("body")),
            images=slide_data.get("images") or [],
            extras=slide_data.get("extras") or {},
        )

        artifact_kwargs: dict = {
            "slide_id": sid,
            "task_id": task_id,
            "index": slide_index,
            "status": "completed",
            "mode": "template",
            "layout_name": slide_data.get("layout_name"),
            "structured_data": structured,
            "source_path": str(source_rel),
            "preview_path": str(preview_rel),
            "revision": rev,
        }
        if current:
            artifact_kwargs["created_at"] = current.created_at
        artifact = SlideArtifact(**artifact_kwargs)

        self._write_json(rev_dir / "slide.json", artifact)
        self._write_json(current_path, artifact)
        self._write_index(task_id)
        return artifact

    def list_slides(self, task_id: str) -> list[SlideArtifact]:
        """返回任务当前所有页面，按 1-based 页码排序。"""
        index_path = slides_dir(self.workspace_base, task_id) / SLIDES_INDEX_FILE
        if index_path.exists():
            try:
                data = json.loads(index_path.read_text(encoding="utf-8"))
                return self._sort_slides(
                    SlideArtifact.model_validate(item)
                    for item in data.get("slides", [])
                )
            except (OSError, ValueError, TypeError):
                pass
        return self._scan_current_slides(task_id)

    def get_slide(self, task_id: str, slide_id: str) -> SlideArtifact | None:
        """返回单页 current artifact；不存在时返回 None。"""
        current_path = slide_dir(self.workspace_base, task_id, slide_id) / "current.json"
        return self._load_current(current_path)

    def revision_count(self, task_id: str, slide_id: str) -> int:
        """返回单页已持久化 revision 数量。"""
        revisions = slide_dir(self.workspace_base, task_id, slide_id) / "revisions"
        if not revisions.exists():
            return 0
        count = 0
        for child in revisions.iterdir():
            if (
                child.is_dir()
                and child.name.isdigit()
                and (child / "slide.json").exists()
            ):
                count += 1
        return count

    @staticmethod
    def _load_current(path: Path) -> SlideArtifact | None:
        if not path.exists():
            return None
        return SlideArtifact.model_validate_json(path.read_text(encoding="utf-8"))

    def _write_index(self, task_id: str) -> None:
        root = slides_dir(self.workspace_base, task_id)
        slides = self._scan_current_slides(task_id)
        root.mkdir(parents=True, exist_ok=True)
        (root / SLIDES_INDEX_FILE).write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "total": len(slides),
                    "slides": [slide.model_dump() for slide in slides],
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )

    def _scan_current_slides(self, task_id: str) -> list[SlideArtifact]:
        root = slides_dir(self.workspace_base, task_id)
        if not root.exists():
            return []
        slides: list[SlideArtifact] = []
        for current_path in root.glob("*/current.json"):
            current = self._load_current(current_path)
            if current is not None:
                slides.append(current)
        return self._sort_slides(slides)

    @staticmethod
    def _sort_slides(slides) -> list[SlideArtifact]:
        return sorted(slides, key=lambda slide: (slide.index, slide.slide_id))

    @staticmethod
    def _write_json(path: Path, artifact: SlideArtifact) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(artifact.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


async def render_html_preview(
    html_path: Path,
    output_path: Path,
    aspect_ratio: str = "16:9",
) -> None:
    """默认 HTML 预览渲染器。

    依赖 Playwright；为避免 server 基础模型测试强制安装重依赖，Playwright 在
    函数内部 lazy import。
    """
    if aspect_ratio not in _VIEWPORTS:
        raise ValueError(f"Unsupported aspect ratio: {aspect_ratio}")

    from playwright.async_api import async_playwright

    width, height = _VIEWPORTS[aspect_ratio]
    output_path.parent.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox",
                "--disable-dev-shm-usage",
                "--disable-gpu",
            ],
        )
        try:
            page = await browser.new_page(viewport={"width": width, "height": height})
            await page.goto(html_path.as_uri(), wait_until="networkidle")
            await page.screenshot(path=str(output_path), full_page=False)
        finally:
            await browser.close()


def build_template_preview_html(slide_data: dict) -> str:
    """把模板模式结构化页面数据转换为可截图的保底 HTML。"""
    title = escape(str(slide_data.get("title") or "Untitled slide"))
    subtitle = escape(str(slide_data.get("subtitle") or ""))
    layout_name = escape(str(slide_data.get("layout_name") or "template"))
    body_items = _normalize_text_list(slide_data.get("body"))
    body_html = "\n".join(f"<li>{escape(str(item))}</li>" for item in body_items)
    image_items = slide_data.get("images") or []
    image_html = "\n".join(
        f"<div class=\"image-ref\">{escape(str(item.get('caption') or item.get('path') or item))}</div>"
        if isinstance(item, dict)
        else f"<div class=\"image-ref\">{escape(str(item))}</div>"
        for item in image_items
    )
    extras = slide_data.get("extras") or {}
    extras_html = "\n".join(
        f"<span>{escape(str(key))}: {escape(str(value))}</span>"
        for key, value in extras.items()
    )
    return f"""<!doctype html>
<html>
  <head>
    <meta charset="utf-8" />
    <style>
      html, body {{
        margin: 0;
        width: 100%;
        height: 100%;
        font-family: Arial, "PingFang SC", sans-serif;
        background: #f4f7fb;
        color: #172033;
      }}
      body {{
        box-sizing: border-box;
        padding: 6.5%;
        display: grid;
        grid-template-rows: auto 1fr auto;
        gap: 28px;
      }}
      header {{
        border-bottom: 4px solid #2f80ed;
        padding-bottom: 18px;
      }}
      .layout {{
        color: #5a6475;
        font-size: 18px;
        text-transform: uppercase;
        letter-spacing: 0;
      }}
      h1 {{
        margin: 10px 0 0;
        font-size: 54px;
        line-height: 1.08;
      }}
      h2 {{
        margin: 14px 0 0;
        color: #465166;
        font-size: 26px;
        font-weight: 500;
      }}
      main {{
        display: grid;
        align-content: center;
      }}
      ul {{
        margin: 0;
        padding-left: 34px;
        font-size: 30px;
        line-height: 1.45;
      }}
      li + li {{
        margin-top: 14px;
      }}
      .images {{
        margin-top: 28px;
        display: flex;
        flex-wrap: wrap;
        gap: 12px;
      }}
      .image-ref {{
        border: 1px solid #cfd8e6;
        background: white;
        padding: 12px 16px;
        font-size: 18px;
      }}
      footer {{
        display: flex;
        flex-wrap: wrap;
        gap: 10px;
        color: #5a6475;
        font-size: 16px;
      }}
    </style>
  </head>
  <body>
    <header>
      <div class="layout">{layout_name}</div>
      <h1>{title}</h1>
      {f"<h2>{subtitle}</h2>" if subtitle else ""}
    </header>
    <main>
      {f"<ul>{body_html}</ul>" if body_html else ""}
      {f"<section class=\"images\">{image_html}</section>" if image_html else ""}
    </main>
    <footer>{extras_html}</footer>
  </body>
</html>
"""


def _normalize_text_list(value) -> list[str]:
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value]
    return [str(value)]
