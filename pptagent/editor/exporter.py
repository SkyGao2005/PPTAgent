"""Optimized PPTX export with progress tracking, validation, and recovery.

Wraps ``Presentation.save()`` with pre-save validation, format
compatibility checks, incremental progress reporting, and partial
save on failure.
"""

import os
import time
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pptagent.presentation.presentation import Presentation, SlidePage, Picture


# ── validation warnings ───────────────────────────────────────

@dataclass
class ExportWarning:
    """A non-fatal issue detected during pre-save validation."""
    slide_idx: int
    severity: str  # "info" | "warning" | "critical"
    category: str  # "image_size" | "missing_image" | "text_overflow" | "font"
    message: str

    def __str__(self):
        return f"[{self.severity}] 第{self.slide_idx}页 [{self.category}] {self.message}"


@dataclass
class ExportResult:
    """Full result of an optimized export operation."""
    output_path: str
    file_size_bytes: int = 0
    total_slides: int = 0
    duration_seconds: float = 0.0
    warnings: list[ExportWarning] = field(default_factory=list)
    success: bool = True
    error_message: str | None = None

    def summary(self) -> str:
        parts = [
            f"导出{'成功' if self.success else '失败'}",
            f"输出: {self.output_path}",
            f"{self.total_slides}页",
            f"{format_file_size(self.file_size_bytes)}",
            f"耗时 {self.duration_seconds:.1f}s",
        ]
        if self.warnings:
            parts.append(f"{len(self.warnings)}个警告")
        return " | ".join(parts)

    def warning_details(self) -> str:
        if not self.warnings:
            return "无警告"
        lines = []
        for w in self.warnings:
            lines.append(f"  {w}")
        return "\n".join(lines)


# ── export options ────────────────────────────────────────────

@dataclass
class ExportOptions:
    """Configurable options for optimized export."""

    max_image_width: int | None = 1920
    """Resize images wider than this (px). None = no limit."""

    max_image_height: int | None = 1080
    """Resize images taller than this (px). None = no limit."""

    validate_images_exist: bool = True
    """Warn if referenced images are missing on disk."""

    validate_text_bounds: bool = False
    """Warn if text content is very long for its expected size."""

    compress_output: bool = False
    """Apply python-pptx compression after save (re-opens and re-saves)."""

    partial_save_on_error: bool = True
    """If one slide fails to build, save what we have so far."""

    progress_callback: Callable[[int, int, str], None] | None = None
    """Called as (current, total, slide_title) during export."""

    def to_dict(self) -> dict:
        return {
            k: v
            for k, v in self.__dict__.items()
            if k != "progress_callback"
        }


# ── image compression ─────────────────────────────────────────

def _compress_slide_images(slide: SlidePage, slide_idx: int,
                           options: "ExportOptions") -> list[ExportWarning]:
    """Resize oversized images on a slide before export.

    Returns warnings about images that were resized.
    """
    ws: list[ExportWarning] = []
    if options.max_image_width is None and options.max_image_height is None:
        return ws

    from pptagent.presentation.shapes import Picture
    from PIL import Image

    for shape in slide.shapes:
        if not isinstance(shape, Picture):
            continue
        img_path = getattr(shape, "img_path", None)
        if not img_path or not os.path.exists(str(img_path)):
            continue

        try:
            with Image.open(str(img_path)) as img:
                orig_w, orig_h = img.size
                new_w, new_h = orig_w, orig_h
                resized = False

                if options.max_image_width and orig_w > options.max_image_width:
                    ratio = options.max_image_width / orig_w
                    new_w = options.max_image_width
                    new_h = int(orig_h * ratio)
                    resized = True

                if options.max_image_height and new_h > options.max_image_height:
                    ratio = options.max_image_height / new_h
                    new_h = options.max_image_height
                    new_w = int(new_w * ratio)
                    resized = True

                if resized:
                    img_resized = img.resize((new_w, new_h), Image.LANCZOS)
                    img_resized.save(str(img_path))
                    ws.append(ExportWarning(
                        slide_idx=slide_idx + 1,
                        severity="info",
                        category="image_size",
                        message=(
                            f"图片已压缩 {os.path.basename(str(img_path))}: "
                            f"{orig_w}x{orig_h} → {new_w}x{new_h} "
                            f"({100 - int(new_w * new_h / (orig_w * orig_h) * 100)}% 缩小)"
                        ),
                    ))
        except Exception:
            pass  # cannot read image; skip compression

    return ws


# ── font / format validation ──────────────────────────────────

# Fonts known to embed correctly in PPTX (non-exhaustive)
SAFE_LATIN_FONTS = {
    "arial", "calibri", "times new roman", "courier new", "verdana",
    "georgia", "consolas", "helvetica", "tahoma", "trebuchet ms",
    "comic sans ms", "impact", "segoe ui", "cambria",
}

