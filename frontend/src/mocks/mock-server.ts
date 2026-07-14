// In-memory mock backend. Emits `GenerationEvent` sequences identical to the
// API contract (§10 of FRONTEND_SPEC) so stores and pages are agnostic about
// whether events come from here or from the real FastAPI SSE endpoint.
// Task state is persisted to localStorage so a page refresh can hydrate.

import { buildDeck, revisedBullets, shortBullets } from "@/mocks/deck"
import type { DeckSlide } from "@/mocks/deck"
import { renderSlidePreview } from "@/mocks/slide-preview"
import { templates as seedTemplates } from "@/mocks/templates"
import type {
  AttachmentReceipt,
  CreateTaskPayload,
  ExportReceipt,
  GenerationEvent,
  SlideArtifact,
  SlideRevision,
  SlideStatus,
  TaskSnapshot,
  TaskStage,
  TaskStatus,
  TemplateSummary,
} from "@/types/api"

const STORAGE_KEY = "pptagent.mock.tasks.v1"
const SPEED = Number(import.meta.env.VITE_MOCK_SPEED ?? "1") || 1

type Listener = (event: GenerationEvent) => void

interface MockRevision {
  revision: number
  label: string
  created_at: string
  snapshot: DeckSlide
}

interface MockSlide {
  slide_id: string
  index: number
  deck: DeckSlide
  status: SlideStatus
  revisions: MockRevision[]
  currentRev: number
  retried: boolean
}

interface MockTask {
  id: string
  topic: string
  templateId: string
  ratio: "16:9" | "4:3"
  totalSlides: number
  status: TaskStatus
  stage: TaskStage
  created_at: string
  updated_at: string
  seq: number
  slides: MockSlide[]
  events: GenerationEvent[]
  listeners: Set<Listener>
  timers: ReturnType<typeof setTimeout>[]
}

const tasks = new Map<string, MockTask>()
let templates: TemplateSummary[] = structuredClone(seedTemplates)
const templateListeners = new Set<Listener>()
let templateSeq = 0
let seedParsingAnimated = false

const IMG_POOL = ["市场趋势图", "客户案例照片", "产品界面截图", "团队工作照"]

function now(): string {
  return new Date().toISOString()
}

function delay(ms: number): number {
  return ms / SPEED
}

function jitter(base: number, spread: number): number {
  return delay(base + Math.random() * spread)
}

function later(task: MockTask, fn: () => void, ms: number): void {
  task.timers.push(setTimeout(fn, ms))
}

function clearTimers(task: MockTask): void {
  task.timers.forEach(clearTimeout)
  task.timers = []
}

function previewOf(task: MockTask, slide: MockSlide, snapshot?: DeckSlide): string {
  const template = getTemplateOrFirst(task.templateId)
  return renderSlidePreview(
    snapshot ?? slide.deck,
    template.palette,
    task.ratio,
    `${slide.index} / ${task.totalSlides}`,
  )
}

function getTemplateOrFirst(templateId: string): TemplateSummary {
  return templates.find((item) => item.id === templateId) ?? templates[0]
}

// ---- persistence ----

function persist(): void {
  const payload = [...tasks.values()].slice(-5).map((task) => ({
    id: task.id,
    topic: task.topic,
    templateId: task.templateId,
    ratio: task.ratio,
    totalSlides: task.totalSlides,
    status: task.status,
    stage: task.stage,
    created_at: task.created_at,
    updated_at: task.updated_at,
    seq: task.seq,
    slides: task.slides.map((slide) => ({
      slide_id: slide.slide_id,
      index: slide.index,
      deck: slide.deck,
      status: slide.status,
      revisions: slide.revisions,
      currentRev: slide.currentRev,
      retried: slide.retried,
    })),
  }))
  localStorage.setItem(STORAGE_KEY, JSON.stringify(payload))
}

function restore(): void {
  const raw = localStorage.getItem(STORAGE_KEY)
  if (!raw) {
    return
  }
  const parsed = JSON.parse(raw) as Array<
    Omit<MockTask, "events" | "listeners" | "timers">
  >
  for (const stored of parsed) {
    const task: MockTask = {
      ...stored,
      events: [],
      listeners: new Set(),
      timers: [],
    }
    // A slide that was mid-generation when the page unloaded lost its timer.
    for (const slide of task.slides) {
      if (slide.status === "generating" || slide.status === "editing") {
        slide.status = slide.revisions.length ? "completed" : "queued"
      }
    }
    tasks.set(task.id, task)
    if (task.status === "running") {
      setTimeout(() => resumePipeline(task), delay(900))
    }
  }
}

