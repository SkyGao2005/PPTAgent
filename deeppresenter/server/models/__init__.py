"""服务层数据模型 —— 事件、产物、任务状态."""

from deeppresenter.server.models.artifacts import SlideArtifact
from deeppresenter.server.models.events import (
    EventType,
    GenerationEvent,
    StageName,
    TaskStatus,
    parse_events_from_jsonl,
)
from deeppresenter.server.models.templates import TemplateManifest, TemplateStatus

__all__ = [
    "EventType",
    "GenerationEvent",
    "SlideArtifact",
    "StageName",
    "TaskStatus",
    "TemplateManifest",
    "TemplateStatus",
    "parse_events_from_jsonl",
]
