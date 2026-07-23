"""Deterministic slide checks that accompany the rendered preview.

A vision model looking at its own render is unreliable exactly when the page
is worst: text that landed on a same-coloured ground is invisible in the
screenshot too, so "review the image" finds nothing to review. These checks
measure the DOM and the pixels instead and name the defect in text.
"""

from __future__ import annotations

import io
import re
from pathlib import Path

from PIL import Image
from playwright.async_api import BrowserContext

# Text-bearing leaf boxes, canvas overflows, computed font sizes, text-on-text
# collisions, and the scaffold contract: required chrome divs present, painted
# template artwork not buried under opaque content. Chrome (`.tpl-*`) is
# excluded from the overflow check: templates bleed decoration past the canvas
# on purpose, and the page cannot move it anyway.
_LINT_JS = """
(expected) => {
    const width = window.innerWidth, height = window.innerHeight;
    const texts = [], overflows = [], holders = [];
    for (const el of document.querySelectorAll('body *')) {
        const cs = getComputedStyle(el);
        if (cs.display === 'none' || cs.visibility === 'hidden'
            || parseFloat(cs.opacity) === 0) continue;
        const rect = el.getBoundingClientRect();
        if (rect.width <= 0 || rect.height <= 0) continue;
        const snippet = (el.textContent || '').trim()
            .replace(/\\s+/g, ' ').slice(0, 40);
        const hasText = [...el.childNodes].some(
            (node) => node.nodeType === 3 && node.textContent.trim());
        const dx = Math.max(0, Math.round(rect.right - width),
                            Math.round(-rect.left));
        const dy = Math.max(0, Math.round(rect.bottom - height),
                            Math.round(-rect.top));
        // Only content that would actually be lost: an empty decorative div
        // overhanging the canvas is clipped harmlessly by the slide itself.
        if ((hasText || el.tagName === 'IMG') && (dx > 2 || dy > 2)
                && overflows.length < 12) {
            overflows.push({
                label: snippet || '<' + el.tagName.toLowerCase()
                    + (el.className ? ' class="' + el.className + '"' : '')
                    + '>',
                dx, dy,
            });
        }
        if (!hasText) continue;
        holders.push(el);
        texts.push({
            x: rect.x, y: rect.y, w: rect.width, h: rect.height,
            size: parseFloat(cs.fontSize), snippet,
        });
    }

    // Two text boxes on top of each other render as glyph soup. Ancestor
    // pairs are one text nested in another, which is normal markup.
    const text_overlaps = [];
    for (let i = 0; i < holders.length; i++) {
        for (let j = i + 1; j < holders.length; j++) {
            if (text_overlaps.length >= 8) break;
            if (holders[i].contains(holders[j])
                || holders[j].contains(holders[i])) continue;
            const a = texts[i], b = texts[j];
            const ix = Math.min(a.x + a.w, b.x + b.w) - Math.max(a.x, b.x);
            const iy = Math.min(a.y + a.h, b.y + b.h) - Math.max(a.y, b.y);
            if (ix <= 3 || iy <= 3) continue;
            const smaller = Math.min(a.w * a.h, b.w * b.h);
            if (smaller <= 0 || (ix * iy) / smaller < 0.25) continue;
            text_overlaps.push({a: a.snippet, b: b.snippet});
        }
    }

    const missing_chrome = (expected.required || []).filter(
        (cls) => !document.querySelector('.' + cls));

    // Template artwork the scaffold paints itself (logos, cut photography)
    // must stay visible. Sample its box: a point is covered when something
    // opaque sits above the element in the hit-test stack.
    const covered_artwork = [];
    for (const cls of expected.artwork || []) {
        const el = document.querySelector('.' + cls);
        if (el === null) continue;
        const rect = el.getBoundingClientRect();
        const x0 = Math.max(0, rect.left), y0 = Math.max(0, rect.top);
        const x1 = Math.min(width, rect.right);
        const y1 = Math.min(height, rect.bottom);
        if (x1 - x0 < 8 || y1 - y0 < 8) continue;
        let covered = 0, total = 0;
        for (let i = 0; i < 5; i++) {
            for (let j = 0; j < 5; j++) {
                const px = x0 + (x1 - x0) * (i + 0.5) / 5;
                const py = y0 + (y1 - y0) * (j + 0.5) / 5;
                total++;
                for (const other of document.elementsFromPoint(px, py)) {
                    if (other === el || el.contains(other)
                        || other.contains(el)) break;
                    // The template layers its own chrome over its own
                    // artwork on purpose; only the page's content can bury.
                    if ([...other.classList].some(
                            (c) => c.startsWith('tpl-')
                                || c.startsWith('dec-'))) continue;
                    const os = getComputedStyle(other);
                    const bg = os.backgroundColor;
                    const opaque = other.tagName === 'IMG'
                        || os.backgroundImage !== 'none'
                        || (bg && bg !== 'transparent'
                            && !/rgba\\([^)]*,\\s*0\\)/.test(bg));
                    if (opaque) { covered++; break; }
                }
            }
        }
        if (total && covered / total >= 0.6) {
            covered_artwork.push({label: cls,
                                  fraction: Math.round(100 * covered / total)});
        }
    }

    return {width, height, texts, overflows, text_overlaps,
            missing_chrome, covered_artwork};
}
"""

