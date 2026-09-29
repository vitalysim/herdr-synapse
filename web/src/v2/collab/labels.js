// Where the presence layer puts its own labels (QA phase 5 L10): a halo's pill and the operator's
// cursor label never cover a label the board draws (an element's text, a claim, freeze, lock or
// proposal label) or each other.
//
// textLines(dl) -> the display list's text lines, once per list (a WeakMap): world lines, and lines
//   in screen-anchored groups (claim, freeze and proposal labels) in px from their anchor, with the
//   rects those groups draw behind their text (a proposal label's chip).
// labelBoxes(lines, camera, viewport) -> [[x0, y0, x1, y1]] in screen px, the lines on screen now.
// placeLabel(candidates, [w, h], obstacles, viewport) -> {x, y} of the first candidate clear of
//   every obstacle (inside the view first, then anywhere), or null when none is clear.
//
// A pan is a linear pass over the prepared lines with no allocation per line beyond its box; the
// entries themselves are walked once per display list.
import { lodVisible, textLayout } from "../render/lod.js";

const IDENTITY = [1, 0, 0, 1, 0, 0];
// A line's ink around its baseline, as fractions of the size (Inter: ascent 0.97, descent 0.24).
const ASCENT = 0.97;
const DESCENT = 0.24;
// Screen px kept between a placed label and a line it avoids.
const GAP_PX = 2;

const finite = (...v) => v.every((n) => typeof n === "number" && Number.isFinite(n));

// m · n for 2D affine matrices [a, b, c, d, e, f] (the SVG order).
function compose(m, n) {
  return [
    m[0] * n[0] + m[2] * n[1],
    m[1] * n[0] + m[3] * n[1],
    m[0] * n[2] + m[2] * n[3],
    m[1] * n[2] + m[3] * n[3],
    m[0] * n[4] + m[2] * n[5] + m[4],
    m[1] * n[4] + m[3] * n[5] + m[5],
  ];
}

const apply = (m, x, y) => [m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]];

function walk(items, m, anchor, lods, clips, out) {
  for (const p of Array.isArray(items) ? items : []) {
    if (!p || typeof p !== "object") continue;
    const path = p.lod ? [...lods, p] : lods;
    if (p.k === "text") {
      const lines = Array.isArray(p.lines) ? p.lines : [];
      const inked = lines.some((l) => l && finite(Number(l.y), Number(l.w)) && String(l.t ?? "").trim());
      if (inked && finite(Number(p.x), Number(p.size)) && Number(p.size) > 0) out.push({ p, lines, m, anchor, lods: path, clips });
    } else if (p.k === "rect" && anchor && finite(p.x, p.y, p.w, p.h)) {
      out.push({ p, rect: [p.x, p.y, p.x + p.w, p.y + p.h], m, anchor, lods: path, clips });
    } else if (p.k === "group") {
      let inner = m;
      let at = anchor;
      if (Array.isArray(p.screen) && p.screen.length === 2 && finite(p.screen[0], p.screen[1])) {
        // Screen px from here on, from the anchor's place in world units (a screen group inside one
        // is taken as a px offset).
        if (anchor) inner = compose(m, [1, 0, 0, 1, p.screen[0], p.screen[1]]);
        else {
          at = apply(m, p.screen[0], p.screen[1]);
          inner = IDENTITY;
        }
      }
      if (Array.isArray(p.t) && p.t.length === 6 && finite(...p.t)) inner = compose(inner, p.t);
      // A group's clip (in its own units: a claim's label stops at the claim beside it) cuts what is inside.
      const clipped = Array.isArray(p.clip) && p.clip.length === 4 && finite(...p.clip) ? [...clips, { m: inner, anchor: at, rect: [p.clip[0], p.clip[1], p.clip[0] + p.clip[2], p.clip[1] + p.clip[3]] }] : clips;
      walk(p.items, inner, at, path, clipped, out);
    }
  }
}

const linesCache = new WeakMap();

/** Every text line the display list draws, prepared for labelBoxes. */
export function textLines(dl) {
  if (!dl || typeof dl !== "object") return [];
  if (linesCache.has(dl)) return linesCache.get(dl);
  const out = [];
  for (const entry of Array.isArray(dl.entries) ? dl.entries : []) {
    if (entry && typeof entry === "object") walk(entry.items, IDENTITY, null, [], [], out);
  }
  linesCache.set(dl, out);
  return out;
}

