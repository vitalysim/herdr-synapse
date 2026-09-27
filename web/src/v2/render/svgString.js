// toSVGString: a display list as one standalone SVG document, byte-identical to
// herdr_team/canvas_svg.write in canonical mode (canvas-v2-phase1.md 1.8; goldens in
// tests/fixtures/display/<scene>.<theme>.svg). The page uses it for exports.
//
// The canonical form, exactly (both writers):
//   <svg xmlns="http://www.w3.org/2000/svg" width="W" height="H" viewBox="x y w h">
//   [<defs>filters synapse-elev-N by N, patterns synapse-hatch-K, clipPaths synapse-clip-K</defs>]
//   <rect x="x" y="y" width="w" height="h" fill="{base.canvas}"/>
//   <g data-layer="zones">[<g data-id="E-n">nodes</g>]...</g> ... marks, labels, overlays
//   </svg>
// - The box is `region`, else the list's bbox, as [x0, y0, x1, y1] normalised like view_box (min and
//   max per axis, at least one unit wide and tall). W and H are pixel_size(box, maxPx): the longer
//   side scaled to maxPx, each rounded half to even (Python's round), at least 1. The scale every
//   lod, zoom text, sw_px and screen group uses is 1 / u, with u = (x1 - x0) / W.
// - All four layer groups are always written. An entry is written in a layer when its bbox meets the
//   box (strictly: a.x0 < b.x1 and b.x0 < a.x1, the same for y; an entry without a bbox always
//   meets it) and at least one of its items in that layer draws at this scale.
// - A text line with dir "rtl" is <text x y unicode-bidi="plaintext"> (never direction="rtl":
//   browsers mirror text-anchor under it and resvg does not, so the line would leave its box).
// - No whitespace or newline anywhere between elements. Numbers are fmt (1.7), attributes are in
//   svgAttrs.js order, `<tag/>` for an element without children. Escaping drops XML-invalid control
//   characters and escapes & < > " in both attribute values and text.
// - Canonical families are Inter and Geist Mono; canonical urls are synapse-asset:<name> and
//   synapse-still:<name>. The page passes "Synapse Sans"/"Synapse Mono" and real urls.
import { fmt, roundHalfEven } from "./fmt.js";
import { CANONICAL_FAMILIES, LAYER_NAMES, defsNodes, entryNodes, escapeXML, serialize } from "./svgAttrs.js";
import { Defs, paletteOf, resolvePaint, shadowsOf } from "../theme/palette.js";

export { fmt } from "./fmt.js";

export const CANONICAL_URLS = {
  asset: (name) => `synapse-asset:${name}`,
  still: (name) => `synapse-still:${name}`,
};

const EMPTY_BOX = [-40, -40, 440, 340];

/** A region or bbox as [x0, y0, x1, y1], normalised the way canvas_render.view_box treats a region. */
export function normalizeBox(box) {
  if (!Array.isArray(box) || box.length !== 4 || !box.every(Number.isFinite)) return EMPTY_BOX.slice();
  const [a, b, c, d] = box.map(Number);
  const x0 = Math.min(a, c);
  const y0 = Math.min(b, d);
  return [x0, y0, Math.max(a, c, x0 + 1), Math.max(b, d, y0 + 1)];
}

/** canvas_render.pixel_size: the picture's size in pixels, longer side maxPx. */
export function pixelSize(box, maxPx = 1024) {
  const width = box[2] - box[0];
  const height = box[3] - box[1];
  const s = maxPx / Math.max(width, height, 1);
  return [Math.max(1, roundHalfEven(width * s)), Math.max(1, roundHalfEven(height * s))];
}

function meets(entry, box) {
  const b = entry?.bbox;
  if (!Array.isArray(b) || b.length !== 4 || !b.every(Number.isFinite)) return true;
  return b[0] < box[2] && box[0] < b[2] && b[1] < box[3] && box[1] < b[3];
}

// {asset(name), still(name)} -> the url(src) svgAttrs asks for.
export function urlResolver(urls) {
  return (src) => {
    if (!src || typeof src !== "object") return null;
    if (typeof src.asset === "string" && src.asset && urls?.asset) return urls.asset(src.asset);
    if (typeof src.still === "string" && src.still && urls?.still) return urls.still(src.still);
    return null;
  };
}

/** The display list (or `region` of it) as a standalone SVG document string. */
export function toSVGString(dl, { theme = "light", region = null, maxPx = 1024, families = CANONICAL_FAMILIES, urls = CANONICAL_URLS } = {}) {
  const box = normalizeBox(region || dl?.bbox);
  const [W, H] = pixelSize(box, maxPx);
  const u = (box[2] - box[0]) / W;
  const palette = paletteOf(dl, theme);
  const defs = new Defs(shadowsOf(dl, theme));
  const ctx = { theme, palette, scale: 1 / u, families: families || CANONICAL_FAMILIES, url: urlResolver(urls || CANONICAL_URLS), defs };
  const entries = (Array.isArray(dl?.entries) ? dl.entries : []).filter((e) => e && typeof e === "object" && meets(e, box));
  let body = "";
  for (const layer of LAYER_NAMES) {
    body += `<g data-layer="${layer}">`;
    for (const entry of entries) {
      const nodes = entryNodes(entry, layer, ctx);
      if (!nodes.length) continue;
      body += `<g data-id="${escapeXML(entry.id ?? "")}">`;
      for (const node of nodes) body += serialize(node);
      body += "</g>";
    }
    body += "</g>";
  }
  const canvas = resolvePaint("base.canvas", palette);
  let out = `<svg xmlns="http://www.w3.org/2000/svg" width="${W}" height="${H}" viewBox="${fmt(box[0])} ${fmt(box[1])} ${fmt(box[2] - box[0])} ${fmt(box[3] - box[1])}">`;
  if (!defs.empty) out += `<defs>${defsNodes(defs).map(serialize).join("")}</defs>`;
  out += `<rect x="${fmt(box[0])}" y="${fmt(box[1])}" width="${fmt(box[2] - box[0])}" height="${fmt(box[3] - box[1])}" fill="${canvas}"/>`;
  return `${out}${body}</svg>`;
}
