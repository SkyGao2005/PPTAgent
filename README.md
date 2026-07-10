# D 组开发规划：对话式局部编辑与版本管理

分支：`feat/slide-chat-editing`

人员：2 人（D1、D2）

周期：两周，负责“选中具体页面，边聊边改”的核心能力

## 本组目标

让用户在 PPT 生成过程中选中已经完成的页面，通过自然语言连续修改，并且只重做目标页，不重新运行整套 Research 和全部页面生成。

本组完成后应跑通：

> 第 N 页生成完成 → 用户选中该页聊天 → 只重生成第 N 页 → 形成新预览和 revision → 继续追问“再短一点” → 撤销上一版 → 最终导出使用各页最新成功版本。

生成和编辑需要并行：后续页面仍在生成时，已经完成的页面可以开始修改。

## 当前代码判断

- `deeppresenter/agents/planner.py` 已支持生成前对大纲进行对话反馈，可复用其多轮上下文思路，但目前没有生成后页级对话。
- HTML 模式会生成独立 `slide_*.html`，天然适合把单页文件作为可编辑源。
- 模板模式的 `pptagent/mcp_server.py` 能按 layout 和 structured elements 创建单页，但页面主要保存在服务内存。
- `save_generated_slides()` 完成后会清空 slides 和初始化状态，不适合围绕已保存页面继续多轮修改。
- 当前页面没有稳定 `slide_id`，也没有统一保存布局、结构化内容、HTML、预览和版本的模型。
- 最终 PPT 导出尚未定义“每页选择哪个 revision”。

## P0 工作范围

1. 定义并持久化统一 `SlideArtifact`。
2. 每页拥有稳定 `slide_id`，不使用数组下标充当身份。
3. 模板模式保存 layout、structured elements 和必要生成信息。
4. HTML 模式保存单页 HTML 和相关资源路径。
5. 提供针对当前页的多轮自然语言修改。
6. 内容、布局、风格和整页重生成四类指令可执行。
7. 单页修改形成新 revision 和新预览。
8. 修改失败不覆盖上一成功版本。
9. 支持版本列表、应用指定版本和撤销。
10. 同一页修改串行，不同页面可并行。
11. 未完成页面收到指令时先排队，页面完成后自动执行。
12. 最终导出使用每页最新成功/current revision。

## P1 工作范围

- 模板模式支持指定 `element_id`，定向修改标题、正文或图片元素。
- 修改前后结构化内容 diff。
- 版本并排预览。
- 对失败修改提供带原指令的一键重试。

本期不做任意框选像素区域、自由拖拽文本框、多用户同时编辑同一页，以及所有动画/SmartArt/宏的无损编辑。

## SlideArtifact 模型

```text
slide_id
task_id
index
status
mode                html / template
template_id
layout_name
structured_data
source_path
preview_path
revision
created_at
updated_at
```

页面顺序由 `index` 决定，身份由 `slide_id` 决定。即使插页、删页或重新排序，页面聊天和 revision 也不能串到其他页。

## 版本目录

```text
workspace/<task_id>/slides/<slide_id>/
  current.json
  conversation.jsonl
  pending_edits.jsonl
  revisions/
    1/
      slide.json
      source.html
      preview.png
    2/
      slide.json
      source.html
      preview.png
```

模板模式可用模板页结构化文件替代 `source.html`。路径和格式细节与 C 组的 artifact store 对齐。

## 版本规则

- 初次生成成功为 revision 1。
- 每次成功修改创建新 revision，并原子更新 `current.json`。
- 修改过程先写临时目录；预览和校验成功后再提升为 current。
- 失败草稿不覆盖当前版本。
- 撤销只切换 current 指针，不重新调用模型。
- 默认保留最近 10 个完整 revision；超出后删除最旧的大文件预览，结构化审计信息可以保留。
- 导出记录每一页使用的 revision，保证结果可复现。

## 对话上下文

每次单页修改只提供必要上下文：

- 当前页截图。
- 当前页 `structured_data` 或 HTML。
- 模板 ID、布局名和元素 schema（模板模式）。
- 整套 PPT 标题和大纲，用于保持全局一致。
- 当前页最近若干轮对话。
- 用户选中的 `element_id` 和当前内容（如果有）。

不要每次传入完整任务历史、所有 Agent 工具日志和整套 PPT 页面，避免成本增加、上下文污染和修改范围漂移。

## 指令分类

`SlideEditService` 可先用轻量规则/模型判断意图：

```text
content
  精简、扩写、改标题、改成三点、调整措辞

layout
  换成左右布局、突出结论、图文重新排列

style
  换配色、加强对比、商务一些、统一字体

regenerate
  重新生成本页、换一个方案
```

分类错误时允许用户显式选择修改类型。所有请求必须携带 `task_id` 和 `slide_id`。

## 模板模式处理

1. 初次生成后保存 `template_id`、`layout_name`、`EditorOutput`/structured elements 和必要命令。
2. 内容修改优先保持 layout，只更新 structured elements。
3. 布局修改先重新选择 layout，再按新 schema 重写内容。
4. 调用模板生成能力只重建目标 `SlidePage`。
5. 持久化新页面、生成预览、校验成功后创建 revision。
6. 最终合并按稳定 `slide_id` 和当前 `index` 重建顺序。

需要和 A 组统一模板 registry、只读 induction 数据和 layout/schema 读取方式，不能继续依赖一次性全局 MCP 内存状态。

## HTML 模式处理

1. 初次生成后保存目标页 HTML 和资源引用。
2. 修改时只给 Design Agent 当前页 HTML、截图和用户指令。
3. 输出到临时 revision 目录，不直接覆盖 current HTML。
4. 使用现有 HTML/PPTX 校验和 Playwright 预览能力检查目标页。
5. 成功后发布新 revision 和 `edit.preview_ready`。
6. 最终导出时按页面顺序转换整套 current HTML。

