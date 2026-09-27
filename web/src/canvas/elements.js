// Excalidraw element helpers shared by the adapter and the page kinds (./kinds/): building the
// common fields of an element from a canonical one, bound labels, derived overlays, colours by
// tone, and the style inputs the reverse diff compares.
import { FONT_FAMILY } from "@excalidraw/excalidraw";
import { INK, MUTED, TOKENS, defaultColors, toneColors } from "../theme/tokens.js";

export const MAX_PEN_POINTS = 500;
export const MAX_ARROW_POINTS = 50;
export const SIZES = [16, 20, 28, 36];
// Lines per font size, as herdr_team/canvas_text.py measures a text (LINE_HEIGHT x size per line).
export const LINE_HEIGHT = 1.25;
export const WIDTHS = [1, 2, 4];
export const HEAD_TO_EX = { arrow: "arrow", triangle: "triangle", dot: "dot", none: null };
// font: normal is Inter through Excalidraw's "Helvetica" slot (src/fonts.css), hand the legacy
// Excalifont, code Cascadia (Geist Mono arrives with the phase 1 renderer).
export const FONT_TO_EX = { normal: FONT_FAMILY.Helvetica, hand: FONT_FAMILY.Excalifont, code: FONT_FAMILY.Cascadia };
export const DEFAULT_FONT = "normal";
export const ROUND_ADAPTIVE = 3;
export const ROUND_PROPORTIONAL = 2;
// Excalidraw's own padding between a container and its bound text.
export const BOUND_TEXT_PADDING = 5;
const ROUNDNESS = TOKENS.phase0_excalidraw.roundness;

export const exIdOf = (el) => el.client_id || el.id;
export const textIdOf = (exId) => `${exId}~t`;
export const aliasOf = (exId) => `h${String(exId).replace(/[^A-Za-z0-9_-]/g, "").slice(0, 60)}`;

export function seedOf(text) {
  let hash = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return (Math.abs(hash) % 2147483646) + 1;
}

export const nearest = (list, value) => list.reduce((best, item) => (Math.abs(item - value) < Math.abs(best - value) ? item : best), list[0]);
export const round = (value) => Math.round(Number(value) || 0);

export function headOf(exHead) {
  if (!exHead) return "none";
  if (exHead.startsWith("triangle")) return "triangle";
  if (exHead === "dot" || exHead.startsWith("circle")) return "dot";
  return "arrow";
}

export function fontOf(exFamily) {
  if (exFamily === FONT_FAMILY["Comic Shanns"] || exFamily === FONT_FAMILY.Cascadia) return "code";
  if (exFamily === FONT_FAMILY.Nunito || exFamily === FONT_FAMILY.Helvetica || exFamily === FONT_FAMILY["Liberation Sans"] || exFamily === FONT_FAMILY["Lilita One"]) {
    return "normal";
  }
  return "hand";
}

export function colorOf(value) {
  if (typeof value !== "string") return null;
  const match = /^#([0-9a-fA-F]{6})([0-9a-fA-F]{2})?$/.exec(value.trim());
  if (match) return `#${match[1].toLowerCase()}`;
  const short = /^#([0-9a-fA-F])([0-9a-fA-F])([0-9a-fA-F])$/.exec(value.trim());
  if (short) return `#${short[1]}${short[1]}${short[2]}${short[2]}${short[3]}${short[3]}`.toLowerCase();
  return null;
}

export const fillOf = (value) => (!value || value === "transparent" ? "none" : colorOf(value) || "none");

// What the human typed: Excalidraw keeps it in originalText and puts its wrapped lines in
// text, so comparing text would turn every soft wrap (or a font re-measuring a wrapped label)
// into an edit with hard line breaks.
export const typedText = (ex) => String(ex.originalText ?? ex.text ?? "");

// -- colours and type ---------------------------------------------------------------------

// An element's colours: what the server stored (style.stroke, style.fill, style.text), else its
// tone, else its kind's default tone. Authorship never colours an element (the author chip does).
// A missing fill means none, except on a note (sticky paper); a label takes style.text, else the
// stroke, as the Python renderer does.
export function colorsOf(el) {
  const style = el.style || {};
  const fallback = style.tone ? toneColors(style.tone, style.variant || "soft", el.type) : defaultColors(el.type);
  const stroke = style.stroke || fallback.stroke;
  const fill = style.fill ?? (el.type === "note" ? fallback.fill : null);
  const text = style.text || style.stroke || fallback.text || INK;
  return { stroke, fill, text };
}

