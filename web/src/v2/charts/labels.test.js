// labels.js: label collisions as oriented boxes (qa.js labelOverlaps, K1 and K4).
import { describe, expect, test } from "vitest";
import { countOverlaps, quadsIntersect, rectQuad } from "./labels.js";

const rotated = (cx, cy, w, h, deg) => {
  const a = (deg * Math.PI) / 180;
  const c = Math.cos(a);
  const s = Math.sin(a);
  return {
    pts: [
      [-w / 2, -h / 2],
      [w / 2, -h / 2],
      [w / 2, h / 2],
      [-w / 2, h / 2],
    ].map(([x, y]) => [cx + x * c - y * s, cy + x * s + y * c]),
  };
};

describe("label overlaps", () => {
  test("axis-aligned boxes", () => {
    expect(quadsIntersect(rectQuad(0, 0, 10, 10).pts, rectQuad(5, 5, 10, 10).pts)).toBe(true);
    expect(quadsIntersect(rectQuad(0, 0, 10, 10).pts, rectQuad(20, 0, 10, 10).pts)).toBe(false);
    // touching boxes do not count
    expect(quadsIntersect(rectQuad(0, 0, 10, 10).pts, rectQuad(10, 0, 10, 10).pts)).toBe(false);
  });
  test("45° labels a band apart do not collide although their bounding boxes do", () => {
    const a = rotated(20, 20, 60, 12, -45);
    const b = rotated(40, 20, 60, 12, -45);
    expect(quadsIntersect(a.pts, b.pts)).toBe(false);
    const c = rotated(26, 20, 60, 12, -45);
    expect(quadsIntersect(a.pts, c.pts)).toBe(true);
  });
  test("counts pairs", () => {
    expect(countOverlaps([rectQuad(0, 0, 10, 10), rectQuad(5, 0, 10, 10), rectQuad(8, 0, 10, 10), rectQuad(100, 0, 1, 1)])).toBe(3);
  });
});
