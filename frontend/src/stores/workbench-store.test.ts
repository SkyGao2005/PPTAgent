// Regression tests for the SSE merge / queue / race logic that has bitten
// us before: placeholder migration, stale hydrate responses, artifact_url
// null handling, ghost placeholders, and queued-edit draining.

import { beforeEach, describe, expect, it, vi } from "vitest"

import type {
  GenerationEvent,
  SlideArtifact,
  SlideRevision,
  TaskSnapshot,
} from "@/types/api"

const mockApi = vi.hoisted(() => ({
  getTask: vi.fn(),
  listSlides: vi.fn(),
  cancelTask: vi.fn(),
  resumeTask: vi.fn(),
  exportTask: vi.fn(),
  sendChat: vi.fn(),
  listRevisions: vi.fn(),
  applyRevision: vi.fn(),
  undo: vi.fn(),
  retrySlide: vi.fn(),
}))

vi.mock("@/lib/api", () => ({
  api: mockApi,
  ApiError: class ApiError extends Error {
    readonly status: number

    constructor(message: string, status: number) {
      super(message)
      this.status = status
    }
  },
  USE_MOCK: true,
}))
vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
  }),
}))

import { useWorkbenchStore } from "@/stores/workbench-store"

const stored = new Map<string, string>()

function snapshot(overrides: Partial<TaskSnapshot> = {}): TaskSnapshot {
  return {
    task_id: "t1",
    topic: "demo",
    status: "running",
    stage: "plan",
    progress: 0,
    template_id: "tpl",
    ratio: "16:9",
    total_slides: 0,
    last_seq: 0,
    created_at: "2026-07-15T08:00:00.000Z",
    updated_at: "2026-07-15T08:00:00.000Z",
    ...overrides,
  }
}

function event(
  partial: Partial<GenerationEvent> & { type: string; seq: number },
): GenerationEvent {
  return {
    task_id: "t1",
    stage: "generate",
    status: "running",
    progress: null,
    message: "",
    slide_id: null,
    slide_index: null,
    total_slides: null,
    artifact_url: null,
    created_at: "2026-07-15T08:00:01.000Z",
    payload: {},
    ...partial,
  }
}

async function hydrateTask(totalSlides: number): Promise<void> {
  mockApi.getTask.mockResolvedValue(snapshot({ total_slides: totalSlides }))
  mockApi.listSlides.mockResolvedValue([])
  await useWorkbenchStore.getState().hydrate("t1", totalSlides)
}

beforeEach(() => {
  vi.clearAllMocks()
  stored.clear()
  vi.stubGlobal("localStorage", {
    getItem: (key: string) => stored.get(key) ?? null,
    setItem: (key: string, value: string) => stored.set(key, value),
    removeItem: (key: string) => stored.delete(key),
    clear: () => stored.clear(),
  })
  useWorkbenchStore.setState(useWorkbenchStore.getInitialState(), true)
})

describe("refresh persistence", () => {
  it("restores the selected slide, chat history, and queued edits", async () => {
    await hydrateTask(2)
    await useWorkbenchStore.getState().sendChat("pending-2", "保留这条排队指令")
    useWorkbenchStore.getState().selectSlide("pending-2")

    useWorkbenchStore.setState(useWorkbenchStore.getInitialState(), true)
    await hydrateTask(2)

    const state = useWorkbenchStore.getState()
    expect(state.selectedSlideId).toBe("pending-2")
    expect(state.pendingEdits.get("pending-2")).toEqual(["保留这条排队指令"])
    expect(state.chats.get("pending-2")?.[0]?.content).toBe("保留这条排队指令")
  })
})

describe("applyEvent guards", () => {
  it("ignores events that belong to another task", async () => {
    await hydrateTask(2)
    useWorkbenchStore
      .getState()
      .applyEvent(event({ type: "task.failed", seq: 9, task_id: "other-task" }))
    const state = useWorkbenchStore.getState()
    expect(state.task?.status).toBe("running")
    expect(state.lastSeq).toBe(0)
  })
})

