import { describe, expect, it } from "vitest";
import { beginGesture, TOOLS } from "./gesture.js";
import { createModel } from "./ops.js";

const entries = [
  { id: "E-1", kind: "frame", layer: "zones", v: 3, hit: { shape: "frame", box: [0, 0, 400, 300], band: 40 }, handles: "box", frame: null, edit: { value: "F" } },
  { id: "E-2", kind: "box", layer: "marks", v: 4, hit: { shape: "rect", box: [40, 60, 100, 60] }, handles: "box", connect: true, frame: "E-1", edit: { value: "A" } },
  { id: "E-3", kind: "box", layer: "marks", v: 5, hit: { shape: "rect", box: [600, 60, 100, 60] }, handles: "box", connect: true, frame: null, edit: { value: "B" } },
  { id: "E-4", kind: "arrow", layer: "marks", v: 6, hit: { shape: "line", points: [[140, 90], [600, 90]] }, handles: "ends", frame: null, edit: { value: "" } },
  { id: "E-5", kind: "box", layer: "marks", v: 7, locked: true, hit: { shape: "rect", box: [600, 400, 100, 60] }, handles: "box", connect: true, frame: null, edit: { value: "L" } },
];
const elements = { "E-4": { id: "E-4", type: "arrow", from: "E-2", to: null, points: [[140, 90], [600, 90]] } };
const model = createModel({ dl: 1, version: 7, entries }, (id) => elements[id] || null);

// A SurfacePointer at world point (x, y) with the camera at scale 1 and no offset.
function pointer(type, x, y, extra = {}) {
  return { type, world: [x, y], screen: [x, y], scale: 1, button: 0, buttons: type === "up" ? 0 : 1, shift: false, alt: false, meta: false, ctrl: false,
    pointerId: 1, pointerType: "mouse", hit: null, handle: null, connector: null, ...extra };
}

function run(tool, events, { writable = true, selection = [], query = () => [] } = {}) {
  let sel = selection.slice();
  const ctx = { model, selection: sel, writable, query, setSelection: (ids) => { sel = ids; } };
  const [down, ...rest] = events;
  const g = beginGesture(tool, down, ctx);
  const previews = [];
  let outcome = null;
  if (g) {
    for (const ev of rest) {
      if (ev.type === "up") outcome = g.finish(ev);
      else if (ev.type === "cancel") g.cancel();
      else previews.push(g.update(ev));
    }
  }
  return { g, outcome, previews, selection: sel };
}

