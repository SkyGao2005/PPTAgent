import { CheckIcon, TriangleAlertIcon } from "lucide-react"

import { cn } from "@/lib/utils"
import type { TemplateSummary } from "@/types/api"

interface TemplateCoverProps {
  template: TemplateSummary
  selected?: boolean
  showStatus?: boolean
  className?: string
}

export function TemplateCover({
  template,
  selected = false,
  showStatus = false,
  className,
}: TemplateCoverProps) {
  const { palette } = template
  const titleColor = palette.dark ? "rgba(255,255,255,0.9)" : "#2a2b31"
  const subColor = palette.dark ? "rgba(255,255,255,0.3)" : "rgba(27,28,32,0.16)"
  const accentColor = palette.dark ? palette.primary : palette.accent
  const blockSoft = `color-mix(in srgb, ${palette.primary} 30%, transparent)`
  const blockStrong = `color-mix(in srgb, ${palette.primary} 60%, transparent)`

  // Covers are always 16:9 regardless of the template's slide ratio, so
  // grid cards line up and the card border hugs the thumbnail.
  return (
    <div
      className={cn("relative aspect-video overflow-hidden", className)}
      style={{ backgroundColor: palette.bg }}
    >
      <div
        className="absolute top-[10%] left-[7%] h-[15%] w-[2%] rounded-[2px]"
        style={{ backgroundColor: accentColor }}
      />
      <div
        className="absolute top-[33%] left-[7%] h-[7%] w-[56%] rounded-[3px]"
        style={{ backgroundColor: titleColor }}
      />
      <div
        className="absolute top-[46%] left-[7%] h-[4.5%] w-[36%] rounded-[3px]"
        style={{ backgroundColor: subColor }}
      />
      <div
        className="absolute bottom-[10%] left-[7%] h-[21%] w-[52%] rounded-[4px]"
        style={{ backgroundColor: blockSoft }}
      />
      <div
        className="absolute right-[7%] bottom-[10%] h-[21%] w-[32%] rounded-[4px]"
        style={{ backgroundColor: blockStrong }}
      />

      {selected && (
        <span className="absolute top-2 right-2 flex size-6 items-center justify-center rounded-full bg-primary text-primary-foreground shadow-sm">
          <CheckIcon className="size-3.5" />
        </span>
      )}

      {showStatus && template.status === "parsing" && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2.5 bg-white/75 backdrop-blur-[2px]">
          <span className="text-xs font-medium text-muted-foreground">
            解析中 {template.progress ?? 0}%
          </span>
          <span className="block h-[5px] w-3/5 overflow-hidden rounded-full bg-border">
            <span
              className="animate-stripe block h-full rounded-full bg-[repeating-linear-gradient(45deg,#1b1c20,#1b1c20_7px,#4a4b52_7px,#4a4b52_14px)] bg-[length:28px_100%] transition-[width] duration-400"
              style={{ width: `${template.progress ?? 0}%` }}
            />
          </span>
        </div>
      )}

      {showStatus && template.status === "failed" && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-1.5 bg-white/70 backdrop-blur-sm">
          <span className="animate-light-up flex size-8 items-center justify-center rounded-full bg-destructive text-white shadow-[0_8px_20px_color-mix(in_srgb,var(--destructive)_45%,transparent)]">
            <TriangleAlertIcon className="size-4" />
          </span>
          <span className="mt-0.5 text-[13px] font-bold text-destructive">解析失败</span>
          <span className="max-w-[85%] truncate text-[11px] text-hint">
            {template.error || "解析过程出错，请重新解析"}
          </span>
        </div>
      )}
    </div>
  )
}
