import type { ReactNode } from "react"

import { AppHeader } from "@/components/app-header"
import { SceneBackground } from "@/components/scene-background"

interface AppShellProps {
  children: ReactNode
}

export function AppShell({ children }: AppShellProps) {
  return (
    <div className="relative min-h-svh">
      <SceneBackground />
      <AppHeader />
      {/* pt clears the floating pill nav (top-5 + nav height + breathing room). */}
      <div className="relative z-10 pt-24">{children}</div>
    </div>
  )
}
