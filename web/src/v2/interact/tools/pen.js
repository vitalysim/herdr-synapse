// The pen: pointer samples drawn live as a polyline, then one pen op (at most 500 points).
import { buildPen } from "../ops.js";
import { ghostLine } from "./common.js";

export function beginPen(event) {
  const points = [event.world];
  return {
    update(ev) {
      const last = points[points.length - 1];
      if (ev.world[0] !== last[0] || ev.world[1] !== last[1]) points.push(ev.world);
      return { ghost: ghostLine(points.slice()) };
    },
    finish(ev) {
      if (ev && ev.world) {
        const last = points[points.length - 1];
        if (ev.world[0] !== last[0] || ev.world[1] !== last[1]) points.push(ev.world);
      }
      const op = buildPen(points);
      return op ? { ops: [op], preview: { ghost: ghostLine(points.slice()) } } : null;
    },
    cancel() {},
  };
}
