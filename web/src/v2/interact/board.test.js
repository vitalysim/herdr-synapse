// @vitest-environment jsdom
// Board wiring: the display list is fetched and kept, gestures and keys become ops through
// /ops, and a read-only page never POSTs anything (a spy on the api module). Surface is replaced
// by a stand-in that hands its props to the test; everything else is the real code.
import React from "react";
import { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { createBus } from "../../bus.js";
import { createSceneStore } from "../../sceneStore.js";

const surface = { props: null };
const calls = { get: [], post: [], bytes: [] };
let displayDoc = null;
let opsAnswer = null;

vi.mock("../render/index.js", async (importOriginal) => {
  const real = await importOriginal();
  function FakeSurface(props) {
    surface.props = props;
    return React.createElement("div", { "data-testid": "surface" }, props.children);
  }
  return { ...real, Surface: FakeSurface, installQAHook: () => () => {}, verifyText: async () => [] };
});

vi.mock("../../api.js", async (importOriginal) => {
  const real = await importOriginal();
  return {
    ...real,
    getJSON: vi.fn(async (path) => {
      calls.get.push(path);
      return displayDoc;
    }),
    postJSON: vi.fn(async (path, body) => {
      calls.post.push([path, body]);
      return opsAnswer ? opsAnswer(body) : { version: displayDoc.version + 1, batch: "B-1", applied: [{ index: 0, op: body.ops?.[0]?.op, ids: ["E-9"] }], refused: [], warnings: [] };
    }),
    postBytes: vi.fn(async (path) => {
      calls.bytes.push(path);
      return { ok: true };
    }),
  };
});

const { default: Board, PANEL_INSET_PX, fitViewport } = await import("../Board.jsx");

globalThis.IS_REACT_ACT_ENVIRONMENT = true;

const entries = [
  { id: "E-1", kind: "box", layer: "marks", z: 1, v: 4, bbox: [0, 0, 160, 80], hit: { shape: "rect", box: [0, 0, 160, 80] }, handles: "box", connect: true, frame: null, locked: false, author: "human",
    edit: { field: "text", value: "Hello", box: [16, 12, 128, 56], font: "sans", weight: 500, size: 20, lh: 25, align: "center", wrap: "box", fill: "tone.neutral.text" },
    items: [{ k: "rect", x: 0, y: 0, w: 160, h: 80, r: 8, fill: "tone.neutral.fill", stroke: "tone.neutral.stroke", sw: 2 }] },
  { id: "E-2", kind: "box", layer: "marks", z: 2, v: 5, bbox: [300, 0, 460, 80], hit: { shape: "rect", box: [300, 0, 160, 80] }, handles: "box", connect: true, frame: null, locked: false, author: "alpha",
    edit: { field: "text", value: "World", box: [316, 12, 128, 56], font: "sans", weight: 500, size: 20, lh: 25, align: "center", wrap: "box", fill: "tone.neutral.text" }, items: [] },
];

function pointer(type, x, y, extra = {}) {
  return { type, world: [x, y], screen: [x, y], scale: 1, button: 0, buttons: 1, shift: false, alt: false, meta: false, ctrl: false, pointerId: 1, pointerType: "mouse", hit: null, handle: null, connector: null, ...extra };
}

let container;
let root;

async function mount({ writable }) {
  const store = createSceneStore();
  store.replace({ version: 5, elements: [{ id: "E-1", type: "box", x: 0, y: 0, w: 160, h: 80, updated_seq: 4, author: "human" }, { id: "E-2", type: "box", x: 300, y: 0, w: 160, h: 80, updated_seq: 5, author: "alpha" }] });
  const toast = vi.fn();
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  await act(async () => {
    root.render(React.createElement(Board, { team: "t", teamRow: { name: "t", viz: false, members: [] }, writable, store, bus: createBus(), visible: true, toast, theme: "light", onFallback: vi.fn() }));
  });
  await act(async () => {
    surface.props.onViewport({ w: 800, h: 600 });
  });
  return { store, toast };
}

const send = async (ev) => act(async () => surface.props.onPointer(ev));
const key = async (k, extra = {}) =>
  act(async () => {
    window.dispatchEvent(new KeyboardEvent("keydown", { key: k, bubbles: true, ...extra }));
  });

beforeEach(() => {
  calls.get.length = 0;
  calls.post.length = 0;
  calls.bytes.length = 0;
  opsAnswer = null;
  displayDoc = { dl: 1, version: 5, bbox: [-40, -40, 500, 120], layers: ["zones", "marks", "labels", "overlays"], palettes: { light: { "tone.neutral.text": "#111111" }, dark: {} }, entries };
});

afterEach(async () => {
  await act(async () => root.unmount());
  container.remove();
});

describe("Board", () => {
  it("fetches the whole list once: the stream's snapshot of the same version asks for nothing more, a reset asks again", async () => {
    const { store } = await mount({ writable: true });
    expect(calls.get).toEqual(["/api/teams/t/display"]);
    await act(async () => store.replace({ version: 5, elements: [] }));
    expect(calls.get).toEqual(["/api/teams/t/display"]);
    displayDoc = { ...displayDoc, version: 2, entries: entries.slice(0, 1) };
    await act(async () => store.replace({ version: 2, elements: [] }));
    expect(calls.get).toEqual(["/api/teams/t/display", "/api/teams/t/display"]);
  });

  it("catches up with a delta when the stream is ahead of the list it holds", async () => {
    const { store } = await mount({ writable: true });
    displayDoc = { dl: 1, version: 6, since: 5, full: false, bbox: [-40, -40, 500, 120], upserts: [], removes: ["E-2"] };
    await act(async () => store.applyEvents([{ seq: 6, op: "delete", ids: ["E-2"], changes: [] }]));
    expect(calls.get).toEqual(["/api/teams/t/display", "/api/teams/t/display?since=5"]);
    expect(surface.props.dl.version).toBe(6);
    expect(surface.props.dl.entries.map((e) => e.id)).toEqual(["E-1"]);
  });

  it("fits the board beside the open side panel, not under it (QA R-7)", async () => {
    await mount({ writable: true });
    const cam = surface.props.camera;
    const right = (displayDoc.bbox[2] - cam.x) * cam.scale;
    expect(right).toBeLessThanOrEqual(800 - PANEL_INSET_PX + 0.001);
    expect(fitViewport({ w: 800, h: 600 }, true)).toEqual({ w: 800 - PANEL_INSET_PX, h: 600 });
    expect(fitViewport({ w: 800, h: 600 }, false)).toEqual({ w: 800, h: 600 });
    expect(fitViewport({ w: 500, h: 600 }, true)).toEqual({ w: 500, h: 600 }); // too narrow to spare it
  });

  it("fetches the whole display list and hands it to Surface", async () => {
    await mount({ writable: true });
    expect(calls.get[0]).toBe("/api/teams/t/display");
    expect(surface.props.dl.version).toBe(5);
    expect(surface.props.camera.scale).toBeGreaterThan(0);
  });

  it("turns a drag into one move op with if_version", async () => {
    await mount({ writable: true });
    await send(pointer("down", 10, 10, { hit: "E-1" }));
    await send(pointer("move", 50, 30));
    expect(surface.props.preview.move).toEqual({ ids: ["E-1"], by: [40, 20] });
    await send(pointer("up", 50, 30));
    expect(calls.post).toHaveLength(1);
    expect(calls.post[0][0]).toBe("/api/teams/t/ops");
    expect(calls.post[0][1]).toEqual({ ops: [{ op: "move", ids: ["E-1"], by: [40, 20], if_version: 4 }], atomic: false });
    // The preview holds until the display list reaches the op's version.
    expect(surface.props.preview.move).toEqual({ ids: ["E-1"], by: [40, 20] });
  });

  it("clears the preview and says why on a refusal", async () => {
    const { toast } = await mount({ writable: true });
    opsAnswer = () => ({ version: 5, batch: null, applied: [], refused: [{ index: 0, code: "canvas_stale", message: "E-1 changed since v4 (it is at v6); look again", details: { id: "E-1", current: 6 } }], warnings: [] });
    await send(pointer("down", 10, 10, { hit: "E-1" }));
    await send(pointer("move", 50, 30));
    await send(pointer("up", 50, 30));
    expect(surface.props.preview).toBeNull();
    // Worded for a person ("look again" is the agents' wording).
    expect(toast).toHaveBeenCalledWith("E-1 changed on the board before your change arrived, so it was not applied; try again", "warn");
    opsAnswer = () => ({ version: 5, batch: null, applied: [], refused: [{ index: 0, code: "element_locked", message: "E-1 is locked" }], warnings: [] });
    await send(pointer("down", 10, 10, { hit: "E-1" }));
    await send(pointer("move", 50, 30));
    await send(pointer("up", 50, 30));
    expect(toast).toHaveBeenLastCalledWith("element_locked: E-1 is locked", "warn");
  });

  it("a second drag before the list catches up chains on the first one's version (QA 1 finding 6)", async () => {
    await mount({ writable: true });
    let version = 5;
    opsAnswer = (body) => {
      version += 1;
      return { version, batch: `B-${version}`, applied: [{ index: 0, op: "move", ids: body.ops[0].ids }], refused: [], warnings: [] };
    };
    for (let i = 0; i < 3; i += 1) {
      await send(pointer("down", 10, 10, { hit: "E-1" }));
      await send(pointer("move", 50, 10));
      await send(pointer("up", 50, 10));
    }
    // The list still says E-1 is at v4 (it never caught up in this test); the page knows better.
    expect(surface.props.dl.entries.find((e) => e.id === "E-1").v).toBe(4);
    expect(calls.post.map(([, body]) => body.ops[0].if_version)).toEqual([4, 6, 7]);
    // Another element keeps the list's version.
    await send(pointer("down", 310, 10, { hit: "E-2" }));
    await send(pointer("move", 350, 10));
    await send(pointer("up", 350, 10));
    expect(calls.post.at(-1)[1].ops[0].if_version).toBe(5);
  });

  it("undoes its own last batch with Cmd-Z, and deletes with Delete", async () => {
    await mount({ writable: true });
    await send(pointer("down", 10, 10, { hit: "E-1" }));
    await send(pointer("move", 50, 30));
    await send(pointer("up", 50, 30));
    await key("z", { metaKey: true });
    expect(calls.post[1][1].ops).toEqual([{ op: "undo", batch: "B-1" }]);
    await send(pointer("down", 310, 10, { hit: "E-2" }));
    await send(pointer("up", 310, 10, { hit: "E-2" }));
    expect(surface.props.selection).toEqual(["E-2"]);
    await key("Delete");
    expect(calls.post[2][1].ops).toEqual([{ op: "delete", ids: ["E-2"], if_version: 5 }]);
  });

  it("edits a label through the overlay (E5, E6)", async () => {
    await mount({ writable: true });
    await send(pointer("dblclick", 50, 40, { hit: "E-1" }));
    const area = container.querySelector("textarea.v2-text-editor");
    expect(area.value).toBe("Hello");
    expect(surface.props.preview.hide).toEqual(["E-1"]);
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
      setter.call(area, "Hello there");
      area.dispatchEvent(new Event("input", { bubbles: true }));
    });
    await act(async () => {
      area.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", metaKey: true, bubbles: true }));
    });
    expect(container.querySelector("textarea.v2-text-editor")).toBeNull();
    expect(calls.post[0][1].ops).toEqual([{ op: "edit", id: "E-1", text: "Hello there", if_version: 4 }]);
  });

  it("says so, with the text, when the element being edited is deleted meanwhile (QA 1 finding 11)", async () => {
    const { store, toast } = await mount({ writable: true });
    await send(pointer("dblclick", 50, 40, { hit: "E-1" }));
    const area = container.querySelector("textarea.v2-text-editor");
    await act(async () => {
      const setter = Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set;
      setter.call(area, "Hello there");
      area.dispatchEvent(new Event("input", { bubbles: true }));
    });
    displayDoc = { dl: 1, version: 6, since: 5, full: false, bbox: [-40, -40, 500, 120], upserts: [], removes: ["E-1"] };
    await act(async () => store.applyEvents([{ seq: 6, op: "delete", ids: ["E-1"], changes: [] }]));
    expect(toast).toHaveBeenCalledWith("E-1 was deleted while you were editing it; copy your text before you close the editor", "warn");
    const still = container.querySelector("textarea.v2-text-editor");
    expect(still && still.value).toBe("Hello there");
    await act(async () => {
      still.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", metaKey: true, bubbles: true }));
    });
    expect(container.querySelector("textarea.v2-text-editor")).toBeNull();
    expect(toast).toHaveBeenLastCalledWith("E-1 was deleted while you were editing it, so your text was not saved: “Hello there”", "warn");
    expect(calls.post).toEqual([]);
  });

  it("read-only: tools hidden, and no pointer or key ever POSTs (E10)", async () => {
    await mount({ writable: false });
    expect(container.querySelector(".v2-toolbar")).toBeNull();
    const sequences = [
      [pointer("down", 10, 10, { hit: "E-1" }), pointer("move", 80, 80), pointer("up", 80, 80)],
      [pointer("down", 160, 40, { hit: "E-1", handle: { id: "E-1", handle: "e" } }), pointer("move", 200, 40), pointer("up", 200, 40)],
      [pointer("down", 160, 40, { connector: { id: "E-1", side: "e", point: [160, 40] } }), pointer("move", 300, 40), pointer("up", 310, 40, { hit: "E-2" })],
      [pointer("down", -20, -20), pointer("move", 500, 100), pointer("up", 500, 100)],
      [pointer("dblclick", 50, 40, { hit: "E-1" })],
      [pointer("dblclick", 700, 400)],
    ];
    for (const tool of ["r", "o", "d", "n", "c", "t", "a", "p", "f", "s", "v"]) {
      await key(tool);
      for (const events of sequences) for (const ev of events) await send(ev);
    }
    for (const [k, extra] of [["Delete", {}], ["Backspace", {}], ["ArrowLeft", {}], ["Enter", {}], ["z", { metaKey: true }], ["a", { metaKey: true }], ["Delete", {}]]) await key(k, extra);
    expect(container.querySelector("textarea.v2-text-editor")).toBeNull();
    expect(calls.post).toEqual([]);
    expect(calls.bytes).toEqual([]);
  });
});

