// cylinder: radius (0.5), height (1), segments (24, the page only).
import { CylinderGeometry } from "three";
import { num, solid } from "./_util.js";

export default {
  name: "cylinder",
  build(obj, _solved, ctx) {
    const r = num(obj?.radius, 0.5);
    const h = num(obj?.height, 1);
    const segments = Math.max(6, Math.min(96, Math.round(num(obj?.segments, 24))));
    const geo = new CylinderGeometry(r, r, h, segments);
    geo.translate(0, h / 2, 0);
    return solid(geo, obj, ctx, { edges: true, threshold: 60 });
  },
};
