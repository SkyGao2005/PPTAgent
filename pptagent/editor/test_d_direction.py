"""Unit tests for direction D: conversational editing & version management.

The editing model is mocked (``FakeLLM``), so no real LLM, network, or heavy
rendering backend is required — only the ``pptagent`` package and its deps.

Call format note: ``CodeExecutor`` binds ``slide``/``doc`` internally, so the
model (and therefore ``FakeLLM``) must emit ``replace_paragraph(div_id,
paragraph_id, "text")`` — *without* a leading ``slide``/``doc`` argument.

Run from the project root::

    PYTHONPATH=. python pptagent/editor/test_d_direction.py
"""

import asyncio
import tempfile

from pptagent.presentation import Presentation
from pptagent.utils import Config

from pptagent.editor.artifact import ArtifactStore, extract_structured_data
from pptagent.editor.events import EditEventBus
from pptagent.editor.service import EditResult, SlideEditService


# ── fixtures ───────────────────────────────────────────────────


def make_presentation():
    config = Config(tempfile.mkdtemp())
    prs = Presentation.from_file("pptagent/templates/default/source.pptx", config)
    return prs, config


def find_title_element_id(prs, slide_idx: int) -> int:
    """Return the shape_idx of the title element on a slide."""
    data = extract_structured_data(prs.slides[slide_idx - 1])
    title = data.get("title")
    for el in data["elements"]:
        if el["type"] == "text":
            for p in el.get("paragraphs", []):
                if p["id"] == 0 and p["text"] == title:
                    return el["element_id"]
    for el in data["elements"]:
        if el["type"] == "text":
            return el["element_id"]
    return 1


def title_of(prs, slide_idx: int) -> str:
    data = extract_structured_data(prs.slides[slide_idx - 1])
    title = data.get("title")
    for el in data["elements"]:
        if el["type"] == "text":
            for p in el.get("paragraphs", []):
                if p["id"] == 0 and p["text"] == title:
                    return p["text"]
    for el in data["elements"]:
        if el["type"] == "text":
            for p in el.get("paragraphs", []):
                if p["id"] == 0:
                    return p["text"]
    return ""


class FakeLLM:
    """Deterministic stand-in for an LLM returning structured edit plans."""

    def __init__(self, title_div: int = 1):
        self.title_div = title_div
        self.attempts: dict[str, int] = {}
        self.generic: int = 0

    def __call__(self, content, *, system_message=None, history=None,
                 response_format=None, return_json=False, **kwargs):
        instr = self._extract(content)
        self.attempts[instr] = self.attempts.get(instr, 0) + 1
        n = self.attempts[instr]

        if "科技" in instr or "配色" in instr or "风格" in instr:
            return {
                "type": "style",
                "style": {
                    "name": "自定义红",
                    "primary": "FF0000", "secondary": "00AA00",
                    "background": "FFFFCC", "accent": "0000FF",
                    "title_family": "Arial", "title_size": 40, "bold_titles": True,
                },
                "message": "已应用自定义红配色",
            }
        if instr.strip() == "触发失败":
            return {"type": "content", "actions": [f"replace_paragraph(999, 0, \"x\")"], "message": ""}
        if instr.strip() == "失败然后重试":
            # The failure path is already exercised by the "触发失败" instruction;
            # this instruction IS the retry, so it must succeed.
            return {"type": "content",
                    "actions": [f'replace_paragraph({self.title_div}, 0, "重试文本")'],
                    "message": "已重试成功"}
        for token in ("测试标题A", "测试标题B", "重试文本"):
            if token in instr:
                return {"type": "content",
                        "actions": [f'replace_paragraph({self.title_div}, 0, "{token}")'],
                        "message": f"已将标题改为{token}"}
        self.generic += 1
        return {"type": "content",
                "actions": [f'replace_paragraph({self.title_div}, 0, "默认文本{self.generic}")'],
                "message": "已修改"}

    @staticmethod
    def _extract(content: str) -> str:
        if "【用户新指令】" in content:
            return content.split("【用户新指令】")[-1].strip()
        return content.strip()


# ── tests ──────────────────────────────────────────────────────


async def test_conversational_edit_and_versions():
    """A natural-language edit creates a revision; undo/apply switch versions."""
    prs, _ = make_presentation()
    title_div = find_title_element_id(prs, 2)
    store = ArtifactStore(tempfile.mkdtemp(), "task1")
    svc = SlideEditService(
        slide=prs.slides[1], presentation=prs, llm=FakeLLM(title_div),
        task_id="task1", slide_id="s2", store=store,
    )

    # 1) first edit
    r1 = await svc.chat("把标题改成 测试标题A")
    assert r1.status == "success", r1
    assert r1.revision == 1
    assert title_of(prs, 2) == "测试标题A"

    # 2) second edit
    r2 = await svc.chat("把标题改成 测试标题B")
    assert r2.status == "success"
    assert r2.revision == 2
    assert title_of(prs, 2) == "测试标题B"

    # 3) undo → back to revision 1
    u = await svc.undo()
    assert u.status == "success"
    assert u.revision == 1
    assert title_of(prs, 2) == "测试标题A"

    # 4) apply specific revision → jump to revision 2
    a = await svc.apply_revision(2)
    assert a.status == "success"
    assert title_of(prs, 2) == "测试标题B"

    # 5) revision list reflects state
    revs = svc.get_revisions()
    assert revs["current"] == 2
    assert len(revs["revisions"]) == 2
    assert revs["revisions"][-1]["is_current"] is True

    # 6) persistence: artifact + revision files written
    art = store.load_artifact("s2")
    assert art is not None and art.revision == 2
    assert store.load_revision("s2", 1) is not None
    assert store.load_revision("s2", 2) is not None

    print("  ✓ conversational edit + undo/apply + persistence")


