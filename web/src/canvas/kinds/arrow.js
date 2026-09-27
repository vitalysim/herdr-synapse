// An arrow, bound to the elements it connects, with an optional label on its middle.
import { HEAD_TO_EX, MAX_ARROW_POINTS, ROUND_PROPORTIONAL, absFrom, absPoints, boundLabelOf, boundText, headOf, sample, styleInputs, typedText } from "../elements.js";

export default {
  names: ["arrow"],
  creates: ["arrow"],

  build(el, scene, ctx) {
    const pts = absPoints(el.points);
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
    label.fontSize = Math.min(label.fontSize, 20);
    arrow.boundElements = [{ type: "text", id: label.id }];
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
    return { move: Object.keys(move).length ? move : null, ops: Object.keys(styled).length ? [{ op: "restyle", ...styled }] : [] };
  },
};
