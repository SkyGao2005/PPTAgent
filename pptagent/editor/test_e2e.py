"""End-to-end test: complete user workflow.

Simulates the full lifecycle from a blank or template presentation
through conversational editing, version management, preview
rendering, and export — all with a mocked LLM.

Run from project root (no API key needed):
    PYTHONPATH=. python3 pptagent/editor/test_e2e.py
"""

import asyncio
import os
import tempfile
from pathlib import Path

from pptagent.presentation import Presentation
from pptagent.utils import Config

from pptagent.editor.artifact import ArtifactStore, extract_structured_data, format_structured_context
from pptagent.editor.diff import DiffEngine
from pptagent.editor.editor import SlideEditor
from pptagent.editor.event_bus import EditEventBus
from pptagent.editor.exporter import optimized_save, export_with_profile
from pptagent.editor.features import FeatureStore
from pptagent.editor.preview import PreviewRenderer
from pptagent.editor.service import EditResult, SlideEditService
from pptagent.editor.snapshot import SlideSnapshot, PresentationSnapshot
from pptagent.editor.styles import StyleRegistry
from pptagent.editor.version import VersionManager
from pptagent.editor.version_store import VersionStore


# ── Fake LLM (deterministic, no network) ──────────────────────


class FakeLLM:
    """Returns structured edit plans based on keyword matching."""

    def __call__(self, content, *, system_message=None, history=None,
                 response_format=None, return_json=False, **kwargs):
        text = content[-500:] if len(content) > 500 else content  # last 500 chars

        # Style requests
        if "科技" in text or "配色" in text or "风格" in text:
            return {
                "type": "style",
                "style": {"name": "科技"},
                "message": "已应用科技风配色方案",
            }
        # Text edit requests
        for keyword, result in [
            ("修改标题为", "E2E测试标题"),
            ("把标题改成", "E2E修改标题"),
            ("改为", "E2E文本"),
            ("修改为", "E2E内容"),
            ("改成", "E2E新内容"),
        ]:
            if keyword in text:
                return {
                    "type": "content",
                    "actions": [f'replace_paragraph(1, 0, "{result}")'],
                    "message": f"已将标题改为 {result}",
                }
        # Generic fallback
        return {
            "type": "content",
            "actions": ['replace_paragraph(1, 0, "E2E默认修改")'],
            "message": "已执行编辑",
        }


# ── helpers ───────────────────────────────────────────────────


def make_presentation():
    config = Config(tempfile.mkdtemp())
    prs = Presentation.from_file("pptagent/templates/default/source.pptx", config)
    return prs, config


# ── test phases ───────────────────────────────────────────────


async def test_full_workflow():
    """User: generate → edit → preview → version → export"""
    prs, config = make_presentation()
    total = len(prs.slides)
    workspace = Path(tempfile.mkdtemp())
    task_id = "e2e-task-1"

    print(f"[1] 加载模板: {total} 页")

    # ── Phase 1: Version management ──
    vm = VersionManager()
    vm.commit(prs, "初始模板版本")
    assert len(vm) == 1
    print(f"    版本创建: {len(vm)} 个版本")

    # ── Phase 2: Snapshot + diff ──
    before_all = PresentationSnapshot.capture(prs, "before-any-edits")
    assert len(before_all.slides) == total
    print(f"    整体快照: {len(before_all.slides)} 页")

    # ── Phase 3: Programmatic edit (SlideEditor) ──
    editor = SlideEditor(prs.slides[1], prs)
    snap_before = SlideSnapshot.capture(prs.slides[1])
    editor.edit_text(div_id=1, paragraph_id=0, new_text="程序化编辑测试")
    snap_after = SlideSnapshot.capture(prs.slides[1])
    assert snap_before.checksum != snap_after.checksum
    diff = DiffEngine.diff_slides(snap_before, snap_after)
    assert diff.total_changes > 0
    print(f"    程序化编辑: {diff.total_changes} 处变更")
    editor.undo()
    assert SlideSnapshot.capture(prs.slides[1]).checksum == snap_before.checksum
    print(f"    undo 恢复: checksum 一致")

    # ── Phase 4: Conversational edit (SlideEditService) ──
    store = ArtifactStore(workspace, task_id)
    bus = EditEventBus()
    svc = SlideEditService(
        slide=prs.slides[1], presentation=prs,
        llm=FakeLLM(), task_id=task_id, slide_id="s2",
        store=store, event_bus=bus, max_revisions=5,
    )

    r1 = await svc.chat("把标题改成 E2E对话测试标题")
    assert r1.status == "success", r1
    assert r1.revision == 1
    print(f"    对话编辑: revision={r1.revision}, status={r1.status}")

    # Verify events were published
    events = bus.recent()
    assert any(e.type == "edit.started" for e in events)
    assert any(e.type == "edit.applied" for e in events)
    print(f"    事件总线: {len(events)} 个事件")

    # ── Phase 5: Style restyle ──
    registry = StyleRegistry()
    strategy = registry.get("科技")
    snap_pre_style = SlideSnapshot.capture(prs.slides[1])
    restyle_editor = SlideEditor(prs.slides[1], prs)
    restyle_editor.restyle(strategy)
    snap_post_style = SlideSnapshot.capture(prs.slides[1])
    sdiff = DiffEngine.diff_slides(snap_pre_style, snap_post_style)
    print(f"    风格重绘: {len(sdiff.style_changes)} 处样式变更")
    restyle_editor.undo()

    # ── Phase 6: Conversational style edit ──
    r2 = await svc.chat("换成科技风配色")
    assert r2.status == "success"
    print(f"    对话式风格: {r2.message}")

    # ── Phase 7: Version chain + log ──
    vm.commit(prs, "对话编辑 + 风格修改")
    assert len(vm) == 2
    log = vm.log()
    assert len(log) == 2
    assert log[0]["message"] == "对话编辑 + 风格修改"
    print(f"    版本链: {len(vm)} 版本已记录")

    # ── Phase 8: Persistence (save + reload) ──
    store2 = VersionStore(tempfile.mkdtemp())
    for vid in vm._versions:
        store2.save(vm._versions[vid])
    del vm
    vm2 = store2.load_manager()
    assert len(vm2) == 2
    print(f"    持久化: 保存{len(store2.list_versions())}版 → 重启 → 加载{len(vm2)}版")

    # ── Phase 9: Preview rendering ──
    renderer = PreviewRenderer()
    result = renderer.render(prs.slides[1], slide_idx=2, total_slides=total)
    assert len(result.html) > 500
    assert "第2页" in result.html
    print(f"    预览渲染: {len(result.html)} 字符 HTML")

    # Batch preview all slides
    saved = renderer.save_all_previews(prs, str(workspace / "previews"))
    assert len(saved) == total
    print(f"    批量预览: {len(saved)} 个 HTML 文件")

    # ── Phase 10: Feature extraction ──
    fstore = FeatureStore(tempfile.mkdtemp())
    after_all = PresentationSnapshot.capture(prs, "after-edits")
    slices = fstore.save_version(after_all, "e2e-v1")
    assert len(slices) == total
    text_slides = fstore.find_by_layout("text")
    print(f"    特征切片: {len(slices)} 页, {len(text_slides)} 纯文本页")

    # ── Phase 11: Export ──
    output = str(workspace / "e2e_output.pptx")
    result_exp = export_with_profile(prs, output, "presentation")
    assert result_exp.success
    assert os.path.exists(output)
    assert os.path.getsize(output) > 1000
    print(f"    导出: {result_exp.summary()}")

    # Try web-optimized export (with compression)
    web_output = str(workspace / "e2e_output_web.pptx")
    result_web = export_with_profile(prs, web_output, "web")
    assert result_web.success
    print(f"    网页导出: {result_web.file_size_bytes} bytes, {len(result_web.warnings)} warnings")

    # ── Phase 12: Cross-version diff ──
    changed = before_all.changed_slides(after_all)
    print(f"    跨版本对比: {len(changed)} 页发生变化 → 页码: {changed}")


