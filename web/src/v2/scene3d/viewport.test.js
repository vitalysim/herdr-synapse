// The shared renderer's viewport and scissor math (canvas-v2-phase3-4.md 3.7, 8.3): rectangles,
// device pixel ratio, the y flip, clipping to the Surface, and the LOD cut-offs.
import { describe, expect, test } from "vitest";
import { MAX_PIXELS, MAX_SCENES, MIN_SIDE_PX, moved, pixelRatio, planFrame, stillSize, viewportFor } from "./viewport.js";

const HOST = { left: 100, top: 50, width: 1000, height: 600 };
const rect = (left, top, width, height) => ({ left: HOST.left + left, top: HOST.top + top, width, height });

describe("viewportFor", () => {
  test("y flips: a rectangle at the host's top has its viewport at the canvas's top", () => {
    const vp = viewportFor(rect(10, 0, 200, 100), HOST);
    expect(vp.viewport).toEqual([10, 500, 200, 100]);
    expect(vp.scissor).toEqual([10, 500, 200, 100]);
    expect(vp.visible).toBe(true);
    expect(viewportFor(rect(10, 500, 200, 100), HOST).viewport).toEqual([10, 0, 200, 100]);
  });

  test("a rectangle partly off the host keeps its whole viewport and clips its scissor", () => {
    const vp = viewportFor(rect(-50, -30, 200, 100), HOST);
    expect(vp.viewport).toEqual([-50, 530, 200, 100]);
    expect(vp.scissor).toEqual([0, 530, 150, 70]);
    expect(vp.visible).toBe(true);
  });

  test("off the host, or under 48 px on the shorter side, a scene is not drawn", () => {
    expect(viewportFor(rect(1200, 0, 200, 100), HOST)).toMatchObject({ visible: false, reason: "off" });
    expect(viewportFor(rect(0, -200, 200, 100), HOST)).toMatchObject({ visible: false, reason: "off" });
    expect(viewportFor(rect(0, 0, 400, MIN_SIDE_PX - 1), HOST)).toMatchObject({ visible: false, reason: "small" });
    expect(viewportFor(rect(0, 0, 400, MIN_SIDE_PX), HOST).visible).toBe(true);
  });

  test("device pixels count the ratio, capped at 2", () => {
    expect(pixelRatio(3)).toBe(2);
    expect(pixelRatio(0)).toBe(1);
    expect(pixelRatio(1.5)).toBe(1.5);
    expect(viewportFor(rect(0, 0, 100, 100), HOST, 2).pixels).toBe(40000);
    expect(viewportFor(rect(0, 0, 100, 100), HOST, 4).pixels).toBe(40000);
  });
});

describe("planFrame", () => {
  test("at most 16 scenes a frame, in the order given", () => {
    const rects = Array.from({ length: 20 }, (_, i) => ({ key: `s${i}`, rect: rect((i % 5) * 190, Math.floor(i / 5) * 140, 180, 130) }));
    const plan = planFrame(rects, HOST, 1);
    expect(plan.filter((p) => p.draw).map((p) => p.key)).toEqual(rects.slice(0, MAX_SCENES).map((r) => r.key));
    expect(plan.slice(MAX_SCENES).every((p) => p.reason === "scenes")).toBe(true);
  });

  test("at most 4 megapixels scissored a frame", () => {
    const big = { width: 3000, height: 3000, left: 0, top: 0 };
    const rects = [{ key: "a", rect: { left: 0, top: 0, width: 1400, height: 1000 } }, { key: "b", rect: { left: 0, top: 1000, width: 1400, height: 1000 } }, { key: "c", rect: { left: 0, top: 2000, width: 1400, height: 1000 } }];
    const plan = planFrame(rects, big, 1);
    expect(plan.map((p) => p.draw)).toEqual([true, true, false]);
    expect(plan[2].reason).toBe("pixels");
    expect(1400 * 1000 * 3).toBeGreaterThan(MAX_PIXELS);
  });

  test("hidden and small scenes do not use the budget", () => {
    const plan = planFrame([{ key: "off", rect: rect(5000, 0, 100, 100) }, { key: "tiny", rect: rect(0, 0, 20, 20) }, { key: "ok", rect: rect(0, 0, 100, 100) }], HOST);
    expect(plan.map((p) => [p.key, p.draw, p.reason])).toEqual([["off", false, "off"], ["tiny", false, "small"], ["ok", true, null]]);
  });
});

describe("helpers", () => {
  test("moved ignores sub-pixel jitter", () => {
    expect(moved({ left: 0, top: 0, width: 10, height: 10 }, { left: 0.05, top: 0, width: 10, height: 10 })).toBe(false);
    expect(moved({ left: 0, top: 0, width: 10, height: 10 }, { left: 1, top: 0, width: 10, height: 10 })).toBe(true);
    expect(moved(null, { left: 0, top: 0, width: 1, height: 1 })).toBe(true);
    expect(moved(null, null)).toBe(false);
  });

  test("a still is the slot box ×2, its longer side 1024 px at most", () => {
    expect(stillSize(320, 200)).toEqual([640, 400]);
    expect(stillSize(640, 420)).toEqual([1024, 672]);
    expect(stillSize(200, 1000)).toEqual([205, 1024]);
  });
});
