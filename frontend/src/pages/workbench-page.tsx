import { useEffect, useId, useLayoutEffect, useMemo, useRef, useState } from "react"
import { Link, useLocation, useParams } from "react-router-dom"
import {
  ArrowLeftIcon,
  ArrowUpIcon,
  CheckIcon,
  ChevronDownIcon,
  DownloadIcon,
  FileClockIcon,
  HistoryIcon,
  ListStartIcon,
  LoaderCircleIcon,
  MessageSquareIcon,
  PanelLeftIcon,
  PauseIcon,
  PlayIcon,
  RotateCcwIcon,
  SparklesIcon,
  TriangleAlertIcon,
  Undo2Icon,
  WifiOffIcon,
} from "lucide-react"

import { BrandMark } from "@/components/brand-mark"
import { SceneBackground } from "@/components/scene-background"
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
import { Label } from "@/components/ui/label"
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
import { usePageMetadata } from "@/lib/use-page-metadata"
import { useTemplatesStore } from "@/stores/templates-store"
import { useWorkbenchStore } from "@/stores/workbench-store"
import type { SlideView, WorkChatMessage } from "@/stores/workbench-store"
import type { TaskStage, TaskStatus } from "@/types/api"

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

function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(() => window.matchMedia(query).matches)
  useEffect(() => {
    const media = window.matchMedia(query)
    const update = (): void => setMatches(media.matches)
    update()
    media.addEventListener("change", update)
    return () => media.removeEventListener("change", update)
  }, [query])
  return matches
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
  const src = previewSrc(slide.previewUrl, slide.previewVersion)
  const statusText =
    slide.status === "completed"
      ? "已完成"
      : slide.status === "failed"
        ? "生成失败"
        : slide.status === "queued"
          ? paused
            ? "已暂停"
            : "排队中"
          : slide.status === "editing"
            ? "修改中"
            : "生成中"
  return (
    <div className="flex items-start gap-2">
      <span
        className={cn(
          "font-heading w-4 pt-0.5 text-right text-[10px] font-semibold tabular-nums",
          selected ? "text-foreground" : "text-hint/80",
        )}
      >
        {slide.index}
      </span>
      <button
        type="button"
        title={slide.title}
        aria-label={`第 ${slide.index} 页，${slide.title}，${statusText}`}
        aria-pressed={selected}
        aria-current={selected ? "page" : undefined}
        onClick={onSelect}
        className={cn(
          "relative block min-w-0 flex-1 overflow-hidden rounded-[8px] text-left",
          ratio === "4:3" ? "aspect-[4/3]" : "aspect-video",
          slide.status === "completed" || slide.status === "editing"
            ? "bg-card shadow-[0_2px_8px_rgba(30,32,44,0.08),inset_0_0_0_1px_rgba(27,28,32,0.06)] hover:shadow-[0_4px_12px_rgba(30,32,44,0.16)]"
            : slide.status === "generating"
              ? "skeleton-shimmer"
              : slide.status === "failed"
                ? "border-[1.5px] border-destructive/60"
                : "border-[1.5px] border-dashed border-border bg-white/25",
          selected ? "ring-2 ring-primary" : slide.status === "generating" && "ring-2 ring-primary/60",
        )}
      >
        {src && (
          <img
            src={src}
            alt={slide.title}
            draggable={false}
            className="pointer-events-none absolute inset-0 size-full select-none object-cover"
          />
        )}
        {slide.status === "editing" && (
          <span className="absolute inset-0 flex items-center justify-center bg-card/55">
            <LoaderCircleIcon className="size-4 animate-spin" />
          </span>
        )}
        {slide.status === "queued" && (
          <span className="absolute inset-0 flex items-center justify-center text-[9.5px] text-hint">
            {paused ? "已暂停" : "排队中"}
          </span>
        )}
        {slide.status === "failed" && (
          <span className="absolute inset-0 flex flex-col items-center justify-center gap-1 bg-destructive/10 text-[10px] font-bold text-destructive">
            <TriangleAlertIcon className="size-3.5" />
            生成失败 · 点击重试
          </span>
        )}
      </button>
    </div>
  )
}

function SlidesPanel({
  slides,
  ratio,
  paused,
  selectedId,
  onSelect,
}: {
  slides: SlideView[]
  ratio: "16:9" | "4:3"
  paused: boolean
  selectedId: string | null
  onSelect: (slideId: string) => void
}) {
  return (
    <ScrollArea className="h-full select-none [&_[data-slot=scroll-area-scrollbar]]:hidden">
      <div className="flex flex-col gap-2.5 px-3 py-3">
        {slides.map((slide) => (
          <SlideThumb
            key={slide.id}
            slide={slide}
            ratio={ratio}
            paused={paused}
            selected={selectedId === slide.id}
            onSelect={() => onSelect(slide.id)}
          />
        ))}
      </div>
    </ScrollArea>
  )
}

// ---- center column ----

