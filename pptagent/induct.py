import asyncio
import os
from collections import defaultdict
import functools
from os.path import join

from aiometer import run_all
from jinja2 import Template

from pptagent.agent import Agent
from pptagent.llms import AsyncLLM
from pptagent.model_utils import (
    get_cluster,
    get_image_embedding,
    images_cosine_similarity,
    language_id,
)
from pptagent.presentation import Picture, Presentation, SlidePage
from pptagent.response import SlideSchema
from pptagent.utils import (
    Config,
    get_logger,
    is_image_path,
    package_join,
)

logger = get_logger(__name__)

CATEGORY_SPLIT_TEMPLATE = Template(
    open(package_join("prompts", "category_split.txt"), encoding="utf-8").read()
)
ASK_CATEGORY_PROMPT = open(
    package_join("prompts", "ask_category.txt"), encoding="utf-8"
).read()


class SlideInducter:
    """
    Stage I: Presentation Analysis.
    This stage is to analyze the presentation: cluster slides into different layouts, and extract content schema for each layout.
    """

    def __init__(
        self,
        prs: Presentation,
        ppt_image_folder: str,
        template_image_folder: str,
        config: Config,
        image_models: list,
        language_model: AsyncLLM,
        vision_model: AsyncLLM,
        use_assert: bool = True,
        # P0-4: Optional progress callback for structured progress events.
        # Signature: async def callback(stage_name: str, sub_progress: float) -> None
        progress_callback: callable = None,
    ):
        """
        Initialize the SlideInducter.

        Args:
            prs (Presentation): The presentation object.
            ppt_image_folder (str): The folder containing PPT images.
            template_image_folder (str): The folder containing normalized slide images.
            config (Config): The configuration object.
            image_models (list): A list of image models.
            progress_callback (callable, optional): Async callback(stage_name, sub_progress)
                for reporting structured progress events.
        """
        self.prs = prs
        self.config = config
        self.ppt_image_folder = ppt_image_folder
        self.template_image_folder = template_image_folder
        self.language_model = language_model
        self.vision_model = vision_model
        self.image_models = image_models
        self.progress_callback = progress_callback
        self.schema_extractor = Agent(
            "schema_extractor",
            {
                "language": language_model,
            },
        )
        if not use_assert:
            return

        num_template_images = sum(
            is_image_path(f) for f in os.listdir(template_image_folder)
        )
        num_ppt_images = sum(is_image_path(f) for f in os.listdir(ppt_image_folder))
        num_slides = len(prs.slides)

        if not (num_template_images == num_ppt_images == num_slides):
            raise ValueError(
                f"Slide count mismatch detected:\n"
                f"- Presentation slides: {num_slides}\n"
                f"- Template images: {num_template_images} ({template_image_folder})\n"
                f"- PPT images: {num_ppt_images} ({ppt_image_folder})\n"
                f"All counts must be equal."
            )

    async def _notify_progress(self, stage: str, progress: float) -> None:
        """Notify progress via callback if configured (P0-4).

        Args:
            stage: A short machine-readable stage label (e.g. "category_split").
            progress: Sub-progress in [0.0, 1.0] within the current phase.
        """
        if self.progress_callback is None:
            return
        try:
            result = self.progress_callback(stage, progress)
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            pass  # Never let a progress callback break induction

    async def category_split(self):
        """
        Async version: Split slides into categories based on their functional purpose.
        """
        functional_cluster = await self.language_model(
            CATEGORY_SPLIT_TEMPLATE.render(slides=self.prs.to_text()),
            return_json=True,
        )
        assert isinstance(functional_cluster, dict) and all(
            isinstance(k, str) and isinstance(v, list)
            for k, v in functional_cluster.items()
        ), "Functional cluster must be a dictionary with string keys and list values"
        functional_slides = set(sum(functional_cluster.values(), []))
        content_slides_index = set(range(1, len(self.prs) + 1)) - functional_slides

        return content_slides_index, functional_cluster

    async def layout_split(
        self, content_slides_index: set[int], layout_induction: dict
    ):
        """
        Async version: Cluster slides into different layouts.
        """
        embeddings = get_image_embedding(self.template_image_folder, *self.image_models)
        assert len(embeddings) == len(self.prs)
        content_split = defaultdict(list)
        for slide_idx in content_slides_index:
            slide = self.prs.slides[slide_idx - 1]
            content_type = slide.get_content_type()
            layout_name = slide.slide_layout_name
            content_split[(layout_name, content_type)].append(slide_idx)

        async with asyncio.TaskGroup() as tg:
            for (layout_name, content_type), slides in content_split.items():
                sub_embeddings = [
                    embeddings[f"slide_{slide_idx:04d}.jpg"] for slide_idx in slides
                ]
                similarity = images_cosine_similarity(sub_embeddings)
                for cluster in get_cluster(similarity):
                    slide_indexs = [slides[i] for i in cluster]
                    template_id = max(
                        slide_indexs,
                        key=lambda x: len(self.prs.slides[x - 1].shapes),
                    )

                    tg.create_task(
                        self.vision_model(
                            ASK_CATEGORY_PROMPT,
                            join(self.ppt_image_folder, f"slide_{template_id:04d}.jpg"),
                        )
                    ).add_done_callback(
                        lambda f, tid=template_id, sidxs=slide_indexs, ctype=content_type: (
                            layout_induction[f.result() + ":" + ctype].update(
                                {"template_id": tid, "slides": sidxs}
                            )
                        )
                    )

    async def layout_induct(self):
        """
        Async version: Perform layout induction for the presentation.
        """
        await self._notify_progress("category_split", 0.0)

        layout_induction = defaultdict(lambda: defaultdict(list))
        content_slides_index, functional_cluster = await self.category_split()

        await self._notify_progress("layout_split", 0.3)

        for layout_name, cluster in functional_cluster.items():
            layout_induction[layout_name]["slides"] = cluster
            layout_induction[layout_name]["template_id"] = cluster[0]

        functional_keys = list(layout_induction.keys())
        function_slides_index = set()
        for layout_name, cluster in layout_induction.items():
            function_slides_index.update(cluster["slides"])
        used_slides_index = function_slides_index.union(content_slides_index)
        for i in range(len(self.prs.slides)):
            if i + 1 not in used_slides_index:
                content_slides_index.add(i + 1)
        await self.layout_split(content_slides_index, layout_induction)

        await self._notify_progress("layout_induction_complete", 1.0)
        layout_induction["functional_keys"] = functional_keys
        return layout_induction

    async def content_induct(
        self,
        layout_induction: dict,
        max_at_once: int | None = None,
        max_per_second: int | None = None,
    ):
        """
        Async version: Perform content schema extraction for the presentation.
        """
        await self._notify_progress("content_induction_started", 0.0)

        partial_funcs = []
        layout_names = []
        for layout_name, cluster in layout_induction.items():
            if layout_name == "functional_keys" or "content_schema" in cluster:
                continue
            slide: SlidePage = self.prs.slides[cluster["template_id"] - 1]

            contents = [para.text for para in slide.iter_paragraphs()] + [
                shape.caption for shape in slide.shape_filter(Picture)
            ]
            partial_funcs.append(
                functools.partial(
                    self.schema_extractor,
                    slide=slide.to_html(),
                    response_format=SlideSchema.response_model(contents),
                    slide_idx=cluster["template_id"],
                )
            )
            layout_names.append(layout_name)

        # Run all schema extractions with per-item progress reporting
        completed_count = 0
        total_count = len(partial_funcs)

        async def _run_with_progress(func, layout_name):
            nonlocal completed_count
            result = await func()
            completed_count += 1
            await self._notify_progress(
                "schema_extracting",
                completed_count / total_count if total_count > 0 else 1.0,
            )
            return layout_name, result

        if total_count > 0:
            tasks = [
                functools.partial(_run_with_progress, func, name)
                for func, name in zip(partial_funcs, layout_names)
            ]
            schemas = await run_all(
                tasks, max_at_once=max_at_once, max_per_second=max_per_second
            )
            for layout_name, (_, schema) in schemas:
                layout_induction[layout_name].update(schema)

        layout_induction["language"] = language_id(slide.to_text()).model_dump()
        return layout_induction
