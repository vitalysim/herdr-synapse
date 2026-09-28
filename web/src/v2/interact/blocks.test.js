// The page side of canvas v2 phase 2 (canvas-v2-phase2.md 6.3 and 8.3): inline parts, pins, route
// styles, tool templates and the block gesture, stack drops and select parent. Pure: the builders
// over the hand-written Phase 2 display list (render/__fixtures__/phase2.json), and the shared
// drop-index vectors the server's rule is pinned by (tests/fixtures/display/stack-drop-vectors.json).
import fs from "node:fs";
import path from "node:path";
import { describe, expect, it } from "vitest";
import { beginGesture } from "./gesture.js";
import { READ_ONLY_TOOLS, commandOf, escapeSelection } from "./keymap.js";
import {
  ROUTES,
  buildBlock,
  buildDelete,
  buildEditPart,
  buildFrame,
  buildMove,
  buildPin,
  buildRestyleRoute,
  buildShape,
  buildUnpin,
  createModel,
  dropFrame,
  isContainer,
  pinChoices,
  stackDrop,
  stackDropLine,
  stackIndex,
} from "./ops.js";
import { editTargetOf, withoutPartText } from "./parts.js";
import { toolsetFrom } from "./toolset.js";

const load = (rel) => JSON.parse(fs.readFileSync(path.resolve(import.meta.dirname, rel), "utf8"));
const dl = load("../render/__fixtures__/phase2.json");
const VECTORS = path.resolve(import.meta.dirname, "../../../../tests/fixtures/display/stack-drop-vectors.json");

// The canonical elements the fixture stands for: container blocks are frames (D2), arrows carry
// their style; everything else only needs its type.
const elements = {
  "E-10": { id: "E-10", type: "frame", block: "kanban", frame: null },
  "E-11": { id: "E-11", type: "frame", block: "section", frame: "E-10", group: "E-10" },
  "E-12": { id: "E-12", type: "card", frame: "E-11", group: "E-10" },
  "E-13": { id: "E-13", type: "card", frame: "E-11", group: "E-10" },
  "E-14": { id: "E-14", type: "frame", block: "section", frame: "E-10", group: "E-10" },
  "E-15": { id: "E-15", type: "card", frame: "E-14", group: "E-10" },
  "E-20": { id: "E-20", type: "table", frame: null },
  "E-40": { id: "E-40", type: "frame", block: "graph", frame: null },
  "E-41": { id: "E-41", type: "box", frame: "E-40", group: "E-40" },
  "E-42": { id: "E-42", type: "box", frame: "E-40", group: "E-40" },
  "E-43": { id: "E-43", type: "arrow", frame: "E-40", group: "E-40", from: "E-41", to: "E-42", style: { route: "orthogonal" } },
};
const model = createModel(dl, (id) => elements[id] || null);
const bare = createModel(dl); // no canonical elements: the entries alone decide
const entryOf = (id) => model.entry(id);

function pointer(type, x, y, extra = {}) {
  return { type, world: [x, y], screen: [x, y], scale: 1, button: 0, buttons: type === "up" ? 0 : 1, shift: false, alt: false, meta: false, ctrl: false,
    pointerId: 1, pointerType: "mouse", hit: null, part: null, handle: null, connector: null, ...extra };
}

function run(tool, events, { writable = true, selection = [], toolset } = {}) {
  let sel = selection.slice();
  const ctx = { model, selection: sel, writable, query: () => [], setSelection: (ids) => { sel = ids; } };
  const [down, ...rest] = events;
  const g = beginGesture(tool, down, ctx, toolset);
  const previews = [];
  let outcome = null;
  if (g) {
    for (const ev of rest) {
      if (ev.type === "up") outcome = g.finish(ev);
      else previews.push(g.update(ev));
    }
  }
  return { g, outcome, previews, selection: sel };
}

