// Display list primitive -> SVG node: the one mapping both the page (Surface) and toSVGString use,
// so what the page shows and what it exports are the same drawing (canvas-v2-phase1.md 1.3, 1.8).
//
// A node is [tag, attrs, children]: attrs is an ordered list of [name, value] pairs (values are
// strings, unescaped), children is null (an empty element, written <tag/>), an array of nodes, or
// a string (text content). The attribute order is the canonical order of 1.8; herdr_team/
// canvas_svg.py writes the same order, and golden files hold the two writers equal.
//
// ctx = {theme, palette, scale, families: {sans, mono}, url(src) -> string|null, defs: Defs, page?,
//        slot?(p, ctx) -> node|undefined}. `scale` is screen pixels per world unit: the camera's on
// the page, 1/u (u = world units per output pixel) in a written picture.
import { fmt } from "./fmt.js";
import { lodVisible, textLayout } from "./lod.js";
import { resolvePaint, warnOnce } from "../theme/palette.js";

export const CANONICAL_FAMILIES = { sans: "Inter", mono: "Geist Mono" };
// Faces named after the page's own, for scripts those lack, ahead of the browser's own fallback:
// the ones resvg falls back to for the agent's picture. Unnamed, Chrome on macOS picks a Bold
// face for weight 500 (GeezaPro-Bold, LucidaGrande-Bold) where resvg picks the regular Arial
// Hebrew and Geeza Pro, so the page drew Hebrew and Arabic heavier than the agent saw them (QA 1
// finding 8); named, both go through CSS weight matching. A name the system lacks is skipped.
// Add a script's face here, in one place.
export const SCRIPT_FALLBACKS = ['"Arial Hebrew"', '"Geeza Pro"'];
const withFallbacks = (face) => [face, ...SCRIPT_FALLBACKS].join(", ");
// The page's own faces (src/fonts.css): the same Inter and Geist Mono files, under names no system
// font can shadow, then SCRIPT_FALLBACKS. The browser check (measure.js) measures with these too.
export const PAGE_FAMILIES = { sans: withFallbacks('"Synapse Sans"'), mono: withFallbacks('"Synapse Mono"') };
export const LAYER_NAMES = ["zones", "marks", "labels", "overlays"];

const finite = (...values) => values.every((v) => typeof v === "number" && Number.isFinite(v));
const isPoint = (p) => Array.isArray(p) && p.length >= 2 && finite(p[0], p[1]);
const PATH_D = /^[MLCQZ0-9eE.,+\-\s]*$/;

function malformed(p, why) {
  warnOnce(`malformed:${p?.k}`, `skipped a malformed ${p?.k || "primitive"} (${why}); later ones are skipped silently`);
  return null;
}

function pointsAttr(points) {
  return points.map((p) => `${fmt(p[0])},${fmt(p[1])}`).join(" ");
}

function paint(value, ctx) {
  return resolvePaint(value, ctx.palette);
}

function paintString(resolved, ctx) {
  if (resolved === null) return "none";
  if (typeof resolved === "object") return `url(#${ctx.defs.hatch(resolved.hatch)})`;
  return resolved;
}

function opacity(p) {
  const op = p.op;
  if (typeof op !== "number" || !Number.isFinite(op) || op >= 1) return null;
  return fmt(Math.max(0, op));
}

function strokeWidth(p, ctx) {
  if (finite(p.sw_px)) return fmt(p.sw_px / ctx.scale);
  return fmt(finite(p.sw) ? p.sw : 1);
}

// stroke-width [stroke-dasharray] stroke-linecap stroke-linejoin, for a present stroke.
function strokeTail(p, ctx) {
  const out = [["stroke-width", strokeWidth(p, ctx)]];
  if (Array.isArray(p.dash) && p.dash.length === 2 && finite(p.dash[0], p.dash[1])) out.push(["stroke-dasharray", `${fmt(p.dash[0])} ${fmt(p.dash[1])}`]);
  out.push(["stroke-linecap", "round"], ["stroke-linejoin", "round"]);
  return out;
}

