# PPTAgent 前后端 API 契约

> 本文件是前端（`frontend/`）与后端（FastAPI）之间的接口契约，与 `frontend/src/types/api.ts` 一一对应。
> **任何字段变更必须同步本文件与 `frontend/src/types/api.ts`，不靠口头。**
> 前端不解析自然语言日志推断进度，只消费本文件定义的结构化事件。

标注 **[新增]** 的条目是前端实现过程中补充的、`frontend/FRONTEND_SPEC.md` §10.1 原表未列出的端点，需后端确认实现。

## 1. REST

| 方法 | 路径 | 用途 | 请求体 | 响应 |
|---|---|---|---|---|
| POST | `/api/attachments` **[新增]** | 上传参考资料（PDF/Word/Excel），multipart `file` 字段 | `FormData{file}` | `{attachment_id}` |
| DELETE | `/api/attachments/{id}` **[新增]** | 清理尚未绑定任务的临时参考资料 | — | — |
| POST | `/api/tasks` | 创建任务 | `CreateTaskPayload` | `TaskSnapshot` |
| GET | `/api/tasks/{id}` | 任务快照（含 `last_seq`） | — | `TaskSnapshot` |
| GET | `/api/tasks/{id}/events` | SSE 事件流，支持 `?last_seq=`，事件 `id:` 字段即 `seq` | — | `GenerationEvent` 流 |
| POST | `/api/tasks/{id}/cancel` | 取消（已完成页保留） | — | `TaskSnapshot` |
| POST | `/api/tasks/{id}/retry` | 恢复/重试任务 | — | `TaskSnapshot` |
| GET | `/api/tasks/{id}/slides` | 全部页 + 当前版本 | — | `SlideArtifact[]` |
| POST | `/api/tasks/{id}/export` | 导出 | `{format: "pptx"\|"pdf"}` | `{export_id}` |
| GET | `/api/templates` | 模板列表 | — | `TemplateSummary[]` |
| POST | `/api/templates` | 上传模板（multipart `file`，`.pptx`，P1 支持 `.ppt`） | `FormData{file}` | `TemplateSummary`（status=parsing） |
| DELETE | `/api/templates/{id}` | 删除模板 | — | — |
| POST | `/api/templates/{id}/retry` **[新增]** | 重新解析失败模板 | — | — |
| GET | `/api/templates/events` **[新增]** | 模板解析进度 SSE，支持 `?last_seq=`（`template.*` 事件） | — | `GenerationEvent` 流 |
| POST | `/api/tasks/{id}/slides/{sid}/chat` | 页级修改指令 | `{text, element_id?}` | `{chat_id}` |
| GET | `/api/tasks/{id}/slides/{sid}/revisions` | 版本列表 | — | `SlideRevision[]` |
| POST | `/api/tasks/{id}/slides/{sid}/revisions/{rev}/apply` | 切换到指定版本（纯指针切换） | — | — |
| POST | `/api/tasks/{id}/slides/{sid}/undo` | 撤销到上一版（纯指针切换，即时响应） | — | — |
| POST | `/api/tasks/{id}/slides/{sid}/retry` | 重试失败页 / 重新生成本页 | — | — |

## 2. SSE 事件（`GenerationEvent`）

统一信封，字段与后端 Pydantic 模型一一对应：

```ts
interface GenerationEvent {
  task_id: string
  seq: number                 // 单调递增，SSE 的 id: 字段；客户端按 seq 去重
  type: string                // 见下方事件类型
  stage: "template" | "plan" | "research" | "generate" | "edit" | "export"
  status: "queued" | "running" | "succeeded" | "failed" | "cancelled"
  progress: number | null     // 0-100
  message: string             // 展示用文案
  slide_id: string | null
  slide_index: number | null  // 1-based
  total_slides: number | null
  artifact_url: string | null // 预览图 / 导出产物
  created_at: string          // ISO 8601
  payload: Record<string, unknown>  // 类型特定字段，见 §3
}
```

事件类型：

- `task.created | task.started | task.completed | task.failed | task.cancelled`
- `template.parse_started | template.parse_progress | template.ready | template.failed`
- `stage.started | stage.progress | stage.completed`
- `slide.started | slide.preview_ready | slide.completed | slide.failed`
- `edit.started | edit.preview_ready | edit.applied | edit.failed | edit.reverted`
- `export.started | export.completed | export.failed`

断线重连：客户端 EventSource 重连时携带 `Last-Event-ID`（= 最后收到的 `seq`），或显式 `?last_seq=`；服务端必须按该值补发缺失事件，禁止丢事件或重发已确认序号之前的事件。

## 3. 关键 payload 字段（前端消费）

| 事件 | payload 字段 |
|---|---|
| `task.created` | `template_id`, `ratio`；信封 `total_slides` 为计划页数（未定可为 null，前端以创建页设置兜底） |
| `stage.completed`（plan） | `outline: [{slide_id, index, title}]` — 前端据此把占位页换成真实 slide_id |
| `slide.started` | `title` |
| `slide.preview_ready` | `revision`；信封 `artifact_url` 为预览图 |
| `slide.completed` | `title`, `revision`, `label` |
| `template.*` | `template_id`；`parse_progress` 另带 `progress`；`failed` 另带 `reason`（简短失败原因，前端直接展示，缺失时回落信封 `message`） |
| `edit.started` | `chat_id` |
| `edit.preview_ready` | `chat_id`, `revision` |
| `edit.applied` | `chat_id`, `action`（修改摘要，如"更换配色"）, `revision`, `label` |
| `edit.failed` | `chat_id` |
| `edit.reverted` | `revision`, `label` |
| `export.*` | `format`, `export_id`；`completed` 另带 `filename`，信封 `artifact_url` 为下载链接 |

## 4. 实体类型

完整定义见 `frontend/src/types/api.ts`：`TaskSnapshot`、`SlideArtifact`、`SlideRevision`、`TemplateSummary`（含 `error?: string | null` — 解析失败原因，与 `template.failed` 的 `payload.reason` 一致）、`CreateTaskPayload`（`attachment_ids` 来自 `POST /api/attachments` 回执）、`ChatReceipt`、`AttachmentReceipt`、`ExportReceipt`。

## 5. 约定

1. 预览图 URL 可被前端追加内容事件序号形式的缓存参数（例如 `?v=42`），后端需容忍多余 query 参数。版本号允许分支后复用，不能作为唯一内容标识。
2. `seq` 以任务为作用域单调递增；模板事件流单独编号。
3. REST 响应只用于初始 hydrate 与乐观 UI；一切状态推进以 SSE 事件为准。
