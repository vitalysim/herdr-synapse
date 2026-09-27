import { describe, expect, it } from "vitest";
import * as O from "./ops.js";

// A small board: frame E-1 holding boxes E-2 and E-3, arrow E-4 bound E-2 -> E-3, a free box E-5
// outside, a text E-6, a half-bound arrow E-7 (E-5 -> a point), frame E-8 (empty), comment C-1,
// claim K-1.
const entries = [
  { id: "E-1", kind: "frame", layer: "zones", v: 3, hit: { shape: "frame", box: [100, 100, 480, 260], band: 40 }, handles: "box", frame: null, edit: { value: "Checkout" } },
  { id: "E-8", kind: "frame", layer: "zones", v: 5, hit: { shape: "frame", box: [700, 100, 300, 300], band: 40 }, handles: "box", frame: null, edit: { value: "Later" } },
  { id: "E-2", kind: "box", layer: "marks", v: 4, hit: { shape: "rect", box: [120, 160, 160, 80] }, handles: "box", connect: true, frame: "E-1", edit: { value: "API" } },
  { id: "E-3", kind: "box", layer: "marks", v: 4, hit: { shape: "rect", box: [400, 160, 160, 80] }, handles: "box", connect: true, frame: "E-1", edit: { value: "DB" } },
  { id: "E-4", kind: "arrow", layer: "marks", v: 6, hit: { shape: "line", points: [[280, 200], [400, 200]] }, handles: "ends", frame: "E-1", edit: { value: "" } },
  { id: "E-5", kind: "box", layer: "marks", v: 7, hit: { shape: "rect", box: [120, 500, 160, 80] }, handles: "box", connect: true, frame: null, edit: { value: "Free" } },
  { id: "E-6", kind: "text", layer: "marks", v: 8, hit: { shape: "rect", box: [600, 500, 200, 25] }, handles: "width", connect: true, frame: null, edit: { value: "Note" } },
  { id: "E-7", kind: "arrow", layer: "marks", v: 9, hit: { shape: "line", points: [[200, 580], [200, 640]] }, handles: "ends", frame: null, edit: { value: "" } },
  { id: "C-1", kind: "comment", layer: "overlays", v: 10, hit: { shape: "pin", x: 50, y: 50, r_px: 10 }, handles: "none", frame: null, edit: null },
  { id: "K-1", kind: "claim", layer: "overlays", v: 0, hit: { shape: "none" }, handles: "none", frame: null, edit: null },
];
const elements = {
  "E-1": { id: "E-1", type: "frame", x: 100, y: 100, w: 480, h: 260, frame: null, updated_seq: 3 },
  "E-8": { id: "E-8", type: "frame", x: 700, y: 100, w: 300, h: 300, frame: null, updated_seq: 5 },
  "E-2": { id: "E-2", type: "box", x: 120, y: 160, w: 160, h: 80, frame: "E-1", updated_seq: 4 },
  "E-3": { id: "E-3", type: "box", x: 400, y: 160, w: 160, h: 80, frame: "E-1", updated_seq: 4 },
  "E-4": { id: "E-4", type: "arrow", x: 280, y: 200, w: 120, h: 0, from: "E-2", to: "E-3", points: [[280, 200], [400, 200]], frame: "E-1", updated_seq: 6 },
  "E-5": { id: "E-5", type: "box", x: 120, y: 500, w: 160, h: 80, frame: null, updated_seq: 7 },
  "E-6": { id: "E-6", type: "text", x: 600, y: 500, w: 200, h: 25, frame: null, updated_seq: 8 },
  "E-7": { id: "E-7", type: "arrow", x: 200, y: 580, w: 0, h: 60, from: "E-5", to: null, points: [[200, 580, 0.5], [200, 640, 0.5]], frame: null, updated_seq: 9 },
  "C-1": { id: "C-1", type: "comment", x: 50, y: 50, w: 0, h: 0, point: [50, 50], updated_seq: 10 },
};
const dl = { dl: 1, version: 10, entries };
const m = O.createModel(dl, (id) => elements[id] || null);
const bare = O.createModel(dl); // no store: the display list alone

