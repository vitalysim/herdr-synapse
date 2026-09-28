// Charts on the v2 page (canvas-v2-phase3-4.md 2.10): the public API the integration uses.
//
//   chartSlot           the "chart" slot definition for render/slots/index.js (WW-2):
//                       {mode: "svg", Component, Live}. Component props are the Phase 1 slot props
//                       (prim, entryId, version, element, team, paper, edge, fallback, onStill) plus
//                       entered, onExit, palette and, when the Surface has them, theme, palettes
//                       ({light, dark}) and writable. Live props: {prim, entryId, version, element,
//                       team, theme, palette, onExit}.
//   renderChartStatic   (element, doc, {w, h, theme, palette, team}) -> Promise<picture>
//   (resolve.js resolveOption(option, doc, palette[, theme]) is imported directly by glCharts.js,
//   WW-1; it is not re-exported here so the first-load chunk stays free of the drawing code.)
//   QA                  {name: "charts", read}: render/qa.js registerQA(QA.name, QA.read) (WW-5)
//
// This is all the first load carries for charts: the slot and live components (slot.jsx,
// live.jsx) arrive with the first chart on the board, and until then the slot shows its fallback
// (the display list's Python drawing) through Suspense. The drawing code (render.js) and ECharts
// (echartsCore.js) load after them, on the first picture.
import React, { Suspense, lazy } from "react";
import { chartsQA } from "./qa.js";

const LazySlot = lazy(() => import("./slot.jsx"));
const LazyLive = lazy(() => import("./live.jsx"));

/** The chart slot: its fallback until slot.jsx has loaded, then slot.jsx's ChartSlot. */
function ChartSlot(props) {
  return React.createElement(Suspense, { fallback: props.fallback ?? null }, React.createElement(LazySlot, props));
}

/** The entered chart: nothing until live.jsx has loaded (the static picture stays beneath). */
function ChartLive(props) {
  return React.createElement(Suspense, { fallback: null }, React.createElement(LazyLive, props));
}

/** The static picture of a chart (render.js, loaded on first use). */
export function renderChartStatic(element, doc, opts) {
  return import("./render.js").then((m) => m.renderChartStatic(element, doc, opts));
}

export const chartSlot = { mode: "svg", Component: ChartSlot, Live: ChartLive };

export const QA = { name: "charts", read: chartsQA };
