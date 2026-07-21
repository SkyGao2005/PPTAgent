"""Build the packaged templates into Template IR outside generation runtime."""

from __future__ import annotations

import argparse
import asyncio
from importlib.util import find_spec
from pathlib import Path

from .annotation import DeterministicAnnotator
from .compiler import TemplateCompiler


BUNDLED_TEMPLATE_IDS = ("beamer", "cip", "default", "hit", "thu", "ucas")


def bundled_root() -> Path:
    """Locate the installed pptagent package's template directory."""

    spec = find_spec("pptagent")
    if spec is None or not spec.submodule_search_locations:
        raise RuntimeError("pptagent package is not available")
    return Path(next(iter(spec.submodule_search_locations))) / "templates"


async def compile_bundled(root: Path) -> None:
    """Compile every packaged source explicitly; never called by generation."""

    compiler = TemplateCompiler(annotator=DeterministicAnnotator())
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
    args = parser.parse_args()
    asyncio.run(compile_bundled(args.root.expanduser().resolve()))


if __name__ == "__main__":
    main()
