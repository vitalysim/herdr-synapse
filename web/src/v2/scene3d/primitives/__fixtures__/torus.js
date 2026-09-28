// A test-only primitive (canvas-v2-phase3-4.md G8): a torus lying flat, radius (1) to the tube's
// centre, tube (0.25). Registered by primitives.test.js through registerPrimitive, with no other
// file changed, to prove a page primitive is one module.
import { TorusGeometry } from "three";
import { num, solid } from "../_util.js";

export default {
  name: "torus",
  build(obj, _solved, ctx) {
    const r = num(obj?.radius, 1);
    const t = num(obj?.tube, 0.25);
    const geo = new TorusGeometry(r, t, 16, 48);
    geo.rotateX(Math.PI / 2);
    geo.translate(0, t, 0);
    return solid(geo, obj, ctx);
  },
};
