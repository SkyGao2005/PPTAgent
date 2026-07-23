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

/**
 * Chosen deliberately, as opposed to `""` which means nothing is chosen yet.
 *
 * The two states have to stay apart: an empty selection keeps the submit
 * button disabled so a template is never skipped by accident, while this one
 * enables it and tells the backend to derive the visual system from the
 * manuscript instead of from a template.
 */
export const NO_TEMPLATE = "__no_template__"

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
  setRatio: (ratio: "16:9" | "4:3") => void
  selectTemplate: (templateId: string, ratio: "16:9" | "4:3") => void
  useNoTemplate: () => void
  clearTemplate: () => void
  /** Returns how many files were actually added (capped at MAX_ATTACHMENTS). */
  addAttachments: (files: File[]) => number
  removeAttachment: (attachmentId: string) => void
  createOutline: () => Promise<string>
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
  setRatio: (ratio) => set({ ratio }),
  selectTemplate: (templateId, ratio) => set({ templateId, ratio }),
  // Dropping the template drops what dictated the canvas with it. Keeping the
  // old template's ratio would leave a 4:3 deck behind with nothing on screen
  // explaining why, so the choice resets to the default and stays editable.
  useNoTemplate: () => set({ templateId: NO_TEMPLATE, ratio: "16:9" }),
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
  async createOutline() {
    if (get().creating) {
      throw new Error("Outline creation is already in progress")
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
      const outline = await api.createOutline({
        topic: topic.trim(),
        template_id: templateId === NO_TEMPLATE ? null : templateId,
        page_count: pageCount,
        ratio,
        language,
        attachment_ids: attachmentIds,
      })
      // Release File/Blob references after a successful handoff. Returning to
      // the create page must not silently reuse the previous outline's files.
      set({ attachments: [] })
      return outline.outline_id
    } finally {
      set({ creating: false })
    }
  },
}))
