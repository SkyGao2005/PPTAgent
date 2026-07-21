"""Discover presentation templates that are actually available to the backend."""

from __future__ import annotations

import json
import re
import zipfile
from copy import deepcopy
from functools import lru_cache
from importlib.util import find_spec
from pathlib import Path
from typing import Any
from xml.etree import ElementTree


_SLIDE_PATH = re.compile(r"^ppt/slides/slide\d+\.xml$")
_PRESENTATION_NAMESPACE = "http://schemas.openxmlformats.org/presentationml/2006/main"
_DEFAULT_PALETTE = {
    "bg": "#F4F6F8",
    "surface": "#FFFFFF",
    "primary": "#38506B",
    "accent": "#7FA6C9",
    "ink": "#22303E",
    "dark": False,
}


def _templates_dir() -> Path:
    spec = find_spec("pptagent")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError("pptagent package templates are not available")
    return Path(next(iter(spec.submodule_search_locations))) / "templates"


def _read_json(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data if isinstance(data, dict) else {}


def _presentation_metadata(source: Path) -> tuple[int, str]:
    """Read slide count and aspect ratio directly from the PPTX archive."""
    with zipfile.ZipFile(source) as archive:
        slide_count = sum(1 for name in archive.namelist() if _SLIDE_PATH.match(name))
        root = ElementTree.fromstring(archive.read("ppt/presentation.xml"))
    slide_size = root.find(f"{{{_PRESENTATION_NAMESPACE}}}sldSz")
    if slide_size is None:
        return slide_count, "16:9"
    width = int(slide_size.attrib.get("cx", 0))
    height = int(slide_size.attrib.get("cy", 0))
    if not width or not height:
        return slide_count, "16:9"
    ratio = width / height
    aspect_ratio = "16:9" if abs(ratio - 16 / 9) < abs(ratio - 4 / 3) else "4:3"
    return slide_count, aspect_ratio


def _layout_names(template_dir: Path) -> list[str]:
    induction = _read_json(template_dir / "slide_induction.json")
    return [
        name
        for name, value in induction.items()
        if isinstance(value, dict) and "template_id" in value
    ]


def _normalize_color(color: Any, fallback: str) -> str:
    if not isinstance(color, str) or not color.strip():
        return fallback
    value = color.strip()
    return value if value.startswith("#") else f"#{value}"


def _template_summary(template_dir: Path) -> dict[str, Any]:
    manifest = _read_json(template_dir / "manifest.json")
    source = template_dir / "source.pptx"
    slide_count, aspect_ratio = _presentation_metadata(source)
    description_path = template_dir / "description.txt"
    description = (
        description_path.read_text(encoding="utf-8").strip()
        if description_path.exists()
        else "后端内置模板"
    )
    primary = _normalize_color(
        manifest.get("primary_color"),
        _DEFAULT_PALETTE["primary"],
    )
    palette = {**_DEFAULT_PALETTE, "primary": primary, "accent": primary}
    return {
        "id": template_dir.name,
        "name": manifest.get("name") or template_dir.name,
        "description": description,
        "owner": "system",
        "status": "ready",
        "slides": manifest.get("slide_count") or slide_count,
        "ratio": manifest.get("aspect_ratio") or aspect_ratio,
        "layouts": _layout_names(template_dir),
        "palette": palette,
    }


@lru_cache(maxsize=1)
def _bundled_template_summaries() -> tuple[dict[str, Any], ...]:
    templates_dir = _templates_dir()
    if not templates_dir.exists():
        return ()
    return tuple(
        _template_summary(template_dir)
        for template_dir in sorted(templates_dir.iterdir(), key=lambda path: path.name)
        if template_dir.is_dir() and (template_dir / "source.pptx").is_file()
    )


def bundled_template_summaries() -> list[dict[str, Any]]:
    """Return backend-bundled templates, excluding any frontend demo data."""
    return deepcopy(list(_bundled_template_summaries()))


def is_bundled_template(template_id: str) -> bool:
    """Return whether ``template_id`` maps to a usable bundled PPTX."""
    return any(item["id"] == template_id for item in _bundled_template_summaries())


def bundled_template_ids() -> set[str]:
    """Return identifiers reserved by real backend-bundled templates."""
    return {str(item["id"]) for item in _bundled_template_summaries()}
