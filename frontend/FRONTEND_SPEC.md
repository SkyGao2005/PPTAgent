# PPTAgent 前端实现规格（交接文档）

> 本文档是前端实现的唯一交接入口，内容自包含。实现前请先通读。
> 视觉唯一基准：`../PPTAgent frontend design/` 下的三个 `.dc.html` 静态演示页（可用 `python3 -m http.server` 直接打开预览）。
> 产品与接口背景：仓库根目录 `../PPTAgent二次开发计划_两周_8人.md`（下称"总计划"），本文件已摘录前端需要的全部契约，正常情况下不需要回读总计划。

---

## 1. 目标与范围

用 React 重写 PPTAgent 的主界面（替代旧 Gradio `webui.py`），实现三个页面：

1. **新建任务页**（`/`）——输入主题、上传参考资料、选模板、设页数/比例、开始生成。
2. **模板库页**（`/templates`)——模板网格、上传解析、解析进度、失败重试。
3. **演示工作台**（`/workbench/:taskId`）——三栏布局：左侧页面缩略图 + 页级状态、中间大预览 + 版本切换、右侧针对当前页的 AI 聊天修改。

不做：账户体系、多人协作、自由拖拽编辑器、移动端适配（保证 ≥1280px 桌面可用即可）。

---

## 2. 技术栈（已定，勿更换）

| 层 | 选型 | 备注 |
|---|---|---|
| 框架 | React 19 + TypeScript + Vite 8 | 已初始化 |
| 样式 | Tailwind CSS v4（`@tailwindcss/vite`） | 主题走 CSS 变量，见 §4 |
| 组件库 | shadcn/ui（`components.json` 已配置，style: base-nova，底层 @base-ui/react） | 新组件用 `npx shadcn@latest add <name>` 添加 |
| 状态 | Zustand | 每页一个 store，见 §6 |
| 路由 | react-router-dom v7 | |
| 数据请求 | 原生 `fetch`（`src/lib/api.ts` 已有封装） + 原生 `EventSource`（SSE） | 不引入 react-query/axios |
| 图标 | lucide-react | |
| Toast | sonner（`src/components/ui/sonner.tsx` 已装） | |
| 上传 | react-dropzone（需安装） | |

**字体**：demo 使用 `Sora`（标题/数字）+ `Noto Sans SC`（正文）。脚手架里装的是 Geist，请替换为 `@fontsource/sora` 与 `@fontsource-variable/noto-sans-sc`，并在 `src/index.css` 设置：

```css
--font-sans: "Noto Sans SC", "PingFang SC", "Microsoft YaHei", sans-serif;
--font-display: "Sora", var(--font-sans);  /* 标题、页码、计时等数字 */
```

---

## 3. 现有脚手架状态（截至交接时）

```text
src/
  pages/create-page.tsx        # 已有静态骨架（约 340 行），交互未接 store
  pages/templates-page.tsx     # 同上
  pages/workbench-page.tsx     # 同上（约 570 行）
  stores/create-task-store.ts  # 部分实现
  stores/workbench-store.ts    # 仅 11 行，空壳，需按 §6 重写
  types/api.ts                 # GenerationEvent/TemplateSummary/TaskStatus 等已定义，与本文契约一致
  lib/api.ts                   # fetch 封装 + ApiError，已可用
  mocks/slides.ts, templates.ts# 演示假数据
  components/ui/*              # 已安装的 shadcn 组件（button/dialog/dropdown-menu/skeleton/progress/sheet/...）
```

开发代理：`vite.config.ts` 已把 `/api` 代理到 `http://127.0.0.1:8000`（FastAPI）。

**缺失、需要新建的关键模块**：SSE 客户端（§7）、workbench store 的事件归并逻辑（§6.3）、mock SSE 服务（§9）、运行详情抽屉、断线重连 UI、失败页重试（§8）。

---

## 4. 设计规范（从 demo 提取，写入 `src/index.css` 的 `:root`）

