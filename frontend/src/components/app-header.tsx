import { NavLink } from "react-router-dom"

import { BrandMark } from "@/components/brand-mark"
import { cn } from "@/lib/utils"

const navItems = [
  { to: "/", label: "创建" },
  { to: "/templates", label: "模板库" },
]

export function AppHeader() {
  return (
    <liquid-glass
      blur-amount="3"
      scale="70"
      className="glass-panel fixed top-5 left-1/2 z-50 w-max -translate-x-1/2 rounded-full [--liquid-glass-tint:rgba(255,255,255,0.20)]"
    >
      <div className="flex items-center gap-4 py-[9px] pr-2.5 pl-5">
        <NavLink to="/" className="flex items-center gap-2.5 text-foreground">
          <BrandMark />
          <span className="font-heading text-[17.5px] font-semibold tracking-tight">
            PPTAgent
          </span>
        </NavLink>
        <nav className="flex items-center gap-0.5">
          {navItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.to === "/"}
              className={({ isActive }) =>
                cn(
                  "rounded-full px-[17px] py-[9px] text-[13.5px] transition-colors",
                  isActive
                    ? "bg-primary font-semibold text-primary-foreground"
                    : "font-medium text-foreground/75 hover:bg-white/55 hover:text-foreground",
                )
              }
            >
              {item.label}
            </NavLink>
          ))}
        </nav>
      </div>
    </liquid-glass>
  )
}