describe("placeholder migration", () => {
  it("moves queued chats and pending edits to the real slide id", async () => {
    await hydrateTask(3)
    await useWorkbenchStore.getState().sendChat("pending-2", "换个配色")

    useWorkbenchStore.getState().applyEvent(
      event({
        type: "slide.started",
        seq: 1,
        slide_id: "s2",
        slide_index: 2,
        payload: { title: "第二页" },
      }),
    )

    const state = useWorkbenchStore.getState()
    expect(state.slides.has("pending-2")).toBe(false)
    expect(state.slides.get("s2")?.title).toBe("第二页")
    expect(state.pendingEdits.get("s2")).toEqual(["换个配色"])
    expect(state.pendingEdits.has("pending-2")).toBe(false)
    expect(state.chats.get("s2")?.[0]?.content).toBe("换个配色")
    expect(state.chats.has("pending-2")).toBe(false)
  })
})

describe("hydrate race", () => {
  it("drops a stale response that resolves after a newer hydrate", async () => {
    let resolveStale!: (value: TaskSnapshot) => void
    mockApi.listSlides.mockResolvedValue([])
    mockApi.getTask.mockReturnValueOnce(
      new Promise<TaskSnapshot>((resolve) => (resolveStale = resolve)),
    )
    const stale = useWorkbenchStore.getState().hydrate("task-a", 5)

    mockApi.getTask.mockResolvedValueOnce(
      snapshot({ task_id: "task-b", total_slides: 2 }),
    )
    await useWorkbenchStore.getState().hydrate("task-b", 2)

    resolveStale(snapshot({ task_id: "task-a", total_slides: 5 }))
    await stale

    const state = useWorkbenchStore.getState()
    expect(state.task?.task_id).toBe("task-b")
    expect(state.slideOrder).toHaveLength(2)
  })
})

describe("artifact_url merging", () => {
  it("keeps the existing preview when slide.completed has no artifact_url", async () => {
    await hydrateTask(0)
    const { applyEvent } = useWorkbenchStore.getState()
    applyEvent(
      event({ type: "slide.started", seq: 1, slide_id: "s1", slide_index: 1 }),
    )
    applyEvent(
      event({
        type: "slide.preview_ready",
        seq: 2,
        slide_id: "s1",
        artifact_url: "https://cdn.example/p1.png",
        payload: { revision: 1 },
      }),
    )
    applyEvent(
      event({
        type: "slide.completed",
        seq: 3,
        slide_id: "s1",
        payload: { revision: 1, label: "初稿" },
      }),
    )

    const slide = useWorkbenchStore.getState().slides.get("s1")
    expect(slide?.status).toBe("completed")
    expect(slide?.previewUrl).toBe("https://cdn.example/p1.png")
    expect(slide?.revision).toBe(1)
  })
})

describe("outline reconciliation", () => {
  it("removes leftover placeholders when the outline is shorter", async () => {
    await hydrateTask(4)
    useWorkbenchStore.getState().applyEvent(
      event({
        type: "stage.completed",
        seq: 1,
        stage: "plan",
        payload: {
          outline: [
            { slide_id: "s1", index: 1, title: "封面" },
            { slide_id: "s2", index: 2, title: "目录" },
          ],
        },
      }),
    )

    const state = useWorkbenchStore.getState()
    expect(state.slideOrder).toEqual(["s1", "s2"])
    expect(state.task?.total_slides).toBe(2)
    expect(state.selectedSlideId).toBe("s1")
  })
})

