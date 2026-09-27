// Pictures: path, svg, mermaid, chart, image, and viz. Each is an Excalidraw image whose file
// renderers.js draws locally (ctx.fileJobs); a live viz is an embeddable frame while live
// visuals are on for the team, and its placeholder picture while they are off.
import { ROUND_ADAPTIVE, boxMove, round } from "../elements.js";
import { fileIdFor } from "../renderers.js";

export default {
  names: ["path", "svg", "mermaid", "chart", "viz", "image"],
  creates: ["image"],

  build(el, scene, ctx) {
    const b = ctx.base(el);
    if (el.type === "viz" && ctx.vizOn) {
      return [{ ...b, type: "embeddable", link: `/viz/${encodeURIComponent(ctx.team)}/${el.id}?v=${el.updated_seq || 0}`,
        backgroundColor: "transparent", strokeWidth: 1, roughness: 0, roundness: { type: ROUND_ADAPTIVE } }];
    }
    const fileId = fileIdFor(el, ctx.vizOn);
    if (!fileId) return [];
    ctx.fileJobs.push({ fileId, element: el });
    return [{ ...b, type: "image", fileId, status: "saved", scale: [1, 1], crop: null, backgroundColor: "transparent", strokeColor: "transparent",
      roughness: 0 }];
  },

  // An image the human added, once the page has uploaded its file (ctx.uploads).
  create(ex, ctx) {
    const asset = ctx.uploads.get(ex.fileId);
    if (!asset) return null;
    return { op: "image", asset, at: [round(ex.x), round(ex.y)], w: Math.max(1, round(ex.width)), h: Math.max(1, round(ex.height)) };
  },

  // Moved or resized only: a picture's look is its file.
  diff(ex, snapshot) {
    return { move: boxMove(ex, snapshot), ops: [] };
  },
};
