import { useEffect } from "react"

export function usePageMetadata(title: string): void {
  useEffect(() => {
    document.title = `${title} · PPTAgent`
    window.scrollTo({ top: 0, left: 0 })
    const frame = requestAnimationFrame(() => {
      document.querySelector<HTMLElement>("#main-content")?.focus({ preventScroll: true })
    })
    return () => cancelAnimationFrame(frame)
  }, [title])
}
