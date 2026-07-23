"""Reassemble what the Design agent saw when it generated one slide.

An edit that cannot see the template is an edit that destroys it: the page's
`.tpl-*` chrome, its region grid, and its palette are not visible in the HTML
alone -- they live in the scaffold it imports. Reloading that scaffold, the
page's own manuscript section, and the design plan entry gives an editor the
same material the generator worked from, so a follow-up instruction lands
under the same contract rather than against it.

The tier contract itself is not restated here. ``layout.css`` carries it in
its header comment, written by the compiler, so the scaffold text is the one
description of hard and soft rules that cannot drift from the code.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from deeppresenter.utils.slide_lint import scaffold_expectations

# `@import url('../template_context/refs/s007/layout.css')` and plain
# `<link href="global.css">` are the two ways a generated page pulls styling.
_IMPORT_RE = re.compile(
    r"""@import\s+url\(\s*['"]?([^'")]+)|<link[^>]+href=["']([^"']+\.css)""",
)
_CSS_VARIABLE_RE = re.compile(r"(--template-[A-Za-z0-9_-]+)\s*:\s*([^;}]+)")
_PAGE_BREAK_RE = re.compile(r"^---+$", re.MULTILINE)


@dataclass(frozen=True)
class SlideDesignContext:
    """Everything the generator knew about one page, reloaded from disk.

    Both generation modes are covered. With a template, the scaffold carries
    the tier contract and the palette is fixed. Without one, ``slides/
    global.css`` is the design system the generator invented for this deck --
    the thing every other page also obeys -- so an edit has to see it for the
    same reason a template edit has to see the scaffold.
    """

    slide_index: int
    html: str
    html_path: Path
    manuscript: str | None = None
    scaffold_css: str | None = None
    scaffold_path: Path | None = None
    design_system_css: str | None = None
    theme_css: str | None = None
    plan_entry: dict | None = None
    # Classes the page must emit as empty divs, and the subset that paints
    # template imagery. Derived from the scaffold, never hand-listed.
    hard_classes: list[str] = field(default_factory=list)
    artwork_classes: list[str] = field(default_factory=list)
    palette: dict[str, str] = field(default_factory=dict)
    # Stylesheet references exactly as the page writes them. Dropping one
    # silently strips the styling while leaving the markup intact, which no
    # class-level check would notice.
    stylesheet_refs: list[str] = field(default_factory=list)

    @property
    def has_template(self) -> bool:
        return self.scaffold_css is not None


def _imported_css(html: str, html_path: Path) -> list[tuple[str, Path]]:
    """Stylesheets the page pulls in, as written and as resolved on disk."""

    found: list[tuple[str, Path]] = []
    seen: set[Path] = set()
    for match in _IMPORT_RE.finditer(html):
        target = match.group(1) or match.group(2)
        candidate = (html_path.parent / target).resolve()
        if candidate.is_file() and candidate not in seen:
            seen.add(candidate)
            found.append((target, candidate))
    return found


def _is_scaffold(text: str) -> bool:
    """Whether a stylesheet is a compiled template scaffold.

    A generated page imports both its shared `global.css` and the template's
    per-page `layout.css`. Only the latter carries the tier contract, so it
    is identified by the rules it declares rather than by its filename.
    """

    return ".slide .tpl-" in text or "Layout scaffold for" in text


def _manuscript_section(task_root: Path, slide_index: int) -> str | None:
    """The one `---`-separated manuscript section this page was written from.

    The manuscript path is recorded by the orchestrator rather than guessed:
    a workspace holds several markdown files and the deck is not always named
    after its source.
    """

    record = task_root / "intermediate_output.json"
    if not record.is_file():
        return None
    try:
        manuscript = json.loads(record.read_text(encoding="utf-8")).get("manuscript")
    except (OSError, ValueError):
        return None
    if not manuscript:
        return None
    path = Path(manuscript)
    if not path.is_absolute():
        path = task_root / path
    if not path.is_file():
        return None
    sections = _PAGE_BREAK_RE.split(path.read_text(encoding="utf-8"))
    sections = [section.strip() for section in sections if section.strip()]
    if 1 <= slide_index <= len(sections):
        return sections[slide_index - 1]
    return None


def _plan_entry(task_root: Path, slide_index: int) -> dict | None:
    """This page's row in `design_plan.json`, whatever the key is called."""

    path = task_root / "slides" / "design_plan.json"
    if not path.is_file():
        return None
    try:
        plan = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    for entry in plan.get("slides", []):
        if not isinstance(entry, dict):
            continue
        number = entry.get("slide", entry.get("page", entry.get("index")))
        if number == slide_index:
            return entry
    return None


