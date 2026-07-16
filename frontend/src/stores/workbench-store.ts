import { toast } from "sonner"
import { create } from "zustand"

import { ApiError, api } from "@/lib/api"
import { resolveApiUrl } from "@/lib/api-url"
import type { ConnectionState } from "@/lib/sse"
import { createClientId } from "@/lib/utils"
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
  /** Changes whenever preview content changes, even if revision/path is reused. */
  previewVersion: string | number
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
  return createClientId()
}

interface PersistedWorkbenchState {
  chats: Array<[string, WorkChatMessage[]]>
  pendingEdits: Array<[string, string[]]>
  selectedSlideId: string | null
}

const WORKBENCH_STORAGE_PREFIX = "pptagent.workbench.v1."

function readPersistedWorkbench(taskId: string): PersistedWorkbenchState | null {
  try {
    const raw = localStorage.getItem(`${WORKBENCH_STORAGE_PREFIX}${taskId}`)
    if (!raw) {
      return null
    }
    const parsed = JSON.parse(raw) as Partial<PersistedWorkbenchState>
    if (!Array.isArray(parsed.chats) || !Array.isArray(parsed.pendingEdits)) {
      return null
    }
    const chats = parsed.chats.filter(
      (entry): entry is [string, WorkChatMessage[]] =>
        Array.isArray(entry) && typeof entry[0] === "string" && Array.isArray(entry[1]),
    )
    const pendingEdits = parsed.pendingEdits.filter(
      (entry): entry is [string, string[]] =>
        Array.isArray(entry) &&
        typeof entry[0] === "string" &&
        Array.isArray(entry[1]) &&
        entry[1].every((item) => typeof item === "string"),
    )
    return {
      chats,
      pendingEdits,
      selectedSlideId:
        typeof parsed.selectedSlideId === "string" ? parsed.selectedSlideId : null,
    }
  } catch {
    return null
  }
}

// Export downloads navigate the browser; only same-origin paths and
// http(s)/blob URLs are trusted, never javascript:/data: or //host.
function safeDownloadUrl(url: string | null): string | null {
  if (!url) {
    return null
  }
  const resolved = resolveApiUrl(url)
  if (/^(https?:|blob:)/i.test(resolved)) {
    return resolved
  }
  return resolved.startsWith("/") && !resolved.startsWith("//") ? resolved : null
}