# A text crop whose p2-p98 luminance spread is below this renders as one
# flat surface: the letters do not separate from their ground.
_MIN_LUMINANCE_SPREAD = 32
_MIN_READABLE_FONT_PX = 14
# One uniform rectangle this large is dead canvas, not breathing room.
_EMPTY_RECT_FRACTION = 0.45
_MAX_WARNINGS = 10

_GRID_COLS = 64
_GRID_ROWS = 36
# A cell whose own luminance range stays below this holds no drawing.
_CELL_FLAT_RANGE = 10

_IMPORT_RE = re.compile(
    r"""@import\s+url\(\s*['"]?([^'")]+)|<link[^>]+href=["']([^"']+\.css)""",
)
_TPL_RULE_RE = re.compile(r"\.slide \.(tpl-\d+)\{([^}]*)\}()")
_PAINTED_REGION_RE = re.compile(
    r"\.slide \.(r-[A-Za-z0-9_]+)\{([^}]*)\}\s*/\* (\w+)"
    r"[^\n]*painted from the template asset"
)
_FULL_BLEED_RE = re.compile(r"left:0%?;top:0%?;width:100%;height:100%")


def scaffold_expectations(html_path: Path) -> dict[str, list[str]]:
    """Mandatory scaffold classes the page's imported layout CSS declares.

    ``required`` lists every hard-tier class the page must emit as a div;
    ``artwork`` is the subset that paints a non-background template image and
    therefore must also stay visible. A painted background's job is to sit
    under the content, so it is required but never "covered".
    """

    required: list[str] = []
    artwork: list[str] = []
    html = html_path.read_text(encoding="utf-8", errors="ignore")
    for match in _IMPORT_RE.finditer(html):
        target = match.group(1) or match.group(2)
        css_path = (html_path.parent / target).resolve()
        if not css_path.is_file():
            continue
        css = css_path.read_text(encoding="utf-8", errors="ignore")
        for rule in (*_TPL_RULE_RE.finditer(css), *_PAINTED_REGION_RE.finditer(css)):
            name, body, role = rule.group(1), rule.group(2), rule.group(3)
            required.append(name)
            if (
                "background-image" in body
                and role != "background"
                and not _FULL_BLEED_RE.search(body)
            ):
                artwork.append(name)
    return {"required": required, "artwork": artwork}


def _percentile(histogram: list[int], fraction: float) -> int:
    total = sum(histogram)
    if total == 0:
        return 0
    threshold = total * fraction
    seen = 0
    for value, count in enumerate(histogram):
        seen += count
        if seen >= threshold:
            return value
    return 255


def _luminance_spread(image: Image.Image) -> int:
    histogram = image.histogram()
    return _percentile(histogram, 0.98) - _percentile(histogram, 0.02)


def _empty_cells(image: Image.Image) -> list[list[bool]]:
    small = image.resize(
        (_GRID_COLS * 2, _GRID_ROWS * 2), Image.Resampling.BILINEAR
    )
    pixels = list(small.getdata())
    grid: list[list[bool]] = []
    for row in range(_GRID_ROWS):
        cells: list[bool] = []
        for col in range(_GRID_COLS):
            values = [
                pixels[(row * 2 + dy) * _GRID_COLS * 2 + (col * 2 + dx)]
                for dy in (0, 1)
                for dx in (0, 1)
            ]
            cells.append(max(values) - min(values) <= _CELL_FLAT_RANGE)
        grid.append(cells)
    return grid


