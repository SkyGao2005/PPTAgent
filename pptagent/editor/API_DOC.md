# PPTAgent Editor — API 接口文档

> 给前端组在 `webui.py` 中调用的Python方法清单。

---

## 1. 单页编辑器 `SlideEditor`

```python
from pptagent.editor.editor import SlideEditor
```

| 方法 | 参数 | 返回 | 说明 |
|------|------|------|------|
| `editor.edit_text(div_id, paragraph_id, new_text)` | div_id: int, paragraph_id: int, new_text: str | SlideSnapshot | 修改某段文字 |
| `editor.clone_paragraph(div_id, paragraph_id)` | div_id: int, paragraph_id: int | SlideSnapshot | 克隆（复制）一段文字 |
| `editor.delete_paragraph(div_id, paragraph_id)` | div_id: int, paragraph_id: int | SlideSnapshot | 删除一段文字 |
| `editor.edit_image(img_id, new_image_path)` | img_id: int, new_image_path: str | SlideSnapshot | 替换图片 |
| `editor.delete_image(img_id)` | img_id: int | SlideSnapshot | 删除图片 |
| `editor.undo()` | — | SlideSnapshot 或 None | 撤销上一步 |
| `editor.redo()` | — | SlideSnapshot 或 None | 重做 |
| `editor.restyle(strategy)` | StyleStrategy | SlideSnapshot | 应用风格重绘 |
| `editor.can_undo` / `editor.can_redo` | — | bool | 是否有可撤销/重做的操作 |
| `editor.set_preview(renderer)` | PreviewRenderer | — | 绑定预览渲染器，编辑后自动刷新 |
| `editor.preview()` | — | PreviewResult 或 None | 生成当前页的HTML预览 |
| `editor.save_preview(output_dir)` | output_dir: str | PreviewResult 或 None | 保存预览HTML文件 |

**使用示例**：
```python
editor = SlideEditor(prs.slides[2], prs)
editor.edit_text(div_id=1, paragraph_id=0, new_text="新标题")
if editor.can_undo:
    editor.undo()
```

---

## 2. 版本管理 `VersionManager`

```python
from pptagent.editor.version import VersionManager
```

| 方法 | 参数 | 返回 | 说明 |
|------|------|------|------|
| `vm.commit(prs, message, tags=[])` | prs: Presentation, message: str, tags: list | VersionNode | 保存当前PPT为新版本 |
| `vm.log()` | — | list[dict] | 版本历史（最新优先），每项含 version_id, message, created_at |
| `vm.checkout(version_id, prs)` | version_id: str, prs: Presentation | — | 回退到指定版本 |
| `vm.diff(v1_id, v2_id)` | v1_id: str, v2_id: str | dict | 对比两个版本：{same_content, changed_slides} |
| `vm.branch(name)` | name: str | str | 创建分支，返回当前版本ID |
| `vm.switch_branch(name)` | name: str | str | 切换到其他分支 |
| `vm.current_version_id` | — | str 或 None | 当前版本ID |

**使用示例**：
```python
vm = VersionManager()
vm.commit(prs, "第1版", tags=["auto"])
vm.commit(prs, "修改标题", tags=["edit"])

# 查看历史
for entry in vm.log():
    print(f"{entry['version_id']}: {entry['message']}")

# 回退
vm.checkout(vm.log()[-1]['version_id'], prs)
```

---

## 3. 磁盘持久化 `VersionStore`

```python
from pptagent.editor.version_store import VersionStore
```

| 方法 | 参数 | 返回 | 说明 |
|------|------|------|------|
| `store.save(node)` | VersionNode | — | 保存一个版本到磁盘 |
| `store.get(version_id)` | str | VersionNode 或 None | 从磁盘加载一个版本 |
| `store.list_versions()` | — | list[dict] | 列出所有版本元数据（不加载快照，快） |
| `store.load_manager()` | — | VersionManager | 重建完整的VersionManager |
| `store.delete(version_id)` | str | bool | 删除一个版本 |
| `store.clear()` | — | — | 清空所有版本 |

---

## 4. 差异引擎 `DiffEngine`

```python
from pptagent.editor.diff import DiffEngine
```

| 方法 | 参数 | 返回 | 说明 |
|------|------|------|------|
| `DiffEngine.diff_slides(before, after)` | SlideSnapshot, SlideSnapshot | SlideDiff | 单页精确差异 |
| `DiffEngine.diff_presentations(before, after)` | PresentationSnapshot, PresentationSnapshot | list[SlideDiff] | 多页差异 |

**SlideDiff 的属性**：
- `text_changes: list[TextChange]` — 文字修改
- `image_changes: list[ImageChange]` — 图片变更
- `style_changes: list[StyleChange]` — 字体/颜色变更
- `count_changes: list[ParagraphCountChange]` — 段落增减
- `detail()` → str — 多行详细报告
- `summary()` → str — 一行摘要
- `total_changes: int` — 总变更数

---

## 5. 风格策略 `StyleRegistry`

```python
from pptagent.editor.styles import StyleRegistry
```

