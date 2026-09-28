// The static chart picture (canvas-v2-phase3-4.md D4, 2.10): one function per engine, one cache.
//
//   engine "echarts"      the element's option, resolved (resolve.js) and drawn by ECharts in SSR
//                         mode to an SVG string: echarts.init(null, theme, {renderer: "svg",
//                         ssr: true, width, height}), setOption, renderToSVGString, dispose
//   engine "echarts-raw"  the doc's option, sanitised (sanitize.js) and then as "echarts", or as
//                         chart.gl when it draws an echarts-gl series (bar3D, scatter3D, surface)
//   chart.gl              echarts-gl in scene3d/glCharts.js (a PNG)
//   "vega-lite" / none    canvas/renderers.js renderChart (v1 elements and the Vega-Lite hatch)
//
// A picture is {engine, kind: "svg" | "png", dataURL, svg?, labelOverlaps, texts?}; a failure rejects, and
// the slot keeps its fallback. This module is the lazy half of the chart code: slot.jsx and
// live.jsx import it dynamically, with the first chart they draw (picture.js is the eager half).
import { svgToDataURL } from "../../canvas/renderers.js";
import { docNameOf, loadDoc } from "./docs.js";
import { loadECharts } from "./engine.js";
import { mountGL, renderGL } from "./gl.js";
import { countOverlaps, textBoxes } from "./labels.js";
import { rawOption } from "./raw.js";
import { resolveOption } from "./resolve.js";
import { needsGL } from "./sanitize.js";
import { ensureTheme } from "./theme.js";
import { renderVega } from "./vega.js";
import { engineOf, px } from "./picture.js";

export { cachedPicture, clearPictures, engineOf, pictureKey, typeOf } from "./picture.js";

// A geometry attribute holding NaN (a category named "NaN" in a <text> is fine).
export const NAN_ATTR = /\s(?:d|x|y|x1|x2|y1|y2|cx|cy|r|rx|ry|width|height|points|transform)="[^"]*NaN/;

/** The option an ECharts engine draws, before resolving: the element's own, or the raw doc's. */
export function optionOf(element, doc) {
  const engine = engineOf(element);
  if (engine === "echarts-raw") return rawOption(doc);
  const option = element && element.option;
  if (!option || typeof option !== "object") throw new Error("a chart without an option");
  return option;
}

/** False when the browser's XML parser rejects the SVG (true where there is no DOMParser). */
export function wellFormed(svg, Parser = typeof DOMParser === "undefined" ? null : DOMParser) {
  if (!Parser) return true;
  const doc = new Parser().parseFromString(svg, "image/svg+xml");
  return !doc.getElementsByTagName("parsererror").length && doc.documentElement?.nodeName === "svg";
}

/**
 * The option drawn to an SVG string by ECharts in SSR mode, with its label collisions counted on
 * the laid-out scene. `echarts` is the core namespace (engine.js); `palette` the flat palette of
 * `theme`.
 */
export function echartsSVG(echarts, option, doc, { w, h, theme = "light", palette }) {
  const themeName = ensureTheme(echarts, theme, palette);
  const chart = echarts.init(null, themeName, { renderer: "svg", ssr: true, width: px(w), height: px(h) });
  try {
    chart.setOption(resolveOption(option, doc, palette, theme), { notMerge: true, lazyUpdate: false, silent: true });
    const svg = chart.renderToSVGString();
    const boxes = textBoxes(chart);
    // `texts`: every label ECharts drew, for the QA hook (canvas_qa compares them with the frame Python laid out).
    return { svg, labelOverlaps: countOverlaps(boxes), texts: boxes.map((b) => String(b.text)) };
  } finally {
    chart.dispose();
  }
}

// ECharts draws synchronously (10 to 30 ms a chart); a board of 20 charts would be one long task.
// Static draws run one after another, handing the main thread back between them once a slice has
// run past SLICE_MS, so panning and input stay responsive while the pictures arrive.
const SLICE_MS = 8;
let queue = Promise.resolve();
let sliceStart = 0;
const now = () => (typeof performance !== "undefined" ? performance.now() : Date.now());
const macrotask = () => new Promise((resolve) => setTimeout(resolve, 0));

/** fn() in the draw queue, after the draws queued before it. */
export function queued(fn) {
  const run = queue.then(async () => {
    if (now() - sliceStart > SLICE_MS) {
      await macrotask();
      sliceStart = now();
    }
    return fn();
  });
  queue = run.catch(() => undefined);
  return run;
}

/**
 * The chart's static picture. `doc` is its doc asset (flat charts: datasets and refs; raw: the
 * option) or null; `team` is needed only by Vega-Lite charts, which read their spec asset and data.
 */
export async function renderChartStatic(element, doc, { w, h, theme = "light", palette, team = "" } = {}) {
  const engine = engineOf(element);
  const width = px(w);
  const height = px(h);
  if (engine === "vega-lite") {
    const svg = await renderVega(team, element, width, height);
    return { engine, kind: "svg", svg, dataURL: svgToDataURL(svg), labelOverlaps: null };
  }
  const option = engine === "gl" ? element.option : optionOf(element, doc);
  // A raw option's doc holds the option itself, not datasets or refs.
  const optionDoc = engine === "echarts-raw" ? null : doc;
  if (engine === "gl" || needsGL(option)) {
    const { dataURL } = await renderGL(option, optionDoc, { w: width, h: height, theme, palette });
    if (typeof dataURL !== "string" || !dataURL.startsWith("data:image/")) throw new Error("the 3D chart gave no picture");
    return { engine, kind: "png", dataURL, labelOverlaps: null };
  }
  const echarts = await loadECharts(engine === "echarts-raw");
  const { svg, labelOverlaps, texts } = await queued(() => echartsSVG(echarts, option, optionDoc, { w: width, h: height, theme, palette }));
  if (NAN_ATTR.test(svg)) throw new Error("the chart drew NaN coordinates");
  if (!wellFormed(svg)) throw new Error("the chart's SVG is not well-formed");
  return { engine, kind: "svg", svg, dataURL: svgToDataURL(svg), labelOverlaps, texts };
}

// -- live -----------------------------------------------------------------------------------------

/**
 * Mounts the live chart into `div` (live.jsx, while the chart is entered); resolves to {dispose}.
 * ECharts in SVG mode with the same resolved option; GL charts through glCharts.js.
 */
export async function mountLiveChart(div, { element, prim, team, theme, palette, w, h }) {
  const engine = engineOf(element);
  const doc = await loadDoc(team, docNameOf(prim, element));
  const option = engine === "gl" ? element.option : optionOf(element, doc);
  const optionDoc = engine === "echarts-raw" ? null : doc;
  if (engine === "gl" || needsGL(option)) return mountGL(div, option, optionDoc, { w, h, theme, palette });
  const echarts = await loadECharts(engine === "echarts-raw");
  const chart = echarts.init(div, ensureTheme(echarts, theme, palette), { renderer: "svg", width: w, height: h });
  chart.setOption(resolveOption(option, optionDoc, palette, theme), { notMerge: true });
  return { dispose: () => chart.dispose(), chart };
}

/** The static picture of a slot's element: its doc fetched, then renderChartStatic. */
export async function pictureOf({ team, element, prim, theme, palette, w, h }) {
  const doc = await loadDoc(team, docNameOf(prim, element));
  return renderChartStatic(element, doc, { w, h, theme, palette, team });
}
