# PPTAgent 实习日志

## 7 月 10 日

### 本地环境搭建与项目启动

今天主要完成了 PPTAgent 项目的本地部署。项目是一个 AI 驱动的 PPT 自动生成系统，代码仓库包含两条链路：

- `deeppresenter/`：当前主力链路，负责 CLI 入口、多 Agent 编排、HTML 幻灯片生成和 PPTX 导出
- `pptagent/`：旧版模板解析与模板式生成库，仍被 MCP 服务使用

部署过程中解决了若干环境问题——Docker 用于沙箱工具执行、Playwright 用于 HTML 渲染和 PDF 转换、Node.js 依赖用于 html2pptx 转换。在 Windows 上遇到了 `deeppresenter/__init__.py` 中的 POSIX 断言阻塞，通过环境变量跳过的方式临时解决。

最终在本地成功跑通了 `pptagent generate` 命令，验证了基础生成链路可用。

### 二次开发分工

项目启动会后明确了为期两周（10 个工作日）、8 人的二次开发计划，分为四个方向：

| 方向 | 负责内容 | 人数 |
|------|---------|------|
| A. 模板上传与解析 | PPTX 上传、校验、解析服务化、动态模板注册 | 2 人 |
| B. 新前端交互 | React 三栏工作台、SSE 进度、逐页预览、对话区 | 2 人 |
| C. 任务编排与实时进度 | FastAPI 服务、任务状态机、SSE 事件流、逐页预览渲染 | 2 人 |
| D. 对话式局部编辑 | 单页修改、版本管理、撤销、合并导出 | 2 人 |

各方向通过统一的 `GenerationEvent` 事件协议和 REST API 契约协作，使用短分支 + 每日合并的方式，避免两周后集中合并的冲突。

**我负责方向 C**——任务编排、实时进度与逐页预览。核心目标是在现有 `AgentLoop` 外加一层稳定的服务，把内部 Agent 消息转成前端可消费的结构化事件，并对接上下左右四个方向。

---

## 7 月 13 日

### 接口契约冻结

方向 C 是四个方向的"交通枢纽"——A 的模板解析结果、D 的编辑操作都要通过 C 暴露给 B（前端）。因此第一天最关键的事情是和其他方向对齐接口，大家对着同一份文档各写各的。

与前端负责人（B1）一起完成了 `docs/api-contract.md`，定义了：

- **8 个 REST API 端点**：任务创建/查询/取消/重试、SSE 订阅、幻灯片列表、产物读取、导出
- **24 种事件类型**：覆盖任务生命周期（5 种）、模板解析（4 种）、阶段进度（3 种）、页面事件（4 种）、编辑操作（5 种）、导出（3 种）
- **GenerationEvent 统一结构**：13 个字段，每条事件带递增 `seq` 序号，用于前端去重和断线恢复
- **5 状态任务状态机**：queued → running → succeeded/failed/cancelled，终态不可逆
- **进度权重分配**：准备 5%、规划 10%、研究 25%、逐页生成 50%、导出 10%
- **SSE 断线重连策略**：seq 回放 + 心跳 + 去重

### 数据模型实现

项目代码约定使用 Pydantic + 中文注释。在 `deeppresenter/server/models/` 下完成了：

**`events.py`**——事件模型核心：

- `EventType(str, Enum)`：24 种事件类型枚举
- `StageName(str, Enum)`：6 个生成阶段
- `TaskStatus(str, Enum)`：5 种任务状态
- `VALID_TASK_TRANSITIONS`：合法状态转移表
- `validate_task_transition(current, target)`：状态转移校验函数
- `GenerationEvent(BaseModel)`：完整事件模型，支持 JSON 双向序列化
- `stage_start_progress()` / `stage_end_progress()`：进度计算函数
- `parse_events_from_jsonl()`：从持久化文件解析事件

**`artifacts.py`**——页面产物模型：

- `SlideArtifact`：每页独立持久化，含稳定 UUID、版本号、结构化内容
- `StructuredContent`：可编辑内容（标题、正文、图片）
- 工作区路径工具函数和 `is_path_safe()` 路径穿越防护
- 版本自增 `bumped()` 方法和每页最多 10 版限制

### 事件总线实现

在 `deeppresenter/server/services/event_bus.py` 中实现了进程内事件总线：

- 基于 `asyncio.Queue` 的多订阅者广播，不引入 Redis/Celery
- 发布即持久化到 `events.jsonl`，支持崩溃恢复
- SSE 订阅者可获取历史事件回放后再进入实时推送
- 每 30 秒心跳防止代理超时
- `subscribe()` / `_iterate()` 分离设计——订阅者调用时立即注册，无需等待首次迭代

### 测试

编写了 39 个单元测试，覆盖状态机合法性、进度计算、事件序列化、JSONL 解析、路径安全、EventBus 发布/订阅/心跳/生命周期。全部通过。

同时准备了 `docs/events-example.jsonl`（33 条完整事件流样例），供前端方向同学 mock 开发。

### 提交记录

- `3c48609` feat(server): Day 1 — 事件模型、状态机、API 契约、EventBus
- `ae52d85` docs: 添加 7.13 Day 1 开发日志

分支：`feat/realtime-progress-preview`
