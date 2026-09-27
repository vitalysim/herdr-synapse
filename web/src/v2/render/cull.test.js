// The index and culling (canvas-v2-phase1.md 4.2, D3): a linear scan, fast enough for 2,000 entries.
import { describe, expect, test } from "vitest";
import { contains, createIndex, cull, entryOf, grow } from "./cull.js";

function synthetic(n) {
  const entries = [];
  for (let i = 0; i < n; i += 1) {
    const x = (i % 50) * 200;
    const y = Math.floor(i / 50) * 120;
    entries.push({ id: `E-${i + 1}`, kind: "box", layer: "marks", z: i, v: 1, bbox: [x, y, x + 160, y + 80], hit: { shape: "rect", box: [x, y, 160, 80] }, items: [] });
  }
  return { dl: 1, version: 1, bbox: [-40, -40, 10000, 4840], entries };
}

describe("index", () => {
  test("entries by id, render order kept", () => {
    const index = createIndex(synthetic(5));
    expect(index.entries.map((e) => e.id)).toEqual(["E-1", "E-2", "E-3", "E-4", "E-5"]);
    expect(entryOf(index, "E-3").bbox).toEqual([400, 0, 560, 80]);
    expect(entryOf(index, "E-99")).toBeNull();
    expect(entryOf(null, "E-1")).toBeNull();
  });

  test("a null list is an empty index", () => {
    expect(createIndex(null).entries).toEqual([]);
    expect(cull(createIndex(null), [0, 0, 1, 1])).toEqual([]);
  });
});

describe("cull", () => {
  test("keeps what meets the rect, the always set, and entries without bounds", () => {
    const dl = synthetic(10);
    dl.entries.push({ id: "K-1", layer: "overlays", items: [] });
    const index = createIndex(dl);
    const ids = cull(index, [0, 0, 350, 50]).map((e) => e.id);
    expect(ids).toEqual(["E-1", "E-2", "K-1"]);
    expect(cull(index, [0, 0, 350, 50], new Set(["E-9"])).map((e) => e.id)).toEqual(["E-1", "E-2", "E-9", "K-1"]);
    // A hit box stands in for a missing bbox.
    const onlyHit = createIndex({ entries: [{ id: "E-1", hit: { box: [1000, 1000, 10, 10] } }] });
    expect(cull(onlyHit, [0, 0, 100, 100])).toEqual([]);
  });

  test("2,000 entries: under 2 ms per query", () => {
    const index = createIndex(synthetic(2000));
    const rects = Array.from({ length: 200 }, (_, i) => [i * 40, i * 20, i * 40 + 1400, i * 20 + 900]);
    for (const r of rects.slice(0, 20)) cull(index, r); // warm up
    const t0 = performance.now();
    let seen = 0;
    for (const r of rects) seen += cull(index, r).length;
    const per = (performance.now() - t0) / rects.length;
    expect(seen).toBeGreaterThan(0);
    expect(per).toBeLessThan(2);
  });
});

describe("rect helpers", () => {
  test("grow by one screen on every side, contains", () => {
    expect(grow([0, 0, 100, 50], 1)).toEqual([-100, -50, 200, 100]);
    expect(contains([-100, -50, 200, 100], [0, 0, 100, 50])).toBe(true);
    expect(contains([0, 0, 100, 50], [-1, 0, 100, 50])).toBe(false);
    expect(contains(null, [0, 0, 1, 1])).toBe(false);
  });
});
