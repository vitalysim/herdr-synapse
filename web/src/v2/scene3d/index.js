// The scene3d page part's public API (canvas-v2-phase3-4.md 3.7, 11.2). This module, lazySlot.js and
// qa.js are in the page's first chunk and stay small: the slot's behaviour (slot.jsx), three.js, the
// renderer and echarts-gl load lazily, on the first scene or GL chart on screen.
//
//   scene3dSlot           the slot definition render/slots/index.js registers (WW-2): an HTML-layer
//                         placeholder over an SVG underlay (the still or the drawing), entered on a
//                         double-click because the slot is GL (WW-3)
//   glBudget              the page's live WebGL budget (3.12, WW-6)
//   renderGLChartStatic   (option, doc, {w, h, theme, palette}) -> Promise<{dataURL}> (WW-1)
//   mountGLChartLive      (div, option, doc, opts) -> Promise<{dispose}> (WW-1)
//   QA                    window.__synapseV2.scene3d()
import Scene3DSlot from "./lazySlot.js";
import { scene3dQA } from "./qa.js";

export { glBudget, GL_LIMITS, createBudget } from "./budget.js";
export { PRIMITIVES, LOADERS, GL_MODULES } from "./manifest.js";
export { loadScene3DSlot } from "./lazySlot.js";

export const scene3dSlot = { mode: "html", Component: Scene3DSlot, underlay: true, enter: "gl" };

let glChunk = null;
function glCharts() {
  if (!glChunk) glChunk = import("./glCharts.js");
  return glChunk;
}

export function renderGLChartStatic(option, doc, opts) {
  return glCharts().then((m) => m.renderGLChartStatic(option, doc, opts));
}

export function mountGLChartLive(div, option, doc, opts) {
  return glCharts().then((m) => m.mountGLChartLive(div, option, doc, opts));
}

export const QA = scene3dQA;
