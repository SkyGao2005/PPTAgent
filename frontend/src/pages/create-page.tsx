import { useCallback, useEffect, useState } from "react"
import { useNavigate } from "react-router-dom"
import { useDropzone } from "react-dropzone"
import {
  ChevronDownIcon,
  FileSpreadsheetIcon,
  FileTextIcon,
  LoaderCircleIcon,
  PresentationIcon,
  SparklesIcon,
  Trash2Icon,
  UploadCloudIcon,
} from "lucide-react"
import { toast } from "sonner"

import { AppShell } from "@/components/app-shell"
import { TemplateCover } from "@/components/template-cover"
import { Button } from "@/components/ui/button"
import { Card, CardContent, CardHeader, CardTitle } from "@/components/ui/card"
import { Label } from "@/components/ui/label"
import {
  Select,
  SelectContent,
  SelectItem,
  SelectTrigger,
  SelectValue,
} from "@/components/ui/select"
import { Slider } from "@/components/ui/slider"
import { Textarea } from "@/components/ui/textarea"
import { cn } from "@/lib/utils"
import { MAX_ATTACHMENTS, useCreateTaskStore } from "@/stores/create-task-store"
import { useTemplatesStore } from "@/stores/templates-store"

const suggestions = [
  "2026 半年度经营分析汇报",
  "新产品发布方案介绍",
  "团队季度 OKR 复盘",
  "行业趋势研究分享",
]

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

