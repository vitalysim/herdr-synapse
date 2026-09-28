// arrow3d: from, to (resolved by the solver to points on the facing AABB surfaces), radius (0.04),
// head (0.15). Built in world coordinates (`absolute`): scene.js does not move it. Links compile to
// these too.
import { ConeGeometry, CylinderGeometry, Group, Mesh, Quaternion, Vector3 } from "three";
import { num } from "./_util.js";

const UP = new Vector3(0, 1, 0);

/** An arrow from a to b ([x, y, z]) with a shaft of `radius` and a head `head` long, or null. */
export function arrowBetween(a, b, material, { radius = 0.04, head = 0.15 } = {}) {
  const from = new Vector3(...a);
  const to = new Vector3(...b);
  const dir = to.clone().sub(from);
  const length = dir.length();
  if (!(length > 1e-6)) return null;
  dir.normalize();
  const headLen = Math.min(head, length * 0.5);
  const shaftLen = Math.max(1e-4, length - headLen);
  const group = new Group();
  const shaft = new Mesh(new CylinderGeometry(radius, radius, shaftLen, 12), material);
  shaft.position.set(0, shaftLen / 2, 0);
  const tip = new Mesh(new ConeGeometry(Math.max(radius * 2.5, headLen * 0.45), headLen, 16), material);
  tip.position.set(0, shaftLen + headLen / 2, 0);
  group.add(shaft, tip);
  group.position.copy(from);
  group.quaternion.copy(new Quaternion().setFromUnitVectors(UP, dir));
  return group;
}

const point = (v) => (Array.isArray(v) && v.length === 3 && v.every((n) => Number.isFinite(Number(n))) ? v.map(Number) : null);

export default {
  name: "arrow3d",
  absolute: true,
  build(obj, solved, ctx) {
    const pts = Array.isArray(solved?.points) ? solved.points : [point(obj?.from), point(obj?.to)];
    const a = point(pts[0]);
    const b = point(pts[pts.length - 1]);
    const group = new Group();
    group.name = `${obj?.id ?? "arrow"}:arrow3d`;
    if (!a || !b) return group;
    const arrow = arrowBetween(a, b, ctx.materials.surface(obj), { radius: num(obj?.radius, 0.04), head: num(obj?.head, 0.15) });
    if (arrow) group.add(arrow);
    return group;
  },
};
