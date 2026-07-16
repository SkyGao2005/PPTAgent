"""Prompt construction for the conversational slide editor (D).

The model receives only the *necessary* context for one page (per the plan's
"对话上下文" section): the slide's structured content, its layout, the global
PPT outline/title, the most recent dialogue rounds, and the user's new
instruction (plus an explicitly selected element, if any).  It returns a
structured :class:`EditActionPlan` describing how to change the page.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from pptagent.editor.artifact import (
    format_structured_context,
    extract_structured_data,
)
from pptagent.presentation import SlidePage


# ── structured output contract ─────────────────────────────────


class EditActionPlan(BaseModel):
    """Structured plan returned by the editing model for one instruction."""

    type: str = Field(
        description=(
            "'content' 用基础编辑API改文字/图片/增删元素；"
            "'style' 调整配色与字体；"
            "'regenerate' 重新生成本页（换布局/重排内容）；"
            "'none' 无法执行或需要澄清"
        )
    )
    actions: list[str] = Field(
        default_factory=list,
        description=(
            "当 type='content' 时，给出按顺序执行的编辑API调用行，每行形如："
            "replace_paragraph(div_id, paragraph_id, \"新文本\") / "
            "del_paragraph(div_id, paragraph_id) / "
            "clone_paragraph(div_id, paragraph_id) / "
            "replace_image(img_id, \"图片路径\") / "
            "del_image(figure_id)。注意：不要写 slide / doc 参数；"
            "div_id/img_id/figure_id 即元素 shape_idx，paragraph_id 即段落 id。"
        ),
    )
    style: dict[str, Any] = Field(
        default_factory=dict,
        description=(
            "当 type='style' 时填写，可包含 name（内置风格名或自定义名）以及 "
            "primary/secondary/background/accent（十六进制色，不含#）、"
            "title_family/body_family/title_size/body_size/bold_titles。"
        ),
    )
    message: str = Field(
        default="",
        description="面向用户的简短中文说明，例如'已将标题缩短为……'",
    )


# ── system prompt ──────────────────────────────────────────────

SYSTEM_PROMPT = (
    "你是一名专业的 PowerPoint 单页编辑助手。你只需要修改用户选中的【这一页】，"
    "绝不改动其他页面，也绝不需要重新生成整套幻灯片。\n"
    "你可以通过以下基础编辑 API 直接修改页面（系统已自动把当前页 slide / doc 绑定，"
    "你【不要】再写 slide / doc 参数）：\n"
    "  replace_paragraph(div_id, paragraph_id, \"新文本\")\n"
    "  del_paragraph(div_id, paragraph_id)\n"
    "  clone_paragraph(div_id, paragraph_id)\n"
    "  replace_image(img_id, \"图片路径\")\n"
    "  del_image(figure_id)\n"
    "其中 div_id / img_id / figure_id 就是页面元素里的 shape_idx，paragraph_id 就是段落 id。\n"
    "指令分类规则：\n"
    "  1) 文本/要点/图片说明的增删改、精简、润色 → type='content'，用上面的 API。\n"
    "  2) 仅调整配色、字体、风格（如“换成科技风”“标题改红”）→ type='style'。\n"
    "  3) 整页重排、换布局、按指令重新生成本页内容 → type='regenerate'。\n"
    "  4) 信息不足或无法执行 → type='none'，并在 message 中说明。\n"
    "注意：\n"
    "  - 只输出 JSON，字段严格符合给定结构。content 的 actions 必须是上述 API 调用行。\n"
    "  - 多轮对话中“再短一点 / 换一个说法”等指代，要基于最近几轮对话和当前页面内容理解。\n"
    "  - 保持与全局大纲/标题的一致性，不要引入与主题无关的无关内容。\n"
    "  - 文本中可包含 Markdown（**加粗** *斜体* 等），会被正确渲染。"
)


# ── builders ───────────────────────────────────────────────────


def build_user_prompt(
    slide: SlidePage,
    instruction: str,
    outline: str | None = None,
    element_id: int | None = None,
    recent_dialogue: list[dict[str, Any]] | None = None,
    current_revision: int | None = None,
) -> str:
    """Build the user prompt for one conversational edit instruction."""
    data = extract_structured_data(slide)
    sections: list[str] = []

    if outline:
        sections.append(f"【全局大纲 / 标题】\n{outline.strip()}")

    sections.append("【当前页面结构化内容】\n" + format_structured_context(data))

    if element_id is not None:
        sections.append(
            f"【用户指定元素】本次只修改 shape_idx={element_id} 对应的元素。"
        )

    if recent_dialogue:
        turns = []
        for turn in recent_dialogue[-6:]:
            role = "用户" if turn.get("role") == "user" else "助手"
            turns.append(f"{role}: {turn.get('content', '')}")
        sections.append("【最近对话（用于理解指代）】\n" + "\n".join(turns))

    if current_revision is not None:
        sections.append(f"【当前版本号】第 {current_revision} 版")

    sections.append(f"【用户新指令】\n{instruction}")

    return "\n\n".join(sections)
