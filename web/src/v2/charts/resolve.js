// resolveOption: a stored, theme-neutral ECharts option made drawable (canvas-v2-phase3-4.md 2.6).
// Pure: no DOM, no ECharts, the same result for the same input. glCharts.js (3D-WEB) imports it too.
//
//   "chart.cat.0", "tone.accent.solid"...   any string matching TOKEN_REF -> the palette's hex
//   {"$doc": "<name>"}                       -> the doc asset's refs[name] (sankey links, pie data...)
//   "$sans" / "$mono"                        -> the page's font families
//   {"$fmt": "<fmt id>"}                     -> one of format.js's fixed formatter functions
//   no `dataset` but a doc with datasets     -> dataset = the doc's datasets
//   fixed keys                               -> animation false, richText confined tooltips, no
//                                               toolbox / title / graphic, no extraCssText
//
// Data is never read as token refs: not the doc's (except colour keys inside `$doc` refs, a pie's
// per-slice itemStyle.color), and not names in the option (legend and axis `data`, `name`,
// formatters): a category named "chart.cat.0" stays a name.
import { formatterFor } from "./format.js";
import { chartPalette, MONO_FAMILY, SANS_FAMILY } from "./theme.js";
import { themeOfPalette } from "./picture.js";
import { warnOnce } from "../theme/palette.js";

export { themeOfPalette };

export const TOKEN_REF = /^(chart|tone|base|mat)\.[a-z0-9_.]+$/;
const HEX = /^#[0-9a-fA-F]{6}$/;
const COLOUR_KEY = /colou?r$/i;
// Keys whose string values ECharts' SSR writes into SVG attributes (fill, stroke, style, d) without
// escaping them: a quote or angle bracket there would break the SVG image, so they are scrubbed.
const ATTR_KEY = /(?:colou?r|fontFamily|fontWeight|fontStyle|symbol|borderType|type|align|verticalAlign|cursor|shape)$/i;
// Keys whose strings are data (category and series names, labels, templates): never token refs,
// so a category called "base.ink" stays a name.
const DATA_KEY = /^(?:data|name|text|formatter|valueFormatter|description|dimensions|id|seriesName|subtext)$/;
const scrub = (value) => value.replace(/"/g, "'").replace(/[<>&]/g, "");
// Top-level keys the page never draws from an option (D16 titles are display-list text).
const DROP_TOP = ["toolbox", "title", "graphic", "brush", "timeline"];

function isPlain(value) {
  return !!value && typeof value === "object" && !Array.isArray(value);
}

function single(value, key) {
  return isPlain(value) && Object.keys(value).length === 1 && Object.prototype.hasOwnProperty.call(value, key);
}

function colour(ref, palette) {
  const hex = palette[ref];
  if (typeof hex === "string" && HEX.test(hex)) return hex.toLowerCase();
  warnOnce(`chart-ref:${ref}`, `unknown chart colour reference ${JSON.stringify(ref)} drawn as chart.ink`);
  return palette["chart.ink"] || "#1c2024";
}

// Deep copy of doc data, resolving token refs only under colour keys.
function copyData(value, palette, key = "") {
  if (Array.isArray(value)) return value.map((v) => copyData(v, palette, key));
  if (isPlain(value)) {
    const out = {};
    for (const [k, v] of Object.entries(value)) out[k] = copyData(v, palette, k);
    return out;
  }
  if (typeof value === "string" && COLOUR_KEY.test(key) && TOKEN_REF.test(value)) return colour(value, palette);
  if (typeof value === "string" && ATTR_KEY.test(key)) return scrub(value);
  return value;
}

function docRef(name, doc, palette) {
  const refs = doc && isPlain(doc.refs) ? doc.refs : {};
  if (Object.prototype.hasOwnProperty.call(refs, name)) return copyData(refs[name], palette);
  if (name === "datasets" && Array.isArray(doc?.datasets)) return datasetsOf(doc);
  warnOnce(`chart-doc:${name}`, `chart option names a missing doc ref ${JSON.stringify(name)}`);
  return [];
}

/** The doc's datasets as ECharts dataset options ({id, dimensions, source}). */
export function datasetsOf(doc) {
  if (!doc || !Array.isArray(doc.datasets)) return [];
  return doc.datasets.map((d) => {
    const out = { source: Array.isArray(d.source) ? d.source.map((row) => (Array.isArray(row) ? row.slice() : copyData(row, {}))) : [] };
    if (d.id !== undefined) out.id = String(d.id);
    if (Array.isArray(d.dimensions)) out.dimensions = d.dimensions.slice();
    if (d.sourceHeader !== undefined) out.sourceHeader = d.sourceHeader;
    return out;
  });
}

function walk(value, doc, palette, key) {
  if (Array.isArray(value)) return value.map((v) => walk(v, doc, palette, key));
  if (isPlain(value)) {
    if (single(value, "$doc")) return docRef(String(value.$doc), doc, palette);
    if (Object.prototype.hasOwnProperty.call(value, "$fmt")) return formatterFor(String(value.$fmt));
    const out = {};
    for (const [k, v] of Object.entries(value)) {
      if (k === "extraCssText") continue;
      out[k] = walk(v, doc, palette, k);
    }
    return out;
  }
  if (typeof value === "string") {
    if (DATA_KEY.test(key)) return value;
    if (value === "$sans") return SANS_FAMILY;
    if (value === "$mono") return MONO_FAMILY;
    if (TOKEN_REF.test(value)) return colour(value, palette);
    if (ATTR_KEY.test(key)) return scrub(value);
  }
  return value;
}

function forceTooltip(tooltip) {
  if (Array.isArray(tooltip)) return tooltip.map(forceTooltip);
  if (!isPlain(tooltip)) return tooltip;
  const out = { ...tooltip, renderMode: "richText", confine: true };
  delete out.extraCssText;
  return out;
}

/**
 * The option ready for setOption. `option` is the element's stored option (or a sanitised raw
 * one), `doc` the chart's doc asset (or null), `palette` the flat palette of the theme being drawn
 * (chart.* keys are filled in when it lacks them), `theme` which theme that palette is (read off
 * the palette when omitted).
 */
export function resolveOption(option, doc, palette, theme = themeOfPalette(palette)) {
  const p = chartPalette(palette, theme);
  const out = walk(isPlain(option) ? option : {}, doc, p, "");
  for (const key of DROP_TOP) delete out[key];
  out.animation = false;
  if (out.tooltip !== undefined) out.tooltip = forceTooltip(out.tooltip);
  if (out.dataset === undefined && doc && Array.isArray(doc.datasets) && doc.datasets.length) out.dataset = datasetsOf(doc);
  if (!out.textStyle || !isPlain(out.textStyle)) out.textStyle = {};
  if (!out.textStyle.fontFamily) out.textStyle.fontFamily = SANS_FAMILY;
  return out;
}