/** Below this many screen px per unit the page draws no elevation shadows. */
export const ELEV_MIN_SCALE = 0.35;

/** fill stroke stroke-width [stroke-dasharray] [caps when stroke] [opacity] [filter] (1.8). */
export function paintAttrs(p, ctx, { fill = true } = {}) {
  const attrs = [["fill", fill ? paintString(paint(p.fill, ctx), ctx) : "none"]];
  const stroke = paint(p.stroke, ctx);
  const strokeHex = stroke && typeof stroke === "object" ? stroke.hatch : stroke;
  attrs.push(["stroke", strokeHex || "none"]);
  if (strokeHex) attrs.push(...strokeTail(p, ctx));
  const op = opacity(p);
  if (op !== null) attrs.push(["opacity", op]);
  // A resting shadow is a few units wide: on the page it is dropped where it is under a pixel wide, since
  // every filtered element is repainted on its own on each pan frame (QA phase 2, F13).
  if (p.elev && !(ctx.page && finite(ctx.scale) && ctx.scale < ELEV_MIN_SCALE)) {
    const id = ctx.defs.elevation(p.elev);
    if (id) attrs.push(["filter", `url(#${id})`]);
  }
  return attrs;
}

function textNode(p, ctx) {
  if (!finite(p.x) || !Array.isArray(p.lines)) return malformed(p, "x or lines");
  const lay = textLayout(p, ctx.scale);
  if (!finite(lay.size) || lay.size <= 0) return malformed(p, "size");
  const fill = paint(p.fill === undefined ? "base.ink" : p.fill, ctx);
  const families = ctx.families || CANONICAL_FAMILIES;
  const attrs = [
    ["font-size", fmt(lay.size)],
    ["font-family", families[p.font] || families.sans],
    ["font-weight", fmt(finite(p.weight) ? p.weight : 400)],
    ["fill", paintString(fill, ctx)],
    ["text-anchor", ["start", "middle", "end"].includes(p.anchor) ? p.anchor : "start"],
  ];
  const op = opacity(p);
  if (op !== null) attrs.push(["opacity", op]);
  // Page only (never in a written picture): the fitted box width, and its height against the height the drawn lines
  // take, for the QA audit (qa.js; a label that wraps to more lines than its box holds spills out below it).
  if (ctx.page && !lay.zoomed && Array.isArray(p.box) && finite(p.box[2])) attrs.push(["data-box-w", fmt(p.box[2])]);
  if (ctx.page && !lay.zoomed && Array.isArray(p.box) && finite(p.box[3]) && finite(lay.lh)) {
    attrs.push(["data-box-h", fmt(p.box[3])], ["data-lines-h", fmt(p.lines.length * lay.lh)]);
  }
  const lines = [];
  p.lines.forEach((line, i) => {
    const y = lay.ys[i];
    if (!line || !finite(y)) return;
    // xml:space on each <text>, not on the group: the browser's own style for <text> resets
    // white-space, so an inherited xml:space is ignored and leading indentation collapses (QA 1, 4).
    const la = [
      ["x", fmt(p.x)],
      ["y", fmt(y)],
      ["xml:space", "preserve"],
    ];
    // A right-to-left line takes its base direction from its own text (so "אושר?" keeps its "?" on
    // the left, R-8) through unicode-bidi="plaintext", not direction="rtl": browsers mirror
    // text-anchor under direction="rtl" and resvg does not, so direction would move the line out of
    // the box it was fitted to in one of the two (renderers never move text). plaintext leaves the
    // anchor alone everywhere; resvg ignores it (its PNG keeps an LTR base, as before).
    if (line.dir === "rtl") la.push(["unicode-bidi", "plaintext"]);
    lines.push(["text", la, String(line.t ?? "")]);
  });
  return ["g", attrs, lines];
}

function imageNode(x, y, w, h, href, p) {
  const attrs = [
    ["x", fmt(x)],
    ["y", fmt(y)],
    ["width", fmt(w)],
    ["height", fmt(h)],
    ["preserveAspectRatio", "xMidYMid meet"],
    ["href", href],
  ];
  const op = p ? opacity(p) : null;
  if (op !== null) attrs.push(["opacity", op]);
  return ["image", attrs, null];
}

