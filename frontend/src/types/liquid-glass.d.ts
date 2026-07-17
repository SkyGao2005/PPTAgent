import type { DetailedHTMLProps, HTMLAttributes } from "react"

interface LiquidGlassAttributes
  extends DetailedHTMLProps<HTMLAttributes<HTMLElement>, HTMLElement> {
  /** Backdrop blur in px (default 8). */
  "blur-amount"?: number | string
  /** Refraction displacement intensity (default 70). */
  scale?: number | string
  /** Chromatic aberration intensity (default 2). */
  aberration?: number | string
  /** Backdrop saturation in % (default 140). */
  saturation?: number | string
}

declare module "react" {
  namespace JSX {
    interface IntrinsicElements {
      "liquid-glass": LiquidGlassAttributes
    }
  }
}
