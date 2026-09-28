// @vitest-environment jsdom
// Surface in jsdom (canvas-v2-phase1.md 4.2, 5.2): the sample list draws every entry, once per layer
// it draws in; themes are palettes, never filters; selection, handles, hover, preview and the
// camera gestures behave as the interface says (6.2). Also the QA hook over a drawn Surface.
import fs from "node:fs";
import path from "node:path";
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeAll, describe, expect, test, vi } from "vitest";
import { Surface, isMouseWheel, wheelKind } from "./Surface.jsx";
import { audit, installQAHook } from "./qa.js";
import { entryLayers } from "./svgAttrs.js";

const load = (name) => JSON.parse(fs.readFileSync(path.resolve(import.meta.dirname, "__fixtures__", name), "utf8"));
const sample = load("sample.json");
const sink = load("kitchen-sink.json");
const CAM = { x: 0, y: 0, scale: 1 };

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
  const all = { dl: sample, theme: "light", camera: CAM, onCamera: () => {}, onViewport: () => {}, selection: [], hover: null, preview: null, panMode: false, showChips: false, writable: true, vizOn: false, team: "demo", urls: { asset: (n) => `/asset/${n}`, still: (n) => `/still/${n}`, viz: () => "", artifact: () => "" }, elementOf: () => null, onPointer: () => {}, onStill: () => {}, onRendered: () => {}, ...props };
  act(() => root.render(React.createElement(Surface, all)));
  const handle = {
    node,
    root,
    rerender(next) {
      Object.assign(all, next);
      act(() => root.render(React.createElement(Surface, { ...all })));
    },
  };
  mounted.push(handle);
  return handle;
}

const layerGroup = (node, layer) => node.querySelector(`svg g[data-layer="${layer}"]`);

