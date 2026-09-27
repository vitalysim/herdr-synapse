// Handles: a box or width handle resizes (move {id, w, h} + to when the top or left edge moved);
// an arrow's start or end handle rebinds that end to what it is dropped on, or frees it.
import { arrowPoints, boxOf, buildRebind, buildResize, resizeBox } from "../ops.js";
import { deltaOf, dragged, ghostArrow, targetAt } from "./common.js";

export function beginResize(event, ctx, { id, handle }) {
  const { model } = ctx;
  const box = boxOf(model, id);
  if (!box) return null;
  const entry = model.entry(id);
  // A width entry (a text) resizes sideways only.
  const allowed = entry && entry.handles === "width" ? handle.replace(/[ns]/g, "") : handle;
  if (!allowed) return null;
  const start = event.world;
  const startScreen = event.screen;
  let next = box;
  let live = false;
  const step = (ev) => {
    next = resizeBox(box, allowed, deltaOf(start, ev), { keepAspect: Boolean(ev.shift) && allowed.length === 2 });
    return { boxes: { [id]: next } };
  };
  return {
    update(ev) {
      if (!live && !dragged(startScreen, ev)) return null;
      live = true;
      return step(ev);
    },
    finish(ev) {
      if (!live) return null;
      const preview = step(ev);
      const op = buildResize(model, id, next);
      return op ? { ops: [op], preview } : null;
    },
    cancel() {},
  };
}

export function beginEnds(event, ctx, { id, handle }) {
  const { model } = ctx;
  const points = arrowPoints(model, id);
  if (!points) return null;
  const fixed = handle === "end" ? points[0] : points[points.length - 1];
  const startScreen = event.screen;
  let live = false;
  const draw = (ev) => {
    const target = targetAt(ev, model, { skip: id });
    const tip = target.point;
    return { hide: [id], ghost: handle === "end" ? ghostArrow(fixed, tip) : ghostArrow(tip, fixed) };
  };
  return {
    update(ev) {
      if (!live && !dragged(startScreen, ev)) return null;
      live = true;
      return draw(ev);
    },
    finish(ev) {
      if (!live) return null;
      const op = buildRebind(model, id, handle, targetAt(ev, model, { skip: id }));
      return op ? { ops: [op], preview: draw(ev) } : null;
    },
    cancel() {},
  };
}
