import { toast } from "sonner"
import { create } from "zustand"

import { api } from "@/lib/api"
import { connectTemplateEvents } from "@/lib/sse"
import type { GenerationEvent, TemplateSummary } from "@/types/api"

// One app-wide subscription: parse progress must keep flowing even after
// the user leaves the templates page (the create page shows the selection).
let templateStream: (() => void) | null = null

interface TemplatesState {
  templates: TemplateSummary[]
  loaded: boolean
  loadError: string | null
  uploading: boolean
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

    async fetchTemplates() {
      templateStream ??= connectTemplateEvents((event) =>
        get().applyTemplateEvent(event),
      )
      set({ loadError: null })
      try {
        const incoming = await api.listTemplates()
        set((state) => ({
          // An SSE event may already have moved a template past the snapshot
          // this GET response was built from; never step back to "parsing".
          templates: incoming.map((template) => {
            const existing = state.templates.find((item) => item.id === template.id)
            return existing &&
              template.status === "parsing" &&
              existing.status !== "parsing"
              ? {
                  ...template,
                  status: existing.status,
                  progress: existing.progress,
                  error: existing.error,
                }
              : template
          }),
          loaded: true,
        }))
      } catch {
        set({ loadError: "无法连接服务，请检查网络后重试" })
      }
    },

    async uploadTemplate(file) {
      set({ uploading: true })
      try {
        const template = await api.uploadTemplate(file)
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
      patchTemplate(templateId, { status: "parsing", progress: 8, error: null })
      try {
        await api.retryParse(templateId)
      } catch {
        // Roll back the optimistic "parsing" state so the card does not
        // spin forever after a failed request.
        patchTemplate(templateId, {
          status: previous.status,
          progress: previous.progress,
          error: previous.error,
        })
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
