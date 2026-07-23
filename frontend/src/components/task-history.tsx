import { useCallback, useEffect, useState } from "react"
import { Link } from "react-router-dom"
import {
  AlertCircleIcon,
  ArrowUpRightIcon,
  CheckCircle2Icon,
  Clock3Icon,
  LoaderCircleIcon,
  PauseCircleIcon,
  PresentationIcon,
  RefreshCwIcon,
} from "lucide-react"

import { Button } from "@/components/ui/button"
import { api } from "@/lib/api"
import { resolveApiUrl } from "@/lib/api-url"
import { cn } from "@/lib/utils"
import type {
  TaskHistoryItem,
  TaskHistoryResponse,
  TaskStage,
  TaskStatus,
} from "@/types/api"

const relativeTime = new Intl.RelativeTimeFormat("zh-CN", { numeric: "auto" })
const absoluteDate = new Intl.DateTimeFormat("zh-CN", {
  month: "numeric",
  day: "numeric",
})

const stageLabels: Partial<Record<TaskStage, string>> = {
  prepare: "准备资料",
  plan: "整理内容",
  research: "整理内容",
  generate: "生成页面",
  edit: "修改页面",
  export: "整理导出",
}

const statusClasses: Record<TaskStatus, string> = {
  queued: "border-white/70 bg-white/45 text-muted-foreground",
  running: "border-white/75 bg-white/60 text-foreground",
  completed: "border-success/15 bg-success-subtle/80 text-success",
  failed: "border-destructive/15 bg-destructive/[0.08] text-destructive",
  cancelled: "border-foreground/10 bg-foreground/[0.06] text-muted-foreground",
}

function formatUpdatedAt(value: string, now = Date.now()): string {
  const timestamp = Date.parse(value)
  if (!Number.isFinite(timestamp)) {
    return "时间未知"
  }

  const difference = timestamp - now
  const absoluteDifference = Math.abs(difference)
  if (absoluteDifference < 60_000) {
    return "刚刚"
  }
  if (absoluteDifference < 60 * 60_000) {
    return relativeTime.format(Math.round(difference / 60_000), "minute")
  }
  if (absoluteDifference < 24 * 60 * 60_000) {
    return relativeTime.format(Math.round(difference / (60 * 60_000)), "hour")
  }
  if (absoluteDifference < 7 * 24 * 60 * 60_000) {
    return relativeTime.format(
      Math.round(difference / (24 * 60 * 60_000)),
      "day",
    )
  }
  return absoluteDate.format(timestamp)
}

function statusLabel(task: TaskHistoryItem): string {
  if (task.status === "running") {
    return `生成中 ${Math.round(task.progress)}%`
  }
  const labels: Record<Exclude<TaskStatus, "running">, string> = {
    queued: "等待中",
    completed: "已完成",
    failed: "需处理",
    cancelled: "已暂停",
  }
  return labels[task.status]
}

function StatusIcon({ status }: { status: TaskStatus }) {
  if (status === "running" || status === "queued") {
    return <LoaderCircleIcon className="size-3.5 animate-spin" />
  }
  if (status === "completed") {
    return <CheckCircle2Icon className="size-3.5" />
  }
  if (status === "cancelled") {
    return <PauseCircleIcon className="size-3.5" />
  }
  return <AlertCircleIcon className="size-3.5" />
}

function slideSummary(task: TaskHistoryItem): string {
  if (task.total_slides <= 0) {
    return "页数规划中"
  }
  if (task.status === "running" || task.status === "queued") {
    return `${task.completed_slides} / ${task.total_slides} 页`
  }
  if (task.failed_slides > 0) {
    return `${task.total_slides} 页 · ${task.failed_slides} 页需处理`
  }
  return `${task.total_slides} 页`
}

function HistoryPreview({ task }: { task: TaskHistoryItem }) {
  const previewUrl = task.preview_url ? resolveApiUrl(task.preview_url) : null
  return (
    <span className="relative flex aspect-video w-[104px] shrink-0 items-center justify-center overflow-hidden rounded-[11px] border border-white/70 bg-white/35 shadow-[0_6px_18px_rgba(30,32,44,0.08)]">
      <span className="flex size-full items-center justify-center bg-[linear-gradient(135deg,rgba(255,255,255,0.72),rgba(255,255,255,0.22))] text-hint">
        <PresentationIcon className="size-5" />
      </span>
      {previewUrl && (
        <img
          src={previewUrl}
          alt=""
          loading="lazy"
          decoding="async"
          className="absolute inset-0 size-full bg-white object-contain"
          onError={(event) => {
            event.currentTarget.hidden = true
          }}
        />
      )}
      {(task.status === "running" || task.status === "queued") && (
        <span className="absolute right-1.5 bottom-1.5 flex size-5 items-center justify-center rounded-full border border-white/75 bg-white/75 text-foreground shadow-sm backdrop-blur-md">
          <LoaderCircleIcon className="size-3 animate-spin" />
        </span>
      )}
    </span>
  )
}

