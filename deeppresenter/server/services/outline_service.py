"""Research-manuscript review gate before formal slide generation."""

from __future__ import annotations

import asyncio
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import quote

from deeppresenter.server.models.artifacts import task_dir
from deeppresenter.server.models.events import EventType, StageName
from deeppresenter.server.models.outlines import (
    OutlineComment,
    OutlineDraftRecord,
    OutlineDraftResponse,
    OutlineStatus,
)
from deeppresenter.server.services.event_bus import EventBus
from deeppresenter.server.services.task_manager import TaskManager
from deeppresenter.templates.context import TemplateContextProvider
from deeppresenter.templates.models import SUPPORTED_ASPECT_RATIOS
from deeppresenter.templates.store import TemplateAspectRatioMismatchError
from deeppresenter.utils.markdown import rewrite_markdown_images

OUTLINE_RECORD_FILE = "outline.json"
REVIEWABLE_IMAGE_SUFFIXES = {".gif", ".jpeg", ".jpg", ".png", ".webp"}
RESEARCH_REQUEST_MARKERS = (
    "请调研",
    "需要调研",
    "补充调研",
    "搜索",
    "查找",
    "检索",
    "最新数据",
    "最新事实",
    "新增数据",
    "补充数据",
    "外部证据",
    "please research",
    "new research",
    "search for",
    "look up",
    "latest data",
    "new data",
    "additional evidence",
    "external evidence",
)


class OutlineNotFoundError(LookupError):
    """Raised when a manuscript draft does not exist."""


class OutlineConflictError(RuntimeError):
    """Raised when the requested action is invalid for the current state."""


