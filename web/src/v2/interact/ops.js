// Every op the v2 board sends is shaped here, and only here (canvas-v2-phase1.md 4.3). Pure:
// builders read the display list and the canonical elements through a small model and return
// plain op objects (or null when there is nothing to send). The board POSTs them unchanged to
// /api/teams/<t>/ops, so the server stays the only writer; the page never edits the display
// list or the scene itself.
//
// Boxes here are [x, y, w, h] (as the display list's hit and edit boxes); rects are
// [x0, y0, x1, y1] (as the render interface's Rect).

export const GRID = 20;
export const NUDGE = 1;
export const NUDGE_FAR = GRID;
export const PEN_MAX_POINTS = 500;
export const MIN_SIZE = 10;
export const TONES = ["neutral", "info", "success", "warning", "danger", "accent", "idea", "decision"];
export const VARIANTS = ["soft", "solid", "outline"];
export const SIZES = ["s", "m", "l", "xl"];
export const DASHES = ["solid", "dashed", "dotted"];
export const FONTS = ["normal", "hand", "code"];
// The kinds a create tool makes with the shape op (the text tool goes through the editor).
export const SHAPE_TOOLS = { box: "box", ellipse: "ellipse", diamond: "diamond", note: "note" };

const r2 = (v) => Math.round(v * 100) / 100;
const round = (v) => Math.round(v);
const isElementId = (id) => typeof id === "string" && /^(E|C)-[1-9][0-9]*$/.test(id);

// -- the model the builders read ---------------------------------------------------------------

// model(dl, elementOf, {own}): entries by id from the display list, the canonical element by id
// from the scene store (optional: every builder falls back to the entry alone), and `own(id)`: the
// version this page's own last applied op left an element at (optional). The list catches up
// 100-250 ms after an op, so a second drag in that window would otherwise send the version the
// list still shows and be refused as stale (QA phase 1, finding 6).
export function createModel(dl, elementOf = () => null, { own = null } = {}) {
  const byId = new Map();
  for (const entry of (dl && dl.entries) || []) byId.set(entry.id, entry);
  return {
    dl,
    own: (id) => {
      const v = typeof own === "function" ? own(id) : null;
      return Number.isFinite(v) ? v : null;
    },
    entry: (id) => byId.get(id) || null,
    element: (id) => {
      try {
        return elementOf(id) || null;
      } catch {
        return null;
      }
    },
    entries: () => byId.values(),
  };
}

// The version the page last saw of an element: its entry's `v` (the element's updated_seq),
// or the canonical element's updated_seq when the entry is missing; a newer version this page's
// own op left it at wins (the list has not caught up with it yet). Another writer's change since
// is still refused by the server, since that version is then newer than both.
export function versionOf(m, id) {
  const entry = m.entry(id);
  let v = null;
  if (entry && Number.isFinite(entry.v)) v = entry.v;
  else {
    const el = m.element(id);
    v = el && Number.isFinite(el.updated_seq) ? el.updated_seq : null;
  }
  const own = typeof m.own === "function" ? m.own(id) : null;
  return own !== null && (v === null || own > v) ? own : v;
}

// {id: version} for what one /ops answer changed, when that is known exactly: with one applied
// op, every id it lists is at the answer's version (its event's seq). With several, which op last
// touched an id is not said, so nothing is recorded (the list's own versions then apply).
export function appliedVersions(result) {
  const applied = (result && result.applied) || [];
  if (applied.length !== 1 || !Number.isFinite(result.version)) return {};
  const out = {};
  for (const id of applied[0].ids || []) if (typeof id === "string") out[id] = result.version;
  return out;
}

// if_version for an op over `ids`: the server checks one number against every target, so an op
// carries it when all its targets share one version (always, for a single target).
export function ifVersionOf(m, ids) {
  let seen = null;
  for (const id of ids) {
    const v = versionOf(m, id);
    if (v === null) return null;
    if (seen !== null && v !== seen) return null;
    seen = v;
  }
  return seen;
}

function withVersion(m, op, ids) {
  const v = ifVersionOf(m, ids);
  if (v !== null) op.if_version = v;
  return op;
}

