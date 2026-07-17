import { useCallback, useEffect } from "react"
import { useNavigate } from "react-router-dom"
import { useDropzone } from "react-dropzone"
import {
  ArrowRightIcon,
  CheckIcon,
  ChevronDownIcon,
  FileSpreadsheetIcon,
  FileTextIcon,
  LoaderCircleIcon,
  PlusIcon,
  XIcon,
} from "lucide-react"
import { toast } from "sonner"

import { AppShell } from "@/components/app-shell"
import { Button } from "@/components/ui/button"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { Slider } from "@/components/ui/slider"
import { Textarea } from "@/components/ui/textarea"
import { usePageMetadata } from "@/lib/use-page-metadata"
import { cn } from "@/lib/utils"
import { MAX_ATTACHMENTS, useCreateTaskStore } from "@/stores/create-task-store"
import { useTemplatesStore } from "@/stores/templates-store"

const suggestions = [
  "2026 半年度经营分析汇报",
  "新产品发布方案介绍",
  "团队季度 OKR 复盘",
  "行业趋势研究分享",
]

// Setting chips inside the prompt card, per the liquid-glass design
// ("Template: Meridian ▾", "12 slides ▾").
const chipClass =
  "flex items-center gap-1.5 rounded-full px-3.5 py-[7px] text-[13.5px] font-medium text-muted-foreground transition-colors hover:bg-white/55 hover:text-foreground aria-expanded:bg-white/55 disabled:opacity-50"

