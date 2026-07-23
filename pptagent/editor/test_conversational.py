"""Tests for conversational local editing + version management (direction D).

Runs the full pipeline on the default template with the deterministic
(model-free) planner, so it exercises:

- build_edit_service → register slides → initial revision each
- chat() natural-language edit → new revision
- multi-turn continuity ("再短一点" builds on the previous revision)
- list_revisions / current_revision
- undo / apply_revision (pointer switch, no model)
- failed draft (regen with NoopRegenBackend) → current untouched
- retry
- keep-last-10 pruning
- export reads latest successful revision
- edit.* events with increasing seq in events.jsonl

Run from the repo root:
    PYTHONPATH=. python pptagent/editor/test_conversational.py
"""

import asyncio
import os
import sys
import tempfile
from pathlib import Path

# Make the repo importable when run directly.
_REPO_ROOT = Path(__file__).resolve().parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))


def _load_default_template():
    """Load the default template as a Presentation (13 slides)."""
    from pptagent.presentation import Presentation
    from pptagent.utils import Config
    config = Config(tempfile.mkdtemp())
    template = _REPO_ROOT / "pptagent" / "templates" / "default" / "source.pptx"
    prs = Presentation.from_file(str(template), config)
    return prs


# ───────────────────────────────────────────────────────────────

async def test_full_pipeline():
    from pptagent.editor import build_edit_service

    prs = _load_default_template()
    assert len(prs.slides) >= 2, "need at least 2 slides"
    workspace = tempfile.mkdtemp(prefix="pptagent_test_")
    service = build_edit_service(prs, workspace_root=workspace,
                                 outline={"title": "测试PPT", "outline": []})

    slide_ids = service.slide_ids()
    assert len(slide_ids) == len(prs.slides), "every slide should be registered"
    sid = slide_ids[1]  # second slide (has text)
    print(f"  ✓ registered {len(slide_ids)} slides; editing {sid[:8]}…")

    # initial revision is rev 1
    assert service.current_revision(sid) == 1, "initial revision should be 1"
    revs = service.list_revisions(sid)
    assert revs[0]["revision"] == 1 and revs[0]["is_current"]
    print("  ✓ initial revision = 1")

    # ── turn 1: 精简 ──
    r1 = await service.chat(sid, "精简一下这页的文字")
    assert r1.success and r1.changed, f"turn1 should succeed: {r1.error}"
    assert r1.revision == 2, f"turn1 should create rev 2, got {r1.revision}"
    print(f"  ✓ turn1 '精简' → rev{r1.revision}: {r1.summary}")

    # ── turn 2: 再短一点 (continuity — must build on rev 2) ──
    before = _first_text(prs.slides[1])
    r2 = await service.chat(sid, "再短一点")
    assert r2.success and r2.changed, f"turn2 should succeed: {r2.error}"
    assert r2.revision == 3, f"turn2 should create rev 3, got {r2.revision}"
    after = _first_text(prs.slides[1])
    assert after != before, "turn2 should keep shortening (continuity)"
    print(f"  ✓ turn2 '再短一点' → rev{r2.revision} (builds on rev2): "
          f"'{before[:24]}…' → '{after[:24]}…'")

    # ── list / current ──
    revs = service.list_revisions(sid)
    assert [r["revision"] for r in revs] == [3, 2, 1], "newest-first order"
    assert revs[0]["is_current"]
    print(f"  ✓ revisions: {[r['revision'] for r in revs]} (newest first)")

    # ── undo (no model call) ──
    u = await service.undo(sid)
    assert u.success and u.revision == 2, f"undo → rev2, got {u.revision}"
    assert service.current_revision(sid) == 2
    print(f"  ✓ undo → rev{u.revision} (pointer switch, no model)")

    # ── apply_revision(3) (no model call) ──
    a = await service.apply_revision(sid, 3)
    assert a.success and a.revision == 3, f"apply(3) failed: {a.error}"
    assert service.current_revision(sid) == 3
    print(f"  ✓ apply_revision(3) → rev{a.revision}")

    # ── restyle (换配色) ──
    r3 = await service.chat(sid, "换成商务风格")
    assert r3.success and r3.changed, f"restyle should succeed: {r3.error}"
    print(f"  ✓ turn '换配色→商务' → rev{r3.revision}: {r3.summary}")

    # ── failed draft: regen op with NoopRegenBackend ──
    cur_before_fail = service.current_revision(sid)
    rf = await service.chat(sid, "重新生成本页")
    assert not rf.success, "regen without backend should fail"
    assert rf.error is not None
    assert service.current_revision(sid) == cur_before_fail, \
        "failed draft must NOT promote current revision"
    print(f"  ✓ failed draft '重新生成' → current stays rev{cur_before_fail}")

    # ── retry (re-runs the failed instruction; still fails for noop) ──
    rt = await service.retry(sid)
    assert not rt.success, "retry of noop-regen should still fail"
    assert service.current_revision(sid) == cur_before_fail
    print("  ✓ retry → still fails (noop backend), current unchanged")

    # ── events: edit.* with increasing seq ──
    history = service.bus.history()
    seqs = [e.seq for e in history]
    assert seqs == sorted(seqs), "seq must be monotonic"
    assert seqs and seqs[-1] >= 1
    types = [e.type for e in history]
    assert "edit.started" in types
    assert "edit.applied" in types
    assert "edit.failed" in types
    assert "edit.reverted" in types
    print(f"  ✓ events: {len(history)} total, types={sorted(set(types))}")

    # events.jsonl persisted on disk
    log_path = service.workspace.events_path
    assert log_path.exists(), "events.jsonl should be persisted"
    print(f"  ✓ events.jsonl persisted ({log_path.stat().st_size} bytes)")

    return service, sid