SAFE_CJK_FONTS = {
    "simsun", "simhei", "microsoft yahei", "microsoft jhenghei",
    "pmingliu", "mingliu", "dfkai-sb", "fangsong", "kaiu",
    "noto sans cjk", "noto serif cjk", "source han sans",
    "wenquanyi micro hei", "wenquanyi zen hei",
}


def _validate_fonts(slide: SlidePage, slide_idx: int) -> list[ExportWarning]:
    """Check for fonts that may not embed correctly in PPTX."""
    ws: list[ExportWarning] = []
    seen_fonts: set[str] = set()

    for shape in slide.shapes:
        if not hasattr(shape, "font"):
            continue
        font_name = getattr(shape.font, "name", None)
        if not font_name:
            continue
        name_lower = font_name.lower().strip()
        if name_lower in seen_fonts:
            continue
        seen_fonts.add(name_lower)

        safe = SAFE_LATIN_FONTS | SAFE_CJK_FONTS
        if name_lower not in safe:
            ws.append(ExportWarning(
                slide_idx=slide_idx + 1,
                severity="warning",
                category="font",
                message=(
                    f"字体 「{font_name}」不在已知兼容列表中。"
                    f"导出到其他设备时可能显示为默认字体。"
                    f"建议使用常见字体: {', '.join(sorted(SAFE_CJK_FONTS)[:4])}..."
                ),
            ))

    return ws


# ── pre-save validation ───────────────────────────────────────

def _validate_slide(slide: SlidePage, slide_idx: int,
                    options: ExportOptions) -> list[ExportWarning]:
    """Check a slide for common export issues before saving."""
    ws: list[ExportWarning] = []

    for shape in slide.shapes:
        # Missing images
        if isinstance(shape, Picture) and options.validate_images_exist:
            img_path = getattr(shape, "img_path", None)
            if img_path and not os.path.exists(str(img_path)):
                ws.append(ExportWarning(
                    slide_idx=slide_idx + 1,
                    severity="critical",
                    category="missing_image",
                    message=f"图片文件不存在: {img_path}",
                ))

        # Very large text
        if hasattr(shape, "text_frame") and shape.text_frame.is_textframe:
            if options.validate_text_bounds:
                for para in shape.text_frame.paragraphs:
                    if para.idx != -1 and len(para.text) > 5000:
                        ws.append(ExportWarning(
                            slide_idx=slide_idx + 1,
                            severity="warning",
                            category="text_overflow",
                            message=f"段落文本过长 ({len(para.text)}字)，可能超出文本框",
                        ))

    return ws


def _validate_presentation(prs: Presentation,
                           options: ExportOptions) -> list[ExportWarning]:
    """Pre-save validation across all slides."""
    all_warnings: list[ExportWarning] = []
    for i, slide in enumerate(prs.slides):
        all_warnings.extend(_validate_slide(slide, i, options))
    return all_warnings


# ── optimized save ────────────────────────────────────────────

def optimized_save(
    prs: Presentation,
    output_path: str | Path,
    options: ExportOptions | None = None,
) -> ExportResult:
    """Save a presentation with pre-save validation and progress tracking.

    Args:
        prs:         The Presentation to save.
        output_path: Output file path (.pptx).
        options:     Export options. Uses sensible defaults if None.

    Returns:
        ExportResult with summary, warnings, and timing.
    """
    if options is None:
        options = ExportOptions()

    output_path = str(output_path)
    start_time = time.time()
    result = ExportResult(
        output_path=output_path,
        total_slides=len(prs.slides),
    )

    # ── Phase 1: pre-save validation ──
    result.warnings = _validate_presentation(prs, options)

    # ── Phase 1.5: font compatibility check ──
    for i, slide in enumerate(prs.slides):
        result.warnings.extend(_validate_fonts(slide, i))

    # ── Phase 1.6: image compression ──
    if options.max_image_width or options.max_image_height:
        for i, slide in enumerate(prs.slides):
            result.warnings.extend(_compress_slide_images(slide, i, options))

    criticals = [w for w in result.warnings if w.severity == "critical"]
    if criticals:
        # Critical warnings exist but we still try to save;
        # the caller can choose to abort based on result.
        pass

    # ── Phase 2: incremental build & save ──
    try:
        prs.clear_slides()
    except Exception as e:
        result.success = False
        result.error_message = f"clear_slides() 失败: {e}"
        result.duration_seconds = time.time() - start_time
        return result

    total = len(prs.slides)
    failed_slides: list[int] = []

    for i, slide in enumerate(prs.slides):
        slide_num = i + 1

        # Progress notification
        if options.progress_callback:
            options.progress_callback(slide_num, total,
                                      slide.slide_title or f"Slide {slide_num}")

        try:
            prs.build_slide(slide)
        except Exception as e:
            failed_slides.append(slide_num)
            result.warnings.append(ExportWarning(
                slide_idx=slide_num,
                severity="critical",
                category="build_error",
                message=f"构建失败: {e}",
            ))
            if options.partial_save_on_error:
                # Continue with remaining slides
                continue
            else:
                result.success = False
                result.error_message = f"第{slide_num}页构建失败: {e}"
                break

    # ── Phase 3: write file ──
    try:
        output_dir = os.path.dirname(output_path)
        if output_dir:
            os.makedirs(output_dir, exist_ok=True)
        prs.prs.save(output_path)
        result.file_size_bytes = os.path.getsize(output_path)
    except Exception as e:
        result.success = False
        result.error_message = f"文件写入失败: {e}"
        result.duration_seconds = time.time() - start_time
        return result

    # ── post-save ──
    if options.compress_output and result.success:
        _compress_pptx(output_path)
        result.file_size_bytes = os.path.getsize(output_path)

    result.duration_seconds = time.time() - start_time
    if failed_slides and not options.partial_save_on_error:
        result.success = False
        result.error_message = f"部分页面构建失败: {failed_slides}"

    return result


