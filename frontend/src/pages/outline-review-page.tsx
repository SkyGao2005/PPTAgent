import { useCallback, useEffect, useState } from "react"
import ReactMarkdown from "react-markdown"
import { useNavigate, useParams } from "react-router-dom"
import remarkGfm from "remark-gfm"
import {
  ArrowLeftIcon,
  ArrowRightIcon,
  CheckCircle2Icon,
  CircleAlertIcon,
  FileCheck2Icon,
  LoaderCircleIcon,
  MessageSquareTextIcon,
  RefreshCwIcon,
  ShieldCheckIcon,
  SparklesIcon,
} from "lucide-react"
import { toast } from "sonner"

import { AppShell } from "@/components/app-shell"
import { Button } from "@/components/ui/button"
import { Textarea } from "@/components/ui/textarea"
import { api } from "@/lib/api"
import { resolveApiUrl } from "@/lib/api-url"
import { usePageMetadata } from "@/lib/use-page-metadata"
import { cn } from "@/lib/utils"
import type { OutlineDraft } from "@/types/api"

type PendingAction = "regenerate" | "approve" | null

function revisionLabel(revision: number): string {
  return revision > 0 ? `第 ${revision} 版` : "初稿生成中"
}

function OutlineSkeleton({ count }: { count: number }) {
  return (
    <div className="space-y-6 px-2 py-3 sm:px-5" aria-label="正在生成内容文稿">
      <span className="skeleton-shimmer block h-8 w-2/3 rounded-full" />
      <span className="skeleton-shimmer block h-4 w-full rounded-full opacity-75" />
      <span className="skeleton-shimmer block h-4 w-5/6 rounded-full opacity-65" />
      {Array.from({ length: Math.min(Math.max(count, 3), 6) }, (_, index) => (
        <div key={index} className="space-y-3 border-t border-border/60 pt-6">
          <span className="skeleton-shimmer block h-5 w-2/5 rounded-full" />
          <span className="skeleton-shimmer block h-3.5 w-full rounded-full opacity-70" />
          <span className="skeleton-shimmer block h-3.5 w-4/5 rounded-full opacity-55" />
        </div>
      ))}
    </div>
  )
}

function StatusPill({ outline }: { outline: OutlineDraft }) {
  if (outline.status === "generating") {
    return (
      <span className="glass-chip inline-flex self-start items-center gap-2 rounded-full px-3.5 py-2 text-xs font-semibold text-muted-foreground sm:self-auto">
        <LoaderCircleIcon className="size-3.5 animate-spin" />
        Research 正在生成 {revisionLabel(outline.revision + 1)}
      </span>
    )
  }
  if (outline.status === "failed") {
    return (
      <span className="inline-flex self-start items-center gap-2 rounded-full bg-destructive/10 px-3.5 py-2 text-xs font-semibold text-destructive sm:self-auto">
        <CircleAlertIcon className="size-3.5" />
        生成失败
      </span>
    )
  }
  if (outline.status === "approved") {
    return (
      <span className="inline-flex self-start items-center gap-2 rounded-full bg-emerald-600/10 px-3.5 py-2 text-xs font-semibold text-emerald-800 sm:self-auto">
        <CheckCircle2Icon className="size-3.5" />
        已确认
      </span>
    )
  }
  return (
    <span className="glass-chip inline-flex self-start items-center gap-2 rounded-full px-3.5 py-2 text-xs font-semibold text-muted-foreground sm:self-auto">
      <FileCheck2Icon className="size-3.5 text-primary" />
      {revisionLabel(outline.revision)}文稿 · 待确认
    </span>
  )
}

