import { useCallback, useEffect, useMemo, useState } from "react"
import { useNavigate } from "react-router-dom"
import { useDropzone } from "react-dropzone"
import { LoaderCircleIcon, SearchIcon, TriangleAlertIcon, UploadIcon } from "lucide-react"
import { toast } from "sonner"

import { AppShell } from "@/components/app-shell"
import { TemplateCover } from "@/components/template-cover"
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Badge } from "@/components/ui/badge"
import { Button } from "@/components/ui/button"
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog"
import { Input } from "@/components/ui/input"
import { cn } from "@/lib/utils"
import { usePageMetadata } from "@/lib/use-page-metadata"
import { useCreateTaskStore } from "@/stores/create-task-store"
import { useTemplatesStore } from "@/stores/templates-store"
import type { TemplateStatus, TemplateSummary } from "@/types/api"

type StatusFilter = "all" | TemplateStatus

const statusFilters: Array<{ value: StatusFilter; label: string }> = [
  { value: "all", label: "全部" },
  { value: "ready", label: "已就绪" },
  { value: "parsing", label: "解析中" },
  { value: "failed", label: "解析失败" },
]

function TemplateStatusBadge({ status }: { status: TemplateStatus }) {
  if (status === "ready") {
    return (
      <span className="rounded-full bg-success-subtle px-2.5 py-0.5 text-[11px] font-bold text-success">
        已就绪
      </span>
    )
  }
  if (status === "parsing") {
    return (
      <span className="rounded-full bg-accent px-2.5 py-0.5 text-[11px] font-bold text-accent-foreground">
        解析中
      </span>
    )
  }
  return (
    <span className="rounded-full bg-destructive/10 px-2.5 py-0.5 text-[11px] font-bold text-destructive">
      解析失败
    </span>
  )
}