// ---- event emission ----

function emit(task: MockTask, partial: Partial<GenerationEvent> & { type: string }): void {
  const event: GenerationEvent = {
    task_id: task.id,
    seq: ++task.seq,
    type: partial.type,
    stage: partial.stage ?? task.stage,
    status: partial.status ?? "running",
    progress: partial.progress ?? null,
    message: partial.message ?? "",
    slide_id: partial.slide_id ?? null,
    slide_index: partial.slide_index ?? null,
    total_slides: partial.total_slides ?? task.totalSlides,
    artifact_url: partial.artifact_url ?? null,
    created_at: now(),
    payload: partial.payload ?? {},
  }
  task.updated_at = event.created_at
  task.events.push(event)
  persist()
  for (const listener of task.listeners) {
    listener(event)
  }
}

function emitTemplate(partial: Partial<GenerationEvent> & { type: string }): void {
  const event: GenerationEvent = {
    task_id: "templates",
    seq: ++templateSeq,
    type: partial.type,
    stage: "template",
    status: partial.status ?? "running",
    progress: partial.progress ?? null,
    message: partial.message ?? "",
    slide_id: null,
    slide_index: null,
    total_slides: null,
    artifact_url: null,
    created_at: now(),
    payload: partial.payload ?? {},
  }
  for (const listener of templateListeners) {
    listener(event)
  }
}

// ---- generation pipeline ----

function failIndexOf(task: MockTask): number {
  return task.slides.length >= 7 ? 5 : -1
}

function runResearch(task: MockTask): void {
  task.stage = "research"
  emit(task, { type: "stage.started", stage: "research", message: "解析参考资料与主题背景" })
  later(task, () => {
    if (task.status !== "running") return
    emit(task, {
      type: "stage.completed",
      stage: "research",
      status: "succeeded",
      message: "资料解析完成",
    })
    runPlan(task)
  }, jitter(1500, 500))
}

function runPlan(task: MockTask): void {
  task.stage = "plan"
  emit(task, { type: "stage.started", stage: "plan", message: "规划大纲结构" })
  later(task, () => {
    if (task.status !== "running") return
    emit(task, {
      type: "stage.completed",
      stage: "plan",
      status: "succeeded",
      message: `大纲规划完成，共 ${task.totalSlides} 页`,
      payload: {
        outline: task.slides.map((slide) => ({
          slide_id: slide.slide_id,
          index: slide.index,
          title: slide.deck.title,
        })),
      },
    })
    task.stage = "generate"
    emit(task, { type: "stage.started", stage: "generate", message: "开始逐页生成内容" })
    generateSlide(task, 0)
  }, jitter(1400, 600))
}

function generateSlide(task: MockTask, index: number): void {
  if (task.status !== "running") return
  if (index >= task.slides.length) {
    finishTask(task)
    return
  }
  const slide = task.slides[index]
  if (slide.status === "completed" || slide.status === "failed") {
    generateSlide(task, index + 1)
    return
  }
  slide.status = "generating"
  emit(task, {
    type: "slide.started",
    slide_id: slide.slide_id,
    slide_index: slide.index,
    message: slide.deck.title,
    payload: { title: slide.deck.title },
  })
  const willFail = index === failIndexOf(task) && !slide.retried
  later(task, () => {
    if (task.status !== "running") return
    if (willFail) {
      slide.status = "failed"
      emit(task, {
        type: "slide.failed",
        slide_id: slide.slide_id,
        slide_index: slide.index,
        status: "failed",
        message: "内容引擎响应超时，不影响其他页面",
      })
      generateSlide(task, index + 1)
      return
    }
    emit(task, {
      type: "slide.preview_ready",
      slide_id: slide.slide_id,
      slide_index: slide.index,
      artifact_url: previewOf(task, slide),
      message: `第 ${slide.index} 页预览已就绪`,
      payload: { revision: 1 },
    })
    later(task, () => {
      if (task.status !== "running") return
      completeSlide(task, slide, "初稿")
      generateSlide(task, index + 1)
    }, jitter(400, 400))
  }, jitter(1100, 700))
}

