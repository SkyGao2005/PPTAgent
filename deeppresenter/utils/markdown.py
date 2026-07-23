"""Small Markdown path helpers shared by review and generation handoff."""

from __future__ import annotations

import re
from collections.abc import Callable

MARKDOWN_IMAGE_RE = re.compile(
    r"(!\[[^\]]*\]\()(?P<target><[^>\n]+>|[^\s)]+)"
    r"(?P<suffix>(?:\s+[\"'][^\"']*[\"'])?\))"
)


def rewrite_markdown_images(
    markdown: str,
    rewrite: Callable[[str], str | None],
) -> str:
    """Rewrite Markdown image targets while leaving unsupported targets intact."""

    def replace(match: re.Match[str]) -> str:
        raw_target = match.group("target")
        target = raw_target[1:-1] if raw_target.startswith("<") else raw_target
        replacement = rewrite(target)
        if replacement is None:
            return match.group(0)
        return f"{match.group(1)}<{replacement}>{match.group('suffix')}"

    return MARKDOWN_IMAGE_RE.sub(replace, markdown)