def _compress_pptx(file_path: str) -> None:
    """Minimal PPTX compression: re-opens and re-saves.

    python-pptx's native save applies ZIP compression. Re-saving
    through a fresh load cycle can reduce file size by ~10-20%
    when many edits have been applied in-memory.
    """
    try:
        from pptagent_pptx import Presentation as RawPrs
        prs = RawPrs(file_path)
        prs.save(file_path)
    except Exception:
        pass  # compression is best-effort


# ── helpers ───────────────────────────────────────────────────

def format_file_size(size_bytes: int) -> str:
    """Format bytes into human-readable form."""
    if size_bytes < 1024:
        return f"{size_bytes} B"
    elif size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    elif size_bytes < 1024 * 1024 * 1024:
        return f"{size_bytes / (1024 * 1024):.1f} MB"
    return f"{size_bytes / (1024 * 1024 * 1024):.2f} GB"


# ── export profiles ───────────────────────────────────────────

class ExportProfile:
    """Predefined export configurations for common scenarios.

    Usage::

        options = ExportProfile.print_quality()
        result = optimized_save(prs, "output.pptx", options)
    """

    @staticmethod
    def presentation() -> "ExportOptions":
        """Default: balanced quality and size, suitable for screen display."""
        return ExportOptions(
            max_image_width=1920,
            max_image_height=1080,
            validate_images_exist=True,
            validate_text_bounds=False,
            compress_output=False,
            partial_save_on_error=True,
        )

    @staticmethod
    def print_quality() -> "ExportOptions":
        """High-resolution: no image resizing, strict validation, for print."""
        return ExportOptions(
            max_image_width=None,
            max_image_height=None,
            validate_images_exist=True,
            validate_text_bounds=True,
            compress_output=True,
            partial_save_on_error=False,
        )

    @staticmethod
    def web_optimized() -> "ExportOptions":
        """Compressed: smaller images, full validation, for web sharing."""
        return ExportOptions(
            max_image_width=1280,
            max_image_height=720,
            validate_images_exist=True,
            validate_text_bounds=True,
            compress_output=True,
            partial_save_on_error=True,
        )

    @staticmethod
    def draft() -> "ExportOptions":
        """Quick export: no validation, no compression, for internal review."""
        return ExportOptions(
            max_image_width=None,
            max_image_height=None,
            validate_images_exist=False,
            validate_text_bounds=False,
            compress_output=False,
            partial_save_on_error=True,
        )

    @staticmethod
    def all_profiles() -> dict[str, "ExportOptions"]:
        return {
            "演示 (presentation)": ExportProfile.presentation(),
            "打印 (print)": ExportProfile.print_quality(),
            "网页 (web)": ExportProfile.web_optimized(),
            "草稿 (draft)": ExportProfile.draft(),
        }


def export_with_profile(
    prs: Presentation,
    output_path: str | Path,
    profile: str = "presentation",
) -> ExportResult:
    """Export using a named profile.  Available: presentation, print, web, draft."""
    profiles = ExportProfile.all_profiles()
    key_map = {
        "presentation": "演示 (presentation)",
        "print": "打印 (print)",
        "web": "网页 (web)",
        "draft": "草稿 (draft)",
    }
    name = key_map.get(profile, profile)
    if name not in profiles:
        raise KeyError(f"未知导出方案: {profile}。可用: {list(key_map.keys())}")
    return optimized_save(prs, output_path, profiles[name])


def export_with_report(
    prs: Presentation,
    output_path: str | Path,
    options: ExportOptions | None = None,
) -> ExportResult:
    """Export and print a full report to stdout."""
    result = optimized_save(prs, output_path, options)
    print(result.summary())
    if result.warnings:
        print(result.warning_details())
    if not result.success:
        print(f"\n错误: {result.error_message}")
    return result