// The three Phase 2 tools as tokens.json carries them (6.4), plus one this page cannot use yet.
const MANIFEST = [
  { id: "box", kind: "box", key: "r", title: "Rectangle", glyph: "▭", gesture: "shape", one_shot: true, order: 10 },
  { id: "sticky", kind: "sticky", key: "n", title: "Sticky", glyph: "▢", gesture: "block", one_shot: true, order: 40, template: { op: "sticky", text: "" } },
  { id: "card", kind: "card", key: "c", title: "Card", glyph: "▤", gesture: "block", one_shot: true, order: 45, template: '{"op": "card", "title": ""}' },
  { id: "section", kind: "section", key: "s", title: "Section", glyph: "⬚", gesture: "frame", one_shot: true, order: 90, template: { op: "section", title: "Section", layout: "free" } },
  { id: "hologram", kind: "hologram", key: "g", title: "Hologram", glyph: "H", gesture: "beam", one_shot: true, order: 99 },
];
const TOOLSET = toolsetFrom(MANIFEST);

describe("tool templates and the block gesture (W-e)", () => {
  it("toolsetFrom keeps templates, parses JSON text, and skips gestures it has no handler for", () => {
    expect(TOOLSET.ids).toEqual(["select", "hand", "box", "sticky", "card", "section"]);
    expect(TOOLSET.get("sticky").template).toEqual({ op: "sticky", text: "" });
    expect(TOOLSET.get("card").template).toEqual({ op: "card", title: "" });
    expect(TOOLSET.get("box").template).toBeUndefined();
    expect(TOOLSET.keys.n).toBe("sticky");
    // A shape tool without a template still makes its kind with the shape op; block tools do not.
    expect(TOOLSET.shapes).toEqual({ box: "box" });
  });

  it("drops a block tool without a usable template, and a malformed template on any tool", () => {
    const set = toolsetFrom([
      { id: "a", kind: "a", key: "a", gesture: "block" },
      { id: "b", kind: "b", key: "b", gesture: "block", template: "{not json" },
      { id: "c", kind: "c", key: "c", gesture: "shape", template: { text: "no op" } },
      { id: "d", kind: "d", key: "d", gesture: "block", template: { op: "d" } },
    ]);
    expect(set.ids).toEqual(["select", "hand", "d"]);
  });

  it("a click sends the template at the point; the editor opens on what it makes (P5)", () => {
    const r = run("card", [pointer("down", 1300.4, 500.6), pointer("up", 1300.4, 500.6)], { toolset: TOOLSET });
    expect(r.outcome.ops).toEqual([{ op: "card", title: "", at: [1300, 501] }]);
    expect(r.outcome.editCreated).toBe(true);
  });

  it("a click in a stack (a kanban column) sends in and index, so the new card joins it there (QA phase 2, F6)", () => {
    const done = model.entry("E-15").hit.box; // the one card in Done (E-14)
    const below = [done[0] + 20, done[1] + done[3] / 2 + 1];
    const r = run("card", [pointer("down", ...below), pointer("up", ...below)], { toolset: TOOLSET });
    expect(r.outcome.ops).toEqual([{ op: "card", title: "", in: "E-14", index: 1 }]);
    const above = [done[0] + 20, done[1] + 2];
    expect(run("sticky", [pointer("down", ...above), pointer("up", ...above)], { toolset: TOOLSET }).outcome.ops)
      .toEqual([{ op: "sticky", text: "", in: "E-14", index: 0 }]);
    // Outside any stack, or a drag: at, as before.
    expect(run("card", [pointer("down", 1300, 500), pointer("up", 1300, 500)], { toolset: TOOLSET }).outcome.ops[0].at).toEqual([1300, 500]);
  });

  it("a drag sends the box as w and h, and draws a ghost meanwhile", () => {
    const r = run("sticky", [pointer("down", 1300, 500), pointer("move", 1400, 580), pointer("up", 1460, 700)], { toolset: TOOLSET });
    expect(r.previews[0].ghost.length).toBeGreaterThan(0);
    expect(r.outcome.ops).toEqual([{ op: "sticky", text: "", at: [1300, 500], w: 160, h: 200 }]);
  });

  it("the frame gesture sends the section template with the region (P6)", () => {
    const r = run("section", [pointer("down", 120, 240), pointer("move", 300, 300), pointer("up", 760, 520)], { toolset: TOOLSET });
    expect(r.outcome.ops).toEqual([{ op: "section", title: "Section", layout: "free", region: [120, 240, 760, 520] }]);
    expect(r.outcome.editCreated).toBe(true);
  });

  it("the gesture's own fields win over the template's, and the template is never shared", () => {
    const template = { op: "card", title: "", at: [0, 0] };
    const op = buildBlock(template, { point: [40, 60] });
    expect(op).toEqual({ op: "card", title: "", at: [40, 60] });
    op.title = "changed";
    expect(template.title).toBe("");
    expect(buildShape("box", { point: [1, 2], template: { op: "sticky", text: "" } })).toEqual({ op: "sticky", text: "", at: [1, 2] });
    expect(buildFrame([0, 0, 400, 300], "Frame", { op: "section", title: "S" })).toEqual({ op: "section", title: "S", region: [0, 0, 400, 300] });
    expect(buildBlock(null, { point: [1, 2] })).toBeNull();
  });
});