function headNode(head, strokeHex, p, ctx) {
  if (!head || typeof head !== "object") return null;
  if (head.shape === "dot") {
    if (!finite(head.cx, head.cy, head.r)) return null;
    return [
      "circle",
      [
        ["cx", fmt(head.cx)],
        ["cy", fmt(head.cy)],
        ["r", fmt(head.r)],
        ["fill", strokeHex || "none"],
        ["stroke", "none"],
      ],
      null,
    ];
  }
  if (!Array.isArray(head.points) || head.points.length < 2 || !head.points.every(isPoint)) return null;
  if (head.shape === "triangle") {
    return [
      "polygon",
      [
        ["points", pointsAttr(head.points)],
        ["fill", strokeHex || "none"],
        ["stroke", "none"],
      ],
      null,
    ];
  }
  // chevron (and any unknown head shape): an open stroke in the shaft's paint, never dashed.
  const attrs = [
    ["points", pointsAttr(head.points)],
    ["fill", "none"],
    ["stroke", strokeHex || "none"],
  ];
  if (strokeHex) attrs.push(["stroke-width", strokeWidth(p, ctx)], ["stroke-linecap", "round"], ["stroke-linejoin", "round"]);
  return ["polyline", attrs, null];
}

function groupTransform(p, ctx) {
  const parts = [];
  if (Array.isArray(p.screen) && p.screen.length === 2 && finite(p.screen[0], p.screen[1])) {
    parts.push(`translate(${fmt(p.screen[0])} ${fmt(p.screen[1])}) scale(${(ctx.scaleFmt || fmt)(1 / ctx.scale)})`);
  }
  if (Array.isArray(p.t) && p.t.length === 6 && finite(...p.t)) parts.push(`matrix(${p.t.map(fmt).join(" ")})`);
  return parts.join(" ");
}

function children(items, ctx) {
  const out = [];
  for (const item of Array.isArray(items) ? items : []) {
    const node = primitiveNode(item, ctx);
    if (node) out.push(node);
  }
  return out;
}

