// Renders a DeckSlide into a self-contained SVG data URL. The mock server
// serves these as `preview_url`, so pages consume plain <img> URLs exactly
// like they will with the real backend.

import type { DeckSlide } from "@/mocks/deck"
import type { TemplatePalette } from "@/types/api"

const FONT = "'PingFang SC','Microsoft YaHei','Noto Sans SC',sans-serif"
const NUM_FONT = `'Sora',${FONT}`

interface Theme {
  bg: string
  ink: string
  muted: string
  faint: string
  faintLine: string
  stripeA: string
  stripeB: string
  surface: string
  accent: string
}

function themeOf(palette: TemplatePalette, accentIdx: number): Theme {
  const dark = palette.dark
  const accents = [
    palette.primary,
    palette.accent,
    dark ? "#E8908A" : "#A84A2F",
  ]
  return {
    bg: palette.bg,
    ink: palette.ink,
    muted: dark ? "rgba(255,255,255,0.55)" : "rgba(0,0,0,0.45)",
    faint: dark ? "rgba(255,255,255,0.10)" : "rgba(0,0,0,0.07)",
    faintLine: dark ? "rgba(255,255,255,0.14)" : "rgba(0,0,0,0.10)",
    stripeA: dark ? "rgba(255,255,255,0.06)" : "rgba(0,0,0,0.045)",
    stripeB: dark ? "rgba(255,255,255,0.11)" : "rgba(0,0,0,0.08)",
    surface: dark ? "rgba(255,255,255,0.05)" : palette.surface,
    accent: accents[accentIdx % accents.length],
  }
}

function esc(value: string): string {
  return value
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
}

function charWidth(char: string): number {
  return /[⺀-｠ﾠ-ￜ]/.test(char) ? 1 : 0.56
}

function wrap(text: string, maxUnits: number, maxLines = 3): string[] {
  const lines: string[] = []
  let line = ""
  let units = 0
  for (const char of text) {
    const w = charWidth(char)
    if (units + w > maxUnits && line) {
      lines.push(line)
      line = ""
      units = 0
      if (lines.length === maxLines) {
        break
      }
    }
    line += char
    units += w
  }
  if (line && lines.length < maxLines) {
    lines.push(line)
  }
  return lines
}

interface TextOpts {
  size: number
  fill: string
  weight?: number
  font?: string
  anchor?: "start" | "middle" | "end"
  spacing?: string
}

function text(x: number, y: number, value: string, opts: TextOpts): string {
  const { size, fill, weight = 400, font = FONT, anchor = "start", spacing } = opts
  const extra = spacing ? ` letter-spacing="${spacing}"` : ""
  return `<text x="${x}" y="${y}" font-family="${font}" font-size="${size}" font-weight="${weight}" fill="${fill}" text-anchor="${anchor}"${extra}>${esc(value)}</text>`
}

function paragraph(
  x: number,
  y: number,
  content: string,
  maxUnits: number,
  lineHeight: number,
  opts: TextOpts,
  maxLines = 2,
): { svg: string; bottom: number } {
  const lines = wrap(content, maxUnits, maxLines)
  const svg = lines
    .map((line, index) => text(x, y + index * lineHeight, line, opts))
    .join("")
  return { svg, bottom: y + (lines.length - 1) * lineHeight }
}

function rect(
  x: number,
  y: number,
  w: number,
  h: number,
  fill: string,
  rx = 0,
  extra = "",
): string {
  return `<rect x="${x}" y="${y}" width="${w}" height="${h}" fill="${fill}" rx="${rx}"${extra ? ` ${extra}` : ""}/>`
}

