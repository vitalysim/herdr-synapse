// render.js: the engine dispatch (canvas-v2-phase3-4.md 2.10). ECharts draws flat and raw options
// here for real; the Vega-Lite and GL renderers are stand-ins that record what they were given.
import { beforeAll, beforeEach, describe, expect, test, vi } from "vitest";
import { fallbackPalette } from "../theme/palette.js";
import { optionFixtures } from "./__fixtures__/fixtures.js";

vi.mock("./vega.js", () => ({ renderVega: vi.fn(async () => '<svg xmlns="http://www.w3.org/2000/svg" width="10" height="10"></svg>') }));
vi.mock("./gl.js", () => ({ renderGL: vi.fn(async () => ({ dataURL: "data:image/png;base64,AAAA" })), mountGL: vi.fn() }));

const { renderVega } = await import("./vega.js");
const { renderGL } = await import("./gl.js");
const { cachedPicture, clearPictures, engineOf, pictureKey, pictureOf, renderChartStatic, typeOf } = await import("./render.js");

const fx = Object.fromEntries(optionFixtures().map((f) => [f.name, f]));
const LIGHT = fallbackPalette("light");

beforeAll(async () => {
  // ECharts transforms slowly under a full parallel run: load it once before the timed tests.
  await import("./echartsCore.js");
  await import("./echartsRaw.js");
}, 60000);

beforeEach(() => {
  clearPictures();
  vi.mocked(renderVega).mockClear();
  vi.mocked(renderGL).mockClear();
});

describe("engineOf", () => {
  test.each([
    [{ engine: "echarts", chart: { type: "bar", gl: false } }, "echarts"],
    [{ engine: "echarts-raw" }, "echarts-raw"],
    [{ engine: "echarts", chart: { type: "bar3d", gl: true } }, "gl"],
    [{ engine: "vega-lite" }, "vega-lite"],
    [{}, "vega-lite"],
    [{ engine: "something-new" }, "vega-lite"],
    [null, "vega-lite"],
  ])("%j -> %s", (element, engine) => {
    expect(engineOf(element)).toBe(engine);
  });
  test("typeOf", () => {
    expect(typeOf({ chart: { type: "bar" } })).toBe("bar");
    expect(typeOf({ settings: { type: "line" } })).toBe("line");
    expect(typeOf({})).toBe(null);
  });
});

describe("renderChartStatic", () => {
  test("a flat chart is an SVG drawn by ECharts", async () => {
    const f = fx["local/bar"];
    const pic = await renderChartStatic(f.element, f.doc, { w: f.box.w, h: f.box.h, theme: "light", palette: LIGHT });
    expect(pic.engine).toBe("echarts");
    expect(pic.kind).toBe("svg");
    expect(pic.svg.startsWith("<svg")).toBe(true);
    expect(pic.dataURL.startsWith("data:image/svg+xml;base64,")).toBe(true);
    expect(pic.labelOverlaps).toBe(0);
    expect(renderVega).not.toHaveBeenCalled();
  });
  test("a raw chart is sanitised, then drawn", async () => {
    const f = fx["local/raw-radar"];
    const pic = await renderChartStatic(f.element, f.doc, { w: 400, h: 300, theme: "dark", palette: fallbackPalette("dark") });
    expect(pic.engine).toBe("echarts-raw");
    expect(pic.svg).not.toContain("<img");
  });
  test("a refused raw option rejects (the slot keeps its fallback)", async () => {
    const element = { id: "E-1", engine: "echarts-raw" };
    await expect(renderChartStatic(element, { v: 1, option: { series: [{ type: "map" }] } }, { w: 100, h: 100 })).rejects.toThrow(/refused/);
  });
  test("a flat chart without an option rejects", async () => {
    await expect(renderChartStatic({ id: "E-1", engine: "echarts" }, null, { w: 100, h: 100 })).rejects.toThrow(/without an option/);
  });
  test("a GL chart goes to glCharts with the stored option", async () => {
    const element = { id: "E-9", engine: "echarts", chart: { type: "bar3d", gl: true }, option: { grid3D: {} } };
    const doc = { v: 1, datasets: [] };
    const pic = await renderChartStatic(element, doc, { w: 300.4, h: 200, theme: "dark", palette: LIGHT });
    expect(renderGL).toHaveBeenCalledWith(element.option, doc, { w: 300, h: 200, theme: "dark", palette: LIGHT });
    expect(pic).toEqual({ engine: "gl", kind: "png", dataURL: "data:image/png;base64,AAAA", labelOverlaps: null });
  });
  test("a raw option with an echarts-gl series goes to glCharts, sanitised, without a doc", async () => {
    const element = { id: "E-10", engine: "echarts-raw" };
    const raw = { v: 1, option: { title: { text: "x" }, series: [{ type: "bar3D", data: [[0, 0, 1]] }] } };
    const pic = await renderChartStatic(element, raw, { w: 200, h: 100, theme: "light", palette: LIGHT });
    expect(renderGL).toHaveBeenCalledTimes(1);
    const [option, doc] = vi.mocked(renderGL).mock.calls[0];
    expect(option.title).toBeUndefined();
    expect(option.tooltip).toEqual({ renderMode: "richText", confine: true });
    expect(doc).toBe(null);
    expect(pic.kind).toBe("png");
  });
  test("a GL chart that gives no image rejects", async () => {
    vi.mocked(renderGL).mockResolvedValueOnce({ dataURL: "javascript:alert(1)" });
    await expect(renderChartStatic({ id: "E-9", chart: { gl: true }, option: {} }, null, { w: 10, h: 10 })).rejects.toThrow(/no picture/);
  });
  test("a v1 or Vega-Lite chart goes to the Vega renderer", async () => {
    const element = { id: "E-3", type: "chart", spec_asset: "ab.vl.json", data: "rev.csv" };
    const pic = await renderChartStatic(element, null, { w: 200, h: 100, team: "t1" });
    expect(renderVega).toHaveBeenCalledWith("t1", element, 200, 100);
    expect(pic.engine).toBe("vega-lite");
    expect(pic.dataURL.startsWith("data:image/svg+xml;base64,")).toBe(true);
  });
});

describe("the picture cache", () => {
  test("one render per key; a failure is retried", async () => {
    const make = vi.fn(async () => ({ ok: 1 }));
    const key = pictureKey("t", { id: "E-1", updated_seq: 3 }, "light", 100.2, 50);
    expect(key).toBe("t|E-1|3|light|100x50");
    await cachedPicture(key, make);
    await cachedPicture(key, make);
    expect(make).toHaveBeenCalledTimes(1);
    const bad = vi.fn(async () => {
      throw new Error("x");
    });
    await expect(cachedPicture("k2", bad)).rejects.toThrow();
    await expect(cachedPicture("k2", bad)).rejects.toThrow();
    expect(bad).toHaveBeenCalledTimes(2);
  });
});

describe("the draw queue", () => {
  test("draws run in order, one at a time, and a failure does not stop the next", async () => {
    const { queued } = await import("./render.js");
    const order = [];
    let running = 0;
    const job = (name, fail = false) => () => {
      running += 1;
      expect(running).toBe(1);
      order.push(name);
      running -= 1;
      if (fail) throw new Error(name);
      return name;
    };
    const results = await Promise.allSettled([queued(job("a")), queued(job("b", true)), queued(job("c"))]);
    expect(order).toEqual(["a", "b", "c"]);
    expect(results.map((r) => r.status)).toEqual(["fulfilled", "rejected", "fulfilled"]);
  });
});
