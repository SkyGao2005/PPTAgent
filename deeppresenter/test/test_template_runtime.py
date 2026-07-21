from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from deeppresenter.templates.context import SlideNeed, TemplateContextProvider
from deeppresenter.templates.runtime import (
    TaskTemplateContext,
    TemplateContextConflictError,
)
from deeppresenter.templates.store import (
    LegacyTemplateError,
    TemplateIntegrityError,
    TemplateRevisionNotReadyError,
    TemplateStore,
    UnsafeTemplatePathError,
)


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _write_slide(
    revision: Path,
    slide_id: str,
    page_number: int,
    page_semantics: dict[str, Any],
    roles: list[str],
) -> dict[str, Any]:
    slide = revision / "slides" / slide_id
    semantic = {
        "slide_id": slide_id,
        "page_semantics": page_semantics,
        "regions": [
            {
                "region_id": f"r_{index}",
                "role": role,
                "bbox": {"x": 0.1, "y": 0.1 * index, "width": 0.8, "height": 0.1},
                "capacity": {"max_lines": 2},
                "style_ref": f"text.{role}",
            }
            for index, role in enumerate(roles, start=1)
        ],
        "asset_refs": ["brand_logo"],
    }
    compact = {
        "slide_id": slide_id,
        "page_semantics": page_semantics,
        "regions": semantic["regions"],
        "asset_refs": ["brand_logo"],
        "theme_tokens": {"surface": "#ffffff"},
    }
    _write_json(slide / "semantic.json", semantic)
    _write_json(slide / "context.compact.json", compact)
    (slide / "style_reference.webp").write_bytes(f"image-{slide_id}".encode())
    return {
        "slide_id": slide_id,
        "page_number": page_number,
        "semantic_path": f"slides/{slide_id}/semantic.json",
        "compact_context_path": f"slides/{slide_id}/context.compact.json",
        "style_reference_path": f"slides/{slide_id}/style_reference.webp",
        "family_ids": [page_semantics["layout_pattern"]],
    }


