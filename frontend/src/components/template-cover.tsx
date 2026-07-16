import { CheckIcon } from "lucide-react"

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
  const barColor = palette.dark ? palette.ink : palette.primary
  const subColor = palette.dark ? "rgba(255,255,255,0.35)" : "rgba(0,0,0,0.18)"
  const accentColor = palette.dark ? palette.primary : palette.accent
  const blockColor = palette.dark ? "rgba(255,255,255,0.10)" : "rgba(0,0,0,0.06)"

  // Covers are always 16:9 regardless of the template's slide ratio, so
  // grid cards line up and the card border hugs the thumbnail.
  return (
    <div
      className={cn("relative aspect-video overflow-hidden", className)}
      style={{ backgroundColor: palette.bg }}
    >
      <div
        className="absolute top-[28%] left-[10%] h-[10%] w-[52%] rounded-[3px]"
        style={{ backgroundColor: barColor }}
      />
      <div
        className="absolute top-[46%] left-[10%] h-[5.5%] w-[32%] rounded-sm"
        style={{ backgroundColor: subColor }}
      />
      <div
        className="absolute bottom-[13%] left-[10%] h-[4.5%] w-[13%] rounded-sm"
        style={{ backgroundColor: accentColor }}
      />
      <div
        className="absolute top-[22%] right-[8%] h-[40%] w-[22%] rounded-md"
        style={{ backgroundColor: blockColor }}
      />

      {selected && (
        <span className="absolute top-2 right-2 flex size-6 items-center justify-center rounded-full bg-primary text-primary-foreground shadow-sm">
          <CheckIcon className="size-3.5" />
        </span>
      )}

      {showStatus && template.status === "parsing" && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-2.5 bg-[rgba(250,249,246,0.75)] backdrop-blur-[2px]">
          <span className="text-xs font-medium text-muted-foreground">
            解析中 {template.progress ?? 0}%
          </span>
          <span className="block h-[5px] w-3/5 overflow-hidden rounded-full bg-border">
            <span
              className="animate-stripe block h-full rounded-full bg-[repeating-linear-gradient(45deg,oklch(0.45_0.11_272),oklch(0.45_0.11_272)_7px,oklch(0.58_0.11_272)_7px,oklch(0.58_0.11_272)_14px)] bg-[length:28px_100%] transition-[width] duration-400"
              style={{ width: `${template.progress ?? 0}%` }}
            />
          </span>
        </div>
      )}

      {showStatus && template.status === "failed" && (
        <div className="absolute inset-0 flex flex-col items-center justify-center gap-1.5 bg-[rgba(250,249,246,0.85)]">
          <span className="text-xs font-bold text-destructive">⚠ 解析失败</span>
          <span className="max-w-[85%] truncate text-[11px] text-hint">
            {template.error || "解析过程出错，请重新解析"}
          </span>
        </div>
      )}
    </div>
  )
}
