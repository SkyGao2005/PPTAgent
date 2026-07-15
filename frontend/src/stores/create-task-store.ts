import { create } from "zustand"

import { api } from "@/lib/api"

interface ReferenceAttachment {
  id: string
  name: string
  size: number
  file: File
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
  setRatio: (ratio: "16:9" | "4:3") => void
  setLanguage: (language: string) => void
  setTemplateId: (templateId: string) => void
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
  setRatio: (ratio) => set({ ratio }),
  setLanguage: (language) => set({ language }),
  setTemplateId: (templateId) => set({ templateId }),
  addAttachments: (files) => {
    const room = Math.max(0, MAX_ATTACHMENTS - get().attachments.length)
    const accepted = files.slice(0, room)
    if (accepted.length) {
      set((state) => ({
        attachments: [
          ...state.attachments,
          ...accepted.map((file) => ({
            id: crypto.randomUUID(),
            name: file.name,
            size: file.size,
            file,
          })),
        ],
      }))
    }
    return accepted.length
  },
  removeAttachment: (attachmentId) =>
    set((state) => ({
      attachments: state.attachments.filter(
        (attachment) => attachment.id !== attachmentId,
      ),
    })),
  async createTask() {
    const { topic, templateId, pageCount, ratio, language, attachments } = get()
    set({ creating: true })
    try {
      const receipts = await Promise.all(
        attachments.map((attachment) => api.uploadAttachment(attachment.file)),
      )
      const snapshot = await api.createTask({
        topic: topic.trim(),
        template_id: templateId,
        page_count: pageCount,
        ratio,
        language,
        attachment_ids: receipts.map((receipt) => receipt.attachment_id),
      })
      return snapshot.task_id
    } finally {
      set({ creating: false })
    }
  },
}))