// The size a label is drawn at: the fitted size when the server shrank it (fit.size), else the style's.
export const fontSizeOf = (el) => el.fit?.size || el.style?.size || 20;
export const fontFamilyOf = (el) => FONT_TO_EX[el.style?.font || DEFAULT_FONT] || FONT_TO_EX[DEFAULT_FONT];

// The lines the server broke a label into (fit.lines), when they are this element's current text:
// the same words in the same order (a clamped label may end early with an ellipsis). The page draws
// them as they are, so a human sees the breaks an agent reads in look and in the PNG.
export function serverLines(el) {
  const lines = el.fit?.lines;
  if (!Array.isArray(lines) || !lines.length || !lines.every((line) => typeof line === "string")) return null;
  const squash = (text) => String(text).replace(/\s+/g, "");
  const drawn = squash(lines.join(""));
  const typed = squash(el.text || "");
  if (el.fit.truncated ? !typed.startsWith(drawn.replace(/…$/, "")) : drawn !== typed) return null;
  return lines;
}

export function roundnessOf(kind) {
  const value = ROUNDNESS[kind];
  return value ? { ...value } : null;
}

// Whether the model wraps a free text at its width: set by its author (wrap), or a line ran
// past the widest a text grows, which leaves it taller than its hard lines.
export function wrapsAtWidth(el) {
  if (el.wrap) return true;
  const size = fontSizeOf(el);
  const lines = el.fit?.lines?.length || String(el.text || "").split("\n").length;
  return (el.h || 0) > Math.ceil(lines * LINE_HEIGHT * size) + 1;
}

// -- canonical -> Excalidraw ------------------------------------------------------------

export function base(el, exId, frameEx) {
  const style = el.style || {};
  const colors = colorsOf(el);
  return {
    id: exId,
    x: el.x,
    y: el.y,
    width: Math.max(1, el.w || 1),
    height: Math.max(1, el.h || 1),
    angle: 0,
    strokeColor: colors.stroke,
    backgroundColor: colors.fill || "transparent",
    fillStyle: "solid",
    strokeWidth: style.width || 2,
    strokeStyle: style.dash || "solid",
    roughness: style.rough ?? 0,
    opacity: style.opacity ?? 100,
    seed: seedOf(el.id),
    version: (el.updated_seq || 1) + 1,
    versionNonce: seedOf(`${el.id}:${el.updated_seq || 0}`),
    groupIds: [],
    frameId: frameEx,
    roundness: null,
    boundElements: [],
    updated: Date.parse(el.updated_at || "") || 1,
    link: null,
    locked: false,
    customData: { synapse: { id: el.id, author: el.author, intent: el.intent ?? null, kind: el.type, derived: null, seq: el.updated_seq ?? 0 } },
  };
}

export function boundText(el, container, align = "center") {
  const style = el.style || {};
  const text = String(el.text || "");
  return {
    id: textIdOf(container.id),
    type: "text",
    x: container.x,
    y: container.y,
    width: container.width,
    height: 20,
    angle: 0,
    strokeColor: colorsOf(el).text,
    backgroundColor: "transparent",
    fillStyle: "solid",
    strokeWidth: 1,
    strokeStyle: "solid",
    roughness: 0,
    opacity: style.opacity ?? 100,
    seed: seedOf(`${el.id}~t`),
    version: container.version,
    versionNonce: seedOf(`${el.id}~t:${el.updated_seq || 0}`),
    groupIds: [],
    frameId: container.frameId,
    roundness: null,
    boundElements: null,
    updated: container.updated,
    link: null,
    locked: container.locked,
    text,
    originalText: text,
    fontSize: fontSizeOf(el),
    fontFamily: fontFamilyOf(el),
    textAlign: align,
    verticalAlign: "middle",
    containerId: container.id,
    autoResize: true,
    customData: { synapse: { id: el.id, author: el.author, intent: el.intent ?? null, kind: el.type, derived: "text", seq: el.updated_seq ?? 0 } },
  };
}

export function absPoints(points) {
  return (points || []).filter((p) => Array.isArray(p) && p.length >= 2).map((p) => [Number(p[0]) || 0, Number(p[1]) || 0, p[2]]);
}

