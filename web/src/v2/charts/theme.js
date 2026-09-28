// Chart colours and the two ECharts themes (canvas-v2-phase3-4.md 1.5, 2.10).
//
// A chart option is theme-neutral: its colours are token refs ("chart.cat.0", "chart.axis",
// "tone.accent.solid", "mat.info.top") that resolve.js replaces from a flat {ref: "#rrggbb"}
// palette. The display list's palettes carry the chart.* and mat.* keys (1.5); before a list
// arrives, or for one written before those tokens existed, chartPalette() fills them from the
// bundled token file's `chart` and `mat` blocks (flat {"chart.cat.0": "#3e63dd"} maps per theme,
// written by canvas_theme) and, failing that, from the defaults below (the 1.5 values), so a chart
// always has every colour.
//
// The ECharts themes set what an option leaves out (axis, split line, text, legend and tooltip
// colours) from the same palette. They are registered as "synapse-light" and "synapse-dark", and
// again when the palette they came from changes.
import tokens from "@synapse/tokens";
import { fallbackPalette } from "../theme/palette.js";

export const THEME_NAME = { light: "synapse-light", dark: "synapse-dark" };

// Single quotes: ECharts writes the family into an SVG style="..." attribute without escaping, so a double
// quote would end the attribute and break the SVG image.
export const SANS_FAMILY = "'Synapse Sans', Inter, Helvetica, Arial, sans-serif";
export const MONO_FAMILY = "'Synapse Mono', 'Geist Mono', ui-monospace, Menlo, monospace";

const HEX = /^#[0-9a-fA-F]{6}$/;

// The 1.5 values; `paper`, `ink`, `muted` and `highlight` are refs into the base palette.
export const CHART_DEFAULTS = {
  light: {
    paper: "base.surface",
    ink: "base.ink",
    muted: "base.ink_muted",
    axis: "#8b8d98",
    gridline: "#e8e8ec",
    cat: ["#3e63dd", "#e5484d", "#30a46c", "#f76b15", "#8e4ec6", "#12a594", "#d6409f", "#ffc53d", "#0090ff", "#978365"],
    seq: ["#edf2fe", "#d2deff", "#abbdf9", "#8da4ef", "#5b5bd6", "#3e63dd", "#3358d4", "#1f2d5c", "#141726"],
    div: ["#b54548", "#e5484d", "#f4a9aa", "#fdd8d8", "#f0f0f3", "#d2deff", "#8da4ef", "#3e63dd", "#1f2d5c"],
    highlight: "tone.accent.solid",
    dim: "#d9d9e0",
  },
  dark: {
    paper: "base.surface",
    ink: "base.ink",
    muted: "base.ink_muted",
    axis: "#6f6d78",
    gridline: "#2e2e35",
    cat: ["#5472e4", "#ec5d5e", "#33b074", "#ff801f", "#9a5cd0", "#0eb39e", "#dd4ea3", "#ffc53d", "#3b9eff", "#a39073"],
    seq: ["#182449", "#1d2e62", "#25397c", "#304384", "#3a5bc7", "#5472e4", "#849dff", "#9eb1ff", "#d6e1ff"],
    div: ["#ff9592", "#ec5d5e", "#b54548", "#611623", "#2b2b31", "#1d2e62", "#3a5bc7", "#849dff", "#d6e1ff"],
    highlight: "tone.accent.solid",
    dim: "#3a3a42",
  },
};

// A token block as {ref: value}: a flat map whose keys are already refs ("chart.cat.0", the token
// file's shape) is taken as it is; the 1.5 shape ({cat: [...], axis: "#..."}) is flattened.
function flattenBlock(block, prefix, out = {}) {
  for (const [key, value] of Object.entries(block && typeof block === "object" ? block : {})) {
    if (key.startsWith(`${prefix}.`)) {
      if (typeof value === "string") out[key] = value;
    } else if (Array.isArray(value)) value.forEach((v, i) => (out[`${prefix}.${key}.${i}`] = v));
    else if (typeof value === "string") out[`${prefix}.${key}`] = value;
  }
  return out;
}

// A hex, or a ref resolved through `palette` (one level: token values are refs into base/tone).
function hexOf(value, palette) {
  if (typeof value !== "string") return null;
  if (HEX.test(value)) return value.toLowerCase();
  const inner = palette[value];
  return typeof inner === "string" && HEX.test(inner) ? inner.toLowerCase() : null;
}

const cache = new WeakMap();

/**
 * The palette with every chart.* key a hex: the palette's own value, else the token file's chart
 * block, else CHART_DEFAULTS. `palette` is a flat {ref: hex} map (the display list's for `theme`);
 * a missing one is the bundled tokens'. The result is cached per palette object.
 */
