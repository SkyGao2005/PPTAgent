// SSE client (§7). In mock mode events come from the in-memory mock server;
// in real mode a native EventSource connects to the FastAPI endpoint and the
// server replays missed events based on `last_seq`.

import type { GenerationEvent } from "@/types/api"
import { resolveApiUrl } from "@/lib/api-url"

// Computed locally (not imported from api.ts) so Rollup can fold the DEV
// branch per-module and drop the mock chunks from production builds.
const USE_MOCK = import.meta.env.DEV && import.meta.env.VITE_USE_MOCK === "1"

export type ConnectionState = "connecting" | "open" | "reconnecting"

export function connectTaskEvents(
  taskId: string,
  lastSeq: number,
  onEvent: (event: GenerationEvent) => void,
  onStatusChange: (state: ConnectionState) => void,
): () => void {
  onStatusChange("connecting")

  if (USE_MOCK) {
    let unsubscribe: (() => void) | null = null
    let closed = false
    void import("@/mocks/mock-server").then((server) => {
      if (closed) {
        return
      }
      unsubscribe = server.mockSubscribeTask(taskId, lastSeq, onEvent)
      onStatusChange("open")
    })
    return () => {
      closed = true
      unsubscribe?.()
    }
  }

  const params = new URLSearchParams({ last_seq: String(lastSeq) })
  const source = new EventSource(
    `${resolveApiUrl(`/api/tasks/${encodeURIComponent(taskId)}/events`)}?${params}`,
  )

  source.onopen = () => onStatusChange("open")
  source.onerror = () => onStatusChange("reconnecting")
  source.onmessage = (message) => {
    onEvent(JSON.parse(message.data) as GenerationEvent)
  }

  return () => source.close()
}

export function connectTemplateEvents(
  lastSeq: number,
  onEvent: (event: GenerationEvent) => void,
): () => void {
  if (USE_MOCK) {
    let unsubscribe: (() => void) | null = null
    let closed = false
    void import("@/mocks/mock-server").then((server) => {
      if (closed) {
        return
      }
      unsubscribe = server.mockSubscribeTemplates(lastSeq, onEvent)
    })
    return () => {
      closed = true
      unsubscribe?.()
    }
  }

  const params = new URLSearchParams({ last_seq: String(lastSeq) })
  const source = new EventSource(`${resolveApiUrl("/api/templates/events")}?${params}`)
  source.onmessage = (message) => {
    onEvent(JSON.parse(message.data) as GenerationEvent)
  }
  return () => source.close()
}
