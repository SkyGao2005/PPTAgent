// Regression tests for the SSE merge / queue / race logic that has bitten
// us before: placeholder migration, stale hydrate responses, artifact_url
// null handling, ghost placeholders, and queued-edit draining.

import { beforeEach, describe, expect, it, vi } from "vitest"

import type { GenerationEvent, SlideRevision, TaskSnapshot } from "@/types/api"

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

vi.mock("@/lib/api", () => ({ api: mockApi, USE_MOCK: true }))
vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
  }),
}))

import { useWorkbenchStore } from "@/stores/workbench-store"

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
  useWorkbenchStore.setState(useWorkbenchStore.getInitialState(), true)
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