export function renderSlidePreview(
  slide: DeckSlide,
  palette: TemplatePalette,
  ratio: "16:9" | "4:3",
  pageLabel: string,
): string {
  const W = 1280
  const H = ratio === "4:3" ? 960 : 720
  const t = themeOf(palette, slide.accentIdx)
  const PX = 128
  const parts: string[] = [rect(0, 0, W, H, t.bg)]
  const cy = H / 2

  switch (slide.layout) {
    case "cover": {
      parts.push(rect(PX, cy - 128, 78, 9, t.accent, 5))
      const title = paragraph(PX, cy - 40, slide.title, 15, 92, {
        size: 68,
        fill: t.ink,
        weight: 700,
      })
      parts.push(title.svg)
      if (slide.subtitle) {
        parts.push(
          text(PX, title.bottom + 84, slide.subtitle, { size: 28, fill: t.muted }),
        )
      }
      break
    }
    case "toc": {
      const top = H * 0.16
      parts.push(text(PX, top + 40, "目录", { size: 51, fill: t.ink, weight: 700 }))
      const items = slide.items ?? []
      const colW = (W - PX * 2 - 76) / 2
      const rowH = H * 0.155
      items.forEach((item, index) => {
        const col = index % 2
        const row = Math.floor(index / 2)
        const x = PX + col * (colW + 76)
        const y = top + 132 + row * rowH
        parts.push(
          text(x, y, String(index + 1).padStart(2, "0"), {
            size: 28,
            fill: t.accent,
            weight: 700,
            font: NUM_FONT,
          }),
          text(x + 56, y, wrap(item, 15, 1)[0] ?? "", { size: 28, fill: t.ink }),
          rect(x, y + 20, colW, 1.5, t.faintLine),
        )
      })
      break
    }
    case "section": {
      parts.push(
        text(PX, cy - 30, slide.num ?? "01", {
          size: 154,
          fill: t.faint,
          weight: 700,
          font: NUM_FONT,
        }),
        rect(PX, cy + 32, 51, 8, t.accent, 4),
        text(PX + 77, cy + 52, slide.title, { size: 59, fill: t.ink, weight: 700 }),
      )
      break
    }
    case "bullets": {
      const top = H * 0.155
      parts.push(
        text(PX, top + 34, slide.title, { size: 49, fill: t.ink, weight: 700 }),
        rect(PX, top + 62, 64, 7, t.accent, 4),
      )
      let y = top + 148
      for (const bullet of slide.bullets ?? []) {
        parts.push(`<circle cx="${PX + 7}" cy="${y - 10}" r="6.5" fill="${t.accent}"/>`)
        const par = paragraph(PX + 34, y, bullet, 27, 46, { size: 30, fill: t.ink })
        parts.push(par.svg)
        y = par.bottom + 66
      }
      break
    }
    case "split": {
      const top = H * 0.155
      parts.push(
        text(PX * 0.8, top + 32, slide.title, { size: 46, fill: t.ink, weight: 700 }),
        rect(PX * 0.8, top + 58, 64, 7, t.accent, 4),
      )
      let y = top + 136
      for (const bullet of slide.bullets ?? []) {
        parts.push(
          `<circle cx="${PX * 0.8 + 6}" cy="${y - 9}" r="5.5" fill="${t.accent}"/>`,
        )
        const par = paragraph(PX * 0.8 + 30, y, bullet, 20, 40, {
          size: 27,
          fill: t.ink,
        })
        parts.push(par.svg)
        y = par.bottom + 58
      }
      const boxX = W * 0.55
      const boxW = W - boxX - PX * 0.8
      const boxH = H * 0.56
      const boxY = cy - boxH / 2 + 20
      parts.push(
        `<defs><pattern id="stripe" width="28" height="28" patternTransform="rotate(45)" patternUnits="userSpaceOnUse"><rect width="28" height="28" fill="${t.stripeA}"/><rect width="14" height="28" fill="${t.stripeB}"/></pattern></defs>`,
        rect(boxX, boxY, boxW, boxH, t.bg, 18),
        rect(boxX, boxY, boxW, boxH, "url(#stripe)", 18),
        rect(boxX + boxW / 2 - 92, boxY + boxH / 2 - 26, 184, 52, t.bg, 10),
        text(boxX + boxW / 2, boxY + boxH / 2 + 8, slide.img ?? "示意配图", {
          size: 22,
          fill: t.muted,
          anchor: "middle",
        }),
      )
      break
    }
    case "chart": {
      const top = H * 0.155
      parts.push(
        text(PX, top + 32, slide.title, { size: 46, fill: t.ink, weight: 700 }),
        rect(PX, top + 58, 64, 7, t.accent, 4),
      )
      const bars = slide.bars ?? []
      const maxV = Math.max(...bars.map((bar) => bar.v), 1)
      const areaH = H * 0.4
      const baseY = top + 130 + areaH
      const areaW = W * 0.36
      const slotW = areaW / Math.max(bars.length, 1)
      bars.forEach((bar, index) => {
        const barW = slotW * 0.52
        const barH = (bar.v / maxV) * (areaH * 0.82)
        const x = PX + index * slotW + (slotW - barW) / 2
        parts.push(
          rect(x, baseY - barH, barW, barH, t.accent, 8,
            `opacity="${index === bars.length - 1 ? 1 : 0.55}"`),
          text(x + barW / 2, baseY - barH - 14, String(bar.v), {
            size: 20,
            fill: t.muted,
            font: NUM_FONT,
            anchor: "middle",
          }),
          text(x + barW / 2, baseY + 34, bar.label, {
            size: 20,
            fill: t.muted,
            font: NUM_FONT,
            anchor: "middle",
          }),
        )
      })
      parts.push(rect(PX, baseY, areaW, 1.5, t.faintLine))
      const kpiX = W * 0.55
      const kpiW = W - kpiX - PX
      let ky = top + 170
      for (const kpi of slide.kpis ?? []) {
        parts.push(
          text(kpiX, ky, kpi.k, { size: 24, fill: t.muted }),
          text(kpiX + kpiW, ky, kpi.v, {
            size: 38,
            fill: t.ink,
            weight: 700,
            font: NUM_FONT,
            anchor: "end",
          }),
          rect(kpiX, ky + 22, kpiW, 1.5, t.faintLine),
        )
        ky += areaH / 3 + 10
      }
      break
    }
    case "compare": {
      const top = H * 0.155
      parts.push(
        text(PX * 0.8, top + 32, slide.title, { size: 46, fill: t.ink, weight: 700 }),
        rect(PX * 0.8, top + 58, 64, 7, t.accent, 4),
      )
      const cardY = top + 122
      const cardH = H - cardY - H * 0.14
      const gap = 38
      const cardW = (W - PX * 1.6 - gap) / 2
      const sides = [
        { data: slide.left, x: PX * 0.8, bar: t.accent },
        { data: slide.right, x: PX * 0.8 + cardW + gap, bar: t.muted },
      ]
      for (const side of sides) {
        if (!side.data) {
          continue
        }
        parts.push(
          rect(side.x, cardY, cardW, cardH, t.surface, 16,
            `stroke="${t.faintLine}" stroke-width="1.5"`),
          rect(side.x, cardY, cardW, 7, side.bar, 3),
          text(side.x + 36, cardY + 66, side.data.t, {
            size: 31,
            fill: t.ink,
            weight: 700,
          }),
        )
        side.data.items.forEach((item, index) => {
          parts.push(
            text(side.x + 36, cardY + 124 + index * 50, `· ${item}`, {
              size: 24,
              fill: t.ink,
            }),
          )
        })
      }
      break
    }
    case "timeline": {
      const top = H * 0.155
      parts.push(
        text(PX * 0.8, top + 32, slide.title, { size: 46, fill: t.ink, weight: 700 }),
        rect(PX * 0.8, top + 58, 64, 7, t.accent, 4),
      )
      const steps = slide.steps ?? []
      const lineY = cy + 10
      parts.push(rect(PX * 0.8, lineY, W - PX * 1.6, 2.5, t.faintLine))
      const slotW = (W - PX * 1.6) / Math.max(steps.length, 1)
      steps.forEach((step, index) => {
        const x = PX * 0.8 + index * slotW
        parts.push(
          `<circle cx="${x + 12}" cy="${lineY + 1}" r="12" fill="${t.accent}"/>`,
          text(x, lineY + 56, step.q, {
            size: 28,
            fill: t.accent,
            weight: 700,
            font: NUM_FONT,
          }),
          paragraph(x, lineY + 100, step.t, (slotW - 40) / 23, 34, {
            size: 23,
            fill: t.ink,
          }).svg,
        )
      })
      break
    }
    case "quote": {
      parts.push(
        text(W / 2, cy - 110, "“", {
          size: 130,
          fill: t.accent,
          weight: 700,
          font: NUM_FONT,
          anchor: "middle",
        }),
      )
      const lines = wrap(slide.quote ?? "", 18, 2)
      lines.forEach((line, index) => {
        parts.push(
          text(W / 2, cy - 10 + index * 66, line, {
            size: 44,
            fill: t.ink,
            weight: 700,
            anchor: "middle",
          }),
        )
      })
      if (slide.by) {
        parts.push(
          text(W / 2, cy - 10 + lines.length * 66 + 22, `—— ${slide.by}`, {
            size: 24,
            fill: t.muted,
            anchor: "middle",
          }),
        )
      }
      break
    }
    case "end": {
      parts.push(
        text(W / 2, cy - 36, slide.title, {
          size: 68,
          fill: t.ink,
          weight: 700,
          anchor: "middle",
        }),
        rect(W / 2 - 39, cy + 12, 78, 9, t.accent, 5),
      )
      if (slide.subtitle) {
        parts.push(
          text(W / 2, cy + 84, slide.subtitle, {
            size: 28,
            fill: t.muted,
            anchor: "middle",
          }),
        )
      }
      break
    }
  }

  parts.push(
    text(W - 40, H - 30, pageLabel, {
      size: 19,
      fill: t.muted,
      font: NUM_FONT,
      anchor: "end",
    }),
  )

  const svg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 ${W} ${H}">${parts.join("")}</svg>`
  return `data:image/svg+xml;charset=utf-8,${encodeURIComponent(svg)}`
}
