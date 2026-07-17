/**
 * <liquid-glass> web component — a block-friendly port of the shader mode in
 * https://github.com/rdev/liquid-glass-react (MIT).
 *
 * SVG filters referenced from inside a shadow root are not reliably resolved
 * when applied to an HTML backdrop layer. Filters therefore live in one
 * document-level SVG, while CSS pseudo-elements render the glass behind the
 * element's light-DOM content.
 */

interface Vec2 {
  x: number
  y: number
}

interface MapSize {
  width: number
  height: number
}

interface IdleWindow {
  requestIdleCallback?: (callback: () => void, options: { timeout: number }) => number
  cancelIdleCallback?: (handle: number) => void
}

const SVG_NAMESPACE = "http://www.w3.org/2000/svg"
const FILTER_HOST_ID = "liquid-glass-filter-host"
const MAX_MAP_EDGE = 384
const MAX_MAP_PIXELS = 96_000
const MAX_CACHED_MAPS = 12
const mapCache = new Map<string, string>()
const idleWindow = window as unknown as IdleWindow
let filterSequence = 0
let sharedDefs: SVGDefsElement | null = null

function smoothStep(a: number, b: number, value: number): number {
  const amount = Math.max(0, Math.min(1, (value - a) / (b - a)))
  return amount * amount * (3 - 2 * amount)
}

function roundedRectSDF(
  x: number,
  y: number,
  width: number,
  height: number,
  radius: number,
): number {
  const qx = Math.abs(x) - width + radius
  const qy = Math.abs(y) - height + radius
  return (
    Math.min(Math.max(qx, qy), 0) +
    Math.hypot(Math.max(qx, 0), Math.max(qy, 0)) -
    radius
  )
}

function liquidGlassFragment(uv: Vec2): Vec2 {
  const ix = uv.x - 0.5
  const iy = uv.y - 0.5
  const distanceToEdge = roundedRectSDF(ix, iy, 0.3, 0.2, 0.6)
  const displacement = smoothStep(0.8, 0, distanceToEdge - 0.15)
  const scaled = smoothStep(0, 1, displacement)
  return { x: ix * scaled + 0.5, y: iy * scaled + 0.5 }
}

function mapSizeFor(width: number, height: number): MapSize {
  const edgeScale = MAX_MAP_EDGE / Math.max(width, height)
  const areaScale = Math.sqrt(MAX_MAP_PIXELS / (width * height))
  const scale = Math.min(1, edgeScale, areaScale)
  return {
    width: Math.max(8, Math.round(width * scale)),
    height: Math.max(8, Math.round(height * scale)),
  }
}

function cacheMap(key: string, value: string): void {
  if (mapCache.size >= MAX_CACHED_MAPS) {
    const oldestKey = mapCache.keys().next().value
    if (oldestKey !== undefined) mapCache.delete(oldestKey)
  }
  mapCache.set(key, value)
}

function displacementMap(width: number, height: number): string | null {
  const key = `${width}x${height}`
  const cached = mapCache.get(key)
  if (cached) {
    mapCache.delete(key)
    mapCache.set(key, cached)
    return cached
  }

  const canvas = document.createElement("canvas")
  canvas.width = width
  canvas.height = height
  const context = canvas.getContext("2d")
  if (!context) return null

  let maxScale = 1
  const rawValues = new Float32Array(width * height * 2)
  let rawIndex = 0

  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const position = liquidGlassFragment({ x: x / width, y: y / height })
      const dx = position.x * width - x
      const dy = position.y * height - y
      maxScale = Math.max(maxScale, Math.abs(dx), Math.abs(dy))
      rawValues[rawIndex] = dx
      rawValues[rawIndex + 1] = dy
      rawIndex += 2
    }
  }

  const imageData = context.createImageData(width, height)
  const pixels = imageData.data
  rawIndex = 0

  for (let y = 0; y < height; y += 1) {
    for (let x = 0; x < width; x += 1) {
      const edgeDistance = Math.min(x, y, width - x - 1, height - y - 1)
      const edgeFactor = Math.min(1, edgeDistance / 2)
      const dx = rawValues[rawIndex] * edgeFactor
      const dy = rawValues[rawIndex + 1] * edgeFactor
      const pixelIndex = rawIndex * 2

      pixels[pixelIndex] = Math.max(0, Math.min(255, (dx / maxScale + 0.5) * 255))
      pixels[pixelIndex + 1] = Math.max(
        0,
        Math.min(255, (dy / maxScale + 0.5) * 255),
      )
      pixels[pixelIndex + 2] = pixels[pixelIndex + 1]
      pixels[pixelIndex + 3] = 255
      rawIndex += 2
    }
  }

  context.putImageData(imageData, 0, 0)
  const dataUrl = canvas.toDataURL("image/png")
  cacheMap(key, dataUrl)
  return dataUrl
}