function completeSlide(task: MockTask, slide: MockSlide, label: string): void {
  slide.status = "completed"
  if (!slide.revisions.length) {
    slide.revisions = [
      { revision: 1, label, created_at: now(), snapshot: structuredClone(slide.deck) },
    ]
    slide.currentRev = 0
  }
  emit(task, {
    type: "slide.completed",
    slide_id: slide.slide_id,
    slide_index: slide.index,
    status: "succeeded",
    message: `第 ${slide.index} 页生成完成`,
    artifact_url: previewOf(task, slide),
    payload: {
      title: slide.deck.title,
      revision: slide.currentRev + 1,
      label: slide.revisions[slide.currentRev]?.label ?? label,
    },
  })
}

function finishTask(task: MockTask): void {
  const failed = task.slides.filter((slide) => slide.status === "failed").length
  task.status = "completed"
  task.stage = "export"
  emit(task, {
    type: "stage.completed",
    stage: "generate",
    status: "succeeded",
    message: "逐页生成完成",
  })
  emit(task, {
    type: "task.completed",
    status: "succeeded",
    message: failed
      ? `生成完成，${failed} 页失败可单独重试`
      : "全部页面生成完成",
  })
}

function resumePipeline(task: MockTask): void {
  task.status = "running"
  if (task.stage === "research") {
    runResearch(task)
    return
  }
  if (task.stage === "plan") {
    runPlan(task)
    return
  }
  task.stage = "generate"
  const next = task.slides.findIndex(
    (slide) => slide.status === "queued" || slide.status === "generating",
  )
  if (next < 0) {
    finishTask(task)
    return
  }
  generateSlide(task, next)
}

// ---- chat edits ----

type EditKind = "color" | "layout" | "concise" | "image" | "revise"

function detectKind(text: string): EditKind {
  if (/配色|颜色|色彩|换.?色/.test(text)) return "color"
  if (/版式|布局|排版/.test(text)) return "layout"
  if (/精简|简洁|压缩|短一点|简短|文案/.test(text)) return "concise"
  if (/配图|图片|素材|换.?图/.test(text)) return "image"
  return "revise"
}

function layoutName(layout: string): string {
  const names: Record<string, string> = {
    cover: "封面",
    toc: "目录",
    section: "章节页",
    bullets: "标题+要点",
    split: "左文右图",
    chart: "数据看板",
    compare: "双栏对比",
    timeline: "时间线",
    quote: "引言页",
    end: "封底",
  }
  return names[layout] ?? layout
}

function applyEdit(slide: MockSlide, kind: EditKind): { reply: string; action: string } {
  const deck = slide.deck
  if (kind === "color") {
    deck.accentIdx = (deck.accentIdx + 1) % 3
    return {
      reply: "已为本页更换强调色，标题装饰、要点符号与图表均已同步更新。想换回来随时告诉我。",
      action: "更换配色",
    }
  }
  if (kind === "layout") {
    if (!["bullets", "split", "compare"].includes(deck.layout)) {
      return {
        reply: `这一页是${layoutName(deck.layout)}版式，结构比较特殊，建议保持现有版式。可以试试「换个配色」或「精简文案」。`,
        action: "",
      }
    }
    if (deck.layout === "bullets") {
      deck.layout = "split"
      deck.img = deck.img ?? "示意配图"
    } else if (deck.layout === "split") {
      deck.layout = "bullets"
    } else {
      deck.layout = "bullets"
      deck.bullets = [...(deck.left?.items ?? []), ...(deck.right?.items ?? [])].slice(0, 4)
    }
    return {
      reply: `已将本页调整为「${layoutName(deck.layout)}」版式，内容已自动重排。不满意可点击撤销。`,
      action: "调整版式",
    }
  }
  if (kind === "concise") {
    if (!deck.bullets) {
      return {
        reply: "本页没有可精简的正文要点。可以让我「换个配色」或「重新生成本页」。",
        action: "",
      }
    }
    deck.bullets = shortBullets(deck.bullets)
    return {
      reply: "已精简本页文案：每条要点压缩到一行以内，只保留核心结论。",
      action: "精简文案",
    }
  }
  if (kind === "image") {
    if (deck.layout === "split") {
      const next = IMG_POOL[(IMG_POOL.indexOf(deck.img ?? "") + 1) % IMG_POOL.length]
      deck.img = next
      return {
        reply: `已把配图建议更换为「${next}」，导出时会按此重新检索素材。`,
        action: "更换配图",
      }
    }
    if (deck.layout === "bullets") {
      deck.layout = "split"
      deck.img = IMG_POOL[0]
      return {
        reply: `已为本页加入配图区域，并切换为左文右图版式，配图建议为「${IMG_POOL[0]}」。`,
        action: "添加配图",
      }
    }
    return { reply: "这一页的版式暂不适合插入配图。可以选中一个内容页再试试。", action: "" }
  }
  if (deck.bullets) {
    deck.bullets = revisedBullets(deck.bullets)
    return {
      reply: "已根据你的要求修订本页表述：调整了信息顺序与详略，让重点更靠前。可以继续告诉我想强调什么。",
      action: "内容修订",
    }
  }
  return {
    reply: "这一页没有可直接改写的正文段落。可以换个配色，或让我重新生成本页。",
    action: "",
  }
}

