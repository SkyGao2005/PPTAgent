import type { ReactNode } from "react"

import { AppHeader } from "@/components/app-header"

interface AppShellProps {
  children: ReactNode
}

export function AppShell({ children }: AppShellProps) {
  return (
    <div className="min-h-svh bg-background">
      <AppHeader />
      {children}
    </div>
  )
}