async def test_keep_last_10():
    from pptagent.editor import build_edit_service
    prs = _load_default_template()
    service = build_edit_service(prs, workspace_root=tempfile.mkdtemp(),
                                 keep_revisions=4, planner=_CounterPlanner())
    sid = service.slide_ids()[1]
    for i in range(8):
        r = await service.chat(sid, f"第{i+1}次修改")
        assert r.success, f"edit {i} failed: {r.error}"
    revs = [r["revision"] for r in service.list_revisions(sid)]
    # initial rev1 + 8 edits = revs 2..9; keep-4 prunes to [6,7,8,9]
    assert len(revs) == 4, f"keep-last-N pruning failed: {revs}"
    assert service.current_revision(sid) == 9, f"current should be 9, got {service.current_revision(sid)}"
    assert max(revs) == 9, f"max revision should be 9, got {revs}"
    print(f"  ✓ keep-last-4 pruning: {len(revs)} revisions remain "
          f"({revs}), current={service.current_revision(sid)}")


async def test_export():
    from pptagent.editor import build_edit_service
    prs = _load_default_template()
    service = build_edit_service(prs, workspace_root=tempfile.mkdtemp())
    sid = service.slide_ids()[1]
    await service.chat(sid, "精简")
    await service.chat(sid, "再短一点")
    out = Path(tempfile.mktemp(suffix=".pptx"))
    result = await service.export(str(out))
    assert result["success"], f"export failed: {result}"
    assert os.path.exists(out) and os.path.getsize(out) > 1000
    assert sid in result["revisions"], "manifest should record per-slide rev"
    print(f"  ✓ export → {out.name} ({os.path.getsize(out)} bytes), "
          f"revisions recorded: {result['revisions'][sid]}")
    # export manifest on disk
    man = service.workspace.exports_dir / "export_manifest.json"
    assert man.exists(), "export manifest should be persisted"
    print("  ✓ export manifest persisted")


def _first_text(slide) -> str:
    """Return the first paragraph of the shape with the most text.

    Mirrors ``EditPlanExecutor._resolve_shape(None)`` so the continuity
    check observes the element that actually gets edited.
    """
    best, best_len = None, -1
    for shape in getattr(slide, "shapes", []):
        if not (hasattr(shape, "text_frame") and shape.text_frame.is_textframe):
            continue
        total = sum(len(p.text) for p in shape.text_frame.paragraphs
                    if getattr(p, "idx", -1) != -1)
        if total > best_len:
            best, best_len = shape, total
    if best is None:
        return ""
    for p in best.text_frame.paragraphs:
        if getattr(p, "idx", -1) != -1 and p.text:
            return p.text
    return ""


class _CounterPlanner:
    """A test planner that emits a distinct text each turn.

    Guarantees every turn produces a changed revision, so pruning can be
    exercised deterministically without a model.
    """

    def __init__(self):
        self.n = 0

    async def plan(self, context):
        from pptagent.editor.edit_plan import EditPlan, EditOp, OP_REPLACE_TEXT
        self.n += 1
        return EditPlan(
            ops=[EditOp(op=OP_REPLACE_TEXT, text=f"rev-{self.n}",
                         note="counter")],
            rationale="counter", needs_regeneration=False,
            raw=f"rev-{self.n}")


# ───────────────────────────────────────────────────────────────

async def main():
    print("\n" + "=" * 60)
    print("  PPTAgent Editor — Conversational Edit + Version Tests")
    print("=" * 60 + "\n")
    passed = failed = 0
    for name, coro in [
        ("full_pipeline", test_full_pipeline()),
        ("keep_last_N", test_keep_last_10()),
        ("export", test_export()),
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
    ok = asyncio.run(main())
    sys.exit(0 if ok else 1)
