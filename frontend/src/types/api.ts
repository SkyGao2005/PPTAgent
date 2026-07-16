export type TemplateStatus = "ready" | "parsing" | "failed"

export type TemplateOwner = "system" | "user"

export interface TemplatePalette {
  bg: string
  surface: string
  primary: string
  accent: string
  ink: string
  dark: boolean
}

export interface TemplateSummary {
  id: string
  name: string
  description: string
  owner: TemplateOwner
  status: TemplateStatus
  progress?: number
  /** Failure reason when status === "failed", from template.failed events. */
  error?: string | null
  slides: number
  ratio: "16:9" | "4:3"
  layouts: string[]
  palette: TemplatePalette
}

export type TaskStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"

export type TaskStage =
  | "template"
  | "plan"
  | "research"
  | "generate"
  | "edit"
  | "export"

export type EventStatus =
  | "queued"
  | "running"
  | "succeeded"
  | "failed"
  | "cancelled"

export interface GenerationEvent {
  task_id: string
  seq: number
  type: string
  stage: TaskStage
  status: EventStatus
  progress: number | null
  message: string
  slide_id: string | null
  slide_index: number | null
  total_slides: number | null
  artifact_url: string | null
  created_at: string
  payload: Record<string, unknown>
}

export type SlideStatus =
  | "queued"
  | "generating"
  | "completed"
  | "editing"
  | "failed"

export interface SlideArtifact {
  slide_id: string
  task_id: string
  index: number
  title: string
  summary: string
  status: SlideStatus
  mode: "html" | "template"
  revision: number
  preview_url: string | null
}

export interface TaskSnapshot {
  task_id: string
  topic: string
  status: TaskStatus
  stage: TaskStage
  progress: number
  template_id: string
  ratio: "16:9" | "4:3"
  total_slides: number
  last_seq: number
  created_at: string
  updated_at: string
}

export interface CreateTaskPayload {
  topic: string
  template_id: string
  page_count: number
  ratio: "16:9" | "4:3"
  language: string
  attachment_ids: string[]
}

export interface SlideRevision {
  revision: number
  label: string
  created_at: string
  preview_url: string | null
}

export interface ChatReceipt {
  chat_id: string
}

export interface AttachmentReceipt {
  attachment_id: string
}

export interface ExportReceipt {
  export_id: string
}
