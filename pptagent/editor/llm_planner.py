"""LLM-backed edit planner.

Turns a natural-language instruction + page context into an ``EditPlan``
by calling the repo's ``AsyncLLM`` (the same interface the coder/induction
agents use: ``await vision_model(content, images=path)``).

The prompt gives the model exactly the direction-D context — page
screenshot, structured content with ``shape_idx`` for addressing, the
layout's element schema, the PPT outline, and the recent turns — and
asks for a JSON list of ``EditOp``s. Falls back gracefully when no
screenshot or no model is available.
"""

from __future__ import annotations

import json
from typing import Any

from pptagent.editor.edit_plan import EditPlan, EditPlanner, parse_edit_plan


# ── prompt skeleton ───────────────────────────────────────────

_SYSTEM = (
    "你是一个PPT单页编辑助手。用户会用自然语言描述对当前这一页的修改意图，"
    "你需要把它拆解成一组结构化编辑操作(EditOp)，以JSON数组返回。"
 "只修改当前页，不要影响其他页。"
)

_OP_SCHEMA = """\
可用的编辑操作(每条是一个JSON对象，只填需要的字段):
- {"op":"replace_text","element_id":<int|str|null>,"paragraph_id":<int|null>,"text":"新文本"}  替换一段文字
- {"op":"replace_content","element_id":<int|str>,"paragraphs":["第一段","第二段"]}             重写某元素全部段落
- {"op":"add_paragraph","element_id":<int|str>,"text":"新增的要点"}                              新增一段
- {"op":"delete_paragraph","element_id":<int|str>,"paragraph_id":<int|"last">}                  删除一段
- {"op":"delete_element","element_id":<int|str>}                                                删除整个元素
- {"op":"replace_image","element_id":<int|str>,"image_path":"图片路径"}                          替换图片
- {"op":"restyle","style":"简约|商务|科技|政务"}                                                  换配色/风格
- {"op":"relayout","layout_name":"布局名或null"}                                                  换布局(需重生成)
- {"op":"regenerate"}                                                                            重新生成本页
- {"op":"set_raw_html","html":"<完整HTML>"}                                                       HTML模式重写整页
说明: element_id 为整数时等于元素的 shape_idx(见下方structured_data); 为字符串时匹配元素名; 为null时取第一个文本元素。paragraph_id 为段落 idx,"last" 表示最后一段。"""


def _build_prompt(context: Any) -> str:
    """Assemble the user-turn prompt from an ``EditContext``."""
    ctx_dict = context.to_prompt_dict() if hasattr(context, "to_prompt_dict") else {}
    slide = ctx_dict.get("slide", {})
    pres = ctx_dict.get("presentation", {})
    lines = [
        f"【用户指令】{context.instruction}",
    ]
    if getattr(context, "selected_element", None):
        lines.append(f"【用户选中的元素】{context.selected_element}")
    lines.append("")
    lines.append("【当前页结构化数据】(含 shape_idx 用于 element_id 寻址)")
    lines.append(json.dumps(slide, ensure_ascii=False, indent=2))
    lines.append("")
    lines.append("【全局标题与大纲】(保持一致，勿改其他页)")
    lines.append(json.dumps(pres, ensure_ascii=False, indent=2))
    lines.append("")
    lines.append("【最近对话】")
    for t in ctx_dict.get("recent_turns", [])[-6:]:
        lines.append(f"{t.get('role','user')}: {t.get('content','')}")
    lines.append("")
    lines.append(_OP_SCHEMA)
    lines.append("")
    lines.append("请只输出一个JSON数组(可放在```json```代码块中)，不要解释。")
    return "\n".join(lines)


# ── LLM planner ───────────────────────────────────────────────

class LLMEditPlanner:
    """Plans edits by prompting the vision/language model.

    Args:
        model:        an ``AsyncLLM`` (the repo's call interface).
        screenshot_provider: optional callable returning a screenshot
                      file path for the current page; passed to the
                      model as the image. If ``None``, the call is
                      text-only (still effective given the structured
                      content in the prompt).
        system_message: override the default system prompt.
    """

    def __init__(self, model, screenshot_provider=None,
                 system_message: str | None = None):
        self.model = model
        self._screenshot = screenshot_provider
        self._system = system_message or _SYSTEM

    async def plan(self, context: Any) -> EditPlan:
        prompt = _build_prompt(context)

        # screenshot: a file path on disk (AsyncLLM reads+base64s it)
        image_path = None
        if self._screenshot is not None:
            try:
                result = self._screenshot(context)
                if isinstance(result, str):
                    image_path = result
                elif isinstance(result, tuple) and len(result) >= 1:
                    image_path = result[0]
            except Exception:
                image_path = None

        try:
            if image_path is not None:
                raw = await self.model(
                    prompt, images=image_path,
                    system_message=self._system, return_json=True)
            else:
                raw = await self.model(
                    prompt, system_message=self._system, return_json=True)
        except Exception as e:  # noqa: BLE001
            return EditPlan(ops=[], rationale=f"模型调用失败: {e}",
                              needs_regeneration=False, raw=str(e))

        # AsyncLLM with return_json=True may return a dict, a list (bare
        # JSON array — the format the prompt asks for), or a str. Coerce
        # lists/dicts to a JSON string so parse_edit_plan can parse it;
        # str(list) would emit Python single-quoted repr and break JSON.
        if isinstance(raw, list):
            raw = json.dumps(raw)
        elif isinstance(raw, dict):
            if "ops" in raw:
                raw = json.dumps(raw)
            elif "op" in raw:
                raw = json.dumps([raw])
            else:
                raw = json.dumps(raw)
        elif not isinstance(raw, str):
            raw = str(raw)

        plan = parse_edit_plan(raw)
        if not plan.rationale:
            plan.rationale = "LLM 规划"
        return plan
