// Dragging the selection: a live translate preview, then one move {ids, by} snapped to the grid
// (Alt moves freely). A single element dropped wholly inside another frame joins it; dropped
// wholly outside its own frame it leaves it (ops.buildMove). Over a stack container (a section
// row, column or grid, a kanban column) the preview adds an insertion line where the server will
// put it (canvas-v2-phase2.md 6.3 W-f); the op is the same move.
import { boxOf, buildMove, moveRoots, movingIds, snapDelta, stackDrop } from "../ops.js";
import { deltaOf, dragged, ghostDropLine } from "./common.js";

export function beginMove(event, ctx, ids) {
  const { model } = ctx;
  const start = event.world;
  const startScreen = event.screen;
  const moving = movingIds(model, ids);
  if (!moving.length) return null;
  const roots = moveRoots(model, ids);
  const single = roots.length === 1 ? roots[0] : null;
  const box = single ? boxOf(model, single) : null;
  let by = [0, 0];
  let live = false;
  const preview = () => {
    const out = { move: { ids: moving, by } };
    if (box) {
      const drop = stackDrop(model, single, [box[0] + by[0], box[1] + by[1], box[2], box[3]]);
      if (drop) out.ghost = ghostDropLine(drop.line);
    }
    return out;
  };
  return {
    update(ev) {
      if (!live && !dragged(startScreen, ev)) return null;
      live = true;
      by = snapDelta(model, ids, deltaOf(start, ev), { free: ev.alt });
      return preview();
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
