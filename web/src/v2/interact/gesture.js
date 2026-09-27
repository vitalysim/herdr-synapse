// One place that maps the active tool to a gesture (tools/*.js). Board calls beginGesture on a
// Surface "down", feeds the gesture "move" events and applies the Outcome of "up".
import { SHAPE_TOOLS } from "./ops.js";
import { beginConnect } from "./tools/connect.js";
import { beginCreate } from "./tools/create.js";
import { beginFrame } from "./tools/frame.js";
import { beginPen } from "./tools/pen.js";
import { beginSelect } from "./tools/select.js";

export const TOOLS = ["select", "hand", "box", "ellipse", "diamond", "note", "text", "arrow", "pen", "frame"];

// Tools that go back to select after one use (as in most whiteboards); the pen stays.
export const ONE_SHOT = new Set(["box", "ellipse", "diamond", "note", "text", "arrow", "frame"]);

export function beginGesture(tool, event, ctx) {
  if (!event || event.button !== 0) return null;
  if (tool === "select" || !ctx.writable) return tool === "hand" ? null : beginSelect(event, ctx);
  if (SHAPE_TOOLS[tool]) return beginCreate(event, ctx, SHAPE_TOOLS[tool]);
  if (tool === "arrow") return beginConnect(event, ctx);
  if (tool === "pen") return beginPen(event, ctx);
  if (tool === "frame") return beginFrame(event, ctx);
  if (tool === "text") {
    return { update: () => null, finish: () => ({ editNewText: event.world }), cancel() {} };
  }
  return null;
}
