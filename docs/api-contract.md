# API Contract v1

> 最后更新：2026-07-13 | C1 负责维护 | 字段变更必须同步本文档

---

## 1. 概述

- **协议**：REST（创建/查询/操作）+ SSE（服务端推送进度）
- **编码**：JSON，UTF-8
- **SSE 格式**：`text/event-stream`，每条 `data:` 行是一个完整 JSON 对象
- **事件序号**：每个任务独立递增 `seq`（从 1 开始），用于前端去重和断线恢复
- **认证**：本期不做，所有接口无鉴权
- **基础路径**：`http://localhost:8000`

---

## 2. 通用约定

### 2.1 时间格式

所有时间字段使用 ISO 8601 字符串：`"2026-07-13T14:30:00.123456"`

### 2.2 错误响应

```json
{
  "error": {
    "code": "TASK_NOT_FOUND",
    "message": "可读的错误描述"
  }
}
```

### 2.3 任务状态机

```
queued → running → succeeded
                 → failed → (retry) → running
                 → cancelled
```

终态（succeeded / failed / cancelled）不可再转换，cancelled 不可 retry。

---

## 3. 事件模型 (SSE)

### 3.1 GenerationEvent 通用结构

```json
{
  "task_id": "abc12345",
  "seq": 42,
  "type": "stage.completed",
  "stage": "research",
  "status": null,
  "progress": 40.0,
  "stage_progress": 100.0,
  "message": "资料研究阶段已完成",
  "slide_id": null,
  "slide_index": null,
  "total_slides": null,
  "artifact_url": null,
  "created_at": "2026-07-10T14:30:00.000000",
  "payload": {}
}
```

### 3.2 字段说明

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| task_id | string | ✓ | 任务唯一 ID，8 位 hex |
| seq | int | ✓ | 单任务递增事件序号，从 1 开始 |
| type | string | ✓ | 事件类型，见 3.3 |
| stage | string | 部分 | prepare / plan / research / generate / edit / export |
| status | string | 部分 | queued / running / succeeded / failed / cancelled |
| progress | float | | 总进度 0~100，无法计算时为 null |
| stage_progress | float | | 当前阶段进度 0~100，无法计算时为 null |
| message | string | 建议 | 用户可读的简短说明（中文或英文） |
| slide_id | string | 部分 | 与页面相关的事件填写稳定 UUID |
| slide_index | int | 部分 | 当前页码（1-based） |
| total_slides | int | 部分 | 总页数 |
| artifact_url | string | 部分 | 缩略图 / PPTX 等产物的相对路径 |
| created_at | string | ✓ | ISO 8601 时间 |
| payload | object | | 少量扩展数据，不放大量文本 |

### 3.3 事件类型清单

#### 任务生命周期

| type | 触发时机 | 必填字段 |
|------|---------|---------|
| `task.created` | 任务创建成功 | status=queued, progress=0 |
| `task.started` | 任务开始执行 | status=running, progress=0 |
| `task.completed` | 任务全部完成 | status=succeeded, progress=100, artifact_url |
| `task.failed` | 任务失败 | status=failed, message 含错误信息 |
| `task.cancelled` | 用户取消任务 | status=cancelled, progress 停留在当前值 |

#### 模板解析（方向 A 发布）

| type | 触发时机 | 必填字段 |
|------|---------|---------|
| `template.parse_started` | 开始解析模板 | payload: {template_name} |
| `template.parse_progress` | 解析步骤完成 | progress=当前模板解析进度, message=步骤描述 |
| `template.ready` | 解析成功可选用 | payload: {manifest} |
| `template.failed` | 解析失败 | message=错误原因 |

#### 阶段进度（方向 C 发布）

| type | 触发时机 | 必填字段 |
|------|---------|---------|
| `stage.started` | 进入新阶段 | stage, progress=阶段起始值 |
| `stage.progress` | 阶段内进度更新 | stage, stage_progress |
| `stage.completed` | 阶段完成 | stage, progress=阶段结束值 |

#### 页面级事件（方向 C 发布）

| type | 触发时机 | 必填字段 |
|------|---------|---------|
| `slide.started` | 开始生成某页 | slide_id, slide_index, total_slides |
| `slide.preview_ready` | 缩略图可预览 | slide_id, slide_index, artifact_url=缩略图路径 |
| `slide.completed` | 页面生成完成 | slide_id, slide_index |
| `slide.failed` | 页面生成失败 | slide_id, slide_index, message=错误信息 |

#### 编辑事件（方向 D 发布）

