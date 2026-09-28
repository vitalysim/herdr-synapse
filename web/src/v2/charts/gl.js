// chart.gl (bar3d, scatter3d, surface; canvas-v2-phase3-4.md 3.11): drawn by echarts-gl in
// scene3d/glCharts.js (3D-WEB, interface WW-1), its own lazy chunk with the CanvasRenderer and the
// patched claygl. Found through a glob so a build without it still builds: the chart then draws
// its fallback (the Python iso projection).
const modules = import.meta.glob("../scene3d/glCharts.js");
const PATH = "../scene3d/glCharts.js";

let ready = null;

/** The glCharts module: {renderGLChartStatic, mountGLChartLive}. */
export function loadGLCharts() {
  if (!ready) {
    const load = modules[PATH];
    if (!load) return Promise.reject(new Error("3D charts are not in this build (scene3d/glCharts.js)"));
    ready = load().catch((err) => {
      ready = null;
      throw err;
    });
  }
  return ready;
}

/** A PNG data URL of the chart at w×h: {dataURL}. */
export async function renderGL(option, doc, opts) {
  const m = await loadGLCharts();
  return m.renderGLChartStatic(option, doc, opts);
}

/** Mounts the live GL chart into `div` (under the GL budget); returns {dispose}. */
export async function mountGL(div, option, doc, opts) {
  const m = await loadGLCharts();
  return m.mountGLChartLive(div, option, doc, opts);
}