describe("own versions (QA 1 finding 6)", () => {
  const dl = { dl: 1, version: 5, entries: [{ id: "E-5", v: 5, hit: { shape: "rect", box: [0, 0, 10, 10] } }] };
  it("a newer version from this page's own op wins until the list catches up", () => {
    const own = new Map([["E-5", 9]]);
    const m = O.createModel(dl, () => null, { own: (id) => own.get(id) ?? null });
    expect(O.versionOf(m, "E-5")).toBe(9);
    own.set("E-5", 3); // the list is ahead (someone else changed it since): the list's wins
    expect(O.versionOf(m, "E-5")).toBe(5);
    expect(O.versionOf(O.createModel(dl), "E-5")).toBe(5);
  });
  it("appliedVersions: exact only for one applied op", () => {
    expect(O.appliedVersions({ version: 9, applied: [{ index: 0, op: "move", ids: ["E-5", "E-6"] }] })).toEqual({ "E-5": 9, "E-6": 9 });
    expect(O.appliedVersions({ version: 9, applied: [{ ids: ["E-5"] }, { ids: ["E-6"] }] })).toEqual({});
    expect(O.appliedVersions({ version: 9, applied: [] })).toEqual({});
    expect(O.appliedVersions(null)).toEqual({});
  });
});

describe("if_version", () => {
  it("is the single target's version", () => {
    expect(O.ifVersionOf(m, ["E-5"])).toBe(7);
  });
  it("is shared only when every target has the same one", () => {
    expect(O.ifVersionOf(m, ["E-2", "E-3"])).toBe(4);
    expect(O.ifVersionOf(m, ["E-2", "E-5"])).toBeNull();
  });
  it("is on every op that touches one element", () => {
    const touching = [
      O.buildMove(m, ["E-5"], [20, 0]),
      O.buildNudge(m, ["E-5"], "ArrowLeft"),
      O.buildResize(m, "E-5", [120, 500, 200, 80]),
      O.buildRebind(m, "E-7", "end", { id: "E-6" }),
      O.buildRebind(m, "E-7", "end", { point: [300, 700] }),
      O.buildEdit(m, "E-5", "Freed"),
      O.buildDelete(m, ["E-5"]),
      O.buildRestyle(m, ["E-5"], { tone: "info" }),
    ];
    for (const op of touching) expect(Number.isInteger(op.if_version)).toBe(true);
    expect(touching.map((op) => op.if_version)).toEqual([7, 7, 7, 9, 9, 7, 7, 7]);
  });
});