/** One primitive as a node, or null when it is hidden at this scale, unknown or malformed. */
export function primitiveNode(p, ctx) {
  if (!p || typeof p !== "object") return null;
  if (!lodVisible(p, ctx.scale)) return null;
  switch (p.k) {
    case "rect": {
      if (!finite(p.x, p.y, p.w, p.h) || p.w < 0 || p.h < 0) return malformed(p, "x y w h");
      const attrs = [
        ["x", fmt(p.x)],
        ["y", fmt(p.y)],
        ["width", fmt(p.w)],
        ["height", fmt(p.h)],
      ];
      if (finite(p.r) && fmt(p.r) !== "0" && p.r > 0) attrs.push(["rx", fmt(p.r)]);
      return ["rect", attrs.concat(paintAttrs(p, ctx)), null];
    }
    case "ellipse":
      if (!finite(p.cx, p.cy, p.rx, p.ry) || p.rx < 0 || p.ry < 0) return malformed(p, "cx cy rx ry");
      return [
        "ellipse",
        [
          ["cx", fmt(p.cx)],
          ["cy", fmt(p.cy)],
          ["rx", fmt(p.rx)],
          ["ry", fmt(p.ry)],
          ...paintAttrs(p, ctx),
        ],
        null,
      ];
    case "poly":
      if (!Array.isArray(p.points) || p.points.length < 2 || !p.points.every(isPoint)) return malformed(p, "points");
      return [p.closed ? "polygon" : "polyline", [["points", pointsAttr(p.points)], ...paintAttrs(p, ctx, { fill: !!p.closed })], null];
    case "line":
      if (!Array.isArray(p.points) || p.points.length < 2 || !p.points.every(isPoint)) return malformed(p, "points");
      return ["polyline", [["points", pointsAttr(p.points)], ...paintAttrs(p, ctx, { fill: false })], null];
    case "path": {
      if (typeof p.d !== "string" || !PATH_D.test(p.d)) return malformed(p, "d");
      const attrs = [["d", p.d]];
      if (p.rule === "evenodd") attrs.push(["fill-rule", "evenodd"]);
      return ["path", attrs.concat(paintAttrs(p, ctx)), null];
    }
    case "arrow": {
      if (typeof p.d !== "string" || !PATH_D.test(p.d)) return malformed(p, "d");
      const stroke = paint(p.stroke, ctx);
      const strokeHex = stroke && typeof stroke === "object" ? stroke.hatch : stroke;
      const shaft = [
        ["d", p.d],
        ["fill", "none"],
        ["stroke", strokeHex || "none"],
      ];
      if (strokeHex) shaft.push(...strokeTail(p, ctx));
      const nodes = [["path", shaft, null]];
      for (const head of Array.isArray(p.heads) ? p.heads : []) {
        const node = headNode(head, strokeHex, p, ctx);
        if (node) nodes.push(node);
      }
      const op = opacity(p);
      return ["g", op === null ? [] : [["opacity", op]], nodes];
    }
    case "text":
      return textNode(p, ctx);
    case "image": {
      if (!finite(p.x, p.y, p.w, p.h)) return malformed(p, "x y w h");
      const href = p.src && typeof p.src === "object" ? ctx.url(p.src) : null;
      if (!href) return malformed(p, "src");
      return imageNode(p.x, p.y, p.w, p.h, href, p);
    }
    case "slot": {
      if (ctx.slot) {
        const live = ctx.slot(p, ctx);
        if (live !== undefined) return live;
      }
      return slotNode(p, ctx);
    }
    case "group": {
      const attrs = [];
      const transform = groupTransform(p, ctx);
      if (transform) attrs.push(["transform", transform]);
      if (Array.isArray(p.clip) && p.clip.length === 4 && finite(...p.clip)) {
        attrs.push(["clip-path", `url(#${ctx.defs.clip(p.clip.map(fmt).join(" "))})`]);
      }
      const op = opacity(p);
      if (op !== null) attrs.push(["opacity", op]);
      return ["g", attrs, children(p.items, ctx)];
    }
    default:
      warnOnce(`unknown:${p.k}`, `a primitive of unknown kind ${JSON.stringify(p.k)} draws nothing`);
      return null;
  }
}

/**
 * Whether a slot is drawn from its still (canvas-v2-phase3-4.md 1.4, the writers' still rule, the
 * same in canvas_svg.slot_node): when it has one, and either the theme is light or its fallback is
 * not a faithful drawing (`drawn`). Stills are light pictures; a dark board draws the drawing.
 */
export function usesStill(p, theme) {
  return typeof p?.still === "string" && !!p.still && (theme !== "dark" || p.drawn !== true);
}

/** A slot drawn without the browser: its still when the still rule allows it, else its fallback primitives. */
export function slotNode(p, ctx, { still = true } = {}) {
  const inner = [];
  const href = still && usesStill(p, ctx.theme) && finite(p.x, p.y, p.w, p.h) ? ctx.url({ still: p.still }) : null;
  if (href) inner.push(imageNode(p.x, p.y, p.w, p.h, href, null));
  else inner.push(...children(p.fallback, ctx));
  return ["g", [["data-slot", String(p.slot ?? "")]], inner];
}

/** The layer an item draws in: its own `layer`, else its entry's; an unknown one draws in marks. */
export function layerOf(item, entry) {
  const layer = item?.layer || entry?.layer;
  return LAYER_NAMES.includes(layer) ? layer : "marks";
}

/** The nodes of one entry's items in one layer, in list order. */
export function entryNodes(entry, layer, ctx) {
  const out = [];
  for (const item of Array.isArray(entry?.items) ? entry.items : []) {
    if (layerOf(item, entry) !== layer) continue;
    const node = primitiveNode(item, ctx);
    if (node) out.push(node);
  }
  return out;
}

