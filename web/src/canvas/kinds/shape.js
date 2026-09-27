// box, ellipse, diamond and note: a container with an optional bound label. The server sizes
// the container to its label (canvas_text fit policies); the adapter grows it on the page only if
// Excalidraw's own wrap still needs more room (an element stored before labels were fitted).
import { STICKY_FILLS } from "../../theme/tokens.js";
import { boundLabelOf, boundText, boxMove, colorOf, round, roundnessOf, styleInputs, textStyleInputs, typedText } from "../elements.js";

const EX_TYPE = { box: "rectangle", note: "rectangle", ellipse: "ellipse", diamond: "diamond" };

export default {
  names: ["box", "ellipse", "diamond", "note"],
  creates: ["rectangle", "ellipse", "diamond"],

  build(el, scene, ctx) {
    const container = { ...ctx.base(el), type: EX_TYPE[el.type], roundness: roundnessOf(el.type) };
    if (!el.text) return [container];
    const text = boundText(el, container);
    container.boundElements = [{ type: "text", id: text.id }];
    return [container, text];
  },

  // A shape the human drew. A rectangle on sticky paper (any tone's, or the 0.21 note yellow) is a note.
  create(ex, ctx) {
    const label = boundLabelOf(ex, ctx.byId);
    const kind = ex.type === "rectangle" ? (STICKY_FILLS.has(colorOf(ex.backgroundColor)) ? "note" : "box") : ex.type;
    return {
      op: "shape", kind, text: label ? typedText(label) : "", at: [round(ex.x), round(ex.y)], w: Math.max(1, round(ex.width)),
      h: Math.max(1, round(ex.height)), ...styleInputs(ex, null), ...textStyleInputs(label, null),
    };
  },

  // Moved or resized (w/h become the server's minimum), restyled; the label is the adapter's.
  diff(ex, snapshot) {
    const styled = styleInputs(ex, snapshot);
    return { move: boxMove(ex, snapshot), ops: Object.keys(styled).length ? [{ op: "restyle", ...styled }] : [] };
  },
};
