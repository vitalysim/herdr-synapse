// The chart chunks, loaded once per page on the first chart that needs them (canvas-v2-phase3-4.md
// 2.10): echartsCore.js for every chart, plus echartsRaw.js when a raw option is drawn. Until they
// arrive a chart shows its fallback (the Python drawing), so the page never waits on ECharts to
// show a picture.
let core = null;
let raw = null;

function once(load, reset) {
  return load().catch((err) => {
    reset();
    throw err;
  });
}

/**
 * Resolves to the ECharts core namespace with the flat modules registered, and the raw-option
 * modules too when `withRaw` is true.
 */
export function loadECharts(withRaw = false) {
  if (!core) core = once(() => import("./echartsCore.js").then((m) => m.echarts), () => (core = null));
  if (!withRaw) return core;
  if (!raw) raw = once(() => core.then(() => import("./echartsRaw.js")).then((m) => m.echarts), () => (raw = null));
  return raw;
}