function documentFilterDefs(): SVGDefsElement {
  if (sharedDefs?.isConnected) return sharedDefs

  const existing = document.getElementById(FILTER_HOST_ID)
  const existingDefs = existing?.querySelector<SVGDefsElement>("defs")
  if (existingDefs) {
    sharedDefs = existingDefs
    return existingDefs
  }

  const svg = document.createElementNS(SVG_NAMESPACE, "svg")
  svg.id = FILTER_HOST_ID
  svg.setAttribute("aria-hidden", "true")
  svg.style.position = "fixed"
  svg.style.width = "0"
  svg.style.height = "0"
  svg.style.overflow = "hidden"
  svg.style.pointerEvents = "none"

  const defs = document.createElementNS(SVG_NAMESPACE, "defs")
  svg.append(defs)
  document.body.append(svg)
  sharedDefs = defs
  return defs
}

const userAgent = navigator.userAgent.toLowerCase()
const supportsDisplacement =
  !userAgent.includes("firefox") &&
  !(userAgent.includes("safari") && !userAgent.includes("chrome") && !userAgent.includes("chromium"))

class LiquidGlass extends HTMLElement {
  static get observedAttributes(): string[] {
    return ["blur-amount", "scale", "aberration", "saturation"]
  }

  private readonly filterId = `liquid-glass-filter-${++filterSequence}`
  private filterElement: SVGFilterElement | null = null
  private filterSignature = ""
  private resizeTimer: ReturnType<typeof setTimeout> | undefined
  private idleHandle: number | undefined
  private resizeObserver: ResizeObserver | null = null

  connectedCallback(): void {
    this.resizeObserver = new ResizeObserver(() => this.scheduleBuild())
    this.resizeObserver.observe(this)
    this.applyBackdrop()
    requestAnimationFrame(() => this.scheduleBuild(true))
  }

  disconnectedCallback(): void {
    this.resizeObserver?.disconnect()
    this.resizeObserver = null
    clearTimeout(this.resizeTimer)
    this.cancelIdleBuild()
    this.filterElement?.remove()
    this.filterElement = null
    this.filterSignature = ""
  }

  attributeChangedCallback(): void {
    if (!this.isConnected) return
    this.applyBackdrop()
    this.scheduleBuild(true)
  }

  private numberAttribute(
    name: string,
    fallback: number,
    minimum: number,
    maximum: number,
  ): number {
    const parsed = Number.parseFloat(this.getAttribute(name) ?? "")
    if (Number.isNaN(parsed)) return fallback
    return Math.max(minimum, Math.min(maximum, parsed))
  }

  private applyBackdrop(): void {
    // `blur` is already an HTMLElement method. React therefore assigns a
    // property instead of emitting the attribute, so use a collision-free
    // custom-element attribute for the blur radius.
    const blur = this.numberAttribute("blur-amount", 8, 0, 40)
    const saturation = this.numberAttribute("saturation", 150, 0, 300)
    this.style.setProperty("--liquid-glass-blur", `${blur}px`)
    this.style.setProperty("--liquid-glass-saturation", `${saturation}%`)
  }

  private scheduleBuild(immediate = false): void {
    clearTimeout(this.resizeTimer)
    if (immediate) {
      this.queueIdleBuild()
      return
    }
    this.resizeTimer = setTimeout(() => this.queueIdleBuild(), 80)
  }

