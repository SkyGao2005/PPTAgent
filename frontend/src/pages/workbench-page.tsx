import { useEffect, useMemo, useRef, useState } from "react"
import { Link, useLocation, useParams } from "react-router-dom"
import {
  ArrowUpIcon,
  CheckIcon,
  ChevronDownIcon,
  DownloadIcon,
  FileClockIcon,
  HistoryIcon,
  ListStartIcon,
  LoaderCircleIcon,
  PauseIcon,
  PlayIcon,
  RotateCcwIcon,
  SparklesIcon,
  TriangleAlertIcon,
  Undo2Icon,
  WifiOffIcon,
} from "lucide-react"

import { BrandMark } from "@/components/brand-mark"
import { Button } from "@/components/ui/button"
import {
  AlertDialog,
  AlertDialogContent,
  AlertDialogDescription,
  AlertDialogFooter,
  AlertDialogHeader,
  AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuGroup,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/dropdown-menu"
import { ScrollArea } from "@/components/ui/scroll-area"
import {
  Sheet,
  SheetContent,
  SheetDescription,
  SheetHeader,
  SheetTitle,
  SheetTrigger,
} from "@/components/ui/sheet"
import { Textarea } from "@/components/ui/textarea"
import { connectTaskEvents } from "@/lib/sse"
import { cn, formatClock, previewSrc } from "@/lib/utils"
import { useTemplatesStore } from "@/stores/templates-store"
import { useWorkbenchStore } from "@/stores/workbench-store"
import type { SlideView, WorkChatMessage } from "@/stores/workbench-store"
import type { TaskStage } from "@/types/api"

const QUICK_CHIPS = ["换个配色", "精简文案", "换个版式", "换张配图"]
const STARTER_CHIPS = [
  "把这一页的文案精简一点",
  "换一组配色试试",
  "改成左文右图的版式",
]

const STAGE_TEXT: Partial<Record<TaskStage, string>> = {
  template: "准备模板中…",
  research: "解析资料中…",
  plan: "规划大纲中…",
  generate: "逐页生成中…",
}

// ---- header progress ----

function ProgressSegments({ slides }: { slides: SlideView[] }) {
  const segments = useMemo(() => {
    const count = Math.min(slides.length, 12)
    return Array.from({ length: count }, (_, seg) => {
      const from = Math.floor((seg * slides.length) / count)
      const to = Math.max(Math.floor(((seg + 1) * slides.length) / count), from + 1)
      const slice = slides.slice(from, to)
      if (slice.every((slide) => slide.status === "completed")) return "done"
      if (slice.some((slide) => slide.status === "failed")) return "failed"
      if (slice.some((slide) => slide.status === "generating" || slide.status === "editing"))
        return "active"
      return "idle"
    })
  }, [slides])

  return (
    <span className="flex w-44 gap-[3px]">
      {segments.map((state, index) => (
        <span
          key={index}
          className={cn(
            "h-[5px] flex-1 rounded-full transition-colors duration-300",
            state === "done" && "bg-primary",
            state === "failed" && "bg-destructive/70",
            state === "active" && "animate-soft-pulse bg-primary-soft",
            state === "idle" && "bg-border",
          )}
        />
      ))}
    </span>
  )
}

// ---- left column ----

function SlideThumb({
  slide,
  ratio,
  selected,
  paused,
  onSelect,
}: {
  slide: SlideView
  ratio: "16:9" | "4:3"
  selected: boolean
  paused: boolean
  onSelect: () => void
}) {
  const src = previewSrc(slide.previewUrl, slide.revision)
  return (
    <div className="group">
      <div className="mb-1 flex items-center gap-1.5 px-0.5">
        <span
          className={cn(
            "font-heading w-5 text-[10px] font-semibold tabular-nums",
            selected ? "text-primary" : "text-hint/80",
          )}
        >
          {String(slide.index).padStart(2, "0")}
        </span>
        <span className="min-w-0 flex-1 truncate text-[11px] text-muted-foreground">
          {slide.title}
        </span>
        {(slide.status === "generating" || slide.status === "editing") && (
          <LoaderCircleIcon className="size-3 animate-spin text-primary" />
        )}
        {slide.status === "completed" && (
          <CheckIcon className="size-3 text-success" strokeWidth={3} />
        )}
        {slide.status === "failed" && (
          <TriangleAlertIcon className="size-3 text-destructive" />
        )}
      </div>
      <button
        type="button"
        aria-label={`选择第 ${slide.index} 页`}
        onClick={onSelect}
        className={cn(
          "relative block w-full overflow-hidden rounded-lg border-2 bg-card text-left transition-all",
          ratio === "4:3" ? "aspect-[4/3]" : "aspect-video",
          selected
            ? "border-primary ring-3 ring-primary/15"
            : slide.status === "failed"
              ? "border-destructive/45"
              : "border-border/80 hover:border-input",
        )}
      >
        {src && <img src={src} alt={slide.title} className="absolute inset-0 size-full object-cover" />}
        {slide.status === "generating" && (
          <span className="skeleton-shimmer absolute inset-0" />
        )}
        {slide.status === "editing" && (
          <span className="absolute inset-0 flex items-center justify-center bg-card/55">
            <LoaderCircleIcon className="size-4 animate-spin text-primary" />
          </span>
        )}
        {slide.status === "queued" && (
          <span className="absolute inset-0 flex items-center justify-center bg-secondary/55 text-[10px] text-hint">
            {paused ? "已暂停" : "排队中"}
          </span>
        )}
        {slide.status === "failed" && (
          <span className="absolute inset-0 flex flex-col items-center justify-center gap-0.5 bg-[oklch(0.97_0.015_30)] text-[10px] font-medium text-destructive">
            生成失败
            <span className="text-[9px] text-destructive/70">点击查看并重试</span>
          </span>
        )}
      </button>
    </div>
  )
}

// ---- center column ----

function BootCard({ topic, stage, total }: { topic: string; stage: TaskStage; total: number }) {
  const order: TaskStage[] = ["research", "plan", "generate"]
  const activeIdx = Math.max(order.indexOf(stage), 0)
  const steps = [
    { label: "解析参考资料", hint: "主题分析" },
    { label: "规划大纲结构", hint: `${total} 页` },
    { label: "逐页生成内容", hint: "" },
  ]
  return (
    <div className="animate-fade-up w-[380px] rounded-2xl border bg-card px-10 py-8 shadow-[0_10px_34px_rgba(30,25,15,0.08)]">
      <div className="text-[15px] font-bold">正在准备你的演示</div>
      <div className="mt-1 mb-6 truncate text-xs text-hint">{topic}</div>
      <div className="flex flex-col gap-4">
        {steps.map((step, index) => (
          <div key={step.label} className="flex items-center gap-3">
            {index < activeIdx ? (
              <span className="flex size-5 shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground">
                <CheckIcon className="size-3" strokeWidth={3} />
              </span>
            ) : index === activeIdx ? (
              <LoaderCircleIcon className="size-5 shrink-0 animate-spin text-primary" strokeWidth={2.5} />
            ) : (
              <span className="size-5 shrink-0 rounded-full border-2 border-border" />
            )}
            <span
              className={cn(
                "text-[13px]",
                index === activeIdx ? "font-bold" : index < activeIdx ? "" : "text-hint",
              )}
            >
              {step.label}
            </span>
            <span className="ml-auto text-[11px] text-hint">{step.hint}</span>
          </div>
        ))}
      </div>
    </div>
  )
}

function SlideCanvas({
  slide,
  ratio,
  paused,
  onRetry,
}: {
  slide: SlideView
  ratio: "16:9" | "4:3"
  paused: boolean
  onRetry: () => void
}) {
  const src = previewSrc(slide.previewUrl, slide.revision)
  const aspect = ratio === "4:3" ? "aspect-[4/3]" : "aspect-video"

  if (slide.status === "failed") {
    return (
      <div className={cn("flex w-full flex-col items-center justify-center gap-2 bg-[oklch(0.985_0.008_30)]", aspect)}>
        <span className="flex size-11 items-center justify-center rounded-full bg-destructive text-lg font-bold text-white">
          !
        </span>
        <div className="mt-1 text-lg font-bold">本页生成失败</div>
        <div className="text-sm text-hint">内容引擎响应超时，不影响其他页面</div>
        <Button className="mt-2" onClick={onRetry}>
          重试生成本页
        </Button>
      </div>
    )
  }

  if (slide.status === "queued") {
    return (
      <div className={cn("flex w-full flex-col items-center justify-center gap-1.5 bg-muted", aspect)}>
        <div className="text-lg text-hint">{paused ? "本页已暂停生成" : "本页排队中"}</div>
        <div className="text-sm text-hint/80">
          {paused ? "点击右上角「继续生成」恢复" : "完成前面的页面后将自动开始"}
        </div>
      </div>
    )
  }

  if (slide.status === "generating" && !src) {
    return (
      <div className={cn("relative flex w-full flex-col justify-center gap-4 bg-muted px-[10%]", aspect)}>
        <div className="skeleton-shimmer h-[7%] w-[46%] rounded-lg" />
        <div className="skeleton-shimmer h-[3.6%] w-[72%] rounded-md" />
        <div className="skeleton-shimmer h-[3.6%] w-[64%] rounded-md" />
        <div className="absolute bottom-[7%] left-[10%] flex items-center gap-2 text-sm text-hint">
          <LoaderCircleIcon className="size-4 animate-spin text-primary" />
          AI 正在撰写本页内容…
        </div>
      </div>
    )
  }

  return (
    <div className={cn("relative w-full bg-card", aspect)}>
      {src && (
        <img
          key={`${slide.id}:${slide.revision}:${src.length}`}
          src={src}
          alt={slide.title}
          className="animate-fade-up absolute inset-0 size-full object-cover"
        />
      )}
      {(slide.status === "editing" || slide.status === "generating") && (
        <div className="absolute inset-0 flex items-center justify-center bg-card/45">
          <span className="flex items-center gap-2 rounded-full bg-card/95 px-4 py-2 text-xs font-medium text-muted-foreground shadow-sm">
            <LoaderCircleIcon className="size-3.5 animate-spin text-primary" />
            {slide.status === "editing" ? "正在修改本页…" : "正在生成本页…"}
          </span>
        </div>
      )}
    </div>
  )
}

// ---- right column ----

function ChatBubble({ message }: { message: WorkChatMessage }) {
  if (message.role === "user") {
    return (
      <div className="flex flex-col items-end gap-1">
        <div className="animate-fade-up max-w-[86%] rounded-xl rounded-br-[4px] bg-primary px-3.5 py-2 text-[13px] leading-relaxed whitespace-pre-wrap break-words text-primary-foreground">
          {message.content}
        </div>
        {message.status === "queued" && (
          <span className="text-[10px] text-hint">已排队 · 页面完成后自动执行</span>
        )}
      </div>
    )
  }
  if (message.status === "info") {
    return (
      <div className="animate-fade-up self-center rounded-full bg-secondary px-3 py-1 text-[11px] text-muted-foreground">
        {message.content}
      </div>
    )
  }
  return (
    <div className="flex flex-col items-start gap-1.5">
      <div
        className={cn(
          "animate-fade-up max-w-[86%] rounded-xl rounded-bl-[4px] bg-muted px-3.5 py-2 text-[13px] leading-relaxed whitespace-pre-wrap break-words",
          message.status === "failed" && "text-destructive",
        )}
      >
        {message.status === "pending" ? (
          <span className="flex items-center gap-2 text-muted-foreground">
            <LoaderCircleIcon className="size-3.5 animate-spin text-primary" />
            {message.content}
          </span>
        ) : (
          message.content
        )}
      </div>
      {message.action && (
        <span className="rounded-full bg-success-subtle px-2.5 py-0.5 text-[11px] font-medium text-success">
          ✓ 已应用 · {message.action}
        </span>
      )}
    </div>
  )
}

function ChatPanel({ slide }: { slide: SlideView | null }) {
  const chats = useWorkbenchStore((state) => state.chats)
  const sendChat = useWorkbenchStore((state) => state.sendChat)
  const [draft, setDraft] = useState("")
  // §8.3: drafts are keyed by slide id so an unsent instruction written for
  // one page can never be sent to another after switching slides.
  const draftsRef = useRef(new Map<string, string>())
  const scrollRef = useRef<HTMLDivElement>(null)
  const slideId = slide?.id ?? null
  const messages = slide ? (chats.get(slide.id) ?? []) : []

  useEffect(() => {
    setDraft(slideId ? (draftsRef.current.get(slideId) ?? "") : "")
  }, [slideId])

  useEffect(() => {
    scrollRef.current?.scrollTo({ top: scrollRef.current.scrollHeight })
  }, [messages.length, slide?.id])

  function updateDraft(value: string): void {
    setDraft(value)
    if (slideId) {
      draftsRef.current.set(slideId, value)
    }
  }

  function send(text: string): void {
    if (!slide || !text.trim()) {
      return
    }
    // §8.3: capture the slide id at send time, never the selection afterwards.
    void sendChat(slide.id, text)
    draftsRef.current.delete(slide.id)
    setDraft("")
  }

  const busy = messages.some((message) => message.status === "pending")
  const notReady = slide !== null && slide.status !== "completed" && slide.status !== "editing"

  return (
    <div className="flex h-full min-h-0 flex-col bg-card">
      <div className="border-b px-4 pt-4 pb-3">
        <div className="flex items-center gap-2">
          <span className="flex size-6.5 items-center justify-center rounded-lg bg-primary text-[13px] text-primary-foreground">
            ✦
          </span>
          <h2 className="text-sm font-bold">AI 修改助手</h2>
        </div>
        {slide && (
          <div className="mt-2.5 inline-flex max-w-full items-center gap-1.5 rounded-full border bg-muted px-3 py-1 text-xs text-muted-foreground">
            <span
              className={cn(
                "size-1.5 shrink-0 rounded-full",
                slide.status === "completed"
                  ? "bg-success"
                  : slide.status === "failed"
                    ? "bg-destructive"
                    : "bg-input",
              )}
            />
            <span className="truncate">
              第 {slide.index} 页 · {slide.title}
            </span>
          </div>
        )}
      </div>

      <div ref={scrollRef} className="min-h-0 flex-1 overflow-y-auto px-4 py-4">
        {messages.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center px-2 text-center">
            <span className="mb-3 flex size-11 items-center justify-center rounded-2xl bg-accent text-xl text-primary">
              ✦
            </span>
            <div className="text-[13px] font-bold">和我聊聊这一页</div>
            <div className="mt-1.5 text-xs leading-relaxed text-hint">
              只修改当前选中的页面，
              <br />
              不会影响整份演示。试试说：
            </div>
            <div className="mt-4 flex w-full flex-col gap-1.5">
              {STARTER_CHIPS.map((chip) => (
                <button
                  key={chip}
                  type="button"
                  className="rounded-[9px] border bg-muted/60 px-3 py-2 text-left text-xs text-muted-foreground transition-colors hover:border-primary/60 hover:text-primary"
                  onClick={() => send(chip)}
                >
                  「{chip}」
                </button>
              ))}
            </div>
          </div>
        ) : (
          <div className="flex flex-col gap-3">
            {messages.map((message) => (
              <ChatBubble key={message.id} message={message} />
            ))}
          </div>
        )}
      </div>

      <div className="border-t px-4 pt-3 pb-4">
        <div className="mb-2.5 flex flex-wrap gap-1.5">
          {QUICK_CHIPS.map((chip) => (
            <button
              key={chip}
              type="button"
              className="rounded-full border bg-muted/60 px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:border-primary/60 hover:text-primary disabled:opacity-50"
              disabled={!slide || busy}
              onClick={() => send(chip)}
            >
              {chip}
            </button>
          ))}
        </div>
        {notReady && (
          <div className="mb-2 text-[11px] text-hint">
            {slide?.status === "failed"
              ? "本页生成失败，请先重试生成，指令会排队执行"
              : "本页尚未生成完成，稍等片刻即可编辑"}
          </div>
        )}
        <div className="flex items-end gap-2">
          <Textarea
            value={draft}
            rows={1}
            className="max-h-28 min-h-10 flex-1 resize-none rounded-[10px] bg-background/60 text-[13px]"
            placeholder="描述想怎么修改这一页…"
            disabled={!slide}
            onChange={(event) => updateDraft(event.target.value)}
            onKeyDown={(event) => {
              if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
                event.preventDefault()
                // Mirror the send button: no dispatch while an edit is
                // already pending on this slide.
                if (!busy) {
                  send(draft)
                }
              }
            }}
          />
          <Button
            size="icon-lg"
            className="rounded-[10px]"
            aria-label="发送修改指令"
            disabled={!slide || !draft.trim() || busy}
            onClick={() => send(draft)}
          >
            <ArrowUpIcon />
          </Button>
        </div>
      </div>
    </div>
  )
}