describe("chat receipts", () => {
  it("resolves the pending receipt when edit.applied races the POST response", async () => {
    await hydrateTask(0)
    const { applyEvent } = useWorkbenchStore.getState()
    applyEvent(
      event({ type: "slide.started", seq: 1, slide_id: "s1", slide_index: 1 }),
    )
    applyEvent(
      event({
        type: "slide.completed",
        seq: 2,
        slide_id: "s1",
        artifact_url: "https://cdn.example/p1.png",
        payload: { revision: 1, label: "初稿" },
      }),
    )

    // POST never resolves before the terminal event arrives.
    mockApi.sendChat.mockReturnValue(new Promise(() => {}))
    void useWorkbenchStore.getState().sendChat("s1", "精简文案")

    applyEvent(
      event({
        type: "edit.applied",
        seq: 3,
        slide_id: "s1",
        message: "已精简本页文案",
        artifact_url: "https://cdn.example/p2.png",
        payload: { chat_id: "c1", action: "精简文案", revision: 2 },
      }),
    )

    const messages = useWorkbenchStore.getState().chats.get("s1") ?? []
    const receipt = messages.find((message) => message.role === "system")
    expect(receipt?.status).toBe("applied")
    expect(receipt?.content).toBe("已精简本页文案")
  })

  it("fail-stops the queue on a lost POST and resumes on the slide's next terminal event", async () => {
    await hydrateTask(1)
    const store = useWorkbenchStore.getState()
    await store.sendChat("pending-1", "换个配色")
    await store.sendChat("pending-1", "精简文案")

    mockApi.sendChat.mockRejectedValueOnce(new Error("network"))
    store.applyEvent(
      event({ type: "slide.started", seq: 1, slide_id: "s1", slide_index: 1 }),
    )
    store.applyEvent(
      event({
        type: "slide.completed",
        seq: 2,
        slide_id: "s1",
        artifact_url: "https://cdn.example/p1.png",
        payload: { revision: 1, label: "初稿" },
      }),
    )
    await vi.waitFor(() => expect(mockApi.sendChat).toHaveBeenCalledTimes(1))

    // Fail-stop: the second instruction must NOT be dispatched while the
    // first one's outcome is unknown.
    expect(useWorkbenchStore.getState().pendingEdits.get("s1")).toEqual(["精简文案"])

    // The server had actually accepted the first request: its edit.applied
    // arrives, gets bound to the chatId-less failed receipt, and the queue
    // resumes.
    mockApi.sendChat.mockResolvedValueOnce({ chat_id: "c2" })
    useWorkbenchStore.getState().applyEvent(
      event({
        type: "edit.applied",
        seq: 3,
        slide_id: "s1",
        message: "已更换配色",
        artifact_url: "https://cdn.example/p2.png",
        payload: { chat_id: "c-unknown", action: "更换配色", revision: 2 },
      }),
    )

    await vi.waitFor(() => expect(mockApi.sendChat).toHaveBeenCalledTimes(2))
    expect(useWorkbenchStore.getState().pendingEdits.size).toBe(0)
    const receipts = (useWorkbenchStore.getState().chats.get("s1") ?? []).filter(
      (message) => message.role === "system" && message.status !== "info",
    )
    expect(receipts[0]?.status).toBe("applied")
    expect(receipts[1]?.status).toBe("pending")
  })
})

describe("edit.reverted preview", () => {
  it("switches the canvas to the target revision's image when the event has no artifact_url", async () => {
    await hydrateTask(0)
    const { applyEvent } = useWorkbenchStore.getState()
    applyEvent(
      event({ type: "slide.started", seq: 1, slide_id: "s1", slide_index: 1 }),
    )
    applyEvent(
      event({
        type: "slide.completed",
        seq: 2,
        slide_id: "s1",
        artifact_url: "https://cdn.example/v1.png",
        payload: { revision: 1, label: "初稿" },
      }),
    )
    applyEvent(
      event({
        type: "edit.applied",
        seq: 3,
        slide_id: "s1",
        artifact_url: "https://cdn.example/v2.png",
        payload: { chat_id: "c1", action: "更换配色", revision: 2 },
      }),
    )

    applyEvent(
      event({
        type: "edit.reverted",
        seq: 4,
        slide_id: "s1",
        payload: { revision: 1, label: "初稿" },
      }),
    )

    const slide = useWorkbenchStore.getState().slides.get("s1")
    expect(slide?.revision).toBe(1)
    expect(slide?.previewUrl).toBe("https://cdn.example/v1.png")
  })
})

