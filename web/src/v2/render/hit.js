// Hit testing over a display list index (canvas-v2-phase1.md 1.2 `hit`, `handles`, `connect`;
// 6.2). Pure: world points in, ids out. Tolerances are screen pixels, divided by the scale.
import { lodVisible, textLayout } from "./lod.js";

const FRAME_RIM_PX = 8;
const HANDLE_PX = 6; // half of an 8 px handle plus a little slack
const CONNECTOR_PX = 8;
// A side offers its connection point only when the box is at least this many screen pixels across
// that way: below it the reach circles cover the box's middle, and a press meant to select or move
// a small (or zoomed-out) shape would start an arrow instead (QA phase 1, finding 2).
export const CONNECTOR_MIN_SIDE_PX = 3 * CONNECTOR_PX;
// The same rule for resize handles (QA phase 1, V-1): a handle that resizes along a side is offered
// only when the box is at least this many screen pixels across that way (a corner needs both), so a
// drag from the middle of a small selected shape moves it instead of resizing it. An arrow offers
// its end handles only when its ends are this far apart on screen.
export const HANDLE_MIN_SIDE_PX = 3 * HANDLE_PX;

const isBox = (b) => Array.isArray(b) && b.length === 4 && b.every(Number.isFinite);
const isPoints = (pts) => Array.isArray(pts) && pts.length >= 1 && pts.every((p) => Array.isArray(p) && p.length >= 2 && Number.isFinite(p[0]) && Number.isFinite(p[1]));

/** An entry's box [x, y, w, h]: its hit box, a line's point bounds, a pin's circle, else its bbox. */
export function boxOf(entry) {
  const hit = entry?.hit || {};
  if (isBox(hit.box) && hit.shape !== "line") return hit.box;
  if (hit.shape === "line" && isPoints(hit.points)) {
    const xs = hit.points.map((p) => p[0]);
    const ys = hit.points.map((p) => p[1]);
    const x0 = Math.min(...xs);
    const y0 = Math.min(...ys);
    return [x0, y0, Math.max(...xs) - x0, Math.max(...ys) - y0];
  }
  const b = entry?.bbox;
  if (isBox(b)) return [b[0], b[1], b[2] - b[0], b[3] - b[1]];
  return [0, 0, 0, 0];
}

function inBox(pt, box, tol) {
  return pt[0] >= box[0] - tol && pt[0] <= box[0] + box[2] + tol && pt[1] >= box[1] - tol && pt[1] <= box[1] + box[3] + tol;
}

function segmentDistance(p, a, b) {
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  const len2 = dx * dx + dy * dy;
  let t = len2 ? ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / len2 : 0;
  t = Math.max(0, Math.min(1, t));
  return Math.hypot(p[0] - (a[0] + t * dx), p[1] - (a[1] + t * dy));
}

// The title above a frame when zoomed out (1.6) is part of what a click on the frame selects.
function frameTitleHit(entry, pt, scale) {
  for (const p of entry.items || []) {
    if (!p || p.k !== "text" || !p.zoom || !lodVisible(p, scale)) continue;
    const lay = textLayout(p, scale);
    const width = Math.max(0, ...(p.lines || []).map((l) => Number(l.w) || 0)) * (lay.size / (Number(p.size) || lay.size));
    if (inBox(pt, [Number(p.x), lay.top, width, (Number(p.zoom.bottom) || 0) - lay.top], 0)) return true;
  }
  return false;
}