// A locked element rebuilt from the scene and never turned into an operation (claims, locks,
// comment pins, author chips, frame zones).
export function derived(id, kind, fields, extra = {}) {
  return {
    id,
    angle: 0,
    fillStyle: "solid",
    strokeWidth: 1,
    strokeStyle: "solid",
    roughness: 0,
    opacity: 100,
    seed: seedOf(id),
    version: 2,
    versionNonce: seedOf(`${id}:${JSON.stringify(fields).length}`),
    groupIds: [],
    frameId: null,
    roundness: null,
    boundElements: null,
    updated: 1,
    link: null,
    locked: true,
    ...fields,
    customData: { synapse: { derived: kind, ...extra } },
  };
}

export function label(id, x, y, text, color, kind, extra, size = 14) {
  return derived(id, kind, { type: "text", x, y, width: 10, height: size * LINE_HEIGHT, strokeColor: color || MUTED, backgroundColor: "transparent", text,
    originalText: text, fontSize: size, fontFamily: FONT_TO_EX.normal, textAlign: "left", verticalAlign: "top", containerId: null, autoResize: true }, extra);
}

// -- Excalidraw -> operation inputs ---------------------------------------------------------

export function styleInputs(ex, snapshot) {
  const out = {};
  const stroke = colorOf(ex.strokeColor);
  if (stroke && (!snapshot || colorOf(snapshot.strokeColor) !== stroke)) out.color = stroke;
  if (!snapshot || fillOf(snapshot.backgroundColor) !== fillOf(ex.backgroundColor)) out.fill = fillOf(ex.backgroundColor);
  const width = nearest(WIDTHS, ex.strokeWidth || 2);
  if (!snapshot || nearest(WIDTHS, snapshot.strokeWidth || 2) !== width) out.width = width;
  const dash = ex.strokeStyle === "dashed" || ex.strokeStyle === "dotted" ? ex.strokeStyle : "solid";
  if (!snapshot || (snapshot.strokeStyle || "solid") !== dash) out.dash = dash;
  const opacity = Math.max(10, Math.min(100, round(ex.opacity ?? 100)));
  if (!snapshot || round(snapshot.opacity ?? 100) !== opacity) out.opacity = opacity;
  const rough = Math.max(0, Math.min(2, round(ex.roughness ?? 0)));
  if (!snapshot || round(snapshot.roughness ?? 0) !== rough) out.rough = rough;
  return out;
}

export function textStyleInputs(textEl, snapshot) {
  const out = {};
  if (!textEl) return out;
  const font = fontOf(textEl.fontFamily);
  if (!snapshot || fontOf(snapshot.fontFamily) !== font) out.font = font;
  const size = nearest(SIZES, textEl.fontSize || 20);
  if (!snapshot || nearest(SIZES, snapshot.fontSize || 20) !== size) out.size = size;
  return out;
}

export function sample(points, max) {
  if (points.length <= max) return points;
  const step = (points.length - 1) / (max - 1);
  const out = [];
  for (let i = 0; i < max; i += 1) out.push(points[Math.round(i * step)]);
  return out;
}

export const absFrom = (ex) => (ex.points || []).map(([px, py]) => [round(ex.x + px), round(ex.y + py)]);

// The bound label of a container the human drew or changed.
export const boundLabelOf = (ex, byId) => (ex.boundElements || []).map((b) => byId.get(b.id)).find((t) => t && t.type === "text" && !t.isDeleted);

// A box-like element's move: its new top-left, and w/h when the human resized it (the server
// keeps them as the element's minimum size). Returns the fields, or null when it did not move.
export function boxMove(ex, snapshot, { resizable = true } = {}) {
  const move = {};
  if (Math.abs(ex.x - snapshot.x) >= 1 || Math.abs(ex.y - snapshot.y) >= 1) move.to = [round(ex.x), round(ex.y)];
  if (resizable && (Math.abs(ex.width - snapshot.width) >= 1 || Math.abs(ex.height - snapshot.height) >= 1)) {
    move.to = [round(ex.x), round(ex.y)];
    move.w = Math.max(1, round(ex.width));
    move.h = Math.max(1, round(ex.height));
  }
  return Object.keys(move).length ? move : null;
}
