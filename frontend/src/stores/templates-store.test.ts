// Regression tests for the template snapshot merge: a GET response may
// predate SSE events or local mutations and must never roll them back.

import { beforeEach, describe, expect, it, vi } from "vitest"

import type { GenerationEvent, TemplateSummary } from "@/types/api"

const mockApi = vi.hoisted(() => ({
  listTemplates: vi.fn(),
  uploadTemplate: vi.fn(),
  deleteTemplate: vi.fn(),
  retryParse: vi.fn(),
}))

vi.mock("@/lib/api", () => ({ api: mockApi, USE_MOCK: true }))
vi.mock("@/lib/sse", () => ({ connectTemplateEvents: vi.fn(() => () => {}) }))
vi.mock("sonner", () => ({
  toast: Object.assign(vi.fn(), {
    success: vi.fn(),
    error: vi.fn(),
    info: vi.fn(),
    warning: vi.fn(),
  }),
}))

import { useTemplatesStore } from "@/stores/templates-store"

function template(overrides: Partial<TemplateSummary> & { id: string }): TemplateSummary {
  return {
    name: overrides.id,
    description: "",
    owner: "user",
    status: "ready",
    slides: 10,
    ratio: "16:9",
    layouts: [],
    palette: {
      bg: "#fff",
      surface: "#fff",
      primary: "#000",
      accent: "#888",
      ink: "#000",
      dark: false,
    },
    ...overrides,
  }
}

beforeEach(() => {
  vi.clearAllMocks()
  useTemplatesStore.setState(useTemplatesStore.getInitialState(), true)
})

function progressEvent(
  templateId: string,
  progress: number,
  seq: number,
): GenerationEvent {
  return {
    task_id: "templates",
    seq,
    type: "template.parse_progress",
    stage: "template",
    status: "running",
    progress,
    message: "",
    slide_id: null,
    slide_index: null,
    total_slides: null,
    artifact_url: null,
    created_at: "2026-07-15T08:00:00.000Z",
    payload: { template_id: templateId, progress },
  }
}

describe("fetchTemplates merge", () => {
  it("keeps templates touched by SSE while the snapshot was in flight", async () => {
    useTemplatesStore.setState({
      templates: [
        template({ id: "m1-progress", status: "parsing", progress: 50 }),
        template({ id: "m1-finished", status: "parsing", progress: 70 }),
        template({ id: "m1-quiet", status: "parsing", progress: 20 }),
      ],
    })

    let resolveList!: (value: TemplateSummary[]) => void
    mockApi.listTemplates.mockReturnValue(
      new Promise<TemplateSummary[]>((resolve) => (resolveList = resolve)),
    )
    const fetching = useTemplatesStore.getState().fetchTemplates()

    // While the GET is in flight, SSE advances two templates.
    useTemplatesStore.getState().applyTemplateEvent(progressEvent("m1-progress", 80, 1))
    useTemplatesStore.getState().applyTemplateEvent({
      ...progressEvent("m1-finished", 100, 2),
      type: "template.ready",
    })

    resolveList([
      template({ id: "m1-progress", status: "parsing", progress: 40 }),
      template({ id: "m1-finished", status: "parsing", progress: 90 }),
      template({ id: "m1-quiet", status: "parsing", progress: 60 }),
    ])
    await fetching

    const byId = (id: string) =>
      useTemplatesStore.getState().templates.find((item) => item.id === id)
    expect(byId("m1-progress")?.progress).toBe(80)
    expect(byId("m1-finished")?.status).toBe("ready")
    // Untouched templates take the server snapshot as-is.
    expect(byId("m1-quiet")?.progress).toBe(60)
  })

  it("keeps in-flight uploads but drops templates the server no longer has", async () => {
    useTemplatesStore.setState({
      templates: [template({ id: "m3-deleted-elsewhere" })],
    })

    let resolveList!: (value: TemplateSummary[]) => void
    mockApi.listTemplates.mockReturnValue(
      new Promise<TemplateSummary[]>((resolve) => (resolveList = resolve)),
    )
    const fetching = useTemplatesStore.getState().fetchTemplates()

    // An upload completes while the GET is in flight: the snapshot predates
    // it and must not wipe it out.
    mockApi.uploadTemplate.mockResolvedValue(
      template({ id: "m3-fresh-upload", status: "parsing", progress: 3 }),
    )
    await useTemplatesStore.getState().uploadTemplate(new File(["x"], "a.pptx"))

    resolveList([])
    await fetching

    const ids = useTemplatesStore.getState().templates.map((item) => item.id)
    expect(ids).toContain("m3-fresh-upload")
    expect(ids).not.toContain("m3-deleted-elsewhere")
  })

  it("ignores a stale concurrent snapshot that resolves after a newer one", async () => {
    let resolveStale!: (value: TemplateSummary[]) => void
    mockApi.listTemplates.mockReturnValueOnce(
      new Promise<TemplateSummary[]>((resolve) => (resolveStale = resolve)),
    )
    const stale = useTemplatesStore.getState().fetchTemplates()

    mockApi.listTemplates.mockResolvedValueOnce([template({ id: "m4-fresh" })])
    await useTemplatesStore.getState().fetchTemplates()

    resolveStale([template({ id: "m4-stale" })])
    await stale

    const ids = useTemplatesStore.getState().templates.map((item) => item.id)
    expect(ids).toEqual(["m4-fresh"])
  })

  it("does not resurrect a template deleted in this session", async () => {
    useTemplatesStore.setState({
      templates: [template({ id: "m2-doomed" }), template({ id: "m2-keep" })],
    })
    mockApi.deleteTemplate.mockResolvedValue(undefined)
    await useTemplatesStore.getState().deleteTemplate("m2-doomed")

    mockApi.listTemplates.mockResolvedValue([
      template({ id: "m2-doomed" }),
      template({ id: "m2-keep" }),
    ])
    await useTemplatesStore.getState().fetchTemplates()

    const ids = useTemplatesStore.getState().templates.map((item) => item.id)
    expect(ids).toEqual(["m2-keep"])
  })
})

