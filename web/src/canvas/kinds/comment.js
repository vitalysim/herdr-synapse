// A comment pin: a numbered sticky dot at its point, drawn above everything. Pins are derived
// (locked, never turned into operations); comments are written in the side panel.
import { toneColors } from "../../theme/tokens.js";
import { FONT_TO_EX, derived } from "../elements.js";

export default {
  names: ["comment"],
  layer: "pins",

  build(el, scene, ctx) {
    ctx.pinNumber += 1;
    const tone = el.resolved ? toneColors("neutral", "soft", "note") : toneColors("idea", "soft", "note");
    const [px, py] = Array.isArray(el.point) ? el.point : [el.x, el.y];
    const number = String(el.id || "").replace(/^C-/, "");
    const opacity = el.resolved ? 45 : 100;
    const pin = derived(`pin~${el.id}`, "comment", { type: "ellipse", x: px - 13, y: py - 13, width: 26, height: 26, strokeColor: tone.stroke,
      backgroundColor: tone.fill, opacity, strokeWidth: 2 }, { id: el.id, author: el.author, intent: el.intent ?? null });
    const text = derived(`pin~${el.id}~t`, "comment", { type: "text", x: px - 13, y: py - 13, width: 26, height: 18, strokeColor: tone.text,
      backgroundColor: "transparent", text: number, originalText: number, fontSize: 12, fontFamily: FONT_TO_EX.normal, textAlign: "center",
      verticalAlign: "middle", containerId: pin.id, autoResize: true, opacity }, { id: el.id, author: el.author });
    pin.boundElements = [{ type: "text", id: text.id }];
    return [pin, text];
  },
};
