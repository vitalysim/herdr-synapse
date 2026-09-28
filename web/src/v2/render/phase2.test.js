// @vitest-environment jsdom
// The renderer side of canvas v2 phase 2 (canvas-v2-phase2.md 6.1 to 6.3) over the hand-written
// Phase 2 display list: inline parts honour lod (partAt), the new entry fields draw generically (a
// pin glyph, a tip as an SVG <title>, the hovered part's outline) and semantic zoom needs nothing
// but lod: card bodies and cells give way to skeleton bars below 0.35, and below 0.15 only the
// top-level containers' titles remain, in the "above" form.
import fs from "node:fs";
import path from "node:path";
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeAll, describe, expect, test, vi } from "vitest";
import { createIndex } from "./cull.js";
import { hitTest, partAt, partOf, partsOf } from "./hit.js";
import { lodVisible } from "./lod.js";
import { Surface } from "./Surface.jsx";
import { toSVGString } from "./svgString.js";

const dl = JSON.parse(fs.readFileSync(path.resolve(import.meta.dirname, "__fixtures__", "phase2.json"), "utf8"));
const index = createIndex(dl);
const byId = (id) => dl.entries.find((e) => e.id === id);
const centre = (box) => [box[0] + box[2] / 2, box[1] + box[3] / 2];

beforeAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
});

let mounted = [];
afterEach(() => {
  for (const { root, node } of mounted) {
    act(() => root.unmount());
    node.remove();
  }
  mounted = [];
});

function mount(props) {
  const node = document.createElement("div");
  document.body.appendChild(node);
  const root = createRoot(node);
  const all = { dl, theme: "light", camera: { x: 0, y: 0, scale: 1 }, onCamera: () => {}, onViewport: () => {}, selection: [], hover: null, hoverPart: null, preview: null, panMode: false, showChips: false, writable: true, vizOn: false, team: "demo", urls: {}, elementOf: () => null, onPointer: () => {}, onStill: () => {}, onRendered: () => {}, ...props };
  act(() => root.render(React.createElement(Surface, all)));
  mounted.push({ node, root });
  return node;
}

// The text lines an entry draws, as the page shows them.
const linesOf = (node, id) => [...node.querySelectorAll(`g[data-layer] > g[data-id="${id}"] text`)].map((t) => t.textContent);

describe("parts (W-a)", () => {
  test("partsOf keeps well-formed parts in order; partOf finds one by name", () => {
    expect(partsOf(byId("E-20")).map((p) => p.part)).toEqual(["h.c1", "h.owner", "r1.c1", "r1.owner", "r2.c1", "r2.owner"]);
    expect(partsOf({ parts: [null, { part: "" }, { part: "x" }, { part: "y", hit: { shape: "rect", box: [0, 0, 1, 1] } }] }).map((p) => p.part)).toEqual(["y"]);
    expect(partOf(index, "E-20", "r2.owner").edit.value).toBe("pm");
    expect(partOf(index, "E-20", "nope")).toBeNull();
  });

  test("partAt finds the cell under the point, only where it is drawn at that scale", () => {
    const cell = partOf(index, "E-20", "r1.owner");
    const pt = centre(cell.hit.box);
    expect(hitTest(index, pt, 1)).toBe("E-20");
    expect(partAt(index, "E-20", pt, 1).part).toBe("r1.owner");
    expect(partAt(index, "E-20", pt, 0.3)).toBeNull(); // cells are skeletons below 0.35
    const header = partOf(index, "E-20", "h.owner");
    expect(partAt(index, "E-20", centre(header.hit.box), 0.3).part).toBe("h.owner"); // the header stays
    expect(partAt(index, "E-20", [5000, 5000], 1)).toBeNull();
    expect(partAt(index, "E-41", centre(byId("E-41").hit.box), 1)).toBeNull();
  });

  test("a card's title and body are parts; the body gives way below 0.35", () => {
    const body = partOf(index, "E-12", "body");
    expect(partAt(index, "E-12", centre(body.hit.box), 1).part).toBe("body");
    expect(partAt(index, "E-12", centre(body.hit.box), 0.2)).toBeNull();
    expect(partAt(index, "E-12", centre(partOf(index, "E-12", "title").hit.box), 0.2).part).toBe("title");
  });

  test("a press reports the part under it with the hit", () => {
    const onPointer = vi.fn();
    const node = mount({ onPointer });
    const surface = node.querySelector(".sv2-surface");
    const [x, y] = centre(partOf(index, "E-20", "r2.owner").hit.box);
    const Ctor = typeof PointerEvent === "function" ? PointerEvent : MouseEvent;
    act(() => surface.dispatchEvent(new Ctor("pointerdown", { bubbles: true, cancelable: true, pointerId: 1, button: 0, buttons: 1, clientX: x, clientY: y })));
    const ev = onPointer.mock.calls[0][0];
    expect(ev).toMatchObject({ type: "down", hit: "E-20" });
    expect(ev.part.part).toBe("r2.owner");
  });

  test("the hovered part is outlined, and not where it is not drawn", () => {
    const node = mount({ hover: "E-20", hoverPart: { id: "E-20", part: "r1.owner" } });
    const outline = node.querySelector('.sv2-ui rect.sv2-part[data-part="r1.owner"]');
    expect(outline).not.toBeNull();
    expect(Number(outline.getAttribute("width"))).toBe(200);
    const far = mount({ camera: { x: 0, y: 0, scale: 0.2 }, hover: "E-20", hoverPart: { id: "E-20", part: "r1.owner" } });
    expect(far.querySelector(".sv2-ui rect.sv2-part")).toBeNull();
  });
});