// Canvas v2 phase 2 on the board (canvas-v2-phase2.md 6.3): inline parts through the overlay,
// select parent, and the style bar's pin and route choices.
describe("Board, phase 2", () => {
  const cellEdit = (value, box, part) => ({ field: "text", value, box, font: "sans", weight: 400, size: 16, lh: 20, align: "start", wrap: "box", fill: "base.ink", part });
  const cellText = (t, x, y) => ({ k: "text", x, anchor: "start", font: "sans", weight: 400, size: 16, lh: 20, fill: "base.ink", box: [x, y, 176, 20], lines: [{ y: y + 15, t, w: 60 }] });
  const phase2 = [
    { id: "E-20", kind: "table", layer: "marks", z: 1, v: 7, bbox: [0, 200, 400, 280], hit: { shape: "rect", box: [0, 200, 400, 80] }, handles: "box", connect: false, frame: null, locked: false, author: "human", edit: null,
      parts: [
        { part: "r1.c1", hit: { shape: "rect", box: [0, 200, 200, 40] }, edit: cellEdit("Docs", [12, 208, 176, 24], "r1.c1") },
        { part: "r1.owner", hit: { shape: "rect", box: [200, 200, 200, 40] }, edit: cellEdit("writer", [212, 208, 176, 24], "r1.owner"), lod: [0.35, null] },
      ],
      items: [{ k: "rect", x: 0, y: 200, w: 400, h: 80, r: 8, fill: "base.surface", stroke: "base.grid", sw: 1 }, cellText("Docs", 12, 208), cellText("writer", 212, 208)] },
    { id: "E-30", kind: "kanban", layer: "zones", z: 2, v: 9, bbox: [500, 0, 800, 300], hit: { shape: "frame", box: [500, 0, 300, 300], band: 60 }, handles: "box", connect: false, frame: null, locked: false, author: "human", edit: null, container: { layout: "row", gap: 20, order: ["E-31"] }, items: [] },
    { id: "E-31", kind: "card", layer: "marks", z: 3, v: 8, bbox: [520, 80, 760, 160], hit: { shape: "rect", box: [520, 80, 240, 80] }, handles: "box", connect: true, frame: "E-30", block: "E-30", part: "c1", pin: "human", locked: false, author: "human", edit: null, items: [] },
    { id: "E-32", kind: "arrow", layer: "marks", z: 4, v: 6, bbox: [0, 0, 300, 10], hit: { shape: "line", points: [[0, 5], [300, 5]] }, handles: "ends", connect: false, frame: null, locked: false, author: "human", edit: null, items: [] },
  ];
  const scene = [
    { id: "E-20", type: "table", updated_seq: 7, author: "human" },
    { id: "E-30", type: "frame", block: "kanban", updated_seq: 9, author: "human" },
    { id: "E-31", type: "card", frame: "E-30", group: "E-30", updated_seq: 8, author: "human", pin: { by: "human", who: "human" } },
    { id: "E-32", type: "arrow", updated_seq: 6, author: "human", style: { route: "straight" } },
  ];

  async function mountPhase2({ writable }) {
    displayDoc = { ...displayDoc, version: 9, entries: phase2 };
    const store = createSceneStore();
    store.replace({ version: 9, elements: scene });
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    await act(async () => {
      root.render(React.createElement(Board, { team: "t", teamRow: { name: "t", viz: false, members: [] }, writable, store, bus: createBus(), visible: true, toast: vi.fn(), theme: "light", onFallback: vi.fn() }));
    });
    await act(async () => surface.props.onViewport({ w: 800, h: 600 }));
    return { store };
  }
  const part = (id, name) => phase2.find((e) => e.id === id).parts.find((p) => p.part === name);
  const click = async (selector) => act(async () => container.querySelector(selector).dispatchEvent(new MouseEvent("click", { bubbles: true })));
  const type = async (area, text) =>
    act(async () => {
      Object.getOwnPropertyDescriptor(HTMLTextAreaElement.prototype, "value").set.call(area, text);
      area.dispatchEvent(new Event("input", { bubbles: true }));
    });

  it("a double-click on a cell edits just that cell and sends edit {part} (P1)", async () => {
    await mountPhase2({ writable: true });
    await send(pointer("dblclick", 300, 220, { hit: "E-20", part: part("E-20", "r1.owner") }));
    const area = container.querySelector("textarea.v2-text-editor");
    expect(area.value).toBe("writer");
    // Only the cell's text leaves the drawing while it is typed over.
    expect(surface.props.preview.hide).toEqual(["E-20"]);
    const texts = surface.props.preview.ghost.filter((p) => p.k === "text").map((p) => p.lines[0].t);
    expect(texts).toEqual(["Docs"]);
    await type(area, "docs team");
    await act(async () => area.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", metaKey: true, bubbles: true })));
    expect(calls.post[0][1].ops).toEqual([{ op: "edit", id: "E-20", part: "r1.owner", text: "docs team", if_version: 7 }]);
  });

  it("hovering a part with an editor hands Surface the part to outline", async () => {
    await mountPhase2({ writable: true });
    await send(pointer("hover", 100, 220, { hit: "E-20", part: part("E-20", "r1.c1") }));
    expect(surface.props.hoverPart).toEqual({ id: "E-20", part: "r1.c1" });
    await send(pointer("hover", 900, 500));
    expect(surface.props.hoverPart).toBeNull();
  });

  it("Esc on a selected card selects its kanban; the next Esc clears (P10)", async () => {
    await mountPhase2({ writable: true });
    await send(pointer("down", 600, 100, { hit: "E-31" }));
    await send(pointer("up", 600, 100, { hit: "E-31" }));
    expect(surface.props.selection).toEqual(["E-31"]);
    await key("Escape");
    expect(surface.props.selection).toEqual(["E-30"]);
    await key("Escape");
    expect(surface.props.selection).toEqual([]);
  });

  it("the style bar pins and unpins (P4), and restyles an arrow's route (P7)", async () => {
    await mountPhase2({ writable: true });
    await send(pointer("down", 600, 100, { hit: "E-31" }));
    await send(pointer("up", 600, 100, { hit: "E-31" }));
    expect(container.querySelector('[data-style="pin"]')).toBeNull(); // already the operator's pin
    await click('[data-style="unpin"]');
    expect(calls.post.at(-1)[1].ops).toEqual([{ op: "unpin", ids: ["E-31"], if_version: 8 }]);
    expect(container.querySelector('[data-style^="route:"]')).toBeNull(); // no arrow selected
    await send(pointer("down", 100, 5, { hit: "E-32" }));
    await send(pointer("up", 100, 5, { hit: "E-32" }));
    expect(container.querySelector('[data-style="route:straight"]').className).toBe("on");
    await click('[data-style="route:orthogonal"]');
    expect(calls.post.at(-1)[1].ops).toEqual([{ op: "restyle", ids: ["E-32"], route: "orthogonal", if_version: 6 }]);
    await click('[data-style="pin"]');
    expect(calls.post.at(-1)[1].ops).toEqual([{ op: "pin", ids: ["E-32"], if_version: 6 }]);
  });

  it("opens the editor on a new element's first part when it has no text of its own", async () => {
    const { store } = await mountPhase2({ writable: true });
    await key("r");
    await send(pointer("down", 1000, 400));
    await send(pointer("up", 1000, 400));
    expect(calls.post[0][1].ops[0]).toMatchObject({ op: "shape", kind: "box", at: [1000, 400] });
    const made = { id: "E-9", kind: "card", layer: "marks", z: 9, v: 10, bbox: [1000, 400, 1240, 480], hit: { shape: "rect", box: [1000, 400, 240, 80] }, handles: "box", connect: true, frame: null, locked: false, author: "human", edit: null,
      parts: [{ part: "title", hit: { shape: "rect", box: [1016, 412, 208, 25] }, edit: cellEdit("", [1016, 412, 208, 25], "title") }], items: [] };
    displayDoc = { dl: 1, version: 10, since: 9, full: false, bbox: [0, 0, 1240, 480], upserts: [made], removes: [] };
    await act(async () => store.applyEvents([{ seq: 10, op: "shape", ids: ["E-9"], changes: [] }]));
    // From here on the server answers with the whole list at v10 (this stub serves one document).
    displayDoc = { dl: 1, version: 10, bbox: [0, 0, 1240, 480], layers: ["zones", "marks", "labels", "overlays"], palettes: { light: {}, dark: {} }, entries: [...phase2, made] };
    const area = container.querySelector("textarea.v2-text-editor");
    expect(area).not.toBeNull();
    await type(area, "Ship it");
    await act(async () => area.dispatchEvent(new KeyboardEvent("keydown", { key: "Enter", metaKey: true, bubbles: true })));
    expect(calls.post.at(-1)[1].ops).toEqual([{ op: "edit", id: "E-9", part: "title", text: "Ship it", if_version: 10 }]);
  });

  it("read-only (P11): no style bar, no part editor, and nothing is ever POSTed", async () => {
    await mountPhase2({ writable: false });
    await send(pointer("down", 600, 100, { hit: "E-31" }));
    await send(pointer("up", 600, 100, { hit: "E-31" }));
    expect(surface.props.selection).toEqual(["E-31"]);
    expect(container.querySelector(".v2-stylebar")).toBeNull();
    await send(pointer("hover", 300, 220, { hit: "E-20", part: part("E-20", "r1.owner") }));
    expect(surface.props.hoverPart).toBeNull();
    await send(pointer("dblclick", 300, 220, { hit: "E-20", part: part("E-20", "r1.owner") }));
    expect(container.querySelector("textarea.v2-text-editor")).toBeNull();
    for (const k of ["c", "s", "n", "Enter", "Escape", "Escape"]) await key(k);
    await send(pointer("down", 600, 100, { hit: "E-31" }));
    await send(pointer("move", 900, 100));
    await send(pointer("up", 900, 100));
    expect(calls.post).toEqual([]);
  });
});