| type | 触发时机 | 必填字段 |
|------|---------|---------|
| `edit.started` | 开始修改某页 | slide_id, payload: {instruction} |
| `edit.preview_ready` | 修改后新预览就绪 | slide_id, artifact_url, payload: {revision} |
| `edit.applied` | 修改生效 | slide_id, payload: {revision} |
| `edit.failed` | 修改失败 | slide_id, message |
| `edit.reverted` | 撤销操作 | slide_id, payload: {from_revision, to_revision} |

#### 导出事件（方向 C 发布）

| type | 触发时机 | 必填字段 |
|------|---------|---------|
| `export.started` | 开始导出 | |
| `export.completed` | 导出完成 | artifact_url=最终文件路径 |
| `export.failed` | 导出失败 | message |

---

## 4. REST API

### 4.1 任务

#### POST /api/tasks — 创建任务

```
Request:
{
  "instruction": "AI发展趋势报告",
  "attachments": ["/path/to/file.pdf"],
  "num_pages": "8",
  "powerpoint_type": "16:9",
  "template": null,
  "enable_planner": false,
  "language": "zh"
}

Response 201:
{
  "task_id": "abc12345",
  "status": "queued",
  "created_at": "2026-07-10T14:30:00"
}
```

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| instruction | string | ✓ | PPT 主题/要求 |
| attachments | string[] | | 附件文件路径列表 |
| num_pages | string | | 页数，如 "8" 或 "5-10" |
| powerpoint_type | string | | 比例：16:9 / 4:3 / A1 / A2 / A3 / A4，默认 16:9 |
| template | string | | 模板 ID，不填用系统默认 |
| enable_planner | bool | | 是否启用大纲规划阶段，默认 false |
| language | string | | en / zh，默认 en |

#### GET /api/tasks/{task_id} — 获取任务快照

```
Response 200:
{
  "task_id": "abc12345",
  "status": "running",
  "progress": 55.0,
  "current_stage": "generate",
  "created_at": "2026-07-10T14:30:00",
  "updated_at": "2026-07-10T14:32:30",
  "instruction": "AI发展趋势报告",
  "total_slides": 8,
  "completed_slides": 3,
  "failed_slides": 0,
  "slides": [
    {
      "slide_id": "sld-001-uuid",
      "index": 1,
      "status": "completed",
      "preview_url": "/api/tasks/abc12345/artifacts/slides/sld-001-uuid/revisions/1/preview.png"
    },
    {
      "slide_id": "sld-002-uuid",
      "index": 2,
      "status": "generating",
      "preview_url": null
    },
    {
      "slide_id": "sld-003-uuid",
      "index": 3,
      "status": "pending",
      "preview_url": null
    }
  ]
}
```

#### GET /api/tasks/{task_id}/events — SSE 订阅

```
Query: ?last_seq=0

Response: text/event-stream
data: {"task_id":"abc12345","seq":1,"type":"task.created","status":"queued","progress":0,...}

data: {"task_id":"abc12345","seq":2,"type":"task.started","status":"running","progress":0,...}

data: {"task_id":"abc12345","seq":3,"type":"stage.started","stage":"plan","progress":5,...}

...
```

- `last_seq`：传入上次收到的最大 seq，服务端先回放 seq > last_seq 的历史事件，再推送新事件
- 每 30 秒发一条心跳：`data: {"type":"heartbeat"}\n\n`（不在 events.jsonl 中记录）
- 任务结束后发送 `data: {"type":"eof","task_id":"abc12345"}\n\n` 关闭连接

#### POST /api/tasks/{task_id}/cancel — 取消任务

```
Response 200:
{
  "task_id": "abc12345",
  "status": "cancelled",
  "message": "任务已取消，已完成页面保留"
}
```

- 不再启动新页生成
- 已完成页保留
- 发出 `task.cancelled` 事件

#### POST /api/tasks/{task_id}/retry — 重试失败任务

```
Request:
{
  "retry_failed_slides_only": true
}

Response 200:
{
  "task_id": "abc12345",
  "status": "running",
  "message": "正在重试 2 个失败页面"
}
```

- `retry_failed_slides_only=true`：只重试失败的页
- `retry_failed_slides_only=false`：重试整个失败阶段

#### GET /api/tasks — 列出所有任务

```
Response 200:
{
  "tasks": [
    {
      "task_id": "abc12345",
      "status": "completed",
      "instruction": "AI发展趋势报告",
      "created_at": "2026-07-10T14:30:00"
    }
  ]
}
```

---

### 4.2 幻灯片

#### GET /api/tasks/{task_id}/slides — 获取所有页

