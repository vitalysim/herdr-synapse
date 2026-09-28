// The raw-option chunk (canvas-v2-phase3-4.md 2.4, 2.8): the series and components a sanitised raw
// ECharts option may use beyond the flat set (components.js RAW_MODULES), registered on the same
// ECharts core as echartsCore.js. engine.js loads it only for engine "echarts-raw", so a board
// of flat charts never downloads radar, gauge, parallel, candlestick, theme river, calendar,
// polar, single axis, title, data zoom, mark point or effect scatter.
import * as echarts from "echarts/core";
import { CandlestickChart, EffectScatterChart, GaugeChart, ParallelChart, RadarChart, ThemeRiverChart } from "echarts/charts";
import {
  CalendarComponent,
  DataZoomComponent,
  MarkPointComponent,
  ParallelComponent,
  PolarComponent,
  RadarComponent,
  SingleAxisComponent,
  TitleComponent,
} from "echarts/components";
import { RAW_MODULES } from "./components.js";

/** name -> module, exactly components.js RAW_MODULES (a test holds them equal). */
export const RAW_MODULE_TABLE = {
  CalendarComponent,
  CandlestickChart,
  DataZoomComponent,
  EffectScatterChart,
  GaugeChart,
  MarkPointComponent,
  ParallelChart,
  ParallelComponent,
  PolarComponent,
  RadarChart,
  RadarComponent,
  SingleAxisComponent,
  ThemeRiverChart,
  TitleComponent,
};

const missing = RAW_MODULES.filter((name) => !RAW_MODULE_TABLE[name]);
if (missing.length) throw new Error(`charts/echartsRaw.js does not import ${missing.join(", ")} (listed in components.js)`);

echarts.use(RAW_MODULES.map((name) => RAW_MODULE_TABLE[name]));

export { echarts };