export function CreatePage() {
  const navigate = useNavigate()
  const [advancedOpen, setAdvancedOpen] = useState(false)
  const {
    topic,
    pageCount,
    ratio,
    language,
    templateId,
    attachments,
    creating,
    setTopic,
    setPageCount,
    setRatio,
    setLanguage,
    setTemplateId,
    addAttachments,
    removeAttachment,
    createTask,
  } = useCreateTaskStore()
  const templates = useTemplatesStore((state) => state.templates)
  const templatesLoaded = useTemplatesStore((state) => state.loaded)
  const templatesLoadError = useTemplatesStore((state) => state.loadError)
  const readyTemplates = templates.filter((template) => template.status === "ready")
  // The grid shows only four cards; a template picked from the library must
  // stay visible even when it is not among the first four.
  const firstFour = readyTemplates.slice(0, 4)
  const selectedTemplate = readyTemplates.find((template) => template.id === templateId)
  const shownTemplates =
    selectedTemplate && !firstFour.some((template) => template.id === templateId)
      ? [selectedTemplate, ...firstFour.slice(0, 3)]
      : firstFour

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
    [addAttachments],
  )

  const { getRootProps, getInputProps, isDragActive } = useDropzone({
    onDrop,
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
      <main className="mx-auto w-full max-w-[860px] px-4 py-10 sm:px-6 sm:py-12">
        <div className="animate-fade-up mb-8">
          <h1 className="font-heading text-[28px] font-bold tracking-tight">新建演示</h1>
          <p className="mt-1.5 text-sm text-hint">
            描述你的主题，选择模板，剩下的交给 AI。
          </p>
        </div>

        <div className="space-y-4">
          <Card className="gap-4 shadow-none">
            <CardHeader>
              <div className="flex items-baseline justify-between gap-4">
                <CardTitle className="text-[15px]">演示主题</CardTitle>
                <span className="font-heading text-xs tabular-nums text-hint">
                  {topic.length} / 200
                </span>
              </div>
            </CardHeader>
            <CardContent className="space-y-3.5">
              <Textarea
                value={topic}
                onChange={(event) => setTopic(event.target.value)}
                aria-label="演示主题"
                maxLength={200}
                rows={3}
                className="min-h-24 resize-none rounded-[10px] bg-[#FDFDFC] p-4 text-[15px] leading-7"
                placeholder="例如：2026 半年度经营分析与下半年战略规划，面向管理层汇报……"
              />
              <div className="flex flex-wrap gap-2">
                {suggestions.map((suggestion) => (
                  <button
                    key={suggestion}
                    type="button"
                    className="rounded-full border bg-muted px-3 py-1.5 text-[13px] text-muted-foreground transition-colors hover:border-primary/60 hover:bg-accent hover:text-primary"
                    onClick={() => setTopic(suggestion)}
                  >
                    {suggestion}
                  </button>
                ))}
              </div>
            </CardContent>
          </Card>

          <Card className="gap-4 shadow-none">
            <CardHeader>
              <div className="flex flex-wrap items-baseline gap-2">
                <CardTitle className="text-[15px]">参考资料</CardTitle>
                <span className="text-xs text-hint">可选 · 支持 PDF / Word / Excel</span>
              </div>
              <p className="mt-1 text-[13px] text-hint">
                AI 会阅读这些材料，提取数据与要点写入幻灯片。
              </p>
            </CardHeader>
            <CardContent className="space-y-2">
              {attachments.map((attachment) => (
                <div
                  key={attachment.id}
                  className="flex items-center gap-3 rounded-[10px] border bg-[#FDFDFC] px-3 py-2.5"
                >
                  <span className="flex size-8 shrink-0 items-center justify-center rounded-lg bg-primary/10 text-primary">
                    <AttachmentIcon name={attachment.name} />
                  </span>
                  <span className="min-w-0 flex-1 truncate text-sm font-medium">
                    {attachment.name}
                  </span>
                  <span className="font-heading text-xs tabular-nums text-hint">
                    {formatFileSize(attachment.size)}
                  </span>
                  <Button
                    type="button"
                    variant="ghost"
                    size="icon-sm"
                    aria-label={`移除 ${attachment.name}`}
                    onClick={() => removeAttachment(attachment.id)}
                  >
                    <Trash2Icon />
                  </Button>
                </div>
              ))}
              <div
                {...getRootProps({
                  className: cn(
                    "flex w-full cursor-pointer flex-col items-center justify-center gap-1.5 rounded-[10px] border-[1.5px] border-dashed px-5 py-6 text-sm text-hint transition-colors hover:border-primary/60 hover:bg-accent/40 hover:text-primary",
                    isDragActive && "border-primary bg-accent/60 text-primary",
                  ),
                })}
              >
                <input {...getInputProps()} />
                <UploadCloudIcon className="size-5" />
                <span>{isDragActive ? "松开即可添加" : "点击添加文件，或拖拽到此处"}</span>
              </div>
            </CardContent>
          </Card>

          <Card className="gap-4 shadow-none">
            <CardHeader>
              <div className="flex items-baseline justify-between gap-4">
                <CardTitle className="text-[15px]">模板</CardTitle>
                <Button
                  type="button"
                  variant="link"
                  size="sm"
                  className="h-auto p-0"
                  onClick={() => navigate("/templates")}
                >
                  浏览模板库 →
                </Button>
              </div>
            </CardHeader>
            <CardContent>
              {templatesLoadError && (
                <div className="flex items-center justify-between rounded-[10px] border border-dashed px-4 py-3 text-[13px] text-hint">
                  模板列表加载失败：{templatesLoadError}
                  <Button
                    type="button"
                    variant="link"
                    size="sm"
                    className="h-auto p-0"
                    onClick={() => void useTemplatesStore.getState().fetchTemplates()}
                  >
                    重试
                  </Button>
                </div>
              )}
              <div className="grid grid-cols-2 gap-3 sm:grid-cols-4">
                {shownTemplates.map((template) => {
                  const selected = template.id === templateId
                  return (
                    <button
                      key={template.id}
                      type="button"
                      className={cn(
                        "overflow-hidden rounded-xl border-2 text-left transition-all",
                        selected
                          ? "border-primary bg-accent/50 ring-3 ring-primary/10"
                          : "border-border bg-card hover:border-primary/50",
                      )}
                      onClick={() => setTemplateId(template.id)}
                    >
                      <TemplateCover template={template} />
                      <div className="flex items-center justify-between gap-2 px-2.5 py-2">
                        <span className="truncate text-[13px] font-medium">
                          {template.name}
                        </span>
                        {selected ? (
                          <span className="shrink-0 text-[11px] font-bold text-primary">
                            ✓ 已选
                          </span>
                        ) : (
                          <span className="shrink-0 text-[10px] text-hint">
                            {template.ratio}
                          </span>
                        )}
                      </div>
                    </button>
                  )
                })}
              </div>
            </CardContent>
          </Card>

          <Card className="gap-5 shadow-none">
            <CardHeader>
              <CardTitle className="text-[15px]">生成设置</CardTitle>
            </CardHeader>
            <CardContent className="grid gap-8 sm:grid-cols-2">
              <div>
                <div className="mb-4 flex items-baseline justify-between">
                  <Label className="text-[13px] text-muted-foreground">页数</Label>
                  <span className="font-heading text-sm font-semibold tabular-nums">
                    {pageCount} 页
                  </span>
                </div>
                <Slider
                  aria-label="页数"
                  min={5}
                  max={30}
                  step={1}
                  value={[pageCount]}
                  onValueChange={(value) =>
                    setPageCount(typeof value === "number" ? value : value[0])
                  }
                />
                <div className="font-heading mt-2 flex justify-between text-[11px] text-hint">
                  <span>5</span>
                  <span>30</span>
                </div>
              </div>
              <div>
                <Label className="mb-3 block text-[13px] text-muted-foreground">
                  画面比例
                </Label>
                <div className="grid grid-cols-2 gap-2">
                  {(["16:9", "4:3"] as const).map((item) => (
                    <button
                      key={item}
                      type="button"
                      aria-pressed={ratio === item}
                      className={cn(
                        "flex h-10 items-center justify-center gap-2 rounded-[10px] border-2 text-[13px] font-medium transition-colors",
                        ratio === item
                          ? "border-primary bg-accent/60 text-primary"
                          : "border-border bg-card text-muted-foreground hover:border-input",
                      )}
                      onClick={() => setRatio(item)}
                    >
                      <PresentationIcon className="size-4" />
                      {item}
                    </button>
                  ))}
                </div>
              </div>
              <div className="sm:col-span-2">
                <button
                  type="button"
                  className="flex w-full items-center justify-between border-t pt-4 text-sm font-medium"
                  aria-expanded={advancedOpen}
                  onClick={() => setAdvancedOpen((open) => !open)}
                >
                  高级选项
                  <ChevronDownIcon
                    className={cn(
                      "size-4 text-muted-foreground transition-transform",
                      advancedOpen && "rotate-180",
                    )}
                  />
                </button>
                {advancedOpen && (
                  <div className="mt-4 grid gap-2 sm:max-w-xs">
                    <Label htmlFor="language">输出语言</Label>
                    <Select
                      value={language}
                      onValueChange={(value) => value && setLanguage(value)}
                    >
                      <SelectTrigger id="language" className="w-full">
                        <SelectValue />
                      </SelectTrigger>
                      <SelectContent>
                        <SelectItem value="zh-CN">简体中文</SelectItem>
                        <SelectItem value="en-US">English</SelectItem>
                      </SelectContent>
                    </Select>
                  </div>
                )}
              </div>
            </CardContent>
          </Card>
        </div>

        <div className="mt-7 flex flex-col items-start justify-between gap-4 sm:flex-row sm:items-center">
          <span className="text-[13px] text-hint">
            预计用时约 {estimatedMinutes} 分钟 · 生成过程中可随时编辑已完成页面
          </span>
          <Button
            size="lg"
            className="h-12 w-full rounded-xl px-7 text-[15px] font-bold shadow-lg shadow-primary/20 sm:w-auto"
            disabled={!topic.trim() || !templateId || creating}
            onClick={() => void handleGenerate()}
          >
            {creating ? (
              <LoaderCircleIcon className="animate-spin" />
            ) : (
              <SparklesIcon />
            )}
            开始生成
          </Button>
        </div>
      </main>
    </AppShell>
  )
}