// ---- run log drawer ----

function RunLogSheet() {
  const logs = useWorkbenchStore((state) => state.logs)
  const [autoScroll, setAutoScroll] = useState(true)
  const viewportRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    if (autoScroll) {
      viewportRef.current?.scrollTo({ top: viewportRef.current.scrollHeight })
    }
  }, [logs.length, autoScroll])

  return (
    <Sheet>
      <SheetTrigger render={<Button variant="ghost" size="sm" className="text-muted-foreground" />}>
        <FileClockIcon data-icon="inline-start" />
        运行详情
      </SheetTrigger>
      <SheetContent className="w-[min(94vw,480px)] gap-0 sm:max-w-[480px]">
        <SheetHeader className="border-b">
          <SheetTitle>运行详情</SheetTitle>
          <SheetDescription>
            结构化事件流按时间排列，仅用于排查，不影响主界面进度展示。
          </SheetDescription>
        </SheetHeader>
        <div ref={viewportRef} className="min-h-0 flex-1 overflow-y-auto bg-muted/40 px-4 py-3">
          <ol className="space-y-1 font-mono text-[11px] leading-5 text-muted-foreground">
            {logs.map((entry) => (
              <li key={entry.seq} className="flex gap-2">
                <span className="shrink-0 tabular-nums text-hint/80">{entry.time}</span>
                <span className="shrink-0 text-primary/80">{entry.type}</span>
                <span className="min-w-0 break-all text-foreground/80">{entry.message}</span>
              </li>
            ))}
            {logs.length === 0 && <li className="text-hint">暂无事件</li>}
          </ol>
        </div>
        <div className="flex items-center justify-between border-t px-4 py-2.5">
          <span className="text-[11px] text-hint">{logs.length} 条事件</span>
          <Button variant="outline" size="xs" onClick={() => setAutoScroll((value) => !value)}>
            {autoScroll ? "暂停滚动" : "恢复滚动"}
          </Button>
        </div>
      </SheetContent>
    </Sheet>
  )
}