describe("inline parts (W-a)", () => {
  it("buildEditPart sends edit {id, part, text} with the block version", () => {
    expect(buildEditPart(model, "E-20", "r1.owner", "docs team")).toEqual({ op: "edit", id: "E-20", part: "r1.owner", text: "docs team", if_version: 7 });
    expect(buildEditPart(model, "E-12", "title", "Rotate all keys")).toEqual({ op: "edit", id: "E-12", part: "title", text: "Rotate all keys", if_version: 3 });
  });

  it("sends nothing for an unchanged text, an unknown part, or an entry without parts", () => {
    expect(buildEditPart(model, "E-20", "r1.owner", "writer")).toBeNull();
    expect(buildEditPart(model, "E-20", "r9.owner", "x")).toBeNull();
    expect(buildEditPart(model, "E-41", "title", "x")).toBeNull();
  });

  it("the editor opens on the part asked for, else the entry's text, else its first part", () => {
    const table = entryOf("E-20");
    expect(editTargetOf(table, "r2.c1")).toEqual({ part: "r2.c1", spec: table.parts.find((p) => p.part === "r2.c1").edit });
    expect(editTargetOf(table, "nope")).toBeNull();
    expect(editTargetOf(table).part).toBe("h.c1");
    const node = entryOf("E-41");
    expect(editTargetOf(node)).toEqual({ part: null, spec: node.edit });
    // A card a tool just made opens on its title part (P5), even with a text of its own.
    const card = { ...entryOf("E-12"), edit: { value: "", box: [0, 0, 10, 10] } };
    expect(editTargetOf(card, null, { created: true }).part).toBe("title");
    expect(editTargetOf(card).part).toBeNull();
    // An entry whose own edit names a part (the server's card) edits that part.
    const named = { ...entryOf("E-12"), edit: { ...entryOf("E-12").parts[1].edit } };
    expect(editTargetOf(named)).toEqual({ part: "body", spec: entryOf("E-12").parts[1].edit });
  });

  it("while a part is edited, only the text inside its box is left out of the drawing", () => {
    const table = entryOf("E-20");
    const cell = table.parts.find((p) => p.part === "r1.owner");
    const kept = withoutPartText(table, cell.edit.box);
    const texts = (items) => items.filter((p) => p.k === "text").map((p) => p.lines[0].t);
    expect(texts(table.items)).toContain("writer");
    expect(texts(kept)).not.toContain("writer");
    expect(texts(kept)).toHaveLength(texts(table.items).length - 1);
    expect(kept.filter((p) => p.k !== "text")).toHaveLength(table.items.filter((p) => p.k !== "text").length);
  });
});

