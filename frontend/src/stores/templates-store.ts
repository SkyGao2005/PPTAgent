import { toast } from "sonner"
import { create } from "zustand"

import { api } from "@/lib/api"
import { connectTemplateEvents } from "@/lib/sse"
import type { GenerationEvent, TemplateSummary } from "@/types/api"

// One app-wide subscription: parse progress must keep flowing even after
// the user leaves the templates page (the create page shows the selection).
let templateStream: (() => void) | null = null

// Ids the user deleted in this session; a stale GET snapshot taken before
// the DELETE must not resurrect them.
const deletedTemplateIds = new Set<string>()

// Per-template epoch, bumped by every SSE event and optimistic mutation.
// The GET snapshot has no watermark, so ordering is reconstructed locally:
// a template touched while a request was in flight keeps its local state
// wholesale — this covers stale ready/failed overwriting a newer SSE state
// and parse progress running backwards, not just parsing regressions.
const templateEpochs = new Map<string, number>()

function bumpTemplateEpoch(templateId: string): void {
  templateEpochs.set(templateId, (templateEpochs.get(templateId) ?? 0) + 1)
}

// Monotonic stamp per fetchTemplates() run. Uploads record the stamp they
// completed at: a snapshot whose request predates the upload keeps it, while
// a template missing from a newer snapshot was genuinely deleted (possibly
// in another tab) and gets dropped instead of lingering forever.
let fetchStamp = 0
const uploadStamps = new Map<string, number>()

interface TemplatesState {
  templates: TemplateSummary[]
  loaded: boolean
  loadError: string | null
  uploading: boolean
  /** Watermark of the shared template SSE stream; replayed events are dropped. */
  lastEventSeq: number
  fetchTemplates: () => Promise<void>
  uploadTemplate: (file: File) => Promise<void>
  retryParse: (templateId: string) => Promise<void>
  deleteTemplate: (templateId: string) => Promise<void>
  applyTemplateEvent: (event: GenerationEvent) => void
}

export const useTemplatesStore = create<TemplatesState>((set, get) => {
  function patchTemplate(templateId: string, patch: Partial<TemplateSummary>): void {
    set((state) => ({
      templates: state.templates.map((template) =>
        template.id === templateId ? { ...template, ...patch } : template,
      ),
    }))
  }

  return {
    templates: [],
    loaded: false,
    loadError: null,
    uploading: false,
    lastEventSeq: 0,

    async fetchTemplates() {
      templateStream ??= connectTemplateEvents((event) =>
        get().applyTemplateEvent(event),
      )
      const stamp = ++fetchStamp
      const epochsAtStart = new Map(templateEpochs)
      set({ loadError: null })
      try {
        const incoming = await api.listTemplates()
        // A newer fetch was issued while this one was in flight; its
        // response owns the store — an older snapshot resolving late must
        // not overwrite it (the per-template epochs only shield templates
        // that were individually touched, not the list as a whole).
        if (stamp !== fetchStamp) {
          return
        }
        set((state) => {
          const touched = (id: string): boolean =>
            (templateEpochs.get(id) ?? 0) !== (epochsAtStart.get(id) ?? 0)
          // A template touched (SSE event or optimistic mutation) while this
          // request was in flight keeps its local state; an untouched one
          // reflects server state at-or-after the request and is taken as-is.
          const merged = incoming
            .filter((template) => !deletedTemplateIds.has(template.id))
            .map((template) => {
              const existing = state.templates.find((item) => item.id === template.id)
              return existing && touched(template.id) ? existing : template
            })
          // Local-only entries survive only when their upload completed
          // during this request's flight; otherwise the server genuinely no
          // longer has them.
          const localOnly = state.templates.filter(
            (item) =>
              !incoming.some((template) => template.id === item.id) &&
              (uploadStamps.get(item.id) ?? 0) >= stamp,
          )
          return { templates: [...localOnly, ...merged], loaded: true }
        })
      } catch {
        if (stamp !== fetchStamp) {
          return
        }
        set({ loadError: "无法连接服务，请检查网络后重试" })
      }
    },

    async uploadTemplate(file) {
      set({ uploading: true })
      try {
        const template = await api.uploadTemplate(file)
        uploadStamps.set(template.id, fetchStamp)
        set((state) => ({
          templates: [template, ...state.templates.filter((item) => item.id !== template.id)],
        }))
        toast.info(`正在解析「${file.name}」`, {
          description: "解析完成后即可用于生成",
        })
      } catch {
        toast.error("模板上传失败，请重试")
      } finally {
        set({ uploading: false })
      }
    },

    async retryParse(templateId) {
      const previous = get().templates.find((item) => item.id === templateId)
      if (!previous) {
        return
      }
      // Optimistic progress starts at 0 so the server's own parse_started
      // value can only move it forward, never backwards.
      bumpTemplateEpoch(templateId)
      const epoch = templateEpochs.get(templateId)
      patchTemplate(templateId, { status: "parsing", progress: 0, error: null })
      try {
        await api.retryParse(templateId)
      } catch {
        // Roll back the optimistic "parsing" state so the card does not
        // spin forever after a failed request — but only while no SSE event
        // touched the template: a late HTTP failure must not clobber parse
        // progress that already started streaming.
        if (templateEpochs.get(templateId) === epoch) {
          bumpTemplateEpoch(templateId)
          patchTemplate(templateId, {
            status: previous.status,
            progress: previous.progress,
            error: previous.error,
          })
        }
        toast.error("重新解析请求失败，请重试")
      }
    },

    async deleteTemplate(templateId) {
      const template = get().templates.find((item) => item.id === templateId)
      try {
        await api.deleteTemplate(templateId)
      } catch {
        toast.error("删除模板失败，请重试")
        return
      }
      deletedTemplateIds.add(templateId)
      set((state) => ({
        templates: state.templates.filter((item) => item.id !== templateId),
      }))
      if (template) {
        toast.success(`已删除模板「${template.name}」`)
      }
    },

    applyTemplateEvent(event) {
      const templateId = event.payload.template_id as string | undefined
      if (!templateId) {
        return
      }
      // The template stream carries one monotonic seq; a replayed or
      // out-of-order event (reconnect replay, ready → stale progress,
      // 80% → 30%) must not run the state backwards.
      if (event.seq <= get().lastEventSeq) {
        return
      }
      set({ lastEventSeq: event.seq })
      bumpTemplateEpoch(templateId)
      switch (event.type) {
        case "template.parse_started":
          patchTemplate(templateId, { status: "parsing", progress: 3, error: null })
          break
        case "template.parse_progress":
          patchTemplate(templateId, {
            status: "parsing",
            progress: (event.payload.progress as number) ?? event.progress ?? 0,
          })
          break
        case "template.ready": {
          patchTemplate(templateId, { status: "ready", progress: 100, error: null })
          const template = get().templates.find((item) => item.id === templateId)
          if (template) {
            toast.success(`模板「${template.name}」解析完成`)
          }
          break
        }
        case "template.failed":
          patchTemplate(templateId, {
            status: "failed",
            error:
              (event.payload.reason as string | undefined) || event.message || null,
          })
          break
        default:
          break
      }
    },
  }
})
