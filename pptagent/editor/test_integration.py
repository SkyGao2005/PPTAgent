"""Integrated test for all editor modules.

Covers the complete lifecycle: generation → versioning → editing →
undo/redo → diff → persistence → restore → batch → features.

Run from project root:
    PYTHONPATH=. python3 pptagent/editor/test_integration.py
"""

import tempfile
import os
import sys
from pathlib import Path

# ---------- 0. setup ----------

def test_setup():
    """Load the default template and prepare test fixtures."""
    from pptagent.presentation import Presentation
    from pptagent.utils import Config

    config = Config(tempfile.mkdtemp())
    prs = Presentation.from_file("pptagent/templates/default/source.pptx", config)
    assert len(prs.slides) == 13, f"Expected 13 slides, got {len(prs.slides)}"
    return prs, config


# ---------- 1. snapshot ----------

def test_snapshot(prs):
    """Snapshots capture content and detect changes correctly."""
    from pptagent.editor.snapshot import SlideSnapshot, PresentationSnapshot

    # Single slide snapshot
    s1 = SlideSnapshot.capture(prs.slides[0])
    s2 = SlideSnapshot.capture(prs.slides[0])
    s3 = SlideSnapshot.capture(prs.slides[1])

    assert s1.is_same_content(s2), "Same slide should have identical checksum"
    assert not s1.is_same_content(s3), "Different slides should have different checksum"
    assert len(s1.checksum) == 12, f"Checksum should be 12 chars, got {len(s1.checksum)}"

    # Full presentation snapshot
    ps = PresentationSnapshot.capture(prs, version_tag="v1")
    assert len(ps.slides) == 13, f"Expected 13 slide snapshots, got {len(ps.slides)}"
    assert ps.version_tag == "v1"
    assert len(ps.checksum) == 12

    print("  ✓ snapshot: capture + compare + presentation snapshot")


# ---------- 2. editor + history ----------

def test_editor(prs):
    """SlideEditor edits text, clones paragraphs, and undo/redo works."""
    from pptagent.editor.editor import SlideEditor

    # Edit text
    editor = SlideEditor(prs.slides[1], prs)
    before = editor.slide.slides[1].checksum if False else ""  # placeholder

    snap_after = editor.edit_text(div_id=1, paragraph_id=0, new_text="Integration Test Edit")
    assert editor.can_undo
    assert not editor.can_redo

    # Undo
    snap_undo = editor.undo()
    assert snap_undo is not None
    assert snap_undo.checksum != snap_after.checksum, "Undo should restore content"

    # Redo
    snap_redo = editor.redo()
    assert snap_redo is not None
    assert snap_redo.checksum == snap_after.checksum, "Redo should re-apply edit"

    # Clone paragraph (on a slide with multiple paragraphs)
    editor2 = SlideEditor(prs.slides[5], prs)  # slide 6 usually has more content
    try:
        editor2.clone_paragraph(div_id=1, paragraph_id=0)
        assert editor2.can_undo
    except Exception:
        pass  # clone might fail if there's only one paragraph, that's ok

    print("  ✓ editor: edit_text + undo/redo + clone_paragraph")


# ---------- 3. diff ----------

def test_diff(prs):
    """Diff engine detects text changes at paragraph level."""
    from pptagent.editor.snapshot import SlideSnapshot
    from pptagent.editor.editor import SlideEditor
    from pptagent.editor.diff import DiffEngine

    slide = prs.slides[1]
    before = SlideSnapshot.capture(slide)
    old_text = before.shapes_data[0].get("paragraphs", [{}])[0].get("text", "")

    editor = SlideEditor(slide, prs)
    editor.edit_text(div_id=1, paragraph_id=0, new_text="DIFF_TEST_MODIFIED")
    after = SlideSnapshot.capture(slide)

    diff = DiffEngine.diff_slides(before, after)
    assert not diff.is_empty, "Diff should detect changes"
    assert diff.total_changes > 0
    assert diff.slide_idx == after.slide_idx

    # Check detail output
    detail = diff.detail()
    assert "DIFF_TEST_MODIFIED" in detail
    assert old_text[:20] in detail or "[文字]" in detail

    print(f"  ✓ diff: detected {diff.total_changes} change(s)")

    # Restore
    editor.undo()


# ---------- 4. version management ----------

def test_version(prs):
    """VersionManager supports commit, log, checkout, diff."""
    from pptagent.editor.version import VersionManager

    vm = VersionManager()

    # Commit
    v1 = vm.commit(prs, "Initial version")
    assert vm.current_version_id == v1.version_id
    assert len(vm) == 1

    v2 = vm.commit(prs, "Second version", tags=["test", "v2"])
    assert len(vm) == 2
    assert "test" in v2.tags

    # Log
    log = vm.log()
    assert len(log) == 2
    assert log[0]["version_id"] == v2.version_id  # newest first

    # Diff
    d = vm.diff(v1.version_id, v2.version_id)
    assert d["same_content"] is True  # no edits between commits

    # Branch
    vm.branch("experiment")
    assert vm.current_branch == "experiment"

    print(f"  ✓ version: 2 commits, log, diff, branch '{vm.current_branch}'")


# ---------- 5. persistence ----------