/** The screen boxes of the lines on screen at this camera (lod-hidden ones left out). */
export function labelBoxes(lines, camera, viewport) {
  const out = [];
  if (!camera || !viewport || !(camera.scale > 0)) return out;
  const s = camera.scale;
  const view = [-viewport.w, -viewport.h, viewport.w * 2, viewport.h * 2];
  const toScreen = (m, anchor, cx, cy) => {
    const [lx, ly] = apply(m, cx, cy);
    // In a screen group the local unit is a screen px; otherwise it is a world unit.
    return anchor ? [(anchor[0] - camera.x) * s + lx, (anchor[1] - camera.y) * s + ly] : [(lx - camera.x) * s, (ly - camera.y) * s];
  };
  const boxOf = (m, anchor, l, t, r, b) => {
    let x0 = Infinity;
    let y0 = Infinity;
    let x1 = -Infinity;
    let y1 = -Infinity;
    for (const [cx, cy] of [[l, t], [r, t], [l, b], [r, b]]) {
      const [sx, sy] = toScreen(m, anchor, cx, cy);
      x0 = Math.min(x0, sx);
      y0 = Math.min(y0, sy);
      x1 = Math.max(x1, sx);
      y1 = Math.max(y1, sy);
    }
    return x1 < view[0] || x0 > view[2] || y1 < view[1] || y0 > view[3] ? null : [x0, y0, x1, y1];
  };
  const within = (box, cut) => {
    if (!box) return null;
    let [x0, y0, x1, y1] = box;
    for (const c of cut) {
      x0 = Math.max(x0, c[0]);
      y0 = Math.max(y0, c[1]);
      x1 = Math.min(x1, c[2]);
      y1 = Math.min(y1, c[3]);
    }
    return x1 > x0 && y1 > y0 ? [x0, y0, x1, y1] : null;
  };
  for (const { p, lines: ls, rect, m, anchor, lods, clips } of lines) {
    if (!lods.every((q) => lodVisible(q, s))) continue;
    const cut = clips.map((c) => boxOf(c.m, c.anchor, ...c.rect) || [0, 0, 0, 0]);
    if (rect) {
      const found = within(boxOf(m, anchor, rect[0], rect[1], rect[2], rect[3]), cut);
      if (found) out.push(found);
      continue;
    }
    const layout = textLayout(p, s);
    const size = Number(p.size);
    const grow = layout.zoomed && size > 0 ? layout.size / size : 1;
    const x = Number(p.x);
    ls.forEach((line, i) => {
      if (!line || !String(line.t ?? "").trim()) return;
      const w = Number(line.w) * grow;
      const base = layout.ys[i];
      if (!finite(w, base)) return;
      const left = p.anchor === "middle" ? x - w / 2 : p.anchor === "end" ? x - w : x;
      const found = within(boxOf(m, anchor, left, base - ASCENT * layout.size, left + w, base + DESCENT * layout.size), cut);
      if (found) out.push(found);
    });
  }
  return out;
}

const meets = (a, b) => a[0] < b[2] + GAP_PX && a[2] + GAP_PX > b[0] && a[1] < b[3] + GAP_PX && a[3] + GAP_PX > b[1];

const clamp = (v, lo, hi) => (hi < lo ? v : Math.min(Math.max(v, lo), hi));

/**
 * The first of `candidates` ([[x, y]], top-left) where a `size` box covers no obstacle, or null.
 * With a viewport each candidate is first tried pulled into the view, so a label stays readable at
 * the edge; then as it is.
 */
export function placeLabel(candidates, size, obstacles, viewport = null) {
  const [w, h] = size;
  const free = ([x, y]) => finite(x, y) && !obstacles.some((o) => meets([x, y, x + w, y + h], o));
  const pulled = viewport ? candidates.map(([x, y]) => [clamp(x, 0, viewport.w - w), clamp(y, 0, viewport.h - h)]) : [];
  const found = pulled.find(free) || candidates.find(free);
  return found ? { x: found[0], y: found[1] } : null;
}

// What a label is estimated to take on screen before it is drawn: its characters at the pill's
// font (500 11px Inter averages under 6.4 px), its chip and padding. An overestimate only makes a
// label more careful.
export const PILL_H = 20;
export const PILL_MAX_W = 300;
export const pillWidth = (text) => Math.min(PILL_MAX_W, 32 + String(text || "").length * 6.4);
export const CHIP_W = 20;
export const CURSOR_LABEL = [60, 16];
