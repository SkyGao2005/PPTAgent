"""Persistence-restore contract for TemplateRegistry."""

from __future__ import annotations

import json
from pathlib import Path

from deeppresenter.server.services.template_registry import TemplateRegistry


def _write_manifest(templates_dir: Path, template_id: str) -> Path:
    """Write a manifest exactly as the compiler serializes it."""

    revision_id = "rev_b3bb744905089d95e36c141f"
    payload = {
        "schema_version": "2.0",
        "template_id": template_id,
        "name": template_id,
        "status": "ready",
        "latest_revision_id": revision_id,
        "active_revision_id": revision_id,
        "revisions": [
            {
                "revision_id": revision_id,
                "source_hash": "a" * 64,
                "slide_count": 3,
                "created_at": "2026-07-21T07:47:55.703311Z",
            }
        ],
        "source_hash": "a" * 64,
        "slide_count": 3,
        "aspect_ratio": "16:9",
        "created_at": "2026-07-21T07:38:03.744858Z",
        "updated_at": "2026-07-21T07:47:55.705146Z",
    }
    template_dir = templates_dir / template_id
    template_dir.mkdir(parents=True, exist_ok=True)
    manifest_path = template_dir / "manifest.json"
    manifest_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return manifest_path


def test_registry_restores_serialized_manifests_from_disk(tmp_path: Path) -> None:
    """ISO timestamps and enum values must survive a restart.

    TemplateManifest is a strict model, so a manifest reloaded as a Python
    dict rejects string timestamps and enum values; it must be parsed as JSON.
    """

    workspace = tmp_path / "templates_api"
    _write_manifest(workspace / "templates", "tpl_2022_cbf63bf3a92656d2")

    registry = TemplateRegistry(workspace)

    restored = registry.list_all(include_failed=True)
    assert [manifest.template_id for manifest in restored] == [
        "tpl_2022_cbf63bf3a92656d2"
    ]
    manifest = registry.get("tpl_2022_cbf63bf3a92656d2")
    assert manifest is not None
    assert manifest.status.value == "ready"
    assert manifest.active_revision_id == "rev_b3bb744905089d95e36c141f"
    assert manifest.created_at.year == 2026
    assert registry.find_by_hash("a" * 64) is manifest


def test_registry_skips_corrupt_manifest_without_failing_startup(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "templates_api"
    templates_dir = workspace / "templates"
    _write_manifest(templates_dir, "good")
    broken = templates_dir / "broken"
    broken.mkdir(parents=True, exist_ok=True)
    (broken / "manifest.json").write_text("{not json", encoding="utf-8")

    registry = TemplateRegistry(workspace)

    assert [manifest.template_id for manifest in registry.list_all()] == ["good"]