describe("revision snapshot staleness", () => {
  function revisionEntry(revision: number, label: string): SlideRevision {
    return {
      revision,
      label,
      created_at: "2026-07-15T08:00:00.000Z",
      preview_url: `https://cdn.example/v${revision}.png`,
    }
  }

  it("discards a revisions response that raced an SSE edit", async () => {
    await hydrateTask(0)
    const { applyEvent } = useWorkbenchStore.getState()
    applyEvent(
      event({ type: "slide.started", seq: 1, slide_id: "s1", slide_index: 1 }),
    )
    applyEvent(
      event({
        type: "slide.completed",
        seq: 2,
        slide_id: "s1",
        artifact_url: "https://cdn.example/v1.png",
        payload: { revision: 1, label: "初稿" },
      }),
    )

    let resolveFetch!: (value: SlideRevision[]) => void
    mockApi.listRevisions.mockReturnValue(
      new Promise<SlideRevision[]>((resolve) => (resolveFetch = resolve)),
    )
    const load = useWorkbenchStore.getState().loadRevisions("s1")

    // While the GET is in flight, an edit replaces v2 and prunes v3.
    useWorkbenchStore.getState().applyEvent(
      event({
        type: "edit.applied",
        seq: 3,
        slide_id: "s1",
        artifact_url: "https://cdn.example/v2-new.png",
        payload: { chat_id: "c1", action: "更换配色", revision: 2 },
      }),
    )

    resolveFetch([
      revisionEntry(1, "初稿"),
      revisionEntry(2, "旧的v2"),
      revisionEntry(3, "已被剪枝的v3"),
    ])
    await load

    const labels = (useWorkbenchStore.getState().slides.get("s1")?.revisions ?? []).map(
      (item) => item.label,
    )
    expect(labels).toEqual(["初稿", "更换配色"])
  })
})

describe("optimistic slide locking", () => {
  it("marks the slide editing during undo and rolls back on failure", async () => {
    await hydrateTask(0)
    const { applyEvent } = useWorkbenchStore.getState()
    applyEvent(
      event({ type: "slide.started", seq: 1, slide_id: "s1", slide_index: 1 }),
    )
    applyEvent(
      event({
        type: "slide.completed",
        seq: 2,
        slide_id: "s1",
        artifact_url: "https://cdn.example/v1.png",
        payload: { revision: 1, label: "初稿" },
      }),
    )
    // Undo needs something to undo: bring the slide to v2 first.
    applyEvent(
      event({
        type: "edit.applied",
        seq: 3,
        slide_id: "s1",
        artifact_url: "https://cdn.example/v2.png",
        payload: { chat_id: "c0", action: "更换配色", revision: 2 },
      }),
    )

    let rejectUndo!: (reason: Error) => void
    mockApi.undo.mockReturnValue(
      new Promise((_, reject) => (rejectUndo = reject)),
    )
    const pending = useWorkbenchStore.getState().undo("s1")

    // Locked through the REST→SSE window: conflicting mutations no-op.
    expect(useWorkbenchStore.getState().slides.get("s1")?.status).toBe("editing")
    await useWorkbenchStore.getState().undo("s1")
    expect(mockApi.undo).toHaveBeenCalledTimes(1)

    rejectUndo(new Error("network"))
    await pending
    expect(useWorkbenchStore.getState().slides.get("s1")?.status).toBe("completed")
  })
})

describe("revision switching guards", () => {
  async function completedSlideAtV2(): Promise<void> {
    await hydrateTask(0)
    const { applyEvent } = useWorkbenchStore.getState()
    applyEvent(
      event({ type: "slide.started", seq: 1, slide_id: "s1", slide_index: 1 }),
    )
    applyEvent(
      event({
        type: "slide.completed",
        seq: 2,
        slide_id: "s1",
        artifact_url: "https://cdn.example/v1.png",
        payload: { revision: 1, label: "初稿" },
      }),
    )
    applyEvent(
      event({
        type: "edit.applied",
        seq: 3,
        slide_id: "s1",
        artifact_url: "https://cdn.example/v2.png",
        payload: { chat_id: "c0", action: "更换配色", revision: 2 },
      }),
    )
  }

  it("no-ops when applying the slide's current revision", async () => {
    await completedSlideAtV2()
    await useWorkbenchStore.getState().applyRevision("s1", 2)
    expect(mockApi.applyRevision).not.toHaveBeenCalled()
    expect(useWorkbenchStore.getState().slides.get("s1")?.status).toBe("completed")
  })

  it("rejects (not queues) a switch while the slide is busy", async () => {
    await completedSlideAtV2()
    mockApi.applyRevision.mockReturnValue(new Promise(() => {}))
    void useWorkbenchStore.getState().applyRevision("s1", 1)
    expect(useWorkbenchStore.getState().slides.get("s1")?.status).toBe("editing")

    await useWorkbenchStore.getState().applyRevision("s1", 1)
    expect(mockApi.applyRevision).toHaveBeenCalledTimes(1)
  })

  it("no-ops undo at the first revision", async () => {
    await hydrateTask(0)
    const { applyEvent } = useWorkbenchStore.getState()
    applyEvent(
      event({ type: "slide.started", seq: 1, slide_id: "s1", slide_index: 1 }),
    )
    applyEvent(
      event({
        type: "slide.completed",
        seq: 2,
        slide_id: "s1",
        payload: { revision: 1, label: "初稿" },
      }),
    )
    await useWorkbenchStore.getState().undo("s1")
    expect(mockApi.undo).not.toHaveBeenCalled()
    expect(useWorkbenchStore.getState().slides.get("s1")?.status).toBe("completed")
  })
})

