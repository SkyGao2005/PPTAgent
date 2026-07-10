# A 组开发规划：模板上传与解析

分支：`feat/template-upload-parsing`

人员：2 人（A1、A2）

周期：两周，与任务服务、前端和局部编辑组并行开发

## 本组目标

把 PPTAgent 现有的离线模板解析能力改造成可由前端上传、可实时显示解析进度、可动态加载的服务。

本组完成后应跑通：

> 上传自定义 PPTX → 文件校验 → 页面渲染 → 布局聚类与 schema 抽取 → 生成模板 manifest 和缩略图 → 注册到模板库 → 无需重启即可用于 PPT 生成。

## 当前代码判断

- `pptagent/induct.py` 已实现页面分类、布局聚类和内容 schema 抽取，应以复用为主。
- `pptagent/scripts/template_induct.py` 已串起模板标准化、页面渲染、图片标注和 induction 文件生成，但目前是目录写死的离线脚本。
- `pptagent/mcp_server.py` 只在启动时扫描 `pptagent/templates/`，不能动态加入用户模板。
- `pptagent/pptgen.py::set_reference()` 会对传入的 `slide_induction` 执行 `pop()`；服务端缓存同一字典时，模板重复使用存在状态被破坏的风险。
- 新模板能力应通过统一服务接口提供，不让前端直接操作模板目录。

## P0 工作范围

1. 稳定支持 `.pptx` 上传。
2. 校验扩展名、ZIP/PPTX 可读性、文件大小、页数和加密/损坏情况。
3. 将离线解析流程重构为可调用的 `TemplateInductionService`。
4. 解析过程中通过回调发布结构化进度事件。
5. 输出模板源文件、页面图、缩略图、`image_stats.json`、`slide_induction.json` 和 `manifest.json`。
6. 实现 `TemplateRegistry`，支持模板列表、详情、删除和动态加载。
7. 新模板解析完成后无需重启 MCP/后端即可用于生成。
8. 通过文件 hash 去重并复用解析缓存。
9. 修复模板 induction 数据被破坏性修改的问题。

## P1 工作范围

- 系统存在 LibreOffice 时，将 `.ppt` 转为 `.pptx` 后进入同一解析链路。
- 从模板中补充主色、字体、比例、布局数量等展示信息。
- 提供解析失败后的重新解析接口。

本期不自研二进制 `.ppt` 解析器，不做模板在线商城和复杂权限。

## 建议代码结构

```text
deeppresenter/server/
  routes/templates.py
  services/template_service.py
  services/template_registry.py
  models/templates.py

pptagent/
  induct.py
  mcp_server.py
  scripts/template_induct.py
```

`template_induct.py` 最终只保留命令行包装，解析主逻辑必须可以被 API、测试和 CLI 共同调用。

## 模板目录约定

```text
workspace/templates/<template_id>/
  manifest.json
  original.pptx
  source.pptx
  template.pptx
  slide_induction.json
  image_stats.json
  slide_images/
  template_images/
  thumbnails/
```

`manifest.json` 至少包含：

```text
template_id
name
status
source_hash
aspect_ratio
slide_count
layout_count
thumbnail
created_at
error
```

