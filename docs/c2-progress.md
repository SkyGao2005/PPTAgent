# C2 开发协作与进度记录

最后更新：2026-07-13

## 基本信息

- 项目：PPTAgent 二次开发
- 分支：`feat/realtime-progress-preview`
- 当前负责范围：C2，事件总线、Agent 钩子、逐页预览与 artifact URL
- 协作对象：C1，负责 FastAPI、TaskManager、任务生命周期、任务快照与导出协调
- 协作节奏：每天下午 16:00 后拉取 C1 推送的代码，在最新整合代码上合并当天 C2 修改，验证后 push，方便 C1 第二天继续开发

## 总目标

在现有 `AgentLoop` 外建立稳定的任务服务基础，把内部 Agent 消息、工具调用和页面产物转换成前端可以消费的结构化事件。C2 侧重点是事件协议落地、SSE 数据源、断线回放、生成链路钩子、逐页预览和 artifact 访问路径。

## C2 负责内容

1. 维护 `GenerationEvent` 事件模型和事件示例。
2. 维护 `EventBus`，包括事件发布、递增 `seq`、`events.jsonl` 持久化、历史回放、心跳和多订阅者。
3. 为 `AgentLoop` 增加可选 reporter 钩子，发布阶段开始、进度、完成、失败事件。
4. 为 `AgentEnv.tool_execute()` 增加可选 reporter 钩子，发布工具开始、完成、失败等运行详情事件。
5. 统一页级状态，尽量从真实生成链路中识别页面开始、完成、失败。
6. 实现 HTML 模式逐页预览：检测 `slide_*.html` 完成并通过检查后，渲染单页 PNG/JPG，保存到对应 `SlideArtifact` revision。
7. 支持模板模式逐页预览：在 `generate_slide()` 完成后形成单页 artifact 和预览。
8. 维护 artifact URL 规范，确保前端只拿相对路径或安全 API 路径，不直接暴露任意本地文件。
9. 为上述功能补充单元测试和必要的集成测试。

## C1/C2 接口边界

- C1 暴露 FastAPI 路由和 `TaskManager`，负责创建任务、查询快照、取消、重试、导出。
- C2 暴露事件与预览能力，供 C1 在任务执行过程中调用。
- C1 的 SSE 路由应消费 C2 的 `EventBus.subscribe(last_seq)`。
- C1 的 artifact 路由应复用 C2 的路径安全检查和 artifact 路径规范。
- D 组编辑事件和 A 组模板解析事件复用同一 `GenerationEvent` 基础模型，但业务服务由对应方向实现。

## 当前环境

- 本地仓库：`/Users/wstdmac/image_classification/PPTAgent`
- 虚拟环境：`.venv`
- Python：3.12.13
- 当前安装策略：先安装 C2 开发和测试所需最小依赖，不安装完整 PPTAgent 重依赖。
- 已安装核心依赖：`fastapi`、`uvicorn`、`httpx`、`pytest`、`pytest-asyncio`、`jsonlines`、`pydantic`、`jsonschema`、`aiofiles`、`python-multipart`
- 启用环境：

```bash
source .venv/bin/activate
```

- 当前可用测试：

```bash
.venv/bin/python -m pytest deeppresenter/server/tests -q
```

## 已完成

### 2026-07-13

- 使用本地代理完成 `git pull --ff-only`，同步到 `ae52d85`。
- 确认 C1/C2 Day 1 基建已合入：
  - `deeppresenter/server/models/events.py`
  - `deeppresenter/server/models/artifacts.py`
  - `deeppresenter/server/services/event_bus.py`
  - `deeppresenter/server/tests/test_models.py`
  - `deeppresenter/server/tests/test_event_bus.py`
  - `docs/api-contract.md`
  - `docs/events-example.jsonl`
- 创建 `.venv`，安装 C2 最小开发依赖。
- 跑通 server 基础测试：`39 passed in 1.02s`。
- 将 `.venv` 加入 `.gitignore`。
- 创建本文档，用于记录 C2 计划、协作规则和每日进度。
- 修复 artifact 路径安全检查，改用 `Path.relative_to()`，避免同名前缀相邻目录误判。
- 增强 `EventBus`：
  - 关闭后仍可从 `events.jsonl` 回放历史事件。
  - 重建 EventBus 时从历史最大 seq 继续编号。
  - 关闭后的新订阅者只回放历史并结束，不持续心跳。