export function TemplatesPage() {
  usePageMetadata("模板库")
  const navigate = useNavigate()
  const setTemplateId = useCreateTaskStore((state) => state.setTemplateId)
  const selectedTemplateId = useCreateTaskStore((state) => state.templateId)
  const templates = useTemplatesStore((state) => state.templates)
  const loaded = useTemplatesStore((state) => state.loaded)
  const loadError = useTemplatesStore((state) => state.loadError)
  const uploading = useTemplatesStore((state) => state.uploading)
  const [query, setQuery] = useState("")
  const [statusFilter, setStatusFilter] = useState<StatusFilter>("all")
  const [previewId, setPreviewId] = useState<string | null>(null)
  const [deleteId, setDeleteId] = useState<string | null>(null)
  const [deleting, setDeleting] = useState(false)

  // §5.2: upload by click or by dragging a .pptx anywhere onto the page.
  const onDrop = useCallback((accepted: File[], rejected: unknown[]) => {
    if (useTemplatesStore.getState().uploading) {
      return
    }
    if (rejected.length) {
      toast.error("仅支持 .pptx / .ppt 模板文件")
    }
    const file = accepted[0]
    if (file) {
      void useTemplatesStore.getState().uploadTemplate(file)
    }
  }, [])

  const {
    getRootProps,
    getInputProps,
    isDragActive,
    open: openFilePicker,
  } = useDropzone({
    onDrop,
    noClick: true,
    noKeyboard: true,
    disabled: uploading,
    multiple: false,
    accept: {
      "application/vnd.openxmlformats-officedocument.presentationml.presentation": [
        ".pptx",
      ],
      "application/vnd.ms-powerpoint": [".ppt"],
    },
  })

  // The template SSE stream lives in the store (app-wide), so leaving this
  // page keeps parse progress flowing; here we only refresh the list.
  useEffect(() => {
    void useTemplatesStore.getState().fetchTemplates()
  }, [])

  const counts = useMemo(
    () => ({
      all: templates.length,
      ready: templates.filter((template) => template.status === "ready").length,
      parsing: templates.filter((template) => template.status === "parsing").length,
      failed: templates.filter((template) => template.status === "failed").length,
    }),
    [templates],
  )

  const visibleTemplates = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase()
    return templates.filter((template) => {
      const matchesQuery =
        !normalizedQuery ||
        `${template.name} ${template.description}`.toLowerCase().includes(normalizedQuery)
      const matchesStatus = statusFilter === "all" || template.status === statusFilter
      return matchesQuery && matchesStatus
    })
  }, [query, statusFilter, templates])

  const previewTemplate = templates.find((template) => template.id === previewId) ?? null
  const deleteTemplate = templates.find((template) => template.id === deleteId) ?? null

  function applyTemplate(template: TemplateSummary): void {
    if (template.status !== "ready") {
      return
    }
    setTemplateId(template.id)
    toast.success(`已选择「${template.name}」`)
    navigate("/")
  }

  return (
    <AppShell>
      <main id="main-content" tabIndex={-1} className="outline-none">
        <div
          {...getRootProps({
          className:
            "animate-fade-up relative mx-auto w-full max-w-[1240px] px-6 py-10 lg:px-10",
          })}
        >
        <input {...getInputProps()} />
        {isDragActive && (
          <div className="pointer-events-none absolute inset-3 z-20 flex items-center justify-center rounded-2xl border-2 border-dashed border-primary bg-accent/80 text-sm font-bold text-primary">
            松开即可上传 .pptx 模板
          </div>
        )}
        <div className="flex flex-col justify-between gap-5 sm:flex-row sm:items-end">
          <div>
            <h1 className="font-heading text-[26px] font-bold tracking-tight">模板库</h1>
            <p className="mt-1.5 text-sm text-hint">
              {counts.all} 个模板 · {counts.ready} 个已就绪
            </p>
          </div>
          <Button
            size="lg"
            className="h-10 rounded-[10px] px-5"
            disabled={uploading}
            onClick={openFilePicker}
          >
            {uploading ? <LoaderCircleIcon className="animate-spin" /> : <UploadIcon />}
            {uploading ? "上传中" : "上传模板"}
          </Button>
        </div>

        <div className="mt-7 flex flex-wrap items-center gap-3">
          <div className="relative w-64">
            <SearchIcon className="pointer-events-none absolute top-1/2 left-3 size-4 -translate-y-1/2 text-hint" />
            <Input
              value={query}
              aria-label="搜索模板名称"
              onChange={(event) => setQuery(event.target.value)}
              className="h-9.5 rounded-[10px] bg-card pl-9"
              placeholder="搜索模板名称…"
            />
          </div>
          <div className="flex gap-1.5">
            {statusFilters.map((filter) => {
              const active = statusFilter === filter.value
              return (
                <button
                  key={filter.value}
                  type="button"
                  aria-pressed={active}
                  className={cn(
                    "rounded-full border px-3.5 py-1.5 text-[13px] font-medium transition-colors",
                    active
                      ? "border-primary bg-accent text-primary"
                      : "border-border bg-card text-muted-foreground hover:border-input",
                  )}
                  onClick={() => setStatusFilter(filter.value)}
                >
                  {filter.label} {counts[filter.value]}
                </button>
              )
            })}
          </div>
        </div>

        {loadError ? (
          <div role="alert" className="mt-12 flex flex-col items-center justify-center rounded-2xl border border-dashed px-6 py-16 text-center">
            <TriangleAlertIcon className="size-8 text-hint" />
            <h2 className="mt-4 text-[15px] font-medium">模板列表加载失败</h2>
            <p className="mt-1 text-[13px] text-hint">{loadError}</p>
            <Button
              variant="outline"
              size="sm"
              className="mt-5"
              onClick={() => void useTemplatesStore.getState().fetchTemplates()}
            >
              重新加载
            </Button>
          </div>
        ) : !loaded ? (
          <div className="mt-6 grid gap-5 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {Array.from({ length: 8 }, (_, index) => (
              <div key={index} className="skeleton-shimmer aspect-[4/3] rounded-[14px]" />
            ))}
          </div>
        ) : visibleTemplates.length ? (
          <div className="mt-6 grid gap-5 sm:grid-cols-2 lg:grid-cols-3 xl:grid-cols-4">
            {visibleTemplates.map((template) => {
              const isCurrent = template.id === selectedTemplateId
              return (
                // div[role=button] keeps the card's block-level content valid
                // HTML; retry for failed templates lives in the preview
                // dialog, so no interactive element is nested here.
                <div
                  key={template.id}
                  role="button"
                  tabIndex={0}
                  aria-label={`查看模板「${template.name}」，${template.description || "无描述"}，${template.slides} 页，${template.ratio}，状态：${template.status === "ready" ? "已就绪" : template.status === "parsing" ? "解析中" : "解析失败"}${isCurrent ? "，当前使用" : ""}`}
                  className={cn(
                    "overflow-hidden rounded-[14px] border bg-card text-left transition-all hover:-translate-y-0.5 hover:shadow-[0_6px_20px_rgba(30,25,15,0.10)]",
                    isCurrent && "border-primary ring-3 ring-primary/10",
                  )}
                  onClick={() => setPreviewId(template.id)}
                  onKeyDown={(event) => {
                    if (event.key === "Enter" || event.key === " ") {
                      event.preventDefault()
                      setPreviewId(template.id)
                    }
                  }}
                >
                  <div className="relative">
                    <TemplateCover template={template} showStatus />
                    {isCurrent && (
                      <span className="absolute top-2.5 left-2.5 rounded-full bg-primary px-2.5 py-1 text-[11px] font-bold text-primary-foreground">
                        当前使用
                      </span>
                    )}
                  </div>
                  <div className="px-4 pt-3.5 pb-4">
                    <div className="mb-1.5 flex items-center justify-between gap-3">
                      <span className="truncate text-[15px] font-bold">{template.name}</span>
                      <span className="flex shrink-0 gap-1">
                        {[
                          template.palette.bg,
                          template.palette.primary,
                          template.palette.accent,
                          template.palette.ink,
                        ].map((color, index) => (
                          <span
                            key={index}
                            className="inline-block size-3 rounded-full border border-black/10"
                            style={{ backgroundColor: color }}
                          />
                        ))}
                      </span>
                    </div>
                    <p className="mb-2.5 line-clamp-2 min-h-[38px] text-xs leading-relaxed text-hint">
                      {template.description}
                    </p>
                    <div className="flex items-center gap-2 text-xs text-muted-foreground">
                      <span>{template.slides} 页</span>
                      <span className="text-border">·</span>
                      <span>{template.ratio}</span>
                      <span className="text-border">·</span>
                      <span>{template.layouts.length} 种版式</span>
                      <span className="ml-auto">
                        <TemplateStatusBadge status={template.status} />
                      </span>
                    </div>
                  </div>
                </div>
              )
            })}
          </div>
        ) : (
          <div className="mt-12 flex flex-col items-center justify-center rounded-2xl border border-dashed px-6 py-16 text-center">
            <div className="h-10 w-14 rounded-lg border-2 border-dashed border-input" />
            <h2 className="mt-4 text-[15px] font-medium">没有匹配的模板</h2>
            <p className="mt-1 text-[13px] text-hint">
              换个关键词试试，或上传你自己的 PPTX 模板
            </p>
            <Button
              variant="outline"
              size="sm"
              className="mt-5"
              onClick={() => {
                setQuery("")
                setStatusFilter("all")
              }}
            >
              清除筛选
            </Button>
          </div>
        )}
        </div>
      </main>

      <Dialog open={previewTemplate !== null} onOpenChange={(open) => !open && setPreviewId(null)}>
        {previewTemplate && (
          <DialogContent className="max-h-[90svh] overflow-y-auto rounded-2xl sm:max-w-[840px]">
            <DialogHeader>
              <DialogTitle className="text-xl">{previewTemplate.name}</DialogTitle>
              <DialogDescription>{previewTemplate.description}</DialogDescription>
            </DialogHeader>
            <div className="grid gap-7 md:grid-cols-[1.3fr_1fr]">
              <div>
                <div className="overflow-hidden rounded-xl border">
                  <TemplateCover template={previewTemplate} showStatus />
                </div>
                <h3 className="mt-4 mb-2 text-[13px] font-bold text-muted-foreground">
                  包含版式 · {previewTemplate.layouts.length}
                </h3>
                <div className="flex flex-wrap gap-1.5">
                  {previewTemplate.layouts.map((layout) => (
                    <Badge key={layout} variant="secondary" className="h-6 px-3">
                      {layout}
                    </Badge>
                  ))}
                </div>
              </div>
              <div className="flex flex-col">
                <div className="grid grid-cols-2 gap-3">
                  <div className="rounded-[10px] bg-muted px-3.5 py-3">
                    <div className="mb-1 text-[11px] text-hint">母版页数</div>
                    <div className="font-heading text-[17px] font-bold">
                      {previewTemplate.slides}
                    </div>
                  </div>
                  <div className="rounded-[10px] bg-muted px-3.5 py-3">
                    <div className="mb-1 text-[11px] text-hint">画面比例</div>
                    <div className="font-heading text-[17px] font-bold">
                      {previewTemplate.ratio}
                    </div>
                  </div>
                </div>
                <div className="mt-5">
                  <div className="mb-2 text-[11px] text-hint">配色方案</div>
                  <div className="flex gap-2">
                    {[
                      previewTemplate.palette.bg,
                      previewTemplate.palette.primary,
                      previewTemplate.palette.accent,
                      previewTemplate.palette.ink,
                    ].map((color, index) => (
                      <span
                        key={index}
                        className="size-7 rounded-lg border border-black/10"
                        style={{ backgroundColor: color }}
                        title={color}
                      />
                    ))}
                  </div>
                </div>
                <div className="mt-5 flex items-center gap-2">
                  <span className="text-[11px] text-hint">状态</span>
                  <TemplateStatusBadge status={previewTemplate.status} />
                </div>
                <div className="mt-auto flex flex-col gap-2.5 pt-6">
                  {previewTemplate.status === "ready" ? (
                    <Button
                      className="h-11 rounded-xl text-[15px] font-bold"
                      onClick={() => applyTemplate(previewTemplate)}
                    >
                      使用此模板并返回创建
                    </Button>
                  ) : previewTemplate.status === "failed" ? (
                    <Button
                      className="h-11 rounded-xl text-[15px] font-bold"
                      onClick={() =>
                        void useTemplatesStore.getState().retryParse(previewTemplate.id)
                      }
                    >
                      重新解析
                    </Button>
                  ) : (
                    <p className="text-xs text-hint">模板解析成功后才能用于生成。</p>
                  )}
                  <Button
                    variant="outline"
                    className="h-10 rounded-xl text-[13px] text-hint hover:border-destructive/50 hover:text-destructive"
                    onClick={() => {
                      setPreviewId(null)
                      setDeleteId(previewTemplate.id)
                    }}
                  >
                    删除模板
                  </Button>
                </div>
              </div>
            </div>
          </DialogContent>
        )}
      </Dialog>

      <AlertDialog
        open={deleteTemplate !== null}
        onOpenChange={(open) => !open && !deleting && setDeleteId(null)}
      >
        <AlertDialogContent className="sm:max-w-sm">
          <AlertDialogHeader>
            <AlertDialogTitle>删除模板？</AlertDialogTitle>
            <AlertDialogDescription>
              {deleteTemplate
                ? `「${deleteTemplate.name}」将从模板库永久删除，此操作无法撤销。`
                : "此操作无法撤销。"}
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <Button
              variant="outline"
              disabled={deleting}
              onClick={() => setDeleteId(null)}
            >
              取消
            </Button>
            <Button
              variant="destructive"
              disabled={deleting || !deleteTemplate}
              onClick={() => {
                if (!deleteTemplate) {
                  return
                }
                setDeleting(true)
                void useTemplatesStore
                  .getState()
                  .deleteTemplate(deleteTemplate.id)
                  .then((deleted) => {
                    if (deleted) {
                      setDeleteId(null)
                    }
                  })
                  .finally(() => setDeleting(false))
              }}
            >
              {deleting && <LoaderCircleIcon className="animate-spin" />}
              确认删除
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </AppShell>
  )
}