async def test_custom_style_workflow():
    """User: create custom style → apply → save → reload → apply again"""
    prs, _ = make_presentation()
    registry = StyleRegistry()

    # Create
    custom = registry.create(
        name="我的品牌色",
        primary="1A5276",
        secondary="2C3E50",
        background="ECF0F1",
        accent="E74C3C",
        title_family="Microsoft YaHei",
        body_family="Microsoft YaHei",
        title_size=40,
        body_size=18,
        description="企业品牌规范配色",
    )
    assert len(registry) == 5  # 4 built-in + 1 custom
    assert len(registry.list_custom()) == 1

    # Apply
    editor = SlideEditor(prs.slides[2], prs)
    snap_before = SlideSnapshot.capture(prs.slides[2])
    editor.restyle(registry.get("我的品牌色"))
    snap_after = SlideSnapshot.capture(prs.slides[2])
    diff = DiffEngine.diff_slides(snap_before, snap_after)
    assert len(diff.style_changes) > 0

    # Save + reload
    path = "/tmp/e2e_custom_styles.json"
    registry.save_to_file(path)
    registry.reset()
    assert len(registry) == 4  # only built-in
    n = registry.load_from_file(path)
    assert n == 1
    assert "我的品牌色" in registry.list_names()

    # Apply again after reload
    editor.undo()
    editor.restyle(registry.get("我的品牌色"))
    snap2 = SlideSnapshot.capture(prs.slides[2])
    assert snap2.checksum == snap_after.checksum  # same result after reload
    print(f"    自定义风格: 创建→保存→重置→加载→应用, checksum一致")


async def test_version_branching():
    """User: create version branch → switch between branches"""
    prs, _ = make_presentation()
    vm = VersionManager()

    vm.commit(prs, "主线版本1")
    main_id = vm.current_version_id

    # Create experiment branch
    vm.branch("实验分支")
    vm.commit(prs, "实验版本A")
    exp_id = vm.current_version_id
    assert vm.current_branch == "实验分支"

    # Switch back to main
    vm.switch_branch("main")
    assert vm.current_version_id == main_id
    assert vm.current_branch == "main"

    # Go back to experiment
    vm.switch_branch("实验分支")
    assert vm.current_version_id == exp_id

    print(f"    版本分支: main={main_id}, experiment={exp_id}, 切换正常")


# ── main ──────────────────────────────────────────────────────


def main():
    print("\n" + "=" * 60)
    print("  PPTAgent Editor — End-to-End Test Suite")
    print("=" * 60 + "\n")

    tests = [
        ("完整工作流 (12步)", test_full_workflow),
        ("自定义风格流程", test_custom_style_workflow),
        ("版本分支管理", test_version_branching),
    ]

    passed = failed = 0
    for name, fn in tests:
        try:
            asyncio.run(fn())
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
    raise SystemExit(0 if main() else 1)
