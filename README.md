# C 组开发规划：任务编排、实时进度与逐页预览

分支：`feat/realtime-progress-preview`

人员：2 人（C1、C2）

周期：两周，负责四个方向之间的后端主接口与事件链路

## 本组目标

在现有 `AgentLoop` 外增加稳定的任务服务，把内部 Agent 消息和工具调用转换成前端可消费的结构化事件，并在每一页完成时立即形成独立产物和预览。

本组完成后应实现：

> 创建任务 → 查询任务 → 订阅 SSE → 查看规划/研究/逐页生成/导出进度 → 一页完成立即预览 → 取消或重试 → 刷新/断线后恢复 → 导出最终文件。

## 当前代码判断

- `deeppresenter/main.py::AgentLoop.run()` 已经是异步生成器，适合在阶段边界发事件。
- `deeppresenter/agents/env.py` 统一执行 MCP/本地工具，适合发工具开始、完成和失败事件。
- 当前 `webui.py` 直接消费 `ChatMessage`，只能展示原始过程，缺少任务状态机和结构化页级进度。
- `intermediate_output.json` 和 `.history/` 已有部分中间结果，可作为恢复能力的基础，但目前没有统一任务快照。
- HTML 路线已有 Playwright 和 `html2pptx`；模板路线已有 `generate_slide()`，两种路线需要统一产出 `SlideArtifact`。
- 当前流程没有明确取消检查点、事件序号、断线回放和单页失败隔离。

## P0 工作范围

1. 新增 FastAPI 统一服务入口。
2. 实现任务创建、查询、取消和导出接口。
3. 定义任务状态机和 `GenerationEvent`。
4. 实现 SSE 事件流、心跳、事件序号和断线回放。
5. 为 Planner、Research、Design/PPTAgent、Convert 阶段增加显式事件。
6. 将每页状态标准化为等待、生成中、已完成、失败、修改中。
7. HTML 与模板模式都能在单页完成后生成预览。
8. 每页形成稳定 `slide_id` 和独立产物。
9. 任务刷新后可从快照和 `events.jsonl` 恢复。
10. 取消后不再启动新页面，已完成页面仍保留。
11. 单页失败不清空整套已完成结果。

## P1 工作范围

- 失败阶段或失败页一键重试。
- 两个任务并发时的简单队列/限流。
- 更细的工具耗时统计和开发诊断页面。
- 服务重启后自动继续未完成任务；P0 只要求恢复为可理解状态并允许重试。

本期不引入 Redis、Celery、Kafka 或 Kubernetes。先使用进程内 `asyncio.Task`/`asyncio.Queue`，同时将事件和快照落盘，保持未来替换空间。

## 建议代码结构

```text
deeppresenter/server/
  app.py
  routes/
    tasks.py
    templates.py
    slides.py
  services/
    task_manager.py
    event_bus.py
    preview.py
    artifact_store.py
  models/
    events.py
    tasks.py
    artifacts.py
```

现有 CLI 必须继续工作。新增事件通过可选 reporter/callback 注入，CLI 可以忽略事件或显示简化状态，不能为了 Web 服务完全改坏 `pptagent generate`。

## 任务状态机

```text
queued
  -> running
      -> completed
      -> failed
      -> cancelled
```

如果实现重试：

```text
failed -> queued -> running
```

要求：

- 状态转换集中在一个模块，不允许各路由随意改字符串。
- completed/failed/cancelled 是终态，除显式 retry 外不能回到 running。
- API 返回任务快照和事件必须使用同一套枚举。
- 任务异常退出时必须落盘为 failed，不能永久停在 running。

## 统一事件模型

`GenerationEvent` 至少包含：

```text
task_id
seq
type
stage
status
progress
message
slide_id
slide_index
total_slides
artifact_url
created_at
payload
```

第一阶段事件类型：

- `task.created`、`task.started`、`task.completed`、`task.failed`、`task.cancelled`
- `stage.started`、`stage.progress`、`stage.completed`
- `slide.started`、`slide.preview_ready`、`slide.completed`、`slide.failed`
- `edit.started`、`edit.preview_ready`、`edit.applied`、`edit.failed`、`edit.reverted`
- `export.started`、`export.completed`、`export.failed`

A 组的模板解析事件也使用同一基础模型，由其提供 template stage 的 payload。

## 任务进度权重

总进度按确定阶段计算，不伪造精确剩余时间：

| 阶段 | 权重 |
|---|---:|
| 任务准备和附件处理 | 5% |
| 大纲规划 | 10% |
| 资料研究和稿件生成 | 25% |
| 页面生成 | 50% |
| 合并与导出 | 10% |

页面生成部分按完成页数均分。若未启用 Planner，将规划权重合并到研究阶段。

## SSE 约定

- 每个任务有独立递增 `seq`。
- 服务端将事件追加写入 `workspace/<task_id>/events.jsonl`。
- 前端通过 `Last-Event-ID` 或查询参数传入最后 `seq`。
- 重连时先回放缺失事件，再进入实时队列。
- 定期发送心跳，心跳不改变任务状态。
- 慢客户端不能阻塞生成任务；实时队列需要有限缓冲和落盘兜底。
- 事件 payload 只放必要摘要，不把大段模型输出或图片 base64 写入 SSE。

## 任务工作区

```text
workspace/<task_id>/
  task.json
  events.jsonl
  .input_request.json
  outline.json
  manuscript.md
  slides/
    <slide_id>/
      current.json
      revisions/
  exports/
    latest.pptx
```

所有前端产物通过安全 artifact API 访问，不直接暴露任意本地路径。

