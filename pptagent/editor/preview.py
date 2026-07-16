"""Real-time preview rendering for slides.

Generates a self-contained HTML preview with embedded images
so the user sees exactly what the exported PPTX will look like,
without waiting for a full export.
"""

import base64
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from pptagent.presentation.presentation import Presentation, SlidePage


# ── HTML template for preview ────────────────────────────────

PREVIEW_TEMPLATE = """<!DOCTYPE html>
<html lang="zh">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>PPTAgent — {page_info}</title>
<style>
* {{ margin: 0; padding: 0; box-sizing: border-box; }}
body {{
    background: #e8ecf1;
    display: flex;
    justify-content: center;
    padding: 24px 0;
    font-family: system-ui, -apple-system, sans-serif;
}}
.wrapper {{
    position: relative;
}}
.slide {{
    background: white;
    box-shadow: 0 4px 24px rgba(0,0,0,0.12);
    position: relative;
    overflow: hidden;
}}
.page-indicator {{
    text-align: center;
    color: #64748b;
    font-size: 13px;
    margin-top: 12px;
}}
.edit-panel {{
    position: fixed;
    right: 16px;
    top: 16px;
    background: white;
    border-radius: 8px;
    box-shadow: 0 2px 12px rgba(0,0,0,0.1);
    padding: 12px 16px;
    font-size: 13px;
    color: #334155;
    max-width: 240px;
    z-index: 10;
}}
.edit-panel h3 {{
    font-size: 14px;
    margin-bottom: 8px;
    color: #1e293b;
}}
.edit-panel .row {{
    display: flex;
    justify-content: space-between;
    padding: 4px 0;
    border-bottom: 1px solid #f1f5f9;
}}
.edit-panel .row:last-child {{ border-bottom: none; }}
.edit-panel .label {{ color: #94a3b8; }}
.edit-panel .val {{ font-weight: 600; }}
.status-badge {{
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 12px;
    font-weight: 600;
}}
.status-badge.dirty {{ background: #fef3c7; color: #92400e; }}
.status-badge.clean {{ background: #dcfce7; color: #166534; }}
</style>
</head>
<body>
<div class="wrapper">
    <div class="slide">
        {slide_html}
    </div>
    <div class="page-indicator">{page_info}</div>
</div>
<div class="edit-panel" id="info">
    <h3>Preview {version_badge}</h3>
    <div class="row"><span class="label">页号</span><span class="val">{slide_idx} / {total_slides}</span></div>
    <div class="row"><span class="label">布局</span><span class="val">{layout_name}</span></div>
    <div class="row"><span class="label">元素数</span><span class="val">{shape_count}</span></div>
    <div class="row"><span class="label">校验</span><span class="val">{checksum_short}</span></div>
</div>
</body>
</html>"""


# ── image embedding helper ────────────────────────────────────

def _embed_image_in_html(html: str, image_dir: str | None = None) -> str:
    """Replace <img src=\"...\"> tags with base64-embedded images.

    Makes the preview self-contained — no file-system dependencies.
    """
    img_pattern = re.compile(r'<img\s+[^>]*src="([^"]+)"[^>]*>')

    def _replace(match: re.Match) -> str:
        src = match.group(1)
        # Try to resolve the image path
        candidates = [src]
        if image_dir:
            candidates.append(os.path.join(image_dir, os.path.basename(src)))
        for path in candidates:
            if os.path.exists(path):
                try:
                    with open(path, "rb") as f:
                        b64 = base64.b64encode(f.read()).decode()
                    ext = os.path.splitext(path)[1].lower()
                    mime_map = {".png": "image/png", ".jpg": "image/jpeg",
                                ".jpeg": "image/jpeg", ".gif": "image/gif",
                                ".svg": "image/svg+xml", ".webp": "image/webp"}
                    mime = mime_map.get(ext, "image/png")
                    return match.group(0).replace(
                        f'src="{src}"',
                        f'src="data:{mime};base64,{b64}"',
                    )
                except (IOError, OSError):
                    continue
        return match.group(0)  # keep original if cannot embed

    return img_pattern.sub(_replace, html)


# ── preview renderer ──────────────────────────────────────────

@dataclass
class PreviewResult:
    """Result of a preview render."""
    html: str
    slide_idx: int
    checksum: str
    shape_count: int
    layout_name: str | None


