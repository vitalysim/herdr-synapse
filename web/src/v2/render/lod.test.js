// The zoom rules (canvas-v2-phase1.md 1.3 lod, 1.6 the title above a frame).
import { describe, expect, test } from "vitest";
import { dependsOnScale, lodVisible, textLayout } from "./lod.js";

describe("lodVisible", () => {
  test("min <= scale < max, either end open", () => {
    const band = { lod: [0.75, null] };
    const above = { lod: [null, 0.75] };
    for (const [scale, b, a] of [
      [0.2, false, true],
      [0.74, false, true],
      [0.75, true, false],
      [4, true, false],
    ]) {
      expect(lodVisible(band, scale), `band at ${scale}`).toBe(b);
      expect(lodVisible(above, scale), `above at ${scale}`).toBe(a);
    }
    expect(lodVisible({}, 0.01)).toBe(true);
    expect(lodVisible({ lod: [1, 2] }, 2)).toBe(false);
  });
});

describe("textLayout", () => {
  const title = { k: "text", x: 100, size: 16, lh: 20, base_ratio: 0.95, zoom: { min_px: 12, grow: "up", bottom: 100 }, lines: [{ y: 95.1, t: "Checkout", w: 71.2 }] };

  test("a plain text keeps its size and baselines", () => {
    const lay = textLayout({ size: 20, lh: 25, lines: [{ y: 10 }, { y: 35 }] }, 0.1);
    expect(lay).toMatchObject({ size: 20, lh: 25, ys: [10, 35], zoomed: false });
  });

  test("the title above a frame keeps min_px on screen and grows up from bottom (3 scales)", () => {
    // scale 0.5: max(16, 12 / 0.5) = 24; line 30 tall; top 70; baseline 70 + 0.95 * 24.
    let lay = textLayout(title, 0.5);
    expect(lay.size).toBe(24);
    expect(lay.lh).toBeCloseTo(30, 9);
    expect(lay.top).toBeCloseTo(70, 9);
    expect(lay.ys[0]).toBeCloseTo(92.8, 9);
    // scale 0.1: 120 units, so 12 px on screen.
    lay = textLayout(title, 0.1);
    expect(lay.size * 0.1).toBeCloseTo(12, 9);
    expect(lay.top).toBeCloseTo(100 - 150, 9);
    // scale 1: the size wins (16 >= 12); bottom stays the line's bottom.
    lay = textLayout(title, 1);
    expect(lay.size).toBe(16);
    expect(lay.top).toBe(80);
    expect(lay.ys[0]).toBeCloseTo(95.2, 9);
  });
});

describe("dependsOnScale", () => {
  test("only lod, zoom, screen-px strokes and screen groups depend on the camera", () => {
    expect(dependsOnScale({ items: [{ k: "rect", x: 0, y: 0, w: 1, h: 1 }] })).toBe(false);
    expect(dependsOnScale({ items: [{ k: "rect", sw_px: 2 }] })).toBe(true);
    expect(dependsOnScale({ items: [{ k: "group", items: [{ k: "text", lod: [1, null] }] }] })).toBe(true);
    expect(dependsOnScale({ items: [{ k: "group", screen: [0, 0], items: [] }] })).toBe(true);
    expect(dependsOnScale({ items: [{ k: "slot", fallback: [{ k: "text", zoom: {} }] }] })).toBe(true);
  });
});
