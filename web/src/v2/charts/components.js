// Data only: the ECharts modules the page bundles for charts (canvas-v2-phase3-4.md 2.4, 2.10, D19).
// scripts/postbuild.mjs reads ECHARTS_MODULES into web/dist/charts.json ("echarts"), and the Python
// tests hold every registered chart type's `echarts` list inside it, so a type that needs an
// unbundled series fails a test, not a user.
//
// This file imports nothing, so postbuild (and the first-load chunk) can read it without pulling in
// ECharts. Two lazy chunks register these names, and a test holds them equal to the list:
// echartsCore.js (the flat set, on the first chart drawn) and echartsRaw.js (the raw-option extras,
// added only when a raw option is drawn, so an ordinary board never loads them).
//
// The list is the union of the built-in flat types' modules (2.4) and the raw-option list (2.8).
// ECharts' geo and map modules are never bundled: GeoJSONResource holds a `new Function` (0.2).

/** Series the flat chart types use (2.4). */
export const FLAT_CHARTS = [
  "BarChart",
  "LineChart",
  "ScatterChart",
  "PieChart",
  "HeatmapChart",
  "BoxplotChart",
  "FunnelChart",
  "TreemapChart",
  "SankeyChart",
];

/** Components the flat chart types use (2.4). */
export const FLAT_COMPONENTS = [
  "GridComponent",
  "DatasetComponent",
  "LegendComponent",
  "TooltipComponent",
  "MarkLineComponent",
  "AriaComponent",
  "VisualMapComponent",
];

/**
 * What a raw ECharts option may use besides the flat set (2.4, 2.8). EffectScatterChart is not in
 * 2.4's raw list but _sanitize.SERIES allows "effectScatter", so the page bundles it rather than
 * pass a series it cannot draw (a few KB, in the raw chunk only).
 */
export const RAW_EXTRA = [
  "EffectScatterChart",
  "RadarChart",
  "RadarComponent",
  "GaugeChart",
  "ParallelChart",
  "ParallelComponent",
  "CandlestickChart",
  "ThemeRiverChart",
  "SingleAxisComponent",
  "CalendarComponent",
  "PolarComponent",
  "TitleComponent",
  "DataZoomComponent",
  "MarkPointComponent",
];

/** Features and the renderer (never CanvasRenderer here: that is the GL chunk, 3.11). */
export const FEATURES = ["LabelLayout", "SVGRenderer"];

/** What the chart chunk (echartsCore.js) registers: every flat chart draws with these. */
export const CORE_MODULES = [...new Set([...FLAT_CHARTS, ...FLAT_COMPONENTS, ...FEATURES])].sort();

/** What the raw chunk (echartsRaw.js) adds, loaded only when a raw option is drawn. */
export const RAW_MODULES = RAW_EXTRA.filter((name) => !CORE_MODULES.includes(name)).sort();

/** Every module name the page bundles for charts, sorted, without repeats (charts.json "echarts"). */
export const ECHARTS_MODULES = [...new Set([...CORE_MODULES, ...RAW_MODULES])].sort();

/** The ECharts series `type` each bundled chart module draws (sanitize.js checks raw series against it). */
export const SERIES_OF_MODULE = {
  BarChart: ["bar"],
  LineChart: ["line"],
  ScatterChart: ["scatter"],
  EffectScatterChart: ["effectScatter"],
  PieChart: ["pie"],
  HeatmapChart: ["heatmap"],
  BoxplotChart: ["boxplot"],
  FunnelChart: ["funnel"],
  TreemapChart: ["treemap"],
  SankeyChart: ["sankey"],
  RadarChart: ["radar"],
  GaugeChart: ["gauge"],
  ParallelChart: ["parallel"],
  CandlestickChart: ["candlestick"],
  ThemeRiverChart: ["themeRiver"],
};

/** The series types the bundle can draw. */
export const BUNDLED_SERIES = Object.entries(SERIES_OF_MODULE)
  .filter(([name]) => ECHARTS_MODULES.includes(name))
  .flatMap(([, types]) => types)
  .sort();