describe("select tool", () => {
  it("selects on click and moves on drag (E1)", () => {
    const click = run("select", [pointer("down", 50, 70, { hit: "E-2" }), pointer("up", 50, 70, { hit: "E-2" })]);
    expect(click.selection).toEqual(["E-2"]);
    expect(click.outcome).toBeNull();
    const drag = run("select", [pointer("down", 50, 70, { hit: "E-2" }), pointer("move", 90, 90), pointer("up", 93, 91)]);
    expect(drag.previews[0]).toEqual({ move: { ids: ["E-2"], by: [40, 20] } });
    expect(drag.outcome.ops).toEqual([{ op: "move", ids: ["E-2"], by: [40, 20], if_version: 4 }]);
    expect(drag.outcome.preview).toEqual({ move: { ids: ["E-2"], by: [40, 20] } });
  });
  it("Shift toggles without moving", () => {
    const r = run("select", [pointer("down", 610, 70, { hit: "E-3", shift: true })], { selection: ["E-2"] });
    expect(r.selection).toEqual(["E-2", "E-3"]);
    expect(r.g).toBeNull();
    expect(run("select", [pointer("down", 610, 70, { hit: "E-3", shift: true })], { selection: ["E-2", "E-3"] }).selection).toEqual(["E-2"]);
  });
  it("drags a whole selection and collapses it on a plain click", () => {
    const drag = run("select", [pointer("down", 50, 70, { hit: "E-2" }), pointer("move", 70, 70), pointer("up", 70, 70)], { selection: ["E-2", "E-3"] });
    expect(drag.outcome.ops[0].ids).toEqual(["E-2", "E-3"]);
    const click = run("select", [pointer("down", 50, 70, { hit: "E-2" }), pointer("up", 50, 70)], { selection: ["E-2", "E-3"] });
    expect(click.outcome).toEqual({ select: ["E-2"] });
  });
  it("marquee selects what the band holds (E2)", () => {
    const r = run("select", [pointer("down", -10, -10), pointer("move", 300, 300), pointer("up", 800, 200)], { query: (rect) => (rect[2] >= 700 ? ["E-2", "E-3", "E-4"] : []) });
    expect(r.previews[0].ghost.length).toBe(2);
    expect(r.outcome).toEqual({ select: ["E-2", "E-3", "E-4"] });
    const add = run("select", [pointer("down", -10, -10, { shift: true }), pointer("move", 300, 300), pointer("up", 300, 300)], { selection: ["E-5"], query: () => ["E-2"] });
    expect(add.outcome.select).toEqual(["E-5", "E-2"]);
    expect(run("select", [pointer("down", -10, -10), pointer("up", -10, -10)], { selection: ["E-5"] }).outcome).toEqual({ select: [] });
  });
  it("resizes by a handle (E3)", () => {
    const r = run("select", [pointer("down", 140, 90, { hit: "E-2", handle: { id: "E-2", handle: "e" } }), pointer("move", 180, 95), pointer("up", 200, 95)], { selection: ["E-2"] });
    expect(r.previews[0]).toEqual({ boxes: { "E-2": [40, 60, 140, 60] } });
    expect(r.outcome.ops).toEqual([{ op: "move", id: "E-2", w: 160, if_version: 4 }]);
  });
  it("rebinds an arrow end or frees it", () => {
    const bind = run("select", [pointer("down", 600, 90, { handle: { id: "E-4", handle: "end" } }), pointer("move", 640, 420), pointer("up", 640, 420, { hit: "E-5" })], { selection: ["E-4"] });
    expect(bind.previews[0].hide).toEqual(["E-4"]);
    expect(bind.outcome.ops).toEqual([{ op: "move", id: "E-4", to_element: "E-5", if_version: 6 }]);
    const free = run("select", [pointer("down", 600, 90, { handle: { id: "E-4", handle: "end" } }), pointer("move", 800, 800), pointer("up", 800, 800)], { selection: ["E-4"] });
    expect(free.outcome.ops[0]).toMatchObject({ to_element: null, points: [[140, 90], [800, 800]] });
  });
  it("connects from a connection point (E4)", () => {
    const r = run("select", [pointer("down", 140, 90, { hit: "E-2", connector: { id: "E-2", side: "e", point: [140, 90] } }), pointer("move", 400, 90), pointer("up", 605, 90, { hit: "E-3" })]);
    expect(r.previews[0].ghost[0].k).toBe("arrow");
    expect(r.outcome.ops).toEqual([{ op: "arrow", from: "E-2", to: "E-3" }]);
    const loose = run("select", [pointer("down", 140, 90, { connector: { id: "E-2", side: "e", point: [140, 90] } }), pointer("move", 400, 90), pointer("up", 400, 500)]);
    expect(loose.outcome.ops).toEqual([{ op: "arrow", from: "E-2", to: "400,500" }]);
  });
  it("refuses to drag a locked entry, with a reason", () => {
    const r = run("select", [pointer("down", 610, 410, { hit: "E-5" }), pointer("move", 700, 500), pointer("up", 700, 500)]);
    expect(r.outcome).toEqual({ toast: "E-5 is in a locked region; unlock it first" });
    const handle = run("select", [pointer("down", 700, 430, { handle: { id: "E-5", handle: "e" }, hit: "E-5" }), pointer("move", 750, 430), pointer("up", 750, 430)], { selection: ["E-5"] });
    expect(handle.outcome.ops).toBeUndefined();
  });
});

