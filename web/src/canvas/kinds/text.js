// A free text. It wraps at its width when the server does (the author set wrap, or it grew to the
// widest a text grows); otherwise Excalidraw sizes it to its lines.
import { boxMove, colorOf, colorsOf, fontFamilyOf, fontSizeOf, round, textStyleInputs, typedText, wrapsAtWidth } from "../elements.js";

export default {
  names: ["text"],
  creates: ["text"],

  build(el, scene, ctx) {
    const text = String(el.text || "");
    return [
      {
        ...ctx.base(el),
        type: "text",
        strokeColor: colorsOf(el).text,
        text,
        originalText: text,
        fontSize: fontSizeOf(el),
        fontFamily: fontFamilyOf(el),
        textAlign: "left",
        verticalAlign: "top",
        containerId: null,
        autoResize: !wrapsAtWidth(el),
        backgroundColor: "transparent",
      },
    ];
  },

  create(ex) {
    if (!typedText(ex).trim()) return null;
    return { op: "shape", kind: "text", text: typedText(ex), at: [round(ex.x), round(ex.y)], color: colorOf(ex.strokeColor) || undefined,
      opacity: Math.max(10, Math.min(100, round(ex.opacity ?? 100))), ...(ex.autoResize === false ? { w: Math.max(1, round(ex.width)) } : {}),
      ...textStyleInputs(ex, null) };
  },

  diff(ex, snapshot, canon) {
    let move = boxMove(ex, snapshot, { resizable: false });
    if (ex.autoResize === false && Math.abs(ex.width - snapshot.width) >= 1) {
      // A text the human resized wraps at its new width; its height follows on the server.
      move = { ...(move || {}), w: Math.max(1, round(ex.width)) };
    }
    const ops = [];
    if (typedText(ex) !== String(canon.text || "")) ops.push({ op: "edit", text: typedText(ex) });
    const styled = { ...textStyleInputs(ex, snapshot) };
    const color = colorOf(ex.strokeColor);
    if (color && color !== colorOf(snapshot.strokeColor)) styled.color = color;
    if (Object.keys(styled).length) ops.push({ op: "restyle", ...styled });
    return { move, ops };
  },
};