demo 是"暖纸色轻工作台"风格。**不要使用 shadcn 默认的中性灰主题**，用下表覆盖：

### 4.1 颜色

| Token | 值 | 用途 |
|---|---|---|
| `--background` | `#F0EFEB` | 页面底色（暖纸色） |
| `--card` / surface | `#FFFFFF` | 卡片、顶栏、输入区 |
| `--muted` | `#F6F5F2` / `#F1EFE9` | 次级底、禁用底 |
| `--border` | `#E7E4DD` | 所有描边（1px 实线） |
| `--foreground` | `#211F1C` | 主文字 |
| 次级文字 | `#6B665D` | 说明、进度文案 |
| 三级文字/图标 | `#8B857B` | 元信息、时间戳 |
| placeholder | `#A9A399` | |
| `--primary` | `oklch(0.45 0.11 272)` | 靛蓝主色：主按钮、链接、选中描边 |
| primary hover | `oklch(0.38 0.11 272)` | |
| primary 浅底 | `oklch(0.97 0.01 272)` | 选中态背景、次按钮底 |
| primary 淡彩 | `oklch(0.82 0.05 272)` | logo 副色、进度条已完成段 |
| 成功文字 | `oklch(0.42 0.1 160)` | "生成完成"徽章文字 |
| 成功底 | `oklch(0.95 0.03 160)` | 徽章背景 |
| 危险/失败 | `oklch(0.5 0.15 30)`，hover `oklch(0.55 0.18 30)` | 解析失败、取消生成 hover |
| 危险浅底 | `oklch(0.97 0.015 30)` | 失败卡片背景 |

### 4.2 形状与层次

- 圆角：按钮/输入框 `9px`；卡片 `12–14px`；徽章/胶囊/进度段 `999px`；缩略图 `8px`。
- 阴影：整体扁平，仅中央预览卡用一层柔和投影；其余靠 1px 边框分层。
- 顶栏高度 `56px`，白底 + 下边框。
- 动效：demo 内置 `spin / shimmer / fadeUp / blink / pulse` 关键帧，骨架屏用 shimmer，新预览出现用 fadeUp（约 0.3s），不加多余动画。

---

## 5. 页面规格

> 每个页面的布局、间距、文案以对应 `.dc.html` 为准，本节只列结构、状态与交互逻辑。

### 5.1 新建任务页 `/`（基准：`Create.dc.html`）

结构自上而下：标题区 → 主卡片（演示主题 textarea，0/200 计数，4 个示例主题胶囊）→ 参考资料上传区（可选，PDF/Word/Excel，dropzone）→ 模板选择行（横向卡片，展示最近 4 个 + "浏览模板库 →"链接）→ 生成设置（页数 Slider 5–30 默认 12；比例 16:9 / 4:3 切换）→ 底部"✦ 开始生成"主按钮。

交互：

- 主题为空时禁用开始按钮；点击示例胶囊填充 textarea。
- 上传文件展示文件名 + 大小 + 移除按钮；类型/大小校验失败 toast 提示。
- 点击"开始生成"→ `POST /api/tasks` → 拿到 `task_id` 立即 `navigate(/workbench/:taskId)`，不等生成。

### 5.2 模板库页 `/templates`（基准：`Templates.dc.html`）

- 顶部：标题 + "N 个模板 · M 个已就绪" + 右上"↑ 上传模板"主按钮。
- 筛选：搜索框 + 状态筛选胶囊（全部/已就绪/解析中/解析失败，带计数）。
- 网格卡片（4 列）：缩略图（解析完成前用模板主色画的占位封面）、名称、色板圆点、描述、"N 页 · 比例 · N 种版式"、状态徽章。
- **解析中**：卡片封面模糊 + "解析中 62%" + 细进度条；进度来自 SSE `template.parse_progress`。
- **解析失败**：显示简短原因（如"文件包含不支持的母版结构"）+ "重新解析"按钮。
- 上传：点击或拖入 `.pptx`（P1 支持 `.ppt`）→ `POST /api/templates`（multipart）→ 新卡片以"解析中"插入网格首位。