| 方法 | 参数 | 返回 | 说明 |
|------|------|------|------|
| `registry.list_names()` | — | list[str] | 所有风格名称 |
| `registry.get(name)` | name: str | StyleStrategy | 获取指定风格 |
| `registry.create(name, primary, secondary, ...)` | 颜色(hex无#) + 字体 | StyleStrategy | **创建自定义风格** |
| `registry.delete(name)` | name: str | bool | 删除自定义风格（内置不可删） |
| `registry.is_builtin(name)` | name: str | bool | 是否为内置风格 |
| `registry.list_custom()` | — | list[StyleStrategy] | 列出所有自定义风格 |
| `registry.save_to_file(path)` | path: str | — | 保存到JSON文件 |
| `registry.load_from_file(path)` | path: str | int | 从JSON文件加载（返回加载数） |
| `registry.reset()` | — | — | 恢复为内置默认 |
| `len(registry)` | — | int | 风格总数 |

**创建自定义风格参数表**：

| 参数 | 类型 | 说明 | 示例 |
|------|------|------|------|
| `name` | str | 唯一风格名 | `"橙意"` |
| `primary` | str | 主色 hex (无#) | `"FF5722"` |
| `secondary` | str | 辅色 hex | `"333333"` |
| `background` | str | 背景色 hex | `"FAFAFA"` |
| `accent` | str | 点缀色 hex | `"FF9800"` |
| `title_family` | str | 标题字体 | `"Arial"` |
| `body_family` | str | 正文字体 | `"Microsoft YaHei"` |
| `title_size` | int | 标题字号(pt) | `42` |
| `body_size` | int | 正文字号(pt) | `18` |
| `title_color` | str | 标题专用色(可选) | `"FF5722"` |
| `body_color` | str | 正文专用色(可选) | `"333333"` |
| `bold_titles` | bool | 标题加粗 | `True` |
| `description` | str | 可读描述 | `"暖橙色商务风格"` |

---

## 6. 批量编辑 `BatchEditor`

```python
from pptagent.editor.batch import BatchEditor
```

| 方法 | 参数 | 返回 | 说明 |
|------|------|------|------|
| `batch.edit_text(slide_idx, div_id, paragraph_id, new_text)` | 同上+页码 | — | 对指定页添加编辑任务 |
| `batch.commit(message)` | message: str | int | 提交所有编辑，返回修改页数 |
| `batch.active_slides` | — | list[int] | 被编辑的页码列表 |
| `batch.total_pending_edits` | — | int | 待处理编辑数 |

---

## 7. 特征切片 `FeatureStore`

```python
from pptagent.editor.features import FeatureStore
```

| 方法 | 参数 | 返回 | 说明 |
|------|------|------|------|
| `store.save_version(snapshot, version_id)` | PresentationSnapshot, str | list[FeatureSlice] | 提取并保存所有页特征 |
| `store.find_by_layout(type)` | "text"\|"image"\|"mixed" | list[dict] | 跨版本查特定布局 |
| `store.find_by_element(name)` | element_name: str | list[dict] | 跨版本查包含特定元素的页 |
| `store.get_version_slices(version_id)` | str | list[FeatureSlice] | 取某个版本的所有特征切片 |

---

## 8. 预览渲染 `PreviewRenderer`

```python
from pptagent.editor.preview import PreviewRenderer
```

| 方法 | 参数 | 返回 | 说明 |
|------|------|------|------|
| `renderer.render(slide, slide_idx, total_slides)` | SlidePage, int, int | PreviewResult | 渲染单页为HTML |
| `renderer.render_all(prs)` | Presentation | list[PreviewResult] | 渲染全部页面 |
| `renderer.save_preview(slide, path, ...)` | 同上+输出路径 | PreviewResult | 渲染并保存为HTML文件 |
| `renderer.save_all_previews(prs, output_dir)` | Presentation, str | list[PreviewResult] | 渲染并保存全部页面 |

**PreviewResult 属性**：`html: str`, `checksum: str`, `slide_idx: int`, `shape_count: int`, `layout_name: str`

> **HTML预览可直接在浏览器/iframe中展示，已嵌入base64图片，无需文件系统依赖。**

---

## 9. 导出优化 `exporter`

```python
from pptagent.editor.exporter import optimized_save, ExportOptions, ExportProfile, export_with_profile
```

**基础导出**：
```python
result = optimized_save(prs, "output.pptx")
print(result.summary())  # "导出成功 | 输出: output.pptx | 13页 | 1.4 MB | 耗时 0.0s"
```

**自定义选项**：
```python
opts = ExportOptions(
    max_image_width=1280,      # 图片最大宽度
    max_image_height=720,      # 图片最大高度
    validate_images_exist=True, # 检查图片是否存在
    validate_text_bounds=True,  # 检查文本溢出
    progress_callback=my_callback,  # (current, total, title) 进度回调
)
result = optimized_save(prs, "output.pptx", opts)
```

**预设计划**（一键切换）:
```python
result = export_with_profile(prs, "output.pptx", "web")  # 'presentation' | 'print' | 'web' | 'draft'
```

**ExportResult 属性**：`success: bool`, `file_size_bytes: int`, `warnings: list[ExportWarning]`, `summary() → str`, `warning_details() → str`

---

## 10. 集成入口 `VersionedPPTAgent`

```python
from pptagent.editor.integration import VersionedPPTAgent
```

一键包装 `PPTAgent`，自动拥有版本管理 + 编辑器能力：

```python
agent = VersionedPPTAgent(language_model=..., vision_model=..., workspace="...")
agent.set_reference(...)
prs, history = await agent.generate_pres(...)  # 自动建版本

# 后编辑
editor = agent.get_editor(2)  # 获取第2页编辑器
editor.edit_text(1, 0, "新标题")

# 版本管理
agent.commit("手动编辑")
agent.enable_persistence()
agent.save_versions()
agent.load_versions()
```
