import { describe, expect, it } from "vitest";
import { applyDelta as realApplyDelta, isSupported as realIsSupported } from "../render/index.js";
import { createDisplayClient, displayPath } from "./dlClient.js";

const full = (version, ids) => ({ dl: 1, version, entries: ids.map((id) => ({ id, v: version })) });
const applyDelta = (dl, delta) => {
  if (delta.full) return { ...delta, entries: delta.entries };
  const removes = new Set(delta.removes || []);
  const up = new Map((delta.upserts || []).map((e) => [e.id, e]));
  const entries = dl.entries.filter((e) => !removes.has(e.id) && !up.has(e.id)).concat([...up.values()]);
  return { ...dl, version: delta.version, entries };
};
const isSupported = (dl) => dl && dl.dl === 1;

function fakeServer(answers) {
  const calls = [];
  const pending = [];
  const fetchJSON = (path) => {
    calls.push(path);
    return new Promise((resolve, reject) => pending.push({ path, resolve, reject }));
  };
  const answer = async (value) => {
    const next = pending.shift();
    if (value instanceof Error) next.reject(value);
    else next.resolve(typeof value === "function" ? value(next.path) : value);
    await new Promise((r) => setTimeout(r, 0));
  };
  return { calls, fetchJSON, answer, pending, answers };
}

describe("display client", () => {
  // The renderer's own isSupported wants `entries`, which a delta never has (E9 caught this).
  it("accepts a delta under the renderer's real isSupported", async () => {
    const s = fakeServer();
    const c = createDisplayClient({ team: "t", fetchJSON: s.fetchJSON, applyDelta: realApplyDelta, isSupported: realIsSupported });
    const seen = [];
    c.subscribe((dl) => seen.push([dl.version, realIsSupported(dl), dl.entries.map((e) => e.id).join(",")]));
    c.load();
    await s.answer(full(3, ["E-1", "E-2"]));
    c.sync();
    await s.answer({ dl: 1, version: 4, since: 3, full: false, bbox: [0, 0, 1, 1], upserts: [{ id: "E-3", v: 4, layer: "marks", z: 3 }], removes: ["E-1"] });
    expect(seen).toEqual([[3, true, "E-1,E-2"], [4, true, "E-2,E-3"]]);
    expect(s.calls).toEqual(["/api/teams/t/display", "/api/teams/t/display?since=3"]);
  });

  it("paths", () => {
    expect(displayPath("a b")).toBe("/api/teams/a%20b/display");
    expect(displayPath("t", 12)).toBe("/api/teams/t/display?since=12");
  });

  it("loads the whole list, then asks for deltas since what it holds", async () => {
    const s = fakeServer();
    const c = createDisplayClient({ team: "t", fetchJSON: s.fetchJSON, applyDelta, isSupported });
    const seen = [];
    c.subscribe((dl) => seen.push(dl.version));
    c.load();
    await s.answer(full(5, ["E-1", "E-2"]));
    expect(c.version()).toBe(5);
    c.sync();
    expect(s.calls).toEqual(["/api/teams/t/display", "/api/teams/t/display?since=5"]);
    await s.answer({ dl: 1, full: false, since: 5, version: 7, upserts: [{ id: "E-3", v: 7 }], removes: ["E-1"] });
    expect(c.current().entries.map((e) => e.id).sort()).toEqual(["E-2", "E-3"]);
    expect(seen).toEqual([5, 7]);
  });

  it("keeps one request in flight and coalesces the rest into one", async () => {
    const s = fakeServer();
    const c = createDisplayClient({ team: "t", fetchJSON: s.fetchJSON, applyDelta, isSupported });
    c.load();
    await s.answer(full(1, ["E-1"]));
    c.sync();
    c.sync();
    c.sync();
    expect(s.calls.length).toBe(2);
    await s.answer({ dl: 1, full: false, since: 1, version: 2, upserts: [], removes: [] });
    expect(s.calls).toEqual(["/api/teams/t/display", "/api/teams/t/display?since=1", "/api/teams/t/display?since=2"]);
    await s.answer({ dl: 1, full: false, since: 2, version: 3, upserts: [], removes: [] });
    expect(s.calls.length).toBe(3);
    expect(c.version()).toBe(3);
  });

  it("takes a full answer to a delta request, and refetches whole after a mismatched delta", async () => {
    const s = fakeServer();
    const c = createDisplayClient({ team: "t", fetchJSON: s.fetchJSON, applyDelta, isSupported });
    c.load();
    await s.answer(full(4, ["E-1"]));
    c.sync();
    await s.answer({ dl: 1, full: true, version: 9, entries: [{ id: "E-9", v: 9 }] });
    expect(c.current().entries.map((e) => e.id)).toEqual(["E-9"]);
    c.sync();
    await s.answer({ dl: 1, full: false, since: 3, version: 10, upserts: [], removes: [] });
    expect(s.calls[s.calls.length - 1]).toBe("/api/teams/t/display");
    await s.answer(full(10, ["E-9", "E-10"]));
    expect(c.version()).toBe(10);
  });

  it("drops an older whole list unless it asked for one (a reset)", async () => {
    const s = fakeServer();
    const c = createDisplayClient({ team: "t", fetchJSON: s.fetchJSON, applyDelta, isSupported });
    c.load();
    await s.answer(full(8, ["E-1"]));
    c.load();
    await s.answer(full(2, ["E-5"]));
    expect(c.version()).toBe(2);
  });

  it("hands an unsupported list to the page as is and never deltas it", async () => {
    const s = fakeServer();
    const c = createDisplayClient({ team: "t", fetchJSON: s.fetchJSON, applyDelta, isSupported });
    const seen = [];
    c.subscribe((dl) => seen.push(dl));
    c.load();
    await s.answer({ dl: 2, version: 3, entries: [] });
    expect(isSupported(seen[0])).toBe(false);
    c.sync();
    expect(s.calls[1]).toBe("/api/teams/t/display");
  });

  it("reports failures and stops quietly once closed", async () => {
    const s = fakeServer();
    const errors = [];
    const c = createDisplayClient({ team: "t", fetchJSON: s.fetchJSON, applyDelta, isSupported, onError: (e) => errors.push(e.message) });
    c.load();
    await s.answer(new Error("offline"));
    expect(errors).toEqual(["offline"]);
    c.load();
    c.close();
    await s.answer(full(1, []));
    expect(c.current()).toBeNull();
  });
});
