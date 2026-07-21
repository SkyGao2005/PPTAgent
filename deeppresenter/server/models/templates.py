"""模板相关 Pydantic 模型 —— 需与 C 组统一。

定义 TemplateManifest、TemplateStatus、TemplateErrorCode 等核心模型，
供 routes、services 和外部使用。
"""

import json
import uuid
from datetime import datetime
from enum import Enum
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field


# =============================================================================
# 枚举
# =============================================================================


class TemplateStatus(str, Enum):
    PARSING = "parsing"
    READY = "ready"
    FAILED = "failed"


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


# =============================================================================
# 核心模型
# =============================================================================


class TemplateManifest(BaseModel):
    """模板元信息 —— 存于 workspace/templates/<template_id>/manifest.json"""

    template_id: str = Field(default_factory=lambda: str(uuid.uuid4())[:8])
    name: str
    status: TemplateStatus = TemplateStatus.PARSING
    source_hash: str = ""  # SHA256 of original.pptx
    aspect_ratio: Optional[str] = None  # "16:9" | "4:3"
    slide_count: int = 0
    layout_count: int = 0
    thumbnail: Optional[str] = None  # relative path inside template dir
    created_at: str = Field(default_factory=lambda: datetime.now().isoformat())
    error: Optional[str] = None

    # P1 补充字段
    primary_color: Optional[str] = None
    fonts: list[str] = Field(default_factory=list)

    @classmethod
    def load(cls, path: Path | str) -> "TemplateManifest":
        """从 manifest.json 加载"""
        if isinstance(path, str):
            path = Path(path)
        manifest_path = path / "manifest.json" if path.is_dir() else path
        return cls(**json.loads(manifest_path.read_text(encoding="utf-8")))

    def save(self, path: Path | str) -> None:
        """保存到 manifest.json"""
        if isinstance(path, str):
            path = Path(path)
        target = path / "manifest.json" if path.is_dir() else path
        target.write_text(
            json.dumps(self.model_dump(), ensure_ascii=False, indent=2),
            encoding="utf-8",
        )


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

    max_file_size: int = 50 * 1024 * 1024  # 50 MB
    max_slide_count: int = 100
    allowed_extensions: set[str] = {".pptx"}
    induction_timeout: int = 600  # 10 分钟超时

    class Config:
        env_prefix = "TEMPLATE_"