### 5.3 演示工作台 `/workbench/:taskId`（基准：`Workspace.dc.html`，核心页面）

**顶栏**（56px）：logo（点击返回创建页）｜任务标题 + 模板色点 + "模板名 · 比例 · N 页"｜中间进度区｜右侧操作。

- 进度区三态互斥：
  - 生成中：阶段文案 + 分段进度条（每页一段：完成=主色、进行中=淡彩闪烁、未开始=灰）+ "2/12 · 0:06" 计时。
  - 完成：绿色胶囊"✓ 生成完成 · 共 N 页 · 用时 M:SS"。
  - 已取消：灰胶囊"已暂停 · 完成 x/N 页" + "▶ 继续生成"按钮。
- 右侧：生成中显示"取消生成"（AlertDialog 确认）；"导出 ▾"（DropdownMenu：PPTX / PDF），全部完成前禁用。

**左栏（约 292px，ScrollArea）**：页面缩略图列表。每项 = 页码 + 标题 + 缩略图 + 状态角标：

| 页状态 | 视觉 |
|---|---|
| queued 排队中 | 灰底占位 + "排队中" |
| generating 生成中 | shimmer 骨架 + spinner 角标 |
| done 已完成 | 真实缩略图 + ✓ |
| editing 修改中 | 缩略图 + spinner，半透明遮罩 |
| failed 失败 | 红边 + "重试"按钮（仅重试该页） |

选中页高亮主色描边。**跟随规则**：默认自动跟随最新生成页；用户手动点击某页后固定选择、停止跟随；顶栏出现"回到最新"小按钮可恢复跟随。

**中栏**：16:9 大预览卡（生成中为骨架 + "AI 正在撰写本页内容…"）。下方工具条：左侧"版本 v{n} ▾"（DropdownMenu 列出最近版本，选择即切换）+ "↩ 撤销"（无上一版时禁用）；右侧"⟳ 重新生成本页"。预览图 URL 必须带 `?rev={revision}` 防缓存。

**右栏（约 416px）**：AI 修改助手。

- 头部：徽章"第 N 页 · 页标题"标明聊天对象（跟随选中页切换，聊天历史按页隔离）。
- 空态：居中图标 + "和我聊聊这一页" + 三条示例指令（点击即发送）。
- 消息流：用户消息右对齐胶囊；系统回执带状态（修改中 spinner → 完成/失败）。
- 底部：快捷指令胶囊（换个配色/精简文案/换个版式/换张配图）+ 输入框 + 发送。
- **未完成页**：允许输入，提示"本页尚未生成完成，稍等片刻即可编辑"，发送的指令进入待执行队列（§8.3）。

**运行详情抽屉**（demo 未画，必须补）：顶栏或右下角入口，`Sheet` 从底部/右侧滑出，流式展示原始 Agent/工具日志（等宽字体，按时间排列，可暂停滚动）。主界面任何区域都不出现原始日志。

---

## 6. 状态管理（Zustand）

### 6.1 `templates-store`

```text
templates: TemplateSummary[]
fetchTemplates(); uploadTemplate(file); retryParse(id)
applyTemplateEvent(evt)   // SSE template.* 事件更新解析进度/状态
```

### 6.2 `create-task-store`（已部分实现，保留）

主题、附件、选中模板、页数、比例；`createTask()` 返回 task_id。

### 6.3 `workbench-store`（核心，需重写）