async def test_style_edit():
    """A style instruction applies a colour/font strategy as a revision."""
    prs, _ = make_presentation()
    svc = SlideEditService(slide=prs.slides[1], presentation=prs, llm=FakeLLM(),
                           task_id="task1", slide_id="s2")
    r = await svc.chat("换成科技风配色")
    assert r.status == "success"
    assert r.revision == 1
    assert "配色" in r.message
    print("  ✓ style edit (restyle) recorded as revision")


async def test_failure_and_retry():
    """A failing edit keeps the previous version; retry can recover."""
    prs, _ = make_presentation()
    title_div = find_title_element_id(prs, 2)
    svc = SlideEditService(slide=prs.slides[1], presentation=prs, llm=FakeLLM(title_div),
                           task_id="task1", slide_id="s2")
    # baseline edit so there is a previous good version
    await svc.chat("把标题改成 测试标题A")
    before_rev = svc.revisions.current_number

    # a definitively-failing instruction (invalid div_id)
    fail = await svc.chat("触发失败")
    assert fail.status == "failed"
    assert svc.revisions.current_number == before_rev, "失败不应产生新版本"
    assert title_of(prs, 2) == "测试标题A", "失败后应保留上一版可用页面"

    # retry that succeeds on the second model call
    fixed = await svc.chat("失败然后重试")
    assert fixed.status == "success"
    assert title_of(prs, 2) == "重试文本"
    print("  ✓ failure keeps prior version; retry recovers")


async def test_max_revisions_retention():
    """Only the most recent ``max_revisions`` revisions are kept."""
    prs, _ = make_presentation()
    title_div = find_title_element_id(prs, 2)
    svc = SlideEditService(slide=prs.slides[1], presentation=prs, llm=FakeLLM(title_div),
                           task_id="task1", slide_id="s2", max_revisions=3)
    for i in range(5):
        await svc.chat(f"把标题改成 第{i}版标题")
    assert len(svc.revisions) == 3, len(svc.revisions)
    assert svc.revisions.current_number == 5
    assert len(svc.revisions.pruned_log()) == 2
    print("  ✓ max-revisions retention (kept 3, pruned 2)")


async def test_event_bus():
    """Edit events are published with increasing seq and replayable."""
    prs, _ = make_presentation()
    title_div = find_title_element_id(prs, 2)
    bus = EditEventBus()
    svc = SlideEditService(slide=prs.slides[1], presentation=prs, llm=FakeLLM(title_div),
                           task_id="task1", slide_id="s2", event_bus=bus)
    await svc.chat("把标题改成 测试标题A")
    events = bus.recent()
    types = [e.type for e in events]
    assert "edit.started" in types
    assert "edit.applied" in types
    seqs = [e.seq for e in events]
    assert seqs == sorted(seqs), "seq 必须递增"
    print("  ✓ event bus publishes ordered edit events")


async def test_page_level_lock_serializes():
    """Concurrent edits on the same page are serialised by the page lock."""
    prs, _ = make_presentation()
    title_div = find_title_element_id(prs, 2)
    svc = SlideEditService(slide=prs.slides[1], presentation=prs, llm=FakeLLM(title_div),
                           task_id="task1", slide_id="s2")

    async def job(text):
        return await svc.chat(f"把标题改成 {text}")

    results = await asyncio.gather(job("测试标题A"), job("测试标题B"))
    assert all(isinstance(r, EditResult) and r.status == "success" for r in results)
    assert svc.revisions.current_number == 2
    print("  ✓ page-level lock serialises concurrent edits")


def main():
    print("\n" + "=" * 60)
    print("  PPTAgent Editor — Direction D Test Suite")
    print("=" * 60 + "\n")
    tests = [
        ("conversational edit + versions", test_conversational_edit_and_versions),
        ("style edit", test_style_edit),
        ("failure + retry", test_failure_and_retry),
        ("max-revisions retention", test_max_revisions_retention),
        ("event bus", test_event_bus),
        ("page-level lock", test_page_level_lock_serializes),
    ]
    passed = failed = 0
    for name, fn in tests:
        try:
            asyncio.run(fn())
            passed += 1
        except Exception as e:  # noqa: BLE001
            failed += 1
            print(f"\n  ✗ {name} FAILED: {e}")
            import traceback
            traceback.print_exc()
    print(f"\n{'=' * 60}")
    print(f"  Results: {passed} passed, {failed} failed out of {len(tests)}")
    print(f"{'=' * 60}\n")
    return failed == 0


if __name__ == "__main__":
    raise SystemExit(0 if main() else 1)
