// A new arrow by dragging: from a hovered entry's connection point (select tool) or from
// anywhere with the arrow tool. Ends that land on a connectable entry are bound
// (arrow {from, to}); free ends are points.
import { buildConnect } from "../ops.js";
import { dragged, ghostArrow, targetAt } from "./common.js";

export function beginConnect(event, ctx, { from = null } = {}) {
  const { model } = ctx;
  const origin = from || targetAt(event, model);
  const startScreen = event.screen;
  let live = false;
  const draw = (ev) => {
    const target = targetAt(ev, model, { skip: origin.id });
    return { ghost: ghostArrow(origin.point, target.point) };
  };
  return {
    update(ev) {
      if (!live && !dragged(startScreen, ev)) return null;
      live = true;
      return draw(ev);
    },
    finish(ev) {
      if (!live) return null;
      const op = buildConnect(origin, targetAt(ev, model, { skip: origin.id }));
      return op ? { ops: [op], preview: draw(ev) } : null;
    },
    cancel() {},
  };
}
