// A drag on empty canvas with the select tool: a rubber band; what it wholly contains becomes
// the selection (Shift adds to it).
import { dragged, ghostRect } from "./common.js";

export function beginMarquee(event, ctx) {
  const start = event.world;
  const startScreen = event.screen;
  const base = event.shift ? ctx.selection.slice() : [];
  let rect = null;
  return {
    update(ev) {
      if (!rect && !dragged(startScreen, ev)) return null;
      rect = [start[0], start[1], ev.world[0], ev.world[1]];
      return { ghost: ghostRect(rect) };
    },
    finish(ev) {
      if (!rect) return { select: base };
      rect = [start[0], start[1], ev.world[0], ev.world[1]];
      const norm = [Math.min(rect[0], rect[2]), Math.min(rect[1], rect[3]), Math.max(rect[0], rect[2]), Math.max(rect[1], rect[3])];
      const found = ctx.query(norm) || [];
      return { select: [...new Set([...base, ...found])] };
    },
    cancel() {},
  };
}
