"""Replaceable slide rendering and shape-overlay utilities."""

from __future__ import annotations

import asyncio
import os
import re
import shutil
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, runtime_checkable

from PIL import Image, ImageDraw, ImageFont

from .models import ShapeNode, ShapeScope, SourceGraph


@dataclass(frozen=True, slots=True)
class RenderedSlide:
    page_number: int
    image_path: Path
    media_type: str = "image/webp"


@runtime_checkable
class SlideRenderer(Protocol):
    """Renderer contract used by :class:`TemplateCompiler`."""

    @property
    def renderer_id(self) -> str:
        """Return a stable version/cache key for revision identity."""

    async def render(
        self,
        source_pptx: Path,
        output_dir: Path,
        slide_count: int,
    ) -> list[RenderedSlide]:
        """Render one image per page without modifying the source file."""


class NullRenderer:
    """Deterministic renderer for headless/offline compilation."""

    renderer_id = "none-v1"

    async def render(
        self,
        source_pptx: Path,
        output_dir: Path,
        slide_count: int,
    ) -> list[RenderedSlide]:
        del source_pptx, output_dir, slide_count
        return []


class LibreOfficeRenderer:
    """Render PPTX through LibreOffice and Poppler (via pdf2image).

    If ``required`` is false, a machine without the external binaries still
    produces valid structural IR; the validation report records missing images.
    """

    def __init__(
        self,
        *,
        required: bool = False,
        width: int = 1600,
        quality: int = 88,
    ) -> None:
        self.required = required
        self.width = width
        self.quality = quality
        self._executable, self._version = self._discover_executable()

    @property
    def renderer_id(self) -> str:
        state = "enabled" if self._executable else "unavailable"
        version = re.sub(r"[^A-Za-z0-9.]+", "-", self._version).strip("-")
        return f"libreoffice-poppler-webp-v2-{version}-{self.width}-{self.quality}-{state}"

    async def render(
        self,
        source_pptx: Path,
        output_dir: Path,
        slide_count: int,
    ) -> list[RenderedSlide]:
        output_dir.mkdir(parents=True, exist_ok=True)
        if self._executable is None:
            if self.required:
                raise RuntimeError("LibreOffice is required for template rendering")
            return []

        with tempfile.TemporaryDirectory(prefix="template-render-") as temp_name:
            temp_dir = Path(temp_name)
            profile_dir = temp_dir / "libreoffice-profile"
            profile_dir.mkdir()
            process = await asyncio.create_subprocess_exec(
                self._executable,
                f"-env:UserInstallation={profile_dir.as_uri()}",
                "--headless",
                "--convert-to",
                "pdf",
                "--outdir",
                str(temp_dir),
                str(source_pptx),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            _, stderr = await process.communicate()
            if process.returncode != 0:
                if self.required:
                    detail = stderr.decode("utf-8", errors="replace").strip()
                    raise RuntimeError(f"LibreOffice rendering failed: {detail}")
                return []
            pdf_path = temp_dir / f"{source_pptx.stem}.pdf"
            if not pdf_path.exists():
                if self.required:
                    raise RuntimeError("LibreOffice produced no PDF")
                return []
            try:
                images = await asyncio.to_thread(self._pdf_images, pdf_path)
            except (OSError, RuntimeError):
                if self.required:
                    raise
                return []

        rendered: list[RenderedSlide] = []
        for page_number, image in enumerate(images[:slide_count], start=1):
            target = output_dir / f"slide-{page_number:03d}.webp"
            if image.width != self.width:
                height = round(image.height * self.width / image.width)
                image = image.resize((self.width, height), Image.Resampling.LANCZOS)
            image.save(target, "WEBP", quality=self.quality, method=6)
            rendered.append(RenderedSlide(page_number=page_number, image_path=target))
        return rendered

    @classmethod
    def _discover_executable(cls) -> tuple[str | None, str]:
        """Honor an override, otherwise avoid PATH-injected development builds."""

        configured = os.getenv("DEEPPRESENTER_SOFFICE")
        if configured:
            discovered = cls._inspect_executable(configured)
            return discovered or (None, "unavailable")

        discovered = [
            result
            for candidate in cls._candidate_executables()
            if (result := cls._inspect_executable(candidate)) is not None
        ]
        stable = next(
            (
                item
                for item in discovered
                if "libreofficedev" not in item[1].casefold()
                and "alpha" not in item[1].casefold()
            ),
            None,
        )
        return stable or (discovered[0] if discovered else (None, "unavailable"))

    @staticmethod
    def _candidate_executables() -> list[str | None]:
        return [
            "/Applications/LibreOffice.app/Contents/MacOS/soffice",
            "/opt/homebrew/bin/soffice",
            "/usr/local/bin/soffice",
            shutil.which("libreoffice"),
            shutil.which("soffice"),
        ]

    @staticmethod
    def _inspect_executable(candidate: str | None) -> tuple[str, str] | None:
        if not candidate:
            return None
        resolved = str(Path(candidate).expanduser().resolve())
        if not Path(resolved).is_file():
            return None
        try:
            result = subprocess.run(
                [resolved, "--version"],
                capture_output=True,
                check=False,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return None
        if result.returncode != 0:
            return None
        version = (result.stdout or result.stderr).strip() or "unknown"
        return resolved, version

    @staticmethod
    def _pdf_images(pdf_path: Path) -> list[Image.Image]:
        from pdf2image import convert_from_path

        return convert_from_path(str(pdf_path), dpi=150, fmt="png")


def overlay_labels(source_graph: SourceGraph) -> dict[str, ShapeNode]:
    """Stable short labels for every shape drawn on the overlay image.

    The same mapping is shown to the annotating model, so the overlay drawing
    and the annotation prompt must both derive labels from this function.
    Layout and master chrome is deliberately excluded: it is template
    decoration reproduced verbatim by the scaffold, not a content slot the
    annotator should turn into a region.
    """

    labels: dict[str, ShapeNode] = {}
    for shape in source_graph.shapes:
        if (
            shape.scope is not ShapeScope.SLIDE
            or not shape.visible
            or shape.normalized_bbox.width <= 0
            or shape.normalized_bbox.height <= 0
        ):
            continue
        labels[f"#{len(labels) + 1}"] = shape
    return labels


def create_shape_overlay(
    reference_image: Path,
    source_graph: SourceGraph,
    output_path: Path,
) -> Path:
    """Draw short shape labels over a rendered page for VLM grounding."""

    with Image.open(reference_image) as source:
        image = source.convert("RGBA")
    draw = ImageDraw.Draw(image, "RGBA")
    width, height = image.size
    font = ImageFont.load_default(size=max(18, width // 60))
    palette = [
        (235, 64, 52, 220),
        (26, 115, 232, 220),
        (24, 160, 88, 220),
        (155, 81, 224, 220),
        (230, 145, 20, 220),
    ]
    placed: list[tuple[int, int, int, int]] = []
    for index, (label, shape) in enumerate(overlay_labels(source_graph).items()):
        box = shape.normalized_bbox
        x1 = round(box.x * width)
        y1 = round(box.y * height)
        x2 = round((box.x + box.width) * width)
        y2 = round((box.y + box.height) * height)
        color = palette[index % len(palette)]
        draw.rectangle((x1, y1, x2, y2), outline=color, width=3)
        text_box = draw.textbbox((0, 0), label, font=font)
        label_width = text_box[2] - text_box[0] + 10
        label_height = text_box[3] - text_box[1] + 8
        label_rect = _place_label(
            x1,
            max(0, y1 - label_height),
            label_width,
            label_height,
            placed,
            height,
        )
        placed.append(label_rect)
        draw.rectangle(label_rect, fill=color)
        draw.text(
            (label_rect[0] + 5, label_rect[1] + 4 - text_box[1]),
            label,
            fill=(255, 255, 255, 255),
            font=font,
        )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.convert("RGB").save(output_path, "WEBP", quality=90, method=6)
    return output_path


def _place_label(
    x: int,
    y: int,
    width: int,
    height: int,
    placed: list[tuple[int, int, int, int]],
    canvas_height: int,
) -> tuple[int, int, int, int]:
    """Shift a label box downward until it stops covering earlier labels."""

    def overlaps(rect: tuple[int, int, int, int]) -> bool:
        return any(
            rect[0] < other[2]
            and rect[2] > other[0]
            and rect[1] < other[3]
            and rect[3] > other[1]
            for other in placed
        )

    rect = (x, y, x + width, y + height)
    while overlaps(rect) and rect[3] + height <= canvas_height:
        rect = (rect[0], rect[1] + height, rect[2], rect[3] + height)
    return rect
