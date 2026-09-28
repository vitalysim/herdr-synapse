// @vitest-environment jsdom
// The Phase 3-4 slot integration (canvas-v2-phase3-4.md 1.4, WW-2 to WW-5): the writers' still rule,
// the slot table (chart, mermaid, scene3d, viz equal to SLOT_KINDS), entering, the SVG underlay of a
// scene3d slot, the GL host, a still per view, and registerQA.
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeAll, describe, expect, test, vi } from "vitest";
import { Surface } from "./Surface.jsx";
import { installQAHook, registerQA } from "./qa.js";
import { slotNode, usesStill } from "./svgAttrs.js";
import { SLOT_RENDERERS, enterable, enterableSlotAt } from "./slots/index.js";
import { SLOT_KINDS } from "./version.js";
import { loadScene3DSlot } from "../scene3d/index.js";

beforeAll(async () => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  // The scene3d slot's behaviour loads with the first scene (scene3d/lazySlot.js); load it up front.
  await loadScene3DSlot();
});

const ctxOf = (theme) => ({ theme, palette: {}, scale: 1, url: (src) => `still:${src.still}`, defs: null, families: {} });
const FALLBACK = [{ k: "rect", x: 0, y: 0, w: 10, h: 10, fill: "#ffffff" }];

describe("the still rule (1.4)", () => {
  test("a still draws in light; in dark only when the fallback is not a faithful drawing", () => {
    const drawn = { k: "slot", slot: "chart", x: 0, y: 0, w: 10, h: 10, still: "E-1-v2.png", drawn: true, fallback: FALLBACK };
    const card = { ...drawn, drawn: undefined };
    expect(usesStill(drawn, "light")).toBe(true);
    expect(usesStill(drawn, "dark")).toBe(false);
    expect(usesStill(card, "dark")).toBe(true);
    expect(usesStill({ ...drawn, still: null }, "light")).toBe(false);
    expect(slotNode(drawn, ctxOf("light"))[2][0][0]).toBe("image");
    expect(slotNode(drawn, ctxOf("dark"))[2][0][0]).toBe("rect");
    expect(slotNode(card, ctxOf("dark"))[2][0][0]).toBe("image");
  });
});