/** The layers an entry has items in. */
export function entryLayers(entry) {
  const set = new Set();
  for (const item of Array.isArray(entry?.items) ? entry.items : []) set.add(layerOf(item, entry));
  return set;
}

// -- defs ------------------------------------------------------------------------------------------

/**
 * The <defs> children for what a drawing used: elevation filters by level, then hatch patterns and
 * clip paths in order of first use. A shadow {x, y, blur, color, alpha} is a blurred, offset,
 * flooded copy of the shape's alpha; the shadows merge under the shape itself.
 */
export function defsNodes(defs) {
  const out = [];
  for (const level of [...defs.elevations].sort((a, b) => a - b)) {
    const shadows = defs.shadows[String(level)] || [];
    const prims = [];
    shadows.forEach((s, i) => {
      prims.push(
        [
          "feGaussianBlur",
          [
            ["in", "SourceAlpha"],
            ["stdDeviation", fmt((Number(s.blur) || 0) / 2)],
            ["result", `b${i}`],
          ],
          null,
        ],
        [
          "feOffset",
          [
            ["in", `b${i}`],
            ["dx", fmt(Number(s.x) || 0)],
            ["dy", fmt(Number(s.y) || 0)],
            ["result", `o${i}`],
          ],
          null,
        ],
        [
          "feFlood",
          [
            ["flood-color", String(s.color || "#000000").toLowerCase()],
            ["flood-opacity", fmt(Number(s.alpha) || 0)],
            ["result", `f${i}`],
          ],
          null,
        ],
        [
          "feComposite",
          [
            ["in", `f${i}`],
            ["in2", `o${i}`],
            ["operator", "in"],
            ["result", `s${i}`],
          ],
          null,
        ],
      );
    });
    const merge = shadows.map((_, i) => ["feMergeNode", [["in", `s${i}`]], null]);
    merge.push(["feMergeNode", [["in", "SourceGraphic"]], null]);
    prims.push(["feMerge", [], merge]);
    out.push([
      "filter",
      [
        ["id", `synapse-elev-${level}`],
        ["x", "-50%"],
        ["y", "-50%"],
        ["width", "200%"],
        ["height", "200%"],
        ["color-interpolation-filters", "sRGB"],
      ],
      prims,
    ]);
  }
  for (const [hex, id] of defs.hatches) {
    out.push([
      "pattern",
      [
        ["id", id],
        ["width", "12"],
        ["height", "12"],
        ["patternUnits", "userSpaceOnUse"],
        ["patternTransform", "rotate(45)"],
      ],
      [
        [
          "line",
          [
            ["x1", "0"],
            ["y1", "0"],
            ["x2", "0"],
            ["y2", "12"],
            ["stroke", hex],
            ["stroke-width", "3"],
          ],
          null,
        ],
      ],
    ]);
  }
  for (const [key, id] of defs.clips) {
    const [x, y, w, h] = key.split(" ");
    out.push([
      "clipPath",
      [["id", id]],
      [
        [
          "rect",
          [
            ["x", x],
            ["y", y],
            ["width", w],
            ["height", h],
          ],
          null,
        ],
      ],
    ]);
  }
  return out;
}

// -- serialising -----------------------------------------------------------------------------------

const BAD_XML = /[\u0000-\u0008\u000b\u000c\u000e-\u001f￾￿]/g;

/** XML escaping of both writers: control characters dropped, then & < > " escaped. */
export function escapeXML(value) {
  return String(value).replace(BAD_XML, "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;").replace(/"/g, "&quot;");
}

/** A node as SVG markup (no whitespace between elements). */
export function serialize(node) {
  const [tag, attrs, kids] = node;
  let out = `<${tag}`;
  for (const [name, value] of attrs) out += ` ${name}="${escapeXML(value)}"`;
  if (kids === null || kids === undefined) return `${out}/>`;
  if (typeof kids === "string") return `${out}>${escapeXML(kids)}</${tag}>`;
  out += ">";
  for (const kid of kids) out += serialize(kid);
  return `${out}</${tag}>`;
}
