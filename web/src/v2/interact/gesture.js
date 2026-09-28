// One place that maps the active tool to a gesture (tools/*.js). Board calls beginGesture on a
// Surface "down", feeds the gesture "move" events and applies the Outcome of "up". The create
// tools come from the kind registry (toolset.js); each names one of the GESTURES below.
import { beginBlock } from "./tools/block.js";
import { beginConnect } from "./tools/connect.js";
import { beginCreate } from "./tools/create.js";
import { beginFrame } from "./tools/frame.js";
import { beginPen } from "./tools/pen.js";
import { beginSelect } from "./tools/select.js";
import { GESTURE_NAMES, TOOLSET } from "./toolset.js";

// Every tool id, in tool bar order: select, hand, then the kinds' tools.
export const TOOLS = TOOLSET.ids;

// Tools that go back to select after one use (as in most whiteboards); the pen stays.
export const ONE_SHOT = TOOLSET.oneShot;

// gesture name (canvas_kinds.GESTURES) -> (event, ctx, tool) -> gesture.
export const GESTURES = {
  shape: (event, ctx, tool) => beginCreate(event, ctx, tool.kind, tool.template),
  text: (event) => ({ update: () => null, finish: () => ({ editNewText: event.world }), cancel() {} }),
  connect: (event, ctx) => beginConnect(event, ctx),
  pen: (event, ctx) => beginPen(event, ctx),
  frame: (event, ctx, tool) => beginFrame(event, ctx, tool),
  block: (event, ctx, tool) => beginBlock(event, ctx, tool),
};
if (GESTURE_NAMES.some((name) => !GESTURES[name])) throw new Error("gesture.js: a gesture in toolset.js GESTURE_NAMES has no handler");

export function beginGesture(tool, event, ctx, toolset = TOOLSET) {
  if (!event || event.button !== 0) return null;
  if (tool === "select" || !ctx.writable) return tool === "hand" ? null : beginSelect(event, ctx);
  const found = toolset.get(tool);
  const begin = found && found.gesture ? GESTURES[found.gesture] : null;
  return begin ? begin(event, ctx, found) : null;
}