function HistoryRow({ task }: { task: TaskHistoryItem }) {
  const isActive = task.status === "running" || task.status === "queued"
  const stageLabel = task.stage ? stageLabels[task.stage] : null
  const progress = Math.max(4, Math.min(100, task.progress))

  return (
    <Link
      to={`/workbench/${encodeURIComponent(task.task_id)}`}
      aria-label={`继续对话：${task.topic}`}
      className="group flex min-h-[84px] items-center gap-4 rounded-[18px] px-3.5 py-3 outline-none transition-colors hover:bg-white/45 focus-visible:bg-white/55 focus-visible:ring-2 focus-visible:ring-ring/35"
    >
      <HistoryPreview task={task} />

      <span className="min-w-0 flex-1">
        <span className="block truncate text-[14.5px] font-semibold" title={task.topic}>
          {task.topic}
        </span>
        <span className="mt-1.5 flex flex-wrap items-center gap-x-2 gap-y-1 text-[12px] text-hint">
          <span className="inline-flex items-center gap-1">
            <Clock3Icon className="size-3" />
            {formatUpdatedAt(task.updated_at)}
          </span>
          <span aria-hidden="true">·</span>
          <span className="font-heading tabular-nums">{slideSummary(task)}</span>
          <span aria-hidden="true">·</span>
          <span className="font-heading tabular-nums">{task.ratio}</span>
          {isActive && stageLabel && (
            <>
              <span aria-hidden="true">·</span>
              <span>{stageLabel}</span>
            </>
          )}
        </span>
        {isActive && (
          <span className="mt-2 block h-1 max-w-[320px] overflow-hidden rounded-full bg-foreground/[0.07]">
            <span
              className="block h-full rounded-full bg-foreground/70 transition-[width] duration-500"
              style={{ width: `${progress}%` }}
            />
          </span>
        )}
      </span>

      <span className="ml-2 flex shrink-0 items-center gap-2">
        <span
          className={cn(
            "inline-flex items-center gap-1.5 rounded-full border px-2.5 py-1.5 text-[11.5px] font-medium",
            statusClasses[task.status],
          )}
        >
          <StatusIcon status={task.status} />
          {statusLabel(task)}
        </span>
        <ArrowUpRightIcon className="size-4 text-hint transition-transform duration-200 group-hover:translate-x-0.5 group-hover:-translate-y-0.5 group-hover:text-foreground" />
      </span>
    </Link>
  )
}

function HistorySkeleton() {
  return (
    <div aria-hidden="true" className="space-y-1 p-2">
      {[0, 1, 2].map((item) => (
        <div key={item} className="flex min-h-[84px] items-center gap-4 px-3.5 py-3">
          <span className="skeleton-shimmer aspect-video w-[104px] shrink-0 rounded-[11px]" />
          <span className="min-w-0 flex-1">
            <span className="skeleton-shimmer block h-3.5 w-2/5 rounded-full" />
            <span className="skeleton-shimmer mt-2.5 block h-2.5 w-3/5 rounded-full" />
          </span>
          <span className="skeleton-shimmer h-7 w-[76px] rounded-full" />
        </div>
      ))}
    </div>
  )
}

export function TaskHistory() {
  const [history, setHistory] = useState<TaskHistoryResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [refreshing, setRefreshing] = useState(false)

  const loadHistory = useCallback(async (): Promise<void> => {
    setRefreshing(true)
    try {
      const result = await api.listTasks(6)
      setHistory(result)
      setError(null)
    } catch {
      setError("暂时无法读取历史对话")
    } finally {
      setRefreshing(false)
    }
  }, [])

  useEffect(() => {
    void loadHistory()
  }, [loadHistory])

  const tasks = history?.tasks ?? []
  const isInitialLoading = history === null && error === null

  return (
    <section
      aria-labelledby="task-history-title"
      className="mt-14 w-full animate-fade-up"
      style={{ animationDelay: "80ms" }}
    >
      <div className="mb-3 flex items-end justify-between gap-4 px-1">
        <div>
          <h2
            id="task-history-title"
            className="font-serif text-[25px] font-bold tracking-tight"
          >
            最近对话
          </h2>
          <p className="mt-1 text-[12.5px] text-hint">
            {history && history.total > 0
              ? `共 ${history.total} 个演示记录，从上次离开的地方继续`
              : "生成过的演示会保存在这里"}
          </p>
        </div>
        <Button
          type="button"
          variant="glass"
          size="sm"
          disabled={refreshing}
          aria-label="刷新最近对话"
          onClick={() => void loadHistory()}
        >
          <RefreshCwIcon className={cn(refreshing && "animate-spin")} />
          刷新
        </Button>
      </div>

      <liquid-glass
        blur-amount="8"
        className="glass-panel overflow-hidden rounded-[24px] shadow-[0_18px_48px_rgba(30,32,44,0.12),inset_0_1px_1px_rgba(255,255,255,0.76)]"
      >
        {isInitialLoading ? (
          <div aria-busy="true" aria-label="正在加载最近对话">
            <HistorySkeleton />
          </div>
        ) : error && history === null ? (
          <div
            role="alert"
            className="flex min-h-36 flex-col items-center justify-center px-6 text-center"
          >
            <span className="flex size-9 items-center justify-center rounded-full bg-destructive/[0.08] text-destructive">
              <AlertCircleIcon className="size-4" />
            </span>
            <p className="mt-3 text-[13.5px] font-medium">{error}</p>
            <Button
              type="button"
              variant="glass"
              size="sm"
              className="mt-3"
              onClick={() => void loadHistory()}
            >
              重新加载
            </Button>
          </div>
        ) : tasks.length === 0 ? (
          <div className="flex min-h-36 items-center justify-center gap-3 px-6">
            <span className="flex size-10 items-center justify-center rounded-full border border-white/70 bg-white/45 text-hint shadow-sm">
              <PresentationIcon className="size-4.5" />
            </span>
            <span>
              <span className="block text-[13.5px] font-semibold">还没有历史对话</span>
              <span className="mt-0.5 block text-[12px] text-hint">
                完成第一次生成后，可从这里随时继续
              </span>
            </span>
          </div>
        ) : (
          <div className="space-y-1 p-2">
            {tasks.map((task) => (
              <HistoryRow key={task.task_id} task={task} />
            ))}
          </div>
        )}
      </liquid-glass>

      {error && history !== null && (
        <p role="status" className="mt-2 px-1 text-[11.5px] text-destructive">
          {error}，当前显示上一次结果。
        </p>
      )}
    </section>
  )
}