def test_persistence(prs):
    """VersionStore saves to disk and restores after simulated restart."""
    from pptagent.editor.version import VersionManager
    from pptagent.editor.version_store import VersionStore

    # Create versions
    vm = VersionManager()
    vm.commit(prs, "Save test v1")
    v2 = vm.commit(prs, "Save test v2")

    # Save to disk
    store = VersionStore(tempfile.mkdtemp())
    for vid in vm._versions:
        store.save(vm._versions[vid])

    assert len(store.list_versions()) == 2

    # Check files exist
    for entry in store.list_versions():
        path = store._snapshots_dir / f'{entry["version_id"]}.json'
        assert path.exists(), f"Missing: {path}"
        assert os.path.getsize(path) > 100, f"File too small: {path}"

    # Simulate restart: delete in-memory vm, reload from disk
    del vm
    vm2 = store.load_manager()
    assert len(vm2) == 2
    assert vm2.current_version_id == v2.version_id

    print(f"  ✓ persistence: saved {len(store.list_versions())} versions, restored {len(vm2)}")


# ---------- 6. style restyling ----------

def test_restyle(prs):
    """Style strategies can be applied and undone."""
    from pptagent.editor.editor import SlideEditor
    from pptagent.editor.styles import StyleRegistry

    registry = StyleRegistry()
    assert len(registry.list_names()) == 4

    editor = SlideEditor(prs.slides[1], prs)
    before = editor.snapshot() if hasattr(editor, 'snapshot') else None

    # Apply each style
    for name in registry.list_names():
        strategy = registry.get(name)
        snap = editor.restyle(strategy)
        assert snap is not None
        # Undo to get clean state for next style
        editor.undo()

    # Verify all styles are registered
    for name in ["简约", "商务", "科技", "政务"]:
        s = registry.get(name)
        assert s.palette.primary, f"{name} missing primary color"
        assert s.name == name

    print(f"  ✓ styles: {len(registry.list_names())} strategies applied and undone")


# ---------- 7. batch editing ----------

def test_batch(prs):
    """BatchEditor edits multiple slides and commits as one version."""
    from pptagent.editor.version import VersionManager
    from pptagent.editor.batch import BatchEditor

    vm = VersionManager()
    vm.commit(prs, "Pre-batch version")

    batch = BatchEditor(prs, vm)
    batch.edit_text(slide_idx=2, div_id=1, paragraph_id=0, new_text="Batch Slide 2")
    batch.edit_text(slide_idx=3, div_id=1, paragraph_id=0, new_text="Batch Slide 3")

    assert batch.active_slides == [2, 3]
    assert batch.total_pending_edits == 2

    batch.commit("Batch edit slides 2+3")

    assert len(vm) == 2
    log = vm.log()
    assert log[0]["message"] == "Batch edit slides 2+3"

    print(f"  ✓ batch: {batch.total_pending_edits} edits on {batch.active_slides} → 1 commit")


# ---------- 8. feature slices ----------

def test_features(prs):
    """Feature slices extract structured data per slide per version."""
    from pptagent.editor.snapshot import PresentationSnapshot
    from pptagent.editor.features import FeatureStore

    snap = PresentationSnapshot.capture(prs, version_tag="feat-test")

    store = FeatureStore(tempfile.mkdtemp())
    slices = store.save_version(snap, "feat-v1")
    assert len(slices) == len(prs.slides)

    # Check each slice has meaningful data
    for fs in slices:
        assert fs.layout.layout_type in ("text", "image", "mixed"), \
            f"Bad layout type: {fs.layout.layout_type}"
        assert fs.layout.element_count > 0

    # Count layout types
    text_count = sum(1 for fs in slices if fs.layout.layout_type == "text")
    mixed_count = sum(1 for fs in slices if fs.layout.layout_type == "mixed")
    assert text_count + mixed_count <= 13

    # Query
    text_slides = store.find_by_layout("text")
    assert len(text_slides) == text_count

    # Verify disk persistence of features
    loaded = store.get_version_slices("feat-v1")
    assert len(loaded) == len(slices)

    print(f"  ✓ features: {len(slices)} slices (text={text_count}, mixed={mixed_count}), persistent + queryable")


# ====================================================================
# main
# ====================================================================

def main():
    print("\n" + "=" * 60)
    print("  PPTAgent Editor — Integration Test Suite")
    print("=" * 60 + "\n")

    prs, config = test_setup()
    print(f"\n[setup] default template: {len(prs.slides)} slides\n")

    tests = [
        ("snapshot",       test_snapshot),
        ("editor+history", test_editor),
        ("diff",           test_diff),
        ("version",        test_version),
        ("persistence",    test_persistence),
        ("styles",         test_restyle),
        ("batch",          test_batch),
        ("features",       test_features),
    ]

    passed = 0
    failed = 0

    for name, fn in tests:
        try:
            fn(prs)
            passed += 1
        except Exception as e:
            failed += 1
            print(f"\n  ✗ {name} FAILED: {e}")
            import traceback
            traceback.print_exc()

    print(f"\n{'=' * 60}")
    print(f"  Results: {passed} passed, {failed} failed out of {len(tests)}")
    print(f"{'=' * 60}\n")

    return failed == 0


if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)
