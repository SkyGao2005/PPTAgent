import { beforeEach, describe, expect, it, vi } from "vitest"

vi.mock("@/lib/api", () => ({ api: {}, USE_MOCK: true }))

import { MAX_ATTACHMENTS, useCreateTaskStore } from "@/stores/create-task-store"

function fakeFile(name: string): File {
  return new File(["x"], name)
}

beforeEach(() => {
  useCreateTaskStore.setState(useCreateTaskStore.getInitialState(), true)
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
