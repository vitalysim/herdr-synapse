// The adapter between Synapse's canonical scene (contract 5) and Excalidraw elements.
//
// build():  canonical scene -> Excalidraw elements, one page kind (./kinds/) per element type.
//           Every element carries customData.synapse = {id, author, intent, kind, derived}.
//           Claims, locks, comment pins, frame zones and author chips are derived: locked,
//           rebuilt from the scene, and never turned into operations.
// diff():   what the human changed in Excalidraw -> Synapse Sketch operations, each with
//           if_version, so the server stays the only writer of the canonical scene.
import { restoreElements } from "@excalidraw/excalidraw";
import { activeClaims } from "../sceneStore.js";
import { GRID_LINE, MUTED, SURFACE, authorChip, toneColors } from "../theme/tokens.js";
import {
  BOUND_TEXT_PADDING, FONT_TO_EX, LINE_HEIGHT, ROUND_ADAPTIVE, absFrom, aliasOf, base, derived, exIdOf, label, serverLines, textStyleInputs, typedText,
} from "./elements.js";
import { creatorOf, isKnown, kindOf } from "./kinds/index.js";

export { exIdOf } from "./elements.js";

export function boundsOf(el) {
  return [el.x, el.y, el.x + Math.max(1, el.w || 1), el.y + Math.max(1, el.h || 1)];
}

function regionBox(region) {
  const [x0, y0, x1, y1] = region;
  return { x: Math.min(x0, x1), y: Math.min(y0, y1), width: Math.max(1, Math.abs(x1 - x0)), height: Math.max(1, Math.abs(y1 - y0)) };
}

// -- canonical -> Excalidraw -----------------------------------------------------------

// An author's chip on an element's top-right corner: a small round badge with their initials,
// shown while "show authors" is on. Authorship never colours the element itself.
function chipFor(el, scene) {
  const chip = authorChip(scene, el.author);
  const size = 24;
  const x = el.x + Math.max(1, el.w || 1) - size + 8;
  const y = el.y - 8;
  const dot = derived(`chip~${el.id}`, "chip", { type: "ellipse", x, y, width: size, height: size, strokeColor: chip.bg, backgroundColor: chip.bg,
    strokeWidth: 1 }, { id: el.id, author: el.author });
  // Free text centred on the dot: a label bound to a 24-unit ellipse would get 7 units of room.
  const fontSize = 12;
  const width = measureWidth(chip.initials, fontStringOf(fontSize, FONT_TO_EX.normal)) || fontSize * 1.2;
  const text = derived(`chip~${el.id}~t`, "chip", { type: "text", x: x + (size - width) / 2, y: y + (size - fontSize * LINE_HEIGHT) / 2, width,
    height: fontSize * LINE_HEIGHT, strokeColor: chip.fg, backgroundColor: "transparent", text: chip.initials, originalText: chip.initials, fontSize,
    fontFamily: FONT_TO_EX.normal, textAlign: "center", verticalAlign: "middle", containerId: null, autoResize: true }, { id: el.id, author: el.author });
  return [dot, text];
}

