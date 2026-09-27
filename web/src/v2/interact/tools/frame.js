// The frame tool: drag a region; frame {title: "Frame", region} takes in what it wholly holds,
// then the title editor opens on the new frame.
import { buildFrame } from "../ops.js";
import { dragged, ghostRect } from "./common.js";

export function beginFrame(event) {
  const start = event.world;
  const startScreen = event.screen;
  let rect = null;
  return {
    update(ev) {
      if (!rect && !dragged(startScreen, ev)) return null;
      rect = [start[0], start[1], ev.world[0], ev.world[1]];
      return { ghost: ghostRect(rect, { r: 16 }) };
    },
    finish(ev) {
      if (!rect) return null;
      rect = [start[0], start[1], ev.world[0], ev.world[1]];
      const op = buildFrame(rect);
      return op ? { ops: [op], preview: { ghost: ghostRect(rect, { r: 16 }) }, editCreated: true } : null;
    },
    cancel() {},
  };
}
