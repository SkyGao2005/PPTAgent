import json
import os
from copy import deepcopy
from math import ceil
from os.path import exists
from pathlib import Path
from random import shuffle
from typing import Optional

from fastmcp import FastMCP
from mistune import html as markdown_to_html

from deeppresenter.utils.constants import WORKSPACE_BASE as DEEPPRESENTER_WORKSPACE_BASE
from pptagent.llms import AsyncLLM
from pptagent.multimodal import ImageLabler
from pptagent.pptgen import PPTAgent, get_length_factor
from pptagent.presentation import Presentation
from pptagent.presentation.layout import Layout
from pptagent.response.pptgen import (
    EditorOutput,
    SlideElement,
)
from pptagent.utils import (
    Config,
    Language,
    get_html_table_image,
    get_logger,
    package_join,
    resolve_path_in_workspace,
)

logger = get_logger(__name__)


def mcp_slide_validate(editor_output: EditorOutput, layout: Layout, prs_lang: Language):
    warnings = []
    errors = []
    length_factor = get_length_factor(prs_lang, Language.english())
    layout_elements = {el.name for el in layout.elements}
    editor_elements = {el.name for el in editor_output.elements}
    for el in layout_elements - editor_elements:
        errors.append(f"Element {el} not found in editor output")
    for el in editor_elements - layout_elements:
        errors.append(f"Element {el} not found in layout")
    for el in layout.elements:
        if layout[el.name].type == "image":
            for i in range(len(editor_output[el.name].data)):
                if not exists(editor_output[el.name].data[i]):
                    errors.append(f"Image {editor_output[el.name].data[i]} not found")
        else:
            charater_counts = max([len(i) for i in editor_output[el.name].data])
            expected_length = ceil(layout[el.name].suggested_characters * length_factor)
            if charater_counts - expected_length > 5:
                warnings.append(
                    f"Element {el.name} has {charater_counts} characters, but the expected length is {expected_length}"
                )
    return warnings, errors


