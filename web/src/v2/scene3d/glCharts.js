// 3D data charts through echarts-gl (canvas-v2-phase3-4.md 3.11, D8; interface WW-1). Its own lazy
// chunk: ECharts core with the CanvasRenderer, echarts-gl's Bar3D, Scatter3D and Surface charts
// with Grid3D, and VisualMap, Legend (a colour channel), Tooltip (richText, while entered) and
// Aria. echarts-gl's engine, claygl, is patched at build time
// (vite.config.js cspSafeClayglExpr) so it never calls `new Function` under the page's CSP.
//
// Every echarts-gl instance owns a WebGL context, so each works under a glBudget "echarts-gl" lease:
// - renderGLChartStatic: one at a time (a FIFO), rasterised to a PNG data URL, then disposed and
//   its context lost at once; the chart shows that picture.
// - mountGLChartLive: while a chart is entered; at most 2 live, the least recently touched is
//   demoted back to its static picture past that.
import * as echarts from "echarts/core";
import { CanvasRenderer } from "echarts/renderers";
import { AriaComponent, LegendComponent, TooltipComponent, VisualMapComponent } from "echarts/components";
import { Bar3DChart, Scatter3DChart, SurfaceChart } from "echarts-gl/charts";
import { Grid3DComponent } from "echarts-gl/components";
import { resolveOption } from "../charts/resolve.js";
import { ensureTheme } from "../charts/theme.js";
import { glBudget } from "./budget.js";
import { cornerLabels, surfaceGrid } from "./glOption.js";

echarts.use([CanvasRenderer, AriaComponent, LegendComponent, TooltipComponent, VisualMapComponent, Bar3DChart, Scatter3DChart, SurfaceChart, Grid3DComponent]);

export { echarts };

const DPR = 2;
const SETTLE_FRAMES = 2;
const TIMEOUT_MS = 8000;

const px = (v) => Math.max(1, Math.round(Number(v) || 1));
const frame = () => new Promise((resolve) => (typeof requestAnimationFrame === "function" ? requestAnimationFrame(() => resolve()) : setTimeout(resolve, 16)));

// Loses every WebGL context an ECharts instance holds (its echarts-gl layers), so the browser frees
// them now rather than at garbage collection.
function loseContexts(chart) {
  try {
    const painter = chart.getZr().painter;
    const layers = [];
    if (painter && typeof painter.eachOtherLayer === "function") painter.eachOtherLayer((layer) => layers.push(layer));
    for (const layer of layers) {
      const gl = layer && layer.renderer && (layer.renderer.gl || (layer.renderer.getGL && layer.renderer.getGL()));
      const ext = gl && gl.getExtension && gl.getExtension("WEBGL_lose_context");
      if (ext) ext.loseContext();
    }
  } catch {
    // the chart is going anyway
  }
}

function waitFinished(chart) {
  return new Promise((resolve) => {
    let done = false;
    const finish = () => {
      if (done) return;
      done = true;
      resolve();
    };
    chart.on("finished", finish);
    setTimeout(finish, TIMEOUT_MS);
  });
}

function drawable(option, doc, theme, palette) {
  const resolved = resolveOption(option, doc, palette, theme);
  resolved.animation = false;
  if (Array.isArray(resolved.series)) resolved.series = resolved.series.map(surfaceGrid);
  return cornerLabels(resolved);
}

let serial = Promise.resolve();

/**
 * A static picture of a GL chart at w×h CSS px (×2 device pixels): Promise<{dataURL}>. Renders are
 * serialised; each waits for a lease, then gives it back.
 */
export function renderGLChartStatic(option, doc, { w, h, theme = "light", palette } = {}) {
  const run = async () => {
    const lease = await glBudget.request("echarts-gl", "static", { queue: true });
    const div = document.createElement("div");
    div.style.cssText = `position:absolute;left:-10000px;top:0;width:${px(w)}px;height:${px(h)}px;pointer-events:none;`;
    document.body.appendChild(div);
    let chart = null;
    try {
      chart = echarts.init(div, ensureTheme(echarts, theme, palette), { renderer: "canvas", width: px(w), height: px(h), devicePixelRatio: DPR });
      const finished = waitFinished(chart);
      chart.setOption(drawable(option, doc, theme, palette), { notMerge: true, lazyUpdate: false });
      await finished;
      for (let i = 0; i < SETTLE_FRAMES; i += 1) await frame();
      const dataURL = chart.getDataURL({ type: "png", pixelRatio: DPR, backgroundColor: "transparent" });
      return { dataURL };
    } finally {
      if (chart) {
        loseContexts(chart);
        chart.dispose();
      }
      div.remove();
      glBudget.release(lease);
    }
  };
  const next = serial.then(run, run);
  serial = next.catch(() => undefined);
  return next;
}

/**
 * The live GL chart in `div` (entered): tooltips, orbit and zoom. Promise<{dispose, chart}>. Past
 * the budget's 2 live GL charts the least recently touched is demoted: disposed, and its `onDemote`
 * (opts) told so its holder shows the static picture again.
 */
export async function mountGLChartLive(div, option, doc, { w, h, theme = "light", palette, onDemote = null } = {}) {
  let chart = null;
  let lease = null;
  const dispose = () => {
    if (chart) {
      loseContexts(chart);
      chart.dispose();
      chart = null;
    }
    if (lease) glBudget.release(lease);
    lease = null;
  };
  lease = glBudget.request("echarts-gl", "live", {
    priority: 1,
    onDemote: () => {
      lease = null;
      dispose();
      if (typeof onDemote === "function") onDemote();
    },
  });
  if (!lease) throw new Error("no WebGL budget left for a live 3D chart");
  chart = echarts.init(div, ensureTheme(echarts, theme, palette), { renderer: "canvas", width: px(w), height: px(h) });
  chart.setOption(drawable(option, doc, theme, palette), { notMerge: true });
  return {
    chart,
    dispose,
    touch: () => lease && glBudget.touch(lease),
  };
}
