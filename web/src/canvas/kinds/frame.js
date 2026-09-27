// A frame, drawn as a section: Excalidraw's frame clips and owns its children, and a derived,
// locked zone behind it carries the tone's tint, hairline and rounded corners (Excalidraw frames
// take no fill). The frame keeps Excalidraw's own name label above it, which is what a human
// grabs to select, move or rename the frame.
import { TOKENS, toneColors } from "../../theme/tokens.js";
import { ROUND_ADAPTIVE, boxMove, derived, round } from "../elements.js";

export default {
  names: ["frame"],
  creates: ["frame"],

  build(el, scene, ctx) {
    // A toned frame's stored colours (zone tint and hairline); a frame from before tones is neutral.
    const style = el.style || {};
    const toned = toneColors(style.tone || "neutral", style.variant || "soft", "frame");
    const zone = style.tone ? { stroke: style.stroke || toned.stroke, fill: style.fill ?? toned.fill } : toned;
    const frame = { ...ctx.base(el), type: "frame", name: el.text || null, strokeColor: zone.stroke, backgroundColor: "transparent", roughness: 0 };
    ctx.under.push(derived(`zone~${el.id}`, "zone", { type: "rectangle", x: frame.x, y: frame.y, width: frame.width, height: frame.height,
      strokeColor: zone.stroke, backgroundColor: zone.fill, strokeWidth: 1, roundness: { type: ROUND_ADAPTIVE, value: TOKENS.radius.section } },
    { id: el.id, author: el.author }));
    return [frame];
  },

  create(ex) {
    return { op: "frame", title: ex.name || "Frame", region: [round(ex.x), round(ex.y), round(ex.x + ex.width), round(ex.y + ex.height)] };
  },

  diff(ex, snapshot) {
    const ops = (ex.name || "") !== (snapshot.name || "") ? [{ op: "edit", text: ex.name || "" }] : [];
    return { move: boxMove(ex, snapshot), ops };
  },
};