export function chartPalette(palette, theme = "light") {
  const t = theme === "dark" ? "dark" : "light";
  const base = palette && typeof palette === "object" ? palette : fallbackPalette(t);
  const perTheme = cache.get(base);
  if (perTheme && perTheme[t]) return perTheme[t];
  const out = { ...base };
  // Later layers win over earlier ones; the palette's own hex wins over all of them.
  const layers = [flattenBlock(CHART_DEFAULTS[t], "chart"), flattenBlock(tokens?.chart?.[t], "chart"), flattenBlock(tokens?.mat?.[t], "mat")];
  for (const layer of layers) {
    for (const [ref, value] of Object.entries(layer)) {
      if (HEX.test(String(base[ref] || ""))) continue;
      const hex = hexOf(value, base);
      if (hex) out[ref] = hex;
    }
  }
  // A base palette without the refs the defaults name still gets a colour for each.
  const ink = hexOf(base["base.ink"], base) || (t === "dark" ? "#edeef0" : "#1c2024");
  for (const key of ["chart.paper", "chart.ink", "chart.muted", "chart.highlight"]) {
    if (!HEX.test(String(out[key] || ""))) out[key] = key === "chart.paper" ? (t === "dark" ? "#1b1c20" : "#ffffff") : ink;
  }
  const next = { ...(perTheme || {}), [t]: out };
  cache.set(base, next);
  return out;
}

/** chart.cat.0..n as a list of hexes. */
export function categorical(palette) {
  const out = [];
  for (let i = 0; HEX.test(String(palette[`chart.cat.${i}`] || "")); i += 1) out.push(palette[`chart.cat.${i}`]);
  return out;
}

function ramp(palette, name) {
  const out = [];
  for (let i = 0; HEX.test(String(palette[`chart.${name}.${i}`] || "")); i += 1) out.push(palette[`chart.${name}.${i}`]);
  return out;
}

/**
 * An ECharts theme object for a chart palette (chartPalette's result). Transparent background: the
 * card behind the slot is the display list's chart.paper rect (D16).
 */
export function buildTheme(p) {
  const text = { color: p["chart.muted"], fontFamily: SANS_FAMILY, fontSize: 12 };
  const axis = {
    axisLine: { show: true, lineStyle: { color: p["chart.axis"] } },
    axisTick: { show: true, lineStyle: { color: p["chart.axis"] } },
    axisLabel: { color: p["chart.muted"], fontFamily: SANS_FAMILY, fontSize: 12 },
    splitLine: { show: true, lineStyle: { color: p["chart.gridline"] } },
    splitArea: { show: false },
    nameTextStyle: { color: p["chart.muted"], fontFamily: SANS_FAMILY },
  };
  return {
    color: categorical(p),
    backgroundColor: "transparent",
    textStyle: { ...text, color: p["chart.ink"] },
    title: { textStyle: { color: p["chart.ink"] }, subtextStyle: { color: p["chart.muted"] } },
    legend: { textStyle: { color: p["chart.ink"], fontFamily: SANS_FAMILY, fontSize: 12 }, inactiveColor: p["chart.dim"] },
    tooltip: {
      backgroundColor: p["chart.paper"],
      borderColor: p["chart.axis"],
      textStyle: { color: p["chart.ink"], fontFamily: SANS_FAMILY, fontSize: 12 },
    },
    categoryAxis: { ...axis, splitLine: { show: false, lineStyle: { color: p["chart.gridline"] } } },
    valueAxis: { ...axis, axisLine: { show: false, lineStyle: { color: p["chart.axis"] } }, axisTick: { show: false } },
    timeAxis: axis,
    logAxis: axis,
    visualMap: { inRange: { color: ramp(p, "seq") }, textStyle: { color: p["chart.muted"] } },
    line: { symbol: "circle", symbolSize: 5 },
    markLine: { lineStyle: { color: p["chart.muted"] }, label: { color: p["chart.muted"] } },
    sankey: { lineStyle: { color: "source", opacity: 0.35 }, label: { color: p["chart.ink"] } },
    treemap: { label: { color: p["chart.paper"] }, upperLabel: { color: p["chart.ink"] } },
    radar: {
      axisName: { color: p["chart.muted"] },
      axisLine: { lineStyle: { color: p["chart.axis"] } },
      splitLine: { lineStyle: { color: p["chart.gridline"] } },
      splitArea: { show: false },
    },
    gauge: { axisLabel: { color: p["chart.muted"] }, detail: { color: p["chart.ink"] } },
  };
}

// Per ECharts namespace (the chart chunk and the GL chunk may each hold one): the palette each
// theme name was last registered from.
const registered = new WeakMap();
const signatures = new WeakMap();

function signature(p) {
  if (!signatures.has(p)) signatures.set(p, JSON.stringify(p));
  return signatures.get(p);
}

/**
 * Registers the theme for `theme` from its chart palette when it is not registered yet or the
 * palette changed; returns the theme name to pass to echarts.init.
 */
export function ensureTheme(echarts, theme, palette) {
  const t = theme === "dark" ? "dark" : "light";
  const p = chartPalette(palette, t);
  const key = signature(p);
  let seen = registered.get(echarts);
  if (!seen) {
    seen = { light: null, dark: null };
    registered.set(echarts, seen);
  }
  if (seen[t] !== key) {
    echarts.registerTheme(THEME_NAME[t], buildTheme(p));
    seen[t] = key;
  }
  return THEME_NAME[t];
}
