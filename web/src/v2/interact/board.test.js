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
    for (const tool of ["r", "o", "d", "n", "t", "a", "p", "f", "v"]) {
      await key(tool);
      for (const events of sequences) for (const ev of events) await send(ev);
    }
    for (const [k, extra] of [["Delete", {}], ["Backspace", {}], ["ArrowLeft", {}], ["Enter", {}], ["z", { metaKey: true }], ["a", { metaKey: true }], ["Delete", {}]]) await key(k, extra);
    expect(container.querySelector("textarea.v2-text-editor")).toBeNull();
    expect(calls.post).toEqual([]);
    expect(calls.bytes).toEqual([]);
  });
});