```text
// 数据
task: TaskSnapshot | null          // 状态、阶段、计时起点、总页数
slides: Map<slide_id, SlideView>   // { id, index, title, status, previewUrl, revision, revisions[] }
slideOrder: string[]               // 渲染顺序
chats: Map<slide_id, ChatMessage[]>
pendingEdits: Map<slide_id, string[]>   // 未完成页的排队指令
logs: LogEntry[]                   // 运行详情抽屉
// UI
selectedSlideId: string | null
followLatest: boolean              // 默认 true
connection: 'connecting' | 'open' | 'reconnecting'
lastSeq: number
// actions
hydrate(taskId)                    // GET /tasks/:id + GET /tasks/:id/slides，刷新恢复
applyEvent(evt: GenerationEvent)   // 唯一入口：按 seq 去重后 reduce 到上述状态
selectSlide(id); cancelTask(); resumeTask(); exportTask(fmt)
sendChat(slideId, text, elementId?); undo(slideId); applyRevision(slideId, rev); retrySlide(slideId)
```

**规则**：

- `applyEvent` 必须幂等：`evt.seq <= lastSeq` 直接丢弃；所有状态变化只能由事件驱动，REST 响应只用于初始 hydrate 和乐观 UI。
- 事件 → 状态映射：`slide.started`→generating；`slide.preview_ready`→更新 previewUrl+revision（不改状态）；`slide.completed`→done；`slide.failed`→failed；`edit.*` 同理映射到 editing/done/failed；`task.*`/`stage.*` 更新顶栏。
- 页面标题等未知信息以事件 `message`/`payload` 为准，缺失时显示"第 N 页"。

---

## 7. SSE 客户端（`src/lib/sse.ts`，需新建）

```text
connectTaskEvents(taskId, lastSeq, onEvent, onStatusChange)
```

- 用原生 `EventSource`，URL：`/api/tasks/{taskId}/events?last_seq={n}`。
- `onerror` 时置 `connection='reconnecting'`（顶栏黄色小条"正在重连…"），EventSource 自动重连后服务端按 `Last-Event-ID`/`last_seq` 补发缺失事件；恢复后小条消失。**断线期间绝不把任务显示为失败**。
- 事件 `id` 字段即 `seq`；`message.data` 为 `GenerationEvent` JSON。
- 组件卸载时 `close()`；页面刷新后流程 = `hydrate()` 拿快照 → 用快照里的 `last_seq` 重连。

---

## 8. 关键交互规则（联调时逐条验收）

1. **骨架先行**：任务创建后左栏立即渲染 N 个占位页（N 来自 `task.created` 的 `total_slides`，未知时先渲染设置页数）。
2. **逐页替换**：收到 `slide.preview_ready` 立即替换对应骨架为缩略图（fadeUp），不等任务完成。
3. **页级聊天上下文**：右栏永远绑定 `selectedSlideId`，切页切历史。第 3 页的指令绝不能发给第 4 页——发送时携带发送瞬间的 slide_id，不读发送后的选中态。
4. **待执行队列**：对未完成页发送的指令入 `pendingEdits`，该页 `slide.completed` 后自动逐条发送，聊天区显示"已排队，页面完成后自动执行"。
5. **版本模型**（已定稿，两方案中选了"默认应用 + 撤销"）：每次修改成功即成为当前版；撤销 = `POST .../undo`（纯指针切换，不调模型，要求即时响应）；版本下拉可跳任意最近版本。
6. **单页失败隔离**：某页失败只在该页显示重试，其余页继续；重试调 `POST .../slides/{id}/retry`。
7. **导出**：点击后按钮进入"导出中"态（禁用 + spinner），`export.completed` 后 toast + 下载链接（`artifact_url`）。
8. **取消**：取消后已完成页保留、可继续查看和编辑；顶栏切"已暂停"态 + "继续生成"。
9. **刷新恢复**：刷新 `/workbench/:taskId` 后能恢复任务状态、全部已完成缩略图和聊天对象（聊天历史恢复为 P1，不阻塞）。
10. **防缓存**：所有预览图 URL 带 revision 参数。

---

## 9. Mock 策略（后端就绪前）

后端（FastAPI，另一组人开发）就绪前，前端必须能独立跑通全部交互：

