import { cn } from "@/lib/utils"

interface BrandMarkProps {
  className?: string
}

export function BrandMark({ className }: BrandMarkProps) {
  return (
    <span
      aria-hidden="true"
      className={cn("relative block size-5 shrink-0", className)}
    >
      <span className="absolute top-1 left-0 block h-3 w-4 rounded-[3px] bg-primary-soft" />
      <span className="absolute top-0 left-1 block h-3 w-4 rounded-[3px] bg-primary" />
    </span>
  )
}
