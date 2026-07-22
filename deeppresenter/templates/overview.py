"""Compile a template's overview into an index tier and a detail tier.

The overview is a pure projection of an immutable revision, so it is built
once at compile time rather than re-derived per task. Splitting it in two
replaces lossy truncation with progressive disclosure: the index is small
enough to stay pinned for any template size, and a family's full guidance is
fetched only when the model is choosing that family.
"""

from __future__ import annotations

from typing import Any

from .models import (
    Asset,
    Canvas,
    LayoutFamily,
    Semantic,
    SlideIndexEntry,
    Theme,
)

OVERVIEW_INDEX_PATH = "overview/index.json"


def family_detail_path(family_id: str) -> str:
    """Path of one family's detail document inside a revision."""

    return f"overview/families/{family_id}.json"


def build_index(
    *,
    template_id: str,
    revision_id: str,
    schema_version: str,
    name: str,
    canvas: Canvas,
    theme: Theme,
    families: list[LayoutFamily],
    slides: list[SlideIndexEntry],
    assets: list[Asset],
) -> dict[str, Any]:
    """Build the always-pinned tier.

    Every field here is bounded by the template's structure rather than its
    prose: one short line per family, so the index grows linearly and stays
    readable for a template of any size.
    """

    role_counts: dict[str, int] = {}
    for asset in assets:
        role_counts[asset.role.value] = role_counts.get(asset.role.value, 0) + 1
    return {
        "template_id": template_id,
        "revision_id": revision_id,
        "schema_version": schema_version,
        "name": name,
        "canvas": canvas.model_dump(mode="json"),
        "slide_count": len(slides),
        "family_count": len(families),
        "asset_roles": role_counts,
        "theme": {
            "colors": {color.token: color.value for color in theme.colors},
            "fonts": {font.token: font.family for font in theme.fonts},
            "safe_area": theme.safe_area.model_dump(mode="json"),
        },
        "families": [
            {
                "family_id": family.family_id,
                "stage": family.stage.value,
                "layout_pattern": family.layout_pattern.value,
                "modalities": [kind.value for kind in family.modalities],
                "slide_count": len(family.slide_ids),
                "digest": family.digest or family.name,
            }
            for family in families
        ],
        "detail_hint": (
            "Each family lists only its digest. Call get_family_detail(family_id) "
            "for selection hints, avoid conditions, and member pages."
        ),
    }


def build_family_detail(
    family: LayoutFamily,
    semantics: dict[str, Semantic],
) -> dict[str, Any]:
    """Build the on-demand tier for one family."""

    members = [
        {
            "slide_id": slide_id,
            "page_number": semantics[slide_id].page_number,
            "digest": semantics[slide_id].page_semantics.digest,
            "summary": semantics[slide_id].page_semantics.summary,
            "density": semantics[slide_id].page_semantics.density.value,
            "region_roles": sorted(
                {
                    region.role.value
                    for region in semantics[slide_id].regions
                }
            ),
        }
        for slide_id in family.slide_ids
        if slide_id in semantics
    ]
    return {
        "family_id": family.family_id,
        "name": family.name,
        "stage": family.stage.value,
        "layout_pattern": family.layout_pattern.value,
        "modalities": [kind.value for kind in family.modalities],
        "digest": family.digest or family.name,
        "description": family.description,
        "selection_hints": family.selection_hints,
        "avoid_when": family.avoid_when,
        "representative_slide_id": family.representative_slide_id,
        "slides": members,
    }
