"""PPTAgent Editor — local slide editing and version management.

This package implements direction **D** of the secondary-development plan:
*conversational local editing & version management*.

Public surface
--------------
Low-level editing (kept from the original package)
    :class:`~pptagent.editor.editor.SlideEditor`
    :class:`~pptagent.editor.snapshot.SlideSnapshot`
    :class:`~pptagent.editor.history.EditHistory`
    :class:`~pptagent.editor.styles.StyleRegistry`

Per-slide conversational editing & versioning (direction D)
    :class:`~pptagent.editor.service.SlideEditService`
    :class:`~pptagent.editor.service.EditServiceRegistry`
    :class:`~pptagent.editor.service.EditResult`
    :class:`~pptagent.editor.revision.RevisionManager`
    :class:`~pptagent.editor.artifact.SlideArtifact`
    :class:`~pptagent.editor.artifact.ArtifactStore`
    :class:`~pptagent.editor.event_bus.EditEventBus`
    :class:`~pptagent.editor.prompts.EditActionPlan`

Note: the FastAPI routes live in :mod:`pptagent.editor.api` and are imported
on demand (they require ``fastapi``); they are intentionally *not* re-exported
here so the package can be imported without a web framework installed.
"""

from pptagent.editor.artifact import ArtifactStore, SlideArtifact
from pptagent.editor.editor import SlideEditor
from pptagent.editor.event_bus import EditEventBus, get_default_bus
from pptagent.editor.history import EditHistory
from pptagent.editor.prompts import EditActionPlan
from pptagent.editor.revision import RevisionManager, SlideRevision
from pptagent.editor.service import (
    DEFAULT_REGISTRY,
    EditResult,
    EditServiceRegistry,
    SlideEditService,
)
from pptagent.editor.snapshot import PresentationSnapshot, SlideSnapshot
from pptagent.editor.styles import StyleRegistry, StyleStrategy

__all__ = [
    "SlideEditor",
    "SlideSnapshot",
    "PresentationSnapshot",
    "EditHistory",
    "StyleRegistry",
    "StyleStrategy",
    "SlideArtifact",
    "ArtifactStore",
    "RevisionManager",
    "SlideRevision",
    "EditEventBus",
    "get_default_bus",
    "EditActionPlan",
    "SlideEditService",
    "EditServiceRegistry",
    "EditResult",
    "DEFAULT_REGISTRY",
]