def _make_ir(root: Path, *, status: str = "ready") -> tuple[Path, str, str]:
    template_id = "brand_deck"
    revision_id = "rev_001"
    template = root / template_id
    revision = template / "revisions" / revision_id
    _write_json(
        template / "manifest.json",
        {
            "schema_version": "2.0",
            "template_id": template_id,
            "name": "Brand Deck",
            "status": "ready",
            "latest_revision_id": revision_id,
            "active_revision_id": revision_id,
            "revisions": [
                {
                    "revision_id": revision_id,
                    "source_hash": "a" * 64,
                    "slide_count": 3,
                    "created_at": "2026-07-21T00:00:00Z",
                }
            ],
        },
    )
    _write_json(
        revision / "theme" / "theme.json",
        {
            "colors": {"primary": "#123456", "surface": "#ffffff"},
            "fonts": {"heading": "Inter", "body": "Inter"},
            "brand_invariants": ["logo stays in top-right"],
        },
    )
    (revision / "theme" / "theme.css").write_text(
        ":root { --brand-primary: #123456; }", encoding="utf-8"
    )
    assets_dir = revision / "assets"
    assets_dir.mkdir(parents=True, exist_ok=True)
    (assets_dir / "logo.wmf").write_bytes(b"vector-logo")
    (assets_dir / "logo.png").write_bytes(b"logo")
    (assets_dir / "sample.png").write_bytes(b"sample")
    _write_json(
        assets_dir / "index.json",
        {
            "assets": [
                {
                    "asset_id": "brand_logo",
                    "sha256": hashlib.sha256(b"vector-logo").hexdigest(),
                    "media_type": "image/x-wmf",
                    "role": "logo",
                    "reuse_policy": "always",
                    "path": "assets/logo.wmf",
                    "browser_path": "assets/logo.png",
                    "browser_media_type": "image/png",
                    "browser_sha256": hashlib.sha256(b"logo").hexdigest(),
                    "browser_pixel_width": 160,
                    "browser_pixel_height": 80,
                },
                {
                    "asset_id": "sample_photo",
                    "sha256": hashlib.sha256(b"sample").hexdigest(),
                    "role": "photo",
                    "reuse_policy": "reference_only",
                    "path": "assets/sample.png",
                },
            ]
        },
    )
    _write_json(
        revision / "families" / "index.json",
        {
            "families": [
                {"family_id": "hero", "stage": "cover", "layout_pattern": "hero"},
                {"family_id": "split", "stage": "content", "layout_pattern": "split"},
            ]
        },
    )
    records = [
        _write_slide(
            revision,
            "s001",
            1,
            {
                "stage": "cover",
                "layout_pattern": "hero",
                "message_pattern": "opening",
                "modalities": ["text", "photo"],
                "density": "low",
            },
            ["title", "subtitle", "photo", "logo"],
        ),
        _write_slide(
            revision,
            "s002",
            2,
            {
                "stage": "content",
                "layout_pattern": "split",
                "message_pattern": "comparison",
                "modalities": ["text", "chart"],
                "density": "medium",
            },
            ["title", "body", "chart", "logo"],
        ),
        _write_slide(
            revision,
            "s003",
            3,
            {
                "stage": "content",
                "layout_pattern": "cards",
                "message_pattern": "enumeration",
                "modalities": ["text"],
                "density": "high",
            },
            ["title", "body", "body", "body", "logo"],
        ),
    ]
    index_path = revision / "slides" / "index.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n",
        encoding="utf-8",
    )

    # Runtime integrity verification must explicitly skip this provenance file.
    source = revision / "source" / "original.pptx"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"not-a-real-pptx")
    theme_hash = hashlib.sha256(
        (revision / "theme" / "theme.css").read_bytes()
    ).hexdigest()
    _write_json(
        revision / "revision.json",
        {
            "template_id": template_id,
            "revision_id": revision_id,
            "status": status,
            "schema_version": "2.0",
            "revision_hash": "compiled-revision-hash",
            "file_hashes": {
                "theme/theme.css": theme_hash,
                "source/original.pptx": "0" * 64,
            },
        },
    )
    return revision, template_id, revision_id


def test_store_resolves_only_pinned_ready_ir(tmp_path: Path) -> None:
    _, template_id, revision_id = _make_ir(tmp_path)
    revision = TemplateStore([tmp_path]).resolve(template_id)

    assert revision.revision_id == revision_id
    assert revision.schema_version == "2.0"
    assert len(revision.load_slide_index()) == 3
    assert revision.load_assets()[0]["asset_id"] == "brand_logo"


def test_store_rejects_not_ready_and_legacy_templates(tmp_path: Path) -> None:
    _, template_id, revision_id = _make_ir(tmp_path, status="parsing")
    with pytest.raises(TemplateRevisionNotReadyError):
        TemplateStore([tmp_path]).resolve(template_id, revision_id)

    legacy = tmp_path / "legacy"
    legacy.mkdir()
    (legacy / "source.pptx").write_bytes(b"legacy")
    with pytest.raises(LegacyTemplateError, match="compile it to Template IR"):
        TemplateStore([tmp_path]).resolve("legacy")


def test_store_rejects_path_escape_and_hash_mismatch(tmp_path: Path) -> None:
    revision_path, template_id, revision_id = _make_ir(tmp_path)
    index = json.loads((revision_path / "assets" / "index.json").read_text())
    index["assets"][0]["path"] = "../outside.png"
    _write_json(revision_path / "assets" / "index.json", index)
    with pytest.raises(UnsafeTemplatePathError):
        TemplateStore([tmp_path]).resolve(template_id, revision_id)

    revision_path, template_id, revision_id = _make_ir(tmp_path / "second")
    (revision_path / "theme" / "theme.css").write_text("tampered", encoding="utf-8")
    with pytest.raises(TemplateIntegrityError, match="SHA256 mismatch"):
        TemplateStore([tmp_path / "second"]).resolve(template_id, revision_id)