describe("pins (W-b) and tips (W-d)", () => {
  test("a selected or hovered pinned entry shows a glyph, in the pin's colour", () => {
    const node = mount({ selection: ["E-12"], hover: "E-42" });
    const human = node.querySelector('.sv2-ui .sv2-pin[data-pin-for="E-12"]');
    const agent = node.querySelector('.sv2-ui .sv2-pin[data-pin-for="E-42"]');
    expect(human.getAttribute("data-pin")).toBe("human");
    expect(agent.getAttribute("data-pin")).toBe("agent");
    expect(human.querySelector("circle").getAttribute("stroke")).toBe(dl.palettes.light["base.selection"]);
    expect(agent.querySelector("circle").getAttribute("stroke")).toBe(dl.palettes.light["base.ink_muted"]);
    // Drawn at a fixed screen size: the glyph's group scales by 1 / scale.
    const zoomed = mount({ camera: { x: 0, y: 0, scale: 2 }, selection: ["E-12"] });
    expect(zoomed.querySelector(".sv2-pin").getAttribute("transform")).toMatch(/scale\(0\.5\)/);
  });

  test("no glyph for an entry without a pin, or one neither selected nor hovered", () => {
    const node = mount({ selection: ["E-41"] });
    expect(node.querySelector(".sv2-pin")).toBeNull();
  });

  test("a tip is one SVG <title> on the entry's first drawn group, and the canonical SVG has none", () => {
    const node = mount({});
    const groups = [...node.querySelectorAll('g[data-layer] > g[data-id="E-42"]')];
    const titles = groups.flatMap((g) => [...g.querySelectorAll(":scope > title")]);
    expect(titles.map((t) => t.textContent)).toEqual(["Postgres 17, primary in eu-west"]);
    expect(node.querySelectorAll('g[data-id="E-41"] title')).toHaveLength(0);
    expect(toSVGString(dl, { theme: "light" })).not.toContain("<title>");
    // No drawn line: the QA hook reads <text> nodes only, and a tip is not one.
    expect(linesOf(node, "E-42")).toEqual(["Orders DB"]);
  });

  test("a tip longer than 500 characters is cut", () => {
    const long = { ...dl, entries: dl.entries.map((e) => (e.id === "E-42" ? { ...e, tip: "x".repeat(700) } : e)) };
    const node = mount({ dl: long });
    const title = node.querySelector('g[data-id="E-42"] > title');
    expect(title.textContent).toHaveLength(500);
    expect(title.textContent.endsWith("…")).toBe(true);
  });
});

describe("semantic zoom (W-h: lod only)", () => {
  const bodies = (node) => linesOf(node, "E-12");
  const skeletons = (node, id) => node.querySelectorAll(`g[data-layer] > g[data-id="${id}"] rect[height="8"]`).length;

  test("full detail at 1: bodies and cells, no skeletons", () => {
    const node = mount({});
    expect(bodies(node)).toEqual(["Rotate API keys", "Before the beta"]);
    expect(linesOf(node, "E-20")).toContain("writer");
    expect(skeletons(node, "E-12")).toBe(0);
  });

  test("titles at 0.3: card bodies and cells become skeleton bars, titles and the header stay", () => {
    const node = mount({ camera: { x: 0, y: 0, scale: 0.3 } });
    expect(bodies(node)).toEqual(["Rotate API keys"]);
    expect(skeletons(node, "E-12")).toBe(1);
    expect(linesOf(node, "E-20")).toEqual(["Risk", "Owner"]);
    expect(skeletons(node, "E-20")).toBe(4);
    // Containers show their title above them once the band title is under 12 px.
    expect(linesOf(node, "E-10")).toEqual(["Launch work"]);
  });

  test("overview at 0.1: silhouettes and top-level titles only", () => {
    const node = mount({ camera: { x: 0, y: 0, scale: 0.1 } });
    const drawn = Object.fromEntries(dl.entries.map((e) => [e.id, linesOf(node, e.id)]));
    // The fixture's cards keep their title at every scale (a Python LOD change would drop it); the
    // bodies, cells and nested titles are gone, and only the top-level containers name themselves.
    expect(drawn["E-12"]).not.toContain("Before the beta");
    expect(drawn["E-11"]).toEqual([]);
    expect(drawn["E-10"]).toEqual(["Launch work"]);
    expect(drawn["E-40"]).toEqual(["Checkout flow"]);
    expect(skeletons(node, "E-12")).toBe(0);
  });

  test("lodVisible is the one rule the page and parts share", () => {
    const part = partOf(index, "E-12", "body");
    expect([1, 0.35, 0.34, 0.1].map((s) => lodVisible(part, s))).toEqual([true, true, false, false]);
  });
});

describe("the QA hook's phase 2 helpers", () => {
  test("zoom(scale, at) centres the view on `at` at that scale (clamped); selection() reads the page's", async () => {
    const { installQAHook } = await import("./qa.js");
    let cam = { x: 0, y: 0, scale: 1 };
    const off = installQAHook({ getDL: () => dl, getCamera: () => cam, setCamera: (next) => { cam = next; }, getRoot: () => null, getViewport: () => ({ w: 800, h: 600 }), getSelection: () => ["E-12"] });
    expect(window.__synapseV2.zoom(0.5, [400, 300])).toEqual({ x: -400, y: -300, scale: 0.5 });
    expect(cam.scale).toBe(0.5);
    expect(window.__synapseV2.zoom(100).scale).toBe(8);
    expect(window.__synapseV2.zoom(0)).toBeNull();
    expect(window.__synapseV2.selection()).toEqual(["E-12"]);
    off();
    expect(window.__synapseV2).toBeUndefined();
  });
});