export function OutlineReviewPage() {
  usePageMetadata("审查内容文稿")
  const navigate = useNavigate()
  const { outlineId = "" } = useParams()
  const [outline, setOutline] = useState<OutlineDraft | null>(null)
  const [comment, setComment] = useState("")
  const [loadError, setLoadError] = useState<string | null>(null)
  const [pending, setPending] = useState<PendingAction>(null)
  const [pollVersion, setPollVersion] = useState(0)

  const reload = useCallback(async (): Promise<OutlineDraft> => {
    const next = await api.getOutline(outlineId)
    setOutline(next)
    setLoadError(null)
    return next
  }, [outlineId])

  useEffect(() => {
    let active = true
    let timer: ReturnType<typeof setTimeout> | undefined

    async function poll(): Promise<void> {
      try {
        const next = await api.getOutline(outlineId)
        if (!active) return
        setOutline(next)
        setLoadError(null)
        if (next.status === "generating") {
          timer = setTimeout(() => void poll(), 900)
        }
      } catch {
        if (!active) return
        setLoadError("无法载入这份内容文稿，请返回主页重新开始。")
      }
    }

    if (outlineId) {
      void poll()
    } else {
      setLoadError("缺少内容文稿编号，请返回主页重新开始。")
    }
    return () => {
      active = false
      if (timer) clearTimeout(timer)
    }
  }, [outlineId, pollVersion])

  async function handleRegenerate(): Promise<void> {
    if (!outline || outline.status === "generating" || pending) return
    setPending("regenerate")
    try {
      const next = await api.regenerateOutline(outline.outline_id, comment)
      setOutline(next)
      setComment("")
      setPollVersion((value) => value + 1)
      toast.success("已提交内容修改", {
        description: "修改意见已追加到 Research 上下文，默认不重新调研。",
      })
    } catch {
      toast.error("内容文稿重新生成失败，请重试")
      await reload().catch(() => undefined)
    } finally {
      setPending(null)
    }
  }

  async function handleApprove(): Promise<void> {
    if (!outline || outline.status !== "ready" || pending) return
    setPending("approve")
    try {
      const approval = await api.approveOutline(outline.outline_id)
      navigate(`/workbench/${approval.task_id}`, {
        state: { requestedPages: outline.page_count },
      })
    } catch {
      toast.error("确认内容文稿失败，请重试")
      setPending(null)
    }
  }

  if (loadError && !outline) {
    return (
      <AppShell>
        <main
          id="main-content"
          tabIndex={-1}
          className="mx-auto flex min-h-[60svh] w-full max-w-xl items-center px-4 pb-20 outline-none"
        >
          <div className="glass-card w-full rounded-[26px] p-8 text-center">
            <CircleAlertIcon className="mx-auto size-8 text-destructive" />
            <h1 className="mt-4 font-serif text-2xl font-black">内容文稿未找到</h1>
            <p className="mt-2 text-sm leading-6 text-muted-foreground">{loadError}</p>
            <Button className="mt-6 rounded-full px-5" onClick={() => navigate("/")}>
              返回主页
            </Button>
          </div>
        </main>
      </AppShell>
    )
  }

  return (
    <AppShell>
      <main
        id="main-content"
        tabIndex={-1}
        className="mx-auto w-full max-w-[1180px] px-4 pt-4 pb-20 outline-none sm:px-6 sm:pt-7"
      >
        <div className="animate-fade-up">
          <button
            type="button"
            className="glass-chip inline-flex items-center gap-2 rounded-full px-3.5 py-2 text-[13px] font-medium text-muted-foreground hover:text-foreground"
            onClick={() => navigate("/")}
          >
            <ArrowLeftIcon className="size-3.5" />
            返回修改需求
          </button>

          <div className="mt-6 flex flex-col gap-4 sm:flex-row sm:items-end sm:justify-between">
            <div className="min-w-0">
              <div className="mb-2 flex items-center gap-2 text-[12px] font-semibold tracking-[0.13em] text-hint uppercase">
                <span className="text-muted-foreground">01 输入需求</span>
                <span aria-hidden>·</span>
                <span className="text-primary">02 审查内容</span>
                <span aria-hidden>·</span>
                <span>03 生成演示</span>
              </div>
              <h1 className="font-serif text-[34px] leading-tight font-black tracking-tight sm:text-[42px]">
                先审一遍内容
              </h1>
              <p className="mt-2 max-w-2xl text-[14px] leading-6 text-muted-foreground">
                这里展示 Research 产出的完整 Markdown 文稿。确认后直接进入逐页设计，不会再次研究或改写。
              </p>
            </div>
            {outline ? <StatusPill outline={outline} /> : null}
          </div>
        </div>

        <div className="mt-7 grid items-start gap-6 lg:grid-cols-[minmax(0,1fr)_340px]">
          <liquid-glass
            blur-amount="8"
            className="glass-panel min-w-0 rounded-[28px] shadow-[0_24px_70px_rgba(30,32,44,0.13),inset_0_1px_1px_rgba(255,255,255,0.78)]"
          >
            <section className="p-4 sm:p-6" aria-label="Research 内容文稿">
              <div className="flex flex-wrap items-start justify-between gap-3 border-b border-border/75 px-1 pb-5">
                <div className="min-w-0">
                  <p className="text-[12px] font-semibold tracking-[0.11em] text-hint uppercase">
                    演示主题
                  </p>
                  <h2 className="mt-1.5 truncate text-xl font-bold">
                    {outline?.topic ?? "Research 正在准备内容…"}
                  </h2>
                </div>
                <div className="flex items-center gap-2 text-xs text-hint">
                  <span className="rounded-full bg-white/45 px-3 py-1.5 font-heading tabular-nums">
                    {outline?.page_count ?? "—"} 页
                  </span>
                  <span className="rounded-full bg-white/45 px-3 py-1.5 font-heading tabular-nums">
                    {outline?.ratio ?? "16:9"}
                  </span>
                </div>
              </div>

              <div
                className={cn(
                  "mt-5 transition-opacity duration-200",
                  outline?.status === "generating" && Boolean(outline.manuscript.trim())
                    ? "opacity-55"
                    : "opacity-100",
                )}
              >
                {!outline || !outline.manuscript.trim() ? (
                  <OutlineSkeleton count={outline?.page_count ?? 8} />
                ) : (
                  <article className="manuscript-markdown rounded-[20px] border border-white/55 bg-white/34 px-5 py-6 shadow-[inset_0_1px_0_rgba(255,255,255,0.55)] sm:px-8 sm:py-8">
                    <ReactMarkdown
                      remarkPlugins={[remarkGfm]}
                      components={{
                        img: ({ src, alt }) =>
                          src ? (
                            <img
                              src={resolveApiUrl(src)}
                              alt={alt ?? ""}
                              loading="lazy"
                            />
                          ) : null,
                        a: ({ href, children }) => (
                          <a href={href} target="_blank" rel="noreferrer">
                            {children}
                          </a>
                        ),
                      }}
                    >
                      {outline.manuscript}
                    </ReactMarkdown>
                  </article>
                )}
              </div>

              {outline?.status === "generating" && outline.manuscript.trim() ? (
                <div className="glass-surface sticky bottom-4 mx-auto -mt-2 flex w-max max-w-full items-center gap-2 rounded-full px-4 py-2.5 text-xs font-semibold text-muted-foreground">
                  <LoaderCircleIcon className="size-3.5 animate-spin" />
                  Research 正在按修改意见生成新版本，当前版本仍可查看
                </div>
              ) : null}

              {outline?.status === "failed" ? (
                <div
                  role="alert"
                  className="mt-5 flex items-start gap-3 rounded-[18px] border border-destructive/15 bg-destructive/[0.065] p-4 text-sm"
                >
                  <CircleAlertIcon className="mt-0.5 size-4 shrink-0 text-destructive" />
                  <span>
                    <strong className="block font-semibold">这次内容文稿没有生成成功</strong>
                    <span className="mt-1 block text-xs leading-5 text-muted-foreground">
                      {outline.error_message ?? "请在右侧重新生成。"}
                    </span>
                  </span>
                </div>
              ) : null}
            </section>
          </liquid-glass>

          <aside className="space-y-4 lg:sticky lg:top-28">
            <div className="glass-card rounded-[24px] p-5">
              <div className="flex items-start gap-3">
                <span className="flex size-9 shrink-0 items-center justify-center rounded-full bg-primary text-primary-foreground">
                  <MessageSquareTextIcon className="size-4" />
                </span>
                <div>
                  <h2 className="text-[15px] font-bold">修改内容文稿</h2>
                  <p className="mt-1 text-xs leading-5 text-hint">
                    修改意见会接在现有 Research 上下文后继续处理。
                  </p>
                </div>
              </div>

              <Textarea
                value={comment}
                onChange={(event) => setComment(event.target.value)}
                maxLength={1000}
                rows={5}
                disabled={
                  outline?.status === "generating" ||
                  outline?.status === "approved" ||
                  pending !== null
                }
                className="mt-4 min-h-32 resize-none rounded-[16px] border-white/60 bg-white/38 px-3.5 py-3 text-[13px] leading-6 shadow-inner"
                placeholder="例如：删掉重复的第四幕；把结论提前；精简每页正文……"
              />
              <div className="mt-2 flex items-center justify-between text-[11px] text-hint">
                <span>可留空，直接让 Research 重试当前文稿</span>
                <span className="font-heading tabular-nums">{comment.length} / 1000</span>
              </div>

              <div className="mt-4 flex items-start gap-2.5 rounded-[15px] bg-emerald-700/[0.065] p-3 text-[11.5px] leading-5 text-emerald-950/75">
                <ShieldCheckIcon className="mt-0.5 size-4 shrink-0 text-emerald-700" />
                <span>
                  默认沿用当前 Research 上下文，不重新调研；只有明确要求新事实时才补充必要资料。
                </span>
              </div>

              <Button
                variant="glass"
                className="mt-4 h-10 w-full rounded-full border-white/70"
                disabled={
                  !outline ||
                  outline.status === "generating" ||
                  outline.status === "approved" ||
                  pending !== null
                }
                onClick={() => void handleRegenerate()}
              >
                {pending === "regenerate" ? (
                  <LoaderCircleIcon className="animate-spin" />
                ) : (
                  <RefreshCwIcon />
                )}
                {comment.trim() ? "按意见修改文稿" : "让 Research 重试"}
              </Button>
            </div>

            {outline?.comments.length ? (
              <div className="glass-card rounded-[22px] p-4">
                <p className="px-1 text-[11px] font-semibold tracking-[0.1em] text-hint uppercase">
                  已采纳批注
                </p>
                <div className="mt-3 max-h-40 space-y-2 overflow-y-auto pr-1">
                  {outline.comments
                    .slice()
                    .reverse()
                    .map((item) => (
                      <div
                        key={item.comment_id}
                        className="rounded-[14px] bg-white/38 px-3 py-2.5 text-xs leading-5 text-muted-foreground"
                      >
                        <span className="mb-0.5 block font-heading text-[9.5px] font-semibold text-hint">
                          应用于第 {item.target_revision} 版
                        </span>
                        {item.text}
                      </div>
                    ))}
                </div>
              </div>
            ) : null}

            <div className="rounded-[24px] border border-white/25 bg-foreground p-5 text-background shadow-[0_18px_42px_rgba(30,32,44,0.2)]">
              <div className="flex items-center gap-2 text-sm font-bold">
                <SparklesIcon className="size-4" />
                内容没问题？
              </div>
              <p className="mt-2 text-xs leading-5 text-background/65">
                确认后会锁定当前 Markdown，跳过 Research，直接进入逐页设计与导出。
              </p>
              <Button
                className="mt-4 h-11 w-full rounded-full bg-background font-semibold text-foreground shadow-[0_10px_24px_rgba(0,0,0,0.18)] hover:bg-background/85"
                disabled={!outline || outline.status !== "ready" || pending !== null}
                onClick={() => void handleApprove()}
              >
                {pending === "approve" ? (
                  <LoaderCircleIcon className="animate-spin" />
                ) : (
                  <CheckCircle2Icon />
                )}
                同意内容，继续生成
                {pending !== "approve" ? <ArrowRightIcon /> : null}
              </Button>
              {outline?.status === "approved" && outline.task_id ? (
                <Button
                  variant="ghost"
                  className="mt-2 h-9 w-full rounded-full text-background hover:bg-white/10 hover:text-background"
                  onClick={() =>
                    navigate(`/workbench/${outline.task_id}`, {
                      state: { requestedPages: outline.page_count },
                    })
                  }
                >
                  进入生成工作台
                  <ArrowRightIcon />
                </Button>
              ) : null}
            </div>
          </aside>
        </div>
      </main>
    </AppShell>
  )
}
