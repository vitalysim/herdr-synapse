// box: size [w, h, d] (1, 1, 1). Also the proxy for glTF models and unknown shapes.
import { BoxGeometry } from "three";
import { sizeOf, solid } from "./_util.js";

export function boxGeometry(w, h, d) {
  const geo = new BoxGeometry(w, h, d);
  geo.translate(0, h / 2, 0);
  return geo;
}

export default {
  name: "box",
  build(obj, _solved, ctx) {
    const [w, h, d] = sizeOf(obj?.size, [1, 1, 1]);
    return solid(boxGeometry(w, h, d), obj, ctx, { edges: true });
  },
};
