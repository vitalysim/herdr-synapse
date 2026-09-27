import { describe, expect, test } from "vitest";
import { applyDelta, isSupported } from "./delta.js";
import sample from "./__fixtures__/sample.json";

const clone = (v) => JSON.parse(JSON.stringify(v));

describe("isSupported", () => {
  test("dl range", () => {
    expect(isSupported(sample)).toBe(true);
    expect(isSupported({ ...sample, dl: 2 })).toBe(false);
    expect(isSupported({ ...sample, dl: "1" })).toBe(false);
    expect(isSupported(null)).toBe(false);
    expect(isSupported({ dl: 1 })).toBe(false);
  });
});

describe("applyDelta", () => {
  test("upserts in place keep untouched entries by identity", () => {
    const moved = { ...clone(sample.entries[2]), v: 4, bbox: [420, 160, 580, 240] };
    const next = applyDelta(sample, { dl: 1, version: 4, since: 3, full: false, bbox: [60, 60, 640, 300], upserts: [moved], removes: [] });
    expect(next).not.toBe(sample);
    expect(next.version).toBe(4);
    expect(next.bbox).toEqual([60, 60, 640, 300]);
    expect(next.entries.map((e) => e.id)).toEqual(["E-1", "E-2", "E-3", "E-4"]);
    expect(next.entries[0]).toBe(sample.entries[0]);
    expect(next.entries[1]).toBe(sample.entries[1]);
    expect(next.entries[2]).toBe(moved);
    expect(next.entries[3]).toBe(sample.entries[3]);
    expect(next.palettes).toBe(sample.palettes);
  });

  test("an upsert equal to what the page holds keeps the old object", () => {
    const same = clone(sample.entries[1]);
    const next = applyDelta(sample, { version: 4, upserts: [same], removes: [] });
    expect(next.entries[1]).toBe(sample.entries[1]);
  });

  test("removes, and new entries land in render order", () => {
    const added = { ...clone(sample.entries[1]), id: "E-9", z: 3, v: 5 };
    const raised = { ...clone(sample.entries[1]), z: 9, v: 5 };
    const frame = { ...clone(sample.entries[0]), id: "E-7", z: 0, v: 5 };
    const next = applyDelta(sample, { version: 5, upserts: [added, raised, frame], removes: ["E-4"] });
    expect(next.entries.map((e) => e.id)).toEqual(["E-7", "E-1", "E-3", "E-9", "E-2"]);
    expect(next.entries[1]).toBe(sample.entries[0]);
    expect(next.entries[2]).toBe(sample.entries[2]);
  });

  test("id number orders ties, not the string (E-10 after E-9)", () => {
    const a = { ...clone(sample.entries[1]), id: "E-10", z: 2 };
    const b = { ...clone(sample.entries[1]), id: "E-9", z: 2 };
    const next = applyDelta({ ...sample, entries: [] }, { version: 6, upserts: [a, b] });
    expect(next.entries.map((e) => e.id)).toEqual(["E-9", "E-10"]);
  });

  test("full replaces everything but reuses equal entries", () => {
    const entries = clone(sample.entries);
    entries[2].locked = true; // a lock changed: same v, different entry
    const next = applyDelta(sample, { dl: 1, version: 7, since: 3, full: true, bbox: sample.bbox, palettes: sample.palettes, entries });
    expect(next.full).toBeUndefined();
    expect(next.since).toBeUndefined();
    expect(next.version).toBe(7);
    expect(next.entries[0]).toBe(sample.entries[0]);
    expect(next.entries[2]).not.toBe(sample.entries[2]);
    expect(next.entries[2].locked).toBe(true);
  });

  test("no delta, or no list yet", () => {
    expect(applyDelta(sample, null)).toBe(sample);
    const first = applyDelta(null, { ...clone(sample), full: true });
    expect(first.entries).toHaveLength(4);
  });
});