// ---- public API (mirrors REST contract) ----

function requireTask(taskId: string): MockTask {
  const task = tasks.get(taskId)
  if (!task) {
    throw new Error(`Mock task not found: ${taskId}`)
  }
  return task
}

function requireSlide(task: MockTask, slideId: string): MockSlide {
  const slide = task.slides.find((item) => item.slide_id === slideId)
  if (!slide) {
    throw new Error(`Mock slide not found: ${slideId}`)
  }
  return slide
}

function snapshotOf(task: MockTask): TaskSnapshot {
  const done = task.slides.filter((slide) => slide.status === "completed").length
  return {
    task_id: task.id,
    topic: task.topic,
    status: task.status,
    stage: task.stage,
    progress: task.totalSlides ? Math.round((done / task.totalSlides) * 100) : 0,
    template_id: task.templateId,
    ratio: task.ratio,
    total_slides: task.totalSlides,
    last_seq: task.seq,
    created_at: task.created_at,
    updated_at: task.updated_at,
  }
}

function artifactOf(task: MockTask, slide: MockSlide): SlideArtifact {
  const done = slide.status === "completed" || slide.status === "editing"
  return {
    slide_id: slide.slide_id,
    task_id: task.id,
    index: slide.index,
    title: slide.deck.title,
    summary: slide.deck.subtitle ?? "",
    status: slide.status,
    mode: "html",
    revision: slide.revisions.length ? slide.currentRev + 1 : 0,
    preview_url: done || slide.revisions.length ? previewOf(task, slide) : null,
  }
}

export function mockUploadAttachment(file: File): AttachmentReceipt {
  return { attachment_id: `att-${Date.now().toString(36)}-${file.size.toString(36)}` }
}

export function mockCreateTask(payload: CreateTaskPayload): TaskSnapshot {
  const id = `t${Date.now().toString(36)}`
  const deck = buildDeck(payload.topic, payload.page_count)
  const task: MockTask = {
    id,
    topic: payload.topic,
    templateId: payload.template_id,
    ratio: payload.ratio,
    totalSlides: deck.length,
    status: "running",
    stage: "research",
    created_at: now(),
    updated_at: now(),
    seq: 0,
    slides: deck.map((slide, index) => ({
      slide_id: `${id}-s${index + 1}`,
      index: index + 1,
      deck: slide,
      status: "queued",
      revisions: [],
      currentRev: -1,
      retried: false,
    })),
    events: [],
    listeners: new Set(),
    timers: [],
  }
  tasks.set(id, task)
  emit(task, {
    type: "task.created",
    message: payload.topic,
    payload: { template_id: payload.template_id, ratio: payload.ratio },
  })
  emit(task, { type: "task.started", message: "任务开始执行" })
  later(task, () => runResearch(task), delay(500))
  return snapshotOf(task)
}

export function mockGetTask(taskId: string): TaskSnapshot {
  return snapshotOf(requireTask(taskId))
}

export function mockListSlides(taskId: string): SlideArtifact[] {
  const task = requireTask(taskId)
  return task.slides.map((slide) => artifactOf(task, slide))
}

export function mockCancelTask(taskId: string): TaskSnapshot {
  const task = requireTask(taskId)
  clearTimers(task)
  task.status = "cancelled"
  emit(task, {
    type: "task.cancelled",
    status: "cancelled",
    message: "已暂停生成，已完成的页面可继续查看和编辑",
  })
  return snapshotOf(task)
}

export function mockResumeTask(taskId: string): TaskSnapshot {
  const task = requireTask(taskId)
  emit(task, { type: "task.started", message: "继续生成" })
  resumePipeline(task)
  return snapshotOf(task)
}