def _largest_empty_rect_fraction(image: Image.Image) -> float:
    """Area fraction of the largest all-empty rectangle on the canvas.

    A whole-canvas flatness score misses a page whose top half is fine and
    whose bottom half is dead; the largest uniform rectangle names exactly
    that. Decoration counts as drawing, so template pages keep their
    breathing room without tripping this.
    """

    grid = _empty_cells(image)
    best = 0
    heights = [0] * _GRID_COLS
    for row in grid:
        for col, empty in enumerate(row):
            heights[col] = heights[col] + 1 if empty else 0
        stack: list[int] = []
        for col in range(_GRID_COLS + 1):
            height = heights[col] if col < _GRID_COLS else 0
            while stack and heights[stack[-1]] >= height:
                top = stack.pop()
                width = col if not stack else col - stack[-1] - 1
                best = max(best, heights[top] * width)
            stack.append(col)
    return best / (_GRID_COLS * _GRID_ROWS)


def analyze_slide(screenshot: bytes, data: dict) -> list[str]:
    """Turn one screenshot plus DOM measurements into named defects."""

    image = Image.open(io.BytesIO(screenshot)).convert("L")
    width, height = data["width"], data["height"]
    warnings: list[str] = []

    for cls in data.get("missing_chrome", []):
        warnings.append(
            f"Required template chrome '.{cls}' is not on the page. Emit "
            f'<div class="{cls}"></div> before the content; the scaffold '
            "paints it by itself."
        )

    for item in data.get("covered_artwork", []):
        warnings.append(
            f"Template artwork '.{item['label']}' is covered by opaque "
            f"content over ~{item['fraction']}% of its box. Rearrange the "
            "content so the template's own imagery stays visible."
        )

    small_fonts: list[str] = []
    for text in data["texts"]:
        left = max(0, int(text["x"]))
        top = max(0, int(text["y"]))
        right = min(width, int(text["x"] + text["w"]))
        bottom = min(height, int(text["y"] + text["h"]))
        if right - left >= 4 and bottom - top >= 4:
            spread = _luminance_spread(image.crop((left, top, right, bottom)))
            if spread < _MIN_LUMINANCE_SPREAD:
                warnings.append(
                    f"Text '{text['snippet']}' is nearly invisible against its "
                    f"background (luminance spread {spread}/255). Give it a "
                    "contrasting colour or a backing panel."
                )
        if 0 < text["size"] < _MIN_READABLE_FONT_PX:
            small_fonts.append(text["snippet"])

    for pair in data.get("text_overlaps", []):
        warnings.append(
            f"Text '{pair['a']}' and '{pair['b']}' overlap and render on top "
            "of each other. Give each its own space."
        )

    for overflow in data["overflows"]:
        warnings.append(
            f"'{overflow['label']}' extends {max(overflow['dx'], overflow['dy'])}px "
            f"past the {width}x{height} canvas and will be clipped."
        )

    if small_fonts:
        shown = "', '".join(small_fonts[:3])
        warnings.append(
            f"Text below {_MIN_READABLE_FONT_PX}px is unreadable when "
            f"projected: '{shown}'"
            + (f" and {len(small_fonts) - 3} more." if len(small_fonts) > 3 else ".")
        )

    if not data["texts"]:
        warnings.append("The page has no visible text at all.")
    else:
        empty = _largest_empty_rect_fraction(image)
        if empty >= _EMPTY_RECT_FRACTION:
            warnings.append(
                f"One empty rectangle covers ~{empty:.0%} of the canvas. "
                "Check for invisible content, or spread the content to use "
                "the space."
            )

    return warnings[:_MAX_WARNINGS]


async def lint_slide(
    context: BrowserContext,
    html_path: Path,
    width: int,
    height: int,
) -> list[str]:
    """Render one slide at its exact canvas size and measure it."""

    expected = scaffold_expectations(html_path)
    page = await context.new_page()
    try:
        await page.set_viewport_size({"width": width, "height": height})
        await page.goto(html_path.resolve().as_uri(), wait_until="networkidle")
        data = await page.evaluate(_LINT_JS, expected)
        screenshot = await page.screenshot(type="png")
    finally:
        await page.close()
    return analyze_slide(screenshot, data)
