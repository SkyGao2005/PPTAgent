"""Stable identifiers and canonical hashes for Template IR artifacts."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from .models import SCHEMA_VERSION


_SAFE_FRAGMENT = re.compile(r"[^a-z0-9]+")


def sha256_bytes(value: bytes) -> str:
    """Return the lowercase SHA-256 digest for bytes."""

    return hashlib.sha256(value).hexdigest()


def sha256_file(path: Path, chunk_size: int = 1024 * 1024) -> str:
    """Hash a file without loading it in memory."""

    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(chunk_size), b""):
            digest.update(chunk)
    return digest.hexdigest()


def canonical_hash(value: Mapping[str, Any]) -> str:
    """Hash a JSON-compatible mapping with canonical serialization."""

    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return sha256_bytes(payload)


def derive_template_id(source_hash: str, name: str | None = None) -> str:
    """Derive a stable template ID when the caller has no registry ID.

    Registry-backed callers should pass their own persistent ID to the compiler.
    The source-derived fallback is useful for batch and command-line parsing.
    """

    prefix = _SAFE_FRAGMENT.sub("-", (name or "template").lower()).strip("-")
    prefix = prefix[:40] or "template"
    return f"tpl_{prefix}_{source_hash[:16]}"


def derive_revision_id(
    source_hash: str,
    *,
    compiler_version: str,
    extractor_version: str,
    annotator_id: str,
    renderer_id: str,
) -> str:
    """Derive an immutable revision ID from every output-affecting input."""

    digest = canonical_hash(
        {
            "schema_version": SCHEMA_VERSION,
            "source_hash": source_hash,
            "compiler_version": compiler_version,
            "extractor_version": extractor_version,
            "annotator_id": annotator_id,
            "renderer_id": renderer_id,
        }
    )
    return f"rev_{digest[:24]}"


def asset_id(content_hash: str) -> str:
    """Return a content-addressed asset ID."""

    return f"asset_{content_hash[:24]}"


def family_id(signature: str) -> str:
    """Return a stable ID for a semantic layout family signature."""

    return f"family_{sha256_bytes(signature.encode('utf-8'))[:20]}"