describe("drag (move)", () => {
  it("sends one move with by", () => {
    expect(O.buildMove(m, ["E-5"], [40, 20])).toEqual({ op: "move", ids: ["E-5"], by: [40, 20], if_version: 7 });
  });
  it("snaps the first root's corner to the grid, or not with Alt", () => {
    expect(O.snapDelta(m, ["E-5"], [13, 27])).toEqual([20, 20]);
    expect(O.snapDelta(m, ["E-5"], [13, 27], { free: true })).toEqual([13, 27]);
    const offGrid = O.createModel({ entries: [{ id: "E-9", kind: "box", hit: { shape: "rect", box: [105, 3, 40, 40] } }] });
    expect(O.snapDelta(offGrid, ["E-9"], [10, 10])).toEqual([15, 17]);
  });
  it("leaves out children whose frame moves, and arrows bound at both ends", () => {
    const op = O.buildMove(m, ["E-1", "E-2", "E-4"], [20, 20]);
    expect(op.ids).toEqual(["E-1"]);
    expect(O.moveRoots(m, ["E-4"])).toEqual([]);
    expect(O.buildMove(m, ["E-4"], [20, 20])).toBeNull();
    expect(O.moveRoots(m, ["E-7", "K-1"])).toEqual(["E-7"]);
  });
  it("previews the children and the arrows that follow", () => {
    expect(new Set(O.movingIds(m, ["E-1"]))).toEqual(new Set(["E-1", "E-2", "E-3", "E-4"]));
    expect(new Set(O.movingIds(m, ["E-2", "E-3"]))).toEqual(new Set(["E-2", "E-3", "E-4"]));
    expect(O.movingIds(m, ["E-2"])).toEqual(["E-2"]);
  });
  it("adopts a single element dropped wholly inside another frame", () => {
    expect(O.buildMove(m, ["E-5"], [600, -360])).toMatchObject({ ids: ["E-5"], by: [600, -360], frame: "E-8" });
  });
  it("releases a single element dropped wholly outside its frame", () => {
    expect(O.buildMove(m, ["E-2"], [0, 400])).toMatchObject({ ids: ["E-2"], frame: null });
  });
  it("keeps the frame while the element still touches it", () => {
    expect("frame" in O.buildMove(m, ["E-2"], [0, 120])).toBe(false);
  });
  it("adopts into the innermost frame and never into itself", () => {
    const nested = O.createModel({
      entries: [
        { id: "E-1", kind: "frame", hit: { box: [0, 0, 1000, 1000] }, frame: null },
        { id: "E-2", kind: "frame", hit: { box: [100, 100, 300, 300] }, frame: "E-1" },
        { id: "E-3", kind: "box", hit: { box: [600, 600, 40, 40] }, frame: "E-1" },
      ],
    });
    expect(O.buildMove(nested, ["E-3"], [-440, -440])).toMatchObject({ frame: "E-2" });
    expect(O.dropFrame(nested, "E-2", [110, 110, 300, 300])).toBeUndefined();
  });
  it("does not move a claim, a lock or nothing", () => {
    expect(O.buildMove(m, ["K-1"], [20, 0])).toBeNull();
    expect(O.buildMove(m, ["E-5"], [0, 0])).toBeNull();
  });
  it("carries if_version only when the roots agree", () => {
    expect(O.buildMove(m, ["E-2", "E-3"], [20, 0]).if_version).toBe(4);
    expect("if_version" in O.buildMove(m, ["E-2", "E-5"], [20, 0])).toBe(false);
  });
  it("works from the display list alone", () => {
    expect(O.buildMove(bare, ["E-5"], [20, 0])).toEqual({ op: "move", ids: ["E-5"], by: [20, 0], if_version: 7 });
  });
});

describe("nudge", () => {
  it("moves by 1, or a grid step with Shift", () => {
    expect(O.buildNudge(m, ["E-5"], "ArrowRight").by).toEqual([1, 0]);
    expect(O.buildNudge(m, ["E-5"], "ArrowUp", true).by).toEqual([0, -20]);
    expect(O.buildNudge(m, ["E-5"], "KeyA")).toBeNull();
    expect(O.buildNudge(m, [], "ArrowUp")).toBeNull();
  });
});