describe("late HTTP failure vs SSE outcome", () => {
  it("keeps the SSE terminal state when the retry's HTTP response fails late", async () => {
    await hydrateTask(0)
    const { applyEvent } = useWorkbenchStore.getState()
    applyEvent(
      event({ type: "slide.started", seq: 1, slide_id: "s1", slide_index: 1 }),
    )
    applyEvent(
      event({ type: "slide.failed", seq: 2, slide_id: "s1", status: "failed" }),
    )

    let rejectRetry!: (reason: Error) => void
    mockApi.retrySlide.mockReturnValue(
      new Promise((_, reject) => (rejectRetry = reject)),
    )
    const pending = useWorkbenchStore.getState().retrySlide("s1")
    expect(useWorkbenchStore.getState().slides.get("s1")?.status).toBe("generating")

    // SSE delivers the real outcome before the HTTP response fails.
    applyEvent(
      event({
        type: "slide.completed",
        seq: 3,
        slide_id: "s1",
        artifact_url: "https://cdn.example/v1.png",
        payload: { revision: 1, label: "初稿" },
      }),
    )

    rejectRetry(new Error("network"))
    await pending
    expect(useWorkbenchStore.getState().slides.get("s1")?.status).toBe("completed")
  })
})

describe("fetchRevisions across re-hydrate", () => {
  function artifact(): SlideArtifact {
    return {
      slide_id: "s1",
      task_id: "t1",
      index: 1,
      title: "封面",
      summary: "",
      status: "completed",
      mode: "html",
      revision: 1,
      preview_url: "https://cdn.example/v1.png",
    }
  }

  it("discards a revisions response issued before a re-hydrate of the same task", async () => {
    mockApi.getTask.mockResolvedValue(snapshot({ total_slides: 1 }))
    mockApi.listSlides.mockResolvedValue([artifact()])
    await useWorkbenchStore.getState().hydrate("t1", 1)

    let resolveFetch!: (value: SlideRevision[]) => void
    mockApi.listRevisions.mockReturnValue(
      new Promise<SlideRevision[]>((resolve) => (resolveFetch = resolve)),
    )
    const load = useWorkbenchStore.getState().loadRevisions("s1")

    // Same task re-enters (refresh / back-forward): epochs restart at zero,
    // which must not let the pre-hydrate response pass as fresh (ABA).
    await useWorkbenchStore.getState().hydrate("t1", 1)

    resolveFetch([
      {
        revision: 1,
        label: "初稿",
        created_at: "2026-07-15T08:00:00.000Z",
        preview_url: "https://cdn.example/v1.png",
      },
    ])
    await load

    expect(useWorkbenchStore.getState().slides.get("s1")?.revisions).toEqual([])
  })
})