describe("drawing", () => {
  test("every entry is drawn, once per layer it has items in", () => {
    const { node } = mount({});
    for (const entry of sample.entries) {
      const layers = entryLayers(entry);
      for (const layer of ["zones", "marks", "labels", "overlays"]) {
        const count = layerGroup(node, layer).querySelectorAll(`:scope > g[data-id="${entry.id}"]`).length;
        expect(count, `${entry.id} in ${layer}`).toBe(layers.has(layer) ? 1 : 0);
      }
    }
    expect(node.querySelector("svg").getAttribute("data-version")).toBe("3");
    expect(node.querySelector(".sv2-world").getAttribute("transform")).toBe("matrix(1 0 0 1 0 0)");
  });

  test("text is drawn from the lines as given, in the page faces", () => {
    const { node } = mount({});
    const group = layerGroup(node, "marks").querySelector('g[data-id="E-2"] g[font-size]');
    expect(group.getAttribute("font-family")).toBe('"Synapse Sans", "Arial Hebrew", "Geeza Pro"');
    expect(group.getAttribute("font-weight")).toBe("500");
    // On each <text>, where the browser honours it (its own style for <text> resets an inherited
    // white-space, which dropped leading indentation: QA 1 finding 4); the group carries none.
    expect(group.hasAttribute("xml:space")).toBe(false);
    for (const t of group.querySelectorAll("text")) expect(t.getAttribute("xml:space")).toBe("preserve");
    expect([...group.querySelectorAll("text")].map((t) => [t.getAttribute("x"), t.getAttribute("y"), t.textContent])).toEqual([["200", "206.5", "Checkout API"]]);
  });

  test("a right-to-left line keeps its anchor (unicode-bidi plaintext, no direction)", () => {
    const { node } = mount({ dl: sink });
    const line = [...node.querySelectorAll('g[data-id="E-4"] text')].find((t) => t.textContent === "שלום עולם");
    expect(line.getAttribute("unicode-bidi")).toBe("plaintext");
    expect(line.getAttribute("direction")).toBeNull();
  });

  test("dark is the dark palette: background and paints change, no filter anywhere", () => {
    const view = mount({});
    const fillOf = () => layerGroup(view.node, "marks").querySelector('g[data-id="E-2"] rect').getAttribute("fill");
    expect(fillOf()).toBe("#e8f2fe");
    view.rerender({ theme: "dark" });
    expect(fillOf()).toBe("#122640");
    expect(view.node.querySelector(".sv2-bg").getAttribute("fill")).toBe("#111214");
    expect(view.node.querySelector(".sv2-surface").style.background).toMatch(/rgb\(17, 18, 20\)|#111214/);
    for (const el of view.node.querySelectorAll("*")) {
      expect(el.getAttribute("filter") || "", el.tagName).not.toMatch(/invert/);
      expect(el.style?.filter || "", el.tagName).toBe("");
    }
    const css = fs.readFileSync(path.resolve(import.meta.dirname, "surface.css"), "utf8").replace(/\/\*[\s\S]*?\*\//g, "");
    expect(css).not.toMatch(/filter\s*:/);
  });

  test("an entry with nothing drawable shows its hit box dashed; unknown primitives draw nothing", () => {
    // (The one-time console warnings are module-wide; theme/palette.test.js covers warnOnce.)
    vi.spyOn(console, "warn").mockImplementation(() => {});
    const { node } = mount({ dl: sink });
    const g = layerGroup(node, "marks").querySelector('g[data-id="E-12"]');
    expect(g).not.toBeNull();
    const rect = g.querySelector("rect");
    expect(rect.getAttribute("fill")).toBe("none");
    expect(rect.getAttribute("stroke-dasharray")).toBeTruthy();
  });

  test("assets use the page's urls; a slot without a live renderer shows its still or fallback", () => {
    const { node } = mount({ dl: sink });
    expect(node.querySelector('g[data-id="E-9"] image').getAttribute("href")).toMatch(/^\/asset\/[0-9a-f]{32}\.png$/);
    expect(node.querySelector('g[data-id="E-11"] [data-slot="viz"] image').getAttribute("href")).toBe("/still/E-11-v1.png");
    expect(node.querySelector('g[data-id="E-10"] [data-slot="chart"] text').textContent).toBe("chart");
  });

  test("a null list draws an empty canvas", () => {
    const { node } = mount({ dl: null });
    expect(node.querySelectorAll("g[data-id]")).toHaveLength(0);
    expect(node.querySelector(".sv2-bg")).not.toBeNull();
  });

  test("onRendered gets the version and the root svg", () => {
    const onRendered = vi.fn();
    const { node } = mount({ onRendered });
    expect(onRendered).toHaveBeenCalledWith({ version: 3, root: node.querySelector("svg") });
  });
});

describe("selection, hover and preview", () => {
  const ui = (node) => node.querySelector('g[data-layer="ui"]');

  test("a selected box: outline 4 px outside its hit box and eight handles when writable", () => {
    const { node } = mount({ selection: ["E-2"], camera: { x: 0, y: 0, scale: 2 } });
    const outline = ui(node).querySelector(":scope > rect");
    expect(outline.getAttribute("stroke")).toBe("#6e56cf");
    expect(outline.getAttribute("x")).toBe(String(120 - 2)); // 4 px at scale 2
    expect(ui(node).querySelectorAll("[data-handle]")).toHaveLength(8);
  });

  test("read-only: outline but no handles or connection points", () => {
    const { node } = mount({ selection: ["E-2"], hover: "E-3", writable: false });
    expect(ui(node).querySelectorAll("[data-handle]")).toHaveLength(0);
    expect(ui(node).querySelectorAll(".sv2-connector")).toHaveLength(0);
  });

  test("an arrow offers its two ends", () => {
    const { node } = mount({ selection: ["E-4"] });
    expect([...ui(node).querySelectorAll("[data-handle]")].map((h) => h.getAttribute("data-handle"))).toEqual(["start", "end"]);
  });

  test("hover: outline, author chip and connection points", () => {
    const { node } = mount({ hover: "E-3" });
    expect(ui(node).querySelectorAll(".sv2-connector")).toHaveLength(4);
    expect(ui(node).querySelector(".sv2-chip text").textContent).toContain("AL");
    expect(ui(node).querySelector(".sv2-chip text").textContent).toContain("E-3");
  });

  test("preview: move translates, hide hides, ghost draws", () => {
    const { node } = mount({ preview: { move: { ids: ["E-2"], by: [20, -10] }, hide: ["E-3"], ghost: [{ k: "line", points: [[0, 0], [10, 10]], stroke: "base.ink", sw: 2 }] } });
    expect(layerGroup(node, "marks").querySelector('g[data-id="E-2"]').getAttribute("transform")).toBe("translate(20 -10)");
    expect(node.querySelector('g[data-id="E-3"]')).toBeNull();
    expect(node.querySelector(".sv2-ghost polyline").getAttribute("points")).toBe("0,0 10,10");
  });
});

describe("gestures", () => {
  function pointer(node, type, init) {
    const Ctor = typeof PointerEvent === "function" ? PointerEvent : MouseEvent;
    node.querySelector(".sv2-surface").dispatchEvent(new Ctor(type, { bubbles: true, cancelable: true, pointerId: 1, ...init }));
  }

  test("a left press reaches onPointer with the hit under it", () => {
    const onPointer = vi.fn();
    const { node } = mount({ onPointer, selection: ["E-2"] });
    act(() => pointer(node, "pointerdown", { button: 0, buttons: 1, clientX: 200, clientY: 200 }));
    expect(onPointer).toHaveBeenCalledTimes(1);
    const ev = onPointer.mock.calls[0][0];
    expect(ev).toMatchObject({ type: "down", world: [200, 200], screen: [200, 200], scale: 1, hit: "E-2", handle: null });
    act(() => pointer(node, "pointerup", { button: 0, clientX: 280, clientY: 240 }));
    expect(onPointer.mock.calls[1][0]).toMatchObject({ type: "up", handle: { id: "E-2", handle: "se" } });
  });

  test("panMode: a left drag pans through onCamera and never reaches onPointer", () => {
    const onPointer = vi.fn();
    const onCamera = vi.fn();
    const { node } = mount({ onPointer, onCamera, panMode: true });
    act(() => pointer(node, "pointerdown", { button: 0, buttons: 1, clientX: 100, clientY: 100 }));
    act(() => pointer(node, "pointermove", { button: 0, buttons: 1, clientX: 130, clientY: 90 }));
    expect(onPointer).not.toHaveBeenCalled();
    expect(onCamera).toHaveBeenCalledWith({ x: -30, y: 10, scale: 1 });
  });

  test("the wheel zooms about the cursor; ctrl+wheel too; a two-finger scroll pans", () => {
    const onCamera = vi.fn();
    const { node } = mount({ onCamera });
    const wheel = (init) => node.querySelector(".sv2-surface").dispatchEvent(new WheelEvent("wheel", { bubbles: true, cancelable: true, ...init }));
    act(() => wheel({ deltaY: -100, clientX: 50, clientY: 50 }));
    let cam = onCamera.mock.calls.at(-1)[0];
    expect(cam.scale).toBeGreaterThan(1);
    expect((50 / cam.scale + cam.x).toFixed(9)).toBe("50.000000000"); // the cursor's world point stays
    act(() => wheel({ deltaY: 10, ctrlKey: true, clientX: 0, clientY: 0 }));
    expect(onCamera.mock.calls.at(-1)[0].scale).toBeLessThan(1);
    act(() => wheel({ deltaX: 12.5, deltaY: 7.5 }));
    cam = onCamera.mock.calls.at(-1)[0];
    expect(cam).toEqual({ x: 12.5, y: 7.5, scale: 1 });
  });
});

describe("zoom settling (QA 1 finding 3)", () => {
  test("wheel events at the zoom limit still let the board settle and redraw crisply", () => {
    vi.useFakeTimers();
    try {
      let camera = { x: 0, y: 0, scale: 1 };
      const handle = mount({ camera, onCamera: (next) => { camera = next; } });
      const surface = handle.node.querySelector(".sv2-surface");
      const svg = () => handle.node.querySelector("svg.sv2-svg");
      const wheel = () => act(() => surface.dispatchEvent(new WheelEvent("wheel", { bubbles: true, cancelable: true, deltaY: -100, ctrlKey: true, clientX: 10, clientY: 10 })));
      wheel();
      handle.rerender({ camera });
      expect(svg().getAttribute("data-settled")).toBe("0");
      // At the limit the camera stops changing, but the wheel keeps extending the gesture.
      for (let i = 0; i < 10; i += 1) {
        act(() => vi.advanceTimersByTime(40));
        wheel();
      }
      act(() => vi.advanceTimersByTime(2000));
      expect(svg().getAttribute("data-settled")).toBe("1");
    } finally {
      vi.useRealTimers();
    }
  });
});

describe("wheel classification", () => {
  const ev = (deltaY, wheelDeltaY, extra = {}) => ({ deltaMode: 0, deltaX: 0, deltaY, wheelDeltaY, ...extra });
  test("notches are a mouse wheel, at any device pixel ratio", () => {
    expect(isMouseWheel(ev(-120, 120))).toBe(true);
    expect(isMouseWheel(ev(100, -120))).toBe(true);
    expect(isMouseWheel(ev(-60, 60), 2)).toBe(true); // Chrome divides by the ratio under emulation
    expect(isMouseWheel(ev(-60, 60), 1)).toBe(false);
    expect(isMouseWheel(ev(3, undefined, { deltaMode: 1 }))).toBe(true);
  });
  test("trackpad scrolls pan", () => {
    expect(isMouseWheel(ev(4.5, -13))).toBe(false);
    expect(isMouseWheel(ev(-120, 120, { deltaX: 2 }))).toBe(false);
    expect(isMouseWheel(ev(12, undefined))).toBe(false);
  });
  test("a burst that started as a trackpad stays one", () => {
    const state = { kind: null, at: 0 };
    expect(wheelKind(state, ev(4.5, -13), 1000)).toBe("trackpad");
    expect(wheelKind(state, ev(-40, 120), 1050)).toBe("trackpad"); // notch-like, same burst
    expect(wheelKind(state, ev(-120, 120), 1500)).toBe("mouse"); // a new burst
    expect(wheelKind(state, ev(-120, 120), 1550)).toBe("mouse");
  });
});

describe("zoom settling", () => {
  function Controlled({ onReady }) {
    const [cam, setCam] = React.useState(CAM);
    onReady(setCam);
    return React.createElement(Surface, { dl: sample, theme: "light", camera: cam, onCamera: setCam, onViewport: () => {}, selection: [], hover: null, preview: null, panMode: false, showChips: false, writable: true, vizOn: false, team: "demo", urls: {}, elementOf: () => null, onPointer: () => {}, onStill: () => {}, onRendered: () => {} });
  }

  test("a wheel zoom is presented by a CSS transform, then drawn once it settles", async () => {
    const node = document.createElement("div");
    document.body.appendChild(node);
    const root = createRoot(node);
    let setCam = null;
    act(() => root.render(React.createElement(Controlled, { onReady: (f) => (setCam = f) })));
    mounted.push({ root, node });
    const svg = () => node.querySelector("svg");
    const world = () => node.querySelector(".sv2-world").getAttribute("transform");
    act(() => node.querySelector(".sv2-surface").dispatchEvent(new WheelEvent("wheel", { bubbles: true, cancelable: true, deltaY: -100, clientX: 0, clientY: 0 })));
    expect(svg().getAttribute("data-settled")).toBe("0");
    expect(world()).toBe("matrix(1 0 0 1 0 0)"); // still drawn at scale 1
    expect(node.querySelector(".sv2-zoomer").style.transform).toMatch(/^matrix\(1\.16/);
    await act(() => new Promise((r) => setTimeout(r, 250)));
    expect(svg().getAttribute("data-settled")).toBe("1");
    expect(world()).toMatch(/^matrix\(1\.16/);
    expect(node.querySelector(".sv2-zoomer").style.transform).toBe("");
    // A camera the parent sets is drawn at once.
    act(() => setCam({ x: 10, y: 0, scale: 2 }));
    expect(svg().getAttribute("data-settled")).toBe("1");
    expect(world()).toBe("matrix(2 0 0 2 -20 0)");
  });
});

describe("QA hook", () => {
  test("audit reports a label whose lines take more height than its box (QA phase 2, F3)", () => {
    const NS = "http://www.w3.org/2000/svg";
    const make = (tag, attrs = {}) => {
      const node = document.createElementNS(NS, tag);
      for (const [k, v] of Object.entries(attrs)) node.setAttribute(k, v);
      return node;
    };
    const root = make("svg");
    const layer = make("g", { "data-layer": "marks" });
    const entry = make("g", { "data-id": "E-9" });
    const block = make("g", { "data-box-w": "160", "data-box-h": "140", "data-lines-h": "160" });
    const text = make("text");
    text.textContent = "a line";
    block.appendChild(text);
    entry.appendChild(block);
    layer.appendChild(entry);
    root.appendChild(layer);
    expect(audit(root)).toEqual([{ id: "E-9", overflowPx: 20, vertical: true }]);
    block.setAttribute("data-lines-h", "140");
    expect(audit(root)).toEqual([]);
  });

  test("lines, audit, fit and ready over a drawn Surface", () => {
    let cam = CAM;
    const { node } = mount({});
    const svg = node.querySelector("svg");
    // jsdom has no layout: a text node's length is its character count * 10 units.
    const proto = Object.getPrototypeOf(svg.querySelector("text"));
    proto.getComputedTextLength = function () {
      return this.textContent.length * 10;
    };
    const uninstall = installQAHook({ getDL: () => sample, getCamera: () => cam, setCamera: (c) => (cam = c), getRoot: () => svg, getViewport: () => ({ w: 1000, h: 500 }) });
    try {
      const qa = window.__synapseV2;
      expect(qa.version()).toBe(3);
      expect(qa.dl()).toBe(sample);
      expect(qa.lines()["E-2"]).toEqual(["Checkout API"]);
      expect(qa.lines()["E-4"]).toEqual(["writes"]);
      // "Checkout API" = 120 in a 128 box; "Orders DB" 90 in 128; "writes" = 60 in 48: 12 over.
      expect(qa.audit()).toEqual([{ id: "E-4", overflowPx: 12 }]);
      // A line whose whitespace the browser collapsed (a lost indent, QA 1 finding 4) is reported.
      proto.getNumberOfChars = function () {
        return this.textContent === "Checkout API" ? 10 : this.textContent.length;
      };
      expect(qa.audit()).toEqual([{ id: "E-4", overflowPx: 12 }, { id: "E-2", overflowPx: 0, collapsed: 2 }]);
      delete proto.getNumberOfChars;
      const fitted = qa.fit();
      expect(fitted).toEqual(cam);
      expect(fitted.scale).toBeGreaterThan(1);
      expect(qa.ready()).toBe(true);
    } finally {
      delete proto.getComputedTextLength;
      uninstall();
    }
    expect(window.__synapseV2).toBeUndefined();
  });
});

describe("screen overlay (canvas-v2-phase5.md 12.1)", () => {
  test("drawn above the SVG world, outside the zoomer, under the GL host and the HTML overlay; never takes a pointer", () => {
    const { node } = mount({ screenOverlay: React.createElement("div", { className: "probe" }, "halo") });
    const surface = node.querySelector(".sv2-surface");
    const layer = surface.querySelector(":scope > .sv2-screen");
    expect(layer).not.toBeNull();
    expect(layer.querySelector(".probe").textContent).toBe("halo");
    const order = [...surface.children].map((el) => el.className);
    expect(order.indexOf("sv2-zoomer")).toBeLessThan(order.indexOf("sv2-screen"));
    expect(order.indexOf("sv2-screen")).toBeLessThan(order.indexOf("sv2-gl-host"));
    expect(order.indexOf("sv2-screen")).toBeLessThan(order.indexOf("sv2-overlay"));
    expect(node.querySelector(".sv2-zoomer .probe")).toBeNull();
    const css = fs.readFileSync(path.resolve(import.meta.dirname, "surface.css"), "utf8");
    expect(css).toMatch(/\.sv2-screen \{[^}]*pointer-events: none;/);
    expect(css).toMatch(/\.sv2-screen \* \{[^}]*pointer-events: none;/);
  });

  test("absent when not given", () => {
    const { node } = mount({});
    expect(node.querySelector(".sv2-screen")).toBeNull();
  });
});