describe("resize", () => {
  const box = [120, 500, 160, 80];
  it("moves the named edges", () => {
    expect(O.resizeBox(box, "e", [40, 999])).toEqual([120, 500, 200, 80]);
    expect(O.resizeBox(box, "s", [999, 20])).toEqual([120, 500, 160, 100]);
    expect(O.resizeBox(box, "nw", [-20, -10])).toEqual([100, 490, 180, 90]);
    expect(O.resizeBox(box, "w", [500, 0])).toEqual([270, 500, 10, 80]);
  });
  it("keeps the aspect on a corner when asked", () => {
    const [, , w, h] = O.resizeBox(box, "se", [160, 0], { keepAspect: true });
    expect(w / h).toBeCloseTo(2);
  });
  it("sends w and h, plus to when the top or left edge moved", () => {
    expect(O.buildResize(m, "E-5", O.resizeBox(box, "e", [40, 0]))).toEqual({ op: "move", id: "E-5", w: 200, if_version: 7 });
    expect(O.buildResize(m, "E-5", O.resizeBox(box, "nw", [-20, -20]))).toEqual({ op: "move", id: "E-5", w: 180, h: 100, to: [100, 480], if_version: 7 });
    for (const handle of ["n", "ne", "e", "se", "s", "sw", "w", "nw"]) {
      const op = O.buildResize(m, "E-5", O.resizeBox(box, handle, [20, 20]));
      expect(op.op).toBe("move");
      expect(op.id).toBe("E-5");
      expect(("to" in op)).toBe(handle.includes("n") || handle.includes("w"));
    }
  });
  it("sends only w for a width entry", () => {
    expect(O.buildResize(m, "E-6", [580, 500, 220, 60])).toEqual({ op: "move", id: "E-6", w: 220, to: [580, 500], if_version: 8 });
    expect(O.buildResize(m, "E-6", [600, 500, 240, 25])).toEqual({ op: "move", id: "E-6", w: 240, if_version: 8 });
  });
  it("sends nothing for no change", () => {
    expect(O.buildResize(m, "E-5", box)).toBeNull();
  });
});

describe("arrow ends", () => {
  it("rebinds an end dropped on an entry", () => {
    expect(O.buildRebind(m, "E-7", "end", { id: "E-6", point: [0, 0] })).toEqual({ op: "move", id: "E-7", to_element: "E-6", if_version: 9 });
    expect(O.buildRebind(m, "E-7", "start", { id: "E-6" })).toEqual({ op: "move", id: "E-7", from: "E-6", if_version: 9 });
  });
  it("frees an end dropped on empty canvas, with the points", () => {
    expect(O.buildRebind(m, "E-7", "end", { id: null, point: [300.123, 700] })).toEqual({
      op: "move", id: "E-7", to_element: null, points: [[200, 580], [300.12, 700]], if_version: 9,
    });
    expect(O.buildRebind(m, "E-4", "start", { point: [250, 250] }).points).toEqual([[250, 250], [400, 200]]);
  });
  it("does nothing when the end stays on what it binds", () => {
    expect(O.buildRebind(m, "E-7", "start", { id: "E-5" })).toBeNull();
    expect(O.buildRebind(m, "E-7", "end", { id: "E-7" })).toBeNull();
    expect(O.buildRebind(m, "E-7", "middle", { id: "E-6" })).toBeNull();
  });
});

describe("connect", () => {
  it("binds both ends", () => {
    expect(O.buildConnect({ id: "E-2" }, { id: "E-5" })).toEqual({ op: "arrow", from: "E-2", to: "E-5" });
  });
  it("frees an end on empty canvas", () => {
    expect(O.buildConnect({ id: "E-2", point: [1, 1] }, { id: null, point: [300.5, 400] })).toEqual({ op: "arrow", from: "E-2", to: "300.5,400" });
    expect(O.buildConnect({ point: [0, 0] }, { id: "E-2" })).toEqual({ op: "arrow", from: "0,0", to: "E-2" });
  });
  it("uses points for two free ends and ignores a click", () => {
    expect(O.buildConnect({ point: [0, 0] }, { point: [100, 0] })).toEqual({ op: "arrow", points: [[0, 0], [100, 0]] });
    expect(O.buildConnect({ point: [0, 0] }, { point: [2, 2] })).toBeNull();
    expect(O.buildConnect({ id: "E-2" }, { id: "E-2" })).toBeNull();
  });
});

