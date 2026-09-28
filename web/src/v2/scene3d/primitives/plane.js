// plane: size [w, d], thickness (0.02). Horizontal; others stand `on` it. With no size (or "fit") the
// server sizes it to what stands on it (canvas_scene3d._solver._hug): its solved extent says how big.
import { num, sizeOf, solid } from "./_util.js";
import { boxGeometry } from "./box.js";

export default {
  name: "plane",
  build(obj, solved, ctx) {
    const ext = Array.isArray(solved?.ext) ? solved.ext : null;
    const [w, d] = Array.isArray(obj?.size) || !ext ? sizeOf(obj?.size, [4, 4]) : [num(ext[0], 4), num(ext[2], 4)];
    const t = num(obj?.thickness, 0.02);
    return solid(boxGeometry(w, t, d), obj, ctx, { edges: true });
  },
};
