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
  /** Immutable Template IR revision used by bundled and parsed templates. */
  revision_id?: string
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
  /** First rendered page from the pinned Template IR revision. */
  thumbnail_url?: string | null
}

export type TaskStatus =
  | "queued"
  | "running"
  | "completed"
  | "failed"
  | "cancelled"

export type TaskStage =
  | "template"
  | "prepare"
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
  manuscript_approved?: boolean
  total_slides: number
  last_seq: number
  created_at: string
  updated_at: string
  generation_params?: {
    num_pages?: string | number | null
  }
}

export interface TaskHistoryItem {
  task_id: string
  topic: string
  instruction: string
  status: TaskStatus
  stage: TaskStage | null
  progress: number
  template_id: string
  ratio: "16:9" | "4:3"
  total_slides: number
  completed_slides: number
  failed_slides: number
  preview_url: string | null
  created_at: string
  updated_at: string
}

export interface TaskHistoryResponse {
  tasks: TaskHistoryItem[]
  total: number
}

export interface CreateTaskPayload {
  topic: string
  /** Null asks the backend to derive the visual system from the manuscript. */
  template_id: string | null
  page_count: number
  ratio: "16:9" | "4:3"
  language: string
  attachment_ids: string[]
}

export type OutlineStatus = "generating" | "ready" | "failed" | "approved"

export interface OutlineComment {
  comment_id: string
  text: string
  target_revision: number
  created_at: string
}

export interface OutlineDraft {
  outline_id: string
  status: OutlineStatus
  topic: string
  page_count: number
  ratio: "16:9" | "4:3"
  template_id: string | null
  revision: number
  last_seq: number
  manuscript: string
  comments: OutlineComment[]
  task_id: string | null
  error_message: string | null
  created_at: string
  updated_at: string
}

export interface OutlineApproval {
  task_id: string
  outline_id: string
  status: string
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
  task_id: string
  format: "pptx" | "pdf"
  artifact_path: string
  artifact_url: string
  download_url: string
  filename: string
  status: "completed"
}
