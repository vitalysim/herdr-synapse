// Shared helpers for the scene primitives (one module each in this folder). A primitive's build
// returns an Object3D in its local frame: centred on x and z, base at y = 0, unrotated, at its
// native size (the Python primitive's `extent`); scene.js places and rotates it.
import { EdgesGeometry, LineSegments, Mesh } from "three";

export const num = (v, d) => (Number.isFinite(Number(v)) && Number(v) > 0 ? Number(v) : d);

/** [a, b, c] positive numbers from a list, or the defaults. */
export function sizeOf(value, defaults) {
  const list = Array.isArray(value) ? value : [];
  return defaults.map((d, i) => num(list[i], d));
}

/** A mesh with the object's surface material, plus crisp edges when `edges` is set. */
export function solid(geometry, obj, ctx, { edges = false, threshold = 30 } = {}) {
  const mesh = new Mesh(geometry, ctx.materials.surface(obj));
  mesh.name = `${obj?.id ?? "object"}:surface`;
  if (!edges) return mesh;
  const lines = new LineSegments(new EdgesGeometry(geometry, threshold), ctx.materials.edge(obj));
  lines.name = `${obj?.id ?? "object"}:edges`;
  lines.raycast = () => {};
  mesh.add(lines);
  return mesh;
}
