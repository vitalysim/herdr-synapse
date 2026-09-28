// Stills (canvas-v2-phase3-4.md 3.7): the queue (once per key, paused during gestures) and the
// readback's pixel work (rows flipped, linear light encoded to sRGB as the canvas output would be).
import { describe, expect, test } from "vitest";
import { createStillQueue, encodeSRGB, flipRows, srgbByte } from "./stills.js";

describe("stills", () => {
  test("a key is queued once for the page's life; forget lets a failed one try again", () => {
    const q = createStillQueue();
    expect(q.add("E-1@3@iso", { view: "iso" })).toBe(true);
    expect(q.add("E-1@3@iso", { view: "iso" })).toBe(false);
    q.add("E-1@3@top", { view: "top" });
    q.pause(true);
    expect(q.next()).toBeNull();
    q.pause(false);
    expect(q.next().key).toBe("E-1@3@iso");
    q.forget("E-1@3@iso");
    expect(q.add("E-1@3@iso", { view: "iso" })).toBe(true);
    q.drop("E-1@");
    expect(q.size).toBe(0);
  });

  test("sRGB encoding: 0 and 1 stay, mid grey is 188, bytes and floats agree", () => {
    expect(srgbByte(0)).toBe(0);
    expect(srgbByte(1)).toBe(255);
    expect(srgbByte(0.5)).toBe(188);
    expect(srgbByte(-1)).toBe(0);
    expect(srgbByte(2)).toBe(255);
    const floats = new Float32Array([0.5, 0.2159, 0, 0.3, 1, 1, 1, 0]);
    const bytes = new Uint8Array([128, 55, 0, 77, 255, 255, 255, 0]);
    expect([...encodeSRGB(floats, true)]).toEqual([188, 128, 0, 255, 255, 255, 255, 255]);
    const fromBytes = [...encodeSRGB(bytes, false)];
    expect(fromBytes[3]).toBe(255);
    expect(Math.abs(fromBytes[1] - 128)).toBeLessThanOrEqual(1);
  });

  test("readPixels rows are bottom-up; the PNG is top-down", () => {
    const px = new Uint8ClampedArray([1, 1, 1, 1, 2, 2, 2, 2]); // 1 × 2: bottom row first
    expect([...flipRows(px, 1, 2)]).toEqual([2, 2, 2, 2, 1, 1, 1, 1]);
  });
});
