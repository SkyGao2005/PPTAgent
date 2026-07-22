from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from deeppresenter.main import AgentLoop
from deeppresenter.server.models.artifacts import task_dir
from deeppresenter.server.models.events import TaskStatus
from deeppresenter.server.services.task_manager import TaskManager
from deeppresenter.templates.context import TemplateContextProvider
from deeppresenter.templates.store import (
    TemplateAspectRatioMismatchError,
    TemplateStore,
)
from deeppresenter.utils.config import DeepPresenterConfig
from deeppresenter.utils.typings import InputRequest


def _write_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def _make_template_ir(root: Path, *, ratio: str = "16:9") -> tuple[str, str]:
    template_id = "brand_deck"
    revision_id = "rev_001"
    template = root / template_id
    revision = template / "revisions" / revision_id
    slide = revision / "slides" / "s001"

    _write_json(
        template / "manifest.json",
        {
            "schema_version": "2.0",
            "template_id": template_id,
            "name": "Brand Deck",
            "status": "ready",
            "active_revision_id": revision_id,
        },
    )
    _write_json(
        revision / "revision.json",
        {
            "schema_version": "2.0",
            "template_id": template_id,
            "revision_id": revision_id,
            "revision_hash": "compiled-revision",
            "status": "ready",
            "canvas": {"aspect_ratio": ratio},
            # Source provenance may be declared, but generation must skip it.
            "file_hashes": {"source/original.pptx": "0" * 64},
        },
    )
    _write_json(
        revision / "theme" / "theme.json",
        {
            "colors": {"primary": "#123456", "surface": "#ffffff"},
            "brand_invariants": ["keep the logo visible"],
        },
    )
    (revision / "theme" / "theme.css").write_text(
        ":root { --brand-primary: #123456; }",
        encoding="utf-8",
    )

    logo = b"reusable-logo"
    (revision / "assets").mkdir(parents=True, exist_ok=True)
    (revision / "assets" / "logo.png").write_bytes(logo)
    _write_json(
        revision / "assets" / "index.json",
        {
            "assets": [
                {
                    "asset_id": "brand_logo",
                    "path": "assets/logo.png",
                    "role": "logo",
                    "reuse_policy": "always",
                    "sha256": hashlib.sha256(logo).hexdigest(),
                }
            ]
        },
    )

    semantics = {
        "slide_id": "s001",
        "page_semantics": {
            "stage": "cover",
            "layout_pattern": "hero",
            "message_pattern": "opening",
            "modalities": ["text", "photo"],
            "density": "low",
        },
        "regions": [
            {
                "region_id": "title",
                "role": "title",
                "bbox": {"x": 0.1, "y": 0.1, "width": 0.8, "height": 0.2},
            }
        ],
        "asset_refs": ["brand_logo"],
    }
    _write_json(slide / "semantic.json", semantics)
    _write_json(
        slide / "context.compact.json",
        {
            "slide_id": "s001",
            "page_semantics": semantics["page_semantics"],
            "regions": semantics["regions"],
            "asset_refs": ["brand_logo"],
        },
    )
    (slide / "style_reference.webp").write_bytes(b"style-reference")
    index = {
        "slide_id": "s001",
        "page_number": 1,
        "semantic_path": "slides/s001/semantic.json",
        "compact_context_path": "slides/s001/context.compact.json",
        "style_reference_path": "slides/s001/style_reference.webp",
    }
    index_path = revision / "slides" / "index.jsonl"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(json.dumps(index) + "\n", encoding="utf-8")

    source = revision / "source" / "original.pptx"
    source.parent.mkdir(parents=True, exist_ok=True)
    source.write_bytes(b"source-must-never-be-read-during-generation")
    return template_id, revision_id


