import { Component, type ErrorInfo, type ReactNode } from "react"

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
        className="flex min-h-svh flex-col items-center justify-center gap-4 bg-background px-6 text-center"
      >
        <h1 className="text-lg font-bold">页面资源加载失败</h1>
        <p className="max-w-md text-sm text-muted-foreground">
          可能是网络中断，或当前标签页仍在使用旧版本资源。刷新后会重新载入最新前端文件。
        </p>
        <Button onClick={() => window.location.reload()}>刷新页面</Button>
      </main>
    )
  }
}
