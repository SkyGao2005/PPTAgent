import { toast } from "sonner"
import { create } from "zustand"

import { api } from "@/lib/api"
import type { GenerationEvent, TemplateSummary } from "@/types/api"

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
      set({ loadError: null })
      try {
        const templates = await api.listTemplates()
        set({ templates, loaded: true })
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
      patchTemplate(templateId, { status: "parsing", progress: 8, error: null })
      await api.retryParse(templateId)
    },

    async deleteTemplate(templateId) {
      const template = get().templates.find((item) => item.id === templateId)
      set((state) => ({
        templates: state.templates.filter((item) => item.id !== templateId),
      }))
      await api.deleteTemplate(templateId)
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
