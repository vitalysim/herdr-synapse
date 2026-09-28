// Every op the v2 board sends is shaped here, and only here (canvas-v2-phase1.md 4.3). Pure:
// builders read the display list and the canonical elements through a small model and return
// plain op objects (or null when there is nothing to send). The board POSTs them unchanged to
// /api/teams/<t>/ops, so the server stays the only writer; the page never edits the display
// list or the scene itself.
//
// Boxes here are [x, y, w, h] (as the display list's hit and edit boxes); rects are
// [x0, y0, x1, y1] (as the render interface's Rect).
import { TOOLSET, templateOf } from "./toolset.js";

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
// Arrow route styles (canvas-v2-phase2.md 3.2 and 6.3 W-c): the style bar's straight, elbow, curve.
export const ROUTES = ["straight", "orthogonal", "curved"];
// The kinds a create tool makes with the shape op (the text tool goes through the editor): the
// registry's tools with the "shape" gesture (toolset.js).
export const SHAPE_TOOLS = TOOLSET.shapes;

const r2 = (v) => Math.round(v * 100) / 100;
const round = (v) => Math.round(v);
const isElementId = (id) => typeof id === "string" && /^(E|C)-[1-9][0-9]*$/.test(id);

// -- the model the builders read ---------------------------------------------------------------

// model(dl, elementOf, {own}): entries by id from the display list, the canonical element by id
// from the scene store (optional: every builder falls back to the entry alone), and `own(id)`: the
// version this page's own last applied op left an element at (optional; Board passes newestVersion,
// which also knows the newest list). The list catches up
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