```
Response 200:
{
  "slides": [
    {
      "slide_id": "sld-001-uuid",
      "index": 1,
      "status": "completed",
      "mode": "html",
      "layout_name": null,
      "current_revision": 1,
      "preview_url": "/api/tasks/abc12345/artifacts/slides/sld-001-uuid/revisions/1/preview.png",
      "created_at": "...",
      "updated_at": "..."
    }
  ],
  "total": 8
}
```

#### GET /api/tasks/{task_id}/slides/{slide_id} — 获取单页详情

```
Response 200:
{
  "slide_id": "sld-001-uuid",
  "index": 1,
  "status": "completed",
  "mode": "html",
  "layout_name": null,
  "structured_data": {
    "title": "AI发展趋势总览",
    "body": ["要点1", "要点2"],
    "images": [{"path": "chart.png", "caption": "增长趋势"}]
  },
  "current_revision": 3,
  "preview_url": "/api/tasks/abc12345/artifacts/slides/sld-001-uuid/revisions/3/preview.png",
  "revision_count": 3,
  "created_at": "...",
  "updated_at": "..."
}
```

---

### 4.3 局部编辑（方向 D 对接）

#### POST /api/tasks/{task_id}/slides/{slide_id}/chat — 发送修改指令

```
Request:
{
  "instruction": "把这页文字精简到三点",
  "element_id": null
}

Response 202:
{
  "slide_id": "sld-001-uuid",
  "status": "editing",
  "message": "修改指令已接收"
}
```

- `element_id`：如果用户点击了具体元素则传入，否则 null
- 如果页面还在生成中，指令排队等待完成后自动执行（返回 202 + `queued: true`）
- 如果页面空闲，立即执行

#### GET /api/tasks/{task_id}/slides/{slide_id}/revisions — 获取版本列表

```
Response 200:
{
  "slide_id": "sld-001-uuid",
  "current_revision": 3,
  "revisions": [
    {"revision": 3, "preview_url": "...", "created_at": "...", "message": "精简到三点"},
    {"revision": 2, "preview_url": "...", "created_at": "...", "message": "重新排版"},
    {"revision": 1, "preview_url": "...", "created_at": "...", "message": "初始生成"}
  ]
}
```

- 最多保留 10 个 revision，超过后删除最旧的预览图

#### POST /api/tasks/{task_id}/slides/{slide_id}/revisions/{revision}/apply — 切换版本

```
Response 200:
{
  "slide_id": "sld-001-uuid",
  "current_revision": 2,
  "preview_url": "...",
  "message": "已切换到版本 2"
}
```

- 不调模型，纯指针切换

#### POST /api/tasks/{task_id}/slides/{slide_id}/undo — 撤销

```
Response 200:
{
  "slide_id": "sld-001-uuid",
  "current_revision": 2,
  "previous_revision": 3,
  "preview_url": "..."
}
```

- 不调模型，current revision 指针回退一步
- 如果已经是 revision 1 则返回 400

#### POST /api/tasks/{task_id}/slides/{slide_id}/retry — 重试失败修改

```
Response 200:
{
  "slide_id": "sld-001-uuid",
  "status": "editing",
  "message": "正在重试修改"
}
```

---

### 4.4 产物文件

#### GET /api/tasks/{task_id}/artifacts/{path} — 读取产物

- `path` 限制在 `workspace/<task_id>/` 目录内，做路径穿越校验
- 常见路径：
  - `slides/<slide_id>/revisions/<n>/preview.png` — 单页缩略图
  - `exports/latest.pptx` — 最终导出文件
  - `outline.json` — 大纲
  - `manuscript.md` — 稿件

#### POST /api/tasks/{task_id}/export — 导出 PPTX

```
Response 200:
{
  "task_id": "abc12345",
  "artifact_url": "/api/tasks/abc12345/artifacts/exports/latest.pptx",
  "status": "completed"
}
```

- 读取每页最新成功的 revision，合并导出
- 发出 `export.started` → `export.completed` / `export.failed` 事件

---

### 4.5 模板（方向 A 对接）

#### POST /api/templates — 上传模板

```
Request: multipart/form-data
  file: template.pptx

Response 201:
{
  "template_id": "tpl-001",
  "name": "template.pptx",
  "status": "parsing",
  "message": "模板已上传，正在解析"
}
```

#### GET /api/templates — 获取模板列表

```
Response 200:
{
  "templates": [
    {
      "template_id": "tpl-001",
      "name": "商务蓝模板",
      "status": "ready",
      "slide_count": 12,
      "layout_count": 5,
      "aspect_ratio": "16:9",
      "primary_color": "#1a73e8",
      "thumbnail_url": "/api/templates/tpl-001/thumbnail.png",
      "created_at": "..."
    }
  ]
}
```

#### GET /api/templates/{template_id} — 获取模板详情

