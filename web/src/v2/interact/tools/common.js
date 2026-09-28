// What the pointer tools share: the gesture contract, drag thresholds, drop targets and the ghost
// primitives drawn in Surface's UI group while a gesture runs.
//
// A tool's begin(event, ctx) returns a gesture, or null when the pointer-down does nothing:
//   { update(event) -> Preview | null,        // on every move; what Surface draws meanwhile
//     finish(event) -> Outcome | null,        // on up
//     cancel() }                              // on cancel / Esc
// Outcome: { ops?: Op[], select?: string[], preview?: Preview (kept until the server answers),
//            editCreated?: boolean (open the editor on what the op creates), editNewText?: Pt,
//            toast?: string }
// ctx: { model (ops.createModel), selection: string[], writable, query(rect) -> ids,
//        setSelection(ids) }; events are Surface's SurfacePointer (world, screen, scale, ...).

export const DRAG_PX = 3;

export function dragged(start, event) {
  if (!start || !event || !event.screen) return false;
  return Math.hypot(event.screen[0] - start[0], event.screen[1] - start[1]) >= DRAG_PX;
}

export const deltaOf = (start, event) => [event.world[0] - start[0], event.world[1] - start[1]];

// Where an arrow end lands: a connection point or a connectable entry under the pointer (bound),
// else the world point (free). `skip` is the arrow being edited.
export function targetAt(event, model, { skip = null } = {}) {
  if (event.connector && event.connector.id && event.connector.id !== skip) {
    return { id: event.connector.id, point: event.connector.point || event.world };
  }
  if (event.hit && event.hit !== skip) {
    const entry = model.entry(event.hit);
    if (entry && entry.connect) return { id: entry.id, point: event.world };
  }
  return { id: null, point: event.world };
}

// -- ghosts (display-list primitives, colours as tokens) -----------------------------------------

const SEL = "base.selection";

export function ghostRect([x0, y0, x1, y1], { fill = true, r = 0 } = {}) {
  const x = Math.min(x0, x1);
  const y = Math.min(y0, y1);
  const w = Math.abs(x1 - x0);
  const h = Math.abs(y1 - y0);
  const out = [];
  if (fill) out.push({ k: "rect", x, y, w, h, r, fill: SEL, stroke: null, op: 0.08 });
  out.push({ k: "rect", x, y, w, h, r, fill: null, stroke: SEL, sw_px: 1 });
  return out;
}

export function ghostShape(kind, rect) {
  const [x0, y0, x1, y1] = rect;
  const x = Math.min(x0, x1);
  const y = Math.min(y0, y1);
  const w = Math.abs(x1 - x0);
  const h = Math.abs(y1 - y0);
  if (kind === "ellipse") return [{ k: "ellipse", cx: x + w / 2, cy: y + h / 2, rx: w / 2, ry: h / 2, fill: null, stroke: SEL, sw_px: 1.5 }];
  if (kind === "diamond") {
    const points = [[x + w / 2, y], [x + w, y + h / 2], [x + w / 2, y + h], [x, y + h / 2]];
    return [{ k: "poly", points, closed: true, fill: null, stroke: SEL, sw_px: 1.5 }];
  }
  return [{ k: "rect", x, y, w, h, r: kind === "note" ? 4 : 8, fill: null, stroke: SEL, sw_px: 1.5 }];
}

export function ghostArrow(from, to) {
  const len = Math.hypot(to[0] - from[0], to[1] - from[1]);
  const heads = [];
  if (len > 1) {
    const ux = (to[0] - from[0]) / len;
    const uy = (to[1] - from[1]) / len;
    const size = Math.min(12, len / 2);
    const back = [to[0] - ux * size, to[1] - uy * size];
    heads.push({ at: "end", shape: "chevron", points: [[back[0] - uy * size * 0.5, back[1] + ux * size * 0.5], to, [back[0] + uy * size * 0.5, back[1] - ux * size * 0.5]] });
  }
  return [{ k: "arrow", d: `M${from[0]} ${from[1]} L${to[0]} ${to[1]}`, stroke: SEL, sw: 2, heads }];
}

// A stack drop's insertion line (canvas-v2-phase2.md 6.3 W-f): 3 screen px in the selection colour.
export function ghostDropLine(points) {
  if (!Array.isArray(points) || points.length < 2) return [];
  return [{ k: "line", points, stroke: SEL, sw_px: 3 }];
}

export function ghostLine(points) {
  return [{ k: "line", points, stroke: "base.ink", sw: 2 }];
}