## API 契约

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/api/templates` | 上传模板并创建解析任务 |
| `GET` | `/api/templates` | 获取可用及解析中的模板 |
| `GET` | `/api/templates/{template_id}` | 获取模板详情和解析状态 |
| `GET` | `/api/templates/{template_id}/events` | 订阅模板解析 SSE 事件 |
| `POST` | `/api/templates/{template_id}/retry` | 重试失败解析 |
| `DELETE` | `/api/templates/{template_id}` | 删除用户模板及缓存 |

接口的 Pydantic 模型需要与 C 组统一，模板事件使用全项目统一的 `GenerationEvent`。

## 解析进度阶段

进度按可确定步骤上报，不估算模型剩余时间：

| 阶段 | 建议进度 |
|---|---:|
| 文件校验 | 5% |
| `.ppt` 转换（如需要） | 10% |
| 规范化并读取页面结构 | 20% |
| 渲染原始页和空布局页 | 35% |
| 布局分类与聚类 | 55% |
| 页面元素和内容 schema 抽取 | 80% |
| 生成缩略图、manifest 并注册 | 100% |

主要事件：

- `template.parse_started`
- `template.parse_progress`
- `template.ready`
- `template.failed`

## 两人分工

### A1：上传、校验、manifest 与缓存

- 模板上传接口所需 service。
- 文件名清洗、路径穿越防护、大小和页数限制。
- 模板 hash、重复上传去重、失败临时文件清理。
- 缩略图和 `manifest.json` 生成。
- 与 B 组模板库页面联调。

### A2：解析服务、动态注册与生成衔接

- 重构 `template_induct.py`，抽出 `TemplateInductionService`。
- 复用并必要时扩展 `SlideInducter` 的进度回调。
- 实现 `TemplateRegistry` 和 MCP 动态模板读取。
- 修复 `slide_induction` 的破坏性读取问题。
- 与 D 组模板式单页重生成联调。

关键模块由两人互相 review，不允许只有一人了解完整解析链路。

## 两周安排

| 工作日 | A1 | A2 | 当天结果 |
|---|---|---|---|
| 第 1 天 | 跑真实模板、定义 manifest | 梳理 induction/MCP 链路 | 确定输入输出和错误码 |
| 第 2 天 | 上传与文件校验 | 拆分解析 service | API 可保存合法模板 |
| 第 3 天 | 安全命名、限制和错误模型 | 跑通 service 主流程 | 上传文件可完成一次真实解析 |
| 第 4 天 | 缩略图和 manifest | 动态 registry | 模板详情数据完整 |
| 第 5 天 | 模板列表接口 | MCP 热加载、状态修复 | 新模板无需重启即可选择 |
| 第 6 天 | hash 去重和缓存 | 解析进度回调 | 前端可看到真实解析阶段 |
| 第 7 天 | 异常模板测试 | 重复选择、连续任务测试 | 失败不污染模板库 |
| 第 8 天 | 与 B/C 组联调 | 与 C/D 组联调 | 上传模板可实际生成 PPT |
| 第 9 天 | 修边界问题、补测试 | 修生成兼容问题、补测试 | 主链路稳定 |
| 第 10 天 | 文档和部署协助 | 文档和演示协助 | 完成交付与验收 |

## 测试清单

- 正常 PPTX 上传、解析、注册、删除。
- 同一文件重复上传命中缓存。
- 同一模板连续用于两个生成任务。
- 损坏 PPTX、加密文件、空 PPT、超限文件。
- 解析中模型调用失败，状态变为 failed，临时结果不进入可用列表。
- 服务重启后可从 manifest 恢复模板列表。
- 模板目录名和原始文件名包含中文、空格或特殊字符。
- 多个模板同时解析时工作区不串线。

模型、LibreOffice 和页面渲染相关测试标记为 integration；注册、缓存、校验和状态转换必须有不调用模型的单元测试。

## 完成标准

- 上传一个未内置的 PPTX，能看到解析进度、缩略图和模板信息。
- 解析完成的模板能被生成链路真正选中并生成至少 5 页。
- 新模板无需重启服务即可使用。
- 连续使用同一模板不会因共享字典被修改而失败。
- 所有失败状态都有清晰错误信息，不留下“永久解析中”的模板。
- A 组负责的测试、接口说明和启动说明齐全。

## 与其他组的交付边界

- 给 B 组：模板列表、缩略图、解析状态和 SSE 事件。
- 给 C 组：可调用的解析 service、标准事件回调和模板任务状态。
- 给 D 组：稳定的模板 ID、布局名、content schema 和只读 induction 数据。
- A 组不负责生成任务总进度 UI，也不负责单页对话模型。