/** Whether world point `pt` hits one hit shape (1.2) at `scale`. */
export function hitShape(hit, pt, scale, tolerancePx = 4, entry = null) {
  if (!hit || typeof hit !== "object") return false;
  const tol = tolerancePx / scale;
  let found = false;
  switch (hit.shape) {
    case "rect":
      found = isBox(hit.box) && inBox(pt, hit.box, tol);
      break;
    case "ellipse": {
      if (!isBox(hit.box)) break;
      const [x, y, w, h] = hit.box;
      const rx = w / 2 + tol;
      const ry = h / 2 + tol;
      const dx = (pt[0] - (x + w / 2)) / (rx || 1);
      const dy = (pt[1] - (y + h / 2)) / (ry || 1);
      found = dx * dx + dy * dy <= 1;
      break;
    }
    case "diamond": {
      if (!isBox(hit.box)) break;
      const [x, y, w, h] = hit.box;
      const hw = w / 2 + tol;
      const hh = h / 2 + tol;
      found = Math.abs(pt[0] - (x + w / 2)) / (hw || 1) + Math.abs(pt[1] - (y + h / 2)) / (hh || 1) <= 1;
      break;
    }
    case "line": {
      if (!isPoints(hit.points)) break;
      const t = Math.max(Number(hit.tol_px) || 0, tolerancePx) / scale;
      const pts = hit.points;
      if (pts.length === 1) found = Math.hypot(pt[0] - pts[0][0], pt[1] - pts[0][1]) <= t;
      for (let i = 1; i < pts.length && !found; i += 1) found = segmentDistance(pt, pts[i - 1], pts[i]) <= t;
      // An arrow's label pill is part of its hit (1.5): `box` on a line hit.
      if (!found && isBox(hit.box)) found = inBox(pt, hit.box, tol);
      break;
    }
    case "frame": {
      if (!isBox(hit.box)) break;
      const [x, y, w, h] = hit.box;
      const band = Math.max(0, Number(hit.band) || 0);
      const rim = FRAME_RIM_PX / scale;
      if (!inBox(pt, hit.box, rim)) {
        found = !!entry && frameTitleHit(entry, pt, scale);
        break;
      }
      const inBand = pt[1] <= y + band && pt[1] >= y - rim;
      const onRim = pt[0] <= x + rim || pt[0] >= x + w - rim || pt[1] <= y + rim || pt[1] >= y + h - rim;
      found = inBand || onRim;
      break;
    }
    case "pin": {
      const r = (Number(hit.r_px) || 10) / scale + tol;
      found = Math.hypot(pt[0] - Number(hit.x), pt[1] - Number(hit.y)) <= r;
      break;
    }
    case "none":
      return false;
    default: {
      // An unknown shape: the entry's drawn bounds.
      const b = entry?.bbox;
      found = isBox(b) && inBox(pt, [b[0], b[1], b[2] - b[0], b[3] - b[1]], tol);
    }
  }
  if (!found && Array.isArray(hit.also)) found = hit.also.some((h) => hitShape(h, pt, scale, tolerancePx, entry));
  return found;
}

/**
 * The topmost entry under world point `pt`, or null, in drawn order: overlays, then arrow label
 * pills (drawn in the labels layer above every mark, 1.2), then the rest, last in render order first.
 */
export function hitTest(index, pt, scale, { tolerancePx = 4, skip = [] } = {}) {
  if (!index) return null;
  const skipped = skip.length ? new Set(skip) : null;
  const entries = index.entries;
  const now = Date.now();
  const live = (e) => !(skipped && skipped.has(e.id)) && !(e.until && Date.parse(e.until) < now);
  const tol = tolerancePx / scale;
  for (let i = entries.length - 1; i >= 0; i -= 1) {
    const e = entries[i];
    if (e.layer === "overlays" && live(e) && hitShape(e.hit || { shape: "rect", box: boxOf(e) }, pt, scale, tolerancePx, e)) return e.id;
  }
  for (let i = entries.length - 1; i >= 0; i -= 1) {
    const e = entries[i];
    if (e.layer !== "overlays" && e.hit?.shape === "line" && isBox(e.hit.box) && live(e) && inBox(pt, e.hit.box, tol)) return e.id;
  }
  for (let i = entries.length - 1; i >= 0; i -= 1) {
    const e = entries[i];
    if (e.layer !== "overlays" && live(e) && hitShape(e.hit || { shape: "rect", box: boxOf(e) }, pt, scale, tolerancePx, e)) return e.id;
  }
  return null;
}

/**
 * The inline parts of an entry (canvas-v2-phase2.md 6.2): [{part, hit, edit, lod?}] that are
 * well formed, in list order (the last is on top). Malformed items are skipped, never thrown on.
 */
export function partsOf(entry) {
  const parts = entry && Array.isArray(entry.parts) ? entry.parts : [];
  return parts.filter((p) => p && typeof p === "object" && typeof p.part === "string" && p.part && p.hit && typeof p.hit === "object");
}

/**
 * The part of entry `id` under world point `pt` at `scale`, or null: the last in the list on top,
 * and only parts drawn at this scale (a part's `lod` band, as for primitives). Parts are hit-tested
 * before the entry's own hit, so a caller that found the entry asks here next.
 */
export function partAt(index, id, pt, scale, { tolerancePx = 0 } = {}) {
  if (!index || !id) return null;
  const entry = index.byId.get(id);
  const parts = partsOf(entry);
  for (let i = parts.length - 1; i >= 0; i -= 1) {
    const p = parts[i];
    if (!lodVisible(p, scale)) continue;
    if (hitShape(p.hit, pt, scale, tolerancePx, entry)) return p;
  }
  return null;
}

/** The part of entry `id` named `part`, or null. */
export function partOf(index, id, part) {
  if (!index || !id || !part) return null;
  return partsOf(index.byId.get(id)).find((p) => p.part === part) || null;
}

/**
 * The ids of the marks and zones inside (mode "contain") or touching (mode "intersect") world
 * `rect` [x0, y0, x1, y1], in render order. Overlays and entries without a hit never count.
 */