function BootCanvas({ stage, ratio }: { stage: TaskStage; ratio: "16:9" | "4:3" }) {
  return (
    <div
      className={cn(
        "relative flex w-full flex-col justify-center gap-4 bg-card px-[10%]",
        ratio === "4:3" ? "aspect-[4/3]" : "aspect-video",
      )}
    >
      <div className="skeleton-shimmer h-[7%] w-[46%] rounded-lg" />
      <div className="skeleton-shimmer h-[3.6%] w-[72%] rounded-md" />
      <div className="skeleton-shimmer h-[3.6%] w-[64%] rounded-md" />
      <div className="absolute bottom-[7%] left-[10%] flex items-center gap-2 text-sm text-hint">
        <LoaderCircleIcon className="size-4 animate-spin" />
        {STAGE_TEXT[stage] ?? "正在准备你的演示…"}
      </div>
    </div>
  )
}

interface PreviewLayer {
  key: string
  src: string
  phase: "entering" | "present" | "leaving"
}

const PREVIEW_TRANSITION_MS = 160

function usePreviewLayers(
  slide: SlideView,
  src: string | null | undefined,
): PreviewLayer[] {
  const previewKey = `${slide.id}:${slide.revision}:${slide.previewVersion}`
  const [layers, setLayers] = useState<PreviewLayer[]>(() =>
    src ? [{ key: previewKey, src, phase: "present" }] : [],
  )
  const slideIdRef = useRef(slide.id)
  const previewKeyRef = useRef(previewKey)
  const requestRef = useRef(0)

  useLayoutEffect(() => {
    const request = ++requestRef.current
    let frame = 0
    let cleanupTimer = 0

    if (slideIdRef.current !== slide.id) {
      slideIdRef.current = slide.id
      previewKeyRef.current = previewKey
      setLayers(src ? [{ key: previewKey, src, phase: "present" }] : [])
      return
    }

    if (!src) {
      previewKeyRef.current = previewKey
      setLayers([])
      return
    }

    if (previewKeyRef.current === previewKey) {
      return
    }

    // A newer preview can arrive before the previous entrance frame runs.
    // Settle the already-loaded newest layer first so cancelling that frame
    // can never leave the canvas transparent while the next image preloads.
    setLayers((current) => {
      const newest = current[current.length - 1]
      if (!newest || (current.length === 1 && newest.phase === "present")) {
        return current
      }
      return [{ ...newest, phase: "present" }]
    })

    const image = new Image()
    let committed = false
    const commit = (): void => {
      if (committed || requestRef.current !== request || slideIdRef.current !== slide.id) {
        return
      }
      committed = true
      previewKeyRef.current = previewKey
      setLayers((current) => [
        ...current.map((layer) => ({ ...layer, phase: "leaving" as const })),
        { key: previewKey, src, phase: "entering" as const },
      ].slice(-2))
      frame = window.requestAnimationFrame(() => {
        setLayers((current) =>
          current.map((layer) =>
            layer.key === previewKey ? { ...layer, phase: "present" } : layer,
          ),
        )
      })
      cleanupTimer = window.setTimeout(() => {
        setLayers((current) => current.filter((layer) => layer.key === previewKey))
      }, PREVIEW_TRANSITION_MS)
    }

    image.addEventListener("load", commit, { once: true })
    image.src = src
    if (image.complete && image.naturalWidth > 0) {
      commit()
    }

    return () => {
      image.removeEventListener("load", commit)
      window.cancelAnimationFrame(frame)
      window.clearTimeout(cleanupTimer)
    }
  }, [previewKey, slide.id, src])

  return layers
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
  const src = previewSrc(slide.previewUrl, slide.previewVersion)
  const previewLayers = usePreviewLayers(slide, src)
  const aspect = ratio === "4:3" ? "aspect-[4/3]" : "aspect-video"
  const previewBusy = slide.status === "editing" || slide.status === "generating"
  const busyLabelRef = useRef("正在生成本页…")
  if (slide.status === "editing") {
    busyLabelRef.current = "正在修改本页…"
  } else if (slide.status === "generating") {
    busyLabelRef.current = "正在生成本页…"
  }

  if (slide.status === "failed") {
    return (
      <div
        className={cn(
          "relative flex w-full flex-col items-center justify-center gap-1.5 overflow-hidden bg-card",
          aspect,
        )}
      >
        <div
          aria-hidden="true"
          className="absolute inset-0"
          style={{
            background:
              "radial-gradient(58% 72% at 50% 30%, color-mix(in srgb, var(--destructive) 13%, transparent), transparent 72%)",
          }}
        />
        <span className="animate-light-up relative flex size-14 items-center justify-center rounded-full bg-destructive text-white shadow-[0_14px_34px_color-mix(in_srgb,var(--destructive)_45%,transparent)]">
          <TriangleAlertIcon className="size-6" />
        </span>
        <div className="relative mt-2.5 font-serif text-[22px] font-black tracking-tight">
          本页生成失败
        </div>
        <div className="relative text-sm text-hint">
          内容引擎响应超时，不影响其他页面
        </div>
        <Button
          className="relative mt-3.5 h-10 rounded-full bg-destructive px-5 text-white shadow-[0_10px_26px_color-mix(in_srgb,var(--destructive)_40%,transparent)] hover:bg-destructive/85"
          onClick={onRetry}
        >
          <RotateCcwIcon />
          重试生成本页
        </Button>
      </div>
    )
  }

  if (slide.status === "queued") {
    return (
      <div className={cn("flex w-full flex-col items-center justify-center gap-1.5 bg-card", aspect)}>
        <div className="text-lg text-hint">{paused ? "本页已暂停生成" : "本页排队中"}</div>
        <div className="text-sm text-hint/80">
          {paused ? "点击右上角「继续生成」恢复" : "完成前面的页面后将自动开始"}
        </div>
      </div>
    )
  }

  if (slide.status === "generating" && !src) {
    return (
      <div className={cn("relative flex w-full flex-col justify-center gap-4 bg-card px-[10%]", aspect)}>
        <div className="skeleton-shimmer h-[7%] w-[46%] rounded-lg" />
        <div className="skeleton-shimmer h-[3.6%] w-[72%] rounded-md" />
        <div className="skeleton-shimmer h-[3.6%] w-[64%] rounded-md" />
        <div className="absolute bottom-[7%] left-[10%] flex items-center gap-2 text-sm text-hint">
          <LoaderCircleIcon className="size-4 animate-spin" />
          AI 正在撰写本页内容…
        </div>
      </div>
    )
  }

  return (
    <div className={cn("relative w-full bg-card", aspect)}>
      {previewLayers.map((layer, index) => (
        <img
          key={layer.key}
          src={layer.src}
          alt={index === previewLayers.length - 1 ? slide.title : ""}
          aria-hidden={index !== previewLayers.length - 1}
          data-phase={layer.phase}
          draggable={false}
          className="slide-preview-layer pointer-events-none absolute inset-0 size-full select-none object-cover"
        />
      ))}
      <div
        data-visible={previewBusy}
        aria-hidden={!previewBusy}
        className="slide-preview-busy absolute inset-0 flex items-center justify-center bg-card/45"
      >
        <span className="flex items-center gap-2 rounded-full bg-card/95 px-4 py-2 text-xs font-medium text-muted-foreground shadow-sm">
          <LoaderCircleIcon className="size-3.5 animate-spin" />
          {busyLabelRef.current}
        </span>
      </div>
    </div>
  )
}