export function mockRetrySlide(taskId: string, slideId: string): void {
  const task = requireTask(taskId)
  const slide = requireSlide(task, slideId)
  const isRegen = slide.status === "completed"
  slide.retried = true
  slide.status = "generating"
  emit(task, {
    type: "slide.started",
    slide_id: slide.slide_id,
    slide_index: slide.index,
    message: isRegen ? `重新生成第 ${slide.index} 页` : `重试第 ${slide.index} 页`,
    payload: { title: slide.deck.title },
  })
  later(task, () => {
    if (isRegen) {
      slide.deck.bullets = revisedBullets(slide.deck.bullets)
      slide.revisions = [
        ...slide.revisions.slice(0, slide.currentRev + 1),
        {
          revision: slide.revisions.length + 1,
          label: "重新生成",
          created_at: now(),
          snapshot: structuredClone(slide.deck),
        },
      ]
      slide.currentRev = slide.revisions.length - 1
    }
    emit(task, {
      type: "slide.preview_ready",
      slide_id: slide.slide_id,
      slide_index: slide.index,
      artifact_url: previewOf(task, slide),
      message: `第 ${slide.index} 页预览已就绪`,
      payload: { revision: Math.max(slide.currentRev + 1, 1) },
    })
    completeSlide(task, slide, isRegen ? "重新生成" : "初稿")
  }, jitter(1300, 500))
}

export function mockSendChat(
  taskId: string,
  slideId: string,
  text: string,
): { chat_id: string } {
  const task = requireTask(taskId)
  const slide = requireSlide(task, slideId)
  const chatId = `c${Date.now().toString(36)}${Math.floor(Math.random() * 1e3)}`
  slide.status = "editing"
  emit(task, {
    type: "edit.started",
    stage: "edit",
    slide_id: slide.slide_id,
    slide_index: slide.index,
    message: text,
    payload: { chat_id: chatId },
  })
  later(task, () => {
    const { reply, action } = applyEdit(slide, detectKind(text))
    if (action) {
      slide.revisions = [
        ...slide.revisions.slice(0, slide.currentRev + 1),
        {
          revision: slide.currentRev + 2,
          label: action,
          created_at: now(),
          snapshot: structuredClone(slide.deck),
        },
      ]
      slide.currentRev = slide.revisions.length - 1
      emit(task, {
        type: "edit.preview_ready",
        stage: "edit",
        slide_id: slide.slide_id,
        slide_index: slide.index,
        artifact_url: previewOf(task, slide),
        payload: { chat_id: chatId, revision: slide.currentRev + 1 },
      })
    }
    slide.status = "completed"
    emit(task, {
      type: "edit.applied",
      stage: "edit",
      status: "succeeded",
      slide_id: slide.slide_id,
      slide_index: slide.index,
      artifact_url: action ? previewOf(task, slide) : null,
      message: reply,
      payload: {
        chat_id: chatId,
        action,
        revision: slide.currentRev + 1,
        label: action || undefined,
      },
    })
  }, jitter(1100, 600))
  return { chat_id: chatId }
}

export function mockListRevisions(taskId: string, slideId: string): SlideRevision[] {
  const task = requireTask(taskId)
  const slide = requireSlide(task, slideId)
  return slide.revisions.map((revision) => ({
    revision: revision.revision,
    label: revision.label,
    created_at: revision.created_at,
    preview_url: previewOf(task, slide, revision.snapshot),
  }))
}

function switchRevision(task: MockTask, slide: MockSlide, index: number, verb: string): void {
  const revision = slide.revisions[index]
  slide.deck = structuredClone(revision.snapshot)
  slide.currentRev = index
  emit(task, {
    type: "edit.reverted",
    stage: "edit",
    status: "succeeded",
    slide_id: slide.slide_id,
    slide_index: slide.index,
    artifact_url: previewOf(task, slide),
    message: `${verb}到 ${revision.label}（v${revision.revision}）`,
    payload: { revision: revision.revision, label: revision.label },
  })
}

export function mockUndo(taskId: string, slideId: string): void {
  const task = requireTask(taskId)
  const slide = requireSlide(task, slideId)
  if (slide.currentRev <= 0) {
    return
  }
  switchRevision(task, slide, slide.currentRev - 1, "已撤销")
}

