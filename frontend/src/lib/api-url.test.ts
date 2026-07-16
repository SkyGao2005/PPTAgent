import { describe, expect, it } from "vitest"

import { resolveWithApiBase } from "@/lib/api-url"

describe("resolveWithApiBase", () => {
  it("routes relative REST and artifact paths through the configured backend", () => {
    expect(resolveWithApiBase("/api/tasks/t1", "https://api.example.com/")).toBe(
      "https://api.example.com/api/tasks/t1",
    )
    expect(resolveWithApiBase("previews/s1.png", "https://api.example.com/base")).toBe(
      "https://api.example.com/base/previews/s1.png",
    )
  })

  it("does not rewrite absolute data, blob, or http URLs", () => {
    expect(resolveWithApiBase("https://cdn.example.com/s1.png", "https://api.test")).toBe(
      "https://cdn.example.com/s1.png",
    )
    expect(resolveWithApiBase("blob:preview", "https://api.test")).toBe("blob:preview")
    expect(resolveWithApiBase("data:image/png;base64,x", "https://api.test")).toBe(
      "data:image/png;base64,x",
    )
  })

  it("anchors backend-relative artifact paths at the same-origin root", () => {
    expect(resolveWithApiBase("previews/s1.png", "")).toBe("/previews/s1.png")
  })
})
