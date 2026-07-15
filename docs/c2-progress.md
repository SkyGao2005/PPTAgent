# C2 开发协作与进度记录

最后更新：2026-07-15

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
- 当前安装策略：已安装完整 PPTAgent 运行依赖；Docker sandbox 镜像下载不稳定时，可使用去掉 `sandbox` MCP 的配置继续无 Docker 链路验证。
- 已安装核心依赖：`fastapi`、`uvicorn`、`httpx`、`pytest`、`pytest-asyncio`、`jsonlines`、`pydantic`、`jsonschema`、`aiofiles`、`python-multipart`、`playwright`、`docker`、`fastmcp`、`mcp`、`openai`、`json-repair`、`pdf2image`、`pypdf`、`python-pptx`
- Playwright 浏览器缓存：`.local-playwright`（已加入 `.gitignore`）
- 启用环境：

```bash
source .venv/bin/activate
```

- 当前可用测试：

```bash
.venv/bin/python -m pytest deeppresenter/server/tests -q
```

- 真实 HTML 预览冒烟测试：

```bash
DEEPPRESENTER_RUN_REAL_PREVIEW_TEST=1 \
PLAYWRIGHT_BROWSERS_PATH=/Users/wstdmac/image_classification/PPTAgent/.local-playwright \
.venv/bin/python -m pytest deeppresenter/server/tests/test_preview.py::test_render_html_preview_real_playwright_opt_in -q
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

### 2026-07-14

- 拉取 C1 最新代码，同步到 `c4f5e70`：
  - 新增 `deeppresenter/server/app.py`
  - 新增 `deeppresenter/server/routes/tasks.py`
  - 新增 `deeppresenter/server/services/task_manager.py`
  - 新增 `deeppresenter/server/tests/test_task_manager.py`
  - 新增 `docs/devlog/0714.md`
- 确认 C1 当前实现：
  - FastAPI app 和 `/api/tasks` 基础路由已落地。
  - TaskManager 已支持创建、查询、取消、任务快照、EventBus 和占位执行器。
  - 真实 AgentLoop 尚未替换占位执行器。
- 将 TaskManager 与 C2 服务打通：
  - 任务创建时实例化 `EventReporter(task_id, bus.publish)`。
  - 任务创建时实例化 `PreviewService(workspace_base)`。
  - 新增 `get_event_reporter(task_id)`。
  - 新增 `get_preview_service(task_id)`。
  - 占位执行器改为通过 `EventReporter` 发布任务/阶段事件，减少手写 `GenerationEvent`。
- 补充 TaskManager 测试，确认任务创建后可以获取 C2 的 `EventReporter` 和 `PreviewService`。
- 将 TaskManager 的真实执行路径接入 `AgentLoop`：
  - `TaskManager(..., use_placeholder=False)` 会构造 `InputRequest` 并运行真实 `AgentLoop`。
  - 真实执行器注入 `EventReporter` 和 `PreviewService`，复用此前 C2 接好的阶段、工具、预览事件钩子。
  - 将最终产物路径转换为任务工作区相对路径并写入 `TaskSnapshot.result_artifact`。
  - FastAPI `create_app()` 默认使用真实执行器，可通过 `DEEPPRESENTER_SERVER_PLACEHOLDER=1` 切回占位链路。
  - `/api/tasks` 创建任务已透传 `powerpoint_type`、`template`、`convert_type`。
- 补充真实执行器参数传递测试，确认创建任务时的生成参数完整进入执行链。
- 使用占位模式完成 FastAPI 冒烟测试：
  - `GET /health` 返回 `{"status":"ok"}`。
  - `POST /api/tasks` 可创建任务并返回 `task_id`。
  - `GET /api/tasks/{task_id}/events?last_seq=0` 可回放 `task.created`、阶段事件和 `task.completed`。
  - `GET /api/tasks/{task_id}` 返回 `succeeded`、`progress=100` 和 `result_artifact`。
- 完善逐页预览读取能力：
  - `PreviewService.render_html_slide()` 每次写入 `current.json` 后同步维护 `slides/index.json`。
  - 新增 `PreviewService.list_slides()`、`get_slide()`、`revision_count()`。
  - 新增 `GET /api/tasks/{task_id}/slides`，返回当前页列表、`preview_url` 和 `current_revision`。
  - 新增 `GET /api/tasks/{task_id}/slides/{slide_id}`，返回单页详情、结构化数据和 revision 数量。
  - `GET /api/tasks/{task_id}` 已内联当前 slides 摘要，和 API 契约示例保持一致。
  - 补充预览服务与 slides 路由测试。
- 推进第 1 天 HTML 预览做实：
  - 安装 `playwright` Python 包，浏览器下载到 `.local-playwright`。
  - 新增真实 Playwright opt-in 冒烟测试，验证 HTML 可截图为 PNG。
  - 真实预览测试结果：`1 passed in 1.36s`。
  - `AgentEnv` 预览失败路径改为发布标准 `slide.failed` 事件，并在 payload 中记录 `error`、`html_file`、`aspect_ratio`、`source_preserved`。
  - `EventReporter.slide_failed()` 支持轻量 payload。
  - 补充 `slide.failed` payload 单元测试。
- 本仓库本地 Git 提交名已设置为 `bhqmz111`。
- 当前 server 测试结果：`76 passed, 1 skipped in 1.35s`。

### 2026-07-15

- 拉取 C1 最新代码，同步到 `dcd25b7`：
  - C1 新增模板模式结构化页衔接：`PreviewService.render_template_slide()` 和 `AgentEnv._maybe_capture_template_slide()`。
  - C1 新增 retry/export API：`POST /api/tasks/{task_id}/retry`、`POST /api/tasks/{task_id}/export`。
  - C1 新增任务恢复：`TaskManager.restore_snapshots()` 和 FastAPI startup 恢复钩子。
  - C1 新增取消检查点和 `completed_slide_ids` 快照字段。
- 验证拉取后的 server 测试：`86 passed, 1 skipped in 1.91s`。
- 完善模板模式逐页预览：
  - `PreviewService.render_template_slide()` 不再只持久化结构化数据，而是生成 `template_preview.html` 并复用 HTML renderer 输出 `preview.png`。
  - 模板模式 `SlideArtifact.preview_path` 已指向 `slides/<slide_id>/revisions/<n>/preview.png`。
  - 新增 `build_template_preview_html()`，把 `layout_name`、标题、副标题、正文、图片引用和 extras 转为可截图 HTML，并做 HTML escape。
  - `AgentEnv._maybe_capture_template_slide()` 传递 `aspect_ratio` 给模板预览渲染。
  - 补充模板预览单测：结构化数据可生成 `source.json`、`template_preview.html`、`preview.png`；字符串 body 可归一化为列表。
  - 补充模板预览路由测试：`GET /slides` 可返回模板页 `preview_url`，artifact API 可读取模板页 PNG。
- 真实 HTML 预览 opt-in 测试结果：`1 passed in 1.19s`。
- 当前 server 测试结果：`90 passed, 1 skipped in 1.87s`。
- 使用阿里云 PyPI 镜像完成完整运行依赖安装：
  - `pip check` 结果：`No broken requirements found`。
  - 关键依赖导入通过：`docker`、`fastmcp`、`mcp`、`openai`、`json_repair`、`yaml`、`aiohttp`、`pdf2image`、`pptx`、`PIL`、`fasttext` 等。
- 尝试拉取 `deeppresenter-sandbox` Docker 镜像：
  - Docker Desktop daemon 可用。
  - `docker.1ms.run/forceless/deeppresenter-sandbox` 多次下载到后段后因 `unexpected EOF` 中断。
  - Docker Hub 原源在匿名 token 请求阶段出现 EOF。
- 为无 Docker 开发/验证路径做解耦：
  - `AgentEnv.__aenter__()` 只在 MCP 配置里包含 `docker` 命令或 `sandbox` server 时访问 Docker。
  - 去掉 `sandbox` 的 `mcp.json` 可绕开 Docker daemon 和 sandbox 镜像，用于源码安装、非 sandbox MCP、模板模式/C2 事件预览验证。
  - 新增 `test_agent_env.py` 覆盖无 sandbox 不访问 Docker、sandbox 配置需要 Docker。
- 完成本地 `deeppresenter-sandbox` 镜像构建：
  - Dockerfile 改用阿里云 Debian apt 镜像源，避免 `deb.debian.org` EOF。
  - 移除构建时易卡住的 `fonts-noto-cjk` / `fonts-noto-cjk-extra`，保留 `fonts-wqy`、`fonts-arphic`、`fonts-ipafont` 等中文字体。
  - 跳过 Playwright 自带 Chromium 下载，复用系统 `/usr/bin/chromium`。
  - 构建成功镜像：`deeppresenter-sandbox:latest`，大小约 5.77GB。
  - 容器启动冒烟测试：`docker run --rm deeppresenter-sandbox node --version` 返回 `v24.18.0`。
- 当前 server 测试结果：`92 passed, 1 skipped in 2.40s`。
- 完成真实 AgentLoop 端到端冒烟测试：
  - 配置本地 `deeppresenter/config.yaml` 后，模型 API 校验通过。
  - `deeppresenter/html2pptx` npm 依赖已安装，Python Playwright Chromium 已安装。
  - sandbox MCP、deeppresenter MCP、pptagent MCP、task MCP 连接验证通过，工具总数 17。
  - 修复 `inspect_manuscript` 导入时触发 HuggingFace 下载的问题，语言检测改为仅使用本地缓存，未命中则返回 `unknown`。
  - 修复 `AgentEnv` 只接受单个 text/image block 的限制，支持工具返回 `TextContent + ImageContent`，避免读取预览图片时中断。
  - 强化 Design Agent HTML 结构约束，要求可见文本必须包裹在块级语义元素内，减少 html2pptx 裸文本校验失败。
  - 真实任务 `59f7a380` 结果：prepare/research/generate/export 全流程完成，`slide.preview_ready` 已发出，`task.completed` 状态为 `succeeded`。
  - 真实产物：`.local-workspace/59f7a380/test_slide.pptx`、`test_slide.pdf`、`slides/.../preview.png`。
- 当前 server 测试结果：`93 passed, 1 skipped in 2.45s`。

## 已发现问题和待处理点

1. `TaskStatus` 使用 `succeeded`，但部分文档/示例仍可能出现 `completed`。需要在模型、API 文档、前端约定中统一。
2. 当前虚拟环境已安装完整 PPTAgent 运行依赖；真实 LLM 生成仍需要本地 `deeppresenter/config.yaml` 填入可用模型 API key。
3. TaskManager 已接入真实 `AgentLoop`，并已完成 1 页真实 LLM/MCP 端到端冒烟测试；后续仍需扩大到多页任务和模板任务。
4. HTML 预览已接入 `inspect_slide` 成功路径，并在完整 AgentLoop 真实生成中验证可发出 `slide.preview_ready`。
5. 模板模式已能基于结构化数据生成保底 PNG 预览，但尚未验证真实 `generate_slide()` 返回结构与模板视觉还原度。
6. Docker sandbox 已通过本地构建准备完成，并已验证 sandbox MCP 连接和真实任务工具调用；后续需要观察多页并发任务稳定性。

## 下一步计划

1. 补齐完整运行依赖并做真实链路冒烟测试：
   - 配置 `DEEPPRESENTER_CONFIG_FILE` 或默认 `deeppresenter/config.yaml`，填入可用模型 API key。
   - 准备 `deeppresenter/mcp.json`，验证 sandbox MCP 可连接。
   - 启动 FastAPI 后创建一个最小任务，验证 SSE 阶段事件、逐页预览和最终产物。
2. 补充 API/任务快照与 C2 产物的进一步对齐：
   - 后续可根据前端需要裁剪 `GET /api/tasks/{task_id}` 内联 slides 字段规模。
   - artifact URL 统一用 `/api/tasks/{task_id}/artifacts/{path}`。
   - 预览失败时当前只记录 warning；后续可增加标准 warning 事件。
3. 模板模式真实链路验证：
   - 用真实或 mock `generate_slide()` 结果验证 `AgentEnv._maybe_capture_template_slide()` 事件和 artifact。
   - 根据真实返回结构调整 `slide_index`、`layout_name`、`structured_data` 提取逻辑。
   - 后续可替换当前保底 HTML 预览为更高保真的模板单页截图。
4. 补真实链路验证：
   - 用 mock 或最小真实任务验证阶段事件顺序。
   - 确认工具事件 payload 不泄漏大文本。
5. 每次与 C1 合并后：
   - 运行 server 单测。
   - 更新本文档的“已完成”和“待处理点”。
   - 在提交信息中标明 C2 修改范围。
