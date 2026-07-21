"""模板解析服务 —— 可被 API、测试、CLI 共同调用。

从 pptagent/scripts/template_induct.py 抽取主逻辑，添加：
- 进度回调（P0-4）
- 文件校验（P0-2）
- 缩略图生成（P0-5）
- 失败清理（P0-2）
- 超时保护
"""

import asyncio
import hashlib
import io
import json
import logging
import re
import shutil
import zipfile
from pathlib import Path
from typing import Callable, Optional

# Try to import pptagent dependencies — they may not be available
# in minimal test environments (e.g. CI without LLM SDKs).
try:
    from pptagent.induct import SlideInducter  # noqa: F401
    from pptagent.llms import AsyncLLM  # noqa: F401
    from pptagent.multimodal import ImageLabler  # noqa: F401
    from pptagent.presentation import Presentation  # noqa: F401
    from pptagent.utils import Config, get_logger as _pptagent_get_logger  # noqa: F401
    _pptagent_available = True
except ImportError:
    _pptagent_available = False
    # Define placeholder type aliases so type hints don't fail at import time.
    # These are NOT functional — methods that use them will raise at runtime.
    AsyncLLM = None  # type: ignore
    Presentation = None  # type: ignore
    ImageLabler = None  # type: ignore
    SlideInducter = None  # type: ignore
    Config = None  # type: ignore

from deeppresenter.server.models.templates import (
    GenerationEvent,
    TemplateErrorCode,
    TemplateManifest,
    TemplateSettings,
    TemplateStatus,
)

try:
    from deeppresenter.utils.log import get_logger as _dp_get_logger
    logger = _dp_get_logger()
except (ImportError, TypeError):
    logger = logging.getLogger(__name__)

# 解析进度阶段映射
PROGRESS_STAGES = [
    (0.05, "file_validated"),
    (0.10, "ppt_converted"),
    (0.20, "structure_parsed"),
    (0.35, "slides_rendered"),
    (0.55, "layout_clustered"),
    (0.80, "schema_extracted"),
    (1.00, "completed"),
]


# =============================================================================
# 文件校验
# =============================================================================


class PptxValidationResult:
    """PPTX 文件校验结果"""

    def __init__(self, valid: bool, slide_count: int = 0,
                 encrypted: bool = False, error: Optional[dict] = None):
        self.valid = valid
        self.slide_count = slide_count
        self.encrypted = encrypted
        self.error = error


def validate_pptx(content: bytes) -> PptxValidationResult:
    """校验 PPTX 文件完整性

    检查项：
    1. ZIP 结构的可读性
    2. 加密/密码保护
    3. 幻灯片数量
    """
    # 1. ZIP 可读性
    try:
        with zipfile.ZipFile(io.BytesIO(content)) as zf:
            bad_file = zf.testzip()
            if bad_file is not None:
                return PptxValidationResult(
                    valid=False,
                    error={
                        "code": TemplateErrorCode.CORRUPTED_FILE,
                        "message": f"ZIP 内文件损坏: {bad_file}",
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

    # 2. PPTX 解析（同时检测加密和页数）
    # 使用 python-pptx 原生 Presentation 做快速校验，避免依赖
    # pptagent Presentation（需要 config 等额外参数）
    try:
        import pptx
        prs = pptx.Presentation(io.BytesIO(content))
    except Exception as e:
        error_msg = str(e).lower()
        if "encrypted" in error_msg or "password" in error_msg:
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
                "message": f"无法解析 PPTX: {str(e)}",
            },
        )

    # 3. 页数校验
    slide_count = len(prs.slides)
    if slide_count == 0:
        return PptxValidationResult(
            valid=False,
            error={
                "code": TemplateErrorCode.INVALID_PAGE_COUNT,
                "message": "模板至少需要 1 页幻灯片",
            },
        )

    settings = TemplateSettings()
    if slide_count > settings.max_slide_count:
        return PptxValidationResult(
            valid=False,
            error={
                "code": TemplateErrorCode.INVALID_PAGE_COUNT,
                "message": f"模板页数超过 {settings.max_slide_count} 页限制",
            },
        )

    return PptxValidationResult(valid=True, slide_count=slide_count)


def sanitize_filename(filename: str) -> str:
    """清洗文件名，防路径穿越

    规则：
    - 删除路径分隔符
    - 替换特殊字符为下划线
    - 截断到 100 字符
    - 空名降级为 "untitled"
    """
    import re

    # 取文件名部分（去除路径）
    name = Path(filename).name
    # 分离扩展名
    if "." in name:
        stem, ext = name.rsplit(".", 1)
    else:
        stem, ext = name, ""

    # 替换危险字符
    stem = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", stem)
    stem = stem.replace("..", "_")
    stem = stem.strip(". ") or "untitled"
    stem = stem[:100]

    return stem


