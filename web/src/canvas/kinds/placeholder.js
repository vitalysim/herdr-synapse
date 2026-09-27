// A kind this page does not know (a newer server's): a locked card with the element's type and
// text in its box, so the element is never dropped and nothing throws. It turns into no operation.
import { toneColors } from "../../theme/tokens.js";
import { FONT_TO_EX, boundText, roundnessOf } from "../elements.js";

export default {
  names: [],
  readonly: true,

  build(el, scene, ctx) {
    const tone = toneColors("neutral", "soft", "frame");
    const card = { ...ctx.base(el), type: "rectangle", strokeColor: tone.stroke, backgroundColor: tone.fill, strokeStyle: "dashed", roughness: 0,
      roundness: roundnessOf("box"), locked: true };
    const body = String(el.text || el.title || "").slice(0, 200);
    const text = boundText({ ...el, text: body ? `${el.type}: ${body}` : String(el.type || "element"), style: { size: 16 }, fit: null }, card);
    text.strokeColor = tone.text;
    text.fontFamily = FONT_TO_EX.normal;
    card.boundElements = [{ type: "text", id: text.id }];
    return [card, text];
  },
};
