import { describe, expect, it } from "vitest";
import { combinePreviews, createPendingPreviews } from "./preview.js";

describe("previews", () => {
  it("merge: the last move wins, boxes merge, hide and ghost add up", () => {
    const out = combinePreviews([
      { move: { ids: ["E-1"], by: [1, 1] }, hide: ["E-9"] },
      null,
      { move: { ids: ["E-2"], by: [2, 2] }, boxes: { "E-3": [0, 0, 1, 1] }, ghost: [{ k: "rect" }] },
      { boxes: { "E-4": [1, 1, 2, 2] }, hide: ["E-8"] },
    ]);
    expect(out).toEqual({ move: { ids: ["E-2"], by: [2, 2] }, boxes: { "E-3": [0, 0, 1, 1], "E-4": [1, 1, 2, 2] }, hide: ["E-9", "E-8"], ghost: [{ k: "rect" }] });
    expect(combinePreviews([null, {}, { hide: [] }])).toBeNull();
  });
  it("pending: kept until the list reaches the version, dropped on refusal, timed out", () => {
    let seen = [];
    let t = 0;
    const p = createPendingPreviews({ onChange: (list) => { seen = list; }, timeoutMs: 100, now: () => t });
    const a = p.add({ hide: ["E-1"] });
    const b = p.add({ hide: ["E-2"] });
    const c = p.add({ hide: ["E-3"] });
    p.settle(a, 12, 10);
    p.drop(b);
    expect(seen.length).toBe(2);
    p.reached(11);
    expect(seen.length).toBe(2);
    p.reached(12);
    expect(seen).toEqual([{ hide: ["E-3"] }]);
    t = 500;
    p.reached(12);
    expect(seen).toEqual([]);
    const d = p.add({ hide: ["E-4"] });
    p.settle(d, 5, 9); // the list is already past it
    expect(seen).toEqual([]);
    expect(p.add(null)).toBeNull();
    void c;
  });
});
