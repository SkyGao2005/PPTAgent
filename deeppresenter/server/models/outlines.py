"""Persistent manuscript-review models used before slide generation starts."""

from __future__ import annotations

from enum import StrEnum
from typing import Optional

from pydantic import BaseModel, Field


class OutlineStatus(StrEnum):
    """Lifecycle of a reviewable Research manuscript."""

    GENERATING = "generating"
    READY = "ready"
    FAILED = "failed"
    APPROVED = "approved"


class OutlineComment(BaseModel):
    """User feedback appended to the Research conversation."""

    comment_id: str
    text: str
    target_revision: int = Field(ge=1)
    created_at: str


class OutlineDraftRecord(BaseModel):
    """On-disk manuscript draft, including private generation parameters."""

    outline_id: str
    status: OutlineStatus
    topic: str
    requested_page_count: int = Field(ge=1)
    ratio: str
    language: str
    attachment_paths: list[str] = Field(default_factory=list)
    template: Optional[str] = None
    template_id: Optional[str] = None
    template_revision_id: Optional[str] = None
    template_context_path: Optional[str] = None
    convert_type: Optional[str] = None
    revision: int = Field(default=0, ge=0)
    comments: list[OutlineComment] = Field(default_factory=list)
    manuscript_path: Optional[str] = None
    task_id: Optional[str] = None
    error_message: Optional[str] = None
    created_at: str
    updated_at: str


class OutlineDraftResponse(BaseModel):
    """Reader-facing manuscript state; private filesystem paths stay server-side."""

    outline_id: str
    status: OutlineStatus
    topic: str
    page_count: int
    ratio: str
    template_id: Optional[str] = None
    revision: int
    last_seq: int = Field(default=0, ge=0)
    manuscript: str
    comments: list[OutlineComment]
    task_id: Optional[str] = None
    error_message: Optional[str] = None
    created_at: str
    updated_at: str


class OutlineApprovalResponse(BaseModel):
    """Formal task created from an approved Research manuscript."""

    task_id: str
    outline_id: str
    status: str
