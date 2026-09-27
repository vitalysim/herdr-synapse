import { describe, expect, test } from "vitest";
import { MAX_SCALE, MIN_SCALE, camera } from "./camera.js";

const close = (a, b) => a.forEach((v, i) => expect(v).toBeCloseTo(b[i], 9));

describe("camera", () => {
  test("create has defaults and clamps", () => {
    expect(camera.create()).toEqual({ x: 0, y: 0, scale: 1 });
    expect(camera.create({ scale: 100 }).scale).toBe(MAX_SCALE);
    expect(camera.create({ scale: 0.001 }).scale).toBe(MIN_SCALE);
  });

  test("toScreen and toWorld round trip", () => {
    for (const cam of [camera.create(), { x: -120.5, y: 40, scale: 0.37 }, { x: 1e4, y: -3e3, scale: 7.9 }]) {
      for (const pt of [[0, 0], [123.4, -56.7], [-1e3, 2e3]]) close(camera.toWorld(cam, camera.toScreen(cam, pt)), pt);
    }
    expect(camera.toScreen({ x: 10, y: 20, scale: 2 }, [15, 25])).toEqual([10, 10]);
  });

  test("zoomAt keeps the point under the cursor fixed", () => {
    const cam = { x: 37, y: -12, scale: 1.3 };
    const cursor = [412, 233];
    const before = camera.toWorld(cam, cursor);
    for (const f of [1.1, 0.5, 3, 1 / 7]) {
      const next = camera.zoomAt(cam, f, cursor);
      close(camera.toWorld(next, cursor), before);
    }
  });

  test("zoomAt clamps to 0.05..8 and honours custom limits", () => {
    expect(camera.zoomAt({ x: 0, y: 0, scale: 7 }, 10, [0, 0]).scale).toBe(8);
    expect(camera.zoomAt({ x: 0, y: 0, scale: 0.06 }, 0.01, [0, 0]).scale).toBe(0.05);
    expect(camera.zoomAt({ x: 0, y: 0, scale: 1 }, 10, [0, 0], { max: 2 }).scale).toBe(2);
    // At the limit nothing moves.
    expect(camera.zoomAt({ x: 5, y: 6, scale: 8 }, 2, [100, 100])).toEqual({ x: 5, y: 6, scale: 8 });
  });

  test("panBy moves the picture with the pointer", () => {
    const cam = { x: 100, y: 100, scale: 2 };
    const next = camera.panBy(cam, 40, -20);
    expect(next).toEqual({ x: 80, y: 110, scale: 2 });
    // The world point that was under screen (0,0) is now under (40,-20).
    close(camera.toScreen(next, camera.toWorld(cam, [0, 0])), [40, -20]);
  });

  test("fit centres the rect with padding and caps the scale", () => {
    const vp = { w: 1000, h: 600 };
    const cam = camera.fit([0, 0, 904, 252], vp, { padPx: 48 });
    expect(cam.scale).toBeCloseTo(Math.min((1000 - 96) / 904, (600 - 96) / 252, 1.5), 9);
    const [x0, y0, x1, y1] = camera.viewRect(cam, vp);
    expect((x0 + x1) / 2).toBeCloseTo(452, 9);
    expect((y0 + y1) / 2).toBeCloseTo(126, 9);
    expect(camera.fit([0, 0, 10, 10], vp).scale).toBe(1.5);
    expect(camera.fit([0, 0, 1e6, 1e6], vp).scale).toBe(MIN_SCALE);
    // Degenerate input gives a usable camera.
    expect(camera.fit(null, vp)).toEqual({ x: 0, y: 0, scale: 1 });
    expect(Number.isFinite(camera.fit([5, 5, 5, 5], { w: 0, h: 0 }).scale)).toBe(true);
  });

  test("viewRect and matrix", () => {
    expect(camera.viewRect({ x: 10, y: 20, scale: 2 }, { w: 200, h: 100 })).toEqual([10, 20, 110, 70]);
    expect(camera.matrix({ x: 10, y: 20, scale: 2 })).toBe("matrix(2 0 0 2 -20 -40)");
  });
});
