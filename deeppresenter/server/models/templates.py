"""模板相关 Pydantic 模型 —— 需与 C 组统一。

定义 TemplateManifest、TemplateStatus、TemplateErrorCode 等核心模型，
供 routes、services 和外部使用。
"""

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

from deeppresenter.templates.models import (
    TemplateManifest as TemplateManifest,
    TemplateStatus as TemplateStatus,
)


# =============================================================================
# 枚举
# =============================================================================


class AspectRatio(str, Enum):
    WIDE = "16:9"
    STANDARD = "4:3"


class TemplateErrorCode(str, Enum):
    """与 C 组统一错误码"""

    FILE_TOO_LARGE = "FILE_TOO_LARGE"
    UNSUPPORTED_FORMAT = "UNSUPPORTED_FORMAT"
    CORRUPTED_FILE = "CORRUPTED_FILE"
    ENCRYPTED_FILE = "ENCRYPTED_FILE"
    PARSE_FAILED = "PARSE_FAILED"
    DUPLICATE_TEMPLATE = "DUPLICATE_TEMPLATE"
    TEMPLATE_NOT_FOUND = "TEMPLATE_NOT_FOUND"
    INVALID_PAGE_COUNT = "INVALID_PAGE_COUNT"
    LIBREOFFICE_CONVERSION_FAILED = "LIBREOFFICE_CONVERSION_FAILED"
    ALREADY_PARSING = "ALREADY_PARSING"
    INVALID_STATE = "INVALID_STATE"


class TemplateErrorResponse(BaseModel):
    """统一错误响应"""
    code: TemplateErrorCode
    message: str
    details: Optional[dict] = None


class TemplateSuccessResponse(BaseModel):
    """统一成功响应"""
    template_id: str
    manifest: TemplateManifest


class TemplateListResponse(BaseModel):
    """模板列表响应"""
    templates: list[TemplateManifest]
    count: int


class TemplatePalette(BaseModel):
    """Frontend-safe palette derived from one compiled Template IR revision."""

    model_config = ConfigDict(extra="forbid")

    bg: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    surface: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    primary: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    accent: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    ink: str = Field(pattern=r"^#[0-9A-Fa-f]{6}$")
    dark: bool


class TemplateSummary(BaseModel):
    """Stable response shared by template upload, list, and detail endpoints."""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str
    owner: Literal["system", "user"]
    status: Literal["ready", "parsing", "failed"]
    progress: int = Field(ge=0, le=100)
    error: str | None = None
    slides: int = Field(ge=0)
    ratio: Literal["16:9", "4:3"]
    layouts: list[str]
    palette: TemplatePalette
    revision_id: str | None = None
    thumbnail_url: str | None = None


# =============================================================================
# 进度事件模型（与 C 组统一 GenerationEvent）
# =============================================================================


class GenerationEvent(BaseModel):
    """全项目统一生成事件"""

    event: str  # template.parse_started | template.parse_progress | template.ready | template.failed
    template_id: str
    progress: float = 0.0  # 0.0 ~ 1.0
    stage: str = ""  # file_validated | structure_parsed | layout_clustered | ...
    error: Optional[str] = None
    timestamp: str = Field(default_factory=lambda: datetime.now().isoformat())


# =============================================================================
# 配置
# =============================================================================


class TemplateSettings(BaseModel):
    """模板相关配置（可从环境变量覆盖）"""

    model_config = ConfigDict(validate_default=True)

    max_file_size: int = 50 * 1024 * 1024  # 50 MB
    max_slide_count: int = 100
    allowed_extensions: set[str] = {".pptx"}
    induction_timeout: int = 600  # 10 分钟超时