async function downloadArtifact(url: string, filename: string): Promise<void> {
  const response = await fetch(url)
  if (!response.ok) {
    throw new Error(`Download failed: ${response.status}`)
  }
  const objectUrl = URL.createObjectURL(await response.blob())
  try {
    const anchor = document.createElement("a")
    anchor.href = objectUrl
    anchor.download = filename
    anchor.click()
  } finally {
    URL.revokeObjectURL(objectUrl)
  }
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

  // Attributes a terminal edit event to its chat receipt. A chat_id match is
  // definitive. Without one there are two possible owners: a failed,
  // chatId-less receipt (a POST whose HTTP receipt was lost but that the
  // server may still have processed) and the newest pending receipt whose
  // POST response lost the race against SSE. When only one candidate exists
  // it is the owner; when both exist the backend gives us no client request
  // id to tell them apart, and binding to either could attribute one
  // instruction's outcome to another — so bind to neither.
  function findReceipt(
    messages: WorkChatMessage[],
    chatId: string | undefined,
  ): WorkChatMessage | undefined {
    const matched = chatId
      ? messages.find((message) => message.chatId === chatId)
      : undefined
    if (matched) {
      return matched
    }
    const candidates = messages.filter(
      (message) =>
        !message.chatId &&
        (message.status === "failed" || message.status === "pending"),
    )
    return candidates.length === 1 ? candidates[0] : undefined
  }

  // Per-slide epoch, bumped by every SSE-driven revision mutation. A GET has
  // no watermark, so a response that raced such a mutation (a same-number
  // revision replaced from an earlier base, a pruned revision resurrected)
  // cannot be merged — it is discarded wholesale. A response whose epoch is
  // unchanged reflects server state at-or-after the request, and the server
  // had already applied everything SSE delivered before it, so replacing the
  // list is safe. Epoch numbers restart at zero on hydrate, so freshness
  // checks must also compare hydrateGeneration or a pre-hydrate request
  // could pass as fresh (ABA when re-entering the same task).
  let revisionEpochs = new Map<string, number>()

  function bumpRevisionEpoch(slideId: string): void {
    revisionEpochs.set(slideId, (revisionEpochs.get(slideId) ?? 0) + 1)
  }

  // Per-slide epoch, bumped by every SSE event touching the slide. An
  // optimistic mutation records the epoch it started at; its HTTP-failure
  // rollback applies only while the epoch is unchanged — a late rejection
  // must not clobber a state the server already confirmed via SSE.
  let statusEpochs = new Map<string, number>()

  function bumpStatusEpoch(slideId: string): void {
    statusEpochs.set(slideId, (statusEpochs.get(slideId) ?? 0) + 1)
  }

  // Returns null when the response is stale: the task switched, the task
  // was re-hydrated, or the slide's revisions changed while the request
  // was in flight.
  async function fetchRevisions(slideId: string): Promise<SlideRevision[] | null> {
    const { taskId } = get()
    if (!taskId) {
      return null
    }
    const generation = hydrateGeneration
    const epoch = revisionEpochs.get(slideId) ?? 0
    const fetched = await api.listRevisions(taskId, slideId)
    const fresh =
      get().taskId === taskId &&
      generation === hydrateGeneration &&
      (revisionEpochs.get(slideId) ?? 0) === epoch
    return fresh ? fetched : null
  }

  // Concurrent slide mutations are rejected, not queued: the optimistic
  // status keeps the UI (undo, version menu, regenerate, export) locked
  // through the whole REST→SSE window — not just until the HTTP response —
  // and a second operation during that window is dropped with feedback, so
  // the user re-issues it against the settled state.
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
    const generation = hydrateGeneration
    const epoch = statusEpochs.get(slideId) ?? 0
    patchSlide(slideId, { status: optimisticStatus })
    try {
      await run()
    } catch {
      // Roll back only while this mutation still owns the status: if SSE
      // touched the slide (or the task re-hydrated) meanwhile, the server
      // outcome wins over the late HTTP failure.
      if (
        generation === hydrateGeneration &&
        (statusEpochs.get(slideId) ?? 0) === epoch
      ) {
        patchSlide(slideId, { status: previousStatus })
      }
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
          previewVersion: 0,
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
    const generation = hydrateGeneration
    const epoch = statusEpochs.get(slideId) ?? 0
    patchSlide(slideId, { status: "editing" })
    try {
      const { chat_id } = await api.sendChat(taskId, slideId, text, elementId)
      patchChat(slideId, receiptId, { chatId: chat_id })
    } catch {
      // Same epoch binding as mutateSlide: a late rejection must not clobber
      // a status SSE already moved on.
      if (
        previousStatus &&
        generation === hydrateGeneration &&
        (statusEpochs.get(slideId) ?? 0) === epoch
      ) {
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
      const persisted = readPersistedWorkbench(taskId)
      revisionEpochs = new Map()
      statusEpochs = new Map()
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
            previewVersion: `${artifact.revision}-${task.last_seq}`,
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
              previewVersion: 0,
              revision: 0,
              revisions: [],
            })
            slideOrder.push(id)
          }
        }
        const active = [...slides.values()].find(
          (slide) => slide.status === "generating" || slide.status === "editing",
        )
        const restoreId = (storedId: string): string | null => {
          if (slides.has(storedId)) {
            return storedId
          }
          const match = /^pending-(\d+)$/.exec(storedId)
          if (!match) {
            return null
          }
          const index = Number(match[1])
          return [...slides.values()].find((slide) => slide.index === index)?.id ?? null
        }
        const chats = new Map<string, WorkChatMessage[]>()
        for (const [storedId, messages] of persisted?.chats ?? []) {
          const id = restoreId(storedId)
          if (!id || !Array.isArray(messages)) {
            continue
          }
          const restored = messages.map((message) =>
            message.role === "system" && message.status === "pending"
              ? {
                  ...message,
                  status: "failed" as const,
                  content: "页面已刷新，无法确认这条修改是否完成，请检查预览后重试",
                }
              : message,
          )
          chats.set(id, [...(chats.get(id) ?? []), ...restored])
        }
        const pendingEdits = new Map<string, string[]>()
        for (const [storedId, queue] of persisted?.pendingEdits ?? []) {
          const id = restoreId(storedId)
          if (id && Array.isArray(queue) && queue.length) {
            pendingEdits.set(id, [...(pendingEdits.get(id) ?? []), ...queue])
          }
        }
        const restoredSelection = persisted?.selectedSlideId
          ? restoreId(persisted.selectedSlideId)
          : null
        const selectedSlideId = restoredSelection ?? active?.id ?? slideOrder[0] ?? null
        set({
          task,
          slides,
          slideOrder,
          chats,
          pendingEdits,
          selectedSlideId,
          followLatest: task.status === "running" && !restoredSelection,
          lastSeq: task.last_seq,
          hydrated: true,
        })
        for (const slideId of pendingEdits.keys()) {
          flushPendingEdits(slideId)
        }
      } catch (error) {
        if (generation !== hydrateGeneration) {
          return
        }
        const missing =
          error instanceof ApiError && (error.status === 404 || error.status === 410)
        set({
          hydrated: true,
          loadError: missing
            ? "任务不存在或已过期"
            : "任务加载失败，请检查网络或后端服务后重试",
        })
      }
    },

    applyEvent(event) {
      const state = get()
      // Reject events from a previous task's stream and stale replays.
      if (event.task_id !== state.taskId || event.seq <= state.lastSeq) {
        return
      }
      // Any slide-scoped event is server-side activity on that slide: an
      // optimistic rollback armed before this point must no longer fire.
      if (event.slide_id) {
        bumpStatusEpoch(event.slide_id)
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
                previewVersion: 0,
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
              if (slide.status === "generating" || slide.status === "editing") {
                bumpStatusEpoch(id)
                slides.set(id, {
                  ...slide,
                  status: slide.status === "editing" && slide.revision > 0
                    ? "completed"
                    : "queued",
                })
              }
            }
            const chats = new Map(current.chats)
            for (const [slideId, messages] of chats) {
              chats.set(
                slideId,
                messages.map((message) =>
                  message.role === "system" && message.status === "pending"
                    ? {
                        ...message,
                        status: "failed",
                        content: "任务已暂停，本次修改未完成，请继续任务后重试",
                      }
                    : message,
                ),
              )
            }
            return { slides, chats, followLatest: false }
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
              const chats = new Map(current.chats)
              const pendingEdits = new Map(current.pendingEdits)
              for (const id of stale) {
                slides.delete(id)
                chats.delete(id)
                pendingEdits.delete(id)
              }
              const slideOrder = current.slideOrder.filter((id) => slides.has(id))
              const selectedSlideId =
                current.selectedSlideId && slides.has(current.selectedSlideId)
                  ? current.selectedSlideId
                  : (slideOrder[0] ?? null)
              return { slides, slideOrder, selectedSlideId, chats, pendingEdits }
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
            patch.previewVersion = event.seq
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
          // A regeneration from an earlier base reuses an existing revision
          // number: the entry is replaced and everything above it is pruned,
          // the same branch semantics as edit.applied.
          const existing = slide?.revisions.find((item) => item.revision === revision)
          const entry: SlideRevision = {
            revision,
            label,
            created_at: event.created_at,
            preview_url: event.artifact_url ?? existing?.preview_url ?? null,
          }
          const revisions = slide
            ? [...slide.revisions.filter((item) => item.revision < revision), entry]
            : undefined
          const patch: Partial<SlideView> = { status: "completed", revision, revisions }
          if (event.artifact_url) {
            patch.previewUrl = event.artifact_url
            patch.previewVersion = event.seq
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
            previewVersion: event.artifact_url ? event.seq : slide?.previewVersion,
            revision: revision ?? slide?.revision,
            revisions,
          })
          const receipt = findReceipt(get().chats.get(event.slide_id) ?? [], chatId)
          if (receipt) {
            const patch: Partial<WorkChatMessage> = {
              status: "applied",
              content: event.message,
              action: action || undefined,
            }
            // Stamp the chat_id so this receipt stops matching future
            // unattributed events.
            if (chatId && !receipt.chatId) {
              patch.chatId = chatId
            }
            patchChat(event.slide_id, receipt.id, patch)
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
          const receipt = findReceipt(get().chats.get(event.slide_id) ?? [], chatId)
          if (receipt) {
            const patch: Partial<WorkChatMessage> = {
              status: "failed",
              content: event.message || "修改失败，可重新发送指令",
            }
            if (chatId && !receipt.chatId) {
              patch.chatId = chatId
            }
            patchChat(event.slide_id, receipt.id, patch)
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
            patch.previewVersion = event.seq
          } else if (revision !== undefined) {
            // §3 of the contract allows reverted events carrying only
            // revision/label; the canvas must still switch to the target
            // revision's image instead of keeping the previous one.
            const known = get()
              .slides.get(slideId)
              ?.revisions.find((item) => item.revision === revision)
            if (known?.preview_url) {
              patch.previewUrl = known.preview_url
              patch.previewVersion = event.seq
            } else {
              // Never display the previous revision under the new label.
              patch.previewUrl = null
              patch.previewVersion = event.seq
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
                    patchSlide(slideId, {
                      revisions: fetched,
                      previewUrl: target.preview_url,
                      previewVersion: event.seq,
                    })
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
                    void downloadArtifact(url, filename).catch(() => {
                      toast.error("下载失败，请检查后端资源跨域配置")
                    })
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
      const { taskId, task, slides, exporting } = get()
      if (!taskId || !task || exporting) {
        return
      }
      // Mirrors the export trigger's disabled state: the menu may already be
      // open when SSE flips a slide back to generating/editing, so the
      // action itself must re-check instead of trusting the UI gate.
      const allDone =
        slides.size > 0 &&
        [...slides.values()].every((slide) => slide.status === "completed")
      if (task.status === "running" || !allDone) {
        toast.info("请等待所有页面完成后再导出")
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
      const slide = get().slides.get(slideId)
      // Undo below v2 is a server-side no-op that would strand the
      // optimistic "editing" status: no edit.reverted ever arrives.
      if (!taskId || slide?.status !== "completed" || slide.revision <= 1) {
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
      const slide = get().slides.get(slideId)
      if (!taskId || !slide) {
        return
      }
      if (slide.status !== "completed") {
        // Not queued: the slide moved on while the menu was open. Tell the
        // user to redo the switch once the current update settles.
        toast.info("本页正在更新，完成后请重新选择版本")
        return
      }
      // Applying the current revision is a server-side no-op (no
      // edit.reverted event), which would strand the optimistic "editing".
      if (slide.revision === revision) {
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
          const slide = get().slides.get(slideId)
          const current = fetched.find((item) => item.revision === slide?.revision)
          patchSlide(slideId, {
            revisions: fetched,
            ...(current?.preview_url
              ? { previewUrl: current.preview_url, previewVersion: get().lastSeq }
              : {}),
          })
        }
      } catch {
        toast.error("获取版本历史失败")
      }
    },
  }
})

const persistedWorkbenchJson = new Map<string, string>()

useWorkbenchStore.subscribe((state) => {
  if (!state.taskId || !state.hydrated || state.loadError) {
    return
  }
  const serialized = JSON.stringify({
    chats: [...state.chats],
    pendingEdits: [...state.pendingEdits],
    selectedSlideId: state.selectedSlideId,
  } satisfies PersistedWorkbenchState)
  try {
    const key = `${WORKBENCH_STORAGE_PREFIX}${state.taskId}`
    if (
      persistedWorkbenchJson.get(state.taskId) === serialized &&
      localStorage.getItem(key) === serialized
    ) {
      return
    }
    localStorage.setItem(key, serialized)
    persistedWorkbenchJson.set(state.taskId, serialized)
  } catch {
    // Storage can be unavailable or full; the in-memory workbench remains usable.
  }
})