// The whole Excalidraw scene for one canonical scene. `options.animated` maps pen ids to
// the points drawn so far while a stroke streams in; `options.authors` shows author chips.
export function build(scene, options) {
  const { team, hiddenAuthors = new Set(), authors = false, vizOn = true, animated = new Map(), now = Date.now() } = options;
  const ctx = {
    team, vizOn, animated, canonToEx: new Map(), arrows: [], fileJobs: [], under: [], pinNumber: 0,
    base: (el) => base(el, exIdOf(el), el.frame ? ctx.canonToEx.get(el.frame) || null : null),
  };
  const visible = (scene.elements || []).filter((el) => !hiddenAuthors.has(el.author));
  for (const el of scene.elements || []) ctx.canonToEx.set(el.id, exIdOf(el));
  const body = [];
  const pins = [];
  const chips = [];
  for (const el of visible) {
    const kind = kindOf(el.type);
    const group = kind.build(el, scene, ctx);
    (kind.layer === "pins" ? pins : body).push(...group);
    if (authors && group.length && kind.layer !== "pins") chips.push(...chipFor(el, scene));
  }
  const overlays = [];
  for (const claim of activeClaims(scene, now)) {
    if (hiddenAuthors.has(claim.author) || !Array.isArray(claim.region)) continue;
    const color = authorChip(scene, claim.author).bg;
    const box = regionBox(claim.region);
    overlays.push(derived(`claim~${claim.id}`, "claim", { type: "rectangle", ...box, strokeColor: color, backgroundColor: "transparent", strokeStyle: "dashed",
      strokeWidth: 2, opacity: 80 }, { id: claim.id, author: claim.author, intent: claim.intent ?? null }));
    overlays.push(label(`claim~${claim.id}~t`, box.x, box.y - 22, `${claim.author}: ${claim.label || "working here"}`, color, "claim", { id: claim.id, author: claim.author }));
  }
  const lockTone = toneColors("neutral", "solid");
  for (const lock of scene.locks || []) {
    if (!Array.isArray(lock.region)) continue;
    const box = regionBox(lock.region);
    overlays.push(derived(`lock~${lock.id}`, "lock", { type: "rectangle", ...box, strokeColor: lockTone.stroke, backgroundColor: lockTone.fill, fillStyle: "cross-hatch",
      strokeWidth: 1, opacity: 45 }, { id: lock.id, author: "human" }));
    overlays.push(label(`lock~${lock.id}~t`, box.x, box.y - 22, `locked by the operator${lock.label ? `: ${lock.label}` : ""}`, MUTED, "lock", { id: lock.id }));
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
  const raw = [...ctx.under, ...body, ...pins, ...overlays, ...chips].map((el) => (el.type === "text" ? { ...el, lineHeight: LINE_HEIGHT } : el));
  // The server's line breaks, by the Excalidraw id of the text that draws them.
  const fitted = new Map();
  for (const el of visible) {
    const lines = serverLines(el);
    if (lines) fitted.set(el.type === "text" ? exIdOf(el) : `${exIdOf(el)}~t`, lines);
  }
  widenForWords(raw, fitted);
  const restored = restoreElements(raw, null, { refreshDimensions: true, repairBindings: true });
  const centered = centerBoundText(growForText(drawServerLines(restored, fitted)));
  const elements = liftArrowLabels(centered, new Set(body.map((el) => el.id)));
  return { elements, fileJobs: ctx.fileJobs };
}

// -- arrow labels sit in pills, above every mark ---------------------------------------------

// The pill around an arrow label (design spec 6.1: surface fill, grid hairline, radius 6, padding 8 x 2),
// as the server draws it (canvas_render._arrow_label).
const PILL_PAD = [8, 2];
const PILL_RADIUS = 6;

// Every arrow's label (./kinds/arrow.js), centred where the server placed it (label_at: clear of every
// mark and label) or else on the arrow's middle, with a pill behind it, goes right after the last canvas
// mark (before claims, locks, pins and chips): a line drawn later never crosses a label (QA R-3), as in
// the agent's picture.
function liftArrowLabels(elements, bodyIds) {
  const byId = new Map(elements.map((el) => [el.id, el]));
  const labels = elements.filter((el) => el.type === "text" && el.customData?.synapse?.derived === "label").map((text) => {
    const arrow = byId.get(text.id.replace(/~t$/, ""));
    const at = text.customData.synapse.at;
    const [cx, cy] = at || (arrow ? arrowMidpoint(arrow).map((v, i) => v + (i ? arrow.y : arrow.x)) : [text.x + text.width / 2, text.y + text.height / 2]);
    return { ...text, x: cx - text.width / 2, y: cy - text.height / 2 };
  });
  if (!labels.length) return elements;
  const lifted = new Set(labels.map((el) => el.id));
  const rest = elements.filter((el) => !lifted.has(el.id));
  const pills = restoreElements(labels.map((text) => {
    const width = advanceWidth(text) + PILL_PAD[0] * 2;
    const height = text.height + PILL_PAD[1] * 2;
    const cx = text.x + text.width / 2;
    const cy = text.y + text.height / 2;
    return derived(`pill~${text.id}`, "pill", { type: "rectangle", x: cx - width / 2, y: cy - height / 2, width, height, strokeColor: GRID_LINE,
      backgroundColor: SURFACE, strokeWidth: 1, roundness: { type: ROUND_ADAPTIVE, value: PILL_RADIUS }, frameId: text.frameId },
    { id: text.customData?.synapse?.id });
  }), null);
  const layer = labels.flatMap((text, index) => [pills[index], text]);
  let after = -1;
  rest.forEach((el, index) => {
    if (bodyIds.has(el.id) || el.customData?.synapse?.derived === "text") after = index;
  });
  return [...rest.slice(0, after + 1), ...layer, ...rest.slice(after + 1)];
}

// Excalidraw wraps a label at its own inner width (the container less 5 units a side), wider
// than the server's padded box, so left alone it breaks lines differently. A fitted label is
// drawn with the server's lines instead; originalText keeps what was typed, for editing and the diff.
// The text box is GLYPH_OVERHANG wider than its widest advance: Excalidraw draws text into a box
// that size, and a glyph reaching past its advance (a code "/" at 1x) lost its edge (QA F-15).
export const GLYPH_OVERHANG = 2;

function drawServerLines(elements, fitted) {
  if (!fitted.size) return elements;
  return elements.map((el) => {
    const lines = el.type === "text" ? fitted.get(el.id) : null;
    if (!lines) return el;
    const font = fontStringOf(el.fontSize, el.fontFamily);
    const widest = Math.ceil(Math.max(...lines.map((line) => measureWidth(line, font)))) + GLYPH_OVERHANG;
    const width = el.containerId || el.autoResize ? widest : Math.max(el.width, widest);
    const customData = { ...el.customData, synapse: { ...el.customData?.synapse, overhang: GLYPH_OVERHANG } };
    return { ...el, text: lines.join("\n"), width, height: lines.length * el.fontSize * LINE_HEIGHT, customData };
  });
}

// A text's width without the overhang drawServerLines added: what its lines take, for fitting checks.
const advanceWidth = (text) => text.width - (text.customData?.synapse?.overhang || 0);

// -- containers grow to fit their labels -------------------------------------------------------

// The server sizes a shape to its label (herdr_team/canvas_text fit policies) with more padding
// than Excalidraw's, so a fitted label never needs these. They catch what it cannot: an element
// stored before labels were fitted, or a font the page measures wider. A container only grows
// (right and down, from the server's top-left), never below the server's size.
const WORD_MAX_W = { rectangle: 320, ellipse: 480, diamond: 480 };
const FAMILY_NAMES = { [FONT_TO_EX.normal]: "Helvetica", [FONT_TO_EX.hand]: "Excalifont", [FONT_TO_EX.code]: "Cascadia" };
let measureContext = null;

// Excalidraw's font string for a text element (its getFontString), for measureText.
export function fontStringOf(fontSize, fontFamily) {
  return `${fontSize}px ${FAMILY_NAMES[fontFamily] || "Helvetica"}, Segoe UI Emoji`;
}

function measureWidth(text, font) {
  if (!measureContext) {
    if (typeof document === "undefined") return 0;
    measureContext = document.createElement("canvas").getContext("2d");
  }
  measureContext.font = font;
  return measureContext.measureText(text).width;
}

// Room a container of `type` gives its text for a container dimension, and the reverse
// (Excalidraw's getBoundTextMaxWidth and computeContainerDimensionForBoundText).
const innerOf = (type, size) => (type === "ellipse" ? Math.round((size / 2) * Math.SQRT2) : type === "diamond" ? Math.round(size / 2) : size) - BOUND_TEXT_PADDING * 2;
function outerFor(type, inner) {
  const need = Math.ceil(inner) + BOUND_TEXT_PADDING * 2;
  if (type === "ellipse") return Math.round((need / Math.SQRT2) * 2) + 1;
  if (type === "diamond") return 2 * need + 1;
  return need;
}

// Before Excalidraw wraps: widen a container whose longest word would otherwise break mid-word
// (labels the server fitted keep their size: their lines are the server's).
function widenForWords(elements, fitted) {
  const byId = new Map(elements.map((el) => [el.id, el]));
  for (const text of elements) {
    if (text.type !== "text" || !text.containerId || text.customData?.synapse?.derived !== "text" || fitted.has(text.id)) continue;
    const container = byId.get(text.containerId);
    if (!container || !(container.type in WORD_MAX_W)) continue;
    const font = fontStringOf(text.fontSize, text.fontFamily);
    const widest = Math.max(0, ...String(text.text).split(/\s+/).map((word) => (word ? measureWidth(word, font) : 0)));
    if (widest <= innerOf(container.type, container.width)) continue;
    const width = Math.min(Math.max(container.width, WORD_MAX_W[container.type]), outerFor(container.type, widest + 1));
    if (width > container.width) {
      container.width = width;
      container.customData = { ...container.customData, synapse: { ...container.customData.synapse, grown: true } };
    }
  }
}

// After Excalidraw has wrapped and measured: grow a container shorter or narrower than its text.
function growForText(elements) {
  const byId = new Map(elements.map((el) => [el.id, el]));
  const grown = new Map();
  for (const text of elements) {
    if (text.type !== "text" || !text.containerId || text.customData?.synapse?.derived !== "text") continue;
    const container = byId.get(text.containerId);
    if (!container || !(container.type in WORD_MAX_W)) continue;
    const advance = advanceWidth(text);
    const width = advance > innerOf(container.type, container.width) + 0.5 ? outerFor(container.type, advance) : container.width;
    const height = text.height > innerOf(container.type, container.height) + 0.5 ? outerFor(container.type, text.height) : container.height;
    if (width > container.width || height > container.height) {
      grown.set(container.id, { ...container, width: Math.max(width, container.width), height: Math.max(height, container.height),
        customData: { ...container.customData, synapse: { ...container.customData.synapse, grown: true } } });
    }
  }
  return grown.size ? elements.map((el) => grown.get(el.id) || el) : elements;
}

// restoreElements measures bound text but leaves it where it was put; place it the way
// Excalidraw does when the human edits: centred in the container's inner box, or on the
// middle of an arrow.
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
    const w = innerOf(container.type, container.width);
    const h = innerOf(container.type, container.height);
    const x = container.x + (container.width - w) / 2;
    const y = container.y + (container.height - h) / 2;
    // Whole units: a label at a half unit is drawn blurred at zoom 1.
    return { ...el, x: Math.round(x + (w - el.width) / 2), y: Math.round(y + (h - el.height) / 2) };
  });
}

