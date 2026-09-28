// @vitest-environment jsdom
// slot.jsx: the chart slot (canvas-v2-phase3-4.md 2.10, D17, 8.3). The fallback shows at once and
// the static picture replaces it; the still is posted once per (id, v), always drawn light; a
// read-only page posts nothing; a failure keeps the fallback and reports itself to the QA hook.
// The static renderer is a stand-in here (ECharts in jsdom has no canvas to measure text with);
// render.test.js and static.test.js draw for real.
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeAll, beforeEach, describe, expect, test, vi } from "vitest";

// render.js is the lazy half slot.jsx imports on its first chart: pictureOf({team, element, prim,
// theme, palette, w, h}) fetches the doc and draws.
vi.mock("./render.js", () => ({
  pictureOf: vi.fn(async ({ element, theme }) => ({ engine: element.engine, kind: "svg", dataURL: `data:image/svg+xml;base64,${btoa(theme)}`, labelOverlaps: 0, texts: ["$0", "revenue"] })),
}));
vi.mock("../../canvas/renderers.js", () => ({ rasterize: vi.fn(async () => new Blob(["png"], { type: "image/png" })), svgToDataURL: (s) => s, fitSVG: (s) => s, renderChart: vi.fn() }));

const { pictureOf } = await import("./render.js");
const { clearPictures } = await import("./picture.js");
const { rasterize } = await import("../../canvas/renderers.js");
const { default: ChartSlot, resetPostedStills } = await import("./slot.jsx");
const { resetChartsQA, chartsQA } = await import("./qa.js");
const { fallbackPalette } = await import("../theme/palette.js");

const PRIM = { k: "slot", slot: "chart", x: 10, y: 20, w: 300, h: 200, ref: { id: "E-5", v: 7, doc: "0123456789abcdef0123456789abcdef.json" } };
const ELEMENT = { id: "E-5", type: "chart", engine: "echarts", chart: { type: "bar", gl: false }, option: { series: [] }, updated_seq: 7, doc_asset: PRIM.ref.doc };
const FALLBACK = <g data-fallback="1" />;

beforeAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
});

let mounted = [];
function mount(props) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  document.body.appendChild(node);
  const root = createRoot(node);
  act(() => root.render(<ChartSlot prim={PRIM} entryId="E-5" version={7} element={ELEMENT} team="t1" fallback={FALLBACK} {...props} />));
  mounted.push({ root, node });
  return { node, root, rerender: (next) => act(() => root.render(<ChartSlot prim={PRIM} entryId="E-5" version={7} element={ELEMENT} team="t1" fallback={FALLBACK} {...props} {...next} />)) };
}
const settle = () => act(async () => {
  for (let i = 0; i < 10; i += 1) await new Promise((r) => setTimeout(r, 0));
});
const themesRendered = () => vi.mocked(pictureOf).mock.calls.map((c) => c[0].theme);

beforeEach(() => {
  clearPictures();
  resetPostedStills();
  resetChartsQA();
  vi.mocked(pictureOf).mockClear();
  vi.mocked(rasterize).mockClear();
  window.__synapseV2 = {};
});
afterEach(() => {
  for (const { root, node } of mounted) {
    act(() => root.unmount());
    node.remove();
  }
  mounted = [];
  delete window.__synapseV2;
});