describe("the slot table", () => {
  test("its kinds are SLOT_KINDS; scene3d is an underlay html slot entered because it is GL", () => {
    expect(Object.keys(SLOT_RENDERERS).sort()).toEqual([...SLOT_KINDS].sort());
    expect(SLOT_RENDERERS.scene3d).toMatchObject({ mode: "html", underlay: true, enter: "gl" });
    expect(typeof SLOT_RENDERERS.chart.Live).toBe("function");
    expect(enterable({ k: "slot", slot: "scene3d", gl: true })).toBe(true);
    expect(enterable({ k: "slot", slot: "chart" })).toBe(true);
    expect(enterable({ k: "slot", slot: "viz" })).toBe(false);
    expect(enterable({ k: "slot", slot: "mermaid" })).toBe(false);
  });

  test("enterableSlotAt finds the slot under a point, inside groups", () => {
    const slot = { k: "slot", slot: "scene3d", gl: true, x: 10, y: 40, w: 100, h: 60 };
    const entry = { id: "E-1", items: [{ k: "rect", x: 0, y: 0, w: 120, h: 110 }, { k: "group", items: [slot] }] };
    expect(enterableSlotAt(entry, [50, 60])).toBe(slot);
    expect(enterableSlotAt(entry, [50, 20])).toBeNull();
    expect(enterableSlotAt(entry)).toBe(slot);
  });
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
  const all = { theme: "light", camera: { x: 0, y: 0, scale: 1 }, onCamera: () => {}, onViewport: () => {}, selection: [], team: "t", urls: { asset: (n) => `/a/${n}`, still: (n) => `/s/${n}` }, onPointer: () => {}, onStill: () => {}, onRendered: () => {}, ...props };
  act(() => root.render(React.createElement(Surface, all)));
  mounted.push({ node, root });
  return node;
}

const SCENE_DL = {
  dl: 1,
  version: 3,
  bbox: [0, 0, 400, 300],
  entries: [
    {
      id: "E-9",
      v: 3,
      layer: "marks",
      bbox: [0, 0, 400, 300],
      hit: { shape: "box", box: [0, 0, 400, 300] },
      items: [
        { k: "rect", x: 0, y: 0, w: 400, h: 300, fill: "#ffffff" },
        { k: "slot", slot: "scene3d", x: 10, y: 40, w: 380, h: 250, gl: true, drawn: true, still: "E-9-v3-iso.png", views: { iso: "E-9-v3-iso.png", front: null, top: null }, fallback: [{ k: "rect", x: 10, y: 40, w: 380, h: 250, fill: "#eeeeee" }] },
      ],
    },
  ],
};

describe("Surface with a scene3d slot", () => {
  test("the SVG keeps the drawing beneath (the still without one), and the GL host sits between", () => {
    const element = { id: "E-9", type: "scene3d", solved: {}, objects: [] };
    // Beneath the canvas, a faithful drawing wins over the still in both themes: it is exact at
    // every size, and it is what shows while the canvas cannot draw.
    const light = mount({ dl: SCENE_DL, elementOf: () => element });
    expect(light.querySelector('svg g[data-slot="scene3d"] image')).toBeNull();
    expect(light.querySelector('svg g[data-slot="scene3d"] rect')).toBeTruthy();
    const undrawn = JSON.parse(JSON.stringify(SCENE_DL));
    undrawn.entries[0].items[1].drawn = false;
    const still = mount({ dl: undrawn, elementOf: () => element });
    expect(still.querySelector('svg g[data-slot="scene3d"] image').getAttribute("href")).toBe("/s/E-9-v3-iso.png");
    expect(light.querySelector(".sv2-gl-host[data-gl-host]")).toBeTruthy();
    expect(light.querySelector('.sv2-html .sv2-scene3d[data-id="E-9"]')).toBeTruthy();
    const children = [...light.querySelector(".sv2-surface").children].map((c) => c.className);
    expect(children.indexOf("sv2-gl-host")).toBeGreaterThan(children.indexOf("sv2-zoomer"));
    expect(children.indexOf("sv2-gl-host")).toBeLessThan(children.indexOf("sv2-html"));
    const dark = mount({ dl: SCENE_DL, theme: "dark", elementOf: () => element });
    expect(dark.querySelector('svg g[data-slot="scene3d"] image')).toBeNull();
    expect(dark.querySelector('svg g[data-slot="scene3d"] rect')).toBeTruthy();
  });

  test("the placeholder takes pointer events only while entered, with Save view on writable pages", () => {
    const element = { id: "E-9", type: "scene3d", solved: {}, objects: [] };
    const idle = mount({ dl: SCENE_DL, elementOf: () => element, writable: true, onOps: () => {} });
    expect(idle.querySelector(".sv2-scene3d").style.pointerEvents).toBe("none");
    expect(idle.querySelector(".sv2-scene3d").getAttribute("aria-label")).toBe("3D scene, 0 objects");
    const named = mount({ dl: SCENE_DL, elementOf: () => ({ ...element, text: "Rack", objects: [{ id: "a", label: "gpu-1" }, { id: "b" }] }) });
    expect(named.querySelector(".sv2-scene3d").getAttribute("aria-label")).toBe('3D scene "Rack", 2 objects: gpu-1');
    const entered = mount({ dl: SCENE_DL, elementOf: () => element, writable: true, entered: "E-9", onEnter: () => {}, onOps: () => {} });
    expect(entered.querySelector(".sv2-scene3d").style.pointerEvents).toBe("auto");
    expect(entered.querySelector(".sv2-save-view")).toBeTruthy();
    const readOnly = mount({ dl: SCENE_DL, elementOf: () => element, writable: false, entered: "E-9", onEnter: () => {} });
    expect(readOnly.querySelector(".sv2-save-view")).toBeNull();
  });
});

describe("registerQA", () => {
  test("adds window.__synapseV2[name] now and on every later install; built-ins are never replaced", () => {
    const fn = vi.fn(() => ({ ok: 1 }));
    const off = registerQA("probe", fn);
    const uninstall = installQAHook({ getDL: () => null, getCamera: () => null, setCamera: () => {}, getRoot: () => null, getViewport: () => ({ w: 0, h: 0 }) });
    expect(window.__synapseV2.probe()).toEqual({ ok: 1 });
    registerQA("ready", () => "hijack");
    expect(window.__synapseV2.ready()).toBe(false);
    off();
    expect(window.__synapseV2.probe).toBeUndefined();
    uninstall();
    expect(() => registerQA("bad name", fn)).toThrow();
    expect(typeof window.__synapseV2).toBe("undefined");
  });
});
