"""Independent PPTX-to-Template-IR compilation service.

This module owns upload validation and compilation progress only. Presentation
generation never imports it; generation resolves an immutable READY revision
through :mod:`deeppresenter.templates.store` instead.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import re
import subprocess
import zipfile
from collections.abc import Awaitable, Callable
from pathlib import Path
from shutil import which
from typing import Any

from deeppresenter.server.models.templates import (
    GenerationEvent,
    TemplateErrorCode,
    TemplateManifest,
    TemplateSettings,
    TemplateStatus,
)
from deeppresenter.templates import TemplateCompiler, derive_template_id
from deeppresenter.templates.ids import sha256_file
from deeppresenter.templates.models import utc_now

try:
    from deeppresenter.utils.log import get_logger as _get_logger

    logger = _get_logger()
except (ImportError, TypeError):
    logger = logging.getLogger(__name__)


ProgressCallback = Callable[[dict[str, Any]], Awaitable[Any] | Any]


class PptxValidationResult:
    """Result of cheap structural validation performed before compilation."""

    def __init__(
        self,
        valid: bool,
        slide_count: int = 0,
        encrypted: bool = False,
        error: dict[str, Any] | None = None,
    ) -> None:
        self.valid = valid
        self.slide_count = slide_count
        self.encrypted = encrypted
        self.error = error


def validate_pptx(content: bytes) -> PptxValidationResult:
    """Validate ZIP integrity, encryption and slide count without a model."""

    try:
        with zipfile.ZipFile(io.BytesIO(content)) as archive:
            damaged = archive.testzip()
            if damaged is not None:
                return PptxValidationResult(
                    valid=False,
                    error={
                        "code": TemplateErrorCode.CORRUPTED_FILE,
                        "message": f"ZIP 内文件损坏: {damaged}",
                    },
                )
    except zipfile.BadZipFile:
        return PptxValidationResult(
            valid=False,
            error={
                "code": TemplateErrorCode.CORRUPTED_FILE,
                "message": "文件不是有效的 ZIP/PPTX 格式",
            },
        )

    try:
        import pptx

        presentation = pptx.Presentation(io.BytesIO(content))
    except Exception as exc:
        message = str(exc).lower()
        if "encrypted" in message or "password" in message:
            return PptxValidationResult(
                valid=False,
                encrypted=True,
                error={
                    "code": TemplateErrorCode.ENCRYPTED_FILE,
                    "message": "文件已加密，请解密后上传",
                },
            )
        return PptxValidationResult(
            valid=False,
            error={
                "code": TemplateErrorCode.CORRUPTED_FILE,
                "message": f"无法解析 PPTX: {exc}",
            },
        )

    slide_count = len(presentation.slides)
    limit = TemplateSettings().max_slide_count
    if slide_count < 1 or slide_count > limit:
        message = (
            "模板至少需要 1 页幻灯片"
            if slide_count < 1
            else f"模板页数超过 {limit} 页限制"
        )
        return PptxValidationResult(
            valid=False,
            error={
                "code": TemplateErrorCode.INVALID_PAGE_COUNT,
                "message": message,
            },
        )
    return PptxValidationResult(valid=True, slide_count=slide_count)


def sanitize_filename(filename: str) -> str:
    """Return a path-safe identifier-like filename stem."""

    stem = Path(filename).name.rsplit(".", 1)[0]
    stem = re.sub(r'[^A-Za-z0-9._\-\u4e00-\u9fff]+', "_", stem)
    stem = stem.replace("..", "_").strip(". _")[:100]
    return stem or "untitled"


class TemplateInductionService:
    """Compile uploaded PPTX files into immutable Template IR revisions."""

    def __init__(
        self,
        *,
        workspace: Path,
        compiler: TemplateCompiler | None = None,
        progress_callback: ProgressCallback | None = None,
        settings: TemplateSettings | None = None,
        **_: Any,
    ) -> None:
        self.workspace = Path(workspace)
        self.compiler = compiler or TemplateCompiler()
        self.progress_callback = progress_callback
        self.settings = settings or TemplateSettings()

    async def run_induction(
        self,
        template_id: str,
        progress_callback: ProgressCallback | None = None,
    ) -> TemplateManifest:
        """Compile one uploaded template with timeout and progress events."""

        template_dir = self.workspace / "templates" / template_id
        source = template_dir / "original.pptx"
        callback = progress_callback or self.progress_callback
        await self._report(
            callback,
            "template.parse_started",
            0.0,
            "parse_started",
            template_id,
        )
        await self._report(
            callback,
            "template.parse_progress",
            0.1,
            "file_validated",
            template_id,
        )
        try:
            if not source.is_file():
                raise FileNotFoundError(f"原始文件不存在: {source}")
            current = TemplateManifest.load(template_dir)
            if current.status is not TemplateStatus.COMPILING:
                current = current.model_copy(
                    update={
                        "status": TemplateStatus.COMPILING,
                        "error": None,
                        "updated_at": utc_now(),
                    }
                )
                current.save(template_dir)
            await self._report(
                callback,
                "template.parse_progress",
                0.25,
                "extracting_source_graph",
                template_id,
            )
            manifest = await asyncio.wait_for(
                self.compiler.compile(
                    template_id,
                    source,
                    template_dir,
                    name=current.name,
                ),
                timeout=self.settings.induction_timeout,
            )
        except TimeoutError:
            message = f"解析超时 ({self.settings.induction_timeout}s)"
            manifest = self._write_failed_manifest(template_id, template_dir, message)
            await self._report(
                callback,
                "template.failed",
                0.0,
                "timeout",
                template_id,
                error=message,
            )
            return manifest
        except Exception as exc:
            logger.exception("Template IR compilation failed: %s", template_id)
            manifest = self._write_failed_manifest(template_id, template_dir, str(exc))
            await self._report(
                callback,
                "template.failed",
                0.0,
                "failed",
                template_id,
                error=str(exc),
            )
            return manifest

        await self._report(
            callback,
            "template.parse_progress",
            1.0,
            "completed",
            template_id,
        )
        await self._report(
            callback,
            "template.ready",
            1.0,
            "ready",
            template_id,
        )
        return manifest

    @staticmethod
    async def _report(
        callback: ProgressCallback | None,
        event: str,
        progress: float,
        stage: str,
        template_id: str,
        *,
        error: str | None = None,
    ) -> None:
        if callback is None:
            return
        payload = GenerationEvent(
            event=event,
            template_id=template_id,
            progress=progress,
            stage=stage,
            error=error,
        ).model_dump(mode="json")
        result = callback(payload)
        if asyncio.iscoroutine(result):
            await result

    @staticmethod
    def _write_failed_manifest(
        template_id: str,
        template_dir: Path,
        error: str,
    ) -> TemplateManifest:
        try:
            current = TemplateManifest.load(template_dir)
        except (OSError, ValueError, json.JSONDecodeError):
            current = TemplateManifest(
                template_id=template_id,
                name=template_id,
                status=TemplateStatus.COMPILING,
            )
        failed = current.model_copy(
            update={
                "status": TemplateStatus.FAILED,
                "error": error,
                "updated_at": utc_now(),
            }
        )
        failed.save(template_dir)
        return failed

    @staticmethod
    def convert_ppt_to_pptx(ppt_path: Path, output_path: Path) -> bool:
        """Convert legacy .ppt files with LibreOffice when available."""

        executable = which("libreoffice") or which("soffice")
        if executable is None:
            return False
        try:
            subprocess.run(
                [
                    executable,
                    "--headless",
                    "--convert-to",
                    "pptx",
                    "--outdir",
                    str(output_path.parent),
                    str(ppt_path),
                ],
                check=True,
                timeout=120,
                capture_output=True,
            )
        except (OSError, subprocess.SubprocessError):
            return False
        return output_path.exists()


async def cli_main() -> None:
    """Minimal standalone compiler entry retained for direct module use."""

    import argparse

    parser = argparse.ArgumentParser(description="Compile PPTX to Template IR")
    parser.add_argument("pptx_path", type=Path)
    parser.add_argument("--templates-root", type=Path, default=Path(".templates"))
    parser.add_argument("--template-id")
    args = parser.parse_args()

    template_id = args.template_id or derive_template_id(
        sha256_file(args.pptx_path),
        args.pptx_path.stem,
    )
    target = args.templates_root / template_id
    manifest = await TemplateCompiler().compile(
        template_id,
        args.pptx_path,
        target,
        name=args.pptx_path.stem,
    )
    print(manifest.model_dump_json(indent=2))


if __name__ == "__main__":
    asyncio.run(cli_main())
