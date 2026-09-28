// The lazy chart chunk (canvas-v2-phase3-4.md 2.10): ECharts core, tree-shaken to the flat chart
// types' modules in components.js (CORE_MODULES), with the SVG renderer only. No `echarts` root
// import (it would bundle every chart, geo included) and no CanvasRenderer (that belongs to the GL
// chunk, scene3d/glCharts.js). The raw-option extras are echartsRaw.js. Loaded only through
// engine.js, so the first-load chunk never carries ECharts.
import * as echarts from "echarts/core";
import { BarChart, BoxplotChart, FunnelChart, HeatmapChart, LineChart, PieChart, SankeyChart, ScatterChart, TreemapChart } from "echarts/charts";
import {
  AriaComponent,
  DatasetComponent,
  GridComponent,
  LegendComponent,
  MarkLineComponent,
  TooltipComponent,
  VisualMapComponent,
} from "echarts/components";
import { LabelLayout } from "echarts/features";
import { SVGRenderer } from "echarts/renderers";
import { CORE_MODULES } from "./components.js";

/** name -> module, exactly components.js CORE_MODULES (a test holds them equal). */
export const MODULE_TABLE = {
  AriaComponent,
  BarChart,
  BoxplotChart,
  DatasetComponent,
  FunnelChart,
  GridComponent,
  HeatmapChart,
  LabelLayout,
  LegendComponent,
  LineChart,
  MarkLineComponent,
  PieChart,
  SVGRenderer,
  SankeyChart,
  ScatterChart,
  TooltipComponent,
  TreemapChart,
  VisualMapComponent,
};

const missing = CORE_MODULES.filter((name) => !MODULE_TABLE[name]);
if (missing.length) throw new Error(`charts/echartsCore.js does not import ${missing.join(", ")} (listed in components.js)`);

echarts.use(CORE_MODULES.map((name) => MODULE_TABLE[name]));

export { echarts };