class PPTAgentServer(PPTAgent):
    roles = ["coder"]

    def __init__(self):
        self.source_doc = None
        self.mcp = FastMCP("PPTAgent")
        self.slides = []
        self.layout: Layout | None = None
        self.editor_output: EditorOutput | None = None
        if os.getenv("PPTAGENT_MODEL") is not None:
            model = AsyncLLM(
                os.getenv("PPTAGENT_MODEL"),
                os.getenv("PPTAGENT_API_BASE"),
                os.getenv("PPTAGENT_API_KEY"),
            )
        elif os.getenv("CONFIG_FILE") is not None:
            from deeppresenter.utils.config import DeepPresenterConfig

            endpoint = DeepPresenterConfig.load_from_file(
                os.getenv("CONFIG_FILE")
            ).research_agent._endpoints[0]
            model = AsyncLLM(
                model=endpoint.model,
                base_url=endpoint.base_url,
                api_key=endpoint.api_key,
            )
        else:
            raise Exception("Please set the valid endpoint for the model correctly")
        workspace = os.getenv("WORKSPACE", None)
        if workspace is not None:
            os.chdir(workspace)

        if not model.to_sync().test_connection():
            msg = "Unable to connect to the model, please set the PPTAGENT_MODEL, PPTAGENT_API_BASE, and PPTAGENT_API_KEY environment variables correctly"
            logger.error(msg)
            raise Exception(msg)
        super().__init__(language_model=model, vision_model=model)

        # load templates — supports both bundled templates and dynamic registry (P0-7)
        self._registry: Optional[object] = None  # TemplateRegistry, set externally
        self.template_description = {}
        self.templates = {}
        self._load_templates_from_disk()

    @classmethod
    def list_templates(cls) -> list:
        """List all available templates (bundled + workspace)."""
        templates = set()

        # 1. Bundled templates (shipped with pptagent)
        templates_dir = Path(package_join("templates"))
        if templates_dir.exists():
            for p in templates_dir.iterdir():
                if p.is_dir():
                    templates.add(p.name)

        # 2. WORKSPACE env var templates (user templates)
        workspace = os.getenv("WORKSPACE", None)
        if workspace:
            ws_templates = Path(workspace) / "templates"
            if ws_templates.exists():
                for p in ws_templates.iterdir():
                    if p.is_dir() and (p / "manifest.json").exists():
                        templates.add(p.name)

        # 3. DeepPresenter workspace templates (uploaded via API)
        workspace_base = os.getenv(
            "DEEPPRESENTER_WORKSPACE_BASE",
            str(DEEPPRESENTER_WORKSPACE_BASE),
        )
        for suffix in ("templates_api/templates", "workspace/templates_api/templates"):
            dp_templates = Path(workspace_base) / suffix
            if dp_templates.exists():
                for p in dp_templates.iterdir():
                    if p.is_dir() and (p / "manifest.json").exists():
                        templates.add(p.name)

        return sorted(templates)

    # ── Dynamic template loading (P0-7) ──────────────────────

    def _load_templates_from_disk(self) -> None:
        """Load templates from bundled templates directory and/or workspace.

        Called at startup and on reload_templates().  If a TemplateRegistry
        is attached, fresh manifests are fetched from it; otherwise the
        bundled templates dir is used as fallback alongside any templates
        found in the DeepPresenter cache or WORKSPACE directory.
        """
        # 1. Try registry first (runtime templates)
        if self._registry is not None:
            manifests = self._registry.list_all()
            for manifest in manifests:
                if manifest.status.value != "ready":
                    continue
                self._load_single_template(manifest.template_id)

        # 2. Also discover templates from workspace directories
        #    (covers templates uploaded via API when registry is not attached)
        discovered = set(self.templates.keys())  # already loaded via registry

        search_dirs: list[Path] = []
        # WORKSPACE env var
        workspace = os.getenv("WORKSPACE", None)
        if workspace:
            search_dirs.append(Path(workspace) / "templates")
        # DeepPresenter workspace
        workspace_base = os.getenv(
            "DEEPPRESENTER_WORKSPACE_BASE",
            str(DEEPPRESENTER_WORKSPACE_BASE),
        )
        for suffix in ("templates_api/templates", "workspace/templates_api/templates"):
            search_dirs.append(Path(workspace_base) / suffix)

        for search_dir in search_dirs:
            if not search_dir.exists():
                continue
            for template_dir in search_dir.iterdir():
                if not template_dir.is_dir():
                    continue
                tid = template_dir.name
                if tid in discovered:
                    continue
                if not (template_dir / "manifest.json").exists():
                    continue
                discovered.add(tid)
                try:
                    self._load_single_template(tid)
                except Exception as e:
                    logger.warning(f"Failed to load workspace template {tid}: {e}")

        # 3. Also load bundled templates (shipped with pptagent)
        templates_dir = Path(package_join("templates"))
        if templates_dir.exists():
            for template_dir in templates_dir.iterdir():
                if not template_dir.is_dir():
                    continue
                if template_dir.name in self.templates:
                    continue  # already loaded via registry or workspace
                try:
                    self._load_template_from_dir(template_dir.name, template_dir)
                except Exception as e:
                    logger.warning(
                        f"Failed to load bundled template {template_dir.name}: {e}"
                    )

        logger.info(
            f"{len(self.templates)} templates loaded: "
            + ", ".join(self.templates.keys())
        )

    def _load_single_template(self, template_id: str) -> bool:
        """Load a single template by ID — supports hot-loading (P0-7).

        Returns True on success, False on failure.
        """
        # Determine the template directory
        if self._registry is not None:
            template_dir = self._registry.templates_dir / template_id
        else:
            # Fallback: search workspace directories
            template_dir = None
            candidates: list[Path] = []

            workspace = os.getenv("WORKSPACE", None)
            if workspace:
                candidates.append(Path(workspace) / "templates" / template_id)

            workspace_base = os.getenv(
                "DEEPPRESENTER_WORKSPACE_BASE",
                str(DEEPPRESENTER_WORKSPACE_BASE),
            )
            for suffix in (
                "templates_api/templates",
                "workspace/templates_api/templates",
            ):
                candidates.append(Path(workspace_base) / suffix / template_id)

            for c in candidates:
                if c.is_dir():
                    template_dir = c
                    break

        if template_dir is None or not template_dir.is_dir():
            logger.warning(f"Template directory not found: {template_id}")
            return False

        try:
            return self._load_template_from_dir(template_id, template_dir)
        except Exception as e:
            logger.warning(f"Failed to load template {template_id}: {e}")
            return False

    def _load_template_from_dir(self, template_id: str, template_dir: Path) -> bool:
        """Load template data from a directory into self.templates."""
        # Check for description
        desc_path = template_dir / "description.txt"
        if desc_path.exists():
            self.template_description[template_id] = desc_path.read_text(
                encoding="utf-8"
            ).strip()
        else:
            self.template_description[template_id] = template_id

        # Load Presentation
        source_pptx = template_dir / "source.pptx"
        if not source_pptx.exists():
            source_pptx = template_dir / "original.pptx"
        if not source_pptx.exists():
            logger.warning(f"No .pptx found in {template_dir}")
            return False

        prs_config = Config(str(template_dir))
        prs = Presentation.from_file(str(source_pptx), prs_config)

        # Load image stats
        image_stats_path = template_dir / "image_stats.json"
        if image_stats_path.exists():
            image_labler = ImageLabler(prs, prs_config)
            image_labler.apply_stats(
                json.loads(image_stats_path.read_text(encoding="utf-8"))
            )

        # Load slide induction
        induction_path = template_dir / "slide_induction.json"
        if induction_path.exists():
            slide_induction = json.loads(induction_path.read_text(encoding="utf-8"))
        else:
            slide_induction = {}

        self.templates[template_id] = {
            "presentation": prs,
            "slide_induction": slide_induction,
            "config": prs_config,
        }

        logger.debug(f"Loaded template {template_id} from {template_dir}")
        return True

    def register_tools(self):
        @self.mcp.tool()
        def markdown_table_to_image(markdown_table: str, path: str, css: str):
            """
            Convert a markdown table to an image and save it to the specified path.

            Args:
                markdown_table (str): The markdown table content to convert
                path (str): The file path where the image will be saved
                css (str): Custom CSS styles for the table. Use class selectors
                                    (table, thead, th, td) to style the table elements. Avoid
                                    changing background colors outside the table area.

            Returns:
                str: Confirmation message with the path to the saved image
            """
            html = markdown_to_html(markdown_table)
            get_html_table_image(html, path, css)
            return f"Markdown table converted to image and saved to {path}"

        @self.mcp.tool()
        def list_templates():
            """List all available templates."""
            # Hot-load: ensure we pick up templates uploaded since startup
            self._load_templates_from_disk()
            return {
                "message": "Please choose one the following templates by calling `set_template`",
                "templates": [
                    {
                        "name": template_name,
                        "description": self.template_description.get(
                            template_name, template_name
                        ),
                    }
                    for template_name in self.templates.keys()
                ],
            }

        @self.mcp.tool()
        def reload_templates():
            """Reload all templates from disk and registry (hot-load).

            Call this after a new template has been parsed to make it
            immediately available for generation without restarting the
            MCP server.  (P0-7)
            """
            self._load_templates_from_disk()
            return {
                "message": f"Reloaded {len(self.templates)} templates",
                "templates": list(self.templates.keys()),
            }

        @self.mcp.tool()
        def set_template(template_name: str = "default"):
            """Select a PowerPoint template by name.

            This only needs to be called once before creating slides.
            Calling it multiple times resets the template context and clears all
            generated slides that have not been saved.

            Args:
                template_name: The name of the template to select

            Returns:
                dict: Success message and list of available layouts
            """
            assert template_name in self.templates, (
                f"Template {template_name} not available, please choose from {', '.join(self.templates.keys())}"
            )

            template_data = self.templates[template_name]
            # P0-9: Pass deepcopy so set_reference() cannot mutate the cached dict.
            # This is a double safeguard alongside the .get() fix in pptgen.py.
            self.set_reference(
                slide_induction=deepcopy(template_data["slide_induction"]),
                presentation=template_data["presentation"],
            )
            self.slides.clear()

            return {
                "message": "Template set successfully, please select layout from given layouts later",
                "template_description": self.template_description[template_name],
                "available_layouts": list(self.layouts.keys()),
            }

        @self.mcp.tool()
        async def create_slide(layout: str):
            """Create a slide with a given layout.

            Args:
                layout: Name of the layout to use. Must be one of the available layouts given by set_template.

            Returns:
                dict: Success message, instructions, and content schema for the selected layout.
            """
            assert self._initialized, (
                "PPTAgent not initialized, please call `set_template` first"
            )
            assert layout in self.layouts, (
                "Given layout was not in available layouts: " + ", ".join(self.layouts)
            )
            if self.layout is not None:
                message = "Layout update from " + self.layout.title + " to " + layout
                message += "\nDid you forget to call `generate_slide` after setting slide content?"
            else:
                message = "Layout " + layout + " selected successfully"
            self.layout = self.layouts[layout]
            return {
                "message": message,
                "instructions": "Generate slide content strictly following the schema below",
                "schema": self.layout.content_schema,
            }

        @self.mcp.tool()
        async def write_slide(structured_slide_elements: list[dict]):
            """Write the slide elements for generating a PowerPoint slide.
            Note that this function will not generate a slide, you should call `generate_slide`.

            Args:
                structured_slide_elements: List of slide elements with their content
                should follow the content schema and adhere to
                [
                    {
                        "name": "element_name",
                        "data": ["content1", "content2", "..."]
                        // Array of strings for text elements
                        // OR array of image paths for image elements: ["/path/to/image1.jpg", "/path/to/image2.png"]
                    }
                ]
            Returns:
                dict: Success message, warnings, and errors
            """
            self.structured_slide_elements = structured_slide_elements
            assert self.layout is not None, (
                "Layout is not selected, please call `create_slide` before writing slide"
            )
            editor_output = EditorOutput(
                elements=[SlideElement(**e) for e in structured_slide_elements]
            )
            warnings, errors = mcp_slide_validate(
                editor_output, self.layout, self.reference_lang
            )
            if errors:
                raise ValueError("Errors:\n" + "\n".join(errors))

            self.editor_output = editor_output
            if warnings:
                return {
                    "message": "Slide elements set with warnings, please try your best to fix the. You should only proceed after fixing all warnings or 5 retries.",
                    "warnings": warnings,
                }
            return {
                "message": "Slide elements set successfully. Ready to generate slide."
            }

        @self.mcp.tool()
        async def generate_slide():
            """Generate a PowerPoint slide after layout and slide elements are set.

            Returns:
                dict: Success message with slide number and next steps
            """
            if self.editor_output is None:
                raise ValueError(
                    "Slide elements are not set, please call `write_slide` before generating slide"
                )

            command_list, template_id = self._generate_commands(
                self.editor_output, self.layout
            )
            slide, _ = await self._edit_slide(command_list, template_id)

            # Reset state after successful generation
            self.layout = None
            self.editor_output = None
            self.slides.append(slide)

            slide_number = len(self.slides)
            available_layouts = list(self.layouts.keys())
            shuffle(available_layouts)

            return {
                "message": f"Slide {slide_number:02d} generated successfully",
                "next_steps": "You can now save the slides or continue generating more slides",
                "available_layouts": available_layouts,
            }

        @self.mcp.tool()
        async def save_generated_slides(pptx_path: str):
            """Save the generated slides to a PowerPoint file.

            Args:
                pptx_path: The path to save the PowerPoint file
            """
            pptx = resolve_path_in_workspace(pptx_path)
            assert len(self.slides), (
                "No slides generated, please call `generate_slide` first"
            )
            pptx.parent.mkdir(parents=True, exist_ok=True)
            self.empty_prs.slides = self.slides
            self.empty_prs.save(str(pptx))
            self.slides = []
            return f"total {len(self.empty_prs.slides)} slides saved to {pptx}"


def main():
    server = PPTAgentServer()
    server.register_tools()
    server.mcp.run(show_banner=False)


if __name__ == "__main__":
    main()
