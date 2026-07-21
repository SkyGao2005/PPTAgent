import type { TemplatePalette } from "@/types/api"

export type TemplatePaletteKey = "bg" | "surface" | "primary" | "accent" | "ink"

export interface TemplatePaletteSwatch {
  key: TemplatePaletteKey
  label: string
  color: string
}

const PALETTE_LABELS: Record<TemplatePaletteKey, string> = {
  bg: "背景",
  surface: "卡片",
  primary: "主色",
  accent: "强调",
  ink: "文字",
}

export const MAIN_PALETTE_KEYS = ["bg", "primary", "accent", "ink"] as const

export function getTemplatePaletteSwatches(
  palette: TemplatePalette,
  keys: readonly TemplatePaletteKey[] = MAIN_PALETTE_KEYS,
): TemplatePaletteSwatch[] {
  return keys.map((key) => ({
    key,
    label: PALETTE_LABELS[key],
    color: palette[key],
  }))
}

export function normalizeTemplateLayouts(layouts: readonly string[]): string[] {
  return [...new Set(layouts.map((layout) => layout.trim()).filter(Boolean))]
}

export function summarizeTemplateLayouts(
  layouts: readonly string[],
  limit: number,
): { visible: string[]; remaining: number } {
  const normalized = normalizeTemplateLayouts(layouts)
  const visible = normalized.slice(0, Math.max(0, limit))
  return { visible, remaining: normalized.length - visible.length }
}

export function formatTemplateLayoutSummary(
  layouts: readonly string[],
  limit = 2,
): string {
  const { visible, remaining } = summarizeTemplateLayouts(layouts, limit)
  if (visible.length === 0) {
    return "页面形式待解析"
  }
  return `${visible.join(" · ")}${remaining > 0 ? ` · +${remaining}` : ""}`
}
