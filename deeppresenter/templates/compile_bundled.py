"""Build the packaged templates into Template IR outside generation runtime."""

from __future__ import annotations

import argparse
import asyncio
from importlib.util import find_spec
from pathlib import Path

from .annotation import DeterministicAnnotator, SemanticAnnotator, VLMAnnotator
from .compiler import TemplateCompiler
from .deeppresenter_client import DeepPresenterStructuredVLMClient
from .rendering import LibreOfficeRenderer


BUNDLED_TEMPLATE_IDS = ("beamer", "cip", "default", "hit", "thu", "ucas")


def bundled_root() -> Path:
    """Locate the installed pptagent package's template directory."""

    spec = find_spec("pptagent")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError("pptagent package is not available")
    return Path(next(iter(spec.submodule_search_locations))) / "templates"


def build_annotator(config_path: str | None) -> SemanticAnnotator:
    """Match the annotator the API service uses for uploaded templates.

    The bundled templates have always been compiled with a VLM. Falling back
    to the deterministic annotator silently produces a worse revision -- no
    page images means no judgement of which sample images a deck may replace,
    and the semantics are guessed from geometry alone -- so the fallback is
    announced rather than taken quietly.
    """

    from deeppresenter.utils.config import DeepPresenterConfig

    config = DeepPresenterConfig.load_from_file(config_path)
    model = config.vision_model or config.design_agent
    if not model.is_multimodal:
        print(
            f"warning: {model.model_name} is not multimodal; compiling with the "
            "deterministic annotator, which yields a lower-quality revision"
        )
        return DeterministicAnnotator()
    return VLMAnnotator(
        DeepPresenterStructuredVLMClient(model),
        model_id=model.model_name,
    )


async def compile_bundled(root: Path, config_path: str | None = None) -> None:
    """Compile every packaged source explicitly; never called by generation."""

    compiler = TemplateCompiler(
        renderer=LibreOfficeRenderer(required=True),
        annotator=build_annotator(config_path),
    )
    for template_id in BUNDLED_TEMPLATE_IDS:
        template_dir = root / template_id
        source = template_dir / "source.pptx"
        if not source.is_file():
            raise FileNotFoundError(f"Missing bundled template source: {source}")
        manifest = await compiler.compile(
            template_id,
            source,
            template_dir,
            name=template_id,
        )
        print(f"{template_id}: {manifest.active_revision_id}")


def main() -> None:
    """Command-line entry for maintainers and release builds."""

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=bundled_root())
    parser.add_argument(
        "--config",
        default=None,
        help="DeepPresenter config file; defaults to DEEPPRESENTER_CONFIG_FILE",
    )
    args = parser.parse_args()
    asyncio.run(
        compile_bundled(args.root.expanduser().resolve(), args.config)
    )


if __name__ == "__main__":
    main()