@pytest.mark.asyncio
async def test_real_task_rejects_ratio_that_conflicts_with_pinned_ir(
    tmp_path: Path,
) -> None:
    ir_root = tmp_path / "ir"
    template_id, _ = _make_template_ir(ir_root, ratio="4:3")
    provider = TemplateContextProvider(TemplateStore([ir_root]))
    manager = TaskManager(
        tmp_path / "tasks",
        use_placeholder=False,
        template_context_provider=provider,
    )

    with pytest.raises(TemplateAspectRatioMismatchError, match="uses 4:3"):
        await manager.create(
            instruction="Reject conflicting output geometry",
            template_id=template_id,
            powerpoint_type="16:9",
        )


@pytest.mark.asyncio
async def test_real_task_pins_revision_and_materializes_context(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ir_root = tmp_path / "ir"
    template_id, revision_id = _make_template_ir(ir_root)
    provider = TemplateContextProvider(TemplateStore([ir_root]))
    captured: dict[str, Any] = {}

    class FakeAgentLoop:
        def __init__(self, **kwargs: Any) -> None:
            captured["loop_kwargs"] = kwargs

        async def run(self, request: InputRequest):
            captured["request"] = request
            if False:
                yield "unreachable"

    monkeypatch.setattr(
        DeepPresenterConfig,
        "load_from_file",
        classmethod(lambda cls, config_path=None: object()),
    )
    monkeypatch.setattr("deeppresenter.main.AgentLoop", FakeAgentLoop)

    manager = TaskManager(
        tmp_path / "tasks",
        use_placeholder=False,
        template_context_provider=provider,
    )
    task_id = await manager.create(
        instruction="Generate from compiled template",
        template_id=template_id,
    )
    await manager._runners[task_id]

    snapshot = manager.get_snapshot(task_id)
    assert snapshot is not None
    assert snapshot.status == TaskStatus.COMPLETED
    assert snapshot.generation_params["template_revision_id"] == revision_id

    context_dir = task_dir(manager.workspace_base, task_id) / "template_context"
    context_snapshot = json.loads(
        (context_dir / "snapshot.json").read_text(encoding="utf-8")
    )
    request = captured["request"]
    assert request.template_id == template_id
    assert request.template_revision_id == revision_id
    assert request.template_context_path == str(context_dir)
    assert context_snapshot["revision_id"] == revision_id
    assert (context_dir / "assets" / "brand_logo.png").read_bytes() == b"reusable-logo"
    assert not list(context_dir.rglob("*.pptx"))
    assert "template_context_provider" not in captured["loop_kwargs"]


@pytest.mark.asyncio
async def test_task_creation_materializes_context_before_global_ir_disappears(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ir_root = tmp_path / "ir"
    template_id, revision_id = _make_template_ir(ir_root)
    provider = TemplateContextProvider(TemplateStore([ir_root]))
    start_runner = asyncio.Event()
    captured: dict[str, Any] = {}

    class FakeAgentLoop:
        def __init__(self, **kwargs: Any) -> None:
            pass

        async def run(self, request: InputRequest):
            captured["request"] = request
            if False:
                yield "unreachable"

    class GatedTaskManager(TaskManager):
        async def _run_agent_loop(self, **kwargs: Any) -> None:
            await start_runner.wait()
            await super()._run_agent_loop(**kwargs)

    monkeypatch.setattr(
        DeepPresenterConfig,
        "load_from_file",
        classmethod(lambda cls, config_path=None: object()),
    )
    monkeypatch.setattr("deeppresenter.main.AgentLoop", FakeAgentLoop)
    manager = GatedTaskManager(
        tmp_path / "tasks",
        use_placeholder=False,
        template_context_provider=provider,
    )

    task_id = await manager.create(
        instruction="Start later from a task-local context",
        template_id=template_id,
    )
    workspace = task_dir(manager.workspace_base, task_id)
    shutil.rmtree(ir_root)
    start_runner.set()
    await manager._runners[task_id]

    snapshot = manager.get_snapshot(task_id)
    assert snapshot is not None
    assert snapshot.status == TaskStatus.COMPLETED
    assert snapshot.generation_params["template_revision_id"] == revision_id
    assert (workspace / "template_context" / "snapshot.json").is_file()
    assert captured["request"].template_context_path == str(
        workspace / "template_context"
    )


@pytest.mark.asyncio
async def test_real_task_rejects_legacy_pptagent_convert_type(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        DeepPresenterConfig,
        "load_from_file",
        classmethod(lambda cls, config_path=None: object()),
    )
    manager = TaskManager(tmp_path / "tasks", use_placeholder=False)

    with pytest.raises(ValueError, match="PPTAgent layout engine"):
        await manager.create(
            instruction="Do not use the legacy layout engine",
            convert_type="pptagent",
        )


def test_template_local_tools_only_read_task_context_pack(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    ir_root = tmp_path / "ir"
    template_id, revision_id = _make_template_ir(ir_root)
    provider = TemplateContextProvider(TemplateStore([ir_root]))
    task_workspace = tmp_path / "task"
    context = provider.materialize(template_id, task_workspace, revision_id)
    registered: dict[str, Any] = {}

    class RecordingEnv:
        def register_tool(self, function: Any) -> None:
            registered[function.__name__] = function

    original_open = Path.open

    def guarded_open(path: Path, *args: Any, **kwargs: Any):
        if path.suffix.lower() == ".pptx":
            raise AssertionError("template local tool opened source PPTX")
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", guarded_open)
    loop = AgentLoop.__new__(AgentLoop)
    loop.workspace = task_workspace
    request = InputRequest(
        instruction="Use template",
        template_id=template_id,
        template_revision_id=revision_id,
        template_context_path=str(context["context_dir"]),
    )
    task_context = loop._load_task_template_context(request)
    assert task_context is not None
    shutil.rmtree(ir_root)
    loop._register_template_tools(RecordingEnv(), task_context)

    assert set(registered) == {
        "get_template_overview",
        "get_family_detail",
        "search_template_references",
        "get_template_reference",
    }
    overview = json.loads(registered["get_template_overview"]())
    matches = json.loads(
        registered["search_template_references"](
            stage="cover",
            layout_pattern="hero",
        )
    )
    reference = registered["get_template_reference"]("s001")

    assert overview["revision_id"] == revision_id
    assert matches[0]["slide_id"] == "s001"
    assert reference.isError is False
    assert len(reference.content) == 2


def test_input_request_round_trip_preserves_template_fields(tmp_path: Path) -> None:
    context_path = str(tmp_path / "task" / "template_context")
    request = InputRequest(
        instruction="Use compiled IR",
        template="brand_deck",
        template_id="brand_deck",
        template_revision_id="rev_001",
        template_context_path=context_path,
    )

    restored = InputRequest.model_validate(request.model_dump(mode="json"))

    assert restored.template == "brand_deck"
    assert restored.template_id == "brand_deck"
    assert restored.template_revision_id == "rev_001"
    assert restored.template_context_path == context_path


def test_agent_loop_requires_pinned_workspace_local_template_context(
    tmp_path: Path,
) -> None:
    loop = AgentLoop.__new__(AgentLoop)
    loop.workspace = tmp_path / "task"
    loop.workspace.mkdir()

    with pytest.raises(ValueError, match="template_revision_id"):
        loop._load_task_template_context(
            InputRequest(instruction="Use template", template_id="brand_deck")
        )
    with pytest.raises(ValueError, match="template_context_path"):
        loop._load_task_template_context(
            InputRequest(
                instruction="Use template",
                template_id="brand_deck",
                template_revision_id="rev_001",
            )
        )

    outside = tmp_path / "outside"
    outside.mkdir()
    with pytest.raises(ValueError, match="inside the task workspace"):
        loop._load_task_template_context(
            InputRequest(
                instruction="Use template",
                template_id="brand_deck",
                template_revision_id="rev_001",
                template_context_path=str(outside),
            )
        )
