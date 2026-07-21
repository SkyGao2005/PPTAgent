import { useCallback, useEffect, useMemo, useState } from "react"
import { useNavigate } from "react-router-dom"
import { useDropzone } from "react-dropzone"
import {
  ArrowRightIcon,
  LoaderCircleIcon,
  PlusIcon,
  RotateCcwIcon,
  SearchIcon,
  Trash2Icon,
  TriangleAlertIcon,
  UploadIcon,
} from "lucide-react"
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
import { useFlipLayout } from "@/lib/use-flip-layout"
import { cn } from "@/lib/utils"
import { usePageMetadata } from "@/lib/use-page-metadata"
import { usePresenceList } from "@/lib/use-presence-list"
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

const templateKey = (template: TemplateSummary): string => template.id

function TemplateStatusBadge({ status }: { status: TemplateStatus }) {
  if (status === "ready") {
    return (
      <span
        key={status}
        className="template-status-badge rounded-full bg-success-subtle px-2.5 py-0.5 text-[11px] font-bold text-success"
      >
        已就绪
      </span>
    )
  }
  if (status === "parsing") {
    return (
      <span
        key={status}
        className="template-status-badge rounded-full bg-accent px-2.5 py-0.5 text-[11px] font-bold text-accent-foreground"
      >
        解析中
      </span>
    )
  }
  return (
    <span
      key={status}
      className="template-status-badge rounded-full bg-destructive/10 px-2.5 py-0.5 text-[11px] font-bold text-destructive"
    >
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
  const [presenceMotionReady, setPresenceMotionReady] = useState(false)

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

  useEffect(() => {
    if (!loaded) {
      return
    }
    const frame = window.requestAnimationFrame(() => setPresenceMotionReady(true))
    return () => window.cancelAnimationFrame(frame)
  }, [loaded])

  const counts = useMemo(
    () => ({
      all: templates.length,
      ready: templates.filter((template) => template.status === "ready").length,
      parsing: templates.filter((template) => template.status === "parsing").length,
      failed: templates.filter((template) => template.status === "failed").length,
    }),
    [templates],
  )

  const templateEntries = usePresenceList(templates, templateKey, 160)
  const visibleTemplateEntries = useMemo(() => {
    const normalizedQuery = query.trim().toLowerCase()
    return templateEntries.filter((entry) => {
      const template = entry.value
      const matchesQuery =
        !normalizedQuery ||
        `${template.name} ${template.description}`.toLowerCase().includes(normalizedQuery)
      const matchesStatus = statusFilter === "all" || template.status === statusFilter
      return matchesQuery && matchesStatus
    })
  }, [query, statusFilter, templateEntries])
  const templateLayoutKeys = [
    ...visibleTemplateEntries.map((entry) => entry.key),
    "__upload-template__",
  ]
  const templateMotionToken = templateEntries.map((entry) => entry.key).join("|")
  const registerTemplateNode = useFlipLayout(templateLayoutKeys, templateMotionToken)

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
              "animate-fade-up relative mx-auto w-full max-w-[1080px] px-6 pt-2 pb-20 lg:px-8",
          })}
        >
          <input {...getInputProps()} />
          <div
            data-visible={isDragActive}
            aria-hidden={!isDragActive}
            className="drop-target-overlay pointer-events-none absolute inset-3 z-20 flex items-center justify-center rounded-[28px] border-2 border-dashed border-primary bg-white/80 text-sm font-bold"
          >
            松开即可上传 .pptx 模板
          </div>
          <div className="flex flex-col justify-between gap-5 sm:flex-row sm:items-end">
            <div>
              <h1 className="font-serif text-[34px] font-black tracking-tight">模板库</h1>
              <p className="mt-1.5 text-sm text-hint">
                {counts.all} 个模板 · {counts.ready} 个已就绪 · 上传 PPTX 即可解析为新模板
              </p>
            </div>
            <Button
              size="lg"
              className="h-10.5 rounded-full px-5 text-sm font-semibold shadow-[0_10px_26px_rgba(27,28,32,0.28)]"
              disabled={uploading}
              onClick={openFilePicker}
            >
              {uploading ? <LoaderCircleIcon className="animate-spin" /> : <UploadIcon />}
              {uploading ? "上传中" : "上传模板"}
            </Button>
          </div>

          <div className="mt-7 flex flex-wrap items-center gap-3">
            <div className="relative w-64">
              <SearchIcon className="pointer-events-none absolute top-1/2 left-3.5 size-4 -translate-y-1/2 text-hint" />
              <Input
                value={query}
                aria-label="搜索模板名称"
                onChange={(event) => setQuery(event.target.value)}
                className="glass-chip h-9.5 rounded-full border-white/60 pl-10"
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
                      "rounded-full px-4 py-2 text-[13px] font-medium transition-colors",
                      active
                        ? "bg-primary font-semibold text-primary-foreground"
                        : "glass-chip text-muted-foreground hover:bg-white/70 hover:text-foreground",
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
            <div
              role="alert"
              className="template-grid-resolved glass-card mt-10 flex flex-col items-center justify-center rounded-[24px] px-6 py-16 text-center"
            >
              <TriangleAlertIcon className="size-8 text-hint" />
              <h2 className="mt-4 text-[15px] font-medium">模板列表加载失败</h2>
              <p className="mt-1 text-[13px] text-hint">{loadError}</p>
              <Button
                variant="outline"
                size="sm"
                className="mt-5 rounded-full bg-white/60"
                onClick={() => void useTemplatesStore.getState().fetchTemplates()}
              >
                重新加载
              </Button>
            </div>
          ) : !loaded ? (
            <div className="mt-6 grid gap-[18px] sm:grid-cols-2 lg:grid-cols-3">
              {Array.from({ length: 6 }, (_, index) => (
                <div key={index} className="skeleton-shimmer aspect-[4/3] rounded-[20px]" />
              ))}
            </div>
          ) : (
            <div className="template-grid-resolved">
              {visibleTemplateEntries.length ? (
                <div className="mt-6 grid gap-[18px] sm:grid-cols-2 lg:grid-cols-3">
              {visibleTemplateEntries.map((entry) => {
                const template = entry.value
                const isCurrent = template.id === selectedTemplateId
                const cardPhase = presenceMotionReady ? entry.phase : "present"
                return (
                  <div
                    key={entry.key}
                    ref={(node) => registerTemplateNode(entry.key, node)}
                  >
                    <div
                      data-phase={cardPhase}
                      className="template-card-presence"
                    >
                      {/* div[role=button] keeps the card's block-level content valid
                          HTML; retry for failed templates lives in the preview dialog. */}
                      <div
                        role="button"
                        tabIndex={cardPhase === "exiting" ? -1 : 0}
                        aria-hidden={cardPhase === "exiting"}
                        aria-label={`查看模板「${template.name}」，${template.description || "无描述"}，${template.slides} 页，${template.ratio}，状态：${template.status === "ready" ? "已就绪" : template.status === "parsing" ? "解析中" : "解析失败"}${isCurrent ? "，当前使用" : ""}`}
                        className={cn(
                          "glass-card overflow-hidden rounded-[20px] text-left",
                          isCurrent && "ring-2 ring-primary",
                        )}
                        onClick={() => {
                          if (cardPhase !== "exiting") {
                            setPreviewId(template.id)
                          }
                        }}
                        onKeyDown={(event) => {
                          if (
                            cardPhase !== "exiting" &&
                            (event.key === "Enter" || event.key === " ")
                          ) {
                            event.preventDefault()
                            setPreviewId(template.id)
                          }
                        }}
                      >
                        <div className="relative m-2 mb-0 overflow-hidden rounded-xl shadow-[inset_0_0_0_1px_rgba(27,28,32,0.06)]">
                          <TemplateCover template={template} showStatus />
                          {isCurrent && (
                            <span className="absolute top-2 left-2 rounded-full bg-primary px-2.5 py-1 text-[11px] font-bold text-primary-foreground">
                              当前使用
                            </span>
                          )}
                        </div>
                        <div className="flex items-center justify-between gap-3 px-3.5 pt-3 pb-3.5">
                          <div className="min-w-0">
                            <div className="flex items-center gap-2">
                              <span className="truncate text-sm font-semibold tracking-tight">
                                {template.name}
                              </span>
                              <span className="flex shrink-0 gap-1">
                                {[
                                  template.palette.bg,
                                  template.palette.primary,
                                  template.palette.accent,
                                  template.palette.ink,
                                ].map((color, index) => (
                                  <span
                                    key={index}
                                    className="inline-block size-2.5 rounded-full border border-black/10"
                                    style={{ backgroundColor: color }}
                                  />
                                ))}
                              </span>
                            </div>
                            <div className="mt-0.5 truncate text-xs text-hint">
                              {template.slides} 页 · {template.ratio} ·{" "}
                              {template.layouts.length} 种版式
                            </div>
                          </div>
                          <TemplateStatusBadge status={template.status} />
                        </div>
                      </div>
                    </div>
                  </div>
                )
              })}
              <button
                ref={(node) => registerTemplateNode("__upload-template__", node)}
                type="button"
                disabled={uploading}
                className="flex min-h-[200px] flex-col items-center justify-center gap-2.5 rounded-[20px] border-[1.5px] border-dashed border-input bg-white/20 text-hint transition-colors hover:border-foreground/45 hover:bg-white/40 hover:text-foreground"
                onClick={openFilePicker}
              >
                <span className="flex size-9 items-center justify-center rounded-full border-[1.5px] border-current">
                  <PlusIcon className="size-4.5" />
                </span>
                <span className="text-[13px] font-medium">上传 PPTX 解析为模板</span>
                <span className="text-xs opacity-70">.pptx · .ppt</span>
              </button>
                </div>
              ) : (
                <div className="glass-card mt-10 flex flex-col items-center justify-center rounded-[24px] px-6 py-16 text-center">
                  <div className="h-10 w-14 rounded-lg border-2 border-dashed border-input" />
                  <h2 className="mt-4 text-[15px] font-medium">没有匹配的模板</h2>
                  <p className="mt-1 text-[13px] text-hint">
                    换个关键词试试，或上传你自己的 PPTX 模板
                  </p>
                  <Button
                    variant="outline"
                    size="sm"
                    className="mt-5 rounded-full bg-white/60"
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
          )}
        </div>
      </main>

      <Dialog open={previewTemplate !== null} onOpenChange={(open) => !open && setPreviewId(null)}>
        {previewTemplate && (
          <DialogContent className="max-h-[92svh] gap-0 overflow-visible rounded-[28px] bg-transparent p-0 ring-0 shadow-none sm:max-w-[920px] [&_[data-slot=dialog-close]]:top-5 [&_[data-slot=dialog-close]]:right-5 [&_[data-slot=dialog-close]]:z-20 [&_[data-slot=dialog-close]]:rounded-full [&_[data-slot=dialog-close]]:bg-white/35 [&_[data-slot=dialog-close]]:text-foreground/70 [&_[data-slot=dialog-close]]:backdrop-blur-md [&_[data-slot=dialog-close]]:hover:bg-white/60 [&_[data-slot=dialog-close]]:hover:text-foreground">
            <liquid-glass
              blur-amount="15"
              scale="72"
              aberration="1.4"
              saturation="135"
              className="glass-panel max-h-[92svh] overflow-hidden rounded-[28px] [--liquid-glass-tint:rgba(235,238,243,0.35)] shadow-[0_28px_80px_rgba(30,32,44,0.22),inset_0_1px_1px_rgba(255,255,255,0.72)]"
            >
              <div className="max-h-[92svh] overflow-y-auto p-4 sm:p-5">
                <DialogHeader className="gap-1.5 pr-12 sm:px-1">
                  <div className="mb-0.5 flex items-center gap-2.5">
                    <span className="text-[11px] font-bold tracking-[0.12em] text-hint uppercase">
                      模板详情
                    </span>
                    <span className="h-3 w-px bg-foreground/15" />
                    <TemplateStatusBadge status={previewTemplate.status} />
                  </div>
                  <DialogTitle className="font-serif text-[25px] leading-tight font-black tracking-tight sm:text-[28px]">
                    {previewTemplate.name}
                  </DialogTitle>
                  <DialogDescription className="max-w-[620px] text-[13px] leading-5 text-muted-foreground sm:text-sm">
                    {previewTemplate.description}
                  </DialogDescription>
                </DialogHeader>

                <div className="mt-5 grid gap-4 md:grid-cols-[minmax(0,1.35fr)_minmax(270px,0.8fr)] md:gap-5">
                  <section className="min-w-0">
                    <div className="rounded-[22px] bg-black/[0.045] p-2 shadow-[0_18px_42px_rgba(30,32,44,0.16)] ring-1 ring-white/60">
                      <TemplateCover
                        template={previewTemplate}
                        showStatus
                        className="rounded-[16px]"
                      />
                    </div>

                    <div className="glass-card mt-3.5 rounded-[20px] px-4 py-3.5 shadow-[0_8px_24px_rgba(30,32,44,0.08)]">
                      <div className="mb-2.5 flex items-center justify-between gap-3">
                        <h3 className="text-[13px] font-bold text-foreground/80">
                          包含版式
                        </h3>
                        <span className="text-xs tabular-nums text-hint">
                          {previewTemplate.layouts.length} 种
                        </span>
                      </div>
                      <div className="flex flex-wrap gap-1.5">
                        {previewTemplate.layouts.map((layout) => (
                          <Badge
                            key={layout}
                            variant="outline"
                            className="h-7 rounded-full border-white/65 bg-white/35 px-3 text-[12px] font-medium text-foreground/75 shadow-[inset_0_1px_0_rgba(255,255,255,0.65)]"
                          >
                            {layout}
                          </Badge>
                        ))}
                      </div>
                    </div>
                  </section>

                  <aside className="glass-card flex min-w-0 flex-col overflow-hidden rounded-[22px] bg-white/50 shadow-[0_12px_30px_rgba(30,32,44,0.11)]">
                    <div className="grid grid-cols-2 divide-x divide-foreground/10">
                      <div className="flex flex-col items-center px-4 py-4 text-center">
                        <div className="mb-1 text-[11px] font-medium text-hint">母版页数</div>
                        <div className="font-heading text-[21px] leading-none font-bold tabular-nums">
                          {previewTemplate.slides}
                        </div>
                      </div>
                      <div className="flex flex-col items-center px-4 py-4 text-center">
                        <div className="mb-1 text-[11px] font-medium text-hint">画面比例</div>
                        <div className="font-heading text-[21px] leading-none font-bold tabular-nums">
                          {previewTemplate.ratio}
                        </div>
                      </div>
                    </div>

                    <div className="border-t border-foreground/10 px-4 py-4">
                      <div className="mb-3 text-[11px] font-medium text-hint">配色方案</div>
                      <div className="grid grid-cols-4 gap-2">
                        {[
                          { label: "背景", color: previewTemplate.palette.bg },
                          { label: "主色", color: previewTemplate.palette.primary },
                          { label: "强调", color: previewTemplate.palette.accent },
                          { label: "文字", color: previewTemplate.palette.ink },
                        ].map(({ label, color }) => (
                          <div key={label} className="flex min-w-0 flex-col items-center gap-1.5">
                            <span
                              className="size-8 rounded-full border border-black/10 shadow-[0_2px_8px_rgba(30,32,44,0.12),inset_0_1px_1px_rgba(255,255,255,0.45)]"
                              style={{ backgroundColor: color }}
                              title={`${label}：${color}`}
                            />
                            <span className="text-[10px] text-hint">{label}</span>
                          </div>
                        ))}
                      </div>
                    </div>

                    {previewTemplate.status === "failed" && (
                      <div className="border-t border-foreground/10 px-3 py-3">
                        <div className="rounded-[14px] border border-destructive/15 bg-[#fff4f2]/85 px-3 py-2.5 text-xs leading-5 text-destructive backdrop-blur-md">
                          {previewTemplate.error || "解析过程出错，请重新解析"}
                        </div>
                      </div>
                    )}

                    <div className="mt-auto border-t border-foreground/10 bg-white/15 p-3">
                      {previewTemplate.status === "ready" ? (
                        <Button
                          className="h-12 w-full rounded-full text-[15px] font-bold shadow-[0_10px_24px_rgba(27,28,32,0.22)]"
                          onClick={() => applyTemplate(previewTemplate)}
                        >
                          使用此模板
                          <ArrowRightIcon className="size-4" />
                        </Button>
                      ) : previewTemplate.status === "failed" ? (
                        <Button
                          className="h-12 w-full rounded-full text-[15px] font-bold shadow-[0_10px_24px_rgba(27,28,32,0.22)]"
                          onClick={() =>
                            void useTemplatesStore.getState().retryParse(previewTemplate.id)
                          }
                        >
                          <RotateCcwIcon className="size-4" />
                          重新解析
                        </Button>
                      ) : (
                        <Button disabled className="h-12 w-full rounded-full text-[14px] font-bold">
                          <LoaderCircleIcon className="size-4 animate-spin" />
                          正在解析 · {previewTemplate.progress ?? 0}%
                        </Button>
                      )}
                      <Button
                        variant="ghost"
                        className="mt-1.5 h-9 w-full rounded-full text-[12px] text-hint hover:bg-white/55 hover:text-destructive"
                        onClick={() => {
                          setPreviewId(null)
                          setDeleteId(previewTemplate.id)
                        }}
                      >
                        <Trash2Icon className="size-3.5" />
                        删除模板
                      </Button>
                    </div>
                  </aside>
                </div>
              </div>
            </liquid-glass>
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