```
Response 200:
{
  "template_id": "tpl-001",
  "name": "商务蓝模板",
  "status": "ready",
  "manifest": { ... },
  "slide_count": 12,
  "layout_count": 5,
  "thumbnails": ["...slide_0.png", "...slide_1.png"]
}
```

#### GET /api/templates/{template_id}/events — 模板解析 SSE

- 事件类型：`template.parse_started` / `template.parse_progress` / `template.ready` / `template.failed`

#### DELETE /api/templates/{template_id} — 删除模板

```
Response 200:
{
  "template_id": "tpl-001",
  "status": "deleted"
}
```

---

## 5. SlideArtifact 数据模型

### 5.1 字段定义

| 字段 | 类型 | 说明 |
|------|------|------|
| slide_id | string | 稳定 UUID，页码变化时不变 |
| task_id | string | 所属任务 |
| index | int | 当前排序（1-based） |
| status | string | pending / generating / completed / failed / editing |
| mode | string | "html" 或 "template" |
| layout_name | string? | 模板模式下使用的布局名称 |
| structured_data | object | {title, subtitle, body:[], images:[{path, caption}]} |
| source_path | string | HTML 文件或结构化源文件路径 |
| preview_path | string | PNG/JPG 缩略图相对路径 |
| revision | int | 当前版本号，从 1 开始 |
| created_at | string | 创建时间 |
| updated_at | string | 最后更新时间 |

### 5.2 工作区目录结构

```
workspace/<task_id>/
  task.json                  # 任务快照
  events.jsonl               # 事件日志
  outline.json               # 大纲
  manuscript.md              # 稿件
  input_request.json         # 原始请求
  slides/
    <slide_id>/
      current.json            # → revisions/3/slide.json (符号链接或指针)
      revisions/
        1/
          slide.json          # SlideArtifact 完整数据
          preview.png         # 缩略图
          source.html         # 源文件（HTML 模式）
        2/
          slide.json
          preview.png
        3/
          slide.json
          preview.png
  exports/
    latest.pptx               # 最新导出
```

---

## 6. 进度计算规则

| 阶段 | 权重 | 累计 |
|------|------|------|
| prepare（附件处理、初始化） | 5% | 0-5% |
| plan（大纲规划） | 10% | 5-15% |
| research（资料研究/稿件） | 25% | 15-40% |
| generate（页面生成） | 50% | 40-90% |
| export（合并导出） | 10% | 90-100% |

- 跳过 Planner 时，plan 权重合并到 research（即 research=35%）
- generate 阶段 `progress = 40 + 50 * (completed_slides / total_slides)`
- 无法精确计算时 `progress` 可为 null

---

## 7. 模板解析进度（方向 A 参考）

| 步骤 | progress |
|------|----------|
| 文件校验 | 5% |
| .ppt 转 .pptx（如需要） | 10% |
| 规范化并读取页面结构 | 20% |
| 渲染原始页和空布局页 | 35% |
| 布局分类/聚类 | 55% |
| 页面元素与内容 schema 抽取 | 80% |
| 生成缩略图、manifest 并注册 | 100% |

---

## 8. SSE 断线重连

1. 前端记录最近一次收到的 `seq`
2. 断线后重新调用 `GET /api/tasks/{task_id}/events?last_seq=<seq>`
3. 服务端先回放 `events.jsonl` 中 seq > last_seq 的事件
4. 前端根据 seq 去重（相同 seq 的事件只处理一次）
5. 服务端发送心跳（30s）防止中间代理断开
6. 重连期间前端显示"正在重连"，不把任务显示为失败

---

## 9. 通用错误码

| code | HTTP Status | 说明 |
|------|-------------|------|
| TASK_NOT_FOUND | 404 | 任务不存在 |
| SLIDE_NOT_FOUND | 404 | 页面不存在 |
| TEMPLATE_NOT_FOUND | 404 | 模板不存在 |
| TASK_INVALID_STATE | 409 | 任务状态不允许此操作 |
| SLIDE_LOCKED | 409 | 页面正在被其他操作修改 |
| TEMPLATE_PARSE_FAILED | 422 | 模板解析失败 |
| FILE_TOO_LARGE | 413 | 上传文件过大 |
| INVALID_FILE_TYPE | 415 | 不支持的文件格式 |
| ARTIFACT_NOT_FOUND | 404 | 产物文件不存在 |
| PATH_TRAVERSAL | 403 | 路径穿越被拦截 |
| INTERNAL_ERROR | 500 | 服务器内部错误 |

---

## 10. 变更记录

| 日期 | 版本 | 变更 |
|------|------|------|
| 2026-07-10 | v1 | 初始版本，冻结第一版接口字段 |
