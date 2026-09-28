// The create tools (R rectangle, O ellipse, D diamond, N note): drag a box, or click for the
// kind's default size; the editor opens on the new element once it arrives. A tool with a
// template (canvas-v2-phase2.md 6.4) sends that op, placed the same way.
import { buildShape, stackAt } from "../ops.js";
import { dragged, ghostShape } from "./common.js";

export function beginCreate(event, ctx, kind, template = null) {
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
      // A click in a stack (a kanban column, a row section) makes the new element join it there.
      const op = buildShape(kind, rect ? { rect, template } : { point: start, template, into: ctx.model ? stackAt(ctx.model, start) : null });
      return op ? { ops: [op], preview: rect ? { ghost: ghostShape(kind, rect) } : null, editCreated: true } : null;
    },
    cancel() {},
  };
}
