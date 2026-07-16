import { clsx, type ClassValue } from "clsx"
import { twMerge } from "tailwind-merge"

import { resolveApiUrl } from "@/lib/api-url"

export function cn(...inputs: ClassValue[]) {
  return twMerge(clsx(inputs))
}

// Cache busting uses a content event/version token, not the reusable revision
// ordinal. data:/blob: URLs are immutable already and cannot take query params.
export function previewSrc(
  url: string | null,
  contentVersion: string | number,
): string | undefined {
  if (!url) {
    return undefined
  }
  const resolved = resolveApiUrl(url)
  if (resolved.startsWith("data:") || resolved.startsWith("blob:")) {
    return resolved
  }
  return `${resolved}${resolved.includes("?") ? "&" : "?"}v=${encodeURIComponent(contentVersion)}`
}

/** Generate UI-only ids even on non-secure HTTP origins without randomUUID(). */
export function createClientId(): string {
  const randomUuid = globalThis.crypto?.randomUUID
  if (randomUuid) {
    return randomUuid.call(globalThis.crypto)
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}`
}

export function formatClock(totalSeconds: number): string {
  const minutes = Math.floor(totalSeconds / 60)
  const seconds = Math.max(0, totalSeconds % 60)
  return `${minutes}:${String(seconds).padStart(2, "0")}`
}