// The newest version the page knows of element `id`: what its own applied ops left it at (`own`,
// a Map) or what the newest display list shows (`index`, render/cull.js createIndex), whichever is
// higher. Board hands this to createModel as `own`: a gesture keeps the model it started with,
// and when the list catches up while it runs the page drops its own version, so neither the
// model's list nor the own versions alone would do (QA phase 1, V-3). Versions only grow.
export function newestVersion(own, index, id) {
  const mine = own && typeof own.get === "function" ? own.get(id) : undefined;
  const entry = index && index.byId ? index.byId.get(id) : null;
  const shown = entry && Number.isFinite(entry.v) ? entry.v : null;
  if (!Number.isFinite(mine)) return shown;
  return shown !== null && shown > mine ? shown : mine;
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

// Whether element `id` is a container other elements join: a frame, and every block stored as one
// (section, kanban and its columns, graph, mindmap, timeline: canvas-v2-phase2.md D2). Entry kinds
// are now the block's kind, so the canonical type decides; without the element, the entry's frame
// hit or its stack `container` does.
export function isContainer(m, id) {
  const el = m.element(id);
  if (el && typeof el.type === "string") return el.type === "frame";
  const entry = m.entry(id);
  if (!entry) return false;
  return entry.kind === "frame" || (entry.hit && entry.hit.shape === "frame") || Boolean(stackOf(entry));
}

// A stack container's drop hint (6.2 `container`), or null: {layout, gap, order}.
export function stackOf(entry) {
  const c = entry && entry.container;
  if (!c || typeof c !== "object" || !["row", "column", "grid"].includes(c.layout)) return null;
  return { layout: c.layout, gap: Number.isFinite(Number(c.gap)) ? Number(c.gap) : GRID, order: Array.isArray(c.order) ? c.order.filter((id) => typeof id === "string") : [] };
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

// The container a single element dropped at `box` belongs to: a plain frame when the box lands
// wholly inside it, a stack container (a section row, column or grid; a kanban column) when the
// box's centre lands inside it, the innermost such container winning. The element's own frame
// means no change (undefined); wholly outside its own frame it leaves it (null).
export function dropFrame(m, id, box) {
  const current = frameOf(m, id);
  const exclude = new Set([id, ...descendantsOf(m, [id])]);
  const centre = [box[0] + box[2] / 2, box[1] + box[3] / 2];
  let best = null;
  let bestArea = Infinity;
  for (const entry of m.entries()) {
    if (exclude.has(entry.id) || !isContainer(m, entry.id)) continue;
    const frameBox = boxOf(m, entry.id);
    if (!frameBox) continue;
    const inside = stackOf(entry) ? pointIn(frameBox, centre) : contains(frameBox, box);
    if (!inside) continue;
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

const pointIn = (box, [x, y]) => x >= box[0] && x <= box[0] + box[2] && y >= box[1] && y <= box[1] + box[3];

// -- stack drops (canvas-v2-phase2.md 1.5 and 6.3 W-f) ------------------------------------------------

// The index a member dropped with its centre at `centre` takes among a stack container's other
// members (`siblings`: their boxes in layout order, the dragged one left out), by the server's
// rule (herdr_team/canvas_blocks.py drop_index): the number of siblings whose centre comes before
// it along the axis; row-major for a grid (a sibling comes first when its bottom is at or above the
// point, or it spans the point's height and its centre is left of it).
// Shared with the server through tests/fixtures/display/stack-drop-vectors.json.
export function stackIndex(layout, siblings, centre) {
  const [px, py] = centre;
  let index = 0;
  for (const b of siblings) {
    const cx = b[0] + b[2] / 2;
    const cy = b[1] + b[3] / 2;
    let before;
    if (layout === "row") before = cx < px;
    else if (layout === "column") before = cy < py;
    else before = b[1] + b[3] <= py || (b[1] <= py && py < b[1] + b[3] && cx < px);
    if (before) index += 1;
  }
  return index;
}

// Where the insertion line goes for `index` among `siblings` in container box `cbox`: two world
// points. Row: a vertical line in the gap before the index-th sibling (or after the last);
// column: a horizontal one; grid: a vertical line beside the sibling it lands before.
export function stackDropLine(layout, siblings, index, cbox, gap = GRID) {
  const half = gap / 2;
  if (!siblings.length) {
    const inset = Math.min(GRID, cbox[2] / 4, cbox[3] / 4);
    if (layout === "column") return [[cbox[0] + inset, cbox[1] + cbox[3] / 2], [cbox[0] + cbox[2] - inset, cbox[1] + cbox[3] / 2]];
    return [[cbox[0] + cbox[2] / 2, cbox[1] + inset], [cbox[0] + cbox[2] / 2, cbox[1] + cbox[3] - inset]];
  }
  const at = Math.max(0, Math.min(index, siblings.length));
  const next = siblings[at] || null;
  const prev = siblings[at - 1] || null;
  if (layout === "column") {
    const x0 = Math.min(...siblings.map((b) => b[0]));
    const x1 = Math.max(...siblings.map((b) => b[0] + b[2]));
    const y = next && prev ? (prev[1] + prev[3] + next[1]) / 2 : next ? next[1] - half : prev[1] + prev[3] + half;
    return [[x0, y], [x1, y]];
  }
  if (layout === "row") {
    const y0 = Math.min(...siblings.map((b) => b[1]));
    const y1 = Math.max(...siblings.map((b) => b[1] + b[3]));
    const x = next && prev ? (prev[0] + prev[2] + next[0]) / 2 : next ? next[0] - half : prev[0] + prev[2] + half;
    return [[x, y0], [x, y1]];
  }
  const ref = next || prev;
  const x = next ? next[0] - half : prev[0] + prev[2] + half;
  return [[x, ref[1]], [x, ref[1] + ref[3]]];
}

// The innermost stack container (a section row, column or grid; a kanban column) holding world
// point `p`, and the index something new placed there takes: {container, index}, or null. A new
// card or sticky clicked into a stack joins it (`in`), as a drop would (QA phase 2, F6).
export function stackAt(m, p) {
  if (!Array.isArray(p)) return null;
  let best = null;
  let bestArea = Infinity;
  for (const entry of m.entries()) {
    if (!stackOf(entry) || entry.locked) continue;
    const b = boxOf(m, entry.id);
    if (!b || !pointIn(b, p)) continue;
    const area = b[2] * b[3];
    if (area < bestArea) {
      best = entry;
      bestArea = area;
    }
  }
  if (!best) return null;
  const stack = stackOf(best);
  const members = stack.order.length ? stack.order : [...m.entries()].filter((e) => frameOf(m, e.id) === best.id).map((e) => e.id);
  const siblings = members.map((sid) => boxOf(m, sid)).filter(Boolean);
  return { container: best.id, index: stackIndex(stack.layout, siblings, p) };
}

// The drop hint while one element is dragged to `box`: {container, index, line} when it would land
// in a stack container (the one it would join, or the stack it is already in), else null.
export function stackDrop(m, id, box) {
  const joined = dropFrame(m, id, box);
  const target = joined === undefined ? frameOf(m, id) : joined;
  if (!target) return null;
  const entry = m.entry(target);
  const stack = stackOf(entry);
  const cbox = stack ? boxOf(m, target) : null;
  if (!stack || !cbox) return null;
  const members = stack.order.length ? stack.order : [...m.entries()].filter((e) => frameOf(m, e.id) === target).map((e) => e.id);
  const siblings = members.filter((sid) => sid !== id).map((sid) => boxOf(m, sid)).filter(Boolean);
  const centre = [box[0] + box[2] / 2, box[1] + box[3] / 2];
  const index = stackIndex(stack.layout, siblings, centre);
  return { container: target, index, line: stackDropLine(stack.layout, siblings, index, cbox, stack.gap) };
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
// default size on a click. The text starts empty; the editor opens once the element arrives. A
// tool with a template sends that op instead, placed the same way.
export function buildShape(kind, { rect = null, point = null, template = null, into = null } = {}) {
  if (templateOf(template)) return buildBlock(template, { rect, point, into });
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

// The frame gesture: frame {title, region}, or the tool's template with the region (a section).
export function buildFrame(rect, title = "Frame", template = null) {
  const [x, y, w, h] = rectToBox(rect);
  if (w < MIN_SIZE * 2 || h < MIN_SIZE * 2) return null;
  const region = [round(x), round(y), round(x + w), round(y + h)];
  if (templateOf(template)) return buildFromTemplate(template, { region });
  return { op: "frame", title, region };
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
  // Deleting a frame or a block root keeps what it holds, as loose elements (canvas-v2-phase2.md 1.7).
  if (targets.some((id) => isContainer(m, id))) op.with_children = false;
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

// -- blocks: parts, pins, routes and tool templates (canvas-v2-phase2.md 6.3) -------------------------

// An entry's inline part named `part` (6.2 `parts`), or null.
export function partOfEntry(entry, part) {
  const parts = entry && Array.isArray(entry.parts) ? entry.parts : [];
  return parts.find((p) => p && p.part === part) || null;
}

// The part editor's commit: edit {id, part, text} (a table cell, a card's title or body), nothing
// when the part has no editor or the text did not change.
export function buildEditPart(m, id, part, text) {
  const found = partOfEntry(m.entry(id), part);
  if (!found || !found.edit) return null;
  const value = String(text ?? "");
  if (value === (found.edit.value ?? "")) return null;
  return withVersion(m, { op: "edit", id, part, text: value }, [id]);
}

const pinOf = (m, id) => {
  const entry = m.entry(id);
  const pin = entry && entry.pin;
  return pin === "human" || pin === "agent" ? pin : null;
};

// Pin: holds each selected element where it is (no layout, growth or other author moves it).
export function buildPin(m, ids) {
  const targets = [...new Set((ids || []).filter(isElementId))].filter((id) => kindOf(m, id) !== "comment" && pinOf(m, id) !== "human");
  if (!targets.length) return null;
  return withVersion(m, { op: "pin", ids: targets }, targets);
}

// Unpin: the selected elements that hold a pin; the enclosing block then lays them out again.
export function buildUnpin(m, ids) {
  const targets = [...new Set((ids || []).filter(isElementId))].filter((id) => pinOf(m, id));
  if (!targets.length) return null;
  return withVersion(m, { op: "unpin", ids: targets }, targets);
}

// What the style bar's pin buttons offer for a selection: {pin, unpin} (either may be false).
export function pinChoices(m, ids) {
  return { pin: Boolean(buildPin(m, ids)), unpin: Boolean(buildUnpin(m, ids)) };
}

// The style bar's route choice: restyle {ids, route} over the selected arrows only.
export function buildRestyleRoute(m, ids, route) {
  if (!ROUTES.includes(route)) throw new Error(`unknown route ${route}`);
  const targets = [...new Set((ids || []).filter(isElementId))].filter((id) => kindOf(m, id) === "arrow");
  if (!targets.length) return null;
  return withVersion(m, { op: "restyle", ids: targets, route }, targets);
}

// A tool's template (tokens.json `tools[].template`, canvas-v2-phase2.md 6.4): toolset.js reads it.
export { templateOf };

// A template op placed by a gesture: the template's fields, then the gesture's own (`at` and
// optionally `w`/`h` for block and shape, `region` for frame), which win.
export function buildFromTemplate(template, fields) {
  const t = templateOf(template);
  if (!t) return null;
  return { ...JSON.parse(JSON.stringify(t)), ...fields };
}

// The block gesture (6.3 W-e): a click sends the template at the point; a drag also sends the box's
// w and h (sizes are minimums: the kind fits its content).
// With `into` ({container, index}: stackAt), a click in a stack container sends `in` and `index`
// instead of `at`: the new element joins that stack where it was clicked.
export function buildBlock(template, { rect = null, point = null, into = null } = {}) {
  if (rect) {
    const [x, y, w, h] = rectToBox(rect);
    if (w >= MIN_SIZE && h >= MIN_SIZE) return buildFromTemplate(template, { at: [round(x), round(y)], w: round(w), h: round(h) });
    return buildBlock(template, { point: [x, y], into });
  }
  if (!point) return null;
  if (into && typeof into.container === "string" && JOINS.has((templateOf(template) || {}).op)) {
    return buildFromTemplate(template, { in: into.container, index: Math.max(0, Math.round(Number(into.index) || 0)) });
  }
  return buildFromTemplate(template, { at: [round(point[0]), round(point[1])] });
}

// The template ops that join a stack when clicked into one (a card, and a sticky, which a kanban
// column takes as a card); a block of its own (a table, a kanban) stays where it was clicked.
const JOINS = new Set(["card", "sticky"]);

export function buildUndo(batch) {
  return typeof batch === "string" && batch ? { op: "undo", batch } : null;
}
