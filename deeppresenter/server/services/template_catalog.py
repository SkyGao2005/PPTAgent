"""Discover compiled templates and expose a stable frontend summary."""

from __future__ import annotations

import re
from copy import deepcopy
from functools import lru_cache
from importlib.util import find_spec
from pathlib import Path
from typing import Any, Iterable, Mapping

from PIL import Image

from deeppresenter.templates import TemplateStore, TemplateStoreError
from deeppresenter.templates.models import SUPPORTED_ASPECT_RATIOS
from deeppresenter.templates.store import TemplateRevision


_DEFAULT_PALETTE = {
    "bg": "#F4F6F8",
    "surface": "#FFFFFF",
    "primary": "#38506B",
    "accent": "#7FA6C9",
    "ink": "#22303E",
    "dark": False,
}
_HEX_COLOR = re.compile(r"^#?([0-9A-Fa-f]{6})(?:[0-9A-Fa-f]{2})?$")

_STAGE_LABELS = {
    "cover": "封面",
    "agenda": "目录",
    "section": "章节页",
    "summary": "总结",
    "closing": "封底",
    "appendix": "附录",
}
_PATTERN_LABELS = {
    "hero": "重点页",
    "cards": "卡片布局",
    "comparison": "对比页",
    "timeline": "时间线",
    "dashboard": "数据看板",
    "gallery": "图片画廊",
    "full_bleed": "全幅图片",
    "freeform": "自由版式",
}


def _templates_dir() -> Path:
    spec = find_spec("pptagent")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError("pptagent package templates are not available")
    return Path(next(iter(spec.submodule_search_locations))) / "templates"


def bundled_templates_root() -> Path:
    """Return the package root containing independently compiled Template IR."""

    return _templates_dir()


def _normalize_color(color: Any) -> str | None:
    if not isinstance(color, str):
        return None
    match = _HEX_COLOR.fullmatch(color.strip())
    if match is None:
        return None
    return f"#{match.group(1).upper()}"


def _rgb(color: str) -> tuple[int, int, int]:
    return tuple(int(color[index : index + 2], 16) for index in (1, 3, 5))  # type: ignore[return-value]


def _luminance(color: str) -> float:
    channels = []
    for value in _rgb(color):
        channel = value / 255
        channels.append(
            channel / 12.92
            if channel <= 0.04045
            else ((channel + 0.055) / 1.055) ** 2.4
        )
    return 0.2126 * channels[0] + 0.7152 * channels[1] + 0.0722 * channels[2]


def _chroma(color: str) -> int:
    values = _rgb(color)
    return max(values) - min(values)


def _distance(left: str, right: str) -> float:
    return sum((a - b) ** 2 for a, b in zip(_rgb(left), _rgb(right))) ** 0.5


def _theme_color_entries(theme: Mapping[str, Any]) -> list[tuple[int, str, set[str]]]:
    raw_colors = theme.get("colors", [])
    if not isinstance(raw_colors, list):
        return []
    entries: list[tuple[int, str, set[str]]] = []
    for item in raw_colors:
        if not isinstance(item, Mapping):
            continue
        color = _normalize_color(item.get("value"))
        if color is None:
            continue
        count = item.get("usage_count", 0)
        roles = item.get("roles", [])
        entries.append(
            (
                int(count) if isinstance(count, int) else 0,
                color,
                {
                    str(role).strip().lower()
                    for role in roles
                    if isinstance(role, str)
                }
                if isinstance(roles, list)
                else set(),
            )
        )
    return entries


def _reference_colors(revision: TemplateRevision) -> list[tuple[int, str]]:
    """Sample the cover reference, which reflects scheme colors PPTX may hide."""

    record = revision.load_slide_index()[0]
    reference = revision.resolve_path(
        revision.slide_artifact_path(record, "reference")
    )
    try:
        with Image.open(reference) as source:
            image = source.convert("RGB")
            image.thumbnail((160, 90))
            quantized = image.quantize(
                colors=12,
                method=Image.Quantize.MEDIANCUT,
            )
            palette = quantized.getpalette()
            histogram = quantized.getcolors()
    except (OSError, ValueError):
        return []
    if palette is None or histogram is None:
        return []
    result: list[tuple[int, str]] = []
    for count, palette_index in sorted(histogram, reverse=True):
        offset = palette_index * 3
        red, green, blue = palette[offset : offset + 3]
        result.append((count, f"#{red:02X}{green:02X}{blue:02X}"))
    return result


def _role_color(
    entries: Iterable[tuple[int, str, set[str]]],
    wanted: set[str],
) -> str | None:
    for _, color, roles in entries:
        if roles & wanted:
            return color
    return None


