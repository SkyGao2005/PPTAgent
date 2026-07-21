"""Structured edit plan: the contract between the model and the editor.

A conversational edit resolves a natural-language instruction into an
``EditPlan`` — a list of ``EditOp``s the executor can apply deterministically.
Keeping this as an explicit, inspectable structure (rather than letting the
model mutate the slide directly) gives us:

- **reproducibility**: the same plan always yields the same revision
- **safety**: no op touches a slide other than the target page
- **mockability**: tests and demos can build a plan without a model

P0 operation set (direction D):
    replace_text, add_paragraph, delete_paragraph, replace_image,
    restyle (换配色/格式), relayout (换布局), regenerate (重新生成本页)
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


# ── operation kinds ───────────────────────────────────────────

OP_REPLACE_TEXT = "replace_text"          # rewrite a paragraph's text
OP_REPLACE_CONTENT = "replace_content"    # rewrite all paragraphs of an element
OP_ADD_PARAGRAPH = "add_paragraph"
OP_DELETE_PARAGRAPH = "delete_paragraph"
OP_DELETE_ELEMENT = "delete_element"
OP_REPLACE_IMAGE = "replace_image"
OP_RESTYLE = "restyle"                     # 换配色 / 字体
OP_RELAYOUT = "relayout"                  # 换布局 (template mode)
OP_REGENERATE = "regenerate"              # 重新生成本页
OP_SET_RAW_HTML = "set_raw_html"         # HTML mode: replace page HTML


@dataclass
class EditOp:
    """One atomic edit on the target slide."""

    op: str
    element_id: str | None = None         # element name, e.g. "title"/"body_1"
    paragraph_id: int | None = None
    text: str | None = None
    paragraphs: list[str] | None = None    # for replace_content / add lists
    image_path: str | None = None
    style: str | None = None              # named style for restyle
    palette: dict[str, str] | None = None
    fonts: dict[str, Any] | None = None
    layout_name: str | None = None        # for relayout
    html: str | None = None               # for set_raw_html
    note: str = ""                        # human-readable reason

    def to_dict(self) -> dict[str, Any]:
        return {k: v for k, v in self.__dict__.items() if v is not None}


@dataclass
class EditPlan:
    """A resolved plan: a list of ops + metadata."""

    ops: list[EditOp] = field(default_factory=list)
    rationale: str = ""                   # why the planner chose these ops
    needs_regeneration: bool = False       # True ⇒ executor must call regen backend
    raw: str = ""                         # original model text, for the audit log

    def to_dict(self) -> dict[str, Any]:
        return {
            "ops": [o.to_dict() for o in self.ops],
            "rationale": self.rationale,
            "needs_regeneration": self.needs_regeneration,
            "raw": self.raw,
        }


# ── parsing ───────────────────────────────────────────────────

def parse_edit_plan(text: str) -> EditPlan:
    """Robustly extract an ``EditPlan`` from model output.

    Accepts either a bare JSON array of ops, or ``{"ops": [...]}``,
    or a fenced ```json``` block. Falls back to a no-op plan so a
    malformed model reply never corrupts the slide.
    """
    raw = text
    # strip code fences if present
    fence = re.search(r"```(?:json)?\s*(.*?)```", text, re.DOTALL)
    if fence:
        text = fence.group(1).strip()

    # find the first JSON array/object
    start = min(
        [i for i in (text.find("["), text.find("{")) if i != -1],
        default=-1,
    )
    if start == -1:
        return EditPlan(ops=[], rationale="no plan parsed", raw=raw)
    try:
        data = json.loads(text[start:])
    except json.JSONDecodeError:
        return EditPlan(ops=[], rationale="invalid JSON", raw=raw)

    if isinstance(data, list):
        ops_list = data
        rationale = ""
    elif isinstance(data, dict):
        ops_list = data.get("ops", [])
        rationale = data.get("rationale", "")
    else:
        return EditPlan(ops=[], rationale="unexpected JSON shape", raw=raw)

    ops: list[EditOp] = []
    needs_regen = False
    for item in ops_list:
        if not isinstance(item, dict) or "op" not in item:
            continue
        op = EditOp(
            op=item["op"],
            element_id=item.get("element_id"),
            paragraph_id=item.get("paragraph_id"),
            text=item.get("text"),
            paragraphs=item.get("paragraphs"),
            image_path=item.get("image_path"),
            style=item.get("style"),
            palette=item.get("palette"),
            fonts=item.get("fonts"),
            layout_name=item.get("layout_name"),
            html=item.get("html"),
            note=item.get("note", ""),
        )
        if op.op in (OP_REGENERATE, OP_RELAYOUT, OP_SET_RAW_HTML):
            needs_regen = True
        ops.append(op)
    return EditPlan(ops=ops, rationale=rationale,
                    needs_regeneration=needs_regen, raw=raw)


# ── planner protocol ──────────────────────────────────────────

@runtime_checkable
class EditPlanner(Protocol):
    """Turn an edit context into an executable ``EditPlan``."""

    async def plan(self, context: Any) -> EditPlan:
        ...


# ── deterministic planner (no model needed) ───────────────────

class DeterministicPlanner:
    """Rule-based planner for common Chinese edit instructions.

    Lets the full conversational pipeline run — and be tested and
    demoed — without an LLM. It recognises a handful of intent patterns
    and emits the corresponding ``EditOp``s against the page's first
    text element. Real deployments swap in an LLM-backed planner that
    returns the same JSON shape.
    """

    # named styles understood by the StyleRegistry (see styles.py)
    _KNOWN_STYLES = ("简约", "商务", "科技", "政务")

    async def plan(self, context: Any) -> EditPlan:
        instruction = (getattr(context, "instruction", "") or "").strip()
        el = getattr(context, "selected_element", None)
        lower = instruction.lower()

        # 重新生成本页
        if any(k in instruction for k in ("重新生成", "重做本页", "重新做", "再生成本页")):
            return EditPlan(ops=[EditOp(op=OP_REGENERATE, note="重新生成本页")],
                            rationale="整页重做", needs_regeneration=True,
                            raw=instruction)

        # 换布局
        if "换布局" in instruction or "更换布局" in instruction:
            layout = None
            m = re.search(r"布局[为是改]?\s*([A-Za-z0-9_一-龥]+)", instruction)
            if m:
                layout = m.group(1)
            return EditPlan(ops=[EditOp(op=OP_RELAYOUT, layout_name=layout,
                                        note="换布局")],
                            rationale="布局类修改", needs_regeneration=True,
                            raw=instruction)

        # 换配色 / 风格
        for s in self._KNOWN_STYLES:
            if s in instruction and any(k in instruction for k in ("配色", "风格", "换成", f"换{s}", f"用{s}")):
                return EditPlan(ops=[EditOp(op=OP_RESTYLE, style=s,
                                            note=f"应用风格{s}")],
                                rationale=f"风格: {s}", raw=instruction)
        if "换配色" in instruction or "改配色" in instruction or "换个配色" in instruction:
            # default to a different built-in style than "简约"
            return EditPlan(ops=[EditOp(op=OP_RESTYLE, style="商务",
                                        note="换配色→商务")],
                            rationale="配色", raw=instruction)

        # 把 X 替换成/改为 Y
        m = re.search(r"把[「\"']?(.+?)[」\"']?\s*(?:替换成|改成|改为|换成)\s*[「\"']?(.+?)[」\"']?$", instruction)
        if m:
            return EditPlan(ops=[EditOp(op=OP_REPLACE_TEXT,
                                        element_id=el,
                                        text=m.group(2).strip(),
                                        note=f"替换文本: {m.group(1)}→{m.group(2)}")],
                            rationale="文本替换", raw=instruction)

        # 精简 / 缩短 / 再短一点 — shorten the first text element
        if any(k in instruction for k in ("精简", "缩短", "再短一点", "短一点", "再精简", "简洁")):
            return EditPlan(ops=[EditOp(op=OP_REPLACE_TEXT,
                                        element_id=el,
                                        text="__SHORTEN__",
                                        note="精简文本")],
                            rationale="精简", raw=instruction)

        # 默认: 用指令文本替换第一个文本元素(最朴素的"修改标题/正文为...")
        return EditPlan(ops=[EditOp(op=OP_REPLACE_TEXT,
                                    element_id=el,
                                    text="__INSTRUCTION__",
                                    note="按指令替换文本")],
                        rationale="默认文本替换", raw=instruction)
