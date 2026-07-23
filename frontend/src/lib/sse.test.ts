import { afterEach, beforeEach, describe, expect, it, vi } from "vitest"

import {
  connectOutlineEvents,
  connectTaskEvents,
  connectTemplateEvents,
} from "@/lib/sse"

class FakeEventSource {
  static instances: FakeEventSource[] = []

  onopen: (() => void) | null = null
  onerror: (() => void) | null = null
  onmessage: ((event: MessageEvent<string>) => void) | null = null
  close = vi.fn()
  readonly url: string

  constructor(url: string) {
    this.url = url
    FakeEventSource.instances.push(this)
  }
}

beforeEach(() => {
  FakeEventSource.instances = []
  vi.stubGlobal("EventSource", FakeEventSource)
})

afterEach(() => {
  vi.unstubAllGlobals()
})

describe("SSE reconnect watermarks", () => {
  it("includes the task snapshot sequence and reports connection changes", () => {
    const statuses: string[] = []
    const dispose = connectTaskEvents("task 1", 42, vi.fn(), (status) =>
      statuses.push(status),
    )
    const source = FakeEventSource.instances[0]

    expect(source.url).toContain("/api/tasks/task%201/events?last_seq=42")
    expect(statuses).toEqual(["connecting"])
    source.onopen?.()
    source.onerror?.()
    expect(statuses).toEqual(["connecting", "open", "reconnecting"])

    dispose()
    expect(source.close).toHaveBeenCalledOnce()
  })

  it("includes the template stream sequence so initial/reconnect gaps can replay", () => {
    const dispose = connectTemplateEvents(17, vi.fn())
    expect(FakeEventSource.instances[0].url).toContain(
      "/api/templates/events?last_seq=17",
    )
    dispose()
    expect(FakeEventSource.instances[0].close).toHaveBeenCalledOnce()
  })

  it("connects manuscript review to its outline event stream", () => {
    const dispose = connectOutlineEvents("outline 1", 9, vi.fn(), vi.fn())
    expect(FakeEventSource.instances[0].url).toContain(
      "/api/outlines/outline%201/events?last_seq=9",
    )
    dispose()
    expect(FakeEventSource.instances[0].close).toHaveBeenCalledOnce()
  })
})
