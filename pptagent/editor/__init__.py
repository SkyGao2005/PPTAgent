"""PPTAgent Editor - Local slide editing and version management.

This package layers two capabilities on top of ``pptagent``:

1. **Programmatic single-slide editing** (``SlideEditor``) and
   full-presentation git-like versioning (``VersionManager``) — the
   original element-level editing surface.
2. **Conversational local editing + per-page version management**
   (direction D): natural-language edits per page, a per-page revision
   model (keep last 10, undo = pointer switch, failed drafts never
   promote), an edit event bus, and two generation modes.

The conversational layer is the public surface for direction D::

    from pptagent.editor import build_edit_service
    service = build_edit_service(presentation, workspace_root="...")
    await service.chat(slide_id, "把标题改短一点")
    await service.undo(slide_id)
    await service.export("out.pptx")
"""

# element-level editing + presentation-level versioning (existing)
from pptagent.editor.editor import SlideEditor
from pptagent.editor.version import VersionManager, VersionNode
from pptagent.editor.version_store import VersionStore
from pptagent.editor.snapshot import SlideSnapshot, PresentationSnapshot
from pptagent.editor.history import EditHistory, EditRecord
from pptagent.editor.preview import PreviewRenderer, PreviewResult
from pptagent.editor.exporter import optimized_save, ExportOptions, export_with_profile
from pptagent.editor.styles import StyleRegistry, StyleStrategy
from pptagent.editor.diff import DiffEngine, SlideDiff
from pptagent.editor.batch import BatchEditor
from pptagent.editor.features import FeatureStore, FeatureSlice
from pptagent.editor.integration import VersionedPPTAgent, ConversationalPPTAgent, build_edit_service

# conversational local editing + per-page version management (direction D)
from pptagent.editor.artifact import (
    SlideArtifact, SlideWorkspace, TaskMeta, new_slide_id,
)
from pptagent.editor.revision_store import RevisionStore, RevisionInfo
from pptagent.editor.conversation import ConversationLog, ConversationTurn
from pptagent.editor.events import (
    GenerationEvent, EventBus, TaskEventRegistry, global_registry,
    new_task_id, EVENT_TYPES,
)
from pptagent.editor.edit_context import EditContext, build_edit_context
from pptagent.editor.edit_plan import (
    EditOp, EditPlan, EditPlanner, DeterministicPlanner, parse_edit_plan,
)
from pptagent.editor.edit_executor import EditPlanExecutor, EditResult
from pptagent.editor.llm_planner import LLMEditPlanner
from pptagent.editor.regen_backend import (
    EditBackend, TemplateRegenBackend, HTMLEditBackend, NoopRegenBackend,
    RegenUnavailableError,
)
from pptagent.editor.slide_edit_service import SlideEditService, ChatResponse

__all__ = [
    # element-level / presentation-level
    "SlideEditor", "VersionManager", "VersionNode", "VersionStore",
    "SlideSnapshot", "PresentationSnapshot", "EditHistory", "EditRecord",
    "PreviewRenderer", "PreviewResult", "optimized_save", "ExportOptions",
    "export_with_profile", "StyleRegistry", "StyleStrategy", "DiffEngine",
    "SlideDiff", "BatchEditor", "FeatureStore", "FeatureSlice",
    "VersionedPPTAgent", "ConversationalPPTAgent", "build_edit_service",
    # direction D
    "SlideArtifact", "SlideWorkspace", "TaskMeta", "new_slide_id",
    "RevisionStore", "RevisionInfo", "ConversationLog", "ConversationTurn",
    "GenerationEvent", "EventBus", "TaskEventRegistry", "global_registry",
    "new_task_id", "EVENT_TYPES",
    "EditContext", "build_edit_context",
    "EditOp", "EditPlan", "EditPlanner", "DeterministicPlanner", "parse_edit_plan",
    "EditPlanExecutor", "EditResult",
    "LLMEditPlanner",
    "EditBackend", "TemplateRegenBackend", "HTMLEditBackend",
    "NoopRegenBackend", "RegenUnavailableError",
    "SlideEditService", "ChatResponse",
]