## 逐页预览策略

### HTML 模式

1. Design Agent 完成 `slide_*.html`。
2. 文件通过现有检查。
3. 使用 Playwright 渲染目标页 PNG/JPG。
4. 保存为该页 revision 的 preview。
5. 发布 `slide.preview_ready`。

### 模板模式

1. `generate_slide()` 返回目标 `SlidePage`。
2. 立即持久化结构化页面和单页产物。
3. 生成单页 PPTX 或图片预览。
4. 发布 `slide.preview_ready`，然后继续其他页面。

预览失败只发警告/失败事件，不应自动否定已经生成成功的页面源文件。最终导出是否成功另行判断。

## API 契约

| 方法 | 路径 | 用途 |
|---|---|---|
| `POST` | `/api/tasks` | 创建任务，返回 `task_id` |
| `GET` | `/api/tasks/{task_id}` | 获取任务快照 |
| `GET` | `/api/tasks/{task_id}/events` | SSE 订阅和历史回放 |
| `POST` | `/api/tasks/{task_id}/cancel` | 取消任务 |
| `POST` | `/api/tasks/{task_id}/retry` | 重试失败阶段 |
| `GET` | `/api/tasks/{task_id}/slides` | 获取所有页状态和当前版本 |
| `GET` | `/api/tasks/{task_id}/artifacts/{path}` | 读取允许的任务产物 |
| `POST` | `/api/tasks/{task_id}/export` | 按最新页面版本导出 |

模板和局部编辑的业务接口分别由 A、D 组实现，但必须通过 C 组的任务、事件和 artifact 基础设施发布状态。

## 两人分工

### C1：FastAPI、TaskManager 和任务生命周期

- FastAPI app 和任务路由。
- `TaskManager`、状态机和任务快照。
- 创建、查询、取消、失败和重试。
- 导出任务协调。
- 服务重启后的状态恢复。
- Docker/启动脚本和后端接口负责人。

### C2：事件总线、Agent 钩子和预览

- `GenerationEvent`、事件 bus、SSE 和历史回放。
- 在 `AgentLoop`/`AgentEnv` 增加 reporter 钩子。
- 页级状态归一化。
- HTML/模板单页预览服务。
- artifact URL 和缓存版本处理。
- 与 B 组 SSE/预览联调。

C1 兼任后端接口负责人。状态模型和事件协议需由 C1/C2 共同 review。

## 两周安排

| 工作日 | C1 | C2 | 当天结果 |
|---|---|---|---|
| 第 1 天 | 状态机、API 草案 | 事件模型、预览草案 | 与 A/B/D 冻结第一版接口 |
| 第 2 天 | FastAPI、TaskManager | EventBus、SSE 骨架 | 可创建测试任务并接收事件 |
| 第 3 天 | 查询、取消、任务快照 | `seq`、落盘和回放 | 刷新后能恢复 mock 任务 |
| 第 4 天 | AgentLoop 阶段钩子 | AgentEnv 工具钩子 | 真实任务有结构化阶段事件 |
| 第 5 天 | 页级状态模型 | 第一种模式单页预览 | 一页完成即出现预览 |
| 第 6 天 | 第二种模式任务衔接 | 第二种模式单页预览 | 两种模式统一 SlideArtifact |
| 第 7 天 | 取消检查点和恢复 | SSE 重连和预览失败处理 | 取消/断线状态正确 |
| 第 8 天 | 重试、导出协调 | 并发和事件异常处理 | P0 功能冻结 |
| 第 9 天 | API/状态集成测试 | SSE/预览集成测试 | 完整链路和异常路径稳定 |
| 第 10 天 | Docker、接口文档 | 运行诊断、演示修复 | 完成交付 |

## 取消与并发规则

- 取消首先停止启动新阶段和新页面，再取消当前可取消的 asyncio 任务。
- Docker/外部进程需要显式终止或标记回收，不能只取消 Python future。
- 已完成页面和事件保留。
- 同一任务只允许一个主生成流程。
- 单页编辑由 D 组使用页级锁处理；C 组确保主生成和编辑事件可并存。
- 两个任务必须使用不同工作区、事件队列和 artifact 根目录。

## 测试清单

- 合法和非法任务状态转换。
- `seq` 单调递增、序列化和回放。
- SSE 正常连接、重复连接、断开和补事件。
- 真实 AgentLoop 各阶段均有 started/completed/failed。
- HTML 和模板模式逐页形成预览。
- 预览失败时页面源产物仍保留。
- 某一页失败时其他页面继续并保存。
- 取消后不再启动新页面。
- 服务重启后 completed/failed/cancelled 任务状态正确。
- running 任务在异常重启后不会永久显示运行中。
- 两任务并发时事件、页面和文件不串线。
- artifact API 不能读取任务目录外的文件。

## 完成标准

- 前端可只依赖结构化事件展示完整进度，不需要解析 Agent 文本。
- 任务事件有稳定 `seq`，断线重连不重页、不丢完成状态。
- 两种生成模式都能在整套 PPT 完成前提供单页预览。
- 取消、失败、单页异常和服务刷新均有可理解状态。
- 最终导出过程也有事件，并返回安全可访问的文件 URL。
- 不破坏现有 CLI 基本生成入口。

## 与其他组的交付边界

- 接收 A 组：模板解析 service 和模板事件回调。
- 提供 B 组：任务 API、SSE、快照、预览和 artifact URL。
- 提供 D 组：SlideArtifact 存储、页级事件发布和导出协调。
- C 组不负责模板聚类算法，也不负责具体的对话提示词和前端视觉实现。
