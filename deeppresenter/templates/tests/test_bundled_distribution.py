"""Distribution contract for the six built-in Template IR packages."""

from __future__ import annotations

import json
from pathlib import Path

from deeppresenter.templates.compile_bundled import BUNDLED_TEMPLATE_IDS
from deeppresenter.templates.store import TemplateStore


PROJECT_ROOT = Path(__file__).resolve().parents[3]
BUNDLED_ROOT = PROJECT_ROOT / "pptagent" / "templates"


def test_bundled_templates_resolve_with_an_empty_user_store(tmp_path: Path) -> None:
    """A clean install must discover built-ins without populating user storage."""

    empty_user_root = tmp_path / "templates"
    empty_user_root.mkdir()
    store = TemplateStore([empty_user_root, BUNDLED_ROOT])

    for template_id in BUNDLED_TEMPLATE_IDS:
        revision = store.resolve(template_id)
        manifest = json.loads(
            (BUNDLED_ROOT / template_id / "manifest.json").read_text(encoding="utf-8")
        )

        assert manifest["status"] == "ready"
        assert manifest["active_revision_id"] == revision.revision_id
        assert revision.metadata["status"] == "ready"
        assert revision.metadata["template_id"] == template_id
        assert manifest["slide_count"] == len(revision.load_slide_index())
        assert revision.load_theme()
        assert revision.load_families()


def test_bundled_active_revisions_contain_browser_ready_context() -> None:
    """Every bundled page and reusable asset must be usable after installation."""

    store = TemplateStore(BUNDLED_ROOT)
    for template_id in BUNDLED_TEMPLATE_IDS:
        revision = store.resolve(template_id)

        for slide in revision.load_slide_index():
            revision.resolve_path(revision.slide_artifact_path(slide, "semantic"))
            revision.resolve_path(revision.slide_artifact_path(slide, "compact"))
            revision.resolve_path(revision.slide_artifact_path(slide, "reference"))
            revision.resolve_path(revision.slide_artifact_path(slide, "overlay"))

        for asset in revision.load_assets():
            # ``reference_only`` content is already visible in the rendered page
            # image and is intentionally not copied into generation workspaces.
            if asset.get("reuse_policy") == "reference_only":
                continue
            browser_path = asset.get("browser_path") or asset["path"]
            resolved = revision.resolve_path(browser_path)
            assert resolved.suffix.lower() in {
                ".avif",
                ".gif",
                ".jpeg",
                ".jpg",
                ".png",
                ".svg",
                ".webp",
            }


def test_bundled_manifests_reference_only_present_revisions() -> None:
    """Do not ship a manifest that points at a build-cache-only revision."""

    for template_id in BUNDLED_TEMPLATE_IDS:
        template_root = BUNDLED_ROOT / template_id
        manifest = json.loads(
            (template_root / "manifest.json").read_text(encoding="utf-8")
        )
        revision_id = manifest["active_revision_id"]

        assert (template_root / "revisions" / revision_id / "revision.json").is_file()
        assert not list((template_root / "revisions").glob("*.staging"))