// The element's own box [x, y, w, h]: the canonical x, y, w, h, else its hit box, else its bbox.
export function boxOf(m, id) {
  const el = m.element(id);
  if (el && [el.x, el.y, el.w, el.h].every(Number.isFinite)) return [el.x, el.y, el.w, el.h];
  const entry = m.entry(id);
  if (!entry) return null;
  const hit = entry.hit || {};
  if (Array.isArray(hit.box) && hit.box.length === 4) return hit.box.slice();
  if (Array.isArray(hit.points) && hit.points.length) return boxOfPoints(hit.points);
  if (Array.isArray(entry.bbox) && entry.bbox.length === 4) {
    const [x0, y0, x1, y1] = entry.bbox;
    return [x0, y0, x1 - x0, y1 - y0];
  }
  return null;
}

function boxOfPoints(points) {
  const xs = points.map((p) => p[0]);
  const ys = points.map((p) => p[1]);
  const x0 = Math.min(...xs);
  const y0 = Math.min(...ys);
  return [x0, y0, Math.max(...xs) - x0, Math.max(...ys) - y0];
}

export function kindOf(m, id) {
  const entry = m.entry(id);
  if (entry && entry.kind) return entry.kind;
  const el = m.element(id);
  return el ? el.type : null;
}

function frameOf(m, id) {
  const el = m.element(id);
  if (el && "frame" in el) return el.frame || null;
  const entry = m.entry(id);
  return entry ? entry.frame || null : null;
}

function groupOf(m, id) {
  const el = m.element(id);
  return el ? el.group || null : null;
}

// Every element inside the given frames (recursively) or groups, as the server's _descendants.
export function descendantsOf(m, ids) {
  const found = [];
  const seen = new Set(ids);
  let frontier = new Set(ids);
  const all = [...m.entries()].map((entry) => entry.id).filter(isElementId);
  while (frontier.size) {
    const next = new Set();
    for (const id of all) {
      if (seen.has(id)) continue;
      if (frontier.has(frameOf(m, id)) || frontier.has(groupOf(m, id))) {
        seen.add(id);
        found.push(id);
        next.add(id);
      }
    }
    frontier = next;
  }
  return found;
}

function ancestorsOf(m, id) {
  const out = [];
  const seen = new Set([id]);
  let at = id;
  for (;;) {
    const parent = frameOf(m, at) || groupOf(m, at);
    if (!parent || seen.has(parent)) return out;
    out.push(parent);
    seen.add(parent);
    at = parent;
  }
}

function endsOf(m, id) {
  const el = m.element(id);
  return el ? { from: el.from || null, to: el.to || null } : { from: null, to: null };
}

// -- geometry helpers ------------------------------------------------------------------------

export const contains = (outer, inner) =>
  inner[0] >= outer[0] && inner[1] >= outer[1] && inner[0] + inner[2] <= outer[0] + outer[2] && inner[1] + inner[3] <= outer[1] + outer[3];

export const intersects = (a, b) => a[0] < b[0] + b[2] && b[0] < a[0] + a[2] && a[1] < b[1] + b[3] && b[1] < a[1] + a[3];

export function rectToBox([x0, y0, x1, y1]) {
  const ax = Math.min(x0, x1);
  const ay = Math.min(y0, y1);
  return [ax, ay, Math.abs(x1 - x0), Math.abs(y1 - y0)];
}

export const snap = (value, grid = GRID) => Math.round(value / grid) * grid;

// -- move (drag and nudge) ---------------------------------------------------------------------

// What a drag of `ids` sends: the selection without comments' anchors, claims or locks, without
// children whose frame (or group) is also moving (they follow it), and without arrows bound at
// both ends (they follow what they bind; moving them explicitly would unbind them).
export function moveRoots(m, ids) {
  const set = new Set(ids.filter(isElementId));
  return [...set].filter((id) => {
    if (ancestorsOf(m, id).some((parent) => set.has(parent))) return false;
    if (kindOf(m, id) === "arrow") {
      const { from, to } = endsOf(m, id);
      if (from && to) return false;
    }
    return true;
  });
}