export function mockApplyRevision(taskId: string, slideId: string, revision: number): void {
  const task = requireTask(taskId)
  const slide = requireSlide(task, slideId)
  const index = slide.revisions.findIndex((item) => item.revision === revision)
  if (index < 0 || index === slide.currentRev) {
    return
  }
  switchRevision(task, slide, index, "已切换")
}

export function mockExportTask(taskId: string, format: "pptx" | "pdf"): ExportReceipt {
  const task = requireTask(taskId)
  const exportId = `exp-${Date.now().toString(36)}`
  emit(task, {
    type: "export.started",
    stage: "export",
    message: `正在导出 ${format.toUpperCase()}`,
    payload: { format, export_id: exportId },
  })
  later(task, () => {
    const filename = `${task.topic.slice(0, 20) || "presentation"}.${format}`
    const blob = new Blob([`PPTAgent mock export: ${task.topic}`], {
      type: "application/octet-stream",
    })
    emit(task, {
      type: "export.completed",
      stage: "export",
      status: "succeeded",
      artifact_url: URL.createObjectURL(blob),
      message: `${format.toUpperCase()} 导出完成`,
      payload: { format, filename, export_id: exportId },
    })
  }, jitter(1500, 600))
  return { export_id: exportId }
}

export function mockSubscribeTask(
  taskId: string,
  afterSeq: number,
  onEvent: Listener,
): () => void {
  const task = requireTask(taskId)
  for (const event of task.events) {
    if (event.seq > afterSeq) {
      onEvent(event)
    }
  }
  task.listeners.add(onEvent)
  return () => task.listeners.delete(onEvent)
}

// ---- templates ----

function animateParse(templateId: string, from: number): void {
  let progress = from
  const tick = (): void => {
    const template = templates.find((item) => item.id === templateId)
    if (!template) {
      return
    }
    progress += 4 + Math.random() * 9
    if (progress >= 100) {
      template.status = "ready"
      template.progress = 100
      template.error = null
      emitTemplate({
        type: "template.ready",
        status: "succeeded",
        message: `模板「${template.name}」解析完成`,
        payload: { template_id: templateId },
      })
    } else {
      template.status = "parsing"
      template.progress = Math.round(progress)
      emitTemplate({
        type: "template.parse_progress",
        progress: template.progress,
        message: `解析中 ${template.progress}%`,
        payload: { template_id: templateId, progress: template.progress },
      })
      setTimeout(tick, delay(400))
    }
  }
  setTimeout(tick, delay(400))
}

export function mockListTemplates(): TemplateSummary[] {
  if (!seedParsingAnimated) {
    seedParsingAnimated = true
    for (const template of templates) {
      if (template.status === "parsing") {
        animateParse(template.id, template.progress ?? 0)
      }
    }
  }
  return structuredClone(templates)
}

export function mockUploadTemplate(file: File): TemplateSummary {
  const id = `up${Date.now().toString(36)}`
  const name = file.name.replace(/\.pptx?$/i, "") || "我的模板"
  const template: TemplateSummary = {
    id,
    name,
    description: "来自本地上传的 PPTX 文件，正在解析母版、版式与配色",
    owner: "user",
    status: "parsing",
    progress: 3,
    slides: 16,
    ratio: "16:9",
    layouts: ["封面", "目录", "标题 + 要点", "左文右图", "封底"],
    palette: {
      bg: "#F2F1EE",
      surface: "#FFFFFF",
      primary: "#55504A",
      accent: "#A9A399",
      ink: "#33302B",
      dark: false,
    },
  }
  templates = [template, ...templates]
  emitTemplate({
    type: "template.parse_started",
    message: `开始解析「${file.name}」`,
    payload: { template_id: id },
  })
  animateParse(id, 3)
  return structuredClone(template)
}

export function mockRetryParse(templateId: string): void {
  const template = templates.find((item) => item.id === templateId)
  if (!template) {
    return
  }
  template.status = "parsing"
  template.progress = 8
  template.error = null
  emitTemplate({
    type: "template.parse_started",
    message: `重新解析「${template.name}」`,
    payload: { template_id: templateId },
  })
  animateParse(templateId, 8)
}

export function mockDeleteTemplate(templateId: string): void {
  templates = templates.filter((item) => item.id !== templateId)
}

export function mockSubscribeTemplates(onEvent: Listener): () => void {
  templateListeners.add(onEvent)
  return () => templateListeners.delete(onEvent)
}

restore()