describe("template SSE ordering", () => {
  it("buffers an event for an id that only appears in a later snapshot", async () => {
    const templateId = "m0-arrives-late"
    useTemplatesStore.getState().applyTemplateEvent({
      ...progressEvent(templateId, 100, 1),
      type: "template.ready",
    })
    mockApi.listTemplates.mockResolvedValue([
      template({ id: templateId, status: "parsing", progress: 8 }),
    ])

    await useTemplatesStore.getState().fetchTemplates()

    const current = useTemplatesStore.getState().templates[0]
    expect(current.status).toBe("ready")
    expect(current.progress).toBe(100)
  })

  it("drops replayed and out-of-order events", () => {
    useTemplatesStore.setState({
      templates: [template({ id: "m5-seq", status: "parsing", progress: 50 })],
    })
    const { applyTemplateEvent } = useTemplatesStore.getState()

    applyTemplateEvent(progressEvent("m5-seq", 80, 5))
    // Replay / out-of-order: 80% must not run back to 30%.
    applyTemplateEvent(progressEvent("m5-seq", 30, 4))
    let current = useTemplatesStore.getState().templates[0]
    expect(current.progress).toBe(80)

    applyTemplateEvent({
      ...progressEvent("m5-seq", 100, 6),
      type: "template.ready",
    })
    // ready must not fall back to a stale progress with an equal seq.
    applyTemplateEvent(progressEvent("m5-seq", 90, 6))
    current = useTemplatesStore.getState().templates[0]
    expect(current.status).toBe("ready")
  })
})

describe("retryParse rollback binding", () => {
  it("keeps streamed parse progress when the HTTP request fails late", async () => {
    useTemplatesStore.setState({
      templates: [
        template({ id: "m6-retry", status: "failed", error: "解析失败" }),
      ],
    })

    let rejectRetry!: (reason: Error) => void
    mockApi.retryParse.mockReturnValue(
      new Promise((_, reject) => (rejectRetry = reject)),
    )
    const pending = useTemplatesStore.getState().retryParse("m6-retry")

    // The server accepted the retry and already streams progress.
    useTemplatesStore.getState().applyTemplateEvent(progressEvent("m6-retry", 42, 1))

    rejectRetry(new Error("network"))
    await pending

    const current = useTemplatesStore.getState().templates[0]
    expect(current.status).toBe("parsing")
    expect(current.progress).toBe(42)
  })
})
