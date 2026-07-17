"""Edge-case regression tests for the direction-D bug fixes.

Locks in the fixes from the code review:
- LLM planner parses a bare JSON array (not just ``{"ops":...}``)
- ``OP_REPLACE_CONTENT`` rewrites all paragraphs
- a concrete bad ``element_id``/``paragraph_id`` fails instead of
  silently editing the first element
- a mid-plan failure rolls the live slide back to its pre-plan state

Run from the repo root:
    PYTHONPATH=. python pptagent/editor/test_edge_cases.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_template():
    from pptagent.presentation import Presentation
    from pptagent.utils import Config
    return Presentation.from_file(
        str(_REPO_ROOT / "pptagent" / "templates" / "default" / "source.pptx"),
        Config(tempfile.mkdtemp()))


class _FixedPlan:
    """A planner that always returns the same plan (ignores instruction)."""
    def __init__(self, ops, rationale="fixed"):
        from pptagent.editor.edit_plan import EditPlan
        self._plan = EditPlan(ops=ops, rationale=rationale)

    async def plan(self, context):
        return self._plan


class _FakeModel:
    """Stand-in for AsyncLLM that returns a canned payload."""
    def __init__(self, payload):
        self.payload = payload

    async def __call__(self, content, images=None, **kw):
        return self.payload


# ── 1. LLM planner parses a bare JSON array ───────────────────

async def test_llm_planner_bare_array():
    from pptagent.editor import LLMEditPlanner, build_edit_service
    from pptagent.editor.edit_plan import OP_REPLACE_TEXT

    prs = _load_template()
    # a model that returns a bare JSON list (the format the prompt asks for)
    planner = LLMEditPlanner(_FakeModel([{"op": "replace_text",
                                          "text": "hello"}]))
    svc = build_edit_service(prs, workspace_root=tempfile.mkdtemp(),
                             planner=planner)
    sid = svc.slide_ids()[1]
    r = await svc.chat(sid, "把标题改成 hello")
    assert r.success and r.changed, f"LLM bare-array plan should apply: {r.error}"
    assert r.plan and r.plan["ops"], "plan should have ops"
    assert r.plan["ops"][0]["op"] == OP_REPLACE_TEXT
    print("  ✓ LLM planner parses bare JSON array → EditPlan applies")


# ── 2. REPLACE_CONTENT rewrites all paragraphs ────────────────

async def test_replace_content():
    from pptagent.editor import build_edit_service
    from pptagent.editor.edit_plan import EditOp, OP_REPLACE_CONTENT

    prs = _load_template()
    svc = build_edit_service(
        prs, workspace_root=tempfile.mkdtemp(),
        planner=_FixedPlan([EditOp(op=OP_REPLACE_CONTENT,
                                   paragraphs=["第一点", "第二点", "第三点"])]))
    sid = svc.slide_ids()[1]
    slide = svc.get_slide(sid)
    # the body = the shape with the most text; track the OBJECT (the op
    # mutates it in place, so the same reference reflects the new state)
    body = max(
        (s for s in slide.shapes
         if hasattr(s, "text_frame") and s.text_frame.is_textframe),
        key=lambda s: sum(len(p.text) for p in s.text_frame.paragraphs
                          if getattr(p, "idx", -1) != -1))
    before_n = sum(1 for p in body.text_frame.paragraphs
                   if getattr(p, "idx", -1) != -1)

    r = await svc.chat(sid, "重写正文为三点")
    assert r.success and r.changed, f"replace_content should apply: {r.error}"
    # the body object is mutated in place, so check it directly
    texts = [p.text for p in body.text_frame.paragraphs
             if getattr(p, "idx", -1) != -1]
    assert texts == ["第一点", "第二点", "第三点"], \
        f"body should be exactly the 3 new paragraphs, got {texts}"
    print(f"  ✓ REPLACE_CONTENT rewrote {before_n} paras → 3 exactly")


# ── 3. bad paragraph_id fails, does not edit para 0 ───────────

async def test_bad_paragraph_id_fails():
    from pptagent.editor import build_edit_service
    from pptagent.editor.edit_plan import EditOp, OP_DELETE_PARAGRAPH

    prs = _load_template()
    svc = build_edit_service(
        prs, workspace_root=tempfile.mkdtemp(),
        planner=_FixedPlan([EditOp(op=OP_DELETE_PARAGRAPH,
                                   paragraph_id=999)]))
    sid = svc.slide_ids()[1]
    slide = svc.get_slide(sid)
    body = max(
        (s for s in slide.shapes
         if hasattr(s, "text_frame") and s.text_frame.is_textframe),
        key=lambda s: sum(len(p.text) for p in s.text_frame.paragraphs
                          if getattr(p, "idx", -1) != -1))
    before_texts = [p.text for p in body.text_frame.paragraphs
                    if getattr(p, "idx", -1) != -1]

    r = await svc.chat(sid, "删除不存在的段落")
    assert not r.success, "bad paragraph_id should fail"
    after_texts = [p.text for p in body.text_frame.paragraphs
                   if getattr(p, "idx", -1) != -1]
    assert after_texts == before_texts, \
        "bad-id failure must not mutate the slide"
    # current revision must be unchanged (failed draft path)
    assert svc.current_revision(sid) == 1, "failed draft must not promote current"
    print("  ✓ bad paragraph_id fails op + leaves slide + current unchanged")


# ── 4. mid-plan failure rolls the live slide back ─────────────

async def test_partial_failure_rollback():
    from pptagent.editor import build_edit_service
    from pptagent.editor.edit_plan import EditOp, OP_REPLACE_TEXT, OP_DELETE_PARAGRAPH

    prs = _load_template()
    # op1: a legit text replace (will mutate the live slide); op2: bad id (fails)
    svc = build_edit_service(
        prs, workspace_root=tempfile.mkdtemp(),
        planner=_FixedPlan([
            EditOp(op=OP_REPLACE_TEXT, text="SHOULD_BE_ROLLED_BACK"),
            EditOp(op=OP_DELETE_PARAGRAPH, paragraph_id=999),
        ]))
    sid = svc.slide_ids()[1]
    slide = svc.get_slide(sid)
    body = max(
        (s for s in slide.shapes
         if hasattr(s, "text_frame") and s.text_frame.is_textframe),
        key=lambda s: sum(len(p.text) for p in s.text_frame.paragraphs
                          if getattr(p, "idx", -1) != -1))
    before_texts = [p.text for p in body.text_frame.paragraphs
                    if getattr(p, "idx", -1) != -1]

    r = await svc.chat(sid, "两步计划，第二步必败")
    assert not r.success, "plan with a bad op2 should fail overall"
    # rollback REPLACES slide.shapes with fresh deepcopies, so re-fetch
    # the body from the live slide (the pre-chat `body` ref is detached)
    body2 = max(
        (s for s in slide.shapes
         if hasattr(s, "text_frame") and s.text_frame.is_textframe),
        key=lambda s: sum(len(p.text) for p in s.text_frame.paragraphs
                          if getattr(p, "idx", -1) != -1))
    after_texts = [p.text for p in body2.text_frame.paragraphs
                   if getattr(p, "idx", -1) != -1]
    assert "SHOULD_BE_ROLLED_BACK" not in " ".join(after_texts), \
        "op1's mutation must be rolled back when op2 fails"
    assert after_texts == before_texts, \
        "live slide must return to pre-plan state on partial failure"
    assert svc.current_revision(sid) == 1, "failed draft must not promote current"
    print("  ✓ mid-plan failure rolls the live slide back to pre-plan state")


# ── 5. EventBus replay-after-subscribe (no gap loss) ─────────

async def test_event_replay_no_gap():
    from pptagent.editor.events import EventBus
    import asyncio
    bus = EventBus("t1", tempfile.mkdtemp())
    # publish 2 events, then start a subscriber from_seq=0, then publish
    # more concurrently to check the subscribe-first ordering
    for i in range(2):
        bus.publish(__import__("pptagent.editor.events", fromlist=["GenerationEvent"]).GenerationEvent(
            task_id="t1", seq=0, type="edit.started", message=f"e{i}"))
    received = []

    async def consume():
        async for evt in bus.events(from_seq=0):
            received.append(evt.seq)
            if len(received) >= 4:
                break

    consumer = asyncio.create_task(consume())
    await asyncio.sleep(0.05)  # let replay + subscribe settle
    # publish 2 more — these must arrive via the live queue
    for i in range(2):
        bus.publish(__import__("pptagent.editor.events", fromlist=["GenerationEvent"]).GenerationEvent(
            task_id="t1", seq=0, type="edit.applied", message=f"late{i}"))
    await asyncio.wait_for(consumer, timeout=3)
    assert received == sorted(received), f"seq must be monotonic: {received}"
    assert set(received) == {1, 2, 3, 4}, \
        f"all 4 events (replay + live) must arrive, got {received}"
    print(f"  ✓ EventBus replay+live: received {received} (no gap loss)")


async def main():
    print("\n" + "=" * 60)
    print("  PPTAgent Editor — Edge-case regression tests")
    print("=" * 60 + "\n")
    passed = failed = 0
    for name, coro in [
        ("llm_bare_array", test_llm_planner_bare_array()),
        ("replace_content", test_replace_content()),
        ("bad_paragraph_id", test_bad_paragraph_id_fails()),
        ("partial_failure_rollback", test_partial_failure_rollback()),
        ("event_replay_no_gap", test_event_replay_no_gap()),
    ]:
        try:
            await coro
            passed += 1
            print(f"  [{name}] PASSED\n")
        except Exception as e:
            failed += 1
            import traceback
            print(f"  [{name}] FAILED: {e}")
            traceback.print_exc()
    print(f"{'=' * 60}")
    print(f"  Results: {passed} passed, {failed} failed")
    print(f"{'=' * 60}\n")
    return failed == 0


if __name__ == "__main__":
    sys.exit(0 if asyncio.run(main()) else 1)
