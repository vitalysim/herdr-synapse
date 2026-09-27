// The select tool's pointer-down: a handle resizes or rebinds, a connection point starts an
// arrow, an entry is selected (Shift toggles) and dragged, empty canvas starts a marquee.
// A read-only page only selects.
import { beginMarquee } from "./marquee.js";
import { beginMove } from "./move.js";
import { beginConnect } from "./connect.js";
import { beginEnds, beginResize } from "./resize.js";
import { dragged } from "./common.js";

export function lockedReason(entry) {
  return `${entry.id} is in a locked region; unlock it first`;
}

export function beginSelect(event, ctx) {
  const { model, selection, writable } = ctx;
  if (writable && event.handle && event.handle.id) {
    const entry = model.entry(event.handle.id);
    if (entry && !entry.locked) {
      const { handle } = event.handle;
      return handle === "start" || handle === "end" ? beginEnds(event, ctx, event.handle) : beginResize(event, ctx, event.handle);
    }
  }
  if (writable && event.connector && event.connector.id) {
    const entry = model.entry(event.connector.id);
    if (entry && entry.connect && !entry.locked) {
      return beginConnect(event, ctx, { from: { id: entry.id, point: event.connector.point || event.world } });
    }
  }
  const hit = event.hit ? model.entry(event.hit) : null;
  if (!hit) return beginMarquee(event, ctx);
  if (event.shift) {
    const next = selection.includes(hit.id) ? selection.filter((id) => id !== hit.id) : [...selection, hit.id];
    ctx.setSelection(next);
    return null;
  }
  const ids = selection.includes(hit.id) ? selection : [hit.id];
  if (ids !== selection) ctx.setSelection(ids);
  const collapse = { select: [hit.id] };
  if (!writable) return { update: () => null, finish: () => (ids.length > 1 ? collapse : null), cancel() {} };
  const locked = ids.map((id) => model.entry(id)).find((entry) => entry && entry.locked);
  if (locked) {
    let warned = false;
    return {
      update(ev) {
        if (!warned && dragged(event.screen, ev)) warned = true;
        return null;
      },
      finish: () => (warned ? { toast: lockedReason(locked) } : null),
      cancel() {},
    };
  }
  const move = beginMove(event, ctx, ids);
  if (!move) return null;
  return {
    update: (ev) => move.update(ev),
    // A click on one of several selected entries (no drag) selects just that one.
    finish: (ev) => move.finish(ev) || (ids.length > 1 ? collapse : null),
    cancel: () => move.cancel(),
  };
}
