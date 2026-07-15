import { toast } from "sonner"
import { create } from "zustand"

import { api } from "@/lib/api"
import type { ConnectionState } from "@/lib/sse"
import type {
  GenerationEvent,
  SlideRevision,
  SlideStatus,
  TaskSnapshot,
} from "@/types/api"

export interface SlideView {
  id: string
  index: number
  title: string
  status: SlideStatus
  previewUrl: string | null
  revision: number
  revisions: SlideRevision[]
}

export interface WorkChatMessage {
  id: string
  role: "user" | "system"
  content: string
  status: "sent" | "queued" | "pending" | "applied" | "failed" | "info"
  action?: string
  chatId?: string
}

export interface LogEntry {
  seq: number
  time: string
  type: string
  message: string
}

interface WorkbenchState {
  taskId: string | null
  task: TaskSnapshot | null
  slides: Map<string, SlideView>
  slideOrder: string[]
  chats: Map<string, WorkChatMessage[]>
  pendingEdits: Map<string, string[]>
  logs: LogEntry[]
  selectedSlideId: string | null
  followLatest: boolean
  connection: ConnectionState
  lastSeq: number
  exporting: boolean
  hydrated: boolean
  loadError: string | null

  fallbackTotal: number
  hydrate: (taskId: string, fallbackTotal?: number) => Promise<void>
  applyEvent: (event: GenerationEvent) => void
  setConnection: (state: ConnectionState) => void
  selectSlide: (slideId: string) => void
  resumeFollow: () => void
  cancelTask: () => Promise<void>
  resumeTask: () => Promise<void>
  exportTask: (format: "pptx" | "pdf") => Promise<void>
  sendChat: (slideId: string, text: string, elementId?: string) => Promise<void>
  undo: (slideId: string) => Promise<void>
  applyRevision: (slideId: string, revision: number) => Promise<void>
  retrySlide: (slideId: string) => Promise<void>
  loadRevisions: (slideId: string) => Promise<void>
}

function placeholderId(index: number): string {
  return `pending-${index}`
}

function timeOf(iso: string): string {
  const date = new Date(iso)
  const pad = (value: number): string => String(value).padStart(2, "0")
  return `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`
}

function chatMessageId(): string {
  return crypto.randomUUID()
}

// Export downloads navigate the browser; only same-origin paths and
// http(s)/blob URLs are trusted, never javascript:/data: or //host.
function safeDownloadUrl(url: string | null): string | null {
  if (!url) {
    return null
  }
  if (/^(https?:|blob:)/i.test(url)) {
    return url
  }
  return url.startsWith("/") && !url.startsWith("//") ? url : null
}