  private queueIdleBuild(): void {
    this.cancelIdleBuild()
    const callback = () => {
      this.idleHandle = undefined
      this.buildFilter()
    }
    if (idleWindow.requestIdleCallback) {
      this.idleHandle = idleWindow.requestIdleCallback(callback, { timeout: 180 })
    } else {
      this.idleHandle = setTimeout(callback, 0)
    }
  }

  private cancelIdleBuild(): void {
    if (this.idleHandle === undefined) return
    if (idleWindow.cancelIdleCallback) {
      idleWindow.cancelIdleCallback(this.idleHandle)
    } else {
      clearTimeout(this.idleHandle)
    }
    this.idleHandle = undefined
  }

  private ensureFilterElement(): SVGFilterElement {
    if (this.filterElement?.isConnected) return this.filterElement
    const filter = document.createElementNS(SVG_NAMESPACE, "filter")
    filter.id = this.filterId
    filter.setAttribute("x", "-35%")
    filter.setAttribute("y", "-35%")
    filter.setAttribute("width", "170%")
    filter.setAttribute("height", "170%")
    filter.setAttribute("color-interpolation-filters", "sRGB")
    documentFilterDefs().append(filter)
    this.filterElement = filter
    return filter
  }

  private disableFilter(signature: string): void {
    this.filterElement?.remove()
    this.filterElement = null
    this.filterSignature = signature
    this.style.setProperty("--liquid-glass-filter", "none")
  }

  private buildFilter(): void {
    const width = Math.round(this.offsetWidth)
    const height = Math.round(this.offsetHeight)
    if (width < 8 || height < 8) return

    const scale = this.numberAttribute("scale", 70, 0, 160)
    const aberration = this.numberAttribute("aberration", 2, 0, 8)
    const signature = `${width}x${height}:${scale}:${aberration}`
    if (this.filterSignature === signature) return
    if (scale === 0 || !supportsDisplacement) {
      this.disableFilter(signature)
      return
    }

    const mapSize = mapSizeFor(width, height)
    const map = displacementMap(mapSize.width, mapSize.height)
    if (!map) {
      this.disableFilter(signature)
      return
    }

    const filter = this.ensureFilterElement()
    filter.innerHTML = `
  <feImage x="0" y="0" width="100%" height="100%" result="MAP" href="${map}" preserveAspectRatio="none"/>
  <feDisplacementMap in="SourceGraphic" in2="MAP" scale="${scale}" xChannelSelector="R" yChannelSelector="B" result="RED_DISPLACED"/>
  <feColorMatrix in="RED_DISPLACED" type="matrix" values="1 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 0 1 0" result="RED_CHANNEL"/>
  <feDisplacementMap in="SourceGraphic" in2="MAP" scale="${scale * (1 - aberration * 0.05)}" xChannelSelector="R" yChannelSelector="B" result="GREEN_DISPLACED"/>
  <feColorMatrix in="GREEN_DISPLACED" type="matrix" values="0 0 0 0 0 0 1 0 0 0 0 0 0 0 0 0 0 0 1 0" result="GREEN_CHANNEL"/>
  <feDisplacementMap in="SourceGraphic" in2="MAP" scale="${scale * (1 - aberration * 0.1)}" xChannelSelector="R" yChannelSelector="B" result="BLUE_DISPLACED"/>
  <feColorMatrix in="BLUE_DISPLACED" type="matrix" values="0 0 0 0 0 0 0 0 0 0 0 0 1 0 0 0 0 0 1 0" result="BLUE_CHANNEL"/>
  <feBlend in="GREEN_CHANNEL" in2="BLUE_CHANNEL" mode="screen" result="GB_COMBINED"/>
  <feBlend in="RED_CHANNEL" in2="GB_COMBINED" mode="screen"/>
`
    this.filterSignature = signature
    this.style.setProperty("--liquid-glass-filter", `url("#${this.filterId}")`)
  }
}

if (!customElements.get("liquid-glass")) {
  customElements.define("liquid-glass", LiquidGlass)
}

export {}