class OutlineService:
    """Persist Research manuscripts and promote an approved revision to a task."""

    def __init__(
        self,
        workspace_base: Path,
        task_manager: TaskManager,
        *,
        use_placeholder: bool,
        config_path: Optional[str] = None,
        template_context_provider: TemplateContextProvider | None = None,
    ) -> None:
        self.workspace_base = Path(workspace_base)
        self.workspace_base.mkdir(parents=True, exist_ok=True)
        self.task_manager = task_manager
        self.use_placeholder = use_placeholder
        self.config_path = config_path
        self.template_context_provider = template_context_provider
        self._drafts: dict[str, OutlineDraftRecord] = {}
        self._workspaces: dict[str, Path] = {}
        self._runners: dict[str, asyncio.Task[None]] = {}
        self._restored_failures: list[str] = []
        self._lock = asyncio.Lock()
        self._restore()

    async def create(
        self,
        *,
        topic: str,
        attachments: list[str],
        page_count: int,
        ratio: str,
        language: str,
        template: Optional[str],
        template_id: Optional[str],
        template_revision_id: Optional[str],
        convert_type: Optional[str],
    ) -> OutlineDraftResponse:
        """Create and start the first Research manuscript revision."""

        while True:
            outline_id = uuid.uuid4().hex[:8]
            try:
                workspace = self.task_manager.prepare_review_workspace(
                    outline_id,
                    require_new=True,
                )
            except FileExistsError:
                continue
            break
        self._workspaces[outline_id] = workspace

        resolved_template = template
        resolved_template_id = template_id or template
        context_path: str | None = None
        if resolved_template_id and not self.use_placeholder:
            (
                resolved_template,
                resolved_template_id,
                template_revision_id,
                context_path,
            ) = await self._materialize_template(
                workspace=workspace,
                template_id=resolved_template_id,
                template_revision_id=template_revision_id,
                requested_ratio=ratio,
            )

        now = self._now()
        draft = OutlineDraftRecord(
            outline_id=outline_id,
            status=OutlineStatus.GENERATING,
            topic=topic,
            requested_page_count=page_count,
            ratio=ratio,
            language=language,
            attachment_paths=attachments,
            template=resolved_template,
            template_id=resolved_template_id,
            template_revision_id=template_revision_id,
            template_context_path=context_path,
            convert_type=convert_type,
            created_at=now,
            updated_at=now,
        )
        self._drafts[outline_id] = draft
        self._save(draft)
        await self._emit_outline(
            draft,
            EventType.OUTLINE_GENERATING,
            "Research 正在生成内容文稿",
        )
        await self._start_generation(draft, comment=None)
        return self.to_response(draft)

    def get(self, outline_id: str) -> OutlineDraftResponse:
        """Return the current public state for one draft."""

        return self.to_response(self._require(outline_id))

    async def regenerate(
        self,
        outline_id: str,
        comment: str,
    ) -> OutlineDraftResponse:
        """Append feedback to Research context and produce a replacement manuscript."""

        async with self._lock:
            draft = self._require(outline_id)
            if draft.status == OutlineStatus.GENERATING:
                raise OutlineConflictError("内容文稿正在生成，请稍候")
            if draft.status == OutlineStatus.APPROVED:
                raise OutlineConflictError("内容文稿已确认，不能再次修改")
            normalized_comment = comment.strip()
            target_revision = draft.revision + 1
            if normalized_comment:
                draft.comments.append(
                    OutlineComment(
                        comment_id=uuid.uuid4().hex[:10],
                        text=normalized_comment,
                        target_revision=target_revision,
                        created_at=self._now(),
                    )
                )
            draft.status = OutlineStatus.GENERATING
            draft.error_message = None
            draft.updated_at = self._now()
            self._save(draft)

        await self._emit_outline(
            draft,
            EventType.OUTLINE_GENERATING,
            f"Research 正在生成第 {target_revision} 版内容文稿",
        )
        await self._start_generation(draft, comment=normalized_comment or None)
        return self.to_response(draft)

    async def approve(self, outline_id: str) -> str:
        """Create a formal task that starts from the approved Markdown manuscript."""

        async with self._lock:
            draft = self._require(outline_id)
            if draft.status == OutlineStatus.APPROVED and draft.task_id:
                return draft.task_id
            if draft.status != OutlineStatus.READY or not draft.manuscript_path:
                raise OutlineConflictError("内容文稿尚未生成完成，暂不能确认")

            task_id = await self.task_manager.create(
                instruction=draft.topic,
                attachments=draft.attachment_paths,
                num_pages=str(draft.requested_page_count),
                powerpoint_type=draft.ratio,
                template=draft.template,
                template_id=draft.template_id,
                template_revision_id=draft.template_revision_id,
                convert_type=draft.convert_type,
                enable_planner=False,
                language=draft.language,
                approved_manuscript_path=draft.manuscript_path,
                task_id=draft.outline_id,
                prepared_template_context_path=draft.template_context_path,
            )
            draft.task_id = task_id
            draft.status = OutlineStatus.APPROVED
            draft.updated_at = self._now()
            self._save(draft)
            await self._emit_outline(
                draft,
                EventType.OUTLINE_APPROVED,
                "内容文稿已确认，继续生成幻灯片",
            )
            return task_id

    def to_response(self, draft: OutlineDraftRecord) -> OutlineDraftResponse:
        """Project an internal record without exposing attachment filesystem paths."""

        manuscript = ""
        if draft.manuscript_path:
            path = Path(draft.manuscript_path)
            if path.is_file():
                manuscript = self._rewrite_image_urls(
                    draft.outline_id,
                    path.read_text(encoding="utf-8"),
                    path.parent,
                )
        return OutlineDraftResponse(
            outline_id=draft.outline_id,
            status=draft.status,
            topic=draft.topic,
            page_count=draft.requested_page_count,
            ratio=draft.ratio,
            template_id=draft.template_id,
            revision=draft.revision,
            last_seq=self._last_seq(draft.outline_id),
            manuscript=manuscript,
            comments=draft.comments,
            task_id=draft.task_id,
            error_message=draft.error_message,
            created_at=draft.created_at,
            updated_at=draft.updated_at,
        )

    def resolve_asset(self, outline_id: str, asset_path: str) -> Path:
        """Resolve one image referenced by a review manuscript."""

        self._require(outline_id)
        workspace = self._workspace(outline_id).resolve()
        try:
            candidate = (workspace / asset_path).resolve(strict=True)
        except OSError as exc:
            raise FileNotFoundError(asset_path) from exc
        if not candidate.is_relative_to(workspace):
            raise PermissionError("路径访问被拒绝")
        if (
            not candidate.is_file()
            or candidate.suffix.lower() not in REVIEWABLE_IMAGE_SUFFIXES
        ):
            raise FileNotFoundError(asset_path)
        return candidate

    async def _start_generation(
        self,
        draft: OutlineDraftRecord,
        *,
        comment: Optional[str],
    ) -> None:
        if self.use_placeholder:
            await self._generate(draft, comment=comment)
            return

        runner = asyncio.create_task(self._generate(draft, comment=comment))
        self._runners[draft.outline_id] = runner

        def _discard(completed: asyncio.Task[None]) -> None:
            self._runners.pop(draft.outline_id, None)
            if completed.cancelled():
                return
            completed.exception()

        runner.add_done_callback(_discard)

    async def _generate(
        self,
        draft: OutlineDraftRecord,
        *,
        comment: Optional[str],
    ) -> None:
        try:
            next_revision = draft.revision + 1
            manuscript_path = self._workspace(draft.outline_id) / (
                f"manuscript-v{next_revision}.md"
            )
            if self.use_placeholder:
                manuscript = self._placeholder_manuscript(draft, comment)
            else:
                manuscript = await self._agent_manuscript(
                    draft,
                    comment,
                    manuscript_path,
                )
            if not manuscript.strip():
                raise ValueError("Research 生成的 Markdown 文稿为空")
            manuscript_path.write_text(manuscript, encoding="utf-8")
            draft.revision = next_revision
            draft.manuscript_path = str(manuscript_path)
            draft.status = OutlineStatus.READY
            draft.error_message = None
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            draft.status = OutlineStatus.FAILED
            draft.error_message = str(exc)
        finally:
            draft.updated_at = self._now()
            self._save(draft)
            if draft.status == OutlineStatus.READY:
                await self._emit_outline(
                    draft,
                    EventType.OUTLINE_READY,
                    f"第 {draft.revision} 版内容文稿已生成",
                )
            elif draft.status == OutlineStatus.FAILED:
                await self._emit_outline(
                    draft,
                    EventType.OUTLINE_FAILED,
                    draft.error_message or "内容文稿生成失败",
                )

    async def _agent_manuscript(
        self,
        draft: OutlineDraftRecord,
        comment: Optional[str],
        output_path: Path,
    ) -> str:
        from deeppresenter.main import AgentLoop
        from deeppresenter.utils.config import DeepPresenterConfig
        from deeppresenter.utils.typings import (
            ConvertType,
            InputRequest,
            PowerPointType,
        )

        request = InputRequest(
            instruction=draft.topic,
            attachments=draft.attachment_paths,
            num_pages=str(draft.requested_page_count),
            template=draft.template,
            template_id=draft.template_id,
            template_revision_id=draft.template_revision_id,
            template_context_path=draft.template_context_path,
            powerpoint_type=PowerPointType(draft.ratio),
            convert_type=ConvertType.DEEPPRESENTER,
            enable_planner=False,
        )
        loop = AgentLoop(
            config=DeepPresenterConfig.load_from_file(self.config_path),
            session_id=draft.outline_id,
            workspace=self._workspace(draft.outline_id),
            language="zh" if draft.language.lower().startswith("zh") else "en",
        )
        current_path = (
            Path(draft.manuscript_path).expanduser().resolve(strict=True)
            if draft.manuscript_path
            else None
        )
        current_contents = (
            current_path.read_text(encoding="utf-8") if current_path else None
        )
        try:
            generated_path = await loop.generate_manuscript(
                request,
                output_path=output_path,
                current_manuscript=current_path,
                feedback=comment,
                allow_research=(
                    current_path is None or self._requests_new_research(comment)
                ),
            )
            return generated_path.read_text(encoding="utf-8")
        finally:
            if current_path is not None and current_contents is not None:
                if (
                    not current_path.exists()
                    or current_path.read_text(encoding="utf-8") != current_contents
                ):
                    current_path.write_text(current_contents, encoding="utf-8")

    def _placeholder_manuscript(
        self,
        draft: OutlineDraftRecord,
        comment: Optional[str],
    ) -> str:
        titles = [
            draft.topic,
            "议题与目标",
            "关键背景",
            "现状洞察",
            "核心发现",
            "策略框架",
            "重点举措",
            "实施路径",
            "里程碑规划",
            "资源与协同",
            "风险与应对",
            "结论与行动",
            "谢谢观看",
        ]
        contexts = [
            "以主题、汇报对象和日期建立演示开场。",
            "说明本次汇报要回答的问题、目标与内容边界。",
            "梳理影响议题的外部环境、内部条件和关键变化。",
            "用事实和数据概括当前状态，指出最值得关注的差距。",
            "提炼三项主要结论，并解释结论之间的因果关系。",
            "给出贯穿后续方案的总体思路、原则与取舍。",
            "拆解优先级最高的行动，并明确每项行动的预期结果。",
            "按阶段呈现落地步骤、关键责任与前后依赖。",
            "标注时间节点、阶段交付物和验收标准。",
            "说明组织分工、预算投入与跨团队协作方式。",
            "列出主要风险、触发信号和对应缓解措施。",
            "回扣核心观点，明确下一步决策与行动要求。",
            "简洁收束演示，并为讨论与问答留出空间。",
        ]
        page_count = max(5, min(30, draft.requested_page_count))
        if page_count > len(titles):
            extra = page_count - len(titles)
            for number in range(1, extra + 1):
                titles.insert(-2, f"补充分析 {number}")
                contexts.insert(-2, "补充支撑核心结论的证据、案例或细分视角。")
        selected_titles = titles[: page_count - 1] + [titles[-1]]
        selected_contexts = contexts[: page_count - 1] + [contexts[-1]]
        if comment and len(selected_contexts) > 2:
            selected_contexts[2] = (
                f"本版优先响应修改意见“{comment[:80]}”，并沿用已有资料与结论。"
            )
        if draft.revision > 0 and len(selected_titles) > 5:
            selected_titles[3], selected_titles[4] = (
                selected_titles[4],
                selected_titles[3],
            )
            selected_contexts[3], selected_contexts[4] = (
                selected_contexts[4],
                selected_contexts[3],
            )
        sections = []
        for index, (title, context) in enumerate(
            zip(selected_titles, selected_contexts, strict=True),
            start=1,
        ):
            heading = "#" if index == 1 else "##"
            sections.append(
                f"{heading} {title}\n\n"
                f"{context}\n\n"
                f"- 核心信息点 {index}.1\n"
                f"- 核心信息点 {index}.2\n"
                f"- 建议用清晰的视觉层级呈现本页内容"
            )
        return "\n\n---\n\n".join(sections) + "\n"

    def _rewrite_image_urls(
        self,
        outline_id: str,
        markdown: str,
        source_dir: Path,
    ) -> str:
        workspace = self._workspace(outline_id).resolve()

        def rewrite(target: str) -> str | None:
            if (
                not target
                or target.startswith(("#", "//"))
                or re.match(r"^[a-z][a-z0-9+.-]*:", target, flags=re.IGNORECASE)
            ):
                return None
            source = Path(target).expanduser()
            if not source.is_absolute():
                source = source_dir / source
            try:
                resolved = source.resolve(strict=True)
            except OSError:
                return None
            if (
                not resolved.is_relative_to(workspace)
                or resolved.suffix.lower() not in REVIEWABLE_IMAGE_SUFFIXES
            ):
                return None
            relative = resolved.relative_to(workspace).as_posix()
            return f"/api/outlines/{outline_id}/assets/{quote(relative, safe='/')}"

        return rewrite_markdown_images(markdown, rewrite)

    async def _materialize_template(
        self,
        *,
        workspace: Path,
        template_id: str,
        template_revision_id: Optional[str],
        requested_ratio: str,
    ) -> tuple[str, str, str, str]:
        if self.template_context_provider is None:
            raise RuntimeError("Template IR store is not configured")
        revision = self.template_context_provider.store.resolve(
            template_id,
            template_revision_id,
        )
        canvas = revision.metadata.get("canvas", {})
        template_ratio = (
            canvas.get("aspect_ratio") if isinstance(canvas, dict) else None
        ) or revision.manifest.get("aspect_ratio")
        if template_ratio not in SUPPORTED_ASPECT_RATIOS:
            raise TemplateAspectRatioMismatchError(
                f"Template {revision.template_id} uses unsupported canvas ratio "
                f"{template_ratio!r}"
            )
        if requested_ratio != template_ratio:
            raise TemplateAspectRatioMismatchError(
                f"Template {revision.template_id} uses {template_ratio}, "
                f"but the manuscript review requested {requested_ratio}"
            )
        context = await asyncio.to_thread(
            self.template_context_provider.materialize,
            revision.template_id,
            workspace,
            revision.revision_id,
        )
        return (
            revision.template_id,
            revision.template_id,
            revision.revision_id,
            str(context["context_dir"]),
        )

    def _restore(self) -> None:
        for record_path in self.workspace_base.glob(f"*/{OUTLINE_RECORD_FILE}"):
            try:
                draft = OutlineDraftRecord.model_validate_json(
                    record_path.read_text(encoding="utf-8")
                )
            except (OSError, ValueError):
                continue
            self._workspaces[draft.outline_id] = record_path.parent
            if draft.status == OutlineStatus.GENERATING:
                draft.status = OutlineStatus.FAILED
                draft.error_message = "服务重启中断了本次内容生成，请重新生成"
                draft.updated_at = self._now()
                self._save(draft)
                self._restored_failures.append(draft.outline_id)
            self._drafts[draft.outline_id] = draft
            if draft.status != OutlineStatus.APPROVED:
                self.task_manager.prepare_review_workspace(
                    draft.outline_id,
                    require_new=False,
                )

    async def emit_restored_failures(self) -> None:
        """Notify reconnecting SSE clients about generations lost on restart."""

        for outline_id in self._restored_failures:
            draft = self._drafts.get(outline_id)
            if draft is not None and draft.status == OutlineStatus.FAILED:
                await self._emit_outline(
                    draft,
                    EventType.OUTLINE_FAILED,
                    draft.error_message or "内容文稿生成失败",
                )
        self._restored_failures.clear()

    def reconcile_restored_tasks(self) -> None:
        """Finish an approval that persisted task.json before outline.json."""

        for draft in self._drafts.values():
            if (
                draft.status != OutlineStatus.APPROVED
                and self.task_manager.get_snapshot(draft.outline_id) is not None
            ):
                draft.status = OutlineStatus.APPROVED
                draft.task_id = draft.outline_id
                draft.updated_at = self._now()
                self._save(draft)

    def _save(self, draft: OutlineDraftRecord) -> None:
        workspace = self._workspace(draft.outline_id)
        workspace.mkdir(parents=True, exist_ok=True)
        (workspace / OUTLINE_RECORD_FILE).write_text(
            draft.model_dump_json(indent=2),
            encoding="utf-8",
        )

    def _require(self, outline_id: str) -> OutlineDraftRecord:
        draft = self._drafts.get(outline_id)
        if draft is None:
            raise OutlineNotFoundError(f"内容文稿 {outline_id} 不存在")
        return draft

    def _workspace(self, outline_id: str) -> Path:
        return self._workspaces.get(
            outline_id,
            task_dir(self.workspace_base, outline_id),
        )

    def get_event_bus(self, outline_id: str) -> EventBus | None:
        """Return the shared review/task EventBus for SSE subscriptions."""

        self._require(outline_id)
        return self.task_manager.get_event_bus(outline_id)

    def _last_seq(self, outline_id: str) -> int:
        bus = self.task_manager.get_event_bus(outline_id)
        return bus.seq if bus is not None else 0

    async def _emit_outline(
        self,
        draft: OutlineDraftRecord,
        event_type: EventType,
        message: str,
    ) -> None:
        reporter = self.task_manager.get_event_reporter(draft.outline_id)
        if reporter is None:
            return
        await reporter.emit(
            event_type,
            stage=StageName.RESEARCH,
            message=message,
            payload={
                "outline_id": draft.outline_id,
                "outline_status": draft.status.value,
                "revision": draft.revision,
            },
        )

    @staticmethod
    def _now() -> str:
        return datetime.now(timezone.utc).isoformat()

    @staticmethod
    def _requests_new_research(comment: Optional[str]) -> bool:
        if not comment:
            return False
        normalized = comment.casefold()
        return any(marker in normalized for marker in RESEARCH_REQUEST_MARKERS)