// ---- right column ----

function GenerationHeader({
  stage,
  done,
  total,
  success,
}: {
  stage: TaskStage
  done: number
  total: number
  success: boolean
}) {
  const pct = success ? 100 : total > 0 ? Math.min(100, Math.round((done / total) * 100)) : 0
  const circumference = 2 * Math.PI * 22
  return (
    <div className="flex items-center gap-3.5 px-4 py-4">
      <span className="relative size-[52px] shrink-0">
        <svg width="52" height="52" viewBox="0 0 52 52" className="-rotate-90">
          <circle cx="26" cy="26" r="22" fill="none" stroke="rgba(27,28,32,0.1)" strokeWidth="5" />
          <circle
            cx="26"
            cy="26"
            r="22"
            fill="none"
            stroke="var(--primary)"
            strokeWidth="5"
            strokeLinecap="round"
            strokeDasharray={circumference}
            strokeDashoffset={circumference * (1 - pct / 100)}
            className="transition-[stroke-dashoffset] duration-500"
          />
        </svg>
        {success ? (
          // Apple-Pay-style checkmark: draws in once the ring closes.
          <svg
            width="52"
            height="52"
            viewBox="0 0 52 52"
            className="absolute inset-0"
            aria-hidden="true"
          >
            <path
              d="M17 27.5l6.5 6.5L36 20.5"
              fill="none"
              stroke="var(--primary)"
              strokeWidth="4"
              strokeLinecap="round"
              strokeLinejoin="round"
              strokeDasharray="30"
              strokeDashoffset="30"
              style={{ animation: "draw-stroke 0.4s ease-out 0.25s forwards" }}
            />
          </svg>
        ) : (
          <span className="font-heading absolute inset-0 flex items-center justify-center text-[11px] font-bold tabular-nums">
            {pct}%
          </span>
        )}
      </span>
      <div className="min-w-0">
        <div className="font-serif text-[15px] font-extrabold tracking-tight">
          {success ? "生成完成" : "正在生成演示"}
        </div>
        <div className="mt-0.5 text-xs text-hint">
          {success
            ? `共 ${total} 页 · 可以导出了`
            : `${STAGE_TEXT[stage] ?? "生成中…"} · 已完成 ${done}/${total} 页`}
        </div>
      </div>
    </div>
  )
}

