// The raw ECharts option sanitiser, the page's copy (canvas-v2-phase3-4.md 2.8). Python sanitises
// a raw option on write (herdr_team/canvas_charts/_sanitize.py); the page sanitises it again before
// setOption, as defence in depth, from the SAME allow table (assets/canvas/echarts-allow.json,
// written by `python3 -m herdr_team.canvas_charts._sanitize --write`), so the two cannot drift.
// tests/fixtures/charts/sanitize-vectors.json (_sanitize.vectors()) holds both to the same outputs.
//
// Refused (the whole option; the chart draws its fallback): not an object, past the size, depth,
// number or string limits, a string that loads something (a remote, data, script, blob, file or
// image URL; `javascript:` anywhere), no series, or a series type the table does not allow.
// Stripped (listed): top-level keys outside the table, denied keys anywhere (a sankey link's
// `target` node is kept), non-string formatters, and image-carrying image/backgroundColor/color
// objects.
// Forced: animation false; tooltip in richText, confined (never HTML).

// The generated table when it exists; the Python table's values otherwise (before it is written).
const found = import.meta.glob("../../../../assets/canvas/echarts-allow.json", { eager: true, import: "default" });

/** _sanitize.table(), for a checkout where assets/canvas/echarts-allow.json is not written yet. */
export const DEFAULT_ALLOW = {
  v: 1,
  top: [
    "series", "dataset", "xAxis", "yAxis", "grid", "legend", "tooltip", "color", "radar", "polar", "radiusAxis", "angleAxis",
    "parallel", "parallelAxis", "singleAxis", "calendar", "visualMap", "dataZoom", "textStyle", "aria", "markLine", "markPoint",
  ],
  series: [
    "bar", "line", "scatter", "effectScatter", "pie", "heatmap", "boxplot", "funnel", "treemap", "sankey", "radar", "gauge",
    "parallel", "candlestick", "themeRiver", "bar3D", "scatter3D", "surface",
  ],
  deny_keys: [
    "extraCssText", "toolbox", "dataView", "link", "sublink", "target", "triggerEvent", "transform", "graphic", "title", "brush",
    "timeline", "geo", "bmap", "map", "html", "tooltipFormatter", "renderItem",
  ],
  deny_values: ["http:", "https:", "//", "data:", "javascript:", "vbscript:", "blob:", "file:", "image://", "url("],
  limits: { bytes: 65536, depth: 32, numbers: 20000, string: 1000 },
  forced: { animation: false, tooltip: { renderMode: "richText", confine: true } },
};

/** The series echarts-gl draws (scene3d/glCharts.js); a raw option with one renders through it. */
export const GL_SERIES = ["bar3D", "scatter3D", "surface"];

const list = (value, fallback) => (Array.isArray(value) ? value.map(String) : fallback);
const limit = (value, fallback) => (Number.isFinite(Number(value)) && Number(value) > 0 ? Number(value) : fallback);

/** The table in the shape this file reads (missing parts from DEFAULT_ALLOW). */
export function normalizeAllow(table) {
  const t = table && typeof table === "object" && !Array.isArray(table) ? table : DEFAULT_ALLOW;
  const limits = t.limits && typeof t.limits === "object" ? t.limits : {};
  const d = DEFAULT_ALLOW.limits;
  return {
    v: t.v ?? 1,
    top: list(t.top, DEFAULT_ALLOW.top),
    series: list(t.series, DEFAULT_ALLOW.series),
    deny_keys: list(t.deny_keys, DEFAULT_ALLOW.deny_keys),
    deny_values: list(t.deny_values, DEFAULT_ALLOW.deny_values).map((v) => v.toLowerCase()),
    limits: { bytes: limit(limits.bytes, d.bytes), depth: limit(limits.depth, d.depth), numbers: limit(limits.numbers, d.numbers), string: limit(limits.string, d.string) },
  };
}

export const ALLOW_FROM_FILE = Object.values(found)[0] || null;
export const ALLOW = normalizeAllow(ALLOW_FROM_FILE || DEFAULT_ALLOW);

const isPlain = (v) => !!v && typeof v === "object" && !Array.isArray(v);
const has = (obj, key) => Object.prototype.hasOwnProperty.call(obj, key);
const WINDOW_TARGETS = ["blank", "self", "_blank", "_self", "parent", "_parent", "top", "_top"];

class Refusal extends Error {}

