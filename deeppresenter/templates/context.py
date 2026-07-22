"""Bounded template-context retrieval for HTML slide generation."""

from __future__ import annotations

import json
import math
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from deeppresenter.templates.store import (
    JsonObject,
    TemplateStore,
)
from deeppresenter.templates.overview import OVERVIEW_INDEX_PATH, family_detail_path
from deeppresenter.utils.config import ContextBudgetConfig, chars_for_tokens


# Model-facing responses must not grow with the size of a template revision.
# The token budgets in ContextBudgetConfig are authoritative; the character
# bounds used while serializing are derived from them so the two can never
# drift apart.
_DEFAULT_BUDGET = ContextBudgetConfig()
DEFAULT_OVERVIEW_CHARS = chars_for_tokens(_DEFAULT_BUDGET.template_overview_max_tokens)
DEFAULT_SEARCH_RESULT_CHARS = chars_for_tokens(
    _DEFAULT_BUDGET.template_search_result_max_tokens
)
DEFAULT_REFERENCE_CHARS = chars_for_tokens(
    _DEFAULT_BUDGET.template_reference_max_tokens
)
MAX_REFERENCE_RESULTS = 2

_WORD_RE = re.compile(r"[\w\u4e00-\u9fff]+", re.UNICODE)


def _normalized(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower().replace("-", "_").replace(" ", "_")


def _string_set(value: Any) -> set[str]:
    if isinstance(value, str):
        values: Iterable[Any] = [value]
    elif isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        values = value
    else:
        return set()
    return {normalized for item in values if (normalized := _normalized(item))}


def _tokens(value: Any) -> set[str]:
    if value is None:
        return set()
    if not isinstance(value, str):
        value = json.dumps(value, ensure_ascii=False, sort_keys=True)
    return {_normalized(match.group(0)) for match in _WORD_RE.finditer(value)}


def _serialized_chars(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


def _clip_value(
    value: Any,
    *,
    string_limit: int,
    list_limit: int,
    depth: int = 0,
) -> Any:
    if depth >= 6:
        return None
    if isinstance(value, str):
        if len(value) <= string_limit:
            return value
        return value[: max(1, string_limit - 1)] + "…"
    if isinstance(value, Mapping):
        return {
            str(key): _clip_value(
                item,
                string_limit=string_limit,
                list_limit=list_limit,
                depth=depth + 1,
            )
            for key, item in list(value.items())[:24]
        }
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
        return [
            _clip_value(
                item,
                string_limit=string_limit,
                list_limit=list_limit,
                depth=depth + 1,
            )
            for item in list(value)[:list_limit]
        ]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return _clip_value(
        str(value),
        string_limit=string_limit,
        list_limit=list_limit,
        depth=depth + 1,
    )


def _bounded_object(
    payload: Mapping[str, Any],
    max_chars: int,
    identity_keys: tuple[str, ...],
    drop_order: tuple[str, ...] = (),
) -> JsonObject:
    """Fit a projection inside ``max_chars`` losing as little meaning as possible.

    ``drop_order`` lists whole fields least-useful-first. Removing them is
    tried before any clipping, because complete information about fewer things
    beats truncated information about everything -- a region whose bbox has
    been clipped to three numbers is worse than no region at all.
    """

    if max_chars < 256:
        raise ValueError("max_chars must be at least 256")
    raw = dict(payload)
    if _serialized_chars(raw) <= max_chars:
        return raw

    reduced = dict(raw)
    for key in drop_order:
        if reduced.pop(key, None) is None:
            continue
        reduced["truncated"] = True
        if _serialized_chars(reduced) <= max_chars:
            return reduced

    for string_limit, list_limit in ((256, 12), (160, 8), (96, 5), (48, 3)):
        clipped = _clip_value(
            reduced,
            string_limit=string_limit,
            list_limit=list_limit,
        )
        if isinstance(clipped, dict):
            clipped["truncated"] = True
            if _serialized_chars(clipped) <= max_chars:
                return clipped

    fallback: JsonObject = {
        key: raw[key]
        for key in identity_keys
        if key in raw and isinstance(raw[key], (str, int, float, bool, type(None)))
    }
    fallback["truncated"] = True
    fallback["notice"] = "Context omitted to satisfy the requested response bound."
    if _serialized_chars(fallback) > max_chars:
        raise ValueError("max_chars is too small for the payload identity")
    return fallback


# Least useful first. Everything above these -- regions, asset bindings, the
# scaffold path, and the reference images -- is what makes a page reproducible.
REFERENCE_DROP_ORDER = (
    "avoid_when",
    "selection_hints",
    "summary",
    "is_representative",
    "family_ids",
    "page_semantics",
)


@dataclass(frozen=True, slots=True)
class SlideNeed:
    """Semantic requirements used to retrieve visual template references."""

    slide_key: str | None = None
    stage: str | None = None
    layout_pattern: str | None = None
    message_pattern: str | None = None
    modalities: tuple[str, ...] = ()
    density: str | None = None
    region_roles: tuple[str, ...] = ()
    keywords: tuple[str, ...] = ()
    family_ids: tuple[str, ...] = ()
    exclude_slide_ids: tuple[str, ...] = ()

    @classmethod
    def from_value(cls, value: "SlideNeed | Mapping[str, Any]") -> "SlideNeed":
        """Normalize an API mapping without allowing arbitrary fields."""

        if isinstance(value, cls):
            return value
        if not isinstance(value, Mapping):
            raise TypeError("slide need must be SlideNeed or a mapping")

        page = value.get("page_semantics")
        page_semantics = page if isinstance(page, Mapping) else {}

        def scalar(*keys: str) -> str | None:
            for key in keys:
                item = value.get(key, page_semantics.get(key))
                if isinstance(item, str) and item.strip():
                    return item
            return None

        def sequence(*keys: str) -> tuple[str, ...]:
            for key in keys:
                item = value.get(key, page_semantics.get(key))
                values = _string_set(item)
                if values:
                    return tuple(sorted(values))
            return ()

        return cls(
            slide_key=scalar("slide_key", "request_id", "id"),
            stage=scalar("stage", "page_type"),
            layout_pattern=scalar("layout_pattern", "layout"),
            message_pattern=scalar("message_pattern", "message_type"),
            modalities=sequence("modalities", "modality"),
            density=scalar("density"),
            region_roles=sequence("region_roles", "roles", "required_roles"),
            keywords=sequence("keywords", "topics"),
            family_ids=sequence("family_ids", "preferred_family_ids"),
            exclude_slide_ids=sequence("exclude_slide_ids"),
        )

    def as_dict(self) -> JsonObject:
        """Serialize the normalized need for snapshots."""

        return asdict(self)


@dataclass(frozen=True, slots=True)
class ReferenceMatch:
    """One scored reference-slide match."""

    slide_id: str
    page_number: int
    score: float
    reasons: tuple[str, ...] = field(default_factory=tuple)
    family_ids: tuple[str, ...] = field(default_factory=tuple)
    page_semantics: JsonObject = field(default_factory=dict)

    def as_dict(self) -> JsonObject:
        """Serialize the match using JSON-friendly collections."""

        value = asdict(self)
        value["score"] = round(self.score, 3)
        return value


def _page_semantics(
    semantic: Mapping[str, Any], record: Mapping[str, Any]
) -> JsonObject:
    value = semantic.get("page_semantics")
    page = dict(value) if isinstance(value, Mapping) else {}
    aliases = {
        "stage": ("stage", "page_type", "category"),
        "layout_pattern": ("layout_pattern", "layout", "composition"),
        "message_pattern": ("message_pattern", "message_type"),
        "modalities": ("modalities", "modality"),
        "density": ("density",),
    }
    for target, keys in aliases.items():
        if target in page:
            continue
        for source in (semantic, record):
            found = next(
                (source.get(key) for key in keys if source.get(key) is not None), None
            )
            if found is not None:
                page[target] = found
                break
    return page


def _region_roles(semantic: Mapping[str, Any]) -> set[str]:
    regions = semantic.get("regions")
    if not isinstance(regions, list):
        return set()
    roles: set[str] = set()
    for region in regions:
        if not isinstance(region, Mapping):
            continue
        role = _normalized(region.get("role") or region.get("kind"))
        if role:
            roles.add(role)
    return roles


def _family_ids(record: Mapping[str, Any], semantic: Mapping[str, Any]) -> set[str]:
    return _string_set(record.get("family_ids")) | _string_set(
        semantic.get("family_ids")
    )


def _score_reference(
    need: SlideNeed,
    record: Mapping[str, Any],
    semantic: Mapping[str, Any],
) -> ReferenceMatch:
    page = _page_semantics(semantic, record)
    reasons: list[str] = []
    score = 0.0

    exact_fields = (
        ("stage", need.stage, 36.0),
        ("layout_pattern", need.layout_pattern, 26.0),
        ("message_pattern", need.message_pattern, 18.0),
        ("density", need.density, 6.0),
    )
    for field_name, wanted, weight in exact_fields:
        if not wanted:
            continue
        actual = _normalized(page.get(field_name))
        if actual == _normalized(wanted):
            score += weight
            reasons.append(f"{field_name}:exact")
        elif actual:
            score -= weight * (0.45 if field_name == "stage" else 0.15)

    wanted_modalities = _string_set(need.modalities)
    actual_modalities = _string_set(page.get("modalities"))
    if wanted_modalities:
        overlap = wanted_modalities & actual_modalities
        score += 18.0 * len(overlap) / len(wanted_modalities)
        if overlap:
            reasons.append("modalities:" + ",".join(sorted(overlap)))
        score -= (
            6.0 * len(wanted_modalities - actual_modalities) / len(wanted_modalities)
        )

    wanted_roles = _string_set(need.region_roles)
    actual_roles = _region_roles(semantic)
    if wanted_roles:
        coverage = len(wanted_roles & actual_roles) / len(wanted_roles)
        score += 14.0 * coverage
        score -= 5.0 * (1.0 - coverage)
        if coverage:
            reasons.append(f"roles:{coverage:.0%}")

    actual_families = _family_ids(record, semantic)
    wanted_families = _string_set(need.family_ids)
    family_overlap = actual_families & wanted_families
    if family_overlap:
        score += 22.0
        reasons.append("family:" + ",".join(sorted(family_overlap)))

    wanted_keywords = _string_set(need.keywords)
    if wanted_keywords:
        searchable = _tokens(
            {
                "semantic": semantic.get("selection_hints", semantic),
                "record": record.get("summary", ""),
            }
        )
        keyword_overlap = wanted_keywords & searchable
        score += min(8.0, 2.0 * len(keyword_overlap))
        if keyword_overlap:
            reasons.append("keywords:" + ",".join(sorted(keyword_overlap)))

    if (
        record.get("is_representative") is True
        or semantic.get("is_representative") is True
    ):
        score += 1.0

    slide_id = str(record["slide_id"])
    raw_page_number = record.get("page_number", record.get("index", 0))
    try:
        page_number = int(raw_page_number)
    except (TypeError, ValueError):
        page_number = 0
    return ReferenceMatch(
        slide_id=slide_id,
        page_number=page_number,
        score=score,
        reasons=tuple(reasons),
        family_ids=tuple(sorted(actual_families)),
        page_semantics=page,
    )


class TemplateContextProvider:
    """Retrieve and materialize small, revision-pinned model contexts."""

    def __init__(
        self,
        store: TemplateStore,
        budget: ContextBudgetConfig | None = None,
    ):
        """``budget`` lets a caller honour the configured model's limits.

        Without one the shared defaults apply, so every entry point stays
        bounded by the same numbers.
        """

        self.store = store
        budget = budget or _DEFAULT_BUDGET
        self.overview_chars = chars_for_tokens(budget.template_overview_max_tokens)
        self.search_result_chars = chars_for_tokens(
            budget.template_search_result_max_tokens
        )
        self.reference_chars = chars_for_tokens(budget.template_reference_max_tokens)

    def get_overview(
        self,
        template_id: str,
        revision_id: str | None = None,
        *,
        max_chars: int | None = None,
    ) -> JsonObject:
        """Return a compact template overview bounded by serialized chars."""

        max_chars = max_chars or self.overview_chars
        revision = self.store.resolve(template_id, revision_id)
        compiled = self._compiled_overview_index(revision)
        if compiled is not None:
            # The index tier is sized by template structure, not prose, so it
            # normally passes through untouched.
            return _bounded_object(
                compiled,
                max_chars=max_chars,
                identity_keys=("template_id", "revision_id", "schema_version"),
            )
        theme = revision.load_theme()
        families = revision.load_families()
        slides = revision.load_slide_index()
        assets = revision.load_assets()
        role_counts: dict[str, int] = {}
        for asset in assets:
            role = _normalized(asset.get("role") or asset.get("kind") or "other")
            role_counts[role] = role_counts.get(role, 0) + 1

        payload: JsonObject = {
            "template_id": revision.template_id,
            "revision_id": revision.revision_id,
            "schema_version": revision.schema_version,
            "name": revision.manifest.get("name", revision.template_id),
            "canvas": revision.metadata.get("canvas", theme.get("canvas")),
            "slide_count": len(slides),
            "family_count": len(families),
            "asset_roles": role_counts,
            "theme": {
                key: theme[key]
                for key in (
                    "colors",
                    "color_tokens",
                    "fonts",
                    "typography",
                    "spacing",
                    "safe_area",
                    "brand_invariants",
                )
                if key in theme
            },
            "families": [
                {
                    key: family[key]
                    for key in (
                        "family_id",
                        "name",
                        "stage",
                        "layout_pattern",
                        "message_pattern",
                        "modalities",
                        "selection_hints",
                        "avoid_when",
                    )
                    if key in family
                }
                for family in families
            ],
        }
        return _bounded_object(
            payload,
            max_chars=max_chars,
            identity_keys=("template_id", "revision_id", "schema_version"),
        )

    @staticmethod
    def _compiled_overview_index(revision: Any) -> JsonObject | None:
        """Load the compile-time index tier, if this revision has one.

        Revisions built before the layered overview fall back to projecting
        it at read time.
        """

        relative = revision.metadata.get("overview_index_path") or OVERVIEW_INDEX_PATH
        if not (revision.root / relative).is_file():
            return None
        return json.loads(revision.resolve_path(relative).read_text(encoding="utf-8"))

    def get_family_detail(
        self,
        template_id: str,
        family_id: str,
        revision_id: str | None = None,
        *,
        max_chars: int | None = None,
    ) -> JsonObject:
        """Expand one family: selection hints, avoid conditions, member pages."""

        max_chars = max_chars or self.reference_chars
        revision = self.store.resolve(template_id, revision_id)
        relative = family_detail_path(family_id)
        if not (revision.root / relative).is_file():
            known = [
                str(family.get("family_id"))
                for family in revision.load_families()
                if family.get("family_id")
            ]
            raise KeyError(
                f"Unknown family {family_id!r}; available families: {known}"
            )
        payload = json.loads(
            revision.resolve_path(relative).read_text(encoding="utf-8")
        )
        return _bounded_object(
            payload,
            max_chars=max_chars,
            identity_keys=("family_id", "stage", "layout_pattern"),
        )

    def render_overview_markdown(
        self,
        template_id: str,
        revision_id: str | None = None,
        *,
        max_chars: int | None = None,
    ) -> str:
        """Render the bounded overview as model-readable Markdown."""

        max_chars = max_chars or self.overview_chars
        overview = self.get_overview(
            template_id,
            revision_id,
            max_chars=max_chars,
        )
        text = (
            "# Template context\n\n```json\n"
            + json.dumps(
                overview,
                ensure_ascii=False,
                indent=2,
            )
            + "\n```\n"
        )
        if len(text) <= max_chars:
            return text
        compact = json.dumps(overview, ensure_ascii=False, separators=(",", ":"))
        text = "# Template context\n\n```json\n" + compact + "\n```\n"
        if len(text) <= max_chars:
            return text
        # Account for Markdown overhead by asking the object projector for a
        # smaller payload.  The minimum remains comfortably above identities.
        reduced = self.get_overview(
            template_id,
            revision_id,
            max_chars=max(256, max_chars - 50),
        )
        rendered = (
            "# Template context\n\n```json\n"
            + json.dumps(reduced, ensure_ascii=False, separators=(",", ":"))
            + "\n```\n"
        )
        if len(rendered) > max_chars:
            raise ValueError("Bounded template overview does not fit max_chars")
        return rendered

    def search_references(
        self,
        template_id: str,
        need: SlideNeed | Mapping[str, Any],
        revision_id: str | None = None,
        *,
        limit: int = MAX_REFERENCE_RESULTS,
        max_chars_per_result: int | None = None,
    ) -> list[JsonObject]:
        """Score and return at most two template slides for one need."""

        if not 1 <= limit <= MAX_REFERENCE_RESULTS:
            raise ValueError(f"limit must be between 1 and {MAX_REFERENCE_RESULTS}")
        max_chars_per_result = max_chars_per_result or self.search_result_chars
        normalized_need = SlideNeed.from_value(need)
        revision = self.store.resolve(template_id, revision_id)
        excluded = _string_set(normalized_need.exclude_slide_ids)
        matches: list[ReferenceMatch] = []
        for record in revision.load_slide_index():
            slide_id = str(record["slide_id"])
            if _normalized(slide_id) in excluded:
                continue
            semantic = revision.load_slide_semantic(slide_id)
            matches.append(_score_reference(normalized_need, record, semantic))

        matches.sort(
            key=lambda match: (
                -match.score,
                match.page_number if match.page_number > 0 else math.inf,
                match.slide_id,
            )
        )
        return [
            _bounded_object(
                match.as_dict(),
                max_chars=max_chars_per_result,
                identity_keys=("slide_id", "page_number", "score"),
            )
            for match in matches[:limit]
        ]

    def get_reference(
        self,
        template_id: str,
        slide_id: str,
        revision_id: str | None = None,
        *,
        max_chars: int | None = None,
    ) -> JsonObject:
        """Return one compact page context without embedding image bytes."""

        max_chars = max_chars or self.reference_chars
        revision = self.store.resolve(template_id, revision_id)
        record = revision.slide_record(slide_id)
        compact = revision.load_slide_compact(slide_id)
        semantic = revision.load_slide_semantic(slide_id)
        page = _page_semantics(semantic, record)
        regions = semantic.get("regions")
        compact_regions = compact.get("regions")
        if not isinstance(compact_regions, list) and isinstance(regions, list):
            compact_regions = [
                {
                    key: region[key]
                    for key in (
                        "region_id",
                        "source_shape_ids",
                        "kind",
                        "role",
                        "bbox",
                        "reading_order",
                        "capacity",
                        "behavior",
                        "style_ref",
                        "asset_refs",
                    )
                    if isinstance(region, Mapping) and key in region
                }
                for region in regions
                if isinstance(region, Mapping)
            ]

        image_paths: JsonObject = {}
        for kind in ("style_reference", "reference", "overlay"):
            relative = revision.slide_artifact_path(record, kind)
            candidate = revision.root / relative
            if candidate.exists():
                revision.resolve_path(relative)
                image_paths[kind] = relative

        layout_css = compact.get("layout_css_path") or record.get("layout_css_path")
        if isinstance(layout_css, str) and (revision.root / layout_css).exists():
            revision.resolve_path(layout_css)
        else:
            layout_css = None

        payload: JsonObject = {
            "template_id": revision.template_id,
            "revision_id": revision.revision_id,
            "slide_id": slide_id,
            "page_number": record.get("page_number"),
            "family_ids": sorted(_family_ids(record, semantic)),
            "is_representative": bool(
                record.get("is_representative") or semantic.get("is_representative")
            ),
            "summary": record.get("summary"),
            "page_semantics": page,
            "regions": compact_regions or [],
            "asset_refs": compact.get(
                "reusable_asset_ids",
                compact.get("asset_refs", semantic.get("asset_refs", [])),
            ),
            # Theme tokens are deliberately absent: theme.css carries them once
            # and every layout.css repeats them in its own :root. Copying them
            # per page spent a quarter of the budget on a third duplicate.
            "selection_hints": semantic.get("selection_hints", []),
            "avoid_when": semantic.get("avoid_when", []),
            "reference_files": image_paths,
            "layout_css": layout_css,
        }
        return _bounded_object(
            payload,
            max_chars=max_chars,
            # The scaffold path must survive clipping: without it the model
            # falls back to hand-written coordinates.
            identity_keys=(
                "template_id",
                "revision_id",
                "slide_id",
                "page_number",
                "layout_css",
            ),
            drop_order=REFERENCE_DROP_ORDER,
        )

    def materialize(
        self,
        template_id: str,
        task_workspace: str | Path,
        revision_id: str | None = None,
        slide_needs: (
            Sequence[SlideNeed | Mapping[str, Any]] | Mapping[str, Any] | None
        ) = None,
    ) -> JsonObject:
        """Materialize a task-local context pack.

        Importing lazily avoids a module cycle while keeping this method as the
        stable integration entrypoint requested by the task service.
        """

        from deeppresenter.templates.runtime import materialize_context_pack

        return materialize_context_pack(
            provider=self,
            template_id=template_id,
            task_workspace=Path(task_workspace),
            revision_id=revision_id,
            slide_needs=slide_needs,
        )