def _palette(revision: TemplateRevision, theme: Mapping[str, Any]) -> dict[str, Any]:
    theme_entries = _theme_color_entries(theme)
    sampled = _reference_colors(revision)
    candidates = sampled or [(count, color) for count, color, _ in theme_entries]
    if not candidates:
        return dict(_DEFAULT_PALETTE)

    bg = _role_color(theme_entries, {"background", "bg", "canvas"})
    bg = bg or candidates[0][1]

    primary = _role_color(theme_entries, {"primary", "brand", "title", "heading"})
    if primary is None:
        chromatic = [item for item in candidates if _chroma(item[1]) >= 32]
        primary = max(
            chromatic or candidates,
            key=lambda item: (_chroma(item[1]) * 20 + min(item[0], 2000), item[0]),
        )[1]

    accent = _role_color(theme_entries, {"accent", "secondary", "highlight"})
    if accent is None:
        distinct = [
            item
            for item in candidates
            if _distance(item[1], primary) >= 40 and _chroma(item[1]) >= 40
        ]
        accent = (
            max(distinct, key=lambda item: (_chroma(item[1]), item[0]))[1]
            if distinct
            else primary
        )

    surface = _role_color(theme_entries, {"surface", "card"})
    if surface is None:
        light = [item for item in candidates if _luminance(item[1]) >= 0.78]
        surface = max(light, key=lambda item: item[0])[1] if light else "#FFFFFF"

    dark = _luminance(bg) < 0.42
    ink = _role_color(theme_entries, {"text", "body", "ink", "foreground"})
    if ink is None:
        if dark:
            ink = "#FFFFFF"
        else:
            ink = min(candidates, key=lambda item: _luminance(item[1]))[1]

    return {
        "bg": bg,
        "surface": surface,
        "primary": primary,
        "accent": accent,
        "ink": ink,
        "dark": dark,
    }


def _layout_label(family: Mapping[str, Any]) -> str:
    stage = str(family.get("stage") or "content")
    if stage in _STAGE_LABELS:
        return _STAGE_LABELS[stage]

    pattern = str(family.get("layout_pattern") or "title_body")
    modalities = family.get("modalities", [])
    kinds = {
        str(item)
        for item in modalities
        if isinstance(item, str)
    } if isinstance(modalities, list) else set()

    if "chart" in kinds or "metric" in kinds:
        return "数据图表"
    if "table" in kinds:
        return "数据表格"
    if pattern == "split":
        return "图文分栏" if "image" in kinds else "左右分栏"
    if pattern == "columns":
        return "多栏正文"
    if pattern in _PATTERN_LABELS:
        return _PATTERN_LABELS[pattern]
    if "image" in kinds and "text" in kinds:
        return "图文页"
    if "image" in kinds:
        return "图片页"
    return "标题 + 正文"


def _layouts(families: Iterable[Mapping[str, Any]]) -> list[str]:
    result: list[str] = []
    for family in families:
        label = _layout_label(family)
        if label not in result:
            result.append(label)
    return result


def compiled_template_summary(
    templates_root: Path,
    template_id: str,
    *,
    owner: str,
    description: str,
    name: str | None = None,
) -> dict[str, Any]:
    """Build a frontend summary exclusively from one READY Template IR."""

    revision = TemplateStore(templates_root).resolve(template_id)
    manifest = revision.manifest
    canvas = revision.metadata.get("canvas", {})
    ratio = manifest.get("aspect_ratio") or canvas.get("aspect_ratio")
    if ratio not in SUPPORTED_ASPECT_RATIOS:
        raise TemplateStoreError(
            f"Template {template_id} uses unsupported canvas ratio {ratio!r}"
        )
    return {
        "id": template_id,
        "name": name or manifest.get("name") or template_id,
        "description": description,
        "owner": owner,
        "status": "ready",
        "progress": 100,
        "error": None,
        "slides": manifest.get("slide_count") or revision.metadata.get("slide_count", 0),
        "ratio": ratio,
        "layouts": _layouts(revision.load_families()),
        "palette": _palette(revision, revision.load_theme()),
        "revision_id": revision.revision_id,
        "thumbnail_url": (
            f"/api/templates/{template_id}/thumbnail"
            f"?revision_id={revision.revision_id}"
        ),
    }


def _template_summary(template_dir: Path) -> dict[str, Any]:
    description_path = template_dir / "description.txt"
    description = (
        description_path.read_text(encoding="utf-8").strip()
        if description_path.exists()
        else "后端内置模板"
    )
    return compiled_template_summary(
        _templates_dir(),
        template_dir.name,
        owner="system",
        description=description,
    )


@lru_cache(maxsize=1)
def _bundled_template_summaries() -> tuple[dict[str, Any], ...]:
    templates_dir = _templates_dir()
    if not templates_dir.exists():
        return ()
    summaries: list[dict[str, Any]] = []
    for template_dir in sorted(templates_dir.iterdir(), key=lambda path: path.name):
        if not template_dir.is_dir():
            continue
        try:
            summaries.append(_template_summary(template_dir))
        except (OSError, UnicodeError, TemplateStoreError):
            continue
    return tuple(summaries)


def bundled_template_summaries() -> list[dict[str, Any]]:
    """Return backend-bundled templates, excluding any frontend demo data."""

    return deepcopy(list(_bundled_template_summaries()))


def bundled_template_summary(template_id: str) -> dict[str, Any] | None:
    """Return one bundled summary using the exact same list endpoint contract."""

    summary = next(
        (item for item in _bundled_template_summaries() if item["id"] == template_id),
        None,
    )
    return deepcopy(summary) if summary is not None else None


def is_bundled_template(template_id: str) -> bool:
    """Return whether ``template_id`` maps to a READY bundled Template IR."""

    return any(item["id"] == template_id for item in _bundled_template_summaries())


def bundled_template_ids() -> set[str]:
    """Return identifiers reserved by real backend-bundled templates."""

    return {str(item["id"]) for item in _bundled_template_summaries()}
