"""演示脚本：测试局部编辑 + 版本管理 + 持久化的完整流程"""
import tempfile
import os
from pptagent.presentation import Presentation
from pptagent.utils import Config
from pptagent.editor.snapshot import SlideSnapshot, PresentationSnapshot
from pptagent.editor.editor import SlideEditor
from pptagent.editor.diff import DiffEngine
from pptagent.editor.version import VersionManager
from pptagent.editor.version_store import VersionStore

# ── 1. 加载模板 ──
config = Config(tempfile.mkdtemp())
prs = Presentation.from_file("pptagent/templates/default/source.pptx", config)
print(f"【1】加载 default 模板：共 {len(prs.slides)} 页")

# ── 2. 创建版本管理器 + 初始版本 ──
vm = VersionManager()
vm.commit(prs, "初始版本：加载 default 模板")
print(f"【2】初始版本已保存：{vm.current_version_id}")

# ── 3. 编辑第2页 ──
slide = prs.slides[1]
editor = SlideEditor(slide, prs)

print(f"\n【3】编辑前快照：{SlideSnapshot.capture(slide).checksum}")
editor.edit_text(div_id=1, paragraph_id=0, new_text="这是修改后的标题")
print(f"   编辑后快照：{SlideSnapshot.capture(slide).checksum}")

# ── 4. undo ──
snap_after_undo = editor.undo()
print(f"\n【4】undo 后快照：{snap_after_undo.checksum}")

# ── 5. redo ──
snap_after_redo = editor.redo()
print(f"\n【5】redo 后快照：{snap_after_redo.checksum}")

# ── 6. 提交修改 ──
vm.commit(prs, "第2版：修改第2页标题")
print(f"\n【6】新版本：{vm.current_version_id}")

# ── 7. 精确 diff ──
versions = [e["version_id"] for e in vm.log()]
n1 = vm._versions[versions[-1]]  # v1
n2 = vm._versions[versions[0]]   # v2
diff = DiffEngine.diff_slides(n1.snapshot.slides[1], n2.snapshot.slides[1])
print(f"\n【7】精确 Diff：")
print(diff.detail())

# ── 8. 持久化到磁盘 ──
store = VersionStore(tempfile.mkdtemp())
for vid in vm._versions:
    store.save(vm._versions[vid])
del vm

vm2 = store.load_manager()
print(f"\n【8】从磁盘恢复：{len(vm2)} 个版本")
for v in store.list_versions():
    path = store._snapshots_dir / f'{v["version_id"]}.json'
    print(f"   {v['version_id']}: {v['message']} ({os.path.getsize(path)} bytes)")

print("\n🎉 全部测试完成！")
