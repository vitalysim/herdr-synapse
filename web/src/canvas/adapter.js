// The adapter between Synapse's canonical scene (contract 5) and Excalidraw elements.
//
// build():  canonical scene -> Excalidraw elements. Every element carries
//           customData.synapse = {id, author, intent, kind, derived}. Claims, locks,
//           comment pins and provenance tints are derived: locked, rebuilt from the scene,
//           and never turned into operations.
// diff():   what the human changed in Excalidraw -> Synapse Sketch operations, each with
//           if_version, so the server stays the only writer of the canonical scene.
import { FONT_FAMILY, restoreElements } from "@excalidraw/excalidraw";
import { activeClaims } from "../sceneStore.js";
import { fileIdFor } from "./renderers.js";

export const NOTE_FILL = "#ffec99";
export const HUMAN_COLOR = "#1e1e1e";
export const MAX_PEN_POINTS = 500;
export const MAX_ARROW_POINTS = 50;
const SIZES = [16, 20, 28, 36];
// Lines per font size, as herdr_team/canvas.py measures a text (1.25 x size per line).
const LINE_HEIGHT = 1.25;
const WIDTHS = [1, 2, 4];
const HEAD_TO_EX = { arrow: "arrow", triangle: "triangle", dot: "dot", none: null };
const FONT_TO_EX = { hand: FONT_FAMILY.Excalifont, normal: FONT_FAMILY.Nunito, code: FONT_FAMILY["Comic Shanns"] };
const ROUND_ADAPTIVE = 3;
const ROUND_PROPORTIONAL = 2;

export const exIdOf = (el) => el.client_id || el.id;
const textIdOf = (exId) => `${exId}~t`;

export function seedOf(text) {
  let hash = 2166136261;
  for (let i = 0; i < text.length; i += 1) {
    hash ^= text.charCodeAt(i);
    hash = Math.imul(hash, 16777619);
  }
  return (Math.abs(hash) % 2147483646) + 1;
}

const nearest = (list, value) => list.reduce((best, item) => (Math.abs(item - value) < Math.abs(best - value) ? item : best), list[0]);

function headOf(exHead) {
  if (!exHead) return "none";
  if (exHead.startsWith("triangle")) return "triangle";
  if (exHead === "dot" || exHead.startsWith("circle")) return "dot";
  return "arrow";
}