def test_runtime_never_opens_pptx(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _, template_id, revision_id = _make_ir(tmp_path)
    original_open = Path.open

    def guarded_open(path: Path, *args: Any, **kwargs: Any):
        if path.suffix.lower() == ".pptx":
            raise AssertionError("generation runtime opened source PPTX")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    TemplateStore([tmp_path]).resolve(template_id, revision_id)


def test_search_scores_semantics_and_bounds_results(tmp_path: Path) -> None:
    _, template_id, revision_id = _make_ir(tmp_path)
    provider = TemplateContextProvider(TemplateStore([tmp_path]))
    need = SlideNeed(
        stage="content",
        layout_pattern="split",
        message_pattern="comparison",
        modalities=("text", "chart"),
        region_roles=("title", "chart"),
    )

    matches = provider.search_references(
        template_id,
        need,
        revision_id,
        limit=2,
        max_chars_per_result=400,
    )
    reference = provider.get_reference(
        template_id,
        "s002",
        revision_id,
        max_chars=800,
    )

    assert [match["slide_id"] for match in matches] == ["s002", "s003"]
    assert all(len(json.dumps(match, ensure_ascii=False)) <= 400 for match in matches)
    assert len(json.dumps(reference, ensure_ascii=False)) <= 800
    assert reference["slide_id"] == "s002"


def test_materialize_builds_fixed_context_pack_and_assets(tmp_path: Path) -> None:
    ir_root = tmp_path / "ir"
    revision, template_id, revision_id = _make_ir(ir_root)
    provider = TemplateContextProvider(TemplateStore([ir_root]))
    task = tmp_path / "task"

    result = provider.materialize(
        template_id,
        task,
        revision_id,
        [
            {
                "slide_key": "comparison",
                "stage": "content",
                "layout_pattern": "split",
                "modalities": ["text", "chart"],
            }
        ],
    )
    context = task / "template_context"
    snapshot = json.loads((context / "snapshot.json").read_text())
    assets = json.loads((context / "assets" / "index.json").read_text())["assets"]

    assert result["revision_id"] == revision_id
    assert snapshot["revision_id"] == revision_id
    assert (context / "overview.md").exists()
    assert (context / "selection.json").exists()
    assert (context / "theme.css").exists()
    assert [asset["asset_id"] for asset in assets] == ["brand_logo"]
    assert assets[0]["media_type"] == "image/png"
    assert assets[0]["source_media_type"] == "image/x-wmf"
    assert assets[0]["path"].endswith(".png")
    assert (context / assets[0]["path"]).read_bytes() == b"logo"
    assert {item["slide_id"] for item in snapshot["references"]} == {
        "s001",
        "s002",
        "s003",
    }
    assert not list(context.rglob("*.pptx"))

    task_context = TaskTemplateContext.load(
        context,
        expected_template_id=template_id,
        expected_revision_id=revision_id,
    )
    assert (
        task_context.search_references(SlideNeed(stage="cover", layout_pattern="hero"))[
            0
        ]["slide_id"]
        == "s001"
    )

    # A task workspace is writable, so copied files must not alias immutable IR.
    staged_logo = context / assets[0]["path"]
    staged_logo.write_bytes(b"tampered")
    assert (revision / "assets" / "logo.png").read_bytes() == b"logo"
    assert (revision / "assets" / "logo.wmf").read_bytes() == b"vector-logo"
    with pytest.raises(TemplateContextConflictError, match="artifact changed"):
        TaskTemplateContext.load(context)

    # Identical calls are idempotent; a different page request cannot silently
    # overwrite an already pinned task context.
    staged_logo.write_bytes(b"logo")
    again = provider.materialize(
        template_id,
        task,
        revision_id,
        [
            {
                "slide_key": "comparison",
                "stage": "content",
                "layout_pattern": "split",
                "modalities": ["text", "chart"],
            }
        ],
    )
    assert again["snapshot"]["request_hash"] == snapshot["request_hash"]
    with pytest.raises(TemplateContextConflictError):
        provider.materialize(
            template_id,
            task,
            revision_id,
            [{"stage": "cover", "layout_pattern": "hero"}],
        )
