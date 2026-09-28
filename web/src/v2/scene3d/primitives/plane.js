// plane: size [w, d] (4, 4), thickness (0.02). Horizontal; others stand `on` it.
import { num, sizeOf, solid } from "./_util.js";
import { boxGeometry } from "./box.js";

export default {
  name: "plane",
  build(obj, _solved, ctx) {
    const [w, d] = sizeOf(obj?.size, [4, 4]);
    const t = num(obj?.thickness, 0.02);
    return solid(boxGeometry(w, t, d), obj, ctx, { edges: true });
  },
};
