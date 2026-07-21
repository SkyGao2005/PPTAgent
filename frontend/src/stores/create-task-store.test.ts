import { beforeEach, describe, expect, it, vi } from "vitest"

const mockApi = vi.hoisted(() => ({
  uploadAttachment: vi.fn(),
  deleteAttachment: vi.fn(),
  createTask: vi.fn(),
}))

vi.mock("@/lib/api", () => ({ api: mockApi, USE_MOCK: true }))
vi.mock("sonner", () => ({
  toast: { warning: vi.fn() },
}))

import { MAX_ATTACHMENTS, useCreateTaskStore } from "@/stores/create-task-store"

function fakeFile(name: string): File {
  return new File(["x"], name)
}

beforeEach(() => {
  vi.clearAllMocks()
  mockApi.deleteAttachment.mockResolvedValue(undefined)
  useCreateTaskStore.setState(useCreateTaskStore.getInitialState(), true)
})

describe("createTask attachment lifecycle", () => {
  it("reuses successful partial uploads on retry and clears files after success", async () => {
    const store = useCreateTaskStore.getState()
    store.setTopic("季度复盘")
    store.selectTemplate("tpl", "16:9")
    store.addAttachments([fakeFile("a.pdf"), fakeFile("b.pdf")])

    mockApi.uploadAttachment
      .mockResolvedValueOnce({ attachment_id: "att-a" })
      .mockRejectedValueOnce(new Error("network"))
    await expect(store.createTask()).rejects.toThrow("network")

    mockApi.uploadAttachment.mockResolvedValueOnce({ attachment_id: "att-b" })
    mockApi.createTask.mockResolvedValue({ task_id: "t1" })
    await expect(useCreateTaskStore.getState().createTask()).resolves.toBe("t1")

    expect(mockApi.uploadAttachment).toHaveBeenCalledTimes(3)
    expect(mockApi.createTask).toHaveBeenCalledWith(
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
    await expect(store.createTask()).rejects.toThrow()

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