describe("ambiguous receipt attribution", () => {
  it("binds an unmatched terminal event to neither receipt when a lost POST and a new pending coexist", async () => {
    await hydrateTask(0)
    const { applyEvent } = useWorkbenchStore.getState()
    applyEvent(
      event({ type: "slide.started", seq: 1, slide_id: "s1", slide_index: 1 }),
    )
    applyEvent(
      event({
        type: "slide.completed",
        seq: 2,
        slide_id: "s1",
        payload: { revision: 1, label: "初稿" },
      }),
    )

    // Instruction A: the POST fails, but the server may still process it.
    mockApi.sendChat.mockRejectedValueOnce(new Error("network"))
    await useWorkbenchStore.getState().sendChat("s1", "换个配色")

    // Instruction B: dispatched, POST still in flight.
    mockApi.sendChat.mockReturnValue(new Promise(() => {}))
    void useWorkbenchStore.getState().sendChat("s1", "精简文案")

    // A's outcome arrives with a chat_id the client never learned. It could
    // be A's or (in the receipt race) B's — bind neither.
    applyEvent(
      event({
        type: "edit.applied",
        seq: 3,
        slide_id: "s1",
        message: "已更换配色",
        payload: { chat_id: "c-a", action: "更换配色", revision: 2 },
      }),
    )

    const receipts = (useWorkbenchStore.getState().chats.get("s1") ?? []).filter(
      (message) => message.role === "system" && message.status !== "info",
    )
    expect(receipts[0]?.status).toBe("failed")
    expect(receipts[1]?.status).toBe("pending")
  })
})

describe("revision branching", () => {
  it("replaces a same-number revision and prunes the branch above it", async () => {
    await hydrateTask(0)
    const { applyEvent } = useWorkbenchStore.getState()
    applyEvent(
      event({ type: "slide.started", seq: 1, slide_id: "s1", slide_index: 1 }),
    )
    applyEvent(
      event({
        type: "slide.completed",
        seq: 2,
        slide_id: "s1",
        artifact_url: "https://cdn.example/v1.png",
        payload: { revision: 1, label: "初稿" },
      }),
    )
    applyEvent(
      event({
        type: "edit.applied",
        seq: 3,
        slide_id: "s1",
        artifact_url: "https://cdn.example/v2.png",
        payload: { chat_id: "c1", action: "更换配色", revision: 2 },
      }),
    )
    applyEvent(
      event({
        type: "edit.applied",
        seq: 4,
        slide_id: "s1",
        artifact_url: "https://cdn.example/v3.png",
        payload: { chat_id: "c2", action: "精简文案", revision: 3 },
      }),
    )
    // Switch back to v1, then regenerate: the new v2 replaces the old one
    // and the old v3 is pruned.
    applyEvent(
      event({
        type: "edit.reverted",
        seq: 5,
        slide_id: "s1",
        artifact_url: "https://cdn.example/v1.png",
        payload: { revision: 1, label: "初稿" },
      }),
    )
    applyEvent(
      event({ type: "slide.started", seq: 6, slide_id: "s1", slide_index: 1 }),
    )
    applyEvent(
      event({
        type: "slide.completed",
        seq: 7,
        slide_id: "s1",
        artifact_url: "https://cdn.example/v2-new.png",
        payload: { revision: 2, label: "重新生成" },
      }),
    )

    const slide = useWorkbenchStore.getState().slides.get("s1")
    expect(slide?.revision).toBe(2)
    expect(slide?.revisions.map((item) => [item.revision, item.label])).toEqual([
      [1, "初稿"],
      [2, "重新生成"],
    ])
  })
})

describe("export guard", () => {
  it("refuses to export while a slide is editing even if the menu was already open", async () => {
    await hydrateTask(0)
    const { applyEvent } = useWorkbenchStore.getState()
    applyEvent(
      event({ type: "slide.started", seq: 1, slide_id: "s1", slide_index: 1 }),
    )
    applyEvent(
      event({
        type: "slide.completed",
        seq: 2,
        slide_id: "s1",
        payload: { revision: 1, label: "初稿" },
      }),
    )
    applyEvent(event({ type: "task.completed", seq: 3, status: "succeeded" }))
    applyEvent(event({ type: "edit.started", seq: 4, slide_id: "s1" }))

    await useWorkbenchStore.getState().exportTask("pptx")
    expect(mockApi.exportTask).not.toHaveBeenCalled()
    expect(useWorkbenchStore.getState().exporting).toBe(false)
  })
})

