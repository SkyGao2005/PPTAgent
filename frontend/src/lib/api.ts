import type {
  AttachmentReceipt,
  ChatReceipt,
  CreateTaskPayload,
  ExportReceipt,
  SlideArtifact,
  SlideRevision,
  TaskSnapshot,
  TemplateSummary,
} from "@/types/api"

// Mock mode is a development-only affordance. Gating on DEV makes any
// `vite build` statically drop the mock branch even if VITE_USE_MOCK leaks
// into the build environment: Vite defaults NODE_ENV to "production" for
// builds regardless of --mode, and DEV follows NODE_ENV, not the mode. A
// deliberate mock demo build therefore needs NODE_ENV set explicitly:
// `NODE_ENV=development vite build --mode development`.
const USE_MOCK = import.meta.env.DEV && import.meta.env.VITE_USE_MOCK === "1"

const API_BASE_URL = import.meta.env.VITE_API_BASE_URL ?? ""

export class ApiError extends Error {
  readonly status: number

  constructor(message: string, status: number) {
    super(message)
    this.name = "ApiError"
    this.status = status
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const response = await fetch(`${API_BASE_URL}${path}`, {
    headers:
      init?.body instanceof FormData
        ? init.headers
        : { "Content-Type": "application/json", ...init?.headers },
    ...init,
  })

  if (!response.ok) {
    throw new ApiError(`Request failed: ${response.statusText}`, response.status)
  }

  // Contract allows empty bodies (204 or bare 200) on delete/retry/apply/undo.
  if (response.status === 204) {
    return undefined as T
  }
  const text = await response.text()
  return (text ? JSON.parse(text) : undefined) as T
}

interface ApiSurface {
  uploadAttachment: (file: File) => Promise<AttachmentReceipt>
  createTask: (payload: CreateTaskPayload) => Promise<TaskSnapshot>
  getTask: (taskId: string) => Promise<TaskSnapshot>
  listSlides: (taskId: string) => Promise<SlideArtifact[]>
  cancelTask: (taskId: string) => Promise<TaskSnapshot>
  resumeTask: (taskId: string) => Promise<TaskSnapshot>
  exportTask: (taskId: string, format: "pptx" | "pdf") => Promise<ExportReceipt>
  sendChat: (
    taskId: string,
    slideId: string,
    text: string,
    elementId?: string,
  ) => Promise<ChatReceipt>
  listRevisions: (taskId: string, slideId: string) => Promise<SlideRevision[]>
  applyRevision: (taskId: string, slideId: string, revision: number) => Promise<void>
  undo: (taskId: string, slideId: string) => Promise<void>
  retrySlide: (taskId: string, slideId: string) => Promise<void>
  listTemplates: () => Promise<TemplateSummary[]>
  uploadTemplate: (file: File) => Promise<TemplateSummary>
  deleteTemplate: (templateId: string) => Promise<void>
  retryParse: (templateId: string) => Promise<void>
}

const realApi: ApiSurface = {
  uploadAttachment: (file) => {
    const body = new FormData()
    body.append("file", file)
    return request("/api/attachments", { method: "POST", body })
  },
  createTask: (payload) =>
    request("/api/tasks", { method: "POST", body: JSON.stringify(payload) }),
  getTask: (taskId) => request(`/api/tasks/${taskId}`),
  listSlides: (taskId) => request(`/api/tasks/${taskId}/slides`),
  cancelTask: (taskId) => request(`/api/tasks/${taskId}/cancel`, { method: "POST" }),
  resumeTask: (taskId) => request(`/api/tasks/${taskId}/retry`, { method: "POST" }),
  exportTask: (taskId, format) =>
    request(`/api/tasks/${taskId}/export`, {
      method: "POST",
      body: JSON.stringify({ format }),
    }),
  sendChat: (taskId, slideId, text, elementId) =>
    request(`/api/tasks/${taskId}/slides/${slideId}/chat`, {
      method: "POST",
      // element_id is optional in the contract; omit it instead of sending
      // null, which strict backend models may reject with 422.
      body: JSON.stringify(elementId ? { text, element_id: elementId } : { text }),
    }),
  listRevisions: (taskId, slideId) =>
    request(`/api/tasks/${taskId}/slides/${slideId}/revisions`),
  applyRevision: (taskId, slideId, revision) =>
    request(`/api/tasks/${taskId}/slides/${slideId}/revisions/${revision}/apply`, {
      method: "POST",
    }),
  undo: (taskId, slideId) =>
    request(`/api/tasks/${taskId}/slides/${slideId}/undo`, { method: "POST" }),
  retrySlide: (taskId, slideId) =>
    request(`/api/tasks/${taskId}/slides/${slideId}/retry`, { method: "POST" }),
  listTemplates: () => request("/api/templates"),
  uploadTemplate: (file) => {
    const body = new FormData()
    body.append("file", file)
    return request("/api/templates", { method: "POST", body })
  },
  deleteTemplate: (templateId) =>
    request(`/api/templates/${templateId}`, { method: "DELETE" }),
  retryParse: (templateId) =>
    request(`/api/templates/${templateId}/retry`, { method: "POST" }),
}

async function mock<T>(run: (server: typeof import("@/mocks/mock-server")) => T): Promise<T> {
  const server = await import("@/mocks/mock-server")
  return run(server)
}

const mockApi: ApiSurface = {
  uploadAttachment: (file) => mock((server) => server.mockUploadAttachment(file)),
  createTask: (payload) => mock((server) => server.mockCreateTask(payload)),
  getTask: (taskId) => mock((server) => server.mockGetTask(taskId)),
  listSlides: (taskId) => mock((server) => server.mockListSlides(taskId)),
  cancelTask: (taskId) => mock((server) => server.mockCancelTask(taskId)),
  resumeTask: (taskId) => mock((server) => server.mockResumeTask(taskId)),
  exportTask: (taskId, format) => mock((server) => server.mockExportTask(taskId, format)),
  sendChat: (taskId, slideId, text) =>
    mock((server) => server.mockSendChat(taskId, slideId, text)),
  listRevisions: (taskId, slideId) =>
    mock((server) => server.mockListRevisions(taskId, slideId)),
  applyRevision: (taskId, slideId, revision) =>
    mock((server) => server.mockApplyRevision(taskId, slideId, revision)),
  undo: (taskId, slideId) => mock((server) => server.mockUndo(taskId, slideId)),
  retrySlide: (taskId, slideId) => mock((server) => server.mockRetrySlide(taskId, slideId)),
  listTemplates: () => mock((server) => server.mockListTemplates()),
  uploadTemplate: (file) => mock((server) => server.mockUploadTemplate(file)),
  deleteTemplate: (templateId) => mock((server) => server.mockDeleteTemplate(templateId)),
  retryParse: (templateId) => mock((server) => server.mockRetryParse(templateId)),
}

export const api: ApiSurface = USE_MOCK ? mockApi : realApi