describe("create", () => {
  it("drags a box of the kind", () => {
    expect(O.buildShape("box", { rect: [300, 200, 100, 100] })).toEqual({ op: "shape", kind: "box", at: [100, 100], w: 200, h: 100, text: "" });
  });
  it("clicks the default size at the point", () => {
    expect(O.buildShape("note", { point: [10.4, 20.6] })).toEqual({ op: "shape", kind: "note", at: [10, 21], text: "" });
    expect(O.buildShape("ellipse", { rect: [0, 0, 3, 3] })).toEqual({ op: "shape", kind: "ellipse", at: [0, 0], text: "" });
  });
  it("makes only the shape tools' kinds", () => {
    for (const kind of ["box", "ellipse", "diamond", "note"]) expect(O.buildShape(kind, { point: [0, 0] }).kind).toBe(kind);
    expect(O.buildShape("frame", { point: [0, 0] })).toBeNull();
  });
  it("types a free text", () => {
    expect(O.buildText("hello", [5, 6])).toEqual({ op: "shape", kind: "text", text: "hello", at: [5, 6] });
    expect(O.buildText("   ", [5, 6])).toBeNull();
  });
  it("frames a region", () => {
    expect(O.buildFrame([500, 400, 100, 100])).toEqual({ op: "frame", title: "Frame", region: [100, 100, 500, 400] });
    expect(O.buildFrame([0, 0, 5, 5])).toBeNull();
  });
});

describe("pen", () => {
  it("samples to at most 500 points, keeping both ends", () => {
    const points = Array.from({ length: 2001 }, (_, i) => [i, i % 7]);
    const op = O.buildPen(points);
    expect(op.points.length).toBeLessThanOrEqual(500);
    expect(op.points[0]).toEqual([0, 0]);
    expect(op.points[op.points.length - 1]).toEqual([2000, 2000 % 7]);
    expect(op).toMatchObject({ op: "pen", style: "smooth", closed: false });
  });
  it("keeps a short stroke as drawn and drops a dot", () => {
    expect(O.buildPen([[0, 0], [1.234, 2], [3, 4]]).points).toEqual([[0, 0], [1.23, 2], [3, 4]]);
    expect(O.buildPen([[1, 1], [1, 1]])).toBeNull();
  });
});

describe("edit, delete, restyle, undo", () => {
  it("edits only a changed text", () => {
    expect(O.buildEdit(m, "E-5", "Free")).toBeNull();
    expect(O.buildEdit(m, "E-4", "writes")).toEqual({ op: "edit", id: "E-4", text: "writes", if_version: 6 });
    expect(O.buildEdit(m, "C-1", "x")).toBeNull();
  });
  it("deletes elements, frames without their children", () => {
    expect(O.buildDelete(m, ["E-2", "E-3", "K-1"])).toEqual({ op: "delete", ids: ["E-2", "E-3"], if_version: 4 });
    expect(O.buildDelete(m, ["E-1"])).toEqual({ op: "delete", ids: ["E-1"], with_children: false, if_version: 3 });
    expect(O.buildDelete(m, ["K-1"])).toBeNull();
  });
  it("restyles one field over the selection, never a comment", () => {
    expect(O.buildRestyle(m, ["E-2", "E-3", "C-1"], { tone: "danger" })).toEqual({ op: "restyle", ids: ["E-2", "E-3"], tone: "danger", if_version: 4 });
    expect(O.buildRestyle(m, ["E-5"], { size: "l" })).toMatchObject({ size: "l" });
    expect(() => O.buildRestyle(m, ["E-5"], { tone: "pink" })).toThrow();
    expect(O.buildRestyle(m, ["C-1"], { tone: "info" })).toBeNull();
    for (const field of ["variant", "dash", "font"]) {
      const value = { variant: "solid", dash: "dotted", font: "code" }[field];
      expect(O.buildRestyle(m, ["E-5"], { [field]: value })[field]).toBe(value);
    }
  });
  it("undoes a batch", () => {
    expect(O.buildUndo("B-4")).toEqual({ op: "undo", batch: "B-4" });
    expect(O.buildUndo("")).toBeNull();
  });
});
