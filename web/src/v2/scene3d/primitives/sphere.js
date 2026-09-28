// sphere: radius (0.5).
import { SphereGeometry } from "three";
import { num, solid } from "./_util.js";

export default {
  name: "sphere",
  build(obj, _solved, ctx) {
    const r = num(obj?.radius, 0.5);
    const geo = new SphereGeometry(r, 32, 16);
    geo.translate(0, r, 0);
    return solid(geo, obj, ctx);
  },
};
