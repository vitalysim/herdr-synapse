// The create tools (R rectangle, O ellipse, D diamond, N note): drag a box, or click for the
// kind's default size; the editor opens on the new element once it arrives.
import { buildShape } from "../ops.js";
import { dragged, ghostShape } from "./common.js";

export function beginCreate(event, ctx, kind) {
  const start = event.world;
  const startScreen = event.screen;
  let rect = null;
  return {
    update(ev) {
      if (!rect && !dragged(startScreen, ev)) return null;
      rect = [start[0], start[1], ev.world[0], ev.world[1]];
      return { ghost: ghostShape(kind, rect) };
    },
    finish(ev) {
      if (rect) rect = [start[0], start[1], ev.world[0], ev.world[1]];
      const op = buildShape(kind, rect ? { rect } : { point: start });
      return op ? { ops: [op], preview: rect ? { ghost: ghostShape(kind, rect) } : null, editCreated: true } : null;
    },
    cancel() {},
  };
}