class PreviewRenderer:
    """Generates self-contained HTML previews of slides.

    Usage::

        renderer = PreviewRenderer()
        result = renderer.render(slide, slide_idx=2, total_slides=10)
        # Save to file or serve via API
        Path("preview_slide_02.html").write_text(result.html)
    """

    def __init__(self, image_dir: str | None = None):
        self._image_dir = image_dir
        self._last_checksums: dict[int, str] = {}

    def render(
        self,
        slide: SlidePage,
        slide_idx: int = 1,
        total_slides: int = 1,
        embed_images: bool = True,
    ) -> PreviewResult:
        """Render a single slide to a standalone HTML preview.

        Args:
            slide:       The SlidePage to preview.
            slide_idx:   1-based slide number.
            total_slides:Total slide count (for page indicator).
            embed_images:Whether to base64-embed images (True = self-contained).
        """
        from pptagent.editor.snapshot import SlideSnapshot

        # Generate base HTML. show_image=False avoids caption
        # requirements — images rendered as shapes still appear.
        slide_html = slide.to_html(show_content=True, show_image=False)

        # Strip outer <!DOCTYPE>/<html>/<body> from to_html() output
        # so we can wrap it in our preview template.
        # to_html() returns: <!DOCTYPE html>\n<html>\n<body ...>\n...\n</body>\n</html>
        body_match = re.search(
            r'<body[^>]*>(.*?)</body>',
            slide_html,
            re.DOTALL,
        )
        body_content = body_match.group(1) if body_match else slide_html

        # Embed images
        if embed_images:
            body_content = _embed_image_in_html(body_content, self._image_dir)

        # Snapshot for dirty detection
        snap = SlideSnapshot.capture(slide)
        self._last_checksums[slide_idx] = snap.checksum

        # Determine version status
        version_badge = (
            '<span class="status-badge clean">clean</span>'
        )
        is_dirty = self._is_dirty(slide_idx, snap.checksum)
        if is_dirty:
            version_badge = '<span class="status-badge dirty">changed</span>'

        # Build complete preview HTML
        shape_count = len(slide.shapes)
        html = PREVIEW_TEMPLATE.format(
            page_info=f"第{slide_idx}页 / 共{total_slides}页",
            slide_idx=slide_idx,
            total_slides=total_slides,
            layout_name=slide.slide_layout_name or "未知",
            shape_count=shape_count,
            checksum_short=snap.checksum,
            slide_html=body_content,
            version_badge=version_badge,
        )

        return PreviewResult(
            html=html,
            slide_idx=slide_idx,
            checksum=snap.checksum,
            shape_count=shape_count,
            layout_name=slide.slide_layout_name,
        )

    def render_all(
        self,
        prs: Presentation,
        embed_images: bool = True,
    ) -> list[PreviewResult]:
        """Render all slides in a presentation as previews."""
        results = []
        total = len(prs.slides)
        for i, slide in enumerate(prs.slides):
            result = self.render(slide, slide_idx=i + 1, total_slides=total,
                                 embed_images=embed_images)
            results.append(result)
        return results

    def save_preview(
        self,
        slide: SlidePage,
        output_path: str | Path,
        slide_idx: int = 1,
        total_slides: int = 1,
    ) -> PreviewResult:
        """Render and save a preview to an HTML file."""
        result = self.render(slide, slide_idx, total_slides)
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(result.html, encoding="utf-8")
        return result

    def save_all_previews(
        self,
        prs: Presentation,
        output_dir: str | Path,
    ) -> list[PreviewResult]:
        """Render and save all slides as individual HTML files."""
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        results = []
        total = len(prs.slides)
        for i, slide in enumerate(prs.slides):
            path = out / f"slide_{i + 1:02d}.html"
            result = self.save_preview(slide, path, slide_idx=i + 1,
                                       total_slides=total)
            results.append(result)
        return results

    def mark_clean(self) -> None:
        """Mark all slides as clean (reset dirty tracking)."""
        self._last_checksums.clear()

    def _is_dirty(self, slide_idx: int, current_checksum: str) -> bool:
        """Check if a slide has changed since last preview."""
        last = self._last_checksums.get(slide_idx)
        return last is not None and last != current_checksum