describe("ChartSlot", () => {
  test("the fallback shows at once, then the picture", async () => {
    const { node } = mount({ theme: "light", palette: fallbackPalette("light") });
    expect(node.querySelector("[data-fallback]")).not.toBe(null);
    await settle();
    // the fallback stays beneath the picture, hidden (its text lines are still the page's)
    const hidden = node.querySelector("[data-chart-fallback]");
    expect(hidden.getAttribute("visibility")).toBe("hidden");
    expect(hidden.querySelector("[data-fallback]")).not.toBe(null);
    const image = node.querySelector('g[data-slot="chart"] image[data-chart-picture]');
    expect(image.getAttribute("href")).toBe(`data:image/svg+xml;base64,${btoa("light")}`);
    expect([image.getAttribute("x"), image.getAttribute("y"), image.getAttribute("width"), image.getAttribute("height")]).toEqual(["10", "20", "300", "200"]);
    expect(node.querySelector("g[data-slot]").getAttribute("data-chart-engine")).toBe("echarts");
  });

  test("the picture carries the gist as its accessible label", async () => {
    const { node } = mount({ theme: "light", palette: fallbackPalette("light"), element: { ...ELEMENT, gist: ["max EMEA $4.2M", "EMEA up 38%"] } });
    await settle();
    const image = node.querySelector("image[data-chart-picture]");
    expect(image.getAttribute("role")).toBe("img");
    expect(image.getAttribute("aria-label")).toBe("max EMEA $4.2M. EMEA up 38%");
  });

  test("the renderer gets the element, the slot (its ref.doc), the box and the theme", async () => {
    mount({ theme: "dark", palette: fallbackPalette("dark") });
    await settle();
    const [args] = vi.mocked(pictureOf).mock.calls[0];
    expect(args.element).toBe(ELEMENT);
    expect(args.prim.ref.doc).toBe(PRIM.ref.doc);
    expect(args).toMatchObject({ w: 300, h: 200, theme: "dark", team: "t1" });
    expect(args.palette).toEqual(fallbackPalette("dark"));
  });

  test("a light page posts one still per (id, v), from the picture it drew", async () => {
    const onStill = vi.fn();
    mount({ theme: "light", palette: fallbackPalette("light"), onStill });
    await settle();
    expect(themesRendered()).toEqual(["light"]);
    expect(onStill).toHaveBeenCalledTimes(1);
    const [id, v, blob] = onStill.mock.calls[0];
    expect([id, v, blob.type]).toEqual(["E-5", 7, "image/png"]);
    expect(rasterize).toHaveBeenCalledWith(`data:image/svg+xml;base64,${btoa("light")}`, 300, 200, 1024);
  });

  test("a dark page still posts a light still", async () => {
    const onStill = vi.fn();
    const { node } = mount({ theme: "dark", palette: fallbackPalette("dark"), onStill });
    await settle();
    expect(themesRendered().sort()).toEqual(["dark", "light"]);
    expect(onStill).toHaveBeenCalledTimes(1);
    expect(rasterize).toHaveBeenCalledWith(`data:image/svg+xml;base64,${btoa("light")}`, 300, 200, 1024);
    // the page itself shows the dark picture
    expect(node.querySelector("image").getAttribute("href")).toBe(`data:image/svg+xml;base64,${btoa("dark")}`);
  });

  test("once per (id, v) across remounts and theme switches; a new version posts again", async () => {
    const onStill = vi.fn();
    const first = mount({ theme: "light", palette: fallbackPalette("light"), onStill });
    await settle();
    first.rerender({ theme: "dark", palette: fallbackPalette("dark") });
    await settle();
    mount({ theme: "light", palette: fallbackPalette("light"), onStill });
    await settle();
    expect(onStill).toHaveBeenCalledTimes(1);
    first.rerender({ version: 8, element: { ...ELEMENT, updated_seq: 8 } });
    await settle();
    expect(onStill).toHaveBeenCalledTimes(2);
    expect(onStill.mock.calls[1].slice(0, 2)).toEqual(["E-5", 8]);
  });

  test("a read-only page posts nothing and draws no extra light picture", async () => {
    const onStill = vi.fn();
    mount({ theme: "dark", palette: fallbackPalette("dark"), onStill, writable: false });
    mount({ theme: "dark", palette: fallbackPalette("dark") });
    await settle();
    expect(onStill).not.toHaveBeenCalled();
    expect(rasterize).not.toHaveBeenCalled();
    expect(themesRendered()).toEqual(["dark"]);
  });

  test("a failure keeps the fallback and is reported", async () => {
    vi.mocked(pictureOf).mockRejectedValueOnce(new Error("boom"));
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    const { node } = mount({ theme: "light", palette: fallbackPalette("light") });
    await settle();
    expect(node.querySelector("[data-fallback]")).not.toBe(null);
    expect(warn).toHaveBeenCalled();
    expect(chartsQA()).toEqual([{ id: "E-5", engine: "echarts", type: "bar", rendered: false, labelOverlaps: null, texts: null, failed: "boom" }]);
  });

  test("the QA hook lists what each chart drew", async () => {
    mount({ theme: "light", palette: fallbackPalette("light") });
    await settle();
    expect(typeof window.__synapseV2.charts).toBe("function");
    // `texts`: the labels the picture drew, which canvas_qa compares with the frame (QA phase34 L9).
    expect(window.__synapseV2.charts()).toEqual([{ id: "E-5", engine: "echarts", type: "bar", rendered: true, labelOverlaps: 0, texts: ["$0", "revenue"], failed: null }]);
  });

  test("a Vega-Lite picture sits on the light paper on a dark board", async () => {
    const vega = { id: "E-5", type: "chart", engine: "vega-lite", settings: { spec_asset: "x.vl.json" }, updated_seq: 7 };
    const { node } = mount({ element: vega, theme: "dark", palette: fallbackPalette("dark"), paper: "#ffffff", edge: "#e3e5e9" });
    await settle();
    const rect = node.querySelector('g[data-chart-engine="vega-lite"] rect');
    expect(rect && rect.getAttribute("fill")).toBe("#ffffff");
    const light = mount({ element: vega, theme: "light", palette: fallbackPalette("light"), paper: "#ffffff" });
    await settle();
    expect(light.node.querySelector('g[data-chart-engine="vega-lite"] rect')).toBe(null);
  });

  test("no theme prop: the theme is read off the palette", async () => {
    mount({ palette: fallbackPalette("dark") });
    await settle();
    expect(themesRendered()).toEqual(["dark"]);
  });
});
