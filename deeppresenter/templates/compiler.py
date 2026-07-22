"""Independent, immutable PPTX template compiler.

Compilation is intentionally separate from slide generation. Generation reads
only a pinned revision's Template IR and extracted assets; it never opens the
source PPTX or invokes this compiler.
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import tempfile
from collections import defaultdict
from pathlib import Path

from PIL import Image, ImageChops
from pydantic import ValidationError

from .annotation import (
    DeterministicAnnotator,
    SemanticAnnotator,
    SlideAnnotationInput,
)
from .extractor import ExtractionResult, PptxExtractor
from .ids import derive_revision_id, sha256_file
from .models import (
    Asset,
    AssetIndex,
    CompactRegion,
    CompactSlideContext,
    LayoutFamily,
    LayoutFamilyIndex,
    RegionKind,
    ReusePolicy,
    Revision,
    RevisionRef,
    SCHEMA_VERSION,
    Semantic,
    SUPPORTED_ASPECT_RATIOS,
    SlideIndexEntry,
    SourceGraph,
    TemplateManifest,
    TemplateStatus,
    Theme,
    ValidationIssue,
    ValidationReport,
    ValidationSeverity,
    dump_model,
    utc_now,
)
from .overview import (
    OVERVIEW_INDEX_PATH,
    build_family_detail,
    build_index,
    family_detail_path,
)
from .scaffold import build_layout_css
from .rendering import (
    LibreOfficeRenderer,
    RenderedSlide,
    SlideRenderer,
    create_shape_overlay,
)


class TemplateCompilationError(RuntimeError):
    """Compilation failed before an immutable READY revision was produced."""


COMPILER_VERSION = "template-ir-compiler-3"


class TemplateCompiler:
    """Compile a source PPTX into a versioned Template IR revision."""

    def __init__(
        self,
        *,
        extractor: PptxExtractor | None = None,
        renderer: SlideRenderer | None = None,
        annotator: SemanticAnnotator | None = None,
        annotation_concurrency: int = 1,
    ) -> None:
        if annotation_concurrency < 1:
            raise ValueError("annotation_concurrency must be positive")
        self.extractor = extractor or PptxExtractor()
        self.renderer = renderer or LibreOfficeRenderer(required=False)
        self.annotator = annotator or DeterministicAnnotator()
        self.annotation_concurrency = annotation_concurrency

    async def compile(
        self,
        template_id: str,
        source_pptx: Path,
        template_dir: Path,
        name: str | None = None,
    ) -> TemplateManifest:
        """Compile ``source_pptx`` and atomically publish an immutable revision.

        ``template_dir`` is the root for exactly one template. A new source (or
        output-affecting parser/annotator version) creates another revision;
        an existing identical revision is reused without rewriting it.
        """

        source_pptx = Path(source_pptx).expanduser().resolve(strict=True)
        template_dir = Path(template_dir).expanduser().resolve(strict=False)
        if source_pptx.suffix.lower() != ".pptx":
            raise ValueError("TemplateCompiler accepts .pptx files only")
        template_dir.mkdir(parents=True, exist_ok=True)

        source_hash = sha256_file(source_pptx)
        revision_id = derive_revision_id(
            source_hash,
            compiler_version=COMPILER_VERSION,
            extractor_version=self.extractor.version,
            annotator_id=self.annotator.annotator_id,
            renderer_id=self.renderer.renderer_id,
        )
        existing_manifest = self._load_manifest(template_dir, template_id, name)
        revision_dir = template_dir / "revisions" / revision_id
        if revision_dir.exists():
            revision = Revision.load(revision_dir / "revision.json")
            self._assert_existing_revision(revision, template_id, source_hash)
            manifest = self._publish_manifest(
                template_dir,
                existing_manifest,
                revision,
                name=name,
            )
            self._clear_annotation_cache(template_dir)
            return manifest

        staging_parent = template_dir / ".staging"
        staging_parent.mkdir(parents=True, exist_ok=True)
        staging_dir = Path(
            tempfile.mkdtemp(prefix=f"{revision_id}-", dir=staging_parent)
        )
        try:
            revision = await self._compile_revision(
                template_id=template_id,
                revision_id=revision_id,
                source_hash=source_hash,
                source_pptx=source_pptx,
                staging_dir=staging_dir,
                annotation_cache_dir=template_dir / ".annotation-cache",
            )
            revision_dir.parent.mkdir(parents=True, exist_ok=True)
            try:
                os.replace(staging_dir, revision_dir)
            except OSError:
                if not revision_dir.exists():
                    raise
                existing = Revision.load(revision_dir / "revision.json")
                self._assert_existing_revision(existing, template_id, source_hash)
                revision = existing
            manifest = self._publish_manifest(
                template_dir,
                existing_manifest,
                revision,
                name=name,
            )
            self._clear_annotation_cache(template_dir)
            return manifest
        except Exception as exc:
            if not existing_manifest.revisions:
                failed = existing_manifest.model_copy(
                    update={
                        "status": TemplateStatus.FAILED,
                        "error": str(exc),
                        "updated_at": utc_now(),
                    }
                )
                self._write_manifest_atomic(template_dir, failed)
            if isinstance(exc, TemplateCompilationError):
                raise
            raise TemplateCompilationError(
                f"Failed to compile template {template_id}: {exc}"
            ) from exc
        finally:
            if staging_dir.exists():
                shutil.rmtree(staging_dir)
            try:
                staging_parent.rmdir()
            except OSError:
                pass

    async def _compile_revision(
        self,
        *,
        template_id: str,
        revision_id: str,
        source_hash: str,
        source_pptx: Path,
        staging_dir: Path,
        annotation_cache_dir: Path,
    ) -> Revision:
        source_dir = staging_dir / "source"
        source_dir.mkdir(parents=True)
        copied_source = source_dir / "original.pptx"
        shutil.copy2(source_pptx, copied_source)

        extraction = await asyncio.to_thread(self.extractor.extract, copied_source)
        if extraction.canvas.aspect_ratio not in SUPPORTED_ASPECT_RATIOS:
            raise TemplateCompilationError(
                "Template canvas must use a supported slide ratio (16:9 or 4:3); "
                f"got {extraction.canvas.aspect_ratio}"
            )
        self._persist_assets(staging_dir, extraction)
        dump_model(staging_dir / "theme" / "theme.json", extraction.theme)
        (staging_dir / "theme" / "theme.css").write_text(
            self._theme_css(extraction.theme),
            encoding="utf-8",
        )

        render_dir = staging_dir / ".rendered"
        rendered = await self.renderer.render(
            copied_source,
            render_dir,
            len(extraction.source_graphs),
        )
        render_map = {slide.page_number: slide for slide in rendered}
        slide_artifacts = self._prepare_slide_artifacts(
            staging_dir,
            extraction.source_graphs,
            render_map,
        )
        semantics = await self._annotate_slides(
            extraction,
            slide_artifacts,
            staging_dir,
            cache_dir=annotation_cache_dir,
            revision_id=revision_id,
        )
        families, slide_family_ids = self._layout_families(semantics)
        dump_model(
            staging_dir / "families" / "index.json",
            LayoutFamilyIndex(families=families),
        )
        entries = self._persist_contexts_and_index(
            staging_dir,
            extraction,
            semantics,
            slide_artifacts,
            slide_family_ids,
        )
        self._persist_overview(
            staging_dir,
            template_id=template_id,
            revision_id=revision_id,
            extraction=extraction,
            families=families,
            semantics=semantics,
            entries=entries,
        )
        report = self._validate(
            extraction,
            semantics,
            entries,
            rendered_pages=set(render_map),
        )
        dump_model(staging_dir / "validation" / "report.json", report)
        if not report.valid:
            raise TemplateCompilationError("Template IR validation failed")
        if render_dir.exists():
            shutil.rmtree(render_dir)

        file_hashes = self._artifact_hashes(staging_dir)
        revision = Revision(
            revision_id=revision_id,
            template_id=template_id,
            source_hash=source_hash,
            compiler_version=COMPILER_VERSION,
            extractor_version=self.extractor.version,
            annotator_id=self.annotator.annotator_id,
            renderer_id=self.renderer.renderer_id,
            slide_count=len(extraction.source_graphs),
            canvas=extraction.canvas,
            source_path="source/original.pptx",
            theme_path="theme/theme.json",
            slide_index_path="slides/index.jsonl",
            asset_index_path="assets/index.json",
            family_index_path="families/index.json",
            validation_report_path="validation/report.json",
            overview_index_path=OVERVIEW_INDEX_PATH,
            file_hashes=file_hashes,
        )
        dump_model(staging_dir / "revision.json", revision)
        return revision

    def _prepare_slide_artifacts(
        self,
        staging_dir: Path,
        graphs: list[SourceGraph],
        render_map: dict[int, RenderedSlide],
    ) -> dict[str, dict[str, str | None]]:
        result: dict[str, dict[str, str | None]] = {}
        for graph in graphs:
            slide_dir = staging_dir / "slides" / graph.slide_id
            slide_dir.mkdir(parents=True, exist_ok=True)
            dump_model(slide_dir / "source_graph.json", graph)
            reference_path: str | None = None
            overlay_path: str | None = None
            rendered = render_map.get(graph.page_number)
            if rendered is not None:
                reference = slide_dir / "reference.webp"
                self._normalize_reference(rendered.image_path, reference)
                overlay = slide_dir / "overlay.webp"
                create_shape_overlay(reference, graph, overlay)
                reference_path = reference.relative_to(staging_dir).as_posix()
                overlay_path = overlay.relative_to(staging_dir).as_posix()
            result[graph.slide_id] = {
                "reference": reference_path,
                "overlay": overlay_path,
            }
        return result

    async def _annotate_slides(
        self,
        extraction: ExtractionResult,
        slide_artifacts: dict[str, dict[str, str | None]],
        staging_dir: Path,
        *,
        cache_dir: Path,
        revision_id: str,
    ) -> list[Semantic]:
        semaphore = asyncio.Semaphore(self.annotation_concurrency)
        assets = [value.asset for value in extraction.assets]

        async def annotate(graph: SourceGraph) -> Semantic:
            artifacts = slide_artifacts[graph.slide_id]
            relevant = self._relevant_assets(graph, assets)
            # The revision ID encodes every annotation input, so a cache entry
            # from an earlier failed run of the same revision is safe to reuse.
            cache_path = cache_dir / f"{revision_id}-{graph.slide_id}.json"
            semantic = self._load_cached_semantic(cache_path, graph, relevant)
            if semantic is None:
                async with semaphore:
                    semantic = await self.annotator.annotate(
                        SlideAnnotationInput(
                            source_graph=graph,
                            assets=relevant,
                            reference_image_path=self._absolute_or_none(
                                staging_dir, artifacts["reference"]
                            ),
                            overlay_image_path=self._absolute_or_none(
                                staging_dir, artifacts["overlay"]
                            ),
                        )
                    )
                self._validate_semantic_bindings(graph, semantic, relevant)
                dump_model(cache_path, semantic)
            dump_model(
                staging_dir / "slides" / graph.slide_id / "semantic.json",
                semantic,
            )
            return semantic

        # Each coroutine constructs and validates a standalone slide request.
        # The task group cancels in-flight annotations when one slide fails,
        # so cleanup never races an annotator still writing into staging.
        try:
            async with asyncio.TaskGroup() as group:
                tasks = {
                    graph.slide_id: group.create_task(annotate(graph))
                    for graph in extraction.source_graphs
                }
        except ExceptionGroup as exc:
            raise exc.exceptions[0] from exc
        return [tasks[graph.slide_id].result() for graph in extraction.source_graphs]

    @staticmethod
    def _load_cached_semantic(
        cache_path: Path,
        graph: SourceGraph,
        assets: list[Asset],
    ) -> Semantic | None:
        if not cache_path.is_file():
            return None
        try:
            semantic = Semantic.model_validate_json(
                cache_path.read_text(encoding="utf-8")
            )
            TemplateCompiler._validate_semantic_bindings(graph, semantic, assets)
        except (ValidationError, ValueError):
            cache_path.unlink(missing_ok=True)
            return None
        return semantic

    @staticmethod
    def _clear_annotation_cache(template_dir: Path) -> None:
        cache_dir = template_dir / ".annotation-cache"
        if cache_dir.exists():
            shutil.rmtree(cache_dir)

    def _persist_contexts_and_index(
        self,
        staging_dir: Path,
        extraction: ExtractionResult,
        semantics: list[Semantic],
        artifacts: dict[str, dict[str, str | None]],
        family_ids: dict[str, list[str]],
    ) -> list[SlideIndexEntry]:
        asset_by_id = {value.asset.asset_id: value.asset for value in extraction.assets}
        graph_by_id = {graph.slide_id: graph for graph in extraction.source_graphs}
        entries: list[SlideIndexEntry] = []
        for semantic in semantics:
            layout_css_path = f"slides/{semantic.slide_id}/layout.css"
            (staging_dir / layout_css_path).write_text(
                build_layout_css(
                    semantic,
                    graph_by_id[semantic.slide_id],
                    extraction.theme,
                ),
                encoding="utf-8",
            )
            reusable = sorted(
                {
                    asset_id
                    for region in semantic.regions
                    for asset_id in region.asset_ids
                    if asset_id in asset_by_id
                    and asset_by_id[asset_id].reuse_policy
                    in {ReusePolicy.ALWAYS, ReusePolicy.TEMPLATE_ONLY}
                }
                | {
                    asset.asset_id
                    for asset in asset_by_id.values()
                    if asset.reuse_policy is ReusePolicy.ALWAYS
                }
            )
            context = CompactSlideContext(
                slide_id=semantic.slide_id,
                page_number=semantic.page_number,
                page_semantics=semantic.page_semantics,
                regions=[
                    CompactRegion(
                        region_id=region.region_id,
                        role=region.role,
                        kind=region.kind,
                        bbox=region.bbox,
                        required=region.behavior.required,
                        capacity=region.capacity,
                        style_ref=region.style_ref,
                        asset_ids=region.asset_ids,
                    )
                    for region in semantic.regions
                ],
                family_ids=family_ids[semantic.slide_id],
                reference_image_path=artifacts[semantic.slide_id]["reference"],
                overlay_image_path=artifacts[semantic.slide_id]["overlay"],
                layout_css_path=layout_css_path,
                reusable_asset_ids=reusable,
                theme_tokens=extraction.theme.css_variables,
            )
            compact_path = f"slides/{semantic.slide_id}/context.compact.json"
            dump_model(staging_dir / compact_path, context)
            entries.append(
                SlideIndexEntry(
                    slide_id=semantic.slide_id,
                    page_number=semantic.page_number,
                    source_graph_path=f"slides/{semantic.slide_id}/source_graph.json",
                    semantic_path=f"slides/{semantic.slide_id}/semantic.json",
                    compact_context_path=compact_path,
                    reference_image_path=artifacts[semantic.slide_id]["reference"],
                    overlay_image_path=artifacts[semantic.slide_id]["overlay"],
                    layout_css_path=layout_css_path,
                    family_ids=family_ids[semantic.slide_id],
                    stage=semantic.page_semantics.stage,
                    layout_pattern=semantic.page_semantics.layout_pattern,
                    modalities=semantic.page_semantics.modalities,
                )
            )
        index_path = staging_dir / "slides" / "index.jsonl"
        index_path.write_text(
            "".join(
                json.dumps(
                    entry.model_dump(mode="json"), ensure_ascii=False, sort_keys=True
                )
                + "\n"
                for entry in entries
            ),
            encoding="utf-8",
        )
        return entries

    @staticmethod
    def _persist_overview(
        staging_dir: Path,
        *,
        template_id: str,
        revision_id: str,
        extraction: ExtractionResult,
        families: list[LayoutFamily],
        semantics: list[Semantic],
        entries: list[SlideIndexEntry],
    ) -> None:
        """Compile both overview tiers so generation never re-projects them."""

        index = build_index(
            template_id=template_id,
            revision_id=revision_id,
            schema_version=SCHEMA_VERSION,
            name=template_id,
            canvas=extraction.canvas,
            theme=extraction.theme,
            families=families,
            slides=entries,
            assets=[value.asset for value in extraction.assets],
        )
        dump_model(staging_dir / OVERVIEW_INDEX_PATH, index)
        by_slide = {semantic.slide_id: semantic for semantic in semantics}
        for family in families:
            dump_model(
                staging_dir / family_detail_path(family.family_id),
                build_family_detail(family, by_slide),
            )

    @staticmethod
    def _layout_families(
        semantics: list[Semantic],
    ) -> tuple[list[LayoutFamily], dict[str, list[str]]]:
        grouped: dict[str, list[Semantic]] = defaultdict(list)
        for semantic in semantics:
            signature = TemplateCompiler._family_signature(semantic)
            grouped[signature].append(semantic)
        families: list[LayoutFamily] = []
        slide_families: dict[str, list[str]] = defaultdict(list)
        from .ids import family_id

        for signature, members in sorted(grouped.items()):
            identifier = family_id(signature)
            first = members[0]
            family = LayoutFamily(
                family_id=identifier,
                name=(
                    f"{first.page_semantics.stage.value}-"
                    f"{first.page_semantics.layout_pattern.value}"
                ),
                signature=signature,
                stage=first.page_semantics.stage,
                layout_pattern=first.page_semantics.layout_pattern,
                modalities=first.page_semantics.modalities,
                slide_ids=[member.slide_id for member in members],
                representative_slide_id=first.slide_id,
                description=first.page_semantics.summary,
                digest=first.page_semantics.digest,
                selection_hints=list(
                    dict.fromkeys(
                        hint for member in members for hint in member.selection_hints
                    )
                ),
                avoid_when=list(
                    dict.fromkeys(
                        hint for member in members for hint in member.avoid_when
                    )
                ),
            )
            families.append(family)
            for member in members:
                slide_families[member.slide_id].append(identifier)
        return families, dict(slide_families)

    @staticmethod
    def _family_signature(semantic: Semantic) -> str:
        value = {
            "stage": semantic.page_semantics.stage.value,
            "layout": semantic.page_semantics.layout_pattern.value,
            "modalities": sorted(
                kind.value for kind in semantic.page_semantics.modalities
            ),
            "regions": [
                {
                    "role": region.role.value,
                    "kind": region.kind.value,
                    "x": round(region.bbox.x * 8) / 8,
                    "y": round(region.bbox.y * 8) / 8,
                    "w": round(region.bbox.width * 8) / 8,
                    "h": round(region.bbox.height * 8) / 8,
                }
                for region in semantic.regions
                if region.kind not in {RegionKind.DECORATION, RegionKind.BACKGROUND}
            ],
        }
        return json.dumps(
            value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )

    @staticmethod
    def _validate(
        extraction: ExtractionResult,
        semantics: list[Semantic],
        entries: list[SlideIndexEntry],
        *,
        rendered_pages: set[int],
    ) -> ValidationReport:
        issues: list[ValidationIssue] = []
        graph_by_id = {graph.slide_id: graph for graph in extraction.source_graphs}
        asset_ids = {value.asset.asset_id for value in extraction.assets}
        for semantic in semantics:
            graph = graph_by_id.get(semantic.slide_id)
            if graph is None:
                issues.append(
                    ValidationIssue(
                        severity=ValidationSeverity.ERROR,
                        code="MISSING_SOURCE_GRAPH",
                        message="Semantic annotation has no source graph.",
                        slide_id=semantic.slide_id,
                    )
                )
                continue
            shape_ids = {shape.shape_id for shape in graph.shapes}
            for region in semantic.regions:
                if not set(region.source_shape_ids).issubset(shape_ids):
                    issues.append(
                        ValidationIssue(
                            severity=ValidationSeverity.ERROR,
                            code="UNKNOWN_SHAPE_REFERENCE",
                            message=f"Region {region.region_id} references an unknown shape.",
                            slide_id=semantic.slide_id,
                        )
                    )
                if not set(region.asset_ids).issubset(asset_ids):
                    issues.append(
                        ValidationIssue(
                            severity=ValidationSeverity.ERROR,
                            code="UNKNOWN_ASSET_REFERENCE",
                            message=f"Region {region.region_id} references an unknown asset.",
                            slide_id=semantic.slide_id,
                        )
                    )
        for graph in extraction.source_graphs:
            if graph.page_number not in rendered_pages:
                issues.append(
                    ValidationIssue(
                        severity=ValidationSeverity.WARNING,
                        code="MISSING_REFERENCE_IMAGE",
                        message="No slide renderer was available; structural IR remains usable.",
                        slide_id=graph.slide_id,
                    )
                )
        for extracted in extraction.assets:
            asset = extracted.asset
            if (
                asset.reuse_policy in {ReusePolicy.ALWAYS, ReusePolicy.TEMPLATE_ONLY}
                and asset.browser_path is None
            ):
                issues.append(
                    ValidationIssue(
                        severity=ValidationSeverity.ERROR,
                        code="BROWSER_ASSET_UNAVAILABLE",
                        message=(
                            f"Reusable asset {asset.asset_id} ({asset.media_type}) "
                            "has no browser-compatible representation."
                        ),
                    )
                )
        valid = not any(issue.severity is ValidationSeverity.ERROR for issue in issues)
        return ValidationReport(
            valid=valid,
            issues=issues,
            stats={
                "slides": len(extraction.source_graphs),
                "semantics": len(semantics),
                "index_entries": len(entries),
                "assets": len(extraction.assets),
                "rendered_slides": len(rendered_pages),
                "errors": sum(
                    issue.severity is ValidationSeverity.ERROR for issue in issues
                ),
                "warnings": sum(
                    issue.severity is ValidationSeverity.WARNING for issue in issues
                ),
            },
        )

    @classmethod
    def _persist_assets(cls, staging_dir: Path, extraction: ExtractionResult) -> None:
        assets_dir = staging_dir / "assets"
        assets_dir.mkdir(parents=True, exist_ok=True)
        for extracted in extraction.assets:
            target = staging_dir / extracted.asset.path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(extracted.blob)
            browser_fields = cls._browser_asset_fields(
                extracted.asset,
                target,
                assets_dir,
            )
            if browser_fields is not None:
                extracted.asset = extracted.asset.model_copy(update=browser_fields)
        dump_model(
            assets_dir / "index.json",
            AssetIndex(assets=[value.asset for value in extraction.assets]),
        )

    @classmethod
    def _browser_asset_fields(
        cls,
        asset: Asset,
        source: Path,
        assets_dir: Path,
    ) -> dict[str, str | int | None] | None:
        supported_extensions = {"avif", "gif", "jpeg", "jpg", "png", "svg", "webp"}
        supported_media_types = {
            "image/avif",
            "image/gif",
            "image/jpeg",
            "image/png",
            "image/svg+xml",
            "image/webp",
        }
        media_type_by_extension = {
            "avif": "image/avif",
            "gif": "image/gif",
            "jpeg": "image/jpeg",
            "jpg": "image/jpeg",
            "png": "image/png",
            "svg": "image/svg+xml",
            "webp": "image/webp",
        }
        if (
            asset.extension in supported_extensions
            or asset.media_type.lower() in supported_media_types
        ):
            return {
                "browser_path": asset.path,
                "browser_media_type": media_type_by_extension.get(
                    asset.extension,
                    asset.media_type,
                ),
                "browser_sha256": asset.sha256,
                "browser_pixel_width": asset.pixel_width,
                "browser_pixel_height": asset.pixel_height,
            }

        target = assets_dir / f"{asset.asset_id}.browser.png"
        vector_extensions = {"emf", "wmf"}
        converted = (
            cls._convert_vector_asset(source, target)
            if asset.extension in vector_extensions
            else cls._convert_raster_asset(source, target)
        )
        if not converted:
            return None
        with Image.open(target) as image:
            width, height = int(image.width), int(image.height)
        return {
            "browser_path": target.relative_to(assets_dir.parent).as_posix(),
            "browser_media_type": "image/png",
            "browser_sha256": sha256_file(target),
            "browser_pixel_width": width,
            "browser_pixel_height": height,
        }

    @staticmethod
    def _convert_raster_asset(source: Path, target: Path) -> bool:
        try:
            with Image.open(source) as image:
                converted = image.convert("RGBA" if "A" in image.getbands() else "RGB")
                converted.save(target, "PNG", optimize=True)
        except (OSError, ValueError):
            return False
        return target.is_file()

    @staticmethod
    def _convert_vector_asset(source: Path, target: Path) -> bool:
        executable = shutil.which("soffice") or shutil.which("libreoffice")
        if executable is None:
            return False
        with tempfile.TemporaryDirectory(prefix="template-asset-") as temp_name:
            temp_dir = Path(temp_name)
            profile_dir = temp_dir / "libreoffice-profile"
            profile_dir.mkdir()
            try:
                result = subprocess.run(
                    [
                        executable,
                        f"-env:UserInstallation={profile_dir.as_uri()}",
                        "--headless",
                        "--convert-to",
                        "png",
                        "--outdir",
                        str(temp_dir),
                        str(source),
                    ],
                    check=False,
                    capture_output=True,
                    timeout=60,
                )
            except (OSError, subprocess.SubprocessError):
                return False
            converted = temp_dir / f"{source.stem}.png"
            if result.returncode != 0 or not converted.is_file():
                return False
            shutil.copy2(converted, target)
        return TemplateCompiler._trim_uniform_border(target)

    @staticmethod
    def _trim_uniform_border(path: Path) -> bool:
        """Remove the page canvas LibreOffice adds around WMF/EMF artwork."""

        try:
            with Image.open(path) as source:
                image = source.convert("RGBA")
            background = Image.new("RGBA", image.size, image.getpixel((0, 0)))
            bounds = ImageChops.difference(
                image.convert("RGB"),
                background.convert("RGB"),
            ).getbbox()
            if bounds is None:
                return False
            padding = max(2, round(min(image.size) * 0.005))
            left, top, right, bottom = bounds
            crop = (
                max(0, left - padding),
                max(0, top - padding),
                min(image.width, right + padding),
                min(image.height, bottom + padding),
            )
            image.crop(crop).save(path, "PNG", optimize=True)
        except (OSError, ValueError):
            return False
        return path.is_file()

    @staticmethod
    def _theme_css(theme: Theme) -> str:
        lines = [":root {"]
        for key, value in sorted(theme.css_variables.items()):
            escaped = value.replace("\\", "\\\\").replace('"', '\\"')
            if key.startswith("--template-font"):
                escaped = f'"{escaped}"'
            lines.append(f"  {key}: {escaped};")
        lines.append("}")
        return "\n".join(lines) + "\n"

    @staticmethod
    def _relevant_assets(graph: SourceGraph, assets: list[Asset]) -> list[Asset]:
        direct_ids = (
            {asset_id for shape in graph.shapes for asset_id in shape.asset_ids}
            | set(graph.background_asset_ids)
            | set(graph.inherited_asset_ids)
        )
        return [
            asset
            for asset in assets
            if asset.asset_id in direct_ids or asset.reuse_policy is ReusePolicy.ALWAYS
        ]

    @staticmethod
    def _validate_semantic_bindings(
        graph: SourceGraph,
        semantic: Semantic,
        assets: list[Asset],
    ) -> None:
        if (
            semantic.slide_id != graph.slide_id
            or semantic.page_number != graph.page_number
        ):
            raise ValueError("Semantic output is bound to the wrong slide")
        shape_ids = {shape.shape_id for shape in graph.shapes}
        asset_ids = {asset.asset_id for asset in assets}
        for region in semantic.regions:
            if not set(region.source_shape_ids).issubset(shape_ids):
                raise ValueError(
                    f"Unknown shape reference in region {region.region_id}"
                )
            if not set(region.asset_ids).issubset(asset_ids):
                raise ValueError(
                    f"Unknown asset reference in region {region.region_id}"
                )

    @staticmethod
    def _normalize_reference(source: Path, target: Path) -> None:
        from PIL import Image

        with Image.open(source) as image:
            image.convert("RGB").save(target, "WEBP", quality=90, method=6)

    @staticmethod
    def _absolute_or_none(root: Path, relative: str | None) -> str | None:
        return str(root / relative) if relative is not None else None

    @staticmethod
    def _artifact_hashes(staging_dir: Path) -> dict[str, str]:
        hashes: dict[str, str] = {}
        for path in sorted(staging_dir.rglob("*")):
            if not path.is_file() or path.name == "revision.json":
                continue
            relative = path.relative_to(staging_dir).as_posix()
            if relative.startswith("source/"):
                continue
            hashes[relative] = sha256_file(path)
        return hashes

    @staticmethod
    def _assert_existing_revision(
        revision: Revision,
        template_id: str,
        source_hash: str,
    ) -> None:
        if revision.template_id != template_id or revision.source_hash != source_hash:
            raise TemplateCompilationError(
                "Existing revision directory does not match the requested source"
            )

    @staticmethod
    def _load_manifest(
        template_dir: Path,
        template_id: str,
        name: str | None,
    ) -> TemplateManifest:
        manifest_path = template_dir / "manifest.json"
        if manifest_path.exists():
            try:
                manifest = TemplateManifest.load(manifest_path)
            except (ValidationError, ValueError, json.JSONDecodeError):
                raw = json.loads(manifest_path.read_text(encoding="utf-8"))
                existing_id = raw.get("template_id")
                if existing_id is not None and existing_id != template_id:
                    raise ValueError("template_id does not match the existing manifest")
                return TemplateManifest(
                    template_id=template_id,
                    name=name or str(raw.get("name") or template_id),
                    status=TemplateStatus.COMPILING,
                )
            if manifest.template_id != template_id:
                raise ValueError("template_id does not match the existing manifest")
            return manifest
        return TemplateManifest(
            template_id=template_id,
            name=name or template_id,
            status=TemplateStatus.COMPILING,
        )

    def _publish_manifest(
        self,
        template_dir: Path,
        previous: TemplateManifest,
        revision: Revision,
        *,
        name: str | None,
    ) -> TemplateManifest:
        refs = {value.revision_id: value for value in previous.revisions}
        refs[revision.revision_id] = RevisionRef(
            revision_id=revision.revision_id,
            source_hash=revision.source_hash,
            slide_count=revision.slide_count,
            created_at=revision.created_at,
        )
        thumbnail = f"revisions/{revision.revision_id}/slides/s001/reference.webp"
        if not (template_dir / thumbnail).exists():
            thumbnail = None
        manifest = TemplateManifest(
            template_id=previous.template_id,
            name=name or previous.name,
            status=TemplateStatus.READY,
            latest_revision_id=revision.revision_id,
            active_revision_id=revision.revision_id,
            revisions=sorted(refs.values(), key=lambda value: value.created_at),
            source_hash=revision.source_hash,
            slide_count=revision.slide_count,
            aspect_ratio=revision.canvas.aspect_ratio,
            thumbnail=thumbnail,
            created_at=previous.created_at,
            updated_at=utc_now(),
        )
        self._write_manifest_atomic(template_dir, manifest)
        return manifest

    @staticmethod
    def _write_manifest_atomic(
        template_dir: Path,
        manifest: TemplateManifest,
    ) -> None:
        payload = json.dumps(
            manifest.model_dump(mode="json"),
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        temporary = template_dir / ".manifest.json.tmp"
        temporary.write_text(payload, encoding="utf-8")
        os.replace(temporary, template_dir / "manifest.json")
