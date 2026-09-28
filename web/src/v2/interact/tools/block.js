// The block gesture (canvas-v2-phase2.md 6.3 W-e): a click sends the tool's template op at the
// point, a drag sends it over the box (w and h are minimums; the kind fits its content). The editor
// then opens on the new entry's first editable part, else on its own text.
import { buildBlock, stackAt } from "../ops.js";
import { dragged, ghostRect } from "./common.js";

export function beginBlock(event, ctx, tool) {
  const start = event.world;
  const startScreen = event.screen;
  const template = tool && tool.template;
  let rect = null;
  return {
    update(ev) {
      if (!rect && !dragged(startScreen, ev)) return null;
      rect = [start[0], start[1], ev.world[0], ev.world[1]];
      return { ghost: ghostRect(rect, { fill: false, r: 8 }) };
    },
    finish(ev) {
      if (rect) rect = [start[0], start[1], ev.world[0], ev.world[1]];
      // A click in a stack (a kanban column, a row section) makes a new card or sticky join it there.
      const op = buildBlock(template, rect ? { rect } : { point: start, into: ctx.model ? stackAt(ctx.model, start) : null });
      return op ? { ops: [op], preview: rect ? { ghost: ghostRect(rect, { fill: false, r: 8 }) } : null, editCreated: true } : null;
    },
    cancel() {},
  };
}
