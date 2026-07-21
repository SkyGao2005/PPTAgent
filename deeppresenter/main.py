import json
import traceback
import uuid
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import Literal

from deeppresenter.agents.design import Design
from deeppresenter.agents.env import AgentEnv
from deeppresenter.agents.planner import Planner
from deeppresenter.agents.pptagent import PPTAgent
from deeppresenter.agents.research import Research
from deeppresenter.agents.subagent import SubAgent
from deeppresenter.server.models.events import StageName
from deeppresenter.utils.config import DeepPresenterConfig
from deeppresenter.utils.constants import WORKSPACE_BASE
from deeppresenter.utils.log import debug, error, set_logger, timer, warning
from deeppresenter.utils.typings import ChatMessage, ConvertType, InputRequest, Role
from deeppresenter.utils.webview import PlaywrightConverter, convert_html_to_pptx


class AgentLoop:
    def __init__(
        self,
        config: DeepPresenterConfig,
        session_id: str | None = None,
        workspace: Path | None = None,
        language: Literal["zh", "en"] = "en",
        event_reporter=None,
        preview_service=None,
    ):
        self.config = config
        self.language = language
        self.event_reporter = event_reporter
        self.preview_service = preview_service
        if session_id is None:
            session_id = str(uuid.uuid4())[:8]
        self.session_id = session_id
        self.workspace = workspace or WORKSPACE_BASE / session_id
        self.intermediate_output: dict[str, str | Path] = {}
        self.agent = None
        set_logger(
            f"deeppresenter-loop-{self.workspace.stem}",
            self.workspace / ".history" / "deeppresenter-loop.log",
        )
        debug(f"Initialized AgentLoop with workspace={self.workspace}")
        debug(f"Config: {self.config.model_dump_json(indent=2)}")

    @timer("DeepPresenter Loop")
    async def run(
        self,
        request: InputRequest,
        check_llms: bool = False,
        soft_parsing: bool = True,
    ) -> AsyncGenerator[str | ChatMessage, None]:
        """Main loop for DeepPresenter generation process.
        Arguments:
            request: InputRequest object containing task details.
            check_llms: Whether to check LLM availability before running.
            soft_parsing: Whether to use soft parsing on html2pptx.
        Yields:
            ChatMessage or final output path (str). Outline path stored in intermediate_output["outline"].
        """
        if not self.config.design_agent.is_multimodal and self.config.heavy_reflect:
            debug(
                "Reflective design requires a multimodal LLM in the design agent, reflection will only enable on textual state."
            )
        if check_llms:
            await self.config.validate_llms()
        try:
            await self._report_stage_started(StageName.PREPARE)
            request.copy_to_workspace(self.workspace)
            with open(self.workspace / ".input_request.json", "w") as f:
                json.dump(request.model_dump(), f, ensure_ascii=False, indent=2)
            await self._report_stage_completed(StageName.PREPARE)
        except Exception as e:
            await self._report_stage_failed(StageName.PREPARE, str(e))
            raise

        async with AgentEnv(
            self.workspace,
            self.config,
            event_reporter=self.event_reporter,
            preview_service=self.preview_service,
        ) as agent_env:
            hello_message = f"DeepPresenter running in {self.workspace}, with {len(request.attachments)} attachments, prompt={request.instruction}"
            modes = []
            if self.config.offline_mode:
                modes.append("Offline Mode")
            self.agent_env = agent_env
            if self.config.multiagent_mode:
                self.agent_env.register_tool(
                    SubAgent.delegate(
                        self.config, agent_env, self.workspace, self.language
                    )
                )
                modes.append("Multiagent Mode")
            if modes:
                hello_message += f" [{', '.join(modes)}]"
            debug(hello_message)

            yield ChatMessage(role=Role.SYSTEM, content=hello_message)

            # ── Optional Planner phase ────────────────────────────────────
            if request.enable_planner:
                agent_env.current_stage = StageName.PLAN
                await self._report_stage_started(StageName.PLAN)
                self.planner = Planner(
                    self.config,
                    agent_env,
                    self.workspace,
                    self.language,
                )
                self.agent = self.planner
                self.planner_gen = self.planner.loop(request)
                try:
                    async for msg in self.planner_gen:
                        if isinstance(msg, str):
                            self.intermediate_output["outline"] = msg
                            await self._report_stage_completed(StageName.PLAN)
                            yield msg
                            break
                        yield msg
                except Exception as e:
                    error_message = f"Planner agent failed with error: {e}\n{traceback.format_exc()}"
                    error(error_message)
                    await self._report_stage_failed(StageName.PLAN, str(e))
                    raise e
                finally:
                    self.planner.save_history()
                    await self.planner_gen.aclose()
                    self.save_results()

            agent_env.current_stage = StageName.RESEARCH
            await self._report_stage_started(StageName.RESEARCH)
            self.research_agent = Research(
                self.config,
                agent_env,
                self.workspace,
                self.language,
            )
            self.agent = self.research_agent
            try:
                async for msg in self.research_agent.loop(
                    request, self.intermediate_output.get("outline", None)
                ):
                    if isinstance(msg, str):
                        md_file = Path(msg)
                        if not md_file.is_absolute():
                            md_file = self.workspace / md_file
                        self.intermediate_output["manuscript"] = md_file
                        msg = str(md_file)
                        await self._report_stage_completed(StageName.RESEARCH)
                        break
                    yield msg
            except Exception as e:
                error_message = (
                    f"Research agent failed with error: {e}\n{traceback.format_exc()}"
                )
                error(error_message)
                await self._report_stage_failed(StageName.RESEARCH, str(e))
                raise e
            finally:
                self.research_agent.save_history()
                self.save_results()

            if request.convert_type == ConvertType.PPTAGENT:
                agent_env.current_stage = StageName.GENERATE
                await self._report_stage_started(StageName.GENERATE)
                self.pptagent = PPTAgent(
                    self.config,
                    agent_env,
                    self.workspace,
                    self.language,
                )
                self.agent = self.pptagent
                try:
                    async for msg in self.pptagent.loop(request, str(md_file)):
                        if isinstance(msg, str):
                            pptx_file = Path(msg)
                            if not pptx_file.is_absolute():
                                pptx_file = self.workspace / pptx_file
                            self.intermediate_output["pptx"] = pptx_file
                            self.intermediate_output["final"] = pptx_file
                            msg = str(pptx_file)
                            await self._report_stage_completed(
                                StageName.GENERATE,
                                payload=self._slide_outline_payload(),
                            )
                            break
                        yield msg
                except Exception as e:
                    error_message = (
                        f"PPTAgent failed with error: {e}\n{traceback.format_exc()}"
                    )
                    error(error_message)
                    await self._report_stage_failed(StageName.GENERATE, str(e))
                    raise e
                finally:
                    self.pptagent.save_history()
                    self.save_results()
            else:
                agent_env.current_stage = StageName.GENERATE
                await self._report_stage_started(StageName.GENERATE)
                self.designagent = Design(
                    self.config,
                    agent_env,
                    self.workspace,
                    self.language,
                )
                self.agent = self.designagent
                try:
                    async for msg in self.designagent.loop(request, str(md_file)):
                        if isinstance(msg, str):
                            slide_html_dir = Path(msg)
                            if not slide_html_dir.is_absolute():
                                slide_html_dir = self.workspace / slide_html_dir
                            self.intermediate_output["slide_html_dir"] = slide_html_dir
                            await self._report_stage_completed(
                                StageName.GENERATE,
                                payload=self._slide_outline_payload(),
                            )
                            break
                        yield msg
                except Exception as e:
                    error_message = (
                        f"Design agent failed with error: {e}\n{traceback.format_exc()}"
                    )
                    error(error_message)
                    await self._report_stage_failed(StageName.GENERATE, str(e))
                    raise e
                finally:
                    self.designagent.save_history()
                    self.save_results()
                pptx_path = self.workspace / f"{md_file.stem}.pptx"
                agent_env.current_stage = StageName.EXPORT
                await self._report_export_started()
                try:
                    try:
                        # ? this feature is in experimental stage
                        await convert_html_to_pptx(
                            slide_html_dir,
                            pptx_path,
                            aspect_ratio=request.powerpoint_type.value,
                            soft_parsing=soft_parsing,
                        )
                    except Exception as e:
                        warning(
                            f"html2pptx conversion failed, falling back to pdf conversion\n{e}"
                        )
                        pptx_path = pptx_path.with_suffix(".pdf")
                        (self.workspace / ".html2pptx-error.txt").write_text(
                            str(e) + "\n" + traceback.format_exc()
                        )
                    finally:
                        async with PlaywrightConverter() as pc:
                            await pc.convert_to_pdf(
                                list(slide_html_dir.glob("*.html")),
                                pptx_path.with_suffix(".pdf"),
                                aspect_ratio=request.powerpoint_type.value,
                            )
                except Exception as e:
                    await self._report_export_failed(str(e))
                    raise

                self.intermediate_output["final"] = str(pptx_path)
                await self._report_export_completed(str(pptx_path))
                msg = pptx_path
            self.save_results()
            debug(f"DeepPresenter finished, final output at: {msg}")
            yield msg

    def save_results(self):
        with open(self.workspace / "intermediate_output.json", "w") as f:
            json.dump(
                {k: str(v) for k, v in self.intermediate_output.items()},
                f,
                ensure_ascii=False,
                indent=2,
            )

    async def _report_stage_started(self, stage: StageName) -> None:
        if self.event_reporter is None:
            return
        try:
            await self.event_reporter.stage_started(stage)
        except Exception as e:
            warning(f"Failed to report stage start event: {e}")

    def _slide_outline_payload(self) -> dict | None:
        if self.preview_service is None:
            return None
        try:
            outline = []
            for slide in self.preview_service.list_slides(self.session_id):
                outline.append(
                    {
                        "slide_id": slide.slide_id,
                        "index": slide.index,
                        "title": slide.structured_data.title or f"第 {slide.index} 页",
                    }
                )
            return {"outline": outline} if outline else None
        except Exception as e:
            warning(f"Failed to build slide outline payload: {e}")
            return None

    async def _report_stage_completed(
        self,
        stage: StageName,
        payload: dict | None = None,
    ) -> None:
        if self.event_reporter is None:
            return
        try:
            await self.event_reporter.stage_completed(stage, payload=payload)
        except Exception as e:
            warning(f"Failed to report stage completion event: {e}")

    async def _report_stage_failed(self, stage: StageName, message: str) -> None:
        if self.event_reporter is None:
            return
        try:
            await self.event_reporter.stage_failed(stage, message)
        except Exception as e:
            warning(f"Failed to report stage failure event: {e}")

    async def _report_export_started(self) -> None:
        if self.event_reporter is None:
            return
        try:
            await self.event_reporter.export_started()
        except Exception as e:
            warning(f"Failed to report export start event: {e}")

    async def _report_export_completed(self, artifact_url: str) -> None:
        if self.event_reporter is None:
            return
        try:
            artifact = Path(artifact_url)
            filename = artifact.name or None
            if artifact.is_absolute():
                try:
                    rel = artifact.resolve().relative_to(self.workspace.resolve())
                    artifact_url = f"/api/tasks/{self.session_id}/artifacts/{rel.as_posix()}"
                except ValueError:
                    pass
            await self.event_reporter.export_completed(artifact_url, filename)
        except Exception as e:
            warning(f"Failed to report export completion event: {e}")

    async def _report_export_failed(self, message: str) -> None:
        if self.event_reporter is None:
            return
        try:
            await self.event_reporter.export_failed(message)
        except Exception as e:
            warning(f"Failed to report export failure event: {e}")
