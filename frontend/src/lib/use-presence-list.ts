import { useLayoutEffect, useRef, useState } from "react"

export type PresencePhase = "entering" | "present" | "exiting"

export interface PresenceEntry<T> {
  key: string
  value: T
  phase: PresencePhase
}

export function reconcilePresenceEntries<T>(
  current: readonly PresenceEntry<T>[],
  values: readonly T[],
  getKey: (value: T) => string,
): PresenceEntry<T>[] {
  const currentByKey = new Map(current.map((entry) => [entry.key, entry]))
  const incomingKeys = new Set(values.map(getKey))
  const next: PresenceEntry<T>[] = values.map((value) => {
    const key = getKey(value)
    const existing = currentByKey.get(key)

    if (!existing) {
      return { key, value, phase: "entering" }
    }

    return {
      key,
      value,
      phase: existing.phase === "exiting" ? "present" : existing.phase,
    }
  })

  current.forEach((entry, index) => {
    if (incomingKeys.has(entry.key)) {
      return
    }

    const previousKey = current
      .slice(0, index)
      .reverse()
      .find((candidate) => next.some((nextEntry) => nextEntry.key === candidate.key))?.key
    const nextKey = current
      .slice(index + 1)
      .find((candidate) => incomingKeys.has(candidate.key))?.key
    const previousIndex = previousKey
      ? next.findIndex((candidate) => candidate.key === previousKey)
      : -1
    const nextIndex = nextKey ? next.findIndex((candidate) => candidate.key === nextKey) : -1
    const insertionIndex = previousIndex >= 0 ? previousIndex + 1 : nextIndex >= 0 ? nextIndex : next.length

    next.splice(insertionIndex, 0, { ...entry, phase: "exiting" })
  })

  return next
}

export function usePresenceList<T>(
  values: readonly T[],
  getKey: (value: T) => string,
  exitDurationMs: number,
): readonly PresenceEntry<T>[] {
  const [entries, setEntries] = useState<PresenceEntry<T>[]>(() =>
    values.map((value) => ({ key: getKey(value), value, phase: "present" })),
  )
  const latestKeysRef = useRef(new Set(values.map(getKey)))
  const exitTimersRef = useRef(new Map<string, number>())
  latestKeysRef.current = new Set(values.map(getKey))

  useLayoutEffect(() => {
    setEntries((current) => reconcilePresenceEntries(current, values, getKey))
  }, [getKey, values])

  useLayoutEffect(() => {
    latestKeysRef.current.forEach((key) => {
      const timer = exitTimersRef.current.get(key)
      if (timer !== undefined) {
        window.clearTimeout(timer)
        exitTimersRef.current.delete(key)
      }
    })

    entries.forEach((entry) => {
      if (entry.phase !== "exiting" || exitTimersRef.current.has(entry.key)) {
        return
      }
      const timer = window.setTimeout(() => {
        exitTimersRef.current.delete(entry.key)
        if (latestKeysRef.current.has(entry.key)) {
          return
        }
        setEntries((current) => current.filter((candidate) => candidate.key !== entry.key))
      }, exitDurationMs)
      exitTimersRef.current.set(entry.key, timer)
    })

    const frame = entries.some((entry) => entry.phase === "entering")
      ? window.requestAnimationFrame(() => {
          setEntries((current) =>
            current.map((entry) =>
              entry.phase === "entering" ? { ...entry, phase: "present" } : entry,
            ),
          )
        })
      : null

    return () => {
      if (frame !== null) {
        window.cancelAnimationFrame(frame)
      }
    }
  }, [entries, exitDurationMs])

  useLayoutEffect(
    () => () => {
      exitTimersRef.current.forEach((timer) => window.clearTimeout(timer))
      exitTimersRef.current.clear()
    },
    [],
  )

  return entries
}