describe("hydrate snapshot ordering", () => {
  it("fetches the task watermark before the slides snapshot", async () => {
    mockApi.getTask.mockResolvedValue(snapshot({ total_slides: 1 }))
    mockApi.listSlides.mockResolvedValue([])
    await useWorkbenchStore.getState().hydrate("t1", 1)

    const taskOrder = mockApi.getTask.mock.invocationCallOrder[0]
    const slidesOrder = mockApi.listSlides.mock.invocationCallOrder[0]
    expect(taskOrder).toBeLessThan(slidesOrder)
  })
})

describe("terminal edge cases", () => {
  it("does not guess between multiple chat receipts whose HTTP response was lost", async () => {
    await hydrateTask(0)
    const { applyEvent } = useWorkbenchStore.getState()
    applyEvent(
      event({ type: "slide.started", seq: 1, slide_id: "s1", slide_index: 1 }),
    )
    applyEvent(
      event({
        type: "slide.completed",
        seq: 2,
        slide_id: "s1",
        payload: { revision: 1, label: "初稿" },
      }),
    )
    useWorkbenchStore.setState({
      chats: new Map([
        [
          "s1",
          [
            { id: "r1", role: "system", content: "失败 1", status: "failed" },
            { id: "r2", role: "system", content: "失败 2", status: "failed" },
          ],
        ],
      ]),
    })

    applyEvent(
      event({
        type: "edit.applied",
        seq: 3,
        slide_id: "s1",
        message: "已修改",
        payload: { chat_id: "unknown", action: "修改", revision: 2 },
      }),
    )

    expect(
      useWorkbenchStore.getState().chats.get("s1")?.map((message) => message.status),
    ).toEqual(["failed", "failed"])
  })

  it("settles editing slides and receipts when a task is cancelled", async () => {
    await hydrateTask(0)
    const { applyEvent } = useWorkbenchStore.getState()
    applyEvent(
      event({ type: "slide.started", seq: 1, slide_id: "s1", slide_index: 1 }),
    )
    applyEvent(
      event({
        type: "slide.completed",
        seq: 2,
        slide_id: "s1",
        payload: { revision: 1, label: "初稿" },
      }),
    )
    mockApi.sendChat.mockReturnValue(new Promise(() => {}))
    void useWorkbenchStore.getState().sendChat("s1", "精简文案")

    applyEvent(event({ type: "task.cancelled", seq: 3, status: "cancelled" }))

    expect(useWorkbenchStore.getState().slides.get("s1")?.status).toBe("completed")
    const receipt = useWorkbenchStore
      .getState()
      .chats.get("s1")
      ?.find((message) => message.role === "system")
    expect(receipt?.status).toBe("failed")
  })

  it("drops queued state together with placeholders removed by the final outline", async () => {
    await hydrateTask(4)
    await useWorkbenchStore.getState().sendChat("pending-4", "换个配色")

    useWorkbenchStore.getState().applyEvent(
      event({
        type: "stage.completed",
        seq: 1,
        stage: "plan",
        payload: {
          outline: [
            { slide_id: "s1", index: 1, title: "封面" },
            { slide_id: "s2", index: 2, title: "目录" },
          ],
        },
      }),
    )

    const state = useWorkbenchStore.getState()
    expect(state.chats.has("pending-4")).toBe(false)
    expect(state.pendingEdits.has("pending-4")).toBe(false)
  })

  it("clears a stale preview when a reverted event has no resolvable artifact", async () => {
    const artifact: SlideArtifact = {
      slide_id: "s1",
      task_id: "t1",
      index: 1,
      title: "封面",
      summary: "",
      status: "completed",
      mode: "html",
      revision: 2,
      preview_url: "https://cdn.example/stale.png",
    }
    mockApi.getTask.mockResolvedValue(snapshot({ total_slides: 1 }))
    mockApi.listSlides.mockResolvedValue([artifact])
    mockApi.listRevisions.mockRejectedValue(new Error("network"))
    await useWorkbenchStore.getState().hydrate("t1", 1)

    useWorkbenchStore.getState().applyEvent(
      event({
        type: "edit.reverted",
        seq: 1,
        slide_id: "s1",
        payload: { revision: 1, label: "初稿" },
      }),
    )

    const slide = useWorkbenchStore.getState().slides.get("s1")
    expect(slide?.revision).toBe(1)
    expect(slide?.previewUrl).toBeNull()
  })
})
