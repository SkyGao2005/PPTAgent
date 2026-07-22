"""Task-local materialization of immutable template context packs."""

from __future__ import annotations

import hashlib
import json
import math
import os
import shutil
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from deeppresenter.templates.context import (
    DEFAULT_OVERVIEW_CHARS,
    DEFAULT_REFERENCE_CHARS,
    DEFAULT_SEARCH_RESULT_CHARS,
    MAX_REFERENCE_RESULTS,
    SlideNeed,
    TemplateContextProvider,
    _bounded_object,
    _score_reference,
    _string_set,
)
from deeppresenter.templates.store import (
    JsonObject,
    TemplateRevision,
    TemplateStore,
)


class TemplateContextConflictError(RuntimeError):
    """An existing task context was created for a different request."""


_REUSABLE_ASSET_ROLES = {
    "background",
    "background_image",
    "brand",
    "brand_mark",
    "decoration",
    "decorative",
    "decorative_image",
    "frame",
    "icon",
    "logo",
    "texture",
    "watermark",
}
_BLOCKED_REUSE_POLICIES = {
    "forbidden",
    "never",
    "no_reuse",
    "reference_only",
    "sample_only",
}


def _normalized(value: Any) -> str:
    return str(value or "").strip().lower().replace("-", "_").replace(" ", "_")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _canonical_json(value: Any) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _copy_artifact(source: Path, destination: Path) -> None:
    """Copy into the writable task without aliasing the immutable revision."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def _safe_filename(identifier: str, source: Path) -> str:
    suffix = source.suffix.lower()
    if not suffix or len(suffix) > 10:
        suffix = ".bin"
    return f"{identifier}{suffix}"


def _asset_role(asset: Mapping[str, Any]) -> str:
    return _normalized(asset.get("role") or asset.get("kind") or asset.get("type"))


def _asset_is_reusable(asset: Mapping[str, Any], referenced_ids: set[str]) -> bool:
    asset_id = str(asset.get("asset_id", ""))
    policy = _normalized(asset.get("reuse_policy") or asset.get("policy"))
    if policy in _BLOCKED_REUSE_POLICIES:
        return False
    role = _asset_role(asset)
    return (
        asset_id in referenced_ids
        or role in _REUSABLE_ASSET_ROLES
        or role.startswith("logo")
        or role.startswith("background")
        or role.startswith("decor")
    )


def _collect_asset_refs(reference: Mapping[str, Any]) -> set[str]:
    result: set[str] = set()
    for key in ("asset_refs", "asset_ids", "reusable_asset_ids"):
        values = reference.get(key, [])
        if isinstance(values, list):
            result.update(
                str(value) for value in values if isinstance(value, (str, int))
            )
    regions = reference.get("regions")
    if isinstance(regions, list):
        for region in regions:
            if not isinstance(region, Mapping):
                continue
            for key in ("asset_refs", "asset_ids"):
                values = region.get(key, [])
                if isinstance(values, list):
                    result.update(
                        str(value) for value in values if isinstance(value, (str, int))
                    )
            asset_id = region.get("asset_id")
            if isinstance(asset_id, (str, int)):
                result.add(str(asset_id))
    return result


def _normalize_needs(
    slide_needs: (Sequence[SlideNeed | Mapping[str, Any]] | Mapping[str, Any] | None),
) -> list[SlideNeed]:
    if slide_needs is None:
        return [SlideNeed(slide_key="default")]
    if isinstance(slide_needs, Mapping):
        nested = slide_needs.get("slides")
        if isinstance(nested, list):
            values: Sequence[SlideNeed | Mapping[str, Any]] = nested
        elif any(
            key in slide_needs
            for key in (
                "stage",
                "page_type",
                "layout_pattern",
                "layout",
                "page_semantics",
            )
        ):
            values = [slide_needs]
        else:
            mapped: list[Mapping[str, Any]] = []
            for slide_key, value in slide_needs.items():
                if not isinstance(value, Mapping):
                    raise TypeError("mapped slide needs must contain objects")
                item = dict(value)
                item.setdefault("slide_key", str(slide_key))
                mapped.append(item)
            values = mapped
    else:
        values = slide_needs
    normalized = [SlideNeed.from_value(value) for value in values]
    if not normalized:
        return [SlideNeed(slide_key="default")]
    return normalized


def _selection_payload(
    provider: TemplateContextProvider,
    revision: TemplateRevision,
    needs: Sequence[SlideNeed],
) -> JsonObject:
    selections: list[JsonObject] = []
    for index, need in enumerate(needs, start=1):
        slide_key = need.slide_key or f"slide_{index:04d}"
        references = provider.search_references(
            revision.template_id,
            need,
            revision.revision_id,
            limit=2,
        )
        selections.append(
            {
                "slide_key": slide_key,
                "need": need.as_dict(),
                "references": references,
            }
        )
    return {
        "template_id": revision.template_id,
        "revision_id": revision.revision_id,
        "slides": selections,
    }


def _materialize_reference(
    provider: TemplateContextProvider,
    revision: TemplateRevision,
    slide_id: str,
    destination: Path,
) -> tuple[JsonObject, set[str]]:
    context = provider.get_reference(
        revision.template_id,
        slide_id,
        revision.revision_id,
        max_chars=provider.reference_chars,
    )
    slide_dir = destination / "refs" / slide_id
    context_path = slide_dir / "context.compact.json"

    materialized_images: JsonObject = {}
    reference_files = context.get("reference_files")
    if isinstance(reference_files, Mapping):
        # A single style image per reference keeps the page-level context pack
        # bounded. Prefer the text-masked style reference over the source render.
        for kind in ("style_reference", "reference"):
            relative = reference_files.get(kind)
            if not isinstance(relative, str):
                continue
            source = revision.resolve_path(relative)
            target = slide_dir / f"{kind}{source.suffix.lower()}"
            _copy_artifact(source, target)
            materialized_images[kind] = target.relative_to(destination).as_posix()
            break

    # The layout scaffold is the mechanism that makes the template grid the
    # default, so it travels with every reference the model can retrieve.
    layout_css = context.get("layout_css")
    if isinstance(layout_css, str):
        source = revision.resolve_path(layout_css)
        target = slide_dir / "layout.css"
        _copy_artifact(source, target)
        context["layout_css"] = target.relative_to(destination).as_posix()

    # Paths exposed to generation are task-local.  Never retain a revision-root
    # path in a model-facing context pack, even though the source file was used
    # while creating the immutable snapshot.
    context["reference_files"] = materialized_images
    _write_json(context_path, context)

    entry: JsonObject = {
        "slide_id": slide_id,
        "context_path": context_path.relative_to(destination).as_posix(),
        "images": materialized_images,
    }
    return entry, _collect_asset_refs(context)


def _materialize_assets(
    revision: TemplateRevision,
    destination: Path,
    referenced_ids: set[str],
) -> list[JsonObject]:
    materialized: list[JsonObject] = []
    for asset in revision.load_assets():
        if not _asset_is_reusable(asset, referenced_ids):
            continue
        browser_path = asset.get("browser_path") or asset["path"]
        source = revision.resolve_path(str(browser_path))
        filename = _safe_filename(str(asset["asset_id"]), source)
        target = destination / "assets" / filename
        _copy_artifact(source, target)
        entry = {
            key: asset[key]
            for key in (
                "asset_id",
                "role",
                "reuse_policy",
                "alt_text",
                "tags",
            )
            if key in asset
        }
        entry["media_type"] = asset.get("browser_media_type") or asset.get(
            "media_type",
            "application/octet-stream",
        )
        entry["pixel_width"] = asset.get("browser_pixel_width") or asset.get(
            "pixel_width"
        )
        entry["pixel_height"] = asset.get("browser_pixel_height") or asset.get(
            "pixel_height"
        )
        if asset.get("browser_path") and asset.get("media_type") != entry["media_type"]:
            entry["source_media_type"] = asset.get("media_type")
        entry["path"] = target.relative_to(destination).as_posix()
        entry["sha256"] = _sha256(target)
        materialized.append(entry)
    _write_json(destination / "assets" / "index.json", {"assets": materialized})
    return materialized


def _materialize_theme(
    revision: TemplateRevision,
    destination: Path,
) -> JsonObject:
    result: JsonObject = {}
    for source_name, target_name in (
        ("theme/theme.json", "theme.json"),
        ("theme/theme.css", "theme.css"),
    ):
        source_candidate = revision.root / source_name
        if not source_candidate.exists():
            continue
        source = revision.resolve_path(source_name)
        target = destination / target_name
        _copy_artifact(source, target)
        result[target_name.removesuffix(target.suffix)] = target_name
    return result


def _materialized_hashes(destination: Path) -> dict[str, str]:
    result: dict[str, str] = {}
    for path in sorted(destination.rglob("*")):
        if path.is_file() and path.name != "snapshot.json":
            result[path.relative_to(destination).as_posix()] = _sha256(path)
    return result


def _existing_result(
    destination: Path,
    template_id: str,
    revision_id: str,
    request_hash: str,
) -> JsonObject | None:
    if not destination.exists():
        return None
    snapshot_path = destination / "snapshot.json"
    if not snapshot_path.exists():
        raise TemplateContextConflictError(
            f"Existing context directory has no snapshot: {destination}"
        )
    try:
        snapshot = json.loads(snapshot_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TemplateContextConflictError(
            f"Existing context snapshot is invalid: {snapshot_path}"
        ) from exc
    expected = (template_id, revision_id, request_hash)
    actual = (
        snapshot.get("template_id"),
        snapshot.get("revision_id"),
        snapshot.get("request_hash"),
    )
    if actual != expected:
        raise TemplateContextConflictError(
            "Task workspace already contains a different template context"
        )
    return _result_payload(destination, snapshot)


def _result_payload(destination: Path, snapshot: Mapping[str, Any]) -> JsonObject:
    reference_paths = {
        str(item["slide_id"]): str(destination / str(item["context_path"]))
        for item in snapshot.get("references", [])
        if isinstance(item, Mapping)
        and isinstance(item.get("slide_id"), str)
        and isinstance(item.get("context_path"), str)
    }
    return {
        "template_id": snapshot.get("template_id"),
        "revision_id": snapshot.get("revision_id"),
        "context_dir": str(destination),
        "snapshot_path": str(destination / "snapshot.json"),
        "overview_path": str(destination / "overview.md"),
        "overview_json_path": str(destination / "overview.json"),
        "selection_path": str(destination / "selection.json"),
        "theme_css_path": (
            str(destination / "theme.css")
            if (destination / "theme.css").exists()
            else None
        ),
        "theme_json_path": (
            str(destination / "theme.json")
            if (destination / "theme.json").exists()
            else None
        ),
        "asset_index_path": str(destination / "assets" / "index.json"),
        "reference_context_paths": reference_paths,
        "snapshot": dict(snapshot),
    }


def materialize_context_pack(
    provider: TemplateContextProvider,
    template_id: str,
    task_workspace: Path,
    revision_id: str | None = None,
    slide_needs: (
        Sequence[SlideNeed | Mapping[str, Any]] | Mapping[str, Any] | None
    ) = None,
) -> JsonObject:
    """Create ``task_workspace/template_context`` from compiled IR only."""

    revision = provider.store.resolve(template_id, revision_id)
    needs = _normalize_needs(slide_needs)
    request = {
        "template_id": revision.template_id,
        "revision_id": revision.revision_id,
        "slide_needs": [need.as_dict() for need in needs],
    }
    request_hash = hashlib.sha256(_canonical_json(request).encode("utf-8")).hexdigest()

    task_root = Path(task_workspace).expanduser().resolve(strict=False)
    task_root.mkdir(parents=True, exist_ok=True)
    destination = task_root / "template_context"
    existing = _existing_result(
        destination,
        revision.template_id,
        revision.revision_id,
        request_hash,
    )
    if existing is not None:
        return existing

    temporary = Path(tempfile.mkdtemp(prefix=".template-context-", dir=task_root))
    try:
        selection = _selection_payload(provider, revision, needs)
        _write_json(temporary / "selection.json", selection)
        overview_payload = provider.get_overview(
            revision.template_id,
            revision.revision_id,
            max_chars=provider.overview_chars,
        )
        _write_json(temporary / "overview.json", overview_payload)
        overview = provider.render_overview_markdown(
            revision.template_id,
            revision.revision_id,
            max_chars=provider.overview_chars,
        )
        (temporary / "overview.md").write_text(overview, encoding="utf-8")

        references: list[JsonObject] = []
        referenced_asset_ids: set[str] = set()
        # The prompt receives only bounded retrieval results, but the task-local
        # snapshot contains every compact reference.  This lets the model choose
        # per-slide examples after Research without reopening the global store.
        all_slide_ids = [
            str(record["slide_id"]) for record in revision.load_slide_index()
        ]
        for slide_id in all_slide_ids:
            reference, asset_refs = _materialize_reference(
                provider,
                revision,
                slide_id,
                temporary,
            )
            references.append(reference)
            referenced_asset_ids.update(asset_refs)

        assets = _materialize_assets(revision, temporary, referenced_asset_ids)
        theme_paths = _materialize_theme(revision, temporary)
        files = _materialized_hashes(temporary)
        snapshot: JsonObject = {
            "snapshot_version": 2,
            "template_id": revision.template_id,
            "revision_id": revision.revision_id,
            "revision_hash": revision.revision_hash,
            "ir_schema_version": revision.schema_version,
            "request_hash": request_hash,
            "created_at": datetime.now(UTC).isoformat(),
            "overview_path": "overview.md",
            "overview_json_path": "overview.json",
            "selection_path": "selection.json",
            "theme": theme_paths,
            "assets": [
                {
                    "asset_id": asset.get("asset_id"),
                    "role": asset.get("role", asset.get("kind")),
                    "path": asset.get("path"),
                    "sha256": asset.get("sha256"),
                }
                for asset in assets
            ],
            "references": references,
            "files": files,
        }
        _write_json(temporary / "snapshot.json", snapshot)
        try:
            os.replace(temporary, destination)
        except OSError as exc:
            # A concurrent materializer may have won the race. Reuse it only
            # when it pins exactly the same revision and request.
            concurrent = _existing_result(
                destination,
                revision.template_id,
                revision.revision_id,
                request_hash,
            )
            if concurrent is not None:
                shutil.rmtree(temporary, ignore_errors=True)
                return concurrent
            raise TemplateContextConflictError(
                f"Could not publish template context: {destination}"
            ) from exc
        return _result_payload(destination, snapshot)
    except BaseException:
        if temporary.exists():
            shutil.rmtree(temporary, ignore_errors=True)
        raise


def _read_local_json(path: Path, *, max_bytes: int = 1_000_000) -> JsonObject:
    try:
        if path.stat().st_size > max_bytes:
            raise TemplateContextConflictError(
                f"Task-local template JSON is too large: {path}"
            )
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TemplateContextConflictError(
            f"Invalid task-local template JSON: {path}"
        ) from exc
    if not isinstance(value, dict):
        raise TemplateContextConflictError(
            f"Task-local template JSON must be an object: {path}"
        )
    return value


def _resolve_local_path(root: Path, relative_path: str | Path) -> Path:
    raw = str(relative_path).replace("\\", "/")
    relative = Path(raw)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise TemplateContextConflictError(f"Unsafe task-local template path: {raw!r}")
    if relative.suffix.lower() == ".pptx" or "source" in {
        part.lower() for part in relative.parts
    }:
        raise TemplateContextConflictError(
            f"Task-local template context cannot contain source files: {raw!r}"
        )
    candidate = (root / relative).resolve(strict=False)
    if not candidate.is_relative_to(root):
        raise TemplateContextConflictError(
            f"Task-local template path escapes its snapshot: {raw!r}"
        )
    if not candidate.is_file():
        raise TemplateContextConflictError(
            f"Missing task-local template artifact: {raw}"
        )
    resolved = candidate.resolve(strict=True)
    if not resolved.is_relative_to(root):
        raise TemplateContextConflictError(
            f"Task-local template symlink escapes its snapshot: {raw!r}"
        )
    return resolved


@dataclass(frozen=True, slots=True)
class TaskTemplateContext:
    """Verified, self-contained Template IR snapshot used during generation.

    This class deliberately has no ``TemplateStore`` reference.  All overview,
    search, reference, image, theme, and asset reads stay inside one task's
    ``template_context`` directory after creation.
    """

    root: Path
    template_id: str
    revision_id: str
    snapshot: JsonObject
    overview: JsonObject
    references: Mapping[str, JsonObject]
    _hashes: Mapping[str, str] = field(repr=False)

    @classmethod
    def load(
        cls,
        context_dir: str | Path,
        *,
        expected_template_id: str | None = None,
        expected_revision_id: str | None = None,
    ) -> "TaskTemplateContext":
        root = Path(context_dir).expanduser().resolve(strict=True)
        if not root.is_dir():
            raise TemplateContextConflictError(
                f"Task-local template context is not a directory: {root}"
            )
        snapshot = _read_local_json(_resolve_local_path(root, "snapshot.json"))
        if snapshot.get("snapshot_version") != 2:
            raise TemplateContextConflictError(
                "Task-local template context must use snapshot_version=2"
            )

        template_id = snapshot.get("template_id")
        revision_id = snapshot.get("revision_id")
        if not isinstance(template_id, str) or not isinstance(revision_id, str):
            raise TemplateContextConflictError(
                "Task-local template snapshot has no template/revision identity"
            )
        if expected_template_id is not None and template_id != expected_template_id:
            raise TemplateContextConflictError(
                f"Template context pins {template_id!r}, expected {expected_template_id!r}"
            )
        if expected_revision_id is not None and revision_id != expected_revision_id:
            raise TemplateContextConflictError(
                f"Template context pins {revision_id!r}, expected {expected_revision_id!r}"
            )

        raw_hashes = snapshot.get("files")
        if not isinstance(raw_hashes, Mapping) or not raw_hashes:
            raise TemplateContextConflictError(
                "Task-local template snapshot has no file integrity index"
            )
        hashes: dict[str, str] = {}
        for relative_path, expected_hash in raw_hashes.items():
            if not isinstance(relative_path, str) or not isinstance(expected_hash, str):
                raise TemplateContextConflictError(
                    "Task-local template file index is malformed"
                )
            path = _resolve_local_path(root, relative_path)
            actual_hash = _sha256(path)
            if actual_hash != expected_hash:
                raise TemplateContextConflictError(
                    f"Task-local template artifact changed: {relative_path}"
                )
            hashes[relative_path] = expected_hash

        overview_path = snapshot.get("overview_json_path")
        if not isinstance(overview_path, str):
            raise TemplateContextConflictError(
                "Task-local template snapshot has no structured overview"
            )
        overview = _read_local_json(_resolve_local_path(root, overview_path))

        references: dict[str, JsonObject] = {}
        raw_references = snapshot.get("references")
        if not isinstance(raw_references, list) or not raw_references:
            raise TemplateContextConflictError(
                "Task-local template snapshot has no references"
            )
        if len(raw_references) > 512:
            raise TemplateContextConflictError(
                "Task-local template snapshot has too many references"
            )
        for entry in raw_references:
            if not isinstance(entry, Mapping):
                raise TemplateContextConflictError(
                    "Task-local template reference index is malformed"
                )
            slide_id = entry.get("slide_id")
            context_path = entry.get("context_path")
            if not isinstance(slide_id, str) or not isinstance(context_path, str):
                raise TemplateContextConflictError(
                    "Task-local template reference has no identity/path"
                )
            context = _read_local_json(_resolve_local_path(root, context_path))
            if context.get("slide_id") != slide_id:
                raise TemplateContextConflictError(
                    f"Task-local reference identity mismatch: {slide_id}"
                )
            if context.get("template_id") != template_id:
                raise TemplateContextConflictError(
                    f"Task-local reference template mismatch: {slide_id}"
                )
            if context.get("revision_id") != revision_id:
                raise TemplateContextConflictError(
                    f"Task-local reference revision mismatch: {slide_id}"
                )
            if slide_id in references:
                raise TemplateContextConflictError(
                    f"Duplicate task-local template reference: {slide_id}"
                )
            references[slide_id] = context

        return cls(
            root=root,
            template_id=template_id,
            revision_id=revision_id,
            snapshot=snapshot,
            overview=overview,
            references=references,
            _hashes=hashes,
        )

    def resolve_path(self, relative_path: str | Path) -> Path:
        """Resolve and recheck one task-local artifact before use."""

        path = _resolve_local_path(self.root, relative_path)
        relative = path.relative_to(self.root).as_posix()
        expected_hash = self._hashes.get(relative)
        if expected_hash is None:
            raise TemplateContextConflictError(
                f"Artifact is not declared by the task snapshot: {relative}"
            )
        if _sha256(path) != expected_hash:
            raise TemplateContextConflictError(
                f"Task-local template artifact changed: {relative}"
            )
        return path

    def get_overview(
        self,
        *,
        max_chars: int | None = None,
    ) -> JsonObject:
        """Return a bounded copy of the pinned task-local overview."""

        return _bounded_object(
            self.overview,
            max_chars=max_chars or DEFAULT_OVERVIEW_CHARS,
            identity_keys=("template_id", "revision_id", "schema_version"),
        )

    def search_references(
        self,
        need: SlideNeed | Mapping[str, Any],
        *,
        limit: int = MAX_REFERENCE_RESULTS,
        max_chars_per_result: int | None = None,
    ) -> list[JsonObject]:
        """Search only the compact references captured in this task snapshot."""

        if not 1 <= limit <= MAX_REFERENCE_RESULTS:
            raise ValueError(f"limit must be between 1 and {MAX_REFERENCE_RESULTS}")
        max_chars_per_result = max_chars_per_result or DEFAULT_SEARCH_RESULT_CHARS
        normalized_need = SlideNeed.from_value(need)
        excluded = _string_set(normalized_need.exclude_slide_ids)
        matches = []
        for slide_id, reference in self.references.items():
            if _normalized(slide_id) in excluded:
                continue
            record: JsonObject = {
                "slide_id": slide_id,
                "page_number": reference.get("page_number"),
                "family_ids": reference.get("family_ids", []),
                "summary": reference.get(
                    "summary",
                    reference.get("selection_hints", []),
                ),
                "is_representative": reference.get("is_representative", False),
            }
            semantic: JsonObject = {
                "page_semantics": reference.get("page_semantics", {}),
                "regions": reference.get("regions", []),
                "family_ids": reference.get("family_ids", []),
                "selection_hints": reference.get("selection_hints", []),
                "avoid_when": reference.get("avoid_when", []),
                "is_representative": reference.get("is_representative", False),
            }
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
        slide_id: str,
        *,
        max_chars: int | None = None,
    ) -> JsonObject:
        """Return one bounded reference captured in this task snapshot."""

        try:
            reference = self.references[slide_id]
        except KeyError as exc:
            raise KeyError(f"Unknown task-local template slide: {slide_id}") from exc
        return _bounded_object(
            reference,
            max_chars=max_chars or DEFAULT_REFERENCE_CHARS,
            identity_keys=("template_id", "revision_id", "slide_id", "page_number"),
        )

    def reference_image_path(self, slide_id: str) -> Path | None:
        """Return one verified style/reference image for a captured slide."""

        try:
            reference = self.references[slide_id]
        except KeyError as exc:
            raise KeyError(f"Unknown task-local template slide: {slide_id}") from exc
        files = reference.get("reference_files")
        if not isinstance(files, Mapping):
            return None
        for kind in ("style_reference", "reference"):
            relative_path = files.get(kind)
            if isinstance(relative_path, str):
                return self.resolve_path(relative_path)
        return None


@dataclass(slots=True)
class TemplateRuntime:
    """Small facade used by non-agent service integration."""

    store: TemplateStore
    context: TemplateContextProvider = field(init=False)

    def __post_init__(self) -> None:
        self.context = TemplateContextProvider(self.store)

    def get_overview(
        self,
        template_id: str,
        revision_id: str | None = None,
        *,
        max_chars: int | None = None,
    ) -> JsonObject:
        """Return the bounded template overview."""

        return self.context.get_overview(
            template_id,
            revision_id,
            max_chars=max_chars,
        )

    def search_references(
        self,
        template_id: str,
        need: SlideNeed | Mapping[str, Any],
        revision_id: str | None = None,
        *,
        limit: int = 2,
    ) -> list[JsonObject]:
        """Return one or two scored reference summaries."""

        return self.context.search_references(
            template_id,
            need,
            revision_id,
            limit=limit,
        )

    def get_reference(
        self,
        template_id: str,
        slide_id: str,
        revision_id: str | None = None,
        *,
        max_chars: int | None = None,
    ) -> JsonObject:
        """Return one bounded slide context."""

        return self.context.get_reference(
            template_id,
            slide_id,
            revision_id,
            max_chars=max_chars,
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
        """Create the task-local context pack."""

        return self.context.materialize(
            template_id,
            task_workspace,
            revision_id,
            slide_needs,
        )
