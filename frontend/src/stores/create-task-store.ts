import { create } from "zustand"

import { api } from "@/lib/api"

interface ReferenceAttachment {
  id: string
  name: string
  size: number
  file: File
}

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
  addAttachments: (files: File[]) => void
  removeAttachment: (attachmentId: string) => void
  createTask: () => Promise<string>
}

export const useCreateTaskStore = create<CreateTaskState>((set, get) => ({
  topic: "",
  pageCount: 12,
  ratio: "16:9",
  language: "zh-CN",
  templateId: "obsidian",
  attachments: [],
  creating: false,
  setTopic: (topic) => set({ topic }),
  setPageCount: (pageCount) => set({ pageCount }),
  setRatio: (ratio) => set({ ratio }),
  setLanguage: (language) => set({ language }),
  setTemplateId: (templateId) => set({ templateId }),
  addAttachments: (files) =>
    set((state) => ({
      attachments: [
        ...state.attachments,
        ...files.map((file) => ({
          id: crypto.randomUUID(),
          name: file.name,
          size: file.size,
          file,
        })),
      ].slice(0, 8),
    })),
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