describe("drawing tools", () => {
  it("creates a shape by drag or click and asks for the editor", () => {
    const drag = run("box", [pointer("down", 100, 100), pointer("move", 200, 180), pointer("up", 260, 180)]);
    expect(drag.outcome).toMatchObject({ ops: [{ op: "shape", kind: "box", at: [100, 100], w: 160, h: 80, text: "" }], editCreated: true });
    for (const tool of ["ellipse", "diamond", "note"]) {
      const click = run(tool, [pointer("down", 10, 20), pointer("up", 10, 20)]);
      expect(click.outcome.ops).toEqual([{ op: "shape", kind: tool, at: [10, 20], text: "" }]);
    }
  });
  it("draws an arrow with the arrow tool", () => {
    const r = run("arrow", [pointer("down", 50, 70, { hit: "E-2" }), pointer("move", 300, 90), pointer("up", 620, 80, { hit: "E-3" })]);
    expect(r.outcome.ops).toEqual([{ op: "arrow", from: "E-2", to: "E-3" }]);
    const free = run("arrow", [pointer("down", 1000, 1000), pointer("move", 1100, 1000), pointer("up", 1200, 1000)]);
    expect(free.outcome.ops).toEqual([{ op: "arrow", points: [[1000, 1000], [1200, 1000]] }]);
  });
  it("draws with the pen (E7)", () => {
    const events = [pointer("down", 0, 0)];
    for (let i = 1; i <= 800; i += 1) events.push(pointer("move", i, Math.sin(i / 10) * 20));
    events.push(pointer("up", 800, 0));
    const r = run("pen", events);
    const op = r.outcome.ops[0];
    expect(op.op).toBe("pen");
    expect(op.points.length).toBeGreaterThanOrEqual(2);
    expect(op.points.length).toBeLessThanOrEqual(500);
    expect(r.previews[r.previews.length - 1].ghost[0].k).toBe("line");
  });
  it("frames a region and asks for the title editor (E8)", () => {
    const r = run("frame", [pointer("down", -20, -20), pointer("move", 100, 100), pointer("up", 720, 140)]);
    expect(r.outcome).toMatchObject({ ops: [{ op: "frame", title: "Frame", region: [-20, -20, 720, 140] }], editCreated: true });
  });
  it("the text tool opens a new text where clicked", () => {
    expect(run("text", [pointer("down", 30, 40), pointer("up", 30, 40)]).outcome).toEqual({ editNewText: [30, 40] });
  });
  it("cancel sends nothing", () => {
    const r = run("box", [pointer("down", 100, 100), pointer("move", 200, 180), pointer("cancel", 200, 180)]);
    expect(r.outcome).toBeNull();
  });
  it("ignores other buttons", () => {
    expect(beginGesture("box", pointer("down", 0, 0, { button: 1 }), { model, selection: [], writable: true, query: () => [], setSelection() {} })).toBeNull();
  });
});

describe("read-only", () => {
  it("no tool and no pointer sequence ever produces an op (E10)", () => {
    const sequences = [
      [pointer("down", 50, 70, { hit: "E-2" }), pointer("move", 120, 120), pointer("up", 120, 120)],
      [pointer("down", 140, 90, { hit: "E-2", handle: { id: "E-2", handle: "se" } }), pointer("move", 300, 300), pointer("up", 300, 300)],
      [pointer("down", 600, 90, { handle: { id: "E-4", handle: "end" } }), pointer("move", 800, 800), pointer("up", 800, 800)],
      [pointer("down", 140, 90, { connector: { id: "E-2", side: "e", point: [140, 90] } }), pointer("move", 400, 90), pointer("up", 605, 90, { hit: "E-3" })],
      [pointer("down", -10, -10), pointer("move", 300, 300), pointer("up", 800, 200)],
      [pointer("down", 100, 100), pointer("move", 200, 200), pointer("up", 260, 180)],
    ];
    for (const tool of TOOLS) {
      for (const events of sequences) {
        const r = run(tool, events, { writable: false, selection: ["E-2"], query: () => ["E-2"] });
        expect(r.outcome && r.outcome.ops).toBeFalsy();
        expect(r.outcome && r.outcome.editNewText).toBeFalsy();
      }
    }
  });
});
