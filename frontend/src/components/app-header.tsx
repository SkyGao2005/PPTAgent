import { NavLink } from "react-router-dom"

import { Avatar, AvatarFallback } from "@/components/ui/avatar"
import { BrandMark } from "@/components/brand-mark"
import { cn } from "@/lib/utils"

const navItems = [
  { to: "/", label: "创建" },
  { to: "/templates", label: "模板库" },
]

export function AppHeader() {
  return (
    <header className="sticky top-0 z-40 border-b bg-background/92 backdrop-blur-md">
      <div className="mx-auto flex h-15 max-w-7xl items-center justify-between px-4 sm:px-6 lg:px-8">
        <div className="flex items-center gap-5 sm:gap-8">
          <NavLink
            to="/"
            className="flex items-center gap-2.5 text-foreground"
          >
            <BrandMark className="size-[22px]" />
            <span className="font-heading text-[17px] font-semibold tracking-tight">
              PPTAgent
            </span>
          </NavLink>
          <nav className="flex items-center gap-1">
            {navItems.map((item) => (
              <NavLink
                key={item.to}
                to={item.to}
                end={item.to === "/"}
                className={({ isActive }) =>
                  cn(
                    "rounded-lg px-3 py-1.5 text-sm text-muted-foreground transition-colors hover:bg-muted hover:text-foreground",
                    isActive && "bg-muted font-medium text-foreground",
                  )
                }
              >
                {item.label}
              </NavLink>
            ))}
          </nav>
        </div>
        <div className="flex items-center gap-3">
          <span className="hidden text-xs text-muted-foreground sm:inline">
            内部预览
          </span>
          <Avatar size="sm">
            <AvatarFallback className="bg-primary text-primary-foreground">
              李
            </AvatarFallback>
          </Avatar>
        </div>
      </div>
    </header>
  )
}