// ---- page ----

export function WorkbenchPage() {
  const { taskId } = useParams<{ taskId: string }>()
  const location = useLocation()
  const requestedPages =
    (location.state as { requestedPages?: number } | null)?.requestedPages ?? 0
  const task = useWorkbenchStore((state) => state.task)
  const slidesMap = useWorkbenchStore((state) => state.slides)
  const slideOrder = useWorkbenchStore((state) => state.slideOrder)
  const selectedSlideId = useWorkbenchStore((state) => state.selectedSlideId)
  const followLatest = useWorkbenchStore((state) => state.followLatest)
  const connection = useWorkbenchStore((state) => state.connection)
  const exporting = useWorkbenchStore((state) => state.exporting)
  const hydrated = useWorkbenchStore((state) => state.hydrated)
  const loadError = useWorkbenchStore((state) => state.loadError)
  const {
    hydrate,
    applyEvent,
    setConnection,
    selectSlide,
    resumeFollow,
    cancelTask,
    resumeTask,
    exportTask,
    undo,
    applyRevision,
    retrySlide,
    loadRevisions,
  } = useWorkbenchStore.getState()

  const templates = useTemplatesStore((state) => state.templates)
  const templatesLoaded = useTemplatesStore((state) => state.loaded)
  const [cancelOpen, setCancelOpen] = useState(false)
  const [revisionsLoading, setRevisionsLoading] = useState(false)
  const [nowMs, setNowMs] = useState(() => Date.now())

  useEffect(() => {
    if (!templatesLoaded) {
      void useTemplatesStore.getState().fetchTemplates()
    }
  }, [templatesLoaded])

  useEffect(() => {
    if (!taskId) {
      return
    }
    let dispose: (() => void) | undefined
    let disposed = false
    void hydrate(taskId, requestedPages).then(() => {
      if (disposed || useWorkbenchStore.getState().loadError) {
        return
      }
      dispose = connectTaskEvents(
        taskId,
        useWorkbenchStore.getState().lastSeq,
        applyEvent,
        setConnection,
      )
    })
    return () => {
      disposed = true
      dispose?.()
    }
  }, [taskId, requestedPages, hydrate, applyEvent, setConnection])

  const running = task?.status === "running"

  useEffect(() => {
    if (!running) {
      return
    }
    const timer = setInterval(() => setNowMs(Date.now()), 1000)
    return () => clearInterval(timer)
  }, [running])

  const slides = useMemo(
    () => slideOrder.map((id) => slidesMap.get(id)!).filter(Boolean),
    [slideOrder, slidesMap],
  )
  const selected = (selectedSlideId ? slidesMap.get(selectedSlideId) : null) ?? slides[0] ?? null

  useEffect(() => {
    const onKey = (event: KeyboardEvent): void => {
      if (/INPUT|TEXTAREA/.test((event.target as HTMLElement).tagName)) {
        return
      }
      if (!selected) {
        return
      }
      const index = slides.findIndex((slide) => slide.id === selected.id)
      if (event.key === "ArrowDown" || event.key === "ArrowRight") {
        const next = slides[Math.min(slides.length - 1, index + 1)]
        if (next) selectSlide(next.id)
      }
      if (event.key === "ArrowUp" || event.key === "ArrowLeft") {
        const prev = slides[Math.max(0, index - 1)]
        if (prev) selectSlide(prev.id)
      }
    }
    document.addEventListener("keydown", onKey)
    return () => document.removeEventListener("keydown", onKey)
  }, [slides, selected, selectSlide])

  if (!hydrated) {
    return (
      <div className="flex h-svh items-center justify-center bg-background text-sm text-muted-foreground">
        <LoaderCircleIcon className="mr-2 size-4 animate-spin text-primary" />
        正在载入任务…
      </div>
    )
  }

  if (loadError || !task) {
    return (
      <div className="flex h-svh flex-col items-center justify-center gap-3 bg-background">
        <TriangleAlertIcon className="size-8 text-hint" />
        <div className="text-sm font-medium">{loadError ?? "任务不存在"}</div>
        <Button variant="outline" size="sm" render={<Link to="/" />}>
          返回新建任务
        </Button>
      </div>
    )
  }

  const template = templates.find((item) => item.id === task.template_id)
  const doneCount = slides.filter((slide) => slide.status === "completed").length
  const failedCount = slides.filter((slide) => slide.status === "failed").length
  const activeCount = slides.filter(
    (slide) => slide.status === "generating" || slide.status === "editing",
  ).length
  const allDone = slides.length > 0 && doneCount === slides.length
  const isBooting = running && (task.stage === "research" || task.stage === "plan")
  const cancelled = task.status === "cancelled"
  const elapsedSeconds = Math.max(
    0,
    Math.floor(
      ((running ? nowMs : new Date(task.updated_at).getTime()) -
        new Date(task.created_at).getTime()) /
        1000,
    ),
  )
  const canvasMaxWidth = task.ratio === "4:3" ? 760 : 880
  // Export only after the backend confirms the task is over; a window where
  // every slide looks done but task.completed has not arrived must not count.
  const exportDisabled = !allDone || exporting || running
  const revisions = selected?.revisions ?? []
  const undoDisabled = !selected || selected.status !== "completed" || selected.revision <= 1

  return (
    <div className="flex h-svh min-h-[620px] flex-col overflow-hidden bg-background">
      {/* ══ top bar ══ */}
      <header className="z-30 flex h-14 shrink-0 items-center gap-4 border-b bg-card pr-3 pl-5">
        <Link to="/" className="flex shrink-0 items-center gap-2.5 text-foreground" title="返回创建">
          <BrandMark />
          <span className="font-heading text-[15px] font-bold">PPTAgent</span>
        </Link>
        <span className="h-[22px] w-px shrink-0 bg-border" />
        <div className="min-w-0">
          <div className="max-w-72 truncate text-[13px] font-bold">{task.topic}</div>
          <div className="flex items-center gap-1.5 text-[11px] text-hint">
            {template && (
              <span className="inline-flex gap-[3px]">
                {[template.palette.bg, template.palette.primary, template.palette.accent].map(
                  (color, index) => (
                    <span
                      key={index}
                      className="inline-block size-2 rounded-full border border-black/10"
                      style={{ backgroundColor: color }}
                    />
                  ),
                )}
              </span>
            )}
            <span className="truncate">
              {template?.name ?? "模板"} · {task.ratio} · {slides.length || task.total_slides} 页
            </span>
          </div>
        </div>

        {/* center progress: running / completed / failed / cancelled */}
        <div className="flex min-w-0 flex-1 items-center justify-center gap-3.5">
          {running && (
            <>
              <span role="status" className="whitespace-nowrap text-xs text-muted-foreground">
                {STAGE_TEXT[task.stage] ?? "生成中…"}
              </span>
              <ProgressSegments slides={slides} />
              <span className="font-heading whitespace-nowrap text-xs tabular-nums text-muted-foreground">
                {doneCount}/{slides.length || task.total_slides} · {formatClock(elapsedSeconds)}
              </span>
            </>
          )}
          {task.status === "completed" &&
            (activeCount > 0 ? (
              <span
                role="status"
                className="flex items-center gap-1.5 rounded-full bg-accent px-3.5 py-1.5 text-xs font-medium text-accent-foreground"
              >
                <LoaderCircleIcon className="size-3.5 animate-spin" />
                正在更新 {activeCount} 页…
              </span>
            ) : failedCount === 0 ? (
              <span
                role="status"
                className="flex items-center gap-1.5 rounded-full bg-success-subtle px-3.5 py-1.5 text-xs font-bold text-success"
              >
                <CheckIcon className="size-3.5" strokeWidth={3} />
                生成完成 · 共 {slides.length} 页 · 用时 {formatClock(elapsedSeconds)}
              </span>
            ) : (
              <span
                role="status"
                className="flex items-center gap-1.5 rounded-full bg-destructive/10 px-3.5 py-1.5 text-xs font-bold text-destructive"
              >
                <TriangleAlertIcon className="size-3.5" />
                生成结束 · {failedCount} 页失败，可在左侧重试
              </span>
            ))}
          {task.status === "failed" && (
            <span
              role="status"
              className="flex items-center gap-1.5 rounded-full bg-destructive/10 px-3.5 py-1.5 text-xs font-bold text-destructive"
            >
              <TriangleAlertIcon className="size-3.5" />
              任务失败 · 已完成 {doneCount}/{slides.length || task.total_slides} 页
            </span>
          )}
          {cancelled && (
            <span role="status" className="rounded-full bg-secondary px-3.5 py-1.5 text-xs font-medium text-hint">
              已暂停 · 完成 {doneCount}/{slides.length} 页
            </span>
          )}
          {!followLatest && running && (
            <Button variant="outline" size="xs" className="rounded-full" onClick={resumeFollow}>
              <ListStartIcon data-icon="inline-start" />
              回到最新
            </Button>
          )}
        </div>

        <div className="flex shrink-0 items-center gap-2">
          <RunLogSheet />
          {(running || isBooting) && (
            <Button
              variant="outline"
              size="sm"
              className="h-8 text-muted-foreground hover:border-destructive/50 hover:bg-card hover:text-destructive"
              onClick={() => setCancelOpen(true)}
            >
              <PauseIcon data-icon="inline-start" />
              取消生成
            </Button>
          )}
          {cancelled && (
            <Button
              variant="outline"
              size="sm"
              className="h-8 border-primary/60 bg-accent text-accent-foreground hover:bg-accent hover:text-primary"
              onClick={() => void resumeTask()}
            >
              <PlayIcon data-icon="inline-start" />
              继续生成
            </Button>
          )}
          {task.status === "failed" && (
            <Button
              variant="outline"
              size="sm"
              className="h-8 border-primary/60 bg-accent text-accent-foreground hover:bg-accent hover:text-primary"
              onClick={() => void resumeTask()}
            >
              <RotateCcwIcon data-icon="inline-start" />
              重试生成
            </Button>
          )}
          <DropdownMenu>
            <DropdownMenuTrigger
              render={<Button size="sm" className="h-8 px-3.5" disabled={exportDisabled} />}
            >
              {exporting ? (
                <LoaderCircleIcon data-icon="inline-start" className="animate-spin" />
              ) : (
                <DownloadIcon data-icon="inline-start" />
              )}
              {exporting ? "导出中" : "导出"}
              <ChevronDownIcon data-icon="inline-end" />
            </DropdownMenuTrigger>
            <DropdownMenuContent align="end" className="w-56">
              {/* Base UI GroupLabel must live inside a Group, or it throws. */}
              <DropdownMenuGroup>
                <DropdownMenuLabel>导出演示文稿</DropdownMenuLabel>
                <DropdownMenuSeparator />
                <DropdownMenuItem onClick={() => void exportTask("pptx")}>
                  <DownloadIcon />
                  <div>
                    <div>PPTX</div>
                    <div className="text-[10px] text-hint">可在 PowerPoint 中继续编辑</div>
                  </div>
                </DropdownMenuItem>
                <DropdownMenuItem onClick={() => void exportTask("pdf")}>
                  <FileClockIcon />
                  <div>
                    <div>PDF</div>
                    <div className="text-[10px] text-hint">适合直接分享与打印</div>
                  </div>
                </DropdownMenuItem>
              </DropdownMenuGroup>
            </DropdownMenuContent>
          </DropdownMenu>
        </div>
      </header>

      {connection === "reconnecting" && (
        <div
          role="status"
          className="flex h-8 shrink-0 items-center justify-center gap-2 border-b border-amber-200 bg-amber-50 text-xs text-amber-800"
        >
          <WifiOffIcon className="size-3.5" />
          连接中断，正在重连…任务仍在后台继续
        </div>
      )}

      {/* ══ three columns ══ */}
      <main className="flex min-h-0 flex-1">
        <aside className="w-[236px] shrink-0 border-r bg-muted">
          <ScrollArea className="h-full">
            <div className="px-3.5 py-3.5">
              <div className="mb-2.5 px-0.5 text-[11px] font-bold tracking-[0.08em] text-hint">
                幻灯片
              </div>
              <div className="flex flex-col gap-3">
                {slides.map((slide) => (
                  <SlideThumb
                    key={slide.id}
                    slide={slide}
                    ratio={task.ratio}
                    paused={cancelled}
                    selected={selected?.id === slide.id}
                    onSelect={() => selectSlide(slide.id)}
                  />
                ))}
              </div>
            </div>
          </ScrollArea>
        </aside>

        <section className="flex min-w-0 flex-1 flex-col items-center justify-center overflow-auto px-8 py-6">
          {isBooting ? (
            <BootCard topic={task.topic} stage={task.stage} total={task.total_slides} />
          ) : (
            selected && (
              <div className="w-full" style={{ maxWidth: canvasMaxWidth }}>
                <div className="overflow-hidden rounded-xl shadow-[0_14px_44px_rgba(30,25,15,0.16)]">
                  <SlideCanvas
                    slide={selected}
                    ratio={task.ratio}
                    paused={cancelled}
                    onRetry={() => void retrySlide(selected.id)}
                  />
                </div>
                <div className="mt-3.5 flex items-center gap-2.5">
                  <DropdownMenu
                    onOpenChange={(open) => {
                      if (open && selected.status === "completed") {
                        setRevisionsLoading(true)
                        void loadRevisions(selected.id).finally(() =>
                          setRevisionsLoading(false),
                        )
                      }
                    }}
                  >
                    <DropdownMenuTrigger
                      render={
                        <Button
                          variant="outline"
                          size="sm"
                          className="h-8 bg-card"
                          disabled={selected.status !== "completed"}
                        />
                      }
                    >
                      <HistoryIcon data-icon="inline-start" />
                      版本 {selected.revision > 0 ? `v${selected.revision}` : "—"}
                      <ChevronDownIcon data-icon="inline-end" />
                    </DropdownMenuTrigger>
                    <DropdownMenuContent align="start" className="w-64">
                      <DropdownMenuGroup>
                        <DropdownMenuLabel>本页版本历史</DropdownMenuLabel>
                        <DropdownMenuSeparator />
                        {[...revisions].reverse().map((revision) => (
                          <DropdownMenuItem
                            key={revision.revision}
                            // Applying the current revision is a no-op on the
                            // server; the store guards it too, but the menu
                            // should not offer it in the first place.
                            disabled={revision.revision === selected.revision}
                            onClick={() => void applyRevision(selected.id, revision.revision)}
                          >
                            <span
                              className={cn(
                                "font-heading text-xs font-bold",
                                revision.revision === selected.revision
                                  ? "text-primary"
                                  : "text-hint",
                              )}
                            >
                              v{revision.revision}
                            </span>
                            <span className="flex-1">{revision.label}</span>
                            {revision.revision === selected.revision && (
                              <span className="text-[10px] font-bold text-primary">当前</span>
                            )}
                          </DropdownMenuItem>
                        ))}
                        {revisions.length === 0 && (
                          <DropdownMenuItem disabled>
                            {revisionsLoading ? "载入版本历史…" : "暂无历史版本"}
                          </DropdownMenuItem>
                        )}
                      </DropdownMenuGroup>
                    </DropdownMenuContent>
                  </DropdownMenu>
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-8 bg-card"
                    disabled={undoDisabled}
                    onClick={() => void undo(selected.id)}
                  >
                    <Undo2Icon data-icon="inline-start" />
                    撤销
                  </Button>
                  <span className="flex-1" />
                  <span className="text-xs text-hint">
                    {selected.status === "completed" && revisions.length > 1
                      ? `已编辑 · ${revisions.length} 个版本`
                      : `第 ${selected.index} / ${slides.length} 页`}
                  </span>
                  <Button
                    variant="outline"
                    size="sm"
                    className="h-8 bg-card hover:border-primary/60 hover:text-primary"
                    disabled={selected.status !== "completed"}
                    onClick={() => void retrySlide(selected.id)}
                  >
                    <RotateCcwIcon data-icon="inline-start" />
                    重新生成本页
                  </Button>
                </div>
                {selected.status === "generating" && (
                  <div className="mt-3 flex items-center justify-center gap-2 text-xs text-hint">
                    <SparklesIcon className="size-3.5 animate-soft-pulse text-primary" />
                    页面生成中，完成后会自动更新预览
                  </div>
                )}
              </div>
            )
          )}
        </section>

        <aside className="w-[360px] shrink-0 border-l">
          <ChatPanel slide={selected} />
        </aside>
      </main>

      {/* cancel confirm */}
      <AlertDialog open={cancelOpen} onOpenChange={setCancelOpen}>
        <AlertDialogContent className="sm:max-w-sm">
          <AlertDialogHeader>
            <AlertDialogTitle>取消生成？</AlertDialogTitle>
            <AlertDialogDescription>
              已完成的 {doneCount} 页会保留，可继续查看和编辑；未生成的页面将暂停，之后可随时继续。
            </AlertDialogDescription>
          </AlertDialogHeader>
          <AlertDialogFooter>
            <Button variant="outline" onClick={() => setCancelOpen(false)}>
              继续生成
            </Button>
            <Button
              variant="destructive"
              onClick={() => {
                setCancelOpen(false)
                void cancelTask()
              }}
            >
              确认取消
            </Button>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </div>
  )
}