# =============================================================================
# 模板解析服务
# =============================================================================


class TemplateInductionService:
    """
    模板解析服务

    将离线解析流程重构为可调用服务，支持:
    - 进度回调 → SSE 推送前端
    - 超时保护
    - 失败清理
    - 被 API、测试、CLI 共同调用
    """

    def __init__(
        self,
        language_model: AsyncLLM,
        vision_model: AsyncLLM,
        image_models: list,
        workspace: Path,
        progress_callback: Optional[Callable] = None,
        settings: Optional[TemplateSettings] = None,
    ):
        self.language_model = language_model
        self.vision_model = vision_model
        self.image_models = image_models
        self.workspace = Path(workspace)
        self.progress_callback = progress_callback
        self.settings = settings or TemplateSettings()

    # ── 主入口 ────────────────────────────────────────────────

    async def run_induction(self, template_id: str) -> TemplateManifest:
        """执行完整的模板解析流程（带超时保护）"""
        try:
            return await asyncio.wait_for(
                self._run_induction_impl(template_id),
                timeout=self.settings.induction_timeout,
            )
        except asyncio.TimeoutError:
            template_dir = self.workspace / "templates" / template_id
            manifest = TemplateManifest.load(template_dir)
            manifest.status = TemplateStatus.FAILED
            manifest.error = f"解析超时 ({self.settings.induction_timeout}s)"
            manifest.save(template_dir)

            await self._report_progress(
                "template.failed", 0.0, "timeout", template_id,
                error=manifest.error,
            )
            self._cleanup_temp_files(template_dir)
            return manifest

    async def _run_induction_impl(self, template_id: str) -> TemplateManifest:
        """解析实现 —— 7 个阶段"""
        template_dir = self.workspace / "templates" / template_id
        manifest = TemplateManifest.load(template_dir)

        try:
            # ── Stage 1: 文件校验 (5%) ──
            await self._report_progress(
                "template.parse_started", 0.0, "parse_started", template_id,
            )
            await self._report_progress(
                "template.parse_progress", 0.05, "file_validated", template_id,
            )

            original = template_dir / "original.pptx"
            if not original.exists():
                raise FileNotFoundError(f"原始文件不存在: {original}")

            # ── Stage 2: 规范化 (10% → 20%) ──
            await self._report_progress(
                "template.parse_progress", 0.10, "normalizing", template_id,
            )

            source_path = template_dir / "source.pptx"
            if not source_path.exists():
                shutil.copy(original, source_path)

            config = Config(str(template_dir))
            prs = Presentation.from_file(str(source_path), config)

            manifest.slide_count = len(prs.slides)
            manifest.aspect_ratio = self._detect_aspect_ratio(prs)
            manifest.save(template_dir)

            await self._report_progress(
                "template.parse_progress", 0.20, "structure_parsed", template_id,
            )

            # ── Stage 3: 页面渲染 (20% → 35%) ──
            slide_images_dir = template_dir / "slide_images"
            template_images_dir = template_dir / "template_images"
            slide_images_dir.mkdir(exist_ok=True)
            template_images_dir.mkdir(exist_ok=True)

            await self._render_slides(prs, slide_images_dir, template_images_dir)

            await self._report_progress(
                "template.parse_progress", 0.35, "slides_rendered", template_id,
            )

            # ── Stage 4: 图片标注 (35%) ──
            # 标注失败不阻塞解析。标注后统一给未标注图片设置 fallback caption
            from pptagent.presentation import Picture as PptPicture
            image_labler = ImageLabler(prs, config)
            try:
                await image_labler.caption_images_async(self.vision_model)
            except BaseException as e:
                # Python 3.11+ TaskGroup 抛 ExceptionGroup（不继承 Exception）
                logger.warning(f"Image captioning skipped: {e}")
            finally:
                # 确保所有图片都有 caption（标注失败的用默认值）
                import os as _os
                for slide in prs.slides:
                    for shape in slide.shape_filter(PptPicture):
                        if getattr(shape, "caption", None) is None:
                            shape.caption = shape.semantic_name or "Picture"
                        # 同步回 image_stats，确保序列化后 caption 不丢失
                        img_key = _os.path.basename(shape.img_path)
                        if img_key in image_labler.image_stats and img_key != "pic_placeholder.png":
                            image_labler.image_stats[img_key]["caption"] = shape.caption
            image_stats = image_labler.image_stats
            (template_dir / "image_stats.json").write_text(
                json.dumps(image_stats, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            # ── Stage 5: 布局分类与聚类 (35% → 55%) ──
            inducter = SlideInducter(
                prs=prs,
                ppt_image_folder=str(slide_images_dir),
                template_image_folder=str(template_images_dir),
                config=config,
                image_models=self.image_models,
                language_model=self.language_model,
                vision_model=self.vision_model,
                use_assert=False,  # 允许 slide_images vs slides 数量不一致（如含隐藏页的 PPTX）
                progress_callback=self._make_induct_callback(template_id, 0.35, 0.55),
            )
            layout_induction = await inducter.layout_induct()

            await self._report_progress(
                "template.parse_progress", 0.55, "layout_clustered", template_id,
            )

            # ── Stage 6: Content Schema 抽取 (55% → 80%) ──
            inducter.progress_callback = self._make_induct_callback(
                template_id, 0.55, 0.80
            )
            layout_induction = await inducter.content_induct(layout_induction)

            # 计算 layout_count（排除 functional_keys 和 language）
            manifest.layout_count = len([
                k for k in layout_induction
                if k not in ("functional_keys", "language")
                and isinstance(layout_induction[k], dict)
                and "content_schema" in layout_induction[k]
            ])

            await self._report_progress(
                "template.parse_progress", 0.80, "schema_extracted", template_id,
            )

            # 保存 slide_induction.json
            (template_dir / "slide_induction.json").write_text(
                json.dumps(layout_induction, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )

            # ── Stage 7: 缩略图 + manifest + 完成 (80% → 100%) ──
            thumbnail_path = await self._generate_thumbnail(
                prs, template_dir, slide_images_dir
            )
            if thumbnail_path:
                manifest.thumbnail = str(thumbnail_path.relative_to(template_dir))

            manifest.status = TemplateStatus.READY
            manifest.aspect_ratio = self._detect_aspect_ratio(prs)
            manifest.layout_count = manifest.layout_count
            manifest.save(template_dir)

            await self._report_progress(
                "template.parse_progress", 1.0, "completed", template_id,
            )
            await self._report_progress(
                "template.ready", 1.0, "ready", template_id,
            )

            logger.info(f"Template induction completed: {template_id}")
            return manifest

        except BaseException as e:
            # Python 3.11+ TaskGroup raises ExceptionGroup (not a subclass of Exception)
            import traceback as _tb
            logger.error(f"Template induction failed for {template_id}: {e}\n{_tb.format_exc()}")
            manifest.status = TemplateStatus.FAILED
            manifest.error = str(e)
            manifest.save(template_dir)

            await self._report_progress(
                "template.failed", 0.0, "failed", template_id, error=str(e),
            )
            self._cleanup_temp_files(template_dir)
            return manifest

    # ── 进度回调 ──────────────────────────────────────────────

    async def _report_progress(
        self, event: str, progress: float, stage: str,
        template_id: str, error: str = None,
    ) -> None:
        """发布进度事件到回调"""
        if not self.progress_callback:
            return

        event_data = GenerationEvent(
            event=event,
            template_id=template_id,
            progress=progress,
            stage=stage,
            error=error,
        )
        try:
            result = self.progress_callback(event_data.model_dump())
            if asyncio.iscoroutine(result):
                await result
            elif result is not None:
                await result
        except Exception as e:
            logger.warning(f"Progress callback failed: {e}")

    def _make_induct_callback(self, template_id: str,
                               start_pct: float, end_pct: float) -> Callable:
        """为 SlideInducter 创建子进度回调"""

        async def callback(stage_name: str, sub_progress: float) -> None:
            pct = start_pct + sub_progress * (end_pct - start_pct)
            await self._report_progress(
                "template.parse_progress", pct, stage_name, template_id,
            )

        return callback

    # ── 页面渲染 ──────────────────────────────────────────────

    async def _render_slides(
        self, prs: Presentation, slide_dir: Path, template_images_dir: Path
    ) -> None:
        """渲染原始页和空布局页为图片（复用 pptagent 的 ppt_to_images）

        Args:
            slide_dir: slide_images 输出目录
            template_images_dir: template_images 输出目录
        """
        from pptagent.utils import ppt_to_images

        # 模板根目录 = slide_dir 和 template_images_dir 的父目录
        template_root = slide_dir.parent  # e.g. .../templates/blue-template/

        source_path = template_root / "source.pptx"

        # 1. 渲染原始页面 → slide_images/
        if len(list(slide_dir.glob("*"))) == 0:
            await ppt_to_images(str(source_path), str(slide_dir))

        # 2. 生成布局空页并渲染 → template_images/
        if len(list(template_images_dir.glob("*"))) == 0:
            template_pptx = template_root / "template.pptx"
            prs.save(str(template_pptx), layout_only=True)
            await ppt_to_images(str(template_pptx), str(template_images_dir))

    # ── 缩略图 ────────────────────────────────────────────────

    async def _generate_thumbnail(
        self, prs: Presentation, template_dir: Path, slide_images_dir: Path
    ) -> Optional[Path]:
        """生成缩略图（取第一页）"""
        thumb_dir = template_dir / "thumbnails"
        thumb_dir.mkdir(exist_ok=True)

        # 查找第一张幻灯片图
        slide_images = sorted(slide_images_dir.glob("slide_0001.*"))
        if not slide_images:
            slide_images = sorted(slide_images_dir.glob("slide_*.*"))
        if not slide_images:
            logger.warning(f"No slide images found in {slide_images_dir}")
            return None

        first_slide = slide_images[0]
        thumb_path = thumb_dir / "thumbnail_md.jpg"

        try:
            from PIL import Image

            img = Image.open(first_slide)
            img.thumbnail((320, 240))
            img.save(thumb_path, "JPEG", quality=85)
            return thumb_path
        except ImportError:
            logger.warning("Pillow not available, skipping thumbnail generation")
            return None

    # ── 辅助方法 ──────────────────────────────────────────────

    def _detect_aspect_ratio(self, prs: Presentation) -> str:
        """检测宽高比"""
        try:
            w = float(prs.slide_width)
            h = float(prs.slide_height)
            ratio = w / h
            return "16:9" if abs(ratio - 16 / 9) < 0.05 else "4:3"
        except Exception:
            return "16:9"

    def _cleanup_temp_files(self, template_dir: Path) -> None:
        """失败时清理临时文件，保留 original.pptx 和 manifest.json 供排查"""
        keep_files = {"original.pptx", "manifest.json"}
        for item in template_dir.iterdir():
            if item.is_file() and item.name not in keep_files:
                item.unlink(missing_ok=True)
            elif item.is_dir():
                shutil.rmtree(item, ignore_errors=True)

        # 确保 manifest 标记为 failed
        manifest_path = template_dir / "manifest.json"
        if manifest_path.exists():
            manifest = TemplateManifest.load(template_dir)
            if manifest.status != TemplateStatus.FAILED:
                manifest.status = TemplateStatus.FAILED
                manifest.save(template_dir)

    # ── P1: LibreOffice 转换 ──────────────────────────────────

    @staticmethod
    def convert_ppt_to_pptx(ppt_path: Path, output_path: Path) -> bool:
        """使用 LibreOffice 将 .ppt 转为 .pptx (P1)"""
        import subprocess
        from shutil import which

        libreoffice = which("libreoffice") or which("soffice")
        if not libreoffice:
            logger.warning("LibreOffice not found, cannot convert .ppt")
            return False

        try:
            subprocess.run(
                [
                    libreoffice,
                    "--headless",
                    "--convert-to", "pptx",
                    "--outdir", str(output_path.parent),
                    str(ppt_path),
                ],
                check=True,
                timeout=120,
                capture_output=True,
            )
            return output_path.exists()
        except Exception as e:
            logger.error(f"LibreOffice conversion failed: {e}")
            return False


# =============================================================================
# CLI 入口（保留命令行包装）
# =============================================================================


async def cli_main():
    """CLI wrapper —— 所有解析逻辑在 TemplateInductionService 中"""
    import argparse

    parser = argparse.ArgumentParser(description="Template Induction CLI")
    parser.add_argument("pptx_path", type=Path, help="Path to .pptx file")
    parser.add_argument("--workspace", type=Path,
                        default=Path(".cache/pptagent"),
                        help="Workspace directory")
    parser.add_argument("--model", default="gpt-4o", help="LLM model name")
    parser.add_argument("--api-base", help="API base URL")
    parser.add_argument("--api-key", help="API key")
    args = parser.parse_args()

    model = AsyncLLM(args.model, args.api_base, args.api_key)

    async def print_progress(event: dict):
        pct = event.get("progress", 0)
        stage = event.get("stage", "?")
        bar = "█" * int(pct * 20) + "░" * (20 - int(pct * 20))
        print(f"\r[{bar}] {pct:.0%} {stage}", end="", flush=True)
        if event["event"] in ("template.ready", "template.failed"):
            print()

    service = TemplateInductionService(
        language_model=model,
        vision_model=model,
        image_models=["openai/clip-vit-base-patch32"],
        workspace=args.workspace,
        progress_callback=print_progress,
    )

    # 复制文件到模板目录
    template_id = hashlib.sha256(args.pptx_path.read_bytes()).hexdigest()[:8]
    template_dir = args.workspace / "templates" / template_id
    template_dir.mkdir(parents=True, exist_ok=True)
    shutil.copy(args.pptx_path, template_dir / "original.pptx")

    manifest = TemplateManifest(
        template_id=template_id, name=args.pptx_path.stem,
        status=TemplateStatus.PARSING,
    )
    manifest.save(template_dir)

    result = await service.run_induction(template_id)
    print(f"\nDone: {result.model_dump_json(indent=2)}")


if __name__ == "__main__":
    asyncio.run(cli_main())