function fontOf(exFamily) {
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

const fillOf = (value) => (!value || value === "transparent" ? "none" : colorOf(value) || "none");
const round = (value) => Math.round(Number(value) || 0);

function authorColor(scene, name) {
  if (name === "human") return HUMAN_COLOR;
  return scene.authors?.[name]?.color || "#868e96";
}

// What the human typed: Excalidraw keeps it in originalText and puts its wrapped lines in
// text, so comparing text would turn every soft wrap (or a font re-measuring a wrapped label)
// into an edit with hard line breaks.
const typedText = (ex) => String(ex.originalText ?? ex.text ?? "");

// Whether the model wraps a free text at its width: set by its author (wrap), or a line ran
// past the widest a text grows, which leaves it taller than its hard lines.
function wrapsAtWidth(el) {
  if (el.wrap) return true;
  const size = el.style?.size || 20;
  const hardLines = String(el.text || "").split("\n").length;
  return (el.h || 0) > Math.ceil(hardLines * 1.25 * size) + 1;
}

export function boundsOf(el) {
  return [el.x, el.y, el.x + Math.max(1, el.w || 1), el.y + Math.max(1, el.h || 1)];
}

// -- canonical -> Excalidraw -----------------------------------------------------------

function base(el, exId, scene, frameEx) {
  const style = el.style || {};
  return {
    id: exId,
    x: el.x,
    y: el.y,
    width: Math.max(1, el.w || 1),
    height: Math.max(1, el.h || 1),
    angle: 0,
    strokeColor: style.stroke || authorColor(scene, el.author),
    backgroundColor: style.fill || "transparent",
    fillStyle: "solid",
    strokeWidth: style.width || 2,
    strokeStyle: style.dash || "solid",
    roughness: style.rough ?? 1,
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

function boundText(el, container, scene, align = "center") {
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
    strokeColor: style.stroke || authorColor(scene, el.author),
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
    fontSize: style.size || 20,
    fontFamily: FONT_TO_EX[style.font] || FONT_FAMILY.Excalifont,
    textAlign: align,
    verticalAlign: "middle",
    containerId: container.id,
    autoResize: true,
    customData: { synapse: { id: el.id, author: el.author, intent: el.intent ?? null, kind: el.type, derived: "text", seq: el.updated_seq ?? 0 } },
  };
}

function absPoints(points) {
  return (points || []).filter((p) => Array.isArray(p) && p.length >= 2).map((p) => [Number(p[0]) || 0, Number(p[1]) || 0, p[2]]);
}

function buildCanonical(el, scene, ctx) {
  const exId = exIdOf(el);
  const frameEx = el.frame ? ctx.canonToEx.get(el.frame) || null : null;
  const b = base(el, exId, scene, frameEx);
  const style = el.style || {};
  switch (el.type) {
    case "box":
    case "note":
    case "ellipse":
    case "diamond": {
      const type = el.type === "ellipse" ? "ellipse" : el.type === "diamond" ? "diamond" : "rectangle";
      const container = {
        ...b,
        type,
        roundness: type === "rectangle" ? { type: ROUND_ADAPTIVE } : type === "diamond" ? { type: ROUND_PROPORTIONAL } : null,
        backgroundColor: style.fill || (el.type === "note" ? NOTE_FILL : "transparent"),
      };
      if (!el.text) return [container];
      const text = boundText(el, container, scene);
      container.boundElements = [{ type: "text", id: text.id }];
      return [container, text];
    }
    case "text":
      return [
        {
          ...b,
          type: "text",
          text: String(el.text || ""),
          originalText: String(el.text || ""),
          fontSize: style.size || 20,
          fontFamily: FONT_TO_EX[style.font] || FONT_FAMILY.Excalifont,
          textAlign: "left",
          verticalAlign: "top",
          containerId: null,
          // The model wraps a text at its w when it was given one (wrap) or when a line runs past
          // the widest a text grows; Excalidraw then wraps it there too instead of one long line.
          autoResize: !wrapsAtWidth(el),
          backgroundColor: "transparent",
        },
      ];
    case "frame":
      return [{ ...b, type: "frame", name: el.text || null, strokeColor: "#bbb", backgroundColor: "transparent", roughness: 0 }];
    case "arrow": {
      const pts = absPoints(el.points);
      if (pts.length < 2) return [];
      const [x0, y0] = pts[0];
      const arrow = {
        ...b,
        type: "arrow",
        x: x0,
        y: y0,
        points: pts.map(([x, y]) => [x - x0, y - y0]),
        startArrowhead: el.tail in HEAD_TO_EX ? HEAD_TO_EX[el.tail] : null,
        endArrowhead: el.head in HEAD_TO_EX ? HEAD_TO_EX[el.head] : "arrow",
        roundness: el.curve ? { type: ROUND_PROPORTIONAL } : null,
        startBinding: null,
        endBinding: null,
        lastCommittedPoint: null,
        elbowed: false,
        backgroundColor: "transparent",
      };
      ctx.arrows.push({ arrow, from: el.from, to: el.to });
      if (!el.text) return [arrow];
      const label = boundText(el, arrow, scene);
      label.fontSize = Math.min(label.fontSize, 20);
      arrow.boundElements = [{ type: "text", id: label.id }];
      return [arrow, label];
    }
    case "pen": {
      const pts = absPoints(ctx.animated.get(el.id) || el.points);
      if (pts.length < 2) return [];
      const [x0, y0] = pts[0];
      const xs = pts.map((p) => p[0]);
      const ys = pts.map((p) => p[1]);
      const size = { width: Math.max(...xs) - Math.min(...xs), height: Math.max(...ys) - Math.min(...ys) };
      const fill = el.closed && style.fill ? style.fill : "transparent";
      if (el.smooth === false) {
        const rel = pts.map(([x, y]) => [x - x0, y - y0]);
        if (el.closed && (rel[0][0] !== rel[rel.length - 1][0] || rel[0][1] !== rel[rel.length - 1][1])) rel.push([0, 0]);
        return [{ ...b, ...size, type: "line", x: x0, y: y0, points: rel, backgroundColor: fill, startArrowhead: null, endArrowhead: null,
          startBinding: null, endBinding: null, lastCommittedPoint: null, roundness: null }];
      }
      const pressures = pts.every((p) => typeof p[2] === "number") ? pts.map((p) => Math.max(0, Math.min(1, p[2]))) : [];
      return [{ ...b, ...size, type: "freedraw", x: x0, y: y0, points: pts.map(([x, y]) => [x - x0, y - y0]), pressures,
        simulatePressure: pressures.length === 0, lastCommittedPoint: null, backgroundColor: fill }];
    }
    case "viz":
      if (ctx.vizOn) {
        return [{ ...b, type: "embeddable", link: `/viz/${encodeURIComponent(ctx.team)}/${el.id}?v=${el.updated_seq || 0}`,
          backgroundColor: "transparent", strokeWidth: 1, roughness: 0, roundness: { type: ROUND_ADAPTIVE } }];
      }
    // falls through: a switched-off live visual shows its placeholder
    case "path":
    case "svg":
    case "mermaid":
    case "chart":
    case "image": {
      const fileId = fileIdFor(el, ctx.vizOn);
      if (!fileId) return [];
      ctx.fileJobs.push({ fileId, element: el });
      return [{ ...b, type: "image", fileId, status: "saved", scale: [1, 1], crop: null, backgroundColor: "transparent", strokeColor: "transparent",
        roughness: 0 }];
    }
    default:
      return [];
  }
}

function derived(id, kind, fields, extra = {}) {
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

function label(id, x, y, text, color, kind, extra, size = 14) {
  return derived(id, kind, { type: "text", x, y, width: 10, height: size * 1.25, strokeColor: color, backgroundColor: "transparent", text,
    originalText: text, fontSize: size, fontFamily: FONT_FAMILY.Nunito, textAlign: "left", verticalAlign: "top", containerId: null, autoResize: true }, extra);
}

function regionBox(region) {
  const [x0, y0, x1, y1] = region;
  return { x: Math.min(x0, x1), y: Math.min(y0, y1), width: Math.max(1, Math.abs(x1 - x0)), height: Math.max(1, Math.abs(y1 - y0)) };
}

// The whole Excalidraw scene for one canonical scene. `options.animated` maps pen ids to
// the points drawn so far while a stroke streams in.
export function build(scene, options) {
  const { team, hiddenAuthors = new Set(), tint = false, vizOn = true, animated = new Map(), now = Date.now() } = options;
  const ctx = { team, vizOn, animated, canonToEx: new Map(), arrows: [], fileJobs: [] };
  const visible = (scene.elements || []).filter((el) => !hiddenAuthors.has(el.author));
  for (const el of scene.elements || []) ctx.canonToEx.set(el.id, exIdOf(el));
  const tints = [];
  const body = [];
  const pins = [];
  let pinNumber = 0;
  for (const el of visible) {
    if (el.type === "comment") {
      pinNumber += 1;
      const color = authorColor(scene, el.author);
      const [px, py] = Array.isArray(el.point) ? el.point : [el.x, el.y];
      const number = String(el.id || "").replace(/^C-/, "");
      const pin = derived(`pin~${el.id}`, "comment", { type: "ellipse", x: px - 13, y: py - 13, width: 26, height: 26, strokeColor: color,
        backgroundColor: el.resolved ? "#e9ecef" : "#fff3bf", opacity: el.resolved ? 45 : 100, strokeWidth: 2 }, { id: el.id, author: el.author, intent: el.intent ?? null });
      const text = derived(`pin~${el.id}~t`, "comment", { type: "text", x: px - 13, y: py - 13, width: 26, height: 18, strokeColor: color,
        backgroundColor: "transparent", text: number, originalText: number, fontSize: 12, fontFamily: FONT_FAMILY.Nunito, textAlign: "center",
        verticalAlign: "middle", containerId: pin.id, autoResize: true, opacity: pin.opacity }, { id: el.id, author: el.author });
      pin.boundElements = [{ type: "text", id: text.id }];
      pins.push(pin, text);
      continue;
    }
    const group = buildCanonical(el, scene, ctx);
    body.push(...group);
    if (tint && group.length) {
      const pad = 6;
      tints.push(derived(`tint~${el.id}`, "tint", { type: "rectangle", x: el.x - pad, y: el.y - pad, width: Math.max(1, el.w) + pad * 2,
        height: Math.max(1, el.h) + pad * 2, strokeColor: "transparent", backgroundColor: authorColor(scene, el.author), opacity: 18 }, { id: el.id, author: el.author }));
    }
  }
  const overlays = [];
  for (const claim of activeClaims(scene, now)) {
    if (hiddenAuthors.has(claim.author) || !Array.isArray(claim.region)) continue;
    const color = authorColor(scene, claim.author);
    const box = regionBox(claim.region);
    overlays.push(derived(`claim~${claim.id}`, "claim", { type: "rectangle", ...box, strokeColor: color, backgroundColor: "transparent", strokeStyle: "dashed",
      strokeWidth: 2, opacity: 80 }, { id: claim.id, author: claim.author, intent: claim.intent ?? null }));
    overlays.push(label(`claim~${claim.id}~t`, box.x, box.y - 22, `${claim.author}: ${claim.label || "working here"}`, color, "claim", { id: claim.id, author: claim.author }));
  }
  for (const lock of scene.locks || []) {
    if (!Array.isArray(lock.region)) continue;
    const box = regionBox(lock.region);
    overlays.push(derived(`lock~${lock.id}`, "lock", { type: "rectangle", ...box, strokeColor: "#868e96", backgroundColor: "#ced4da", fillStyle: "cross-hatch",
      strokeWidth: 1, opacity: 45 }, { id: lock.id, author: "human" }));
    overlays.push(label(`lock~${lock.id}~t`, box.x, box.y - 22, `locked by the operator${lock.label ? `: ${lock.label}` : ""}`, "#495057", "lock", { id: lock.id }));
  }
  // Bind arrow ends to their elements, as Excalidraw expects on both sides.
  const byId = new Map(body.map((item) => [item.id, item]));
  for (const { arrow, from, to } of ctx.arrows) {
    for (const [end, ref] of [["startBinding", from], ["endBinding", to]]) {
      const target = ref ? byId.get(ctx.canonToEx.get(ref)) : null;
      if (!target || target.type === "text" || target.type === "arrow") continue;
      arrow[end] = { elementId: target.id, focus: 0, gap: 4 };
      target.boundElements = [...(target.boundElements || []), { type: "arrow", id: arrow.id }];
    }
  }
  // Every text gets the model's line height: left out, Excalidraw guesses it from height over
  // lines, and a text built at its wrapped height with its unwrapped text got 2.5.
  const raw = [...tints, ...body, ...pins, ...overlays].map((el) => (el.type === "text" ? { ...el, lineHeight: LINE_HEIGHT } : el));
  const elements = centerBoundText(restoreElements(raw, null, { refreshDimensions: true, repairBindings: true }));
  return { elements, fileJobs: ctx.fileJobs };
}

// restoreElements measures bound text but leaves it where it was put; place it the way
// Excalidraw does when the human edits: centred in the container's inner box, or on the
// middle of an arrow.
const BOUND_TEXT_PADDING = 5;

function arrowMidpoint(arrow) {
  const points = arrow.points || [];
  if (points.length < 2) return [0, 0];
  const lengths = [];
  let total = 0;
  for (let i = 1; i < points.length; i += 1) {
    const length = Math.hypot(points[i][0] - points[i - 1][0], points[i][1] - points[i - 1][1]);
    lengths.push(length);
    total += length;
  }
  let walked = 0;
  for (let i = 1; i < points.length; i += 1) {
    if (walked + lengths[i - 1] >= total / 2) {
      const t = lengths[i - 1] ? (total / 2 - walked) / lengths[i - 1] : 0;
      return [points[i - 1][0] + (points[i][0] - points[i - 1][0]) * t, points[i - 1][1] + (points[i][1] - points[i - 1][1]) * t];
    }
    walked += lengths[i - 1];
  }
  return points[points.length - 1];
}

function centerBoundText(elements) {
  const byId = new Map(elements.map((el) => [el.id, el]));
  return elements.map((el) => {
    if (el.type !== "text" || !el.containerId) return el;
    const container = byId.get(el.containerId);
    if (!container) return el;
    if (container.type === "arrow") {
      const [mx, my] = arrowMidpoint(container);
      return { ...el, x: container.x + mx - el.width / 2, y: container.y + my - el.height / 2 };
    }
    const shrink = container.type === "ellipse" ? Math.SQRT1_2 : container.type === "diamond" ? 0.5 : 1;
    const w = container.width * shrink - BOUND_TEXT_PADDING * 2;
    const h = container.height * shrink - BOUND_TEXT_PADDING * 2;
    const x = container.x + (container.width - w) / 2;
    const y = container.y + (container.height - h) / 2;
    return { ...el, x: x + (w - el.width) / 2, y: y + (h - el.height) / 2 };
  });
}

// -- hit testing and bounds -----------------------------------------------------------------

export function elementAt(scene, x, y, hiddenAuthors = new Set()) {
  const elements = scene.elements || [];
  for (let i = elements.length - 1; i >= 0; i -= 1) {
    const el = elements[i];
    if (hiddenAuthors.has(el.author) || el.type === "frame") continue;
    const margin = el.type === "comment" ? 14 : 4;
    if (x >= el.x - margin && x <= el.x + (el.w || 1) + margin && y >= el.y - margin && y <= el.y + (el.h || 1) + margin) return el;
  }
  for (let i = elements.length - 1; i >= 0; i -= 1) {
    const el = elements[i];
    if (el.type === "frame" && !hiddenAuthors.has(el.author) && x >= el.x && x <= el.x + el.w && y >= el.y && y <= el.y + el.h) return el;
  }
  return null;
}

export function unionBounds(elements) {
  let box = null;
  for (const el of elements) {
    const [x0, y0, x1, y1] = boundsOf(el);
    box = box ? [Math.min(box[0], x0), Math.min(box[1], y0), Math.max(box[2], x1), Math.max(box[3], y1)] : [x0, y0, x1, y1];
  }
  return box;
}

// -- Excalidraw -> operations ---------------------------------------------------------------

export const aliasOf = (exId) => `h${String(exId).replace(/[^A-Za-z0-9_-]/g, "").slice(0, 60)}`;

function styleInputs(ex, snapshot) {
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
  const rough = Math.max(0, Math.min(2, round(ex.roughness ?? 1)));
  if (!snapshot || round(snapshot.roughness ?? 1) !== rough) out.rough = rough;
  return out;
}

function textStyleInputs(textEl, snapshot) {
  const out = {};
  if (!textEl) return out;
  const font = fontOf(textEl.fontFamily);
  if (!snapshot || fontOf(snapshot.fontFamily) !== font) out.font = font;
  const size = nearest(SIZES, textEl.fontSize || 20);
  if (!snapshot || nearest(SIZES, snapshot.fontSize || 20) !== size) out.size = size;
  return out;
}

function sample(points, max) {
  if (points.length <= max) return points;
  const step = (points.length - 1) / (max - 1);
  const out = [];
  for (let i = 0; i < max; i += 1) out.push(points[Math.round(i * step)]);
  return out;
}

const absFrom = (ex) => (ex.points || []).map(([px, py]) => [round(ex.x + px), round(ex.y + py)]);

// Operations for elements the human created: returns {op, deps} or null (unsupported).
function createOp(ex, byId, refOf, uploads) {
  const common = { id: aliasOf(ex.id), client_id: ex.id };
  const boundLabel = (ex.boundElements || []).map((b) => byId.get(b.id)).find((t) => t && t.type === "text" && !t.isDeleted);
  switch (ex.type) {
    case "rectangle":
    case "ellipse":
    case "diamond": {
      const kind = ex.type === "rectangle" ? (colorOf(ex.backgroundColor) === NOTE_FILL ? "note" : "box") : ex.type;
      return {
        op: "shape", kind, text: boundLabel ? typedText(boundLabel) : "", at: [round(ex.x), round(ex.y)], w: Math.max(1, round(ex.width)),
        h: Math.max(1, round(ex.height)), ...styleInputs(ex, null), ...textStyleInputs(boundLabel, null), ...common,
      };
    }
    case "text":
      if (!typedText(ex).trim()) return null;
      return { op: "shape", kind: "text", text: typedText(ex), at: [round(ex.x), round(ex.y)], color: colorOf(ex.strokeColor) || undefined,
        opacity: Math.max(10, Math.min(100, round(ex.opacity ?? 100))), ...(ex.autoResize === false ? { w: Math.max(1, round(ex.width)) } : {}),
        ...textStyleInputs(ex, null), ...common };
    case "arrow": {
      const pts = absFrom(ex);
      if (pts.length < 2) return null;
      const from = ex.startBinding ? refOf(ex.startBinding.elementId) : null;
      const to = ex.endBinding ? refOf(ex.endBinding.elementId) : null;
      const op = { op: "arrow", head: headOf(ex.endArrowhead), tail: headOf(ex.startArrowhead), curve: Boolean(ex.roundness), ...styleInputs(ex, null), ...common };
      delete op.fill;
      delete op.rough;
      if (boundLabel && typedText(boundLabel)) op.label = typedText(boundLabel);
      if (from || to) {
        op.from = from || `${pts[0][0]},${pts[0][1]}`;
        op.to = to || `${pts[pts.length - 1][0]},${pts[pts.length - 1][1]}`;
      } else {
        op.points = sample(pts, MAX_ARROW_POINTS);
      }
      return op;
    }
    case "line": {
      const pts = absFrom(ex);
      if (pts.length < 2) return null;
      const first = pts[0];
      const last = pts[pts.length - 1];
      const closed = pts.length > 2 && first[0] === last[0] && first[1] === last[1];
      const op = { op: "pen", points: sample(pts, MAX_PEN_POINTS), closed, style: "straight", ...styleInputs(ex, null), id: common.id };
      if (!closed) delete op.fill;
      delete op.dash;
      delete op.rough;
      return op;
    }
    case "freedraw": {
      const hasPressure = !ex.simulatePressure && Array.isArray(ex.pressures) && ex.pressures.length === (ex.points || []).length;
      const pts = (ex.points || []).map(([px, py], i) => {
        const point = [round(ex.x + px), round(ex.y + py)];
        if (hasPressure) point.push(Math.round(ex.pressures[i] * 100) / 100);
        return point;
      });
      if (pts.length < 2) return null;
      const op = { op: "pen", points: sample(pts, MAX_PEN_POINTS), closed: false, style: "smooth", ...styleInputs(ex, null), id: common.id };
      delete op.fill;
      delete op.dash;
      delete op.rough;
      return op;
    }
    case "frame":
      return { op: "frame", title: ex.name || "Frame", region: [round(ex.x), round(ex.y), round(ex.x + ex.width), round(ex.y + ex.height)], id: common.id };
    case "image": {
      const asset = uploads.get(ex.fileId);
      if (!asset) return null;
      return { op: "image", asset, at: [round(ex.x), round(ex.y)], w: Math.max(1, round(ex.width)), h: Math.max(1, round(ex.height)), ...common };
    }
    default:
      return null;
  }
}

// Ops creators refer to: canonical ids, or the alias of an element created in this batch.
export function diff({ elements, scene, snapshots, touched, clientToCanon, sentCreates, uploads }) {
  const byId = new Map(elements.map((el) => [el.id, el]));
  const canonById = new Map((scene.elements || []).map((el) => [el.id, el]));
  const exToCanon = new Map();
  for (const el of scene.elements || []) exToCanon.set(exIdOf(el), el.id);
  const creating = new Set();
  const refOf = (exId) => {
    if (!exId) return null;
    const ex = byId.get(exId);
    const synapseId = ex?.customData?.synapse?.id;
    if (synapseId && !ex.customData.synapse.derived) return synapseId;
    if (exToCanon.has(exId)) return exToCanon.get(exId);
    if (clientToCanon.has(exId)) return clientToCanon.get(exId);
    if (creating.has(exId)) return aliasOf(exId);
    return null;
  };

  const creates = [];
  const updates = [];
  const unsupported = [];

  // Creations first, ordered so every reference points backwards in the batch.
  const order = { frame: 0, rectangle: 1, ellipse: 1, diamond: 1, text: 1, image: 1, line: 2, freedraw: 2, arrow: 3 };
  const fresh = elements
    .filter((ex) => !ex.isDeleted && !ex.customData?.synapse && !sentCreates.has(ex.id) && !exToCanon.has(ex.id) && !clientToCanon.has(ex.id))
    .filter((ex) => !(ex.type === "text" && ex.containerId))
    .sort((a, b) => (order[a.type] ?? 9) - (order[b.type] ?? 9));
  for (const ex of fresh) {
    if (order[ex.type] === undefined) {
      unsupported.push(ex.id);
      continue;
    }
    if (ex.type === "image" && !uploads.has(ex.fileId)) continue;
    creating.add(ex.id);
  }
  for (const ex of fresh) {
    if (!creating.has(ex.id)) continue;
    const op = createOp(ex, byId, refOf, uploads);
    if (op) creates.push({ op, exIds: [ex.id, ...(ex.boundElements || []).map((b) => b.id)], create: ex.id });
  }

  // Changes to canonical elements the human touched.
  const versioned = new Set();
  const withVersion = (canon, op) => {
    if (!versioned.has(canon.id)) {
      versioned.add(canon.id);
      op.if_version = canon.updated_seq ?? 0;
    }
    return op;
  };
  for (const ex of elements) {
    const synapse = ex.customData?.synapse;
    if (!synapse) continue;
    const snapshot = snapshots.get(ex.id);
    if (!snapshot || snapshot.version === ex.version) continue;
    // Claims, locks, pins and tints are locked; only Excalidraw itself (re-measuring text
    // when a font loads) changes them, and the next build replaces them anyway.
    if (synapse.derived && synapse.derived !== "text") continue;
    const canon = canonById.get(synapse.id);
    if (!canon) continue;
    const exIds = [ex.id];
    if (synapse.derived === "text") {
      // A container's label: only its text is the human's to change.
      const container = byId.get(ex.containerId);
      const text = ex.isDeleted || !container || container.isDeleted ? "" : typedText(ex);
      if (container && container.isDeleted) continue;
      if (text !== String(canon.text || "")) updates.push({ op: withVersion(canon, { op: "edit", id: canon.id, text }), exIds: [ex.id, ex.containerId] });
      const styled = textStyleInputs(ex, snapshot);
      if (Object.keys(styled).length) updates.push({ op: withVersion(canon, { op: "restyle", id: canon.id, ...styled }), exIds: [ex.id] });
      continue;
    }
    if (ex.isDeleted) {
      updates.push({ op: withVersion(canon, { op: "delete", id: canon.id }), exIds, deleted: true });
      continue;
    }
    if (!touched.has(ex.id)) continue;
    const move = { op: "move", id: canon.id };
    let moved = false;
    if (ex.type === "arrow" || ex.type === "line" || ex.type === "freedraw") {
      const pts = absFrom(ex);
      const before = snapshot.points || [];
      if (JSON.stringify(pts) !== JSON.stringify(before)) {
        // An arrow bound at both ends follows its elements on the server; only a rebinding moves it.
        const bothBound = canon.type === "arrow" && canon.from && canon.to && ex.startBinding && ex.endBinding;
        if (!bothBound) {
          move.points = sample(pts, ex.type === "arrow" ? MAX_ARROW_POINTS : MAX_PEN_POINTS);
          moved = true;
        }
      }
      if (ex.type === "arrow") {
        const from = ex.startBinding ? refOf(ex.startBinding.elementId) : null;
        const to = ex.endBinding ? refOf(ex.endBinding.elementId) : null;
        if ((from || null) !== (canon.from || null)) {
          move.from = from;
          moved = true;
        }
        if ((to || null) !== (canon.to || null)) {
          move.to_element = to;
          moved = true;
        }
        if (moved && !move.points) move.points = sample(pts, MAX_ARROW_POINTS);
      }
    } else {
      if (Math.abs(ex.x - snapshot.x) >= 1 || Math.abs(ex.y - snapshot.y) >= 1) {
        move.to = [round(ex.x), round(ex.y)];
        moved = true;
      }
      if (ex.type !== "text" && (Math.abs(ex.width - snapshot.width) >= 1 || Math.abs(ex.height - snapshot.height) >= 1)) {
        move.to = [round(ex.x), round(ex.y)];
        move.w = Math.max(1, round(ex.width));
        move.h = Math.max(1, round(ex.height));
        moved = true;
      } else if (ex.type === "text" && ex.autoResize === false && Math.abs(ex.width - snapshot.width) >= 1) {
        // A text the human resized wraps at its new width; its height follows on the server.
        move.w = Math.max(1, round(ex.width));
        moved = true;
      }
    }
    if ((ex.frameId || null) !== (snapshot.frameId || null)) {
      move.frame = ex.frameId ? refOf(ex.frameId) : null;
      moved = true;
    }
    if (moved) updates.push({ op: withVersion(canon, move), exIds });
    if (ex.type === "frame") {
      if ((ex.name || "") !== (snapshot.name || "")) updates.push({ op: withVersion(canon, { op: "edit", id: canon.id, text: ex.name || "" }), exIds });
    } else if (ex.type === "text") {
      if (typedText(ex) !== String(canon.text || "")) updates.push({ op: withVersion(canon, { op: "edit", id: canon.id, text: typedText(ex) }), exIds });
      const styled = { ...textStyleInputs(ex, snapshot) };
      const color = colorOf(ex.strokeColor);
      if (color && color !== colorOf(snapshot.strokeColor)) styled.color = color;
      if (Object.keys(styled).length) updates.push({ op: withVersion(canon, { op: "restyle", id: canon.id, ...styled }), exIds });
    } else if (ex.type !== "image" && ex.type !== "embeddable") {
      const styled = styleInputs(ex, snapshot);
      if (ex.type === "arrow" || ex.type === "freedraw") delete styled.fill;
      if (Object.keys(styled).length) updates.push({ op: withVersion(canon, { op: "restyle", id: canon.id, ...styled }), exIds });
    }
  }
  return { creates, updates, unsupported };
}

// What the diff compares against: the fields of each element as it was built.
export function snapshotOf(ex) {
  return {
    version: ex.version,
    x: ex.x,
    y: ex.y,
    width: ex.width,
    height: ex.height,
    frameId: ex.frameId || null,
    name: ex.name,
    strokeColor: ex.strokeColor,
    backgroundColor: ex.backgroundColor,
    strokeWidth: ex.strokeWidth,
    strokeStyle: ex.strokeStyle,
    opacity: ex.opacity,
    roughness: ex.roughness,
    fontFamily: ex.fontFamily,
    fontSize: ex.fontSize,
    points: ex.points ? absFrom(ex) : null,
  };
}
