// The page's kind registry, keyed by canonical element type (the same names as the Python
// registry, herdr_team/canvas_kinds). A kind definition:
//
//   names     the canonical types it draws
//   build(el, scene, ctx) -> Excalidraw elements for one canonical element
//                            ctx: {base(el), team, vizOn, animated, canonToEx, arrows, fileJobs, under, pinNumber}
//   creates   Excalidraw types a human draws that become this kind (optional)
//   create(ex, ctx) -> the operation for a new Excalidraw element, or null   ctx: {byId, refOf, uploads}
//   diff(ex, snapshot, canon, ctx) -> {move: fields | null, ops: [partial op]}  what the human changed
//   layer     "pins" to draw above everything (optional)
//   readonly  never turned into operations (optional)
//
// An unknown kind draws the placeholder card, so an older page tolerates a newer server.
import arrow from "./arrow.js";
import comment from "./comment.js";
import frame from "./frame.js";
import media from "./media.js";
import { PAGE_KINDS } from "./names.js";
import pen from "./pen.js";
import placeholder from "./placeholder.js";
import shape from "./shape.js";
import text from "./text.js";

const byName = new Map();
const byExType = new Map();

export function register(def) {
  for (const name of def.names || []) {
    if (byName.has(name)) throw new Error(`canvas kind ${name} is registered twice`);
    byName.set(name, def);
  }
  for (const exType of def.creates || []) byExType.set(exType, def);
  return def;
}

for (const def of [shape, text, arrow, frame, pen, media, comment]) register(def);

// The definition that draws a canonical element of type `name`; the placeholder when unknown.
export const kindOf = (name) => byName.get(name) || placeholder;
export const isKnown = (name) => byName.has(name);
// The definition that turns a new Excalidraw element of type `exType` into an operation, or null.
export const creatorOf = (exType) => byExType.get(exType) || null;
export const pageKinds = () => [...byName.keys()].sort();

{
  const registered = pageKinds().join(",");
  const listed = [...PAGE_KINDS].sort().join(",");
  if (registered !== listed) console.error(`canvas kinds: registered [${registered}] differ from names.js [${listed}]`);
}
