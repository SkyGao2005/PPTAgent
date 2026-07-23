import { beforeEach, describe, expect, it, vi } from "vitest"

const mockApi = vi.hoisted(() => ({
  uploadAttachment: vi.fn(),
  deleteAttachment: vi.fn(),
  createOutline: vi.fn(),
}))

vi.mock("@/lib/api", () => ({ api: mockApi, USE_MOCK: true }))
vi.mock("sonner", () => ({
  toast: { warning: vi.fn() },
}))

import {
  MAX_ATTACHMENTS,
  NO_TEMPLATE,
  useCreateTaskStore,
} from "@/stores/create-task-store"

function fakeFile(name: string): File {
  return new File(["x"], name)
}

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.deleteAttachment.mockResolvedValue(undefined)
  useCreateTaskStore.setState(useCreateTaskStore.getInitialState(), true)
})

describe("createOutline attachment lifecycle", () => {
  it("reuses successful partial uploads on retry and clears files after success", async () => {
    const store = useCreateTaskStore.getState()
    store.setTopic("季度复盘")
    store.selectTemplate("tpl", "16:9")
    store.addAttachments([fakeFile("a.pdf"), fakeFile("b.pdf")])

    mockApi.uploadAttachment
      .mockResolvedValueOnce({ attachment_id: "att-a" })
      .mockRejectedValueOnce(new Error("network"))
    await expect(store.createOutline()).rejects.toThrow("network")

    mockApi.uploadAttachment.mockResolvedValueOnce({ attachment_id: "att-b" })
    mockApi.createOutline.mockResolvedValue({ outline_id: "o1" })
    await expect(useCreateTaskStore.getState().createOutline()).resolves.toBe("o1")

    expect(mockApi.uploadAttachment).toHaveBeenCalledTimes(3)
    expect(mockApi.createOutline).toHaveBeenCalledWith(
      expect.objectContaining({ attachment_ids: ["att-a", "att-b"] }),
    )
    expect(useCreateTaskStore.getState().attachments).toEqual([])
  })

  it("cleans an already-uploaded temporary attachment when the user removes it", async () => {
    const store = useCreateTaskStore.getState()
    store.setTopic("季度复盘")
    store.selectTemplate("tpl", "16:9")
    store.addAttachments([fakeFile("a.pdf"), fakeFile("b.pdf")])
    mockApi.uploadAttachment
      .mockResolvedValueOnce({ attachment_id: "att-a" })
      .mockRejectedValueOnce(new Error("network"))
    await expect(store.createOutline()).rejects.toThrow()

    const uploaded = useCreateTaskStore.getState().attachments[0]
    useCreateTaskStore.getState().removeAttachment(uploaded.id)
    await vi.waitFor(() => expect(mockApi.deleteAttachment).toHaveBeenCalledWith("att-a"))
  })
})

describe("addAttachments", () => {
  it("reports how many files were actually accepted", () => {
    const { addAttachments } = useCreateTaskStore.getState()
    const first = addAttachments(
      Array.from({ length: MAX_ATTACHMENTS - 1 }, (_, i) => fakeFile(`a${i}.pdf`)),
    )
    expect(first).toBe(MAX_ATTACHMENTS - 1)

    const second = addAttachments([fakeFile("b1.pdf"), fakeFile("b2.pdf")])
    expect(second).toBe(1)
    expect(useCreateTaskStore.getState().attachments).toHaveLength(MAX_ATTACHMENTS)

    const third = addAttachments([fakeFile("c.pdf")])
    expect(third).toBe(0)
  })
})

describe("template selection", () => {
  it("updates the template id and its fixed canvas ratio atomically", () => {
    const store = useCreateTaskStore.getState()

    store.selectTemplate("beamer", "4:3")

    expect(useCreateTaskStore.getState()).toMatchObject({
      templateId: "beamer",
      ratio: "4:3",
    })
  })
})

describe("generating without a template", () => {
  it("sends a null template id so the backend derives the design itself", async () => {
    const store = useCreateTaskStore.getState()
    store.setTopic("自由设计")
    store.useNoTemplate()
    mockApi.createOutline.mockResolvedValue({ outline_id: "o-free" })

    await expect(useCreateTaskStore.getState().createOutline()).resolves.toBe("o-free")

    expect(mockApi.createOutline).toHaveBeenCalledWith(
      expect.objectContaining({ template_id: null }),
    )
  })

  it("keeps the sentinel out of the payload only, so the choice stays visible", () => {
    const store = useCreateTaskStore.getState()
    store.useNoTemplate()

    expect(useCreateTaskStore.getState().templateId).toBe(NO_TEMPLATE)
    expect(useCreateTaskStore.getState().templateId).not.toBe("")
  })

  it("lets the ratio be chosen when no template dictates one", () => {
    const store = useCreateTaskStore.getState()
    store.useNoTemplate()
    store.setRatio("4:3")

    expect(useCreateTaskStore.getState().ratio).toBe("4:3")
    // Picking a template afterwards hands the canvas back to the template.
    useCreateTaskStore.getState().selectTemplate("tpl", "16:9")
    expect(useCreateTaskStore.getState().ratio).toBe("16:9")
  })
})
