// components.js: the bundled ECharts modules (canvas-v2-phase3-4.md 2.4, 2.10, D19). The list is
// data (postbuild reads it into web/dist/charts.json); echartsCore.js registers exactly it; every
// module an option fixture needs is in it.
import { describe, expect, test } from "vitest";
import { BUNDLED_SERIES, CORE_MODULES, ECHARTS_MODULES, FEATURES, FLAT_CHARTS, FLAT_COMPONENTS, RAW_MODULES, SERIES_OF_MODULE } from "./components.js";
import { MODULE_TABLE } from "./echartsCore.js";
import { RAW_MODULE_TABLE } from "./echartsRaw.js";
import { optionFixtures } from "./__fixtures__/fixtures.js";
import { rawOption } from "./raw.js";

// The component an option key needs.
const KEY_MODULES = {
  xAxis: "GridComponent",
  yAxis: "GridComponent",
  grid: "GridComponent",
  dataset: "DatasetComponent",
  legend: "LegendComponent",
  tooltip: "TooltipComponent",
  visualMap: "VisualMapComponent",
  aria: "AriaComponent",
  markLine: "MarkLineComponent",
  markPoint: "MarkPointComponent",
  radar: "RadarComponent",
  polar: "PolarComponent",
  parallel: "ParallelComponent",
  singleAxis: "SingleAxisComponent",
  calendar: "CalendarComponent",
  dataZoom: "DataZoomComponent",
};
const MODULE_OF_SERIES = Object.fromEntries(Object.entries(SERIES_OF_MODULE).flatMap(([name, types]) => types.map((t) => [t, name])));

function* keys(value) {
  if (Array.isArray(value)) for (const v of value) yield* keys(v);
  else if (value && typeof value === "object")
    for (const [k, v] of Object.entries(value)) {
      yield k;
      yield* keys(v);
    }
}

function needed(option) {
  const out = new Set();
  for (const k of keys(option)) if (KEY_MODULES[k]) out.add(KEY_MODULES[k]);
  const series = Array.isArray(option.series) ? option.series : option.series ? [option.series] : [];
  for (const s of series) out.add(MODULE_OF_SERIES[s.type] || `(no module draws series "${s.type}")`);
  return [...out];
}

describe("the module list", () => {
  test("sorted, unique, SVG renderer, never Canvas or geo", () => {
    expect(ECHARTS_MODULES).toEqual([...new Set(ECHARTS_MODULES)].sort());
    expect(ECHARTS_MODULES).toContain("SVGRenderer");
    for (const name of ["CanvasRenderer", "MapChart", "GeoComponent", "ToolboxComponent", "GraphicComponent"]) expect(ECHARTS_MODULES).not.toContain(name);
    expect(FEATURES).toContain("LabelLayout");
  });
  test("the two chunks register exactly the list, the flat set in the core one", () => {
    expect(Object.keys(MODULE_TABLE).sort()).toEqual(CORE_MODULES);
    expect(Object.keys(RAW_MODULE_TABLE).sort()).toEqual(RAW_MODULES);
    expect([...CORE_MODULES, ...RAW_MODULES].sort()).toEqual(ECHARTS_MODULES);
    for (const name of [...FLAT_CHARTS, ...FLAT_COMPONENTS]) expect(CORE_MODULES).toContain(name);
    for (const name of ECHARTS_MODULES) expect(typeof (MODULE_TABLE[name] || RAW_MODULE_TABLE[name])).toBe("function");
  });
  test("bundled series", () => {
    expect(BUNDLED_SERIES).toEqual(expect.arrayContaining(["bar", "line", "scatter", "pie", "heatmap", "boxplot", "funnel", "treemap", "sankey", "radar"]));
  });
});

describe.each(optionFixtures().map((f) => [f.name, f]))("fixture %s", (_name, f) => {
  test("every module it needs is bundled", () => {
    const isRaw = f.element.engine === "echarts-raw";
    const option = isRaw ? rawOption(f.doc) : f.element.option;
    if (!option || f.element.chart?.gl) return;
    // a flat option draws with the core chunk alone; a raw one may use the raw chunk too
    for (const name of needed(option)) expect(isRaw ? ECHARTS_MODULES : CORE_MODULES, name).toContain(name);
    // a fixture that names its modules (the chart type's `echarts` list) is held to them too
    const declared = f.data.echarts || f.data.modules || f.element.echarts;
    if (Array.isArray(declared)) for (const name of declared) expect(ECHARTS_MODULES, name).toContain(name);
  });
});
