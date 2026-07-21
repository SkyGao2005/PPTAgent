import { useCallback, useEffect, useLayoutEffect, useRef } from "react"

const FLIP_DURATION_MS = 200
const FLIP_EASING = "cubic-bezier(0.77, 0, 0.175, 1)"

function readTranslate(transform: string): { x: number; y: number } {
  if (transform === "none") {
    return { x: 0, y: 0 }
  }

  const values = transform
    .slice(transform.indexOf("(") + 1, -1)
    .split(",")
    .map(Number)
  if (transform.startsWith("matrix3d(") && values.length === 16) {
    return { x: values[12], y: values[13] }
  }
  if (transform.startsWith("matrix(") && values.length === 6) {
    return { x: values[4], y: values[5] }
  }
  return { x: 0, y: 0 }
}

export function useFlipLayout(
  keys: readonly string[],
  motionToken: string,
): (key: string, node: HTMLElement | null) => void {
  const nodesRef = useRef(new Map<string, HTMLElement>())
  const previousRectsRef = useRef(new Map<string, DOMRect>())
  const previousMotionTokenRef = useRef<string | null>(null)
  const animationsRef = useRef(new Map<string, Animation>())
  const keySignature = keys.join("\u0000")
  const keysRef = useRef(keys)
  keysRef.current = keys

  const registerNode = useCallback((key: string, node: HTMLElement | null): void => {
    if (node) {
      nodesRef.current.set(key, node)
      return
    }
    nodesRef.current.delete(key)
  }, [])

  useLayoutEffect(() => {
    const movingPositions = new Map<string, { left: number; top: number }>()
    animationsRef.current.forEach((_animation, key) => {
      const node = nodesRef.current.get(key)
      const previousRect = previousRectsRef.current.get(key)
      if (node && previousRect) {
        const translate = readTranslate(window.getComputedStyle(node).transform)
        movingPositions.set(key, {
          left: previousRect.left + translate.x,
          top: previousRect.top + translate.y,
        })
      }
    })
    animationsRef.current.forEach((animation) => animation.cancel())
    animationsRef.current.clear()

    const targetRects = new Map<string, DOMRect>()
    keysRef.current.forEach((key) => {
      const node = nodesRef.current.get(key)
      if (node) {
        targetRects.set(key, node.getBoundingClientRect())
      }
    })

    const shouldAnimate =
      previousMotionTokenRef.current !== null &&
      previousMotionTokenRef.current !== motionToken &&
      !window.matchMedia("(prefers-reduced-motion: reduce)").matches

    if (shouldAnimate) {
      targetRects.forEach((rect, key) => {
        const previousRect = movingPositions.get(key) ?? previousRectsRef.current.get(key)
        const node = nodesRef.current.get(key)
        if (!previousRect || !node) {
          return
        }

        const deltaX = previousRect.left - rect.left
        const deltaY = previousRect.top - rect.top
        if (Math.abs(deltaX) < 0.5 && Math.abs(deltaY) < 0.5) {
          return
        }

        const animation = node.animate(
          [
            { transform: `translate(${deltaX}px, ${deltaY}px)` },
            { transform: "translate(0, 0)" },
          ],
          { duration: FLIP_DURATION_MS, easing: FLIP_EASING },
        )
        animationsRef.current.set(key, animation)
        animation.onfinish = () => {
          if (animationsRef.current.get(key) === animation) {
            animationsRef.current.delete(key)
          }
        }
      })
    }

    previousRectsRef.current = targetRects
    previousMotionTokenRef.current = motionToken
  }, [keySignature, motionToken])

  useEffect(
    () => () => {
      animationsRef.current.forEach((animation) => animation.cancel())
      animationsRef.current.clear()
    },
    [],
  )

  return registerNode
}