describe("pins (W-b) and route styles (W-c)", () => {
  it("buildPin pins what is not pinned by the operator; buildUnpin unpins what holds a pin", () => {
    expect(buildPin(model, ["E-41"])).toEqual({ op: "pin", ids: ["E-41"], if_version: 8 });
    expect(buildPin(model, ["E-42"])).toEqual({ op: "pin", ids: ["E-42"], if_version: 9 }); // an agent pin becomes the operator's
    expect(buildPin(model, ["E-12"])).toBeNull(); // already the operator's
    expect(buildUnpin(model, ["E-12"])).toEqual({ op: "unpin", ids: ["E-12"], if_version: 3 });
    expect(buildUnpin(model, ["E-41", "E-42"])).toEqual({ op: "unpin", ids: ["E-42"], if_version: 9 });
    expect(buildUnpin(model, ["E-41"])).toBeNull();
    expect(pinChoices(model, ["E-12", "E-41"])).toEqual({ pin: true, unpin: true });
    expect(pinChoices(model, ["E-12"])).toEqual({ pin: false, unpin: true });
  });

  it("an op over targets at different versions carries no if_version", () => {
    expect(buildPin(model, ["E-41", "E-13"])).toEqual({ op: "pin", ids: ["E-41", "E-13"] });
  });

  it("buildRestyleRoute restyles only the selected arrows", () => {
    expect(ROUTES).toEqual(["straight", "orthogonal", "curved"]);
    expect(buildRestyleRoute(model, ["E-43", "E-41"], "orthogonal")).toEqual({ op: "restyle", ids: ["E-43"], route: "orthogonal", if_version: 9 });
    expect(buildRestyleRoute(model, ["E-41"], "curved")).toBeNull();
    expect(() => buildRestyleRoute(model, ["E-43"], "zigzag")).toThrow(/unknown route/);
  });
});

describe("containers and stack drops (W-f)", () => {
  it("every block stored as a frame is a container, with or without the canonical element", () => {
    for (const id of ["E-10", "E-11", "E-14", "E-40"]) {
      expect(isContainer(model, id), id).toBe(true);
      expect(isContainer(bare, id), id).toBe(true);
    }
    for (const id of ["E-12", "E-20", "E-41", "E-43"]) expect(isContainer(model, id), id).toBe(false);
    // Deleting a block root keeps its members (1.7).
    expect(buildDelete(model, ["E-40"]).with_children).toBe(false);
    expect(buildDelete(model, ["E-41"]).with_children).toBeUndefined();
  });

  it("stackIndex: siblings whose centre comes first along the axis; row-major for a grid", () => {
    const col = [[0, 0, 100, 40], [0, 60, 100, 40], [0, 120, 100, 40]];
    expect(stackIndex("column", col, [50, 10])).toBe(0);
    expect(stackIndex("column", col, [50, 50])).toBe(1);
    expect(stackIndex("column", col, [50, 500])).toBe(3);
    const row = [[0, 0, 100, 40], [120, 0, 100, 40]];
    expect(stackIndex("row", row, [110, 20])).toBe(1);
    const grid = [[0, 0, 100, 40], [120, 0, 100, 40], [0, 60, 100, 40], [120, 60, 100, 40]];
    expect(stackIndex("grid", grid, [10, 20])).toBe(0);
    expect(stackIndex("grid", grid, [200, 20])).toBe(2);
    expect(stackIndex("grid", grid, [60, 80])).toBe(3);
    expect(stackIndex("grid", grid, [60, 300])).toBe(4);
  });

  const vectors = fs.existsSync(VECTORS) ? JSON.parse(fs.readFileSync(VECTORS, "utf8")) : null;
  it("stackIndex equals the server's rule on every shared vector", () => {
    expect(vectors, "tests/fixtures/display/stack-drop-vectors.json").not.toBeNull();
    expect(vectors.cases.length).toBeGreaterThan(0);
    expect(Object.keys(vectors.rule).sort()).toEqual(["column", "grid", "row"]);
    for (const v of vectors.cases) expect(stackIndex(v.layout, v.siblings, v.centre), JSON.stringify(v)).toBe(v.index);
  });

  it("stackDropLine puts the line in the gap, before the first or after the last", () => {
    const col = [[0, 0, 100, 40], [0, 60, 100, 40]];
    expect(stackDropLine("column", col, 1, [-20, -20, 140, 140], 20)).toEqual([[0, 50], [100, 50]]);
    expect(stackDropLine("column", col, 0, [-20, -20, 140, 140], 20)).toEqual([[0, -10], [100, -10]]);
    expect(stackDropLine("column", col, 2, [-20, -20, 140, 140], 20)).toEqual([[0, 110], [100, 110]]);
    expect(stackDropLine("row", [[0, 0, 100, 40], [120, 0, 100, 60]], 1, [0, 0, 220, 60], 20)).toEqual([[110, 0], [110, 60]]);
    expect(stackDropLine("column", [], 0, [0, 0, 200, 100], 20)).toEqual([[20, 50], [180, 50]]);
  });

  it("a card dragged over another column joins it at the index its centre gives (P2)", () => {
    const card = model.entry("E-13").hit.box; // [152, 372, 240, 80] in Todo
    // Its centre lands in Done, above Persona research: index 0 there, line above that card.
    const box = [card[0] + 300, card[1] - 120, card[2], card[3]];
    expect(dropFrame(model, "E-13", box)).toBe("E-14");
    const drop = stackDrop(model, "E-13", box);
    expect(drop).toMatchObject({ container: "E-14", index: 0 });
    expect(drop.line).toEqual([[452, 262], [692, 262]]);
    expect(buildMove(model, ["E-13"], [300, -120])).toEqual({ op: "move", ids: ["E-13"], by: [300, -120], frame: "E-14", if_version: 3 });
  });

  it("a reorder inside its own column keeps the frame and still shows the line", () => {
    const card = model.entry("E-13").hit.box;
    const box = [card[0], card[1] - 100, card[2], card[3]];
    expect(dropFrame(model, "E-13", box)).toBeUndefined();
    expect(stackDrop(model, "E-13", box)).toMatchObject({ container: "E-11", index: 0 });
    expect(buildMove(model, ["E-13"], [0, -100])).toEqual({ op: "move", ids: ["E-13"], by: [0, -100], if_version: 3 });
  });

  it("the move tool's preview carries the insertion line while over a stack", () => {
    const r = run("select", [pointer("down", 200, 400, { hit: "E-13" }), pointer("move", 500, 290), pointer("up", 500, 290)], { selection: ["E-13"] });
    const line = r.previews[0].ghost;
    expect(line).toHaveLength(1);
    expect(line[0]).toMatchObject({ k: "line", stroke: "base.selection", sw_px: 3 });
    expect(r.outcome.ops[0].frame).toBe("E-14");
    // Not over a stack (a graph node): no line.
    const g = run("select", [pointer("down", 900, 190, { hit: "E-41" }), pointer("move", 940, 230), pointer("up", 940, 230)], { selection: ["E-41"] });
    expect(g.previews[0].ghost).toBeUndefined();
    expect(g.outcome.ops).toEqual([{ op: "move", ids: ["E-41"], by: [40, 40], if_version: 8 }]);
  });
});