// For QA and the corpus comparison (window.__synapseFitAudit): each fitted canonical element's
// server lines against the lines Excalidraw drew, and how far its text runs past the room it has.
export function fitAudit(scene, exElements, { all = false } = {}) {
  const byId = new Map(exElements.map((el) => [el.id, el]));
  const out = [];
  for (const el of scene.elements || []) {
    if (!el.text || (!el.fit && !all)) continue;
    const exId = exIdOf(el);
    const container = byId.get(exId);
    const text = el.type === "text" ? container : byId.get(`${exId}~t`);
    if (!text || text.type !== "text") continue;
    let innerW = text.width;
    let innerH = text.height;
    if (container && container !== text && container.type !== "arrow") {
      innerW = innerOf(container.type, container.width);
      innerH = innerOf(container.type, container.height);
    } else if (el.type === "text" && text.autoResize === false) {
      innerW = el.w || text.width;
    }
    out.push({
      id: el.id, type: el.type, serverLines: el.fit?.lines || null, pageLines: String(text.text).split("\n"), textW: advanceWidth(text), textH: text.height,
      innerW, innerH, overflowPx: Math.max(0, Math.round((advanceWidth(text) - innerW) * 10) / 10, Math.round((text.height - innerH) * 10) / 10),
      fontSize: text.fontSize, grown: Boolean(container?.customData?.synapse?.grown), size: container ? [container.width, container.height] : null,
    });
  }
  return out;
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
  const ctx = { byId, refOf, uploads };

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
    if (!creatorOf(ex.type)) {
      unsupported.push(ex.id);
      continue;
    }
    if (ex.type === "image" && !uploads.has(ex.fileId)) continue;
    creating.add(ex.id);
  }
  for (const ex of fresh) {
    if (!creating.has(ex.id)) continue;
    const op = creatorOf(ex.type).create(ex, ctx);
    if (op) creates.push({ op: { ...op, id: aliasOf(ex.id), ...(ex.type === "line" || ex.type === "freedraw" || ex.type === "frame" ? {} : { client_id: ex.id }) },
      exIds: [ex.id, ...(ex.boundElements || []).map((b) => b.id)], create: ex.id });
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
    // Claims, locks, pins, zones and chips are locked; only Excalidraw itself (re-measuring text
    // when a font loads) changes them, and the next build replaces them anyway.
    if (synapse.derived && synapse.derived !== "text") continue;
    const canon = canonById.get(synapse.id);
    if (!canon || !isKnown(canon.type) || kindOf(canon.type).readonly) continue;
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
    const kind = kindOf(canon.type);
    const changed = kind.diff ? kind.diff(ex, snapshot, canon, ctx) : { move: null, ops: [] };
    const move = { ...(changed.move || {}) };
    if ((ex.frameId || null) !== (snapshot.frameId || null)) move.frame = ex.frameId ? refOf(ex.frameId) : null;
    if (Object.keys(move).length) updates.push({ op: withVersion(canon, { op: "move", id: canon.id, ...move }), exIds });
    for (const op of changed.ops || []) updates.push({ op: withVersion(canon, { ...op, id: canon.id }), exIds });
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
