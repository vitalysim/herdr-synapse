// cone: radius (0.5), height (1).
import { ConeGeometry } from "three";
import { num, solid } from "./_util.js";

export default {
  name: "cone",
  build(obj, _solved, ctx) {
    const r = num(obj?.radius, 0.5);
    const h = num(obj?.height, 1);
    const geo = new ConeGeometry(r, h, 32);
    geo.translate(0, h / 2, 0);
    return solid(geo, obj, ctx);
  },
};