// Agent-panel step timeline, per the liquid-glass design.
function GenerationSteps({
  stage,
  done,
  total,
  success,
}: {
  stage: TaskStage
  done: number
  total: number
  success: boolean
}) {
  const order: TaskStage[] = ["research", "plan", "generate"]
  const activeIdx = success
    ? Number.POSITIVE_INFINITY
    : stage === "template"
      ? 0
      : Math.max(order.indexOf(stage), 0)
  const steps = [
    { label: "解析参考资料", meta: "主题与素材分析" },
    { label: "规划大纲结构", meta: `${total} 页` },
    {
      label: "逐页生成内容",
      meta: done > 0 ? `已完成 ${done}/${total} 页` : "等待中",
    },
  ]
  return (
    <div className="px-4 pt-1 pb-1.5">
      {steps.map((step, index) => {
        const state =
          index < activeIdx ? "done" : index === activeIdx ? "active" : "pending"
        return (
          <div key={step.label} className="flex gap-3">
            <div className="flex w-[18px] flex-col items-center">
              {state === "done" ? (
                <span className="flex size-[18px] shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground">
                  <CheckIcon className="size-2.5" strokeWidth={3.5} />
                </span>
              ) : state === "active" ? (
                <span className="size-[18px] shrink-0 animate-spin rounded-full border-[2.5px] border-primary/15 border-t-primary" />
              ) : (
                <span className="size-[18px] shrink-0 rounded-full border-[1.5px] border-dashed border-primary/25" />
              )}
              {index < steps.length - 1 && (
                <span className="mt-1 mb-0.5 min-h-2 w-[1.5px] flex-1 rounded-full bg-border" />
              )}
            </div>
            <div className={cn("min-w-0 pb-2.5", state === "pending" && "opacity-50")}>
              <div
                className={cn(
                  "text-[13px] leading-[18px]",
                  state === "active" ? "font-semibold" : state === "pending" && "text-hint",
                )}
              >
                {step.label}
              </div>
              <div className="mt-px text-[11px] text-hint">{step.meta}</div>
            </div>
          </div>
        )
      })}
    </div>
  )
}

/**
 * Generation tracker above the chat. On completion it plays a checkmark
 * (Apple-Pay style), holds briefly, then collapses so the chat slides up
 * in sync with its exit.
 */
function GenerationPanel({
  status,
  stage,
  done,
  total,
}: {
  status: TaskStatus
  stage: TaskStage
  done: number
  total: number
}) {
  const running = status === "running"
  const [phase, setPhase] = useState<"hidden" | "running" | "success" | "exit">(
    running ? "running" : "hidden",
  )

  useEffect(() => {
    if (running) {
      setPhase("running")
      return
    }
    setPhase((prev) => {
      // Only celebrate a completion we actually watched happen; a task that
      // hydrates as completed (or ends failed/cancelled) shows no tracker.
      if (prev === "running") {
        return status === "completed" ? "success" : "hidden"
      }
      return prev === "success" || prev === "exit" ? prev : "hidden"
    })
  }, [running, status])

  useEffect(() => {
    if (phase === "success") {
      const timer = setTimeout(() => setPhase("exit"), 1800)
      return () => clearTimeout(timer)
    }
    if (phase === "exit") {
      const timer = setTimeout(() => setPhase("hidden"), 650)
      return () => clearTimeout(timer)
    }
  }, [phase])

  if (phase === "hidden") {
    return null
  }
  const success = phase === "success" || phase === "exit"
  return (
    <div
      data-phase={phase}
      className={cn(
        "generation-tracker flex-none overflow-hidden border-b border-border/70 transition-all duration-600 ease-in-out",
        phase === "exit" ? "max-h-0 border-transparent opacity-0" : "max-h-[300px] opacity-100",
      )}
    >
      <GenerationHeader stage={stage} done={done} total={total} success={success} />
      <GenerationSteps stage={stage} done={done} total={total} success={success} />
    </div>
  )
}