- 新建 `src/mocks/mock-server.ts`：内存版 api + 假 SSE（用 `setInterval` 产出与 §10 契约完全一致的 `GenerationEvent` 序列，模拟：排队 → 逐页生成（每页 1.5–2.5s）→ 完成；随机让 1 页失败以测试重试）。
- 通过 `VITE_USE_MOCK=1` 环境变量切换 mock / 真实 `/api`，切换点收敛在 `src/lib/api.ts` 与 `src/lib/sse.ts` 内部，页面与 store 无感知。
- 假数据可复用 `src/mocks/slides.ts`、`templates.ts` 以及 demo 的 `../PPTAgent frontend design/pptagent-data.js`（8 个模板、deck 生成器可直接翻译成 TS）。

---

## 10. 后端契约（与后端组共同冻结于 `docs/api-contract.md`，此处为前端依赖的子集）

### 10.1 REST

| 方法 | 路径 | 用途 |
|---|---|---|
| POST | `/api/tasks` | 创建任务 → `{task_id}` |
| GET | `/api/tasks/{id}` | 任务快照（含 `last_seq`） |
| GET | `/api/tasks/{id}/events` | SSE，支持 `?last_seq=` |
| POST | `/api/tasks/{id}/cancel` | 取消 |
| POST | `/api/tasks/{id}/retry` | 恢复/重试 |
| GET | `/api/tasks/{id}/slides` | 全部页 + 当前版本 |
| POST | `/api/tasks/{id}/export` | 导出 |
| GET/POST/DELETE | `/api/templates` | 模板列表/上传/删除 |
| POST | `/api/tasks/{id}/slides/{sid}/chat` | 页级修改指令，可带 `element_id` |
| GET | `/api/tasks/{id}/slides/{sid}/revisions` | 版本列表 |
| POST | `/api/tasks/{id}/slides/{sid}/revisions/{rev}/apply` | 切换版本 |
| POST | `/api/tasks/{id}/slides/{sid}/undo` | 撤销 |
| POST | `/api/tasks/{id}/slides/{sid}/retry` | 重试失败页/失败修改 |

### 10.2 事件（`GenerationEvent`，字段见 `src/types/api.ts`，与后端 Pydantic 一一对应）

`task.created|started|completed|failed|cancelled`；
`template.parse_started|parse_progress|ready|failed`；
`stage.started|progress|completed`（stage ∈ template/plan/research/generate/edit/export）；
`slide.started|preview_ready|completed|failed`；
`edit.started|preview_ready|applied|failed|reverted`；
`export.started|completed|failed`。

**约定**：字段变更必须同步 `docs/api-contract.md` 与 `src/types/api.ts`，不靠口头。前端不解析自然语言日志推断进度，只消费上述结构化事件。

---

## 11. 验收标准（演示前逐条打勾）

- [ ] 不看日志即可知道当前阶段、正在生成第几页。
- [ ] 任一页完成后 1–2 秒内左栏出现预览。
- [ ] 页聊天对象始终明确，指令不串页。
- [ ] 刷新后恢复任务与已生成缩略图。
- [ ] SSE 断连重连不产生重复页、不丢完成事件、不误报失败。
- [ ] 单页失败只影响该页，可单独重试。
- [ ] 连续修改同一页 3 次 → 撤销 1 次 → 导出，其他页不受影响。
- [ ] 加载/空/失败/取消/完成五种状态均有明确视觉反馈；1280–1920px 宽度无布局破损。
- [ ] `npm run build` 与 `npm run lint` 通过。

## 12. 建议实现顺序

1. 主题 tokens + 字体替换（§4）→ 2. mock server + SSE 客户端（§7/§9）→ 3. workbench store 事件归并（§6.3）→ 4. 工作台三栏接通 mock 全流程（§5.3/§8）→ 5. 创建页/模板库接 store → 6. 运行详情抽屉、重连 UI、失败重试等收尾 → 7. 切真实 API 联调。

理由：工作台是唯一高复杂度页面，事件归并是唯一高复杂度逻辑，先在 mock 上把它做实，联调只剩字段核对。