def _palette(*sources: str | None) -> dict[str, str]:
    tokens: dict[str, str] = {}
    for source in sources:
        if source:
            for name, value in _CSS_VARIABLE_RE.findall(source):
                tokens.setdefault(name, value.strip())
    return tokens


def load_design_context(
    task_root: Path,
    html_path: Path,
    slide_index: int,
) -> SlideDesignContext:
    """Reload one page's generation-time context from the task workspace."""

    html = html_path.read_text(encoding="utf-8")
    scaffold_css = scaffold_path = None
    design_system: list[str] = []
    refs: list[str] = []
    for reference, path in _imported_css(html, html_path):
        refs.append(reference)
        text = path.read_text(encoding="utf-8", errors="ignore")
        if _is_scaffold(text) and scaffold_css is None:
            scaffold_css, scaffold_path = text, path
        else:
            design_system.append(text)
    theme_path = task_root / "template_context" / "theme.css"
    theme_css = (
        theme_path.read_text(encoding="utf-8", errors="ignore")
        if theme_path.is_file()
        else None
    )
    expectations = scaffold_expectations(html_path)
    return SlideDesignContext(
        slide_index=slide_index,
        html=html,
        html_path=html_path,
        manuscript=_manuscript_section(task_root, slide_index),
        scaffold_css=scaffold_css,
        scaffold_path=scaffold_path,
        design_system_css="\n".join(design_system) or None,
        theme_css=theme_css,
        plan_entry=_plan_entry(task_root, slide_index),
        hard_classes=expectations["required"],
        artwork_classes=expectations["artwork"],
        palette=_palette(theme_css, scaffold_css),
        stylesheet_refs=refs,
    )


def render_context_prompt(context: SlideDesignContext, instruction: str) -> str:
    """Lay the reloaded context out for the editing model."""

    blocks = [
        f"# 编辑指令\n{instruction}",
        f"# 当前页面 HTML（第 {context.slide_index} 页）\n```html\n{context.html}\n```",
    ]
    if context.manuscript:
        blocks.append(
            "# 本页文稿（设计时的内容依据）\n"
            f"```markdown\n{context.manuscript}\n```"
        )
    if context.scaffold_css:
        blocks.append(
            "# 模板脚手架 layout.css（顶部注释即分档契约，务必遵守）\n"
            f"```css\n{context.scaffold_css}\n```"
        )
    if context.design_system_css:
        blocks.append(
            "# 全局设计系统 global.css（整套幻灯片共用，改动必须与之保持一致）\n"
            f"```css\n{context.design_system_css}\n```"
        )
    if context.hard_classes:
        blocks.append(
            "# 硬约束类清单（必须原样保留为空 div，且不得被内容遮挡）\n"
            + ", ".join(f".{name}" for name in context.hard_classes)
            + (
                "\n其中绘制模板图像的："
                + ", ".join(f".{name}" for name in context.artwork_classes)
                if context.artwork_classes
                else ""
            )
        )
    if context.palette:
        blocks.append(
            "# 可用颜色令牌（只能从中取色）\n"
            + "\n".join(f"{name}: {value}" for name, value in context.palette.items())
        )
    if context.plan_entry:
        blocks.append(
            "# 设计计划条目\n```json\n"
            + json.dumps(context.plan_entry, ensure_ascii=False, indent=2)
            + "\n```"
        )
    return "\n\n".join(blocks)