function formatFileSize(bytes: number): string {
  if (bytes < 1024 * 1024) {
    return `${Math.max(1, Math.round(bytes / 1024))} KB`
  }
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`
}

function AttachmentIcon({ name }: { name: string }) {
  if (/\.(xlsx?|csv)$/i.test(name)) {
    return <FileSpreadsheetIcon className="size-4" />
  }
  return <FileTextIcon className="size-4" />
}

function ChipChevron() {
  return <ChevronDownIcon className="size-3.5 text-hint" />
}

export function CreatePage() {
  usePageMetadata("新建演示")
  const navigate = useNavigate()
  const {
    topic,
    pageCount,
    ratio,
    templateId,
    attachments,
    creating,
    setTopic,
    setPageCount,
    setRatio,
    setTemplateId,
    addAttachments,
    removeAttachment,
    createTask,
  } = useCreateTaskStore()
  const templates = useTemplatesStore((state) => state.templates)
  const templatesLoaded = useTemplatesStore((state) => state.loaded)
  const templatesLoadError = useTemplatesStore((state) => state.loadError)
  const readyTemplates = templates.filter((template) => template.status === "ready")
  const selectedTemplate = readyTemplates.find((template) => template.id === templateId)

  useEffect(() => {
    if (!templatesLoaded) {
      void useTemplatesStore.getState().fetchTemplates()
    }
  }, [templatesLoaded])

  // Keep the selection valid: fall back to the first ready template when the
  // selected one was deleted / never existed, and clear it entirely when no
  // template is ready — a stale id must never reach task creation.
  useEffect(() => {
    if (!templatesLoaded) {
      return
    }
    if (readyTemplates.length === 0) {
      if (templateId) {
        setTemplateId("")
      }
      return
    }
    if (!readyTemplates.some((template) => template.id === templateId)) {
      setTemplateId(readyTemplates[0].id)
    }
  }, [templatesLoaded, readyTemplates, templateId, setTemplateId])

  const onDrop = useCallback(
    (accepted: File[], rejected: unknown[]) => {
      if (creating) {
        return
      }
      if (rejected.length) {
        toast.error("部分文件被跳过", {
          description: "仅支持 PDF / Word / Excel，单个文件不超过 20MB",
        })
      }
      if (accepted.length) {
        const added = addAttachments(accepted)
        if (added === accepted.length) {
          toast.success(`已添加 ${added} 个参考文件`)
        } else if (added > 0) {
          toast.warning(`最多 ${MAX_ATTACHMENTS} 个参考文件，仅添加了前 ${added} 个`)
        } else {
          toast.error(`最多 ${MAX_ATTACHMENTS} 个参考文件，请先移除部分文件`)
        }
      }
    },
    [addAttachments, creating],
  )

  const {
    getRootProps,
    getInputProps,
    isDragActive,
    open: openFilePicker,
  } = useDropzone({
    onDrop,
    disabled: creating,
    noClick: true,
    noKeyboard: true,
    maxSize: 20 * 1024 * 1024,
    accept: {
      "application/pdf": [".pdf"],
      "application/msword": [".doc"],
      "application/vnd.openxmlformats-officedocument.wordprocessingml.document": [".docx"],
      "application/vnd.ms-excel": [".xls"],
      "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": [".xlsx"],
      "text/csv": [".csv"],
    },
  })

  async function handleGenerate(): Promise<void> {
    if (!topic.trim() || !templateId || creating) {
      return
    }
    try {
      const taskId = await createTask()
      // §8.1: carry the configured page count so the workbench can render
      // skeleton slides before task.created reports total_slides.
      navigate(`/workbench/${taskId}`, { state: { requestedPages: pageCount } })
    } catch {
      toast.error("任务创建失败，请重试")
    }
  }

  const estimatedMinutes = Math.max(1, Math.round((pageCount * 5) / 60))

  return (
    <AppShell>
      <main
        id="main-content"
        tabIndex={-1}
        className="mx-auto flex w-full max-w-[840px] flex-col items-center px-4 pt-8 pb-16 outline-none sm:pt-12"
      >
        <div className="animate-fade-up flex flex-col items-center text-center">
          <h1 className="font-serif text-[40px] font-black tracking-tight text-balance sm:text-[48px]">
            今天要演示什么？
          </h1>
          <p className="mt-2.5 mb-9 max-w-[520px] text-[15px] text-hint text-pretty">
            描述你的主题、附上参考资料，PPTAgent 负责大纲、版式和每一页内容。
          </p>
        </div>

        <liquid-glass
          blur-amount="9"
          className="glass-panel w-full rounded-[28px] shadow-[0_24px_70px_rgba(30,32,44,0.16),inset_0_1px_1px_rgba(255,255,255,0.8)]"
        >
          <div
            {...getRootProps({
              className: "relative flex flex-col gap-4 p-6 pb-4",
              "aria-label": "演示主题与参考资料，支持拖放文件",
            })}
          >
            <input {...getInputProps()} />
            {isDragActive && (
              <div className="pointer-events-none absolute inset-2 z-10 flex items-center justify-center rounded-[22px] border-2 border-dashed border-primary bg-white/80 text-sm font-bold">
                松开即可添加参考资料
              </div>
            )}

            <Textarea
              value={topic}
              onChange={(event) => setTopic(event.target.value)}
              aria-label="演示主题"
              maxLength={200}
              rows={3}
              className="min-h-22 resize-none rounded-none border-0 bg-transparent p-0 text-[16px] leading-7 shadow-none focus-visible:border-transparent focus-visible:ring-0 md:text-[16px]"
              placeholder="描述你的演示——主题、受众、篇幅、语气……"
            />

            <div className="flex flex-wrap items-center gap-x-2 gap-y-2.5">
              {attachments.map((attachment) => (
                <span
                  key={attachment.id}
                  className="flex items-center gap-1.5 rounded-full border border-border bg-white/60 py-[7px] pr-2.5 pl-3.5 text-[13.5px] font-medium"
                >
                  <span className="text-hint">
                    <AttachmentIcon name={attachment.name} />
                  </span>
                  <span className="max-w-44 truncate">{attachment.name}</span>
                  <span className="font-heading text-xs tabular-nums text-hint">
                    {formatFileSize(attachment.size)}
                  </span>
                  <button
                    type="button"
                    aria-label={`移除 ${attachment.name}`}
                    disabled={creating}
                    className="ml-0.5 rounded-full p-0.5 text-hint transition-colors hover:bg-accent hover:text-foreground"
                    onClick={() => removeAttachment(attachment.id)}
                  >
                    <XIcon className="size-4" />
                  </button>
                </span>
              ))}
              <button
                type="button"
                disabled={creating}
                className="mr-1 flex items-center gap-1.5 rounded-full border border-dashed border-input px-3.5 py-[7px] text-[13.5px] font-medium text-hint transition-colors hover:border-foreground/50 hover:text-foreground"
                onClick={openFilePicker}
              >
                <PlusIcon className="size-4" />
                参考资料
              </button>

              <span className="flex-1" />

              <DropdownMenu>
                <DropdownMenuTrigger
                  render={<button type="button" className={chipClass} />}
                >
                  模板:{" "}
                  <strong className="font-semibold text-foreground">
                    {selectedTemplate?.name ??
                      (templatesLoaded ? "选择模板" : "载入中…")}
                  </strong>
                  <ChipChevron />
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-64">
                  {templatesLoadError ? (
                    <>
                      <DropdownMenuItem disabled>
                        模板列表加载失败
                      </DropdownMenuItem>
                      <DropdownMenuItem
                        onClick={() => void useTemplatesStore.getState().fetchTemplates()}
                      >
                        重试加载
                      </DropdownMenuItem>
                    </>
                  ) : !templatesLoaded ? (
                    <DropdownMenuItem disabled>载入模板中…</DropdownMenuItem>
                  ) : readyTemplates.length === 0 ? (
                    <DropdownMenuItem disabled>暂无可用模板</DropdownMenuItem>
                  ) : (
                    readyTemplates.map((template) => (
                      <DropdownMenuItem
                        key={template.id}
                        onClick={() => setTemplateId(template.id)}
                      >
                        <span className="flex shrink-0 gap-[3px]">
                          {[
                            template.palette.bg,
                            template.palette.primary,
                            template.palette.accent,
                          ].map((color, index) => (
                            <span
                              key={index}
                              className="inline-block size-2.5 rounded-full border border-black/10"
                              style={{ backgroundColor: color }}
                            />
                          ))}
                        </span>
                        <span className="flex-1 truncate">{template.name}</span>
                        <span className="font-heading text-[10px] text-hint">
                          {template.ratio}
                        </span>
                        {template.id === templateId && (
                          <CheckIcon className="size-3.5" />
                        )}
                      </DropdownMenuItem>
                    ))
                  )}
                  <DropdownMenuSeparator />
                  <DropdownMenuItem onClick={() => navigate("/templates")}>
                    浏览模板库 →
                  </DropdownMenuItem>
                </DropdownMenuContent>
              </DropdownMenu>

              <DropdownMenu>
                <DropdownMenuTrigger
                  render={<button type="button" className={chipClass} aria-label="页数" />}
                >
                  <strong className="font-heading font-semibold text-foreground tabular-nums">
                    {pageCount}
                  </strong>{" "}
                  页
                  <ChipChevron />
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-64 p-3">
                  <div className="flex items-baseline justify-between">
                    <span className="text-[13px] font-medium">页数</span>
                    <span className="font-heading text-[13px] font-semibold tabular-nums">
                      {pageCount} 页
                    </span>
                  </div>
                  <Slider
                    aria-label="页数"
                    min={5}
                    max={30}
                    step={1}
                    value={[pageCount]}
                    className="mt-3.5"
                    onValueChange={(value) =>
                      setPageCount(typeof value === "number" ? value : value[0])
                    }
                  />
                  <div className="font-heading mt-2 flex justify-between text-[11px] text-hint">
                    <span>5</span>
                    <span>30</span>
                  </div>
                </DropdownMenuContent>
              </DropdownMenu>

              <DropdownMenu>
                <DropdownMenuTrigger
                  render={<button type="button" className={chipClass} aria-label="画面比例" />}
                >
                  <strong className="font-heading font-semibold text-foreground">
                    {ratio}
                  </strong>
                  <ChipChevron />
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-40">
                  {(["16:9", "4:3"] as const).map((item) => (
                    <DropdownMenuItem key={item} onClick={() => setRatio(item)}>
                      <span className="font-heading flex-1">{item}</span>
                      {ratio === item && <CheckIcon className="size-3.5" />}
                    </DropdownMenuItem>
                  ))}
                </DropdownMenuContent>
              </DropdownMenu>

            </div>

            <div className="flex items-center justify-between gap-3 border-t border-border/80 pt-3.5">
              <span className="pl-1 text-[12.5px] text-hint">
                <span className="font-heading tabular-nums">{topic.length} / 200</span>
                {" · "}预计用时约 {estimatedMinutes} 分钟
              </span>
              <Button
                size="lg"
                className="h-11 rounded-full px-5 text-sm font-semibold shadow-[0_10px_26px_rgba(27,28,32,0.3)]"
                disabled={!topic.trim() || !templateId || creating}
                onClick={() => void handleGenerate()}
              >
                {creating && <LoaderCircleIcon className="animate-spin" />}
                开始生成
                {!creating && <ArrowRightIcon data-icon="inline-end" />}
              </Button>
            </div>
          </div>
        </liquid-glass>

        {templatesLoadError ? (
          <div role="alert" className="mt-4 text-[13px] text-hint">
            模板列表加载失败：{templatesLoadError}
            <Button
              type="button"
              variant="link"
              size="sm"
              className="h-auto p-0 pl-2"
              onClick={() => void useTemplatesStore.getState().fetchTemplates()}
            >
              重试
            </Button>
          </div>
        ) : templatesLoaded && readyTemplates.length === 0 ? (
          <div role="status" className="mt-4 text-[13px] text-hint">
            当前没有可用模板，请先到
            <Button
              type="button"
              variant="link"
              size="sm"
              className="h-auto p-0 px-1 underline-offset-2"
              onClick={() => navigate("/templates")}
            >
              模板库
            </Button>
            上传并等待解析完成。
          </div>
        ) : null}

        <div className={cn("flex flex-wrap justify-center gap-2", templatesLoadError || (templatesLoaded && readyTemplates.length === 0) ? "mt-4" : "mt-5")}>
          {suggestions.map((suggestion) => (
            <button
              key={suggestion}
              type="button"
              className="glass-chip rounded-full px-3.5 py-2 text-[13px] font-medium text-muted-foreground transition-colors hover:bg-white/70 hover:text-foreground"
              onClick={() => setTopic(suggestion)}
            >
              {suggestion}
            </button>
          ))}
        </div>
      </main>
    </AppShell>
  )
}