function ChatBubble({ message }: { message: WorkChatMessage }) {
  if (message.role === "user") {
    return (
      <div className="flex flex-col items-end gap-1">
        <div className="animate-fade-up max-w-[86%] rounded-[14px] rounded-br-[4px] bg-primary px-3.5 py-2 text-[13px] leading-relaxed whitespace-pre-wrap break-words text-primary-foreground">
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
      <div className="animate-fade-up self-center rounded-full border border-border bg-white/60 px-3 py-1 text-[11px] text-muted-foreground">
        {message.content}
      </div>
    )
  }
  return (
    <div className="flex flex-col items-start gap-1.5">
      <div
        className={cn(
          "animate-fade-up max-w-[86%] rounded-[14px] rounded-bl-[4px] border border-border bg-white/70 px-3.5 py-2 text-[13px] leading-relaxed whitespace-pre-wrap break-words",
          message.status === "failed" && "text-destructive",
        )}
      >
        {message.status === "pending" ? (
          <span className="flex items-center gap-2 text-muted-foreground">
            <LoaderCircleIcon className="size-3.5 animate-spin" />
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
  const inputId = useId()
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
    <div className="flex h-full min-h-0 flex-col">
      <div className="border-b border-border/70 px-4 pt-4 pb-3">
        <div className="flex items-center gap-2">
          <span className="flex size-6.5 items-center justify-center rounded-lg bg-primary text-[13px] text-primary-foreground">
            ✦
          </span>
          <h2 className="text-sm font-bold">AI 修改助手</h2>
        </div>
        {slide && (
          <div className="mt-2.5 inline-flex max-w-full items-center gap-1.5 rounded-full border border-border bg-white/55 px-3 py-1 text-xs font-medium text-muted-foreground">
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
              正在编辑：第 {slide.index} 页 · {slide.title}
            </span>
          </div>
        )}
      </div>

      <div
        ref={scrollRef}
        role="log"
        aria-live="polite"
        aria-label="当前页面的修改记录"
        className="min-h-0 flex-1 overflow-y-auto px-4 py-4"
      >
        {messages.length === 0 ? (
          <div className="flex h-full flex-col items-center justify-center px-2 text-center">
            <span className="mb-3 flex size-11 items-center justify-center rounded-2xl bg-primary/8 text-xl">
              ✦
            </span>
            <div className="font-serif text-sm font-extrabold">和我聊聊这一页</div>
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
                  className="rounded-full border border-border bg-white/50 px-3 py-2 text-left text-xs text-muted-foreground transition-colors hover:bg-white/80 hover:text-foreground"
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

      <div className="border-t border-border/70 px-4 pt-3 pb-4">
        <div className="mb-2.5 flex flex-wrap gap-1.5">
          {QUICK_CHIPS.map((chip) => (
            <button
              key={chip}
              type="button"
              className="rounded-full border border-border bg-white/50 px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:bg-white/80 hover:text-foreground disabled:opacity-50"
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
          <Label htmlFor={inputId} className="sr-only">
            当前页面的修改指令
          </Label>
          <Textarea
            id={inputId}
            value={draft}
            rows={1}
            className="max-h-28 min-h-10 flex-1 resize-none rounded-2xl border-border bg-white/65 px-3.5 text-[13px]"
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
            className="rounded-full"
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
      <SheetTrigger
        render={
          <Button
            variant="ghost"
            size="sm"
            className="h-9 rounded-full px-3 text-[13px] text-muted-foreground"
          />
        }
      >
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
                <span className="shrink-0 font-semibold text-foreground/70">{entry.type}</span>
                <span className="min-w-0 break-all text-foreground/80">{entry.message}</span>
              </li>
            ))}
            {logs.length === 0 && <li className="text-hint">暂无事件</li>}
          </ol>
        </div>
        <div className="flex items-center justify-between border-t px-4 py-2.5">
          <span className="text-[11px] text-hint">{logs.length} 条事件</span>
          <Button
            variant="outline"
            size="xs"
            className="rounded-full"
            onClick={() => setAutoScroll((value) => !value)}
          >
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
  usePageMetadata(task?.topic ? `工作台：${task.topic}` : "演示工作台")
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
  const [slidesOpen, setSlidesOpen] = useState(false)
  const [chatOpen, setChatOpen] = useState(false)
  const desktopChat = useMediaQuery("(min-width: 80rem)")
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
      const target = event.target instanceof HTMLElement ? event.target : null
      if (
        event.defaultPrevented ||
        event.altKey ||
        event.ctrlKey ||
        event.metaKey ||
        event.shiftKey ||
        document.querySelector("[role=dialog], [role=menu], [role=listbox]") ||
        target?.isContentEditable ||
        target?.closest(
          "input, textarea, select, button, a, [role=menu], [role=menuitem], [role=dialog], [role=listbox], [role=option], [contenteditable=true]",
        )
      ) {
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
      <main
        id="main-content"
        tabIndex={-1}
        className="relative flex h-svh items-center justify-center text-sm text-muted-foreground outline-none"
      >
        <SceneBackground />
        <span className="relative z-10 flex items-center">
          <LoaderCircleIcon className="mr-2 size-4 animate-spin" />
          正在载入任务…
        </span>
      </main>
    )
  }

  if (loadError || !task) {
    return (
      <main
        id="main-content"
        tabIndex={-1}
        className="relative flex h-svh flex-col items-center justify-center gap-3 outline-none"
      >
        <SceneBackground />
        <div className="glass-card relative z-10 flex flex-col items-center gap-3 rounded-[24px] px-10 py-8">
          <TriangleAlertIcon className="size-8 text-hint" />
          <div role="alert" className="text-sm font-medium">{loadError ?? "任务不存在"}</div>
          <div className="flex gap-2">
            {taskId && (
              <Button
                size="sm"
                className="rounded-full"
                onClick={() => void hydrate(taskId, requestedPages)}
              >
                重新加载
              </Button>
            )}
            <Button variant="outline" size="sm" className="rounded-full" render={<Link to="/" />}>
              返回新建任务
            </Button>
          </div>
        </div>
      </main>
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
  const totalSlides = slides.length || task.total_slides
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
    <div className="relative h-svh overflow-hidden">
      <SceneBackground />
      <div className="relative z-10 flex h-full flex-col gap-3 p-3 sm:p-3.5">
        {/* ══ top bar ══ */}
        <div className="relative z-30 flex-none">
          <liquid-glass blur-amount="10" className="glass-panel rounded-[18px]">
            <header className="flex items-center gap-2.5 overflow-x-auto px-3.5 py-2.5 sm:gap-4 sm:px-4.5">
            <Link
              to="/"
              className="flex shrink-0 items-center gap-2 text-foreground"
              title="返回创建"
            >
              <BrandMark className="size-5" />
              <ArrowLeftIcon className="size-[18px] text-hint" />
            </Link>
            <span className="hidden h-7 w-px shrink-0 bg-border sm:block" />
            <div className="hidden min-w-0 md:block">
              <div className="max-w-80 truncate text-sm font-bold tracking-tight">
                {task.topic}
              </div>
              <div className="mt-0.5 flex items-center gap-1.5 text-xs text-hint">
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
                  {template?.name ?? "模板"} · {task.ratio} · {totalSlides} 页
                </span>
              </div>
            </div>

            {/* center progress: running / completed / failed / cancelled */}
            <div className="hidden min-w-0 flex-1 items-center justify-center gap-3.5 lg:flex">
              {running && (
                <span
                  role="status"
                  className="flex items-center gap-2 rounded-full border border-border bg-accent px-4 py-2 text-[13px] font-semibold whitespace-nowrap"
                >
                  <LoaderCircleIcon className="size-4 animate-spin" />
                  {STAGE_TEXT[task.stage] ?? "生成中…"}
                  <span className="font-heading font-semibold tabular-nums text-muted-foreground">
                    {doneCount}/{totalSlides} · {formatClock(elapsedSeconds)}
                  </span>
                </span>
              )}
              {task.status === "completed" &&
                (activeCount > 0 ? (
                  <span
                    role="status"
                    className="flex items-center gap-2 rounded-full border border-border bg-accent px-4 py-2 text-[13px] font-medium text-accent-foreground"
                  >
                    <LoaderCircleIcon className="size-4 animate-spin" />
                    正在更新 {activeCount} 页…
                  </span>
                ) : failedCount === 0 ? (
                  <span
                    role="status"
                    className="animate-light-up flex items-center gap-2 rounded-full bg-success-subtle px-4 py-2 text-[13px] font-bold text-success"
                  >
                    <CheckIcon className="size-4" strokeWidth={3} />
                    生成完成 · 共 {slides.length} 页 · 用时 {formatClock(elapsedSeconds)}
                  </span>
                ) : (
                  <span
                    role="status"
                    className="animate-light-up flex items-center gap-2 rounded-full bg-destructive/10 px-4 py-2 text-[13px] font-bold text-destructive"
                  >
                    <TriangleAlertIcon className="size-4" />
                    生成结束 · {failedCount} 页失败，可在左侧重试
                  </span>
                ))}
              {task.status === "failed" && (
                <span
                  role="status"
                  className="animate-light-up flex items-center gap-2 rounded-full bg-destructive/10 px-4 py-2 text-[13px] font-bold text-destructive"
                >
                  <TriangleAlertIcon className="size-4" />
                  任务失败 · 已完成 {doneCount}/{totalSlides} 页
                </span>
              )}
              {cancelled && (
                <span
                  role="status"
                  className="animate-light-up rounded-full border border-border bg-white/60 px-4 py-2 text-[13px] font-medium text-hint"
                >
                  已暂停 · 完成 {doneCount}/{slides.length} 页
                </span>
              )}
              {!followLatest && running && (
                <Button
                  variant="outline"
                  size="xs"
                  className="rounded-full bg-white/60"
                  onClick={resumeFollow}
                >
                  <ListStartIcon data-icon="inline-start" />
                  回到最新
                </Button>
              )}
            </div>

            <div className="ml-auto flex shrink-0 items-center gap-2">
              <div className="hidden sm:block">
                <RunLogSheet />
              </div>
              {(running || isBooting) && (
                <Button
                  variant="outline"
                  size="sm"
                  className="h-9 rounded-full bg-white/50 px-3.5 text-[13px] text-muted-foreground hover:border-destructive/50 hover:bg-white hover:text-destructive"
                  onClick={() => setCancelOpen(true)}
                  aria-label="取消生成"
                >
                  <PauseIcon data-icon="inline-start" />
                  <span className="hidden sm:inline">取消生成</span>
                </Button>
              )}
              {cancelled && (
                <Button
                  variant="outline"
                  size="sm"
                  className="h-9 rounded-full border-primary/40 bg-white/60 px-3.5 text-[13px] hover:bg-white"
                  onClick={() => void resumeTask()}
                  aria-label="继续生成"
                >
                  <PlayIcon data-icon="inline-start" />
                  <span className="hidden sm:inline">继续生成</span>
                </Button>
              )}
              {task.status === "failed" && (
                <Button
                  variant="outline"
                  size="sm"
                  className="h-9 rounded-full border-primary/40 bg-white/60 px-3.5 text-[13px] hover:bg-white"
                  onClick={() => void resumeTask()}
                  aria-label="重试生成"
                >
                  <RotateCcwIcon data-icon="inline-start" />
                  <span className="hidden sm:inline">重试生成</span>
                </Button>
              )}
              <DropdownMenu>
                <DropdownMenuTrigger
                  render={
                    <Button
                      size="sm"
                      className="h-9 gap-2 rounded-full px-4.5 text-[13px] shadow-[0_8px_20px_rgba(27,28,32,0.25)]"
                      aria-label={exporting ? "导出中" : "导出"}
                      disabled={exportDisabled}
                    />
                  }
                >
                  {exporting ? (
                    <LoaderCircleIcon className="animate-spin" />
                  ) : (
                    <DownloadIcon />
                  )}
                  <span className="hidden leading-none sm:inline">
                    {exporting ? "导出中" : "导出"}
                  </span>
                  <ChevronDownIcon className="size-3 opacity-70" />
                </DropdownMenuTrigger>
                <DropdownMenuContent align="end" className="w-64">
                  {/* Base UI GroupLabel must live inside a Group, or it throws. */}
                  <DropdownMenuGroup>
                    <DropdownMenuLabel>导出演示文稿</DropdownMenuLabel>
                    <DropdownMenuSeparator />
                    <DropdownMenuItem onClick={() => void exportTask("pptx")}>
                      <span className="flex size-8 shrink-0 items-center justify-center rounded-[10px] bg-foreground/[0.06]">
                        <DownloadIcon className="size-4" />
                      </span>
                      <div className="min-w-0">
                        <div className="font-heading text-[13px] font-semibold">PPTX</div>
                        <div className="mt-0.5 text-[10px] text-hint">
                          可在 PowerPoint 中继续编辑
                        </div>
                      </div>
                    </DropdownMenuItem>
                    <DropdownMenuItem onClick={() => void exportTask("pdf")}>
                      <span className="flex size-8 shrink-0 items-center justify-center rounded-[10px] bg-foreground/[0.06]">
                        <FileClockIcon className="size-4" />
                      </span>
                      <div className="min-w-0">
                        <div className="font-heading text-[13px] font-semibold">PDF</div>
                        <div className="mt-0.5 text-[10px] text-hint">
                          适合直接分享与打印
                        </div>
                      </div>
                    </DropdownMenuItem>
                  </DropdownMenuGroup>
                </DropdownMenuContent>
              </DropdownMenu>
            </div>
            </header>
          </liquid-glass>
          <div className="pointer-events-none absolute top-[calc(100%+0.5rem)] left-1/2 z-20 w-[min(34rem,calc(100%-2rem))] -translate-x-1/2">
            <div
              role="status"
              data-visible={connection === "reconnecting"}
              aria-hidden={connection !== "reconnecting"}
              className="reconnect-status flex h-9 items-center justify-center gap-2 rounded-2xl border border-amber-300/60 bg-amber-50/85 text-xs text-amber-800 shadow-[0_10px_28px_rgba(120,83,12,0.12)] backdrop-blur-md"
            >
              <WifiOffIcon className="size-3.5" />
              连接中断，正在重连…任务仍在后台继续
            </div>
          </div>
        </div>

        <div className="glass-card flex h-10 flex-none items-center gap-2 rounded-2xl px-2 xl:hidden">
          <Button
            variant="outline"
            size="xs"
            className="rounded-full bg-white/60 lg:hidden"
            onClick={() => setSlidesOpen(true)}
          >
            <PanelLeftIcon />
            幻灯片
          </Button>
          <span className="min-w-0 flex-1 truncate text-xs text-muted-foreground">
            {selected ? `第 ${selected.index} 页 · ${selected.title}` : task.topic}
          </span>
          <Button
            variant="outline"
            size="xs"
            className="rounded-full bg-white/60"
            onClick={() => setChatOpen(true)}
          >
            <MessageSquareIcon />
            修改助手
          </Button>
        </div>

        {/* ══ three columns ══ */}
        <main id="main-content" tabIndex={-1} className="flex min-h-0 flex-1 gap-3 outline-none">
          <liquid-glass
            blur-amount="9"
            className="glass-panel hidden w-[188px] flex-none rounded-[20px] lg:block"
          >
            <SlidesPanel
              slides={slides}
              ratio={task.ratio}
              paused={cancelled}
              selectedId={selected?.id ?? null}
              onSelect={selectSlide}
            />
          </liquid-glass>

          <section className="flex min-w-0 flex-1 flex-col items-center justify-center overflow-auto px-1 py-2 select-none sm:px-4">
            {isBooting ? (
              <div className="w-full" style={{ maxWidth: canvasMaxWidth }}>
                <div className="overflow-hidden rounded-2xl shadow-[0_20px_36px_-26px_rgba(30,32,44,0.3),0_2px_8px_rgba(30,32,44,0.08)]">
                  <BootCanvas stage={task.stage} ratio={task.ratio} />
                </div>
              </div>
            ) : (
              selected && (
                <div className="w-full" style={{ maxWidth: canvasMaxWidth }}>
                  <div className="overflow-hidden rounded-2xl shadow-[0_20px_36px_-26px_rgba(30,32,44,0.3),0_2px_8px_rgba(30,32,44,0.08)]">
                    <SlideCanvas
                      slide={selected}
                      ratio={task.ratio}
                      paused={cancelled}
                      onRetry={() => void retrySlide(selected.id)}
                    />
                  </div>
                  <liquid-glass
                    blur-amount="10"
                    className="glass-panel mx-auto mt-3.5 w-fit max-w-full rounded-full"
                  >
                    <div className="flex items-center gap-1 overflow-x-auto px-2 py-1.5">
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
                              variant="ghost"
                              size="sm"
                              className="h-9 gap-1.5 rounded-full px-3.5 text-[13px] leading-none whitespace-nowrap"
                              disabled={selected.status !== "completed"}
                            />
                          }
                        >
                          <HistoryIcon />
                          版本 {selected.revision > 0 ? `v${selected.revision}` : "—"}
                          <ChevronDownIcon />
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
                                className={cn(
                                  revision.revision === selected.revision &&
                                    "bg-foreground/[0.065] data-disabled:opacity-100",
                                )}
                                onClick={() =>
                                  void applyRevision(selected.id, revision.revision)
                                }
                              >
                                <span
                                  className={cn(
                                    "font-heading text-xs font-bold",
                                    revision.revision === selected.revision
                                      ? "text-foreground"
                                      : "text-hint",
                                  )}
                                >
                                  v{revision.revision}
                                </span>
                                <span className="flex-1">{revision.label}</span>
                                {revision.revision === selected.revision && (
                                  <span className="rounded-full bg-foreground px-2 py-0.5 text-[10px] font-bold text-background">
                                    当前
                                  </span>
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
                        variant="ghost"
                        size="sm"
                        className="h-9 gap-1.5 rounded-full px-3.5 text-[13px] leading-none whitespace-nowrap"
                        disabled={undoDisabled}
                        onClick={() => void undo(selected.id)}
                      >
                        <Undo2Icon />
                        撤销
                      </Button>
                      <span className="mx-1 h-5 w-px bg-border" />
                      <span className="font-heading relative top-[1px] flex h-9 items-center px-2 text-[13px] leading-none whitespace-nowrap tabular-nums text-hint">
                        第 {selected.index} / {slides.length} 页
                      </span>
                      <span className="mx-1 h-5 w-px bg-border" />
                      <Button
                        size="sm"
                        className="h-9 gap-1.5 rounded-full px-4 text-[13px] leading-none whitespace-nowrap"
                        disabled={selected.status !== "completed"}
                        onClick={() => void retrySlide(selected.id)}
                      >
                        <RotateCcwIcon />
                        重新生成本页
                      </Button>
                    </div>
                  </liquid-glass>
                  {selected.status === "generating" && (
                    <div className="mt-3 flex items-center justify-center gap-2 text-xs text-hint">
                      <SparklesIcon className="animate-soft-pulse size-3.5" />
                      页面生成中，完成后会自动更新预览
                    </div>
                  )}
                </div>
              )
            )}
          </section>

          <liquid-glass
            blur-amount="9"
            className="glass-panel hidden w-[320px] flex-none overflow-hidden rounded-[20px] xl:block"
          >
            <div className="flex h-full flex-col">
              <GenerationPanel
                status={task.status}
                stage={task.stage}
                done={doneCount}
                total={totalSlides}
              />
              {desktopChat && <ChatPanel slide={selected} />}
            </div>
          </liquid-glass>
        </main>
      </div>

      <Sheet open={slidesOpen} onOpenChange={setSlidesOpen}>
        <SheetContent side="left" className="w-[min(86vw,300px)] gap-0 p-0 lg:hidden">
          <SheetHeader className="border-b">
            <SheetTitle>幻灯片</SheetTitle>
            <SheetDescription>选择要查看或修改的页面</SheetDescription>
          </SheetHeader>
          <div className="min-h-0 flex-1">
            <SlidesPanel
              slides={slides}
              ratio={task.ratio}
              paused={cancelled}
              selectedId={selected?.id ?? null}
              onSelect={(slideId) => {
                selectSlide(slideId)
                setSlidesOpen(false)
              }}
            />
          </div>
        </SheetContent>
      </Sheet>

      <Sheet open={chatOpen} onOpenChange={setChatOpen}>
        <SheetContent side="right" className="w-[min(94vw,390px)] gap-0 p-0 xl:hidden">
          <SheetHeader className="sr-only">
            <SheetTitle>AI 修改助手</SheetTitle>
            <SheetDescription>针对当前页面发送修改指令</SheetDescription>
          </SheetHeader>
          <div className="min-h-0 flex-1 pt-10">
            {!desktopChat && <ChatPanel slide={selected} />}
          </div>
        </SheetContent>
      </Sheet>

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
