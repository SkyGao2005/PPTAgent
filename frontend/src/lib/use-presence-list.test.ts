import { describe, expect, it } from "vitest"

import {
  reconcilePresenceEntries,
  type PresenceEntry,
} from "@/lib/use-presence-list"

interface Item {
  id: string
}

const getKey = (item: Item): string => item.id

function present(...ids: string[]): PresenceEntry<Item>[] {
  return ids.map((id) => ({ key: id, value: { id }, phase: "present" }))
}

describe("reconcilePresenceEntries", () => {
  it("marks inserted items as entering", () => {
    const entries = reconcilePresenceEntries(present("a"), [{ id: "a" }, { id: "b" }], getKey)

    expect(entries.map(({ key, phase }) => [key, phase])).toEqual([
      ["a", "present"],
      ["b", "entering"],
    ])
  })

  it("keeps removed items in their visual position while they exit", () => {
    const entries = reconcilePresenceEntries(
      present("a", "b", "c"),
      [{ id: "x" }, { id: "a" }, { id: "c" }],
      getKey,
    )

    expect(entries.map(({ key, phase }) => [key, phase])).toEqual([
      ["x", "entering"],
      ["a", "present"],
      ["b", "exiting"],
      ["c", "present"],
    ])
  })

  it("preserves the order of adjacent exiting items", () => {
    const entries = reconcilePresenceEntries(
      present("a", "b", "c", "d"),
      [{ id: "a" }, { id: "d" }],
      getKey,
    )

    expect(entries.map(({ key, phase }) => [key, phase])).toEqual([
      ["a", "present"],
      ["b", "exiting"],
      ["c", "exiting"],
      ["d", "present"],
    ])
  })

  it("replaces the stored value for an existing key", () => {
    const entries = reconcilePresenceEntries(
      [{ key: "a", value: { id: "old" }, phase: "present" }],
      [{ id: "a", label: "updated" }],
      (item) => item.id,
    )

    expect(entries[0].value).toEqual({ id: "a", label: "updated" })
  })

  it("restores an exiting item without replaying its entrance", () => {
    const entries = reconcilePresenceEntries(
      [{ key: "a", value: { id: "a" }, phase: "exiting" }],
      [{ id: "a" }],
      getKey,
    )

    expect(entries[0].phase).toBe("present")
  })
})
