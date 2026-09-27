// Dragging the selection: a live translate preview, then one move {ids, by} snapped to the grid
// (Alt moves freely). A single element dropped wholly inside another frame joins it; dropped
// wholly outside its own frame it leaves it (ops.buildMove).
import { buildMove, movingIds, snapDelta } from "../ops.js";
import { deltaOf, dragged } from "./common.js";

export function beginMove(event, ctx, ids) {
  const { model } = ctx;
  const start = event.world;
  const startScreen = event.screen;
  const moving = movingIds(model, ids);
  if (!moving.length) return null;
  let by = [0, 0];
  let live = false;
  return {
    update(ev) {
      if (!live && !dragged(startScreen, ev)) return null;
      live = true;
      by = snapDelta(model, ids, deltaOf(start, ev), { free: ev.alt });
      return { move: { ids: moving, by } };
    },
    finish(ev) {
      if (!live) return null;
      by = snapDelta(model, ids, deltaOf(start, ev), { free: ev.alt });
      const op = buildMove(model, ids, by);
      return op ? { ops: [op], preview: { move: { ids: moving, by } } } : null;
    },
    cancel() {},
  };
}