// Everything that moves on screen during the drag (the preview): the roots, their children, and
// arrows bound at both ends to what moves.
export function movingIds(m, ids) {
  const roots = moveRoots(m, ids);
  const moving = new Set([...roots, ...descendantsOf(m, roots)]);
  for (const id of ids) {
    if (kindOf(m, id) !== "arrow") continue;
    const { from, to } = endsOf(m, id);
    if (from && to && moving.has(from) && moving.has(to)) moving.add(id);
  }
  for (const entry of m.entries()) {
    if (entry.kind !== "arrow" || moving.has(entry.id)) continue;
    const { from, to } = endsOf(m, entry.id);
    if (from && to && moving.has(from) && moving.has(to)) moving.add(entry.id);
  }
  return [...moving];
}

// The drag delta: snapped so the first root's top-left lands on the grid (Alt moves freely).
export function snapDelta(m, ids, [dx, dy], { free = false, grid = GRID } = {}) {
  if (free) return [r2(dx), r2(dy)];
  const roots = moveRoots(m, ids);
  const box = roots.length ? boxOf(m, roots[0]) : null;
  if (!box) return [snap(dx, grid), snap(dy, grid)];
  return [r2(snap(box[0] + dx, grid) - box[0]), r2(snap(box[1] + dy, grid) - box[1])];
}

// The frame a single element dropped at `box` belongs to: wholly inside a frame other than its
// own joins the innermost such frame; wholly outside its own frame leaves it. undefined = no change.
export function dropFrame(m, id, box) {
  const current = frameOf(m, id);
  const exclude = new Set([id, ...descendantsOf(m, [id])]);
  let best = null;
  let bestArea = Infinity;
  for (const entry of m.entries()) {
    if (entry.kind !== "frame" || exclude.has(entry.id)) continue;
    const frameBox = boxOf(m, entry.id);
    if (!frameBox || !contains(frameBox, box)) continue;
    const area = frameBox[2] * frameBox[3];
    if (area < bestArea) {
      best = entry.id;
      bestArea = area;
    }
  }
  if (best) return best === current ? undefined : best;
  if (current) {
    const frameBox = boxOf(m, current);
    if (frameBox && !intersects(frameBox, box)) return null;
  }
  return undefined;
}

export function buildMove(m, ids, by) {
  const roots = moveRoots(m, ids);
  const [dx, dy] = [r2(by[0]), r2(by[1])];
  if (!roots.length) return null;
  const op = { op: "move", ids: roots };
  if (dx || dy) op.by = [dx, dy];
  if (roots.length === 1 && kindOf(m, roots[0]) !== "comment") {
    const box = boxOf(m, roots[0]);
    if (box) {
      const frame = dropFrame(m, roots[0], [box[0] + dx, box[1] + dy, box[2], box[3]]);
      if (frame !== undefined) op.frame = frame;
    }
  }
  if (!op.by && !("frame" in op)) return null;
  return withVersion(m, op, roots);
}

// Arrow keys: 1 unit, or a grid step with Shift.
export function buildNudge(m, ids, key, far = false) {
  const step = far ? NUDGE_FAR : NUDGE;
  const by = { ArrowLeft: [-step, 0], ArrowRight: [step, 0], ArrowUp: [0, -step], ArrowDown: [0, step] }[key];
  if (!by) return null;
  const roots = moveRoots(m, ids);
  if (!roots.length) return null;
  return withVersion(m, { op: "move", ids: roots, by }, roots);
}

// -- resize --------------------------------------------------------------------------------------

