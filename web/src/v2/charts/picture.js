// What the first-load chunk needs to place a chart picture (canvas-v2-phase3-4.md 2.10): which
// engine draws an element, the picture cache and its key, and the theme a palette is. Everything
// that draws (render.js with resolve, sanitize, format, theme, labels, and ECharts itself) loads
// lazily with the first chart, so a board without charts pays nothing for them.

export const ENGINES = ["echarts", "echarts-raw", "vega-lite", "gl"];
const CACHE_MAX = 160;
const HEX = /^#[0-9a-fA-F]{6}$/;

/** Which renderer draws an element: "gl", "echarts", "echarts-raw" or "vega-lite" (the default). */
export function engineOf(element) {
  if (element && element.chart && element.chart.gl) return "gl";
  const engine = element && typeof element.engine === "string" ? element.engine : "";
  return engine === "echarts" || engine === "echarts-raw" ? engine : "vega-lite";
}

/** The chart type to report (flat charts carry chart.type; others their settings' type). */
export function typeOf(element) {
  return (element && element.chart && element.chart.type) || (element && element.settings && element.settings.type) || null;
}

/** "dark" when the palette's surface is dark, else "light" (for callers that pass no theme). */
export function themeOfPalette(palette) {
  const hex = palette && typeof palette["base.surface"] === "string" ? palette["base.surface"] : "";
  if (!HEX.test(hex)) return "light";
  const n = parseInt(hex.slice(1), 16);
  const lum = 0.2126 * ((n >> 16) & 255) + 0.7152 * ((n >> 8) & 255) + 0.0722 * (n & 255);
  return lum < 128 ? "dark" : "light";
}

export const px = (v, min = 1) => Math.max(min, Math.round(Number(v) || 0));

let renderModule = null;

/** render.js (the drawing half), imported once for the page. */
export function loadRender() {
  if (!renderModule) {
    renderModule = import("./render.js").catch((err) => {
      renderModule = null;
      throw err;
    });
  }
  return renderModule;
}

const cache = new Map();

/** The cache key of one picture: (team, id, version, theme, size). */
export function pictureKey(team, element, theme, w, h) {
  return `${team}|${element?.id}|${element?.updated_seq ?? 0}|${theme}|${px(w)}x${px(h)}`;
}

/** make() once per key (the most recent CACHE_MAX pictures are kept; a failure is retried). */
export function cachedPicture(key, make) {
  if (cache.has(key)) {
    const hit = cache.get(key);
    cache.delete(key);
    cache.set(key, hit);
    return hit;
  }
  const promise = make().catch((err) => {
    cache.delete(key);
    throw err;
  });
  cache.set(key, promise);
  while (cache.size > CACHE_MAX) cache.delete(cache.keys().next().value);
  return promise;
}

/** Test hook. */
export function clearPictures() {
  cache.clear();
}