export const useWorkbenchStore = create<WorkbenchState>((set, get) => {
  function patchSlide(slideId: string, patch: Partial<SlideView>): void {
    set((state) => {
      const slide = state.slides.get(slideId)
      if (!slide) {
        return state
      }
      const slides = new Map(state.slides)
      slides.set(slideId, { ...slide, ...patch })
      return { slides }
    })
  }

  function patchChat(
    slideId: string,
    messageId: string,
    patch: Partial<WorkChatMessage>,
  ): void {
    set((state) => {
      const messages = state.chats.get(slideId)
      if (!messages) {
        return state
      }
      const chats = new Map(state.chats)
      chats.set(
        slideId,
        messages.map((message) =>
          message.id === messageId ? { ...message, ...patch } : message,
        ),
      )
      return { chats }
    })
  }

  function pushChat(slideId: string, message: WorkChatMessage): void {
    set((state) => {
      const chats = new Map(state.chats)
      chats.set(slideId, [...(state.chats.get(slideId) ?? []), message])
      return { chats }
    })
  }

  // Per-slide epoch, bumped by every SSE-driven revision mutation. A GET has
  // no watermark, so a response that raced such a mutation (a same-number
  // revision replaced from an earlier base, a pruned revision resurrected)
  // cannot be merged — it is discarded wholesale. A response whose epoch is
  // unchanged reflects server state at-or-after the request, and the server
  // had already applied everything SSE delivered before it, so replacing the
  // list is safe.
  let revisionEpochs = new Map<string, number>()

  function bumpRevisionEpoch(slideId: string): void {
    revisionEpochs.set(slideId, (revisionEpochs.get(slideId) ?? 0) + 1)
  }

  // Returns null when the response is stale: the task switched or the
  // slide's revisions changed while the request was in flight.
  async function fetchRevisions(slideId: string): Promise<SlideRevision[] | null> {
    const { taskId } = get()
    if (!taskId) {
      return null
    }
    const epoch = revisionEpochs.get(slideId) ?? 0
    const fetched = await api.listRevisions(taskId, slideId)
    const fresh =
      get().taskId === taskId && (revisionEpochs.get(slideId) ?? 0) === epoch
    return fresh ? fetched : null
  }

  // Slide mutations are serialized per slide: the optimistic status keeps
  // the UI (undo, version menu, regenerate, export) locked through the whole
  // REST→SSE window — not just until the HTTP response — and a failed
  // request rolls the status back.
  const inflightOps = new Set<string>()

  async function mutateSlide(
    key: string,
    slideId: string,
    optimisticStatus: SlideStatus,
    errorMessage: string,
    run: () => Promise<unknown>,
  ): Promise<void> {
    if (inflightOps.has(key)) {
      return
    }
    const previousStatus = get().slides.get(slideId)?.status
    if (!previousStatus) {
      return
    }
    inflightOps.add(key)
    patchSlide(slideId, { status: optimisticStatus })
    try {
      await run()
    } catch {
      patchSlide(slideId, { status: previousStatus })
      toast.error(errorMessage)
    } finally {
      inflightOps.delete(key)
    }
  }

  // Ensure a slide entry exists for an incoming event; replaces the
  // index-based placeholder created from `task.created` when the real
  // slide_id first shows up.
  function upsertSlide(slideId: string, index: number | null, title?: string): void {
    set((state) => {
      if (state.slides.has(slideId)) {
        return state
      }
      const slides = new Map(state.slides)
      let slideOrder = state.slideOrder
      let chats = state.chats
      let pendingEdits = state.pendingEdits
      const pendingId = index !== null ? placeholderId(index) : null
      if (pendingId && slides.has(pendingId)) {
        const placeholder = slides.get(pendingId)!
        slides.delete(pendingId)
        slides.set(slideId, { ...placeholder, id: slideId, title: title ?? placeholder.title })
        slideOrder = slideOrder.map((id) => (id === pendingId ? slideId : id))
        // Chats and queued edits keyed on the placeholder must follow the
        // real slide id, or queued instructions are silently dropped.
        if (chats.has(pendingId)) {
          chats = new Map(chats)
          chats.set(slideId, chats.get(pendingId)!)
          chats.delete(pendingId)
        }
        if (pendingEdits.has(pendingId)) {
          pendingEdits = new Map(pendingEdits)
          pendingEdits.set(slideId, pendingEdits.get(pendingId)!)
          pendingEdits.delete(pendingId)
        }
      } else {
        slides.set(slideId, {
          id: slideId,
          index: index ?? slides.size + 1,
          title: title ?? `第 ${index ?? slides.size + 1} 页`,
          status: "queued",
          previewUrl: null,
          revision: 0,
          revisions: [],
        })
        slideOrder = [...slideOrder, slideId]
      }
      const selectedSlideId =
        state.selectedSlideId === pendingId ? slideId : state.selectedSlideId
      return { slides, slideOrder, selectedSlideId, chats, pendingEdits }
    })
  }

  function flushPendingEdits(slideId: string): void {
    const state = get()
    const queue = state.pendingEdits.get(slideId)
    if (!queue?.length) {
      return
    }
    const slide = state.slides.get(slideId)
    if (!slide || slide.status !== "completed") {
      return
    }
    const [next, ...rest] = queue
    set((current) => {
      const pendingEdits = new Map(current.pendingEdits)
      if (rest.length) {
        pendingEdits.set(slideId, rest)
      } else {
        pendingEdits.delete(slideId)
      }
      return { pendingEdits }
    })
    // Promote the queued user bubble to "sent" before dispatching.
    const queuedMessage = (state.chats.get(slideId) ?? []).find(
      (message) => message.role === "user" && message.status === "queued" && message.content === next,
    )
    if (queuedMessage) {
      patchChat(slideId, queuedMessage.id, { status: "sent" })
    }
    void dispatchChat(slideId, next)
  }

  async function dispatchChat(
    slideId: string,
    text: string,
    elementId?: string,
  ): Promise<void> {
    const { taskId } = get()
    if (!taskId) {
      return
    }
    const receiptId = chatMessageId()
    pushChat(slideId, {
      id: receiptId,
      role: "system",
      content: "正在修改本页…",
      status: "pending",
    })
    // Optimistically mark the slide as editing so undo / version switch /
    // export stay locked through the REST→SSE window; edit.started then
    // confirms it server-side.
    const previousStatus = get().slides.get(slideId)?.status
    patchSlide(slideId, { status: "editing" })
    try {
      const { chat_id } = await api.sendChat(taskId, slideId, text, elementId)
      patchChat(slideId, receiptId, { chatId: chat_id })
    } catch {
      if (previousStatus) {
        patchSlide(slideId, { status: previousStatus })
      }
      patchChat(slideId, receiptId, { status: "failed", content: "指令发送失败，请重试" })
      // Fail-stop: the server may have received the request even though the
      // HTTP receipt was lost, so dispatching the next queued edit here
      // could mis-bind its result. The queue resumes on this slide's next
      // terminal edit event, or when the user sends another instruction.
    }
  }

  // Guards hydrate() against out-of-order responses when the user switches
  // tasks quickly: only the most recent call may write to the store.
  let hydrateGeneration = 0

  return {
    taskId: null,
    task: null,
    slides: new Map(),
    slideOrder: [],
    chats: new Map(),
    pendingEdits: new Map(),
    logs: [],
    selectedSlideId: null,
    followLatest: true,
    connection: "connecting",
    lastSeq: 0,
    exporting: false,
    hydrated: false,
    loadError: null,
    fallbackTotal: 0,

    async hydrate(taskId, fallbackTotal = 0) {
      const generation = ++hydrateGeneration
      revisionEpochs = new Map()
      set({
        taskId,
        task: null,
        slides: new Map(),
        slideOrder: [],
        chats: new Map(),
        pendingEdits: new Map(),
        logs: [],
        selectedSlideId: null,
        followLatest: true,
        connection: "connecting",
        lastSeq: 0,
        exporting: false,
        hydrated: false,
        loadError: null,
        fallbackTotal,
      })
      try {
        // Sequential on purpose: `task.last_seq` is the SSE replay watermark,
        // so the slides snapshot must be taken at-or-after it. Events between
        // the two snapshots are then replayed by SSE and re-applied
        // idempotently, instead of falling into a gap neither side covers.
        const task = await api.getTask(taskId)
        const artifacts = await api.listSlides(taskId)
        if (generation !== hydrateGeneration) {
          return
        }
        const slides = new Map<string, SlideView>()
        const slideOrder: string[] = []
        for (const artifact of [...artifacts].sort((a, b) => a.index - b.index)) {
          slides.set(artifact.slide_id, {
            id: artifact.slide_id,
            index: artifact.index,
            title: artifact.title || `第 ${artifact.index} 页`,
            status: artifact.status,
            previewUrl: artifact.preview_url,
            revision: artifact.revision,
            revisions: [],
          })
          slideOrder.push(artifact.slide_id)
        }
        // §8.1 skeleton first: when the slide list is not known yet, render
        // placeholders from the snapshot's total_slides, falling back to the
        // page count configured on the create page.
        const plannedTotal = task.total_slides > 0 ? task.total_slides : fallbackTotal
        if (slides.size === 0 && plannedTotal > 0) {
          for (let index = 1; index <= plannedTotal; index++) {
            const id = placeholderId(index)
            slides.set(id, {
              id,
              index,
              title: `第 ${index} 页`,
              status: "queued",
              previewUrl: null,
              revision: 0,
              revisions: [],
            })
            slideOrder.push(id)
          }
        }
        const active = [...slides.values()].find(
          (slide) => slide.status === "generating" || slide.status === "editing",
        )
        const selectedSlideId = active?.id ?? slideOrder[0] ?? null
        set({
          task,
          slides,
          slideOrder,
          selectedSlideId,
          followLatest: task.status === "running",
          lastSeq: task.last_seq,
          hydrated: true,
        })
      } catch {
        if (generation !== hydrateGeneration) {
          return
        }
        set({ hydrated: true, loadError: "任务不存在或已过期" })
      }
    },

    applyEvent(event) {
      const state = get()
      // Reject events from a previous task's stream and stale replays.
      if (event.task_id !== state.taskId || event.seq <= state.lastSeq) {
        return
      }
      set((current) => ({
        lastSeq: event.seq,
        logs: [
          ...current.logs.slice(-499),
          {
            seq: event.seq,
            time: timeOf(event.created_at),
            type: event.type,
            message: event.message,
          },
        ],
      }))

      const patchTask = (patch: Partial<TaskSnapshot>): void => {
        set((current) =>
          current.task
            ? { task: { ...current.task, ...patch, updated_at: event.created_at } }
            : current,
        )
      }

      switch (event.type) {
        case "task.created": {
          // §8.1: fall back to the configured page count until the backend
          // reports the real total.
          const total = event.total_slides ?? 0
          const planned = total > 0 ? total : get().fallbackTotal
          patchTask({ status: "running", total_slides: total || planned })
          if (get().slides.size === 0 && planned > 0) {
            const slides = new Map<string, SlideView>()
            const slideOrder: string[] = []
            for (let index = 1; index <= planned; index++) {
              const id = placeholderId(index)
              slides.set(id, {
                id,
                index,
                title: `第 ${index} 页`,
                status: "queued",
                previewUrl: null,
                revision: 0,
                revisions: [],
              })
              slideOrder.push(id)
            }
            set({ slides, slideOrder, selectedSlideId: slideOrder[0] ?? null })
          }
          break
        }
        case "task.started":
          patchTask({ status: "running" })
          break
        case "task.completed":
          patchTask({ status: "completed", progress: 100 })
          break
        case "task.failed":
          patchTask({ status: "failed" })
          break
        case "task.cancelled": {
          patchTask({ status: "cancelled" })
          set((current) => {
            const slides = new Map(current.slides)
            for (const [id, slide] of slides) {
              if (slide.status === "generating") {
                slides.set(id, { ...slide, status: "queued" })
              }
            }
            return { slides, followLatest: false }
          })
          break
        }
        case "stage.started":
        case "stage.progress":
          patchTask({ stage: event.stage })
          break
        case "stage.completed": {
          const outline = event.payload.outline as
            | Array<{ slide_id: string; index: number; title: string }>
            | undefined
          if (outline) {
            for (const item of outline) {
              upsertSlide(item.slide_id, item.index, item.title)
              patchSlide(item.slide_id, { title: item.title })
            }
            // The final outline is authoritative: drop leftover index
            // placeholders when it has fewer pages than requested, or they
            // stay "queued" forever and block export.
            set((current) => {
              const stale = current.slideOrder.filter((id) => id.startsWith("pending-"))
              if (!stale.length) {
                return current
              }
              const slides = new Map(current.slides)
              for (const id of stale) {
                slides.delete(id)
              }
              const slideOrder = current.slideOrder.filter((id) => slides.has(id))
              const selectedSlideId =
                current.selectedSlideId && slides.has(current.selectedSlideId)
                  ? current.selectedSlideId
                  : (slideOrder[0] ?? null)
              return { slides, slideOrder, selectedSlideId }
            })
            patchTask({ total_slides: outline.length })
          }
          break
        }
        case "slide.started": {
          if (!event.slide_id) {
            break
          }
          upsertSlide(event.slide_id, event.slide_index, event.payload.title as string)
          patchSlide(event.slide_id, { status: "generating" })
          if (get().followLatest) {
            set({ selectedSlideId: event.slide_id })
          }
          break
        }
        case "slide.preview_ready":
        case "edit.preview_ready": {
          if (!event.slide_id) {
            break
          }
          // Spreading `previewUrl: undefined` would erase an existing
          // preview, so only include fields the event actually carries.
          const patch: Partial<SlideView> = {}
          if (event.artifact_url) {
            patch.previewUrl = event.artifact_url
          }
          if (typeof event.payload.revision === "number") {
            patch.revision = event.payload.revision
          }
          patchSlide(event.slide_id, patch)
          break
        }
        case "slide.completed": {
          if (!event.slide_id) {
            break
          }
          bumpRevisionEpoch(event.slide_id)
          const revision = (event.payload.revision as number) ?? 1
          const label = (event.payload.label as string) ?? "初稿"
          const slide = get().slides.get(event.slide_id)
          const revisions =
            slide && !slide.revisions.some((item) => item.revision === revision)
              ? [
                  ...slide.revisions,
                  { revision, label, created_at: event.created_at, preview_url: event.artifact_url },
                ]
              : slide?.revisions
          const patch: Partial<SlideView> = { status: "completed", revision, revisions }
          if (event.artifact_url) {
            patch.previewUrl = event.artifact_url
          }
          if (typeof event.payload.title === "string") {
            patch.title = event.payload.title
          }
          patchSlide(event.slide_id, patch)
          flushPendingEdits(event.slide_id)
          break
        }
        case "slide.failed":
          if (event.slide_id) {
            patchSlide(event.slide_id, { status: "failed" })
          }
          break
        case "edit.started":
          if (event.slide_id) {
            patchSlide(event.slide_id, { status: "editing" })
          }
          break
        case "edit.applied": {
          if (!event.slide_id) {
            break
          }
          bumpRevisionEpoch(event.slide_id)
          const chatId = event.payload.chat_id as string | undefined
          const action = (event.payload.action as string) ?? ""
          const revision = event.payload.revision as number | undefined
          const slide = get().slides.get(event.slide_id)
          let revisions = slide?.revisions
          if (slide && action && revision) {
            revisions = [
              ...slide.revisions.filter((item) => item.revision < revision),
              {
                revision,
                label: action,
                created_at: event.created_at,
                preview_url: event.artifact_url,
              },
            ]
          }
          patchSlide(event.slide_id, {
            status: "completed",
            previewUrl: event.artifact_url ?? slide?.previewUrl,
            revision: revision ?? slide?.revision,
            revisions,
          })
          const messages = get().chats.get(event.slide_id) ?? []
          // The POST receipt may not have its chat_id yet when the terminal
          // event wins the race; fall back to the last pending receipt.
          // Third level: a POST whose HTTP receipt was lost left a failed,
          // chatId-less receipt — with fail-stop there is no concurrent
          // dispatch, so this event's outcome belongs to it.
          const receipt =
            (chatId ? messages.find((message) => message.chatId === chatId) : undefined) ??
            messages.findLast((message) => message.status === "pending") ??
            messages.find((message) => message.status === "failed" && !message.chatId)
          if (receipt) {
            patchChat(event.slide_id, receipt.id, {
              status: "applied",
              content: event.message,
              action: action || undefined,
            })
          }
          flushPendingEdits(event.slide_id)
          break
        }
        case "edit.failed": {
          if (!event.slide_id) {
            break
          }
          // §6.3: edit.* maps to editing/done/failed; a failed edit surfaces
          // the per-slide retry path (§8.6).
          patchSlide(event.slide_id, { status: "failed" })
          const chatId = event.payload.chat_id as string | undefined
          const messages = get().chats.get(event.slide_id) ?? []
          const receipt =
            (chatId ? messages.find((message) => message.chatId === chatId) : undefined) ??
            messages.findLast((message) => message.status === "pending") ??
            messages.find((message) => message.status === "failed" && !message.chatId)
          if (receipt) {
            patchChat(event.slide_id, receipt.id, {
              status: "failed",
              content: event.message || "修改失败，可重新发送指令",
            })
          }
          break
        }
        case "edit.reverted": {
          if (!event.slide_id) {
            break
          }
          const slideId = event.slide_id
          bumpRevisionEpoch(slideId)
          const revision =
            typeof event.payload.revision === "number" ? event.payload.revision : undefined
          const patch: Partial<SlideView> = { status: "completed" }
          if (revision !== undefined) {
            patch.revision = revision
          }
          if (event.artifact_url) {
            patch.previewUrl = event.artifact_url
          } else if (revision !== undefined) {
            // §3 of the contract allows reverted events carrying only
            // revision/label; the canvas must still switch to the target
            // revision's image instead of keeping the previous one.
            const known = get()
              .slides.get(slideId)
              ?.revisions.find((item) => item.revision === revision)
            if (known?.preview_url) {
              patch.previewUrl = known.preview_url
            } else {
              // fetchRevisions discards the response if the task switched
              // or another revision mutation landed meanwhile.
              void fetchRevisions(slideId)
                .then((fetched) => {
                  if (!fetched) {
                    return
                  }
                  const target = fetched.find((item) => item.revision === revision)
                  const slide = get().slides.get(slideId)
                  // Backfill only while the slide still points at that
                  // revision; a newer event may have moved it on.
                  if (slide?.revision === revision && target?.preview_url) {
                    patchSlide(slideId, { revisions: fetched, previewUrl: target.preview_url })
                  } else {
                    patchSlide(slideId, { revisions: fetched })
                  }
                })
                .catch(() => {
                  // Best-effort backfill; the revision label stays correct.
                })
            }
          }
          patchSlide(slideId, patch)
          break
        }
        case "export.started":
          set({ exporting: true })
          break
        case "export.completed": {
          set({ exporting: false })
          const filename = (event.payload.filename as string) ?? "presentation.pptx"
          const url = safeDownloadUrl(event.artifact_url)
          toast.success("导出完成", {
            description: filename,
            action: url
              ? {
                  label: "下载",
                  onClick: () => {
                    const anchor = document.createElement("a")
                    anchor.href = url
                    anchor.download = filename
                    anchor.click()
                  },
                }
              : undefined,
          })
          break
        }
        case "export.failed":
          set({ exporting: false })
          toast.error("导出失败", { description: event.message })
          break
        default:
          break
      }
    },

    setConnection(connection) {
      set({ connection })
    },

    selectSlide(slideId) {
      set({ selectedSlideId: slideId, followLatest: false })
    },

    resumeFollow() {
      const { slides, slideOrder } = get()
      const active = slideOrder.find((id) => {
        const slide = slides.get(id)
        return slide?.status === "generating" || slide?.status === "editing"
      })
      const lastTouched = [...slideOrder]
        .reverse()
        .find((id) => slides.get(id)?.status !== "queued")
      set({
        followLatest: true,
        selectedSlideId: active ?? lastTouched ?? get().selectedSlideId,
      })
    },

    async cancelTask() {
      const { taskId } = get()
      if (!taskId) {
        return
      }
      try {
        await api.cancelTask(taskId)
      } catch {
        toast.error("取消请求失败，请重试")
      }
    },

    async resumeTask() {
      const { taskId } = get()
      if (!taskId) {
        return
      }
      set({ followLatest: true })
      try {
        await api.resumeTask(taskId)
      } catch {
        toast.error("继续生成请求失败，请重试")
      }
    },

    async exportTask(format) {
      const { taskId } = get()
      if (!taskId) {
        return
      }
      set({ exporting: true })
      try {
        await api.exportTask(taskId, format)
      } catch {
        set({ exporting: false })
        toast.error("导出请求失败")
      }
    },

    async sendChat(slideId, text, elementId) {
      const normalized = text.trim()
      if (!normalized) {
        return
      }
      const slide = get().slides.get(slideId)
      if (!slide) {
        return
      }
      if (slide.status !== "completed") {
        // §8.4 queue edits against unfinished slides.
        pushChat(slideId, {
          id: chatMessageId(),
          role: "user",
          content: normalized,
          status: "queued",
        })
        pushChat(slideId, {
          id: chatMessageId(),
          role: "system",
          content: "已排队，页面完成后自动执行",
          status: "info",
        })
        set((current) => {
          const pendingEdits = new Map(current.pendingEdits)
          pendingEdits.set(slideId, [...(pendingEdits.get(slideId) ?? []), normalized])
          return { pendingEdits }
        })
        return
      }
      pushChat(slideId, {
        id: chatMessageId(),
        role: "user",
        content: normalized,
        status: "sent",
      })
      await dispatchChat(slideId, normalized, elementId)
    },

    async undo(slideId) {
      const { taskId } = get()
      if (!taskId || get().slides.get(slideId)?.status !== "completed") {
        return
      }
      await mutateSlide(
        `${taskId}:undo:${slideId}`,
        slideId,
        "editing",
        "撤销失败，请重试",
        () => api.undo(taskId, slideId),
      )
    },

    async applyRevision(slideId, revision) {
      const { taskId } = get()
      if (!taskId || get().slides.get(slideId)?.status !== "completed") {
        return
      }
      await mutateSlide(
        `${taskId}:revision:${slideId}:${revision}`,
        slideId,
        "editing",
        "切换版本失败，请重试",
        () => api.applyRevision(taskId, slideId, revision),
      )
    },

    async retrySlide(slideId) {
      const { taskId } = get()
      const status = get().slides.get(slideId)?.status
      if (!taskId || (status !== "failed" && status !== "completed")) {
        return
      }
      await mutateSlide(
        `${taskId}:retry:${slideId}`,
        slideId,
        "generating",
        "重试请求失败，请重试",
        () => api.retrySlide(taskId, slideId),
      )
    },

    async loadRevisions(slideId) {
      try {
        const fetched = await fetchRevisions(slideId)
        if (fetched) {
          patchSlide(slideId, { revisions: fetched })
        }
      } catch {
        toast.error("获取版本历史失败")
      }
    },
  }
})