// The box after dragging `handle` by [dx, dy] (world units): each named edge follows the
// pointer, and the box never gets smaller than `min` or turns inside out.
export function resizeBox(box, handle, [dx, dy], { min = MIN_SIZE, keepAspect = false } = {}) {
  let [x0, y0] = box;
  let x1 = box[0] + box[2];
  let y1 = box[1] + box[3];
  if (handle.includes("w")) x0 = Math.min(x0 + dx, x1 - min);
  if (handle.includes("e")) x1 = Math.max(x1 + dx, x0 + min);
  if (handle.includes("n")) y0 = Math.min(y0 + dy, y1 - min);
  if (handle.includes("s")) y1 = Math.max(y1 + dy, y0 + min);
  if (keepAspect && handle.length === 2 && box[2] > 0 && box[3] > 0) {
    const ratio = box[2] / box[3];
    const w = x1 - x0;
    const h = y1 - y0;
    if (w / h > ratio) {
      const nh = w / ratio;
      if (handle.includes("n")) y0 = y1 - nh;
      else y1 = y0 + nh;
    } else {
      const nw = h * ratio;
      if (handle.includes("w")) x0 = x1 - nw;
      else x1 = x0 + nw;
    }
  }
  return [r2(x0), r2(y0), r2(x1 - x0), r2(y1 - y0)];
}

// move {id, w, h} (+ to when the top or left edge moved). A `width` entry (a text) sends w only.
// The server treats w and h as minimums, so the element may come back bigger.
export function buildResize(m, id, next) {
  const box = boxOf(m, id);
  const entry = m.entry(id);
  if (!box || !next) return null;
  const widthOnly = entry && entry.handles === "width";
  const op = { op: "move", id };
  const w = Math.max(1, round(next[2]));
  const h = Math.max(1, round(next[3]));
  const x = round(next[0]);
  const y = round(next[1]);
  if (w !== round(box[2])) op.w = w;
  if (!widthOnly && h !== round(box[3])) op.h = h;
  if (x !== round(box[0]) || (!widthOnly && y !== round(box[1]))) op.to = [x, widthOnly ? round(box[1]) : y];
  if (!("w" in op) && !("h" in op) && !("to" in op)) return null;
  if (widthOnly && !("w" in op)) op.w = Math.max(1, round(box[2]));
  return withVersion(m, op, [id]);
}

// -- arrows -----------------------------------------------------------------------------------------

const pointString = ([x, y]) => `${r2(x)},${r2(y)}`;

// The canonical points of an arrow (x, y only), or its hit line.
export function arrowPoints(m, id) {
  const el = m.element(id);
  if (el && Array.isArray(el.points) && el.points.length >= 2) return el.points.map((p) => [p[0], p[1]]);
  const entry = m.entry(id);
  const hit = entry && entry.hit;
  if (hit && Array.isArray(hit.points) && hit.points.length >= 2) return hit.points.map((p) => [p[0], p[1]]);
  return null;
}

// An arrow end dragged to `target`: onto a connectable entry rebinds that end
// (move {id, to_element: X} or {id, from: X}); onto empty canvas frees it at the point
// (move {id, to_element: null, points}).
export function buildRebind(m, id, end, target) {
  if (end !== "start" && end !== "end") return null;
  const key = end === "end" ? "to_element" : "from";
  const bound = endsOf(m, id)[end === "end" ? "to" : "from"];
  if (target && target.id) {
    if (target.id === id || target.id === bound) return null;
    return withVersion(m, { op: "move", id, [key]: target.id }, [id]);
  }
  if (!target || !Array.isArray(target.point)) return null;
  const points = arrowPoints(m, id);
  if (!points) return null;
  const next = points.map((p) => [r2(p[0]), r2(p[1])]);
  next[end === "end" ? next.length - 1 : 0] = [r2(target.point[0]), r2(target.point[1])];
  return withVersion(m, { op: "move", id, [key]: null, points: next }, [id]);
}

// A new arrow from a drag: each end is an entry id (bound) or a world point. Two free ends make
// an arrow by points; otherwise from/to, a free end as "x,y".
export function buildConnect(from, to, { minLength = 8 } = {}) {
  if (!from || !to) return null;
  const fromId = from.id || null;
  const toId = to.id || null;
  if (fromId && toId) return fromId === toId ? null : { op: "arrow", from: fromId, to: toId };
  if (!fromId && !toId) {
    const [a, b] = [from.point, to.point];
    if (!a || !b || Math.hypot(b[0] - a[0], b[1] - a[1]) < minLength) return null;
    return { op: "arrow", points: [[r2(a[0]), r2(a[1])], [r2(b[0]), r2(b[1])]] };
  }
  return { op: "arrow", from: fromId || pointString(from.point), to: toId || pointString(to.point) };
}