// _sanitize._walk_limits: every value and size limit, over the whole input (a hostile value inside
// a key that would be stripped still refuses the option: nothing hostile is passed over silently).
function audit(value, table, path, depth, counter) {
  if (depth > table.limits.depth) throw new Refusal(`the ECharts option nests deeper than ${table.limits.depth} levels at ${path}`);
  if (value === null || typeof value === "boolean") return;
  if (typeof value === "number") {
    counter.numbers += 1;
    if (counter.numbers > table.limits.numbers) throw new Refusal(`the ECharts option holds more than ${table.limits.numbers} numbers`);
    return;
  }
  if (typeof value === "string") {
    const length = [...value].length;
    if (length > table.limits.string) throw new Refusal(`${path} is a string of ${length} characters; the limit is ${table.limits.string}`);
    const lowered = value.trim().toLowerCase();
    if (table.deny_values.some((prefix) => lowered.startsWith(prefix)) || lowered.includes("javascript:")) {
      throw new Refusal(`${path} loads something (${value.trim().slice(0, 24)}...); an option is data only`);
    }
    return;
  }
  if (Array.isArray(value)) {
    value.forEach((item, index) => audit(item, table, `${path}[${index}]`, depth + 1, counter));
    return;
  }
  if (isPlain(value)) {
    for (const [key, item] of Object.entries(value)) audit(item, table, `${path}.${key}`, depth + 1, counter);
    return;
  }
  throw new Refusal(`${path} is not JSON data`);
}

// A `target` naming a node (a sankey or graph link's end), not a link's window target.
function nodeRef(holder, value) {
  return !has(holder, "link") && !has(holder, "sublink") && has(holder, "source") && !WINDOW_TARGETS.includes(String(value).trim().toLowerCase());
}

// _sanitize._strip
function strip(value, table, path, stripped) {
  if (Array.isArray(value)) return value.map((item, index) => strip(item, table, `${path}[${index}]`, stripped));
  if (!isPlain(value)) return value;
  const out = {};
  for (const [key, item] of Object.entries(value)) {
    const where = path ? `${path}.${key}` : key;
    if (table.deny_keys.includes(key) && !(key === "target" && nodeRef(value, item))) {
      stripped.push(where);
      continue;
    }
    if (key === "formatter" && typeof item !== "string") {
      stripped.push(where);
      continue;
    }
    if ((key === "image" || key === "backgroundColor" || key === "color") && isPlain(item) && has(item, "image")) {
      stripped.push(where);
      continue;
    }
    out[key] = strip(item, table, where, stripped);
  }
  return out;
}

const utf8Length = (text) => new TextEncoder().encode(text).length;

/**
 * {option, stripped, refused}: the option made safe, the paths removed from it, and null or the
 * reason the whole option is refused (option is then null). The input is never changed.
 */
export function sanitize(input, allow = ALLOW) {
  const table = allow === ALLOW ? ALLOW : normalizeAllow(allow);
  const stripped = [];
  try {
    if (!isPlain(input)) throw new Refusal("echarts is an ECharts option: a JSON object");
    let text;
    try {
      text = JSON.stringify(input);
    } catch {
      throw new Refusal("the ECharts option is not JSON data");
    }
    if (utf8Length(text) > table.limits.bytes) throw new Refusal(`the ECharts option is over ${Math.floor(table.limits.bytes / 1024)} KB`);
    audit(input, table, "echarts", 0, { numbers: 0 });

    const top = {};
    for (const [key, value] of Object.entries(input)) {
      if (!table.top.includes(key)) stripped.push(key);
      else top[key] = value;
    }
    let series = top.series;
    if (series === undefined || series === null) throw new Refusal(`the ECharts option has no series; one of: ${table.series.join(", ")}`);
    if (isPlain(series)) series = [series];
    if (!Array.isArray(series) || !series.length) throw new Refusal("echarts.series is a list of series");
    series.forEach((item, index) => {
      const type = isPlain(item) ? item.type : undefined;
      if (typeof type !== "string" || !table.series.includes(type)) {
        throw new Refusal(`echarts.series[${index}] is a ${JSON.stringify(type ?? null)} series; the page draws: ${table.series.join(", ")}`);
      }
    });
    top.series = series;
    const out = strip(top, table, "", stripped);
    out.tooltip = { ...(isPlain(out.tooltip) ? out.tooltip : {}), renderMode: "richText", confine: true };
    out.animation = false;
    return { option: out, stripped, refused: null };
  } catch (err) {
    if (err instanceof Refusal) return { option: null, stripped: [], refused: err.message };
    throw err;
  }
}

/** True when a (sanitised) option draws a series only echarts-gl draws. */
export function needsGL(option) {
  const series = option && Array.isArray(option.series) ? option.series : [];
  return series.some((s) => isPlain(s) && GL_SERIES.includes(s.type));
}