describe("select parent (W-g)", () => {
  it("Esc on one member selects its block; the next Esc clears", () => {
    const first = escapeSelection(["E-13"], entryOf, null);
    expect(first).toEqual({ select: ["E-10"], parented: "E-10" });
    expect(escapeSelection(first.select, entryOf, first.parented)).toEqual({ select: [], parented: null });
  });

  it("clears at once for a non-member, several ids, or a block that is gone", () => {
    expect(escapeSelection(["E-20"], entryOf)).toEqual({ select: [], parented: null });
    expect(escapeSelection(["E-12", "E-13"], entryOf)).toEqual({ select: [], parented: null });
    expect(escapeSelection(["E-13"], (id) => (id === "E-13" ? entryOf(id) : null))).toEqual({ select: [], parented: null });
    expect(commandOf({ key: "Escape", type: "keydown" }, { writable: false })).toEqual({ command: "cancel" });
  });
});

describe("read-only pages reach none of the new builders", () => {
  it("block, section and create tools only select, and no key picks them", () => {
    for (const tool of ["card", "sticky", "section"]) {
      const r = run(tool, [pointer("down", 1300, 500), pointer("move", 1400, 580), pointer("up", 1400, 580)], { writable: false, toolset: TOOLSET });
      expect(r.outcome && r.outcome.ops).toBeFalsy(); // a marquee at most
    }
    // A read-only drag of a member moves nothing.
    const r = run("select", [pointer("down", 200, 400, { hit: "E-13" }), pointer("move", 500, 290), pointer("up", 500, 290)], { writable: false });
    expect(r.outcome).toBeNull();
    for (const key of ["c", "s", "n", "r", "Enter", "Delete"]) {
      const cmd = commandOf({ key, type: "keydown" }, { writable: false });
      expect(cmd === null || (cmd.command === "tool" && READ_ONLY_TOOLS.has(cmd.tool)), key).toBe(true);
    }
  });
});
