// The frame tool: drag a region; frame {title: "Frame", region} takes in what it wholly holds,
// then the title editor opens on the new frame. A tool with a template (the section tool,
// canvas-v2-phase2.md 6.4) sends that op with the region instead.
import { buildFrame } from "../ops.js";
import { dragged, ghostRect } from "./common.js";

export function beginFrame(event, ctx, tool = null) {
  const template = tool && tool.template ? tool.template : null;
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
      const op = buildFrame(rect, "Frame", template);
      return op ? { ops: [op], preview: { ghost: ghostRect(rect, { r: 16 }) }, editCreated: true } : null;
    },
    cancel() {},
  };
}
