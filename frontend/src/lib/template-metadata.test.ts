import { describe, expect, it } from "vitest"

import {
  formatTemplateLayoutSummary,
  getTemplatePaletteSwatches,
  normalizeTemplateLayouts,
  summarizeTemplateLayouts,
} from "@/lib/template-metadata"

describe("template metadata", () => {
  it("keeps the four user-facing palette roles in a stable order", () => {
    const swatches = getTemplatePaletteSwatches({
      bg: "#FFFFFF",
      surface: "#F4F4F4",
      primary: "#123456",
      accent: "#ABCDEF",
      ink: "#111111",
      dark: false,
    })

    expect(swatches).toEqual([
      { key: "bg", label: "背景", color: "#FFFFFF" },
      { key: "primary", label: "主色", color: "#123456" },
      { key: "accent", label: "强调", color: "#ABCDEF" },
      { key: "ink", label: "文字", color: "#111111" },
    ])
  })

  it("normalizes duplicate and blank page forms before presenting them", () => {
    const layouts = normalizeTemplateLayouts([
      "封面",
      " 图文分栏 ",
      "封面",
      "",
      "数据表格",
    ])

    expect(layouts).toEqual(["封面", "图文分栏", "数据表格"])
    expect(summarizeTemplateLayouts(layouts, 2)).toEqual({
      visible: ["封面", "图文分栏"],
      remaining: 1,
    })
    expect(formatTemplateLayoutSummary(layouts)).toBe("封面 · 图文分栏 · +1")
  })

  it("uses a clear pending label before page forms are available", () => {
    expect(formatTemplateLayoutSummary([])).toBe("页面形式待解析")
  })
})
