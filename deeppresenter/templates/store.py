"""Read-only access to compiled template IR revisions.

The generation runtime must never inspect a PowerPoint source file.  This
module therefore exposes only files inside a READY revision and rejects both
``source/`` paths and ``.pptx`` artifacts at the path boundary.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping


JsonObject = dict[str, Any]

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
_SHA256_RE = re.compile(r"^[0-9a-fA-F]{64}$")
_MAX_JSON_BYTES = 8 * 1024 * 1024
_MAX_INDEX_BYTES = 16 * 1024 * 1024
_MAX_SLIDES = 1000


class TemplateStoreError(RuntimeError):
    """Base class for template IR access failures."""


class TemplateNotFoundError(TemplateStoreError):
    """The requested template or revision does not exist."""


class TemplateRevisionNotReadyError(TemplateStoreError):
    """The revision exists but is not safe for generation yet."""


class TemplateIntegrityError(TemplateStoreError):
    """A compiled IR file is missing, malformed, or has the wrong hash."""


class UnsafeTemplatePathError(TemplateIntegrityError):
    """A path leaves the revision or points at a forbidden source artifact."""


class LegacyTemplateError(TemplateStoreError):
    """A legacy source-PPTX template must be compiled before generation."""


class TemplateAspectRatioMismatchError(TemplateStoreError):
    """The requested output ratio conflicts with the pinned Template IR canvas."""


def _validate_identifier(value: str, label: str) -> str:
    if not _IDENTIFIER_RE.fullmatch(value) or value in {".", ".."}:
        raise UnsafeTemplatePathError(f"Invalid {label}: {value!r}")
    return value


def _json_size_guard(path: Path, max_bytes: int = _MAX_JSON_BYTES) -> None:
    size = path.stat().st_size
    if size > max_bytes:
        raise TemplateIntegrityError(
            f"IR file is too large ({size} bytes, limit {max_bytes}): {path.name}"
        )


def _read_json_file(path: Path, max_bytes: int = _MAX_JSON_BYTES) -> Any:
    _json_size_guard(path, max_bytes)
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise TemplateIntegrityError(f"Invalid JSON file: {path}") from exc


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _path_from_record(record: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value

    files = record.get("files")
    if isinstance(files, Mapping):
        for key in keys:
            value = files.get(key.removesuffix("_path")) or files.get(key)
            if isinstance(value, str) and value.strip():
                return value
    return None


@dataclass(frozen=True, slots=True)
class TemplateRevision:
    """A pinned, validated READY template revision."""

    template_id: str
    revision_id: str
    template_root: Path
    root: Path
    metadata: JsonObject
    manifest: JsonObject
    _hashes: Mapping[str, str] = field(repr=False)

    @property
    def schema_version(self) -> str:
        """Return the IR schema version as a stable string."""

        value = self.metadata.get("schema_version", "unknown")
        return str(value)

    @property
    def revision_hash(self) -> str:
        """Return a compiler-provided revision hash or a metadata hash."""

        for key in ("revision_hash", "content_hash", "ir_hash"):
            value = self.metadata.get(key)
            if isinstance(value, str) and value:
                return value
        canonical = json.dumps(
            self.metadata,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        return hashlib.sha256(canonical).hexdigest()

    def resolve_path(self, relative_path: str | Path) -> Path:
        """Resolve a runtime IR path and enforce the revision sandbox."""

        raw = str(relative_path).replace("\\", "/")
        pure = PurePosixPath(raw)
        if pure.is_absolute() or not pure.parts or ".." in pure.parts:
            raise UnsafeTemplatePathError(f"Unsafe template path: {raw!r}")
        if pure.suffix.lower() == ".pptx" or "source" in {
            part.lower() for part in pure.parts
        }:
            raise UnsafeTemplatePathError(
                f"Generation cannot access template source files: {raw!r}"
            )

        root = self.root.resolve(strict=True)
        candidate = (root / Path(*pure.parts)).resolve(strict=False)
        if not candidate.is_relative_to(root):
            raise UnsafeTemplatePathError(f"Template path escapes revision: {raw!r}")
        if not candidate.exists() or not candidate.is_file():
            raise TemplateIntegrityError(f"Missing template IR artifact: {raw}")

        resolved = candidate.resolve(strict=True)
        if not resolved.is_relative_to(root):
            raise UnsafeTemplatePathError(f"Template symlink escapes revision: {raw!r}")

        expected = self._hashes.get(pure.as_posix())
        if expected is not None:
            actual = _sha256(resolved)
            if actual != expected:
                raise TemplateIntegrityError(
                    f"SHA256 mismatch for {raw}: expected {expected}, got {actual}"
                )
        return resolved

    def read_json(
        self, relative_path: str | Path, max_bytes: int = _MAX_JSON_BYTES
    ) -> Any:
        """Read a validated JSON artifact from this revision."""

        return _read_json_file(self.resolve_path(relative_path), max_bytes=max_bytes)

    def read_text(
        self,
        relative_path: str | Path,
        max_bytes: int = _MAX_JSON_BYTES,
    ) -> str:
        """Read a bounded UTF-8 text artifact from this revision."""

        path = self.resolve_path(relative_path)
        _json_size_guard(path, max_bytes=max_bytes)
        try:
            return path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as exc:
            raise TemplateIntegrityError(
                f"Invalid UTF-8 template artifact: {path}"
            ) from exc

    def load_slide_index(self) -> list[JsonObject]:
        """Load and validate ``slides/index.jsonl``."""

        path = self.resolve_path("slides/index.jsonl")
        _json_size_guard(path, max_bytes=_MAX_INDEX_BYTES)
        records: list[JsonObject] = []
        seen: set[str] = set()
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise TemplateIntegrityError(f"Invalid slide index: {path}") from exc

        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise TemplateIntegrityError(
                    f"Invalid slides/index.jsonl line {line_number}"
                ) from exc
            if not isinstance(value, dict):
                raise TemplateIntegrityError(
                    f"Slide index line {line_number} must be an object"
                )
            slide_id_value = value.get("slide_id") or value.get("id")
            if not isinstance(slide_id_value, str):
                raise TemplateIntegrityError(
                    f"Slide index line {line_number} has no slide_id"
                )
            slide_id = _validate_identifier(slide_id_value, "slide_id")
            if slide_id in seen:
                raise TemplateIntegrityError(f"Duplicate slide_id: {slide_id}")
            value["slide_id"] = slide_id
            records.append(value)
            seen.add(slide_id)
            if len(records) > _MAX_SLIDES:
                raise TemplateIntegrityError(
                    f"Slide index exceeds {_MAX_SLIDES} entries"
                )
        if not records:
            raise TemplateIntegrityError("Template revision contains no indexed slides")
        return records

    def slide_record(self, slide_id: str) -> JsonObject:
        """Return one slide index record."""

        wanted = _validate_identifier(slide_id, "slide_id")
        for record in self.load_slide_index():
            if record["slide_id"] == wanted:
                return record
        raise TemplateNotFoundError(f"Unknown slide_id: {slide_id}")

    def slide_artifact_path(self, record: Mapping[str, Any], kind: str) -> str:
        """Resolve an indexed slide artifact path with canonical fallbacks."""

        slide_id = _validate_identifier(str(record.get("slide_id", "")), "slide_id")
        aliases: dict[str, tuple[str, ...]] = {
            "semantic": ("semantic_path", "semantic"),
            "compact": (
                "compact_context_path",
                "context_path",
                "compact_path",
                "context_compact_path",
            ),
            "reference": ("reference_path", "reference_image_path", "preview_path"),
            "style_reference": ("style_reference_path", "style_image_path"),
            "overlay": ("overlay_path", "overlay_image_path"),
        }
        if kind not in aliases:
            raise ValueError(f"Unknown slide artifact kind: {kind}")
        explicit = _path_from_record(record, *aliases[kind])
        if explicit:
            return explicit
        filenames = {
            "semantic": "semantic.json",
            "compact": "context.compact.json",
            "reference": "reference.webp",
            "style_reference": "style_reference.webp",
            "overlay": "overlay.webp",
        }
        return f"slides/{slide_id}/{filenames[kind]}"

    def load_slide_semantic(self, slide_id: str) -> JsonObject:
        """Load the semantic annotation for one slide."""

        record = self.slide_record(slide_id)
        value = self.read_json(self.slide_artifact_path(record, "semantic"))
        if not isinstance(value, dict):
            raise TemplateIntegrityError(
                f"semantic.json for {slide_id} must be an object"
            )
        return value

    def load_slide_compact(self, slide_id: str) -> JsonObject:
        """Load the precompiled bounded context for one slide."""

        record = self.slide_record(slide_id)
        value = self.read_json(self.slide_artifact_path(record, "compact"))
        if not isinstance(value, dict):
            raise TemplateIntegrityError(
                f"context.compact.json for {slide_id} must be an object"
            )
        return value

    def load_theme(self) -> JsonObject:
        """Load theme tokens when a JSON theme is available."""

        theme_path = self.root / "theme" / "theme.json"
        if not theme_path.exists():
            return {}
        value = self.read_json("theme/theme.json")
        if not isinstance(value, dict):
            raise TemplateIntegrityError("theme/theme.json must be an object")
        return value

    def load_theme_css(self) -> str | None:
        """Load generated theme CSS when present."""

        css_path = self.root / "theme" / "theme.css"
        if not css_path.exists():
            return None
        return self.read_text("theme/theme.css", max_bytes=2 * 1024 * 1024)

    def load_families(self) -> list[JsonObject]:
        """Load family descriptors in deterministic filename order."""

        families_dir = self.root / "families"
        if not families_dir.exists():
            return []
        result: list[JsonObject] = []
        for path in sorted(families_dir.glob("*.json")):
            relative = path.relative_to(self.root).as_posix()
            value = self.read_json(relative)
            if isinstance(value, dict) and isinstance(value.get("families"), list):
                values = value["families"]
            else:
                values = [value]
            for family in values:
                if not isinstance(family, dict):
                    raise TemplateIntegrityError(
                        f"Family descriptor must be an object: {relative}"
                    )
                result.append(family)
        return result

    def load_assets(self) -> list[JsonObject]:
        """Load and normalize the extracted reusable asset index."""

        value = self.read_json("assets/index.json")
        if isinstance(value, dict) and isinstance(value.get("assets"), list):
            entries: Iterable[Any] = value["assets"]
        elif isinstance(value, list):
            entries = value
        elif isinstance(value, dict):
            entries = [
                {"asset_id": asset_id, **entry}
                for asset_id, entry in value.items()
                if isinstance(entry, dict)
            ]
        else:
            raise TemplateIntegrityError("assets/index.json has an invalid shape")

        assets: list[JsonObject] = []
        seen: set[str] = set()
        for position, raw in enumerate(entries, start=1):
            if not isinstance(raw, dict):
                raise TemplateIntegrityError("Asset index entries must be objects")
            asset_id_value = (
                raw.get("asset_id") or raw.get("id") or f"asset_{position:04d}"
            )
            asset_id = _validate_identifier(str(asset_id_value), "asset_id")
            if asset_id in seen:
                raise TemplateIntegrityError(f"Duplicate asset_id: {asset_id}")
            asset = dict(raw)
            asset["asset_id"] = asset_id
            path_value = _path_from_record(
                asset,
                "path",
                "relative_path",
                "file_path",
                "artifact_path",
            )
            if path_value is None:
                raise TemplateIntegrityError(
                    f"Asset {asset_id} has no extracted file path"
                )
            asset_path = self.resolve_path(path_value)
            asset_hash = asset.get("sha256")
            if asset_hash is not None:
                if not isinstance(asset_hash, str) or not _SHA256_RE.fullmatch(
                    asset_hash
                ):
                    raise TemplateIntegrityError(
                        f"Asset {asset_id} has an invalid SHA256"
                    )
                actual_hash = _sha256(asset_path)
                if actual_hash != asset_hash.lower():
                    raise TemplateIntegrityError(
                        f"SHA256 mismatch for asset {asset_id}: "
                        f"expected {asset_hash.lower()}, got {actual_hash}"
                    )
            asset["path"] = path_value
            assets.append(asset)
            seen.add(asset_id)
        return assets


class TemplateStore:
    """Resolve immutable template revisions from one or more IR roots."""

    def __init__(self, roots: list[Path] | tuple[Path, ...] | Path):
        if isinstance(roots, Path):
            values = [roots]
        else:
            values = list(roots)
        if not values:
            raise ValueError("TemplateStore requires at least one root")
        self.roots = tuple(
            Path(root).expanduser().resolve(strict=False) for root in values
        )

    def resolve(
        self,
        template_id: str,
        revision_id: str | None = None,
    ) -> TemplateRevision:
        """Resolve and pin a READY revision.

        ``revision_id=None`` is allowed only when ``manifest.json`` declares an
        active/current revision.  No "newest directory" fallback is used.
        """

        safe_template_id = _validate_identifier(template_id, "template_id")
        legacy_dirs: list[Path] = []
        for root in self.roots:
            template_root = (
                root if root.name == safe_template_id else root / safe_template_id
            )
            if not template_root.exists() or not template_root.is_dir():
                continue
            manifest = self._load_manifest(template_root)
            pinned_revision_id = revision_id or self._active_revision_id(manifest)
            if pinned_revision_id is None:
                if self._is_legacy_template(template_root):
                    legacy_dirs.append(template_root)
                    continue
                raise TemplateNotFoundError(
                    f"Template {template_id!r} has no active compiled revision"
                )
            safe_revision_id = _validate_identifier(pinned_revision_id, "revision_id")
            revision_root = template_root / "revisions" / safe_revision_id
            if not revision_root.exists():
                if self._is_legacy_template(template_root):
                    legacy_dirs.append(template_root)
                    continue
                if revision_id is not None:
                    continue
                raise TemplateNotFoundError(
                    f"Active revision {safe_revision_id!r} is missing for {template_id!r}"
                )
            return self._open_revision(
                safe_template_id,
                safe_revision_id,
                template_root,
                revision_root,
                manifest,
            )

        if legacy_dirs:
            raise LegacyTemplateError(
                f"Template {template_id!r} only contains a legacy source.pptx; "
                "compile it to Template IR before generation"
            )
        suffix = f" revision {revision_id!r}" if revision_id else ""
        raise TemplateNotFoundError(f"Template {template_id!r}{suffix} was not found")

    def _load_manifest(self, template_root: Path) -> JsonObject:
        manifest_path = template_root / "manifest.json"
        if not manifest_path.exists():
            return {}
        value = _read_json_file(manifest_path)
        if not isinstance(value, dict):
            raise TemplateIntegrityError(
                f"manifest.json must be an object: {manifest_path}"
            )
        manifest_template_id = value.get("template_id")
        if (
            manifest_template_id is not None
            and manifest_template_id != template_root.name
        ):
            raise TemplateIntegrityError(
                f"Manifest template_id does not match directory {template_root.name!r}"
            )
        return value

    @staticmethod
    def _active_revision_id(manifest: Mapping[str, Any]) -> str | None:
        for key in (
            "active_revision_id",
            "current_revision_id",
            "ready_revision_id",
            "latest_revision_id",
            "revision_id",
        ):
            value = manifest.get(key)
            if isinstance(value, str) and value:
                return value
        active = manifest.get("active_revision")
        if isinstance(active, Mapping):
            value = active.get("revision_id") or active.get("id")
            if isinstance(value, str) and value:
                return value
        return None

    @staticmethod
    def _is_legacy_template(template_root: Path) -> bool:
        return any(
            (template_root / name).exists()
            for name in ("source.pptx", "original.pptx", "template.pptx")
        )

    def _open_revision(
        self,
        template_id: str,
        revision_id: str,
        template_root: Path,
        revision_root: Path,
        manifest: JsonObject,
    ) -> TemplateRevision:
        resolved_template_root = template_root.resolve(strict=True)
        resolved_revision_root = revision_root.resolve(strict=True)
        revisions_root = (resolved_template_root / "revisions").resolve(strict=True)
        if not resolved_revision_root.is_relative_to(revisions_root):
            raise UnsafeTemplatePathError("Revision directory escapes template root")

        metadata_path = resolved_revision_root / "revision.json"
        if not metadata_path.exists():
            raise TemplateIntegrityError(f"Missing revision.json: {metadata_path}")
        metadata = _read_json_file(metadata_path)
        if not isinstance(metadata, dict):
            raise TemplateIntegrityError("revision.json must be an object")

        status = metadata.get("status", metadata.get("state"))
        if not isinstance(status, str) or status.lower() != "ready":
            raise TemplateRevisionNotReadyError(
                f"Template {template_id!r} revision {revision_id!r} is not READY"
            )
        metadata_template_id = metadata.get("template_id")
        if metadata_template_id is not None and metadata_template_id != template_id:
            raise TemplateIntegrityError("revision.json template_id mismatch")
        metadata_revision_id = metadata.get("revision_id") or metadata.get("id")
        if metadata_revision_id is not None and metadata_revision_id != revision_id:
            raise TemplateIntegrityError("revision.json revision_id mismatch")

        hashes = self._extract_hashes(metadata)
        revision = TemplateRevision(
            template_id=template_id,
            revision_id=revision_id,
            template_root=resolved_template_root,
            root=resolved_revision_root,
            metadata=metadata,
            manifest=manifest,
            _hashes=hashes,
        )
        self._validate_required_ir(revision)
        self._verify_declared_hashes(revision)
        return revision

    @staticmethod
    def _extract_hashes(metadata: Mapping[str, Any]) -> dict[str, str]:
        hashes: dict[str, str] = {}

        def add(path_value: Any, hash_value: Any) -> None:
            if not isinstance(path_value, str) or not isinstance(hash_value, str):
                return
            normalized = PurePosixPath(path_value.replace("\\", "/")).as_posix()
            if not _SHA256_RE.fullmatch(hash_value):
                raise TemplateIntegrityError(f"Invalid SHA256 for {normalized}")
            hashes[normalized] = hash_value.lower()

        mappings: list[Any] = [metadata.get("file_hashes"), metadata.get("files")]
        integrity = metadata.get("integrity")
        if isinstance(integrity, Mapping):
            mappings.append(integrity.get("files"))
        for mapping in mappings:
            if not isinstance(mapping, Mapping):
                continue
            for path_value, value in mapping.items():
                if isinstance(value, str):
                    add(path_value, value)
                elif isinstance(value, Mapping):
                    add(path_value, value.get("sha256") or value.get("hash"))

        artifacts = metadata.get("artifacts")
        if isinstance(artifacts, list):
            for artifact in artifacts:
                if isinstance(artifact, Mapping):
                    add(
                        artifact.get("path") or artifact.get("relative_path"),
                        artifact.get("sha256") or artifact.get("hash"),
                    )
        return hashes

    @staticmethod
    def _validate_required_ir(revision: TemplateRevision) -> None:
        revision.resolve_path("slides/index.jsonl")
        revision.resolve_path("assets/index.json")
        theme_json = revision.root / "theme" / "theme.json"
        theme_css = revision.root / "theme" / "theme.css"
        if not theme_json.exists() and not theme_css.exists():
            raise TemplateIntegrityError(
                "Template IR must contain theme/theme.json or theme/theme.css"
            )

        for record in revision.load_slide_index():
            revision.resolve_path(revision.slide_artifact_path(record, "semantic"))
            revision.resolve_path(revision.slide_artifact_path(record, "compact"))
            for kind in ("style_reference", "reference", "overlay"):
                explicit = _path_from_record(
                    record,
                    {
                        "style_reference": "style_reference_path",
                        "reference": "reference_image_path",
                        "overlay": "overlay_image_path",
                    }[kind],
                )
                if explicit is not None:
                    revision.resolve_path(explicit)
        revision.load_assets()
        revision.load_families()

    @staticmethod
    def _verify_declared_hashes(revision: TemplateRevision) -> None:
        for relative_path in revision._hashes:
            pure = PurePosixPath(relative_path)
            # The compiler may record source provenance, but generation must not
            # open it even for integrity verification.
            if pure.suffix.lower() == ".pptx" or "source" in {
                part.lower() for part in pure.parts
            }:
                continue
            revision.resolve_path(relative_path)
