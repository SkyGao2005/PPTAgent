import { create } from "zustand"
import { toast } from "sonner"

import { api } from "@/lib/api"
import { createClientId } from "@/lib/utils"

interface ReferenceAttachment {
  id: string
  name: string
  size: number
  file: File
  uploadedId?: string
}

export const MAX_ATTACHMENTS = 8

interface CreateTaskState {
  topic: string
  pageCount: number
  ratio: "16:9" | "4:3"
  language: string
  templateId: string
  attachments: ReferenceAttachment[]
  creating: boolean
  setTopic: (topic: string) => void
  setPageCount: (pageCount: number) => void
  setLanguage: (language: string) => void
  selectTemplate: (templateId: string, ratio: "16:9" | "4:3") => void
  clearTemplate: () => void
  /** Returns how many files were actually added (capped at MAX_ATTACHMENTS). */
  addAttachments: (files: File[]) => number
  removeAttachment: (attachmentId: string) => void
  createTask: () => Promise<string>
}

export const useCreateTaskStore = create<CreateTaskState>((set, get) => ({
  topic: "",
  pageCount: 12,
  ratio: "16:9",
  language: "zh-CN",
  // Selected after the template list loads; never assume a backend id.
  templateId: "",
  attachments: [],
  creating: false,
  setTopic: (topic) => set({ topic }),
  setPageCount: (pageCount) => set({ pageCount }),
  setLanguage: (language) => set({ language }),
  selectTemplate: (templateId, ratio) => set({ templateId, ratio }),
  clearTemplate: () => set({ templateId: "" }),
  addAttachments: (files) => {
    const room = Math.max(0, MAX_ATTACHMENTS - get().attachments.length)
    const accepted = files.slice(0, room)
    if (accepted.length) {
      set((state) => ({
        attachments: [
          ...state.attachments,
          ...accepted.map((file) => ({
            id: createClientId(),
            name: file.name,
            size: file.size,
            file,
          })),
        ],
      }))
    }
    return accepted.length
  },
  removeAttachment: (attachmentId) => {
    const attachment = get().attachments.find((item) => item.id === attachmentId)
    set((state) => ({
      attachments: state.attachments.filter((item) => item.id !== attachmentId),
    }))
    if (attachment?.uploadedId) {
      void api.deleteAttachment(attachment.uploadedId).catch(() => {
        toast.warning("参考文件已移除，但服务端临时文件清理失败")
      })
    }
  },
  async createTask() {
    if (get().creating) {
      throw new Error("Task creation is already in progress")
    }
    const { topic, templateId, pageCount, ratio, language, attachments } = get()
    set({ creating: true })
    try {
      // Upload sequentially and retain each receipt. A retry reuses completed
      // uploads instead of creating duplicate server-side temporary objects.
      const attachmentIds: string[] = []
      for (const attachment of attachments) {
        let uploadedId = get().attachments.find((item) => item.id === attachment.id)?.uploadedId
        if (!uploadedId) {
          const receipt = await api.uploadAttachment(attachment.file)
          uploadedId = receipt.attachment_id
          set((state) => ({
            attachments: state.attachments.map((item) =>
              item.id === attachment.id ? { ...item, uploadedId } : item,
            ),
          }))
        }
        attachmentIds.push(uploadedId)
      }
      const snapshot = await api.createTask({
        topic: topic.trim(),
        template_id: templateId,
        page_count: pageCount,
        ratio,
        language,
        attachment_ids: attachmentIds,
      })
      // Release File/Blob references after a successful handoff. Returning to
      // the create page must not silently reuse the previous task's files.
      set({ attachments: [] })
      return snapshot.task_id
    } finally {
      set({ creating: false })
    }
  },
}))