export function queryRect(index, rect, { mode = "contain" } = {}) {
  if (!index) return [];
  const x0 = Math.min(rect[0], rect[2]);
  const x1 = Math.max(rect[0], rect[2]);
  const y0 = Math.min(rect[1], rect[3]);
  const y1 = Math.max(rect[1], rect[3]);
  const out = [];
  for (const e of index.entries) {
    if (e.layer === "overlays" || e.hit?.shape === "none") continue;
    const [x, y, w, h] = boxOf(e);
    const inside = x >= x0 && y >= y0 && x + w <= x1 && y + h <= y1;
    const touches = x <= x1 && x + w >= x0 && y <= y1 && y + h >= y0;
    if (mode === "intersect" ? touches : inside) out.push(e.id);
  }
  return out;
}

/**
 * The resize handles an entry offers, as [{handle, point}] (none for a locked entry). With a
 * `scale`, handles are left out on a side too small on screen (HANDLE_MIN_SIDE_PX): the body wins.
 */
export function handlePoints(entry, scale = null) {
  if (!entry || entry.locked) return [];
  const s = Number.isFinite(scale) && scale > 0 ? scale : null;
  const kind = entry.handles;
  if (kind === "ends") {
    const pts = entry.hit?.points;
    if (!isPoints(pts) || pts.length < 2) return [];
    const first = pts[0];
    const last = pts[pts.length - 1];
    if (s !== null && Math.hypot(last[0] - first[0], last[1] - first[1]) * s < HANDLE_MIN_SIDE_PX) return [];
    return [
      { handle: "start", point: first },
      { handle: "end", point: last },
    ];
  }
  const [x, y, w, h] = boxOf(entry);
  const cx = x + w / 2;
  const cy = y + h / 2;
  const tall = s === null || h * s >= HANDLE_MIN_SIDE_PX;
  const wide = s === null || w * s >= HANDLE_MIN_SIDE_PX;
  if (kind === "width") {
    return wide
      ? [
          { handle: "w", point: [x, cy] },
          { handle: "e", point: [x + w, cy] },
        ]
      : [];
  }
  if (kind !== "box") return [];
  const both = tall && wide;
  return [
    both && { handle: "nw", point: [x, y] },
    tall && { handle: "n", point: [cx, y] },
    both && { handle: "ne", point: [x + w, y] },
    wide && { handle: "e", point: [x + w, cy] },
    both && { handle: "se", point: [x + w, y + h] },
    tall && { handle: "s", point: [cx, y + h] },
    both && { handle: "sw", point: [x, y + h] },
    wide && { handle: "w", point: [x, cy] },
  ].filter(Boolean);
}

/** The handle under `pt` of the single selected entry, or null (handles show for one entry only). */
export function handleAt(index, selection, pt, scale) {
  if (!index || !Array.isArray(selection) || selection.length !== 1) return null;
  const entry = index.byId.get(selection[0]);
  const reach = HANDLE_PX / scale;
  for (const { handle, point } of handlePoints(entry, scale)) {
    if (Math.abs(pt[0] - point[0]) <= reach && Math.abs(pt[1] - point[1]) <= reach) return { id: entry.id, handle };
  }
  return null;
}

/**
 * The connection points of an entry arrows may bind to: the side midpoints of its box. With a
 * `scale`, a side is left out when the box is too small on screen across it (CONNECTOR_MIN_SIDE_PX),
 * so the middle of a small shape always selects and moves it.
 */
export function connectorPoints(entry, scale = null) {
  if (!entry || !entry.connect || entry.locked) return [];
  const [x, y, w, h] = boxOf(entry);
  const s = Number.isFinite(scale) && scale > 0 ? scale : null;
  const tall = s === null || h * s >= CONNECTOR_MIN_SIDE_PX;
  const wide = s === null || w * s >= CONNECTOR_MIN_SIDE_PX;
  const out = [];
  if (tall) out.push({ side: "n", point: [x + w / 2, y] });
  if (wide) out.push({ side: "e", point: [x + w, y + h / 2] });
  if (tall) out.push({ side: "s", point: [x + w / 2, y + h] });
  if (wide) out.push({ side: "w", point: [x, y + h / 2] });
  return out;
}

/**
 * The connection point under `pt`, or null: the hovered entry's first, then every other
 * connectable entry's, topmost first. Not only the entry under `pt`: an arrow already bound to a
 * box ends on the box's side point, and would otherwise hide the very point a new arrow needs.
 */
export function connectorAt(index, pt, scale, { hover = null } = {}) {
  if (!index) return null;
  const reach = CONNECTOR_PX / scale;
  const near = (entry) => {
    for (const { side, point } of connectorPoints(entry, scale)) {
      if (Math.hypot(pt[0] - point[0], pt[1] - point[1]) <= reach) return { id: entry.id, side, point };
    }
    return null;
  };
  const hovered = hover ? index.byId.get(hover) : null;
  const first = hovered ? near(hovered) : null;
  if (first) return first;
  const entries = index.entries;
  for (let i = entries.length - 1; i >= 0; i -= 1) {
    const e = entries[i];
    if (e === hovered || !e.connect || (e.until && Date.parse(e.until) < Date.now())) continue;
    const found = near(e);
    if (found) return found;
  }
  return null;
}
