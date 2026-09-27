// A pen stroke: smooth strokes are Excalidraw freedraw (with pressure when the author sent it),
// straight ones a line. Strokes an agent just drew animate in along their points (ctx.animated).
import { MAX_PEN_POINTS, absFrom, absPoints, round, sample, styleInputs } from "../elements.js";

export default {
  names: ["pen"],
  creates: ["line", "freedraw"],

  build(el, scene, ctx) {
    const style = el.style || {};
    const pts = absPoints(ctx.animated.get(el.id) || el.points);
    if (pts.length < 2) return [];
    const b = ctx.base(el);
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
  },

  create(ex) {
    if (ex.type === "line") {
      const pts = absFrom(ex);
      if (pts.length < 2) return null;
      const first = pts[0];
      const last = pts[pts.length - 1];
      const closed = pts.length > 2 && first[0] === last[0] && first[1] === last[1];
      const op = { op: "pen", points: sample(pts, MAX_PEN_POINTS), closed, style: "straight", ...styleInputs(ex, null) };
      if (!closed) delete op.fill;
      delete op.dash;
      delete op.rough;
      return op;
    }
    const hasPressure = !ex.simulatePressure && Array.isArray(ex.pressures) && ex.pressures.length === (ex.points || []).length;
    const pts = (ex.points || []).map(([px, py], i) => {
      const point = [round(ex.x + px), round(ex.y + py)];
      if (hasPressure) point.push(Math.round(ex.pressures[i] * 100) / 100);
      return point;
    });
    if (pts.length < 2) return null;
    const op = { op: "pen", points: sample(pts, MAX_PEN_POINTS), closed: false, style: "smooth", ...styleInputs(ex, null) };
    delete op.fill;
    delete op.dash;
    delete op.rough;
    return op;
  },

  diff(ex, snapshot) {
    const pts = absFrom(ex);
    const move = JSON.stringify(pts) !== JSON.stringify(snapshot.points || []) ? { points: sample(pts, MAX_PEN_POINTS) } : null;
    const styled = styleInputs(ex, snapshot);
    if (ex.type === "freedraw") delete styled.fill;
    return { move, ops: Object.keys(styled).length ? [{ op: "restyle", ...styled }] : [] };
  },
};