修改时必须限制 Agent 只能写目标页临时目录，防止一条单页指令改到其他页面。

## 并发规则

- 每个 `slide_id` 对应一个 `asyncio.Lock`。
- 同一页的修改请求按提交顺序串行执行。
- 不同页面的修改可以并行，但需遵守全局模型并发限制。
- 主生成写入初始 revision 时也要获取页锁。
- 页面未完成时，将用户指令写入 `pending_edits.jsonl`。
- 初始 revision 创建后按顺序执行待处理指令。
- 导出读取 current revision 的一致性快照；导出过程中产生的新 revision 进入下一次导出。

## 编辑事件

D 组通过 C 组 EventBus 发布：

- `edit.queued`
- `edit.started`
- `edit.preview_ready`
- `edit.applied`
- `edit.failed`
- `edit.reverted`

每个事件应包含 `task_id`、`slide_id`、目标/结果 revision、简短 message 和 artifact URL（如有）。

## API 契约

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/api/tasks/{task_id}/slides/{slide_id}/chat` | 发送修改指令，可带 `element_id` |
| `GET` | `/api/tasks/{task_id}/slides/{slide_id}` | 获取当前页和 current revision |
| `GET` | `/api/tasks/{task_id}/slides/{slide_id}/revisions` | 获取版本列表 |
| `POST` | `/api/tasks/{task_id}/slides/{slide_id}/revisions/{revision}/apply` | 应用指定版本 |
| `POST` | `/api/tasks/{task_id}/slides/{slide_id}/undo` | 撤销到上一成功版本 |
| `POST` | `/api/tasks/{task_id}/slides/{slide_id}/retry` | 重试失败修改 |

请求示意：

```json
{
  "instruction": "把标题缩短，并把正文整理成三点",
  "element_id": null,
  "base_revision": 2
}
```

`base_revision` 用于检测用户是否在旧版本上发起修改。发现冲突时返回当前版本或按明确规则排队，不能静默覆盖更新版本。

## 两人分工

### D1：模板模式、结构化页面和 revision

- `SlideArtifact`/revision 数据模型。
- 模板模式布局和 structured elements 持久化。
- 模板单页重生成。
- revision 创建、应用、撤销和 current 指针。
- 最终 PPT 合并时的版本选择。
- 与 A 组模板解析和 registry 联调。

### D2：HTML 模式、对话上下文和编辑队列

- HTML 单页修改和写入范围限制。
- 页级对话历史和上下文裁剪。
- 指令分类和 `SlideEditService`。
- 页级锁、待执行指令和不同页并行。
- 与 B 组聊天/快捷指令联调。

两人共同维护统一 SlideArtifact，禁止 HTML 和模板模式各自定义互不兼容的版本模型。

## 两周安排

| 工作日 | D1 | D2 | 当天结果 |
|---|---|---|---|
| 第 1 天 | Artifact/revision 草案 | 对话/并发草案 | 与 B/C 冻结模型和 API |
| 第 2 天 | revision 存储、current 指针 | 页级锁、对话存储 | 可对 mock 页面创建/撤销版本 |
| 第 3 天 | 保存模板页结构化数据 | 保存 HTML 页和资源 | 两种模式形成初始 revision |
| 第 4 天 | 模板单页重生成原型 | HTML 单页修改原型 | 两种模式至少一种真实修改成功 |
| 第 5 天 | 模板修改校验和预览 | HTML 修改校验和预览 | 周中跑通完整页级修改 |
| 第 6 天 | revision 应用和撤销 | 多轮上下文和指令分类 | 连续对话修改可用 |
| 第 7 天 | 最终合并版本选择 | 待执行队列和同页串行 | 生成中可编辑已完成页 |
| 第 8 天 | 失败回滚、冲突处理 | 编辑重试、事件完善 | P0 功能冻结 |
| 第 9 天 | 模板模式回归 | HTML 模式回归 | 连改三次、撤销、导出稳定 |
| 第 10 天 | 文档和演示修复 | 文档和演示修复 | 完成交付 |

## 测试清单

- 初始页面正确形成 revision 1。
- 内容修改只改变目标页。
- 布局修改能更新 layout/schema 和预览。
- HTML 修改不能写到其他页面目录。
- 同一页快速提交两条指令时按顺序执行。
- 不同页面修改可并行且对话不串页。
- 未完成页面的修改进入队列，初始生成完成后执行。
- 修改失败不改变 current revision。
- 连续修改三次后版本顺序正确。
- 撤销不调用模型，页面和最终导出同时回到上一版。
- 基于旧 `base_revision` 的请求不会静默覆盖新版本。
- 导出时页数、顺序、模板和 current revision 正确。
- 服务重启后可读取页面版本和对话历史。

模型生成测试标记为 integration；revision 指针、锁、队列、失败回滚和导出选择必须有可重复单元测试。

## 完成标准

- 修改目标页不会重新运行整套研究和所有页面生成。
- 后续页面仍生成时，已完成页面可开始修改。
- 连续指令“缩短内容”→“再短一点”能基于上一版继续。
- 修改失败保留上一有效版本。
- 撤销无需模型调用，并正确影响最终导出。
- 两种生成模式使用同一页面身份和 revision 规则。
- 导出的 PPT 使用每页 current revision，顺序和页数正确。

## 与其他组的交付边界

- 从 A 组接收：模板 ID、layout、content schema 和只读 induction 数据。
- 从 C 组接收：ArtifactStore、EventBus、预览和导出协调能力。
- 向 B 组提供：页级聊天、编辑状态、revision、应用和撤销 API。
- D 组不负责总任务状态机、模板上传解析和前端视觉实现。
