import { Component, type ErrorInfo, type ReactNode } from "react"
import { RotateCcwIcon, TriangleAlertIcon } from "lucide-react"

import { SceneBackground } from "@/components/scene-background"
import { Button } from "@/components/ui/button"

interface AppErrorBoundaryProps {
  children: ReactNode
}
interface AppErrorBoundaryState {
  error: Error | null
}

export class AppErrorBoundary extends Component<
  AppErrorBoundaryProps,
  AppErrorBoundaryState
> {
  state: AppErrorBoundaryState = { error: null }

  static getDerivedStateFromError(error: Error): AppErrorBoundaryState {
    return { error }
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    console.error("Frontend route failed to render", error, info)
  }

  render(): ReactNode {
    if (!this.state.error) {
      return this.props.children
    }
    return (
      <main
        id="main-content"
        tabIndex={-1}
        className="relative flex min-h-svh flex-col items-center justify-center px-6 text-center"
      >
        <SceneBackground />
        {/* Plain CSS glass here: this screen also covers a failed chunk load,
            so it must not depend on the <liquid-glass> element being defined. */}
        <div className="glass-card relative z-10 flex w-[min(92vw,400px)] flex-col items-center rounded-[28px] px-8 py-9">
          <span className="flex size-12 items-center justify-center rounded-full bg-foreground/[0.06] text-hint">
            <TriangleAlertIcon className="size-6" />
          </span>
          <h1 className="mt-4 font-serif text-[22px] font-black tracking-tight">
            页面资源加载失败
          </h1>
          <p className="mt-1.5 text-[13px] leading-5 text-hint">
            可能是网络中断，或当前标签页仍在使用旧版本资源。刷新后会重新载入最新前端文件。
          </p>
          <Button
            className="mt-6 h-10 rounded-full px-5 text-[13px] font-semibold shadow-[0_10px_26px_rgba(27,28,32,0.28)]"
            onClick={() => window.location.reload()}
          >
            <RotateCcwIcon />
            刷新页面
          </Button>
        </div>
      </main>
    )
  }
}