- 新增 `EventReporter`，作为 AgentLoop/AgentEnv 和 EventBus 之间的薄适配层。
- 接入 `AgentLoop` 可选事件钩子：
  - `prepare`、`plan`、`research`、`generate` 阶段 started/completed/failed。
  - HTML 导出阶段 `export.started`、`export.completed`、`export.failed`。
  - 默认 `event_reporter=None`，保持 CLI 兼容。
- 接入 `AgentEnv.tool_execute()` 可选工具事件：
  - 工具开始、完成、失败通过 `stage.progress` + payload 发布为运行详情。
  - reporter 异常只记录 warning，不影响原有生成流程。
- 新增/更新 C2 单元测试：
  - 路径安全 sibling prefix 边界测试。
  - EventBus 关闭后回放、重启 seq 恢复、关闭后订阅测试。
  - EventReporter 任务、阶段、工具、页面和导出事件测试。
- 新增 HTML 逐页预览原型服务 `PreviewService`：
  - 支持解析 `slide_01.html` / `slide-01.html` 页码。
  - 基于 `task_id + slide_index` 生成稳定 `slide_id`。
  - 渲染单页 HTML 到 `slides/<slide_id>/revisions/<n>/preview.png`。
  - 写入 `revisions/<n>/slide.json` 和 `current.json`。
  - 提供 artifact API URL 生成工具。
  - 默认渲染器 lazy import Playwright；测试使用 fake renderer，避免强制安装重依赖。
- 新增 PreviewService 单元测试：
  - 页码解析、稳定 slide_id、artifact URL。
  - artifact/current/revision 持久化。
  - 显式 revision 更新 current。
  - 拒绝任务工作区外 HTML 文件。
- 将 HTML 预览接入点挂到 `AgentEnv.tool_execute()`：
  - `AgentLoop(..., preview_service=preview_service)` 会传递给 `AgentEnv`。
  - `inspect_slide` 成功后解析 `html_file`，调用 `PreviewService.render_html_slide()`。
  - 预览生成成功后发布 `slide.preview_ready` 和 `slide.completed`。
  - 未传 `preview_service` 时保持原有 CLI/Agent 行为。
- 当前 server 测试结果：`54 passed in 0.24s`。

## 已发现问题和待处理点

1. `TaskStatus` 使用 `succeeded`，但部分文档/示例仍可能出现 `completed`。需要在模型、API 文档、前端约定中统一。
2. 当前虚拟环境没有安装完整 PPTAgent 运行依赖。接入真实 `AgentLoop`、Playwright 预览、PPT/PDF 转换时，需要按需补装 `docker`、`fastmcp`、`openai`、`playwright`、`pdf2image`、`pypdf` 等依赖。
3. `AgentLoop` 的阶段事件已接入，但尚未做真实 LLM/MCP 集成验证；需要等 C1 TaskManager 骨架或补齐运行依赖后验证完整链路。
4. HTML 预览已接入 `inspect_slide` 成功路径，但尚未在本地 Playwright 环境中做真实截图验证。

## 下一步计划

1. 与 C1 TaskManager 对接：
   - C1 创建任务后实例化 `EventBus` 和 `EventReporter`。
   - C1 调用 `AgentLoop(..., event_reporter=reporter)`。
   - SSE 路由使用 `EventBus.subscribe(last_seq)`。
2. 将 HTML 预览与 C1 TaskManager 串起来：
   - C1 创建任务时构造 `PreviewService(workspace_base)`。
   - C1 调用 `AgentLoop(..., event_reporter=reporter, preview_service=preview_service)`。
   - 预览失败时当前只记录 warning；后续可增加标准 warning 事件。
3. 模板模式预览调研：
   - 确认 `pptagent/mcp_server.py::generate_slide()` 能否返回足够信息。
   - 需要时和 A/D 组协商 `SlidePage` 持久化结构。
4. 补真实链路验证：
   - 安装 AgentLoop 运行所需依赖。
   - 用 mock 或最小真实任务验证阶段事件顺序。
   - 确认工具事件 payload 不泄漏大文本。
5. 每次与 C1 合并后：
   - 运行 server 单测。
   - 更新本文档的“已完成”和“待处理点”。
   - 在提交信息中标明 C2 修改范围。