// -- creating ---------------------------------------------------------------------------------------

// A create tool's drag (rect in world units) or click (rect null, at the point): the kind's
// default size on a click. The text starts empty; the editor opens once the element arrives.
export function buildShape(kind, { rect = null, point = null } = {}) {
  if (!Object.values(SHAPE_TOOLS).includes(kind)) return null;
  if (rect) {
    const [x, y, w, h] = rectToBox(rect);
    if (w >= MIN_SIZE && h >= MIN_SIZE) return { op: "shape", kind, at: [round(x), round(y)], w: round(w), h: round(h), text: "" };
    return buildShape(kind, { point: [x, y] });
  }
  if (!point) return null;
  return { op: "shape", kind, at: [round(point[0]), round(point[1])], text: "" };
}

// A new free text (a double-click on empty canvas, or the text tool), sent once typed.
export function buildText(text, at) {
  const value = String(text ?? "");
  if (!value.trim() || !at) return null;
  return { op: "shape", kind: "text", text: value, at: [round(at[0]), round(at[1])] };
}

// At most `max` points, evenly spaced over the stroke, keeping its first and last.
export function samplePoints(points, max = PEN_MAX_POINTS) {
  const list = (points || []).filter((p) => Array.isArray(p) && Number.isFinite(p[0]) && Number.isFinite(p[1]));
  if (list.length <= max) return list.slice();
  const out = [];
  const step = (list.length - 1) / (max - 1);
  for (let i = 0; i < max; i += 1) out.push(list[Math.round(i * step)]);
  return out;
}

export function buildPen(points) {
  const sampled = samplePoints(points).map((p) => [r2(p[0]), r2(p[1])]);
  const unique = sampled.filter((p, i) => i === 0 || p[0] !== sampled[i - 1][0] || p[1] !== sampled[i - 1][1]);
  if (unique.length < 2) return null;
  return { op: "pen", points: unique, style: "smooth", closed: false };
}

export function buildFrame(rect, title = "Frame") {
  const [x, y, w, h] = rectToBox(rect);
  if (w < MIN_SIZE * 2 || h < MIN_SIZE * 2) return null;
  return { op: "frame", title, region: [round(x), round(y), round(x + w), round(y + h)] };
}

// -- editing and styling ------------------------------------------------------------------------------

// The editor's commit: nothing when the text did not change.
export function buildEdit(m, id, text) {
  const entry = m.entry(id);
  if (!entry || !entry.edit) return null;
  const value = String(text ?? "");
  const before = entry.edit.value ?? "";
  if (value === before) return null;
  return withVersion(m, { op: "edit", id, text: value }, [id]);
}

export function buildDelete(m, ids) {
  const targets = [...new Set(ids.filter(isElementId))];
  if (!targets.length) return null;
  const op = { op: "delete", ids: targets };
  if (targets.some((id) => kindOf(m, id) === "frame")) op.with_children = false;
  return withVersion(m, op, targets);
}

const STYLE_CHOICES = { tone: TONES, variant: VARIANTS, size: SIZES, dash: DASHES, font: FONTS };

// The style bar: one field at a time (tone, variant, size, dash or font).
export function buildRestyle(m, ids, patch) {
  const targets = [...new Set(ids.filter(isElementId))].filter((id) => kindOf(m, id) !== "comment");
  if (!targets.length || !patch) return null;
  const op = { op: "restyle", ids: targets };
  let any = false;
  for (const [field, choices] of Object.entries(STYLE_CHOICES)) {
    if (patch[field] === undefined) continue;
    if (!choices.includes(patch[field])) throw new Error(`unknown ${field} ${patch[field]}`);
    op[field] = patch[field];
    any = true;
  }
  return any ? withVersion(m, op, targets) : null;
}

export function buildUndo(batch) {
  return typeof batch === "string" && batch ? { op: "undo", batch } : null;
}
