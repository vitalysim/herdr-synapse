// The scene primitives the page draws: one module each in this folder, listed here (explicit
// imports, so the bundle holds exactly these). A primitive is
//   {name, build(obj, solvedEntry, ctx) -> THREE.Object3D, container?, absolute?, loader?}
// and ../manifest.js lists the same names as data (a test holds the two equal; postbuild writes
// them to web/dist/charts.json for the Python registry's test).
import arrow3d from "./arrow3d.js";
import box from "./box.js";
import cone from "./cone.js";
import cylinder from "./cylinder.js";
import gltf from "./gltf.js";
import group from "./group.js";
import plane from "./plane.js";
import sphere from "./sphere.js";
import text3d from "./text3d.js";

const registry = new Map();

/** Adds a primitive (a module's default export); a later one of the same name replaces it. */
export function registerPrimitive(def) {
  if (!def || typeof def.name !== "string" || typeof def.build !== "function") throw new Error("registerPrimitive: {name, build} required");
  registry.set(def.name, def);
  return () => {
    if (registry.get(def.name) === def) registry.delete(def.name);
  };
}

for (const def of [box, sphere, cylinder, cone, plane, text3d, arrow3d, group, gltf]) registerPrimitive(def);

/** The primitive for a shape; an unknown shape draws as a box (its proxy). */
export function primitiveOf(shape) {
  return registry.get(shape) || registry.get("box");
}

export function primitiveNames() {
  return [...registry.keys()];
}

export const BUILTIN_PRIMITIVES = ["box", "sphere", "cylinder", "cone", "plane", "text3d", "arrow3d", "group", "gltf"];
