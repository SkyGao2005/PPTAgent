"""Feature slice extraction and storage for individual slides.

Each ``FeatureSlice`` captures the structured style parameters of one
slide — colors, fonts, layout — as a queryable, versioned record.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pptagent.editor.snapshot import SlideSnapshot, PresentationSnapshot


# ── feature data types ───────────────────────────────────────

@dataclass
class ColorInfo:
    """Detected colors on a slide."""
    dominant_colors: list[str] = field(default_factory=list)   # top 3-5 colours used
    text_colors: list[str] = field(default_factory=list)
    title_color: str | None = None
    body_color: str | None = None

    def to_dict(self) -> dict:
        return {
            "dominant_colors": self.dominant_colors,
            "text_colors": self.text_colors,
            "title_color": self.title_color,
            "body_color": self.body_color,
        }


@dataclass
class FontInfo:
    """Font usage on a slide."""
    families: list[str] = field(default_factory=list)   # font names found
    title_size: int | None = None
    body_size: int | None = None
    min_size: int | None = None
    max_size: int | None = None
    bold_count: int = 0

    def to_dict(self) -> dict:
        return {
            "families": self.families,
            "title_size": self.title_size,
            "body_size": self.body_size,
            "min_size": self.min_size,
            "max_size": self.max_size,
            "bold_count": self.bold_count,
        }


@dataclass
class LayoutInfo:
    """Layout structure of a slide."""
    layout_type: str = ""               # "text" | "image" | "mixed"
    element_count: int = 0
    text_elements: int = 0
    image_elements: int = 0
    has_title: bool = False
    has_subtitle: bool = False
    has_bullets: bool = False
    element_names: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "layout_type": self.layout_type,
            "element_count": self.element_count,
            "text_elements": self.text_elements,
            "image_elements": self.image_elements,
            "has_title": self.has_title,
            "has_subtitle": self.has_subtitle,
            "has_bullets": self.has_bullets,
            "element_names": self.element_names,
        }


# ── feature slice ────────────────────────────────────────────

@dataclass
class FeatureSlice:
    """Structured feature record for a single slide at a point in time.

    Independent of any specific template — purely descriptive.
    Can be stored, queried, and compared across versions.
    """

    slide_idx: int
    version_id: str
    layout_name: str | None
    slide_title: str | None
    colors: ColorInfo = field(default_factory=ColorInfo)
    fonts: FontInfo = field(default_factory=FontInfo)
    layout: LayoutInfo = field(default_factory=LayoutInfo)
    checksum: str = ""

    @classmethod
    def extract(cls, snapshot: SlideSnapshot, version_id: str = "") -> "FeatureSlice":
        """Extract features from a SlideSnapshot."""
        colors = ColorInfo()
        fonts = FontInfo()
        layout = LayoutInfo(
            element_count=len(snapshot.shapes_data),
        )

        all_sizes = []

        for shape in snapshot.shapes_data:
            el_name: str = shape.get("element_name", "").lower()
            layout.element_names.append(shape.get("element_name", ""))

            # Detect element type
            if "title" in el_name:
                layout.has_title = True
            if "subtitle" in el_name or "sub" in el_name:
                layout.has_subtitle = True
            if "bullet" in el_name or "list" in el_name:
                layout.has_bullets = True

            # Images
            if shape.get("images"):
                layout.image_elements += 1

            # Text
            paras: list[dict] = shape.get("paragraphs", [])
            if paras:
                layout.text_elements += 1

            # --- extract font details from shape data ---
            # Note: shape_data dicts don't directly carry font info
            # after snapshot conversion. We'll derive what we can
            # from element names and paragraph counts.

            # --- color extraction (from element type heuristics) ---
            # After a restyle() the name-based heuristics still work
            # because we track element roles, not raw hex values here.
            if "title" in el_name:
                layout.has_title = True
                # Title color typically = first paragraph's color metadata
                # But since we don't have raw color in shapes_data,
                # we mark for later enrichment

        # Determine layout type
        if layout.image_elements > 0 and layout.text_elements > 0:
            layout.layout_type = "mixed"
        elif layout.image_elements > 0:
            layout.layout_type = "image"
        else:
            layout.layout_type = "text"

        return cls(
            slide_idx=snapshot.slide_idx,
            version_id=version_id,
            layout_name=snapshot.layout_name,
            slide_title=snapshot.slide_title,
            colors=colors,
            fonts=fonts,
            layout=layout,
            checksum=snapshot.checksum,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "slide_idx": self.slide_idx,
            "version_id": self.version_id,
            "layout_name": self.layout_name,
            "slide_title": self.slide_title,
            "colors": self.colors.to_dict(),
            "fonts": self.fonts.to_dict(),
            "layout": self.layout.to_dict(),
            "checksum": self.checksum,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "FeatureSlice":
        return cls(
            slide_idx=data["slide_idx"],
            version_id=data.get("version_id", ""),
            layout_name=data.get("layout_name"),
            slide_title=data.get("slide_title"),
            colors=ColorInfo(**data.get("colors", {})),
            fonts=FontInfo(**data.get("fonts", {})),
            layout=LayoutInfo(**data.get("layout", {})),
            checksum=data.get("checksum", ""),
        )


# ── feature store ────────────────────────────────────────────

class FeatureStore:
    """Persistent storage for FeatureSlice records.

    Directory layout::

        .features/
        ├── index.json          # maps version_id -> list of slide indices
        └── <version_id>/
            └── slide_<idx>.json
    """

    def __init__(self, storage_dir: Path | str):
        if isinstance(storage_dir, str):
            storage_dir = Path(storage_dir)
        self._dir = storage_dir
        self._index_file = self._dir / "index.json"
        self._dir.mkdir(parents=True, exist_ok=True)

    # ── save ──────────────────────────────────────────────────

    def save_slice(self, feature: FeatureSlice) -> None:
        """Persist a single feature slice."""
        slice_dir = self._dir / feature.version_id
        slice_dir.mkdir(parents=True, exist_ok=True)
        filepath = slice_dir / f"slide_{feature.slide_idx:02d}.json"
        with open(filepath, "w", encoding="utf-8") as f:
            json.dump(feature.to_dict(), f, ensure_ascii=False, indent=2)

    def save_version(self, snapshot: PresentationSnapshot, version_id: str) -> list[FeatureSlice]:
        """Extract and save features for every slide in a version snapshot."""
        slices = []
        for snap in snapshot.slides:
            fs = FeatureSlice.extract(snap, version_id=version_id)
            slices.append(fs)
            self.save_slice(fs)

        # Update index
        index = self._read_index()
        entry = {"version_id": version_id, "slide_count": len(slices)}
        index = [e for e in index if e["version_id"] != version_id]
        index.append(entry)
        self._write_index(index)
        return slices

    # ── query ─────────────────────────────────────────────────

    def get_slice(self, version_id: str, slide_idx: int) -> FeatureSlice | None:
        """Load a specific feature slice."""
        filepath = self._dir / version_id / f"slide_{slide_idx:02d}.json"
        if not filepath.exists():
            return None
        with open(filepath, "r", encoding="utf-8") as f:
            return FeatureSlice.from_dict(json.load(f))

    def get_version_slices(self, version_id: str) -> list[FeatureSlice]:
        """Load all feature slices for a version."""
        slice_dir = self._dir / version_id
        if not slice_dir.exists():
            return []
        slices = []
        for f in sorted(slice_dir.glob("slide_*.json")):
            with open(f, "r", encoding="utf-8") as fp:
                slices.append(FeatureSlice.from_dict(json.load(fp)))
        return slices

    def list_versions(self) -> list[dict[str, Any]]:
        return self._read_index()

    # ── cross-version query ───────────────────────────────────

    def find_by_layout(self, layout_type: str) -> list[dict[str, Any]]:
        """Find all slides with a specific layout type across all versions."""
        results = []
        for entry in self._read_index():
            for fs in self.get_version_slices(entry["version_id"]):
                if fs.layout.layout_type == layout_type:
                    results.append(fs.to_dict())
        return results

    def find_by_element(self, element_name: str) -> list[dict[str, Any]]:
        """Find all slides containing a named element."""
        results = []
        for entry in self._read_index():
            for fs in self.get_version_slices(entry["version_id"]):
                if element_name in fs.layout.element_names:
                    results.append(fs.to_dict())
        return results

    # ── helpers ───────────────────────────────────────────────

    def _read_index(self) -> list[dict[str, Any]]:
        if not self._index_file.exists():
            return []
        with open(self._index_file, "r", encoding="utf-8") as f:
            return json.load(f)

    def _write_index(self, index: list[dict[str, Any]]) -> None:
        with open(self._index_file, "w", encoding="utf-8") as f:
            json.dump(index, f, ensure_ascii=False, indent=2)
