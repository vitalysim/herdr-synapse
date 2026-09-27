// An arrow, bound to the elements it connects, with an optional label in a pill where the server put it.
//
// The label is a locked free text (derived "label"), not Excalidraw's bound text: Excalidraw always draws a
// bound arrow label on the arrow's middle and in the arrow's layer, and the server places labels clear of
// marks and other labels, drawn above every mark (QA R-3). The adapter centres it and adds the pill. A
// human rewrites a label by typing one on the arrow (double-click it): the new bound text becomes an edit.
import { HEAD_TO_EX, MAX_ARROW_POINTS, ROUND_PROPORTIONAL, absFrom, absPoints, boundLabelOf, boundText, headOf, sample, styleInputs, typedText } from "../elements.js";

// A label is drawn at this fraction of the arrow's text size (herdr_team/canvas_render.ARROW_LABEL_SCALE),
// for an arrow stored before the server recorded its label's size (fit.size).
const ARROW_LABEL_SCALE = 0.8;
// A two-point curve bows right of its direction by this fraction of its length at its control point
// (canvas_render.CURVE_BOW), so half that at its middle: the page bends its arrow through that middle.
const CURVE_BOW = 0.2;

// Where the server centred the label (label_at, QA R-3), or null for the arrow's own middle.
function labelAt(el) {
  const at = el.label_at;
  return Array.isArray(at) && at.length === 2 && at.every((v) => Number.isFinite(Number(v))) ? [Number(at[0]), Number(at[1])] : null;
}

// Excalidraw draws a rounded two-point arrow straight; the server's picture bows it. A middle point
// on the server's curve makes the page bend the same way (and its label, placed on that curve, sit on it).
function drawnPoints(el, pts) {
  if (!el.curve || pts.length !== 2) return pts;
  const [[ax, ay], [bx, by]] = pts;
  const bow = CURVE_BOW / 2;
  return [pts[0], [(ax + bx) / 2 - (by - ay) * bow, (ay + by) / 2 + (bx - ax) * bow], pts[1]];
}

export default {
  names: ["arrow"],
  creates: ["arrow"],

  build(el, scene, ctx) {
    const pts = drawnPoints(el, absPoints(el.points));
    if (pts.length < 2) return [];
    const [x0, y0] = pts[0];
    const arrow = {
      ...ctx.base(el),
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
    const label = boundText(el, arrow);
    label.fontSize = el.fit?.size || Math.min(label.fontSize, 20) * ARROW_LABEL_SCALE;
    label.containerId = null;
    label.locked = true;
    label.customData = { synapse: { ...label.customData.synapse, derived: "label", at: labelAt(el) } };
    return [arrow, label];
  },

  create(ex, ctx) {
    const pts = absFrom(ex);
    if (pts.length < 2) return null;
    const from = ex.startBinding ? ctx.refOf(ex.startBinding.elementId) : null;
    const to = ex.endBinding ? ctx.refOf(ex.endBinding.elementId) : null;
    const op = { op: "arrow", head: headOf(ex.endArrowhead), tail: headOf(ex.startArrowhead), curve: Boolean(ex.roundness), ...styleInputs(ex, null) };
    delete op.fill;
    delete op.rough;
    const label = boundLabelOf(ex, ctx.byId);
    if (label && typedText(label)) op.label = typedText(label);
    if (from || to) {
      op.from = from || `${pts[0][0]},${pts[0][1]}`;
      op.to = to || `${pts[pts.length - 1][0]},${pts[pts.length - 1][1]}`;
    } else {
      op.points = sample(pts, MAX_ARROW_POINTS);
    }
    return op;
  },

  diff(ex, snapshot, canon, ctx) {
    const move = {};
    const pts = absFrom(ex);
    if (JSON.stringify(pts) !== JSON.stringify(snapshot.points || [])) {
      // An arrow bound at both ends follows its elements on the server; only a rebinding moves it.
      if (!(canon.from && canon.to && ex.startBinding && ex.endBinding)) move.points = sample(pts, MAX_ARROW_POINTS);
    }
    const from = ex.startBinding ? ctx.refOf(ex.startBinding.elementId) : null;
    const to = ex.endBinding ? ctx.refOf(ex.endBinding.elementId) : null;
    if ((from || null) !== (canon.from || null)) move.from = from;
    if ((to || null) !== (canon.to || null)) move.to_element = to;
    if (Object.keys(move).length && !move.points) move.points = sample(pts, MAX_ARROW_POINTS);
    const styled = styleInputs(ex, snapshot);
    delete styled.fill;
    const ops = Object.keys(styled).length ? [{ op: "restyle", ...styled }] : [];
    // A label the human typed on the arrow (its drawn label is locked, so this is the way to rewrite it).
    const typed = boundLabelOf(ex, ctx.byId);
    if (typed && typedText(typed) && typedText(typed) !== String(canon.text || "")) ops.push({ op: "edit", text: typedText(typed) });
    return { move: Object.keys(move).length ? move : null, ops };
  },
};
