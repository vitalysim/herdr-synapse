// The scene primitives (canvas-v2-phase3-4.md 3.2, 8.3): built in node (three builds geometry
// without WebGL), each one's bounds equal the Python primitive's extent (tests/fixtures/scene3d/
// extents.json when 3D-PY has written it, else the same extents computed here from the 3.2 table);
// the registry equals manifest.js; one module is enough to add a primitive.
import fs from "node:fs";
import path from "node:path";
import { describe, expect, test } from "vitest";
import { Box3, Vector3 } from "three";
import { createMaterials } from "./materials.js";
import { BUILTIN_PRIMITIVES, primitiveNames, primitiveOf, registerPrimitive } from "./primitives/index.js";
import { LOADERS, PRIMITIVES } from "./manifest.js";

const FIXTURE = path.resolve(import.meta.dirname, "../../../../tests/fixtures/scene3d/extents.json");
const ctx = () => ({ palette: {}, materials: createMaterials({}), assets: new Map() });

// [primitive, params, extent] as 3.2 defines them.
const LOCAL = [
  ["box", { size: [1.2, 0.4, 1.2] }, [1.2, 0.4, 1.2]],
  ["box", {}, [1, 1, 1]],
  ["sphere", { radius: 0.35 }, [0.7, 0.7, 0.7]],
  ["cylinder", { radius: 0.6, height: 1.4 }, [1.2, 1.4, 1.2]],
  ["cone", { radius: 0.35, height: 0.7 }, [0.7, 0.7, 0.7]],
  ["plane", { size: [8, 5] }, [8, 0.02, 5]],
  ["plane", { size: [2, 3], thickness: 0.1 }, [2, 0.1, 3]],
];

function cases() {
  if (!fs.existsSync(FIXTURE)) return LOCAL;
  const doc = JSON.parse(fs.readFileSync(FIXTURE, "utf8"));
  const list = Array.isArray(doc) ? doc : doc.cases || doc.extents || [];
  return list.map((c) => (Array.isArray(c) ? c : [c.shape || c.primitive, c.params || c.object || {}, c.extent]));
}

function boundsOf(obj) {
  const box = new Box3().setFromObject(obj);
  return { size: box.getSize(new Vector3()).toArray(), min: box.min.toArray(), max: box.max.toArray() };
}

describe("primitives", () => {
  test("the registry equals the manifest (data read by postbuild) and has every built-in", () => {
    expect([...primitiveNames()].sort()).toEqual([...PRIMITIVES].sort());
    expect([...BUILTIN_PRIMITIVES].sort()).toEqual([...PRIMITIVES].sort());
    expect(LOADERS).toEqual(["gltf"]);
    expect(primitiveOf("gltf").loader).toBe("gltf");
  });

  test("each solid's bounds equal the Python extent, centred on x and z with its base at y = 0", () => {
    for (const [shape, params, extent] of cases()) {
      const def = primitiveOf(shape);
      if (def.container || def.absolute || shape === "text3d" || shape === "gltf") continue;
      const { size, min, max } = boundsOf(def.build({ id: "o", shape, ...params }, null, ctx()));
      size.forEach((v, i) => expect(v, `${shape} ${JSON.stringify(params)} axis ${i}`).toBeCloseTo(extent[i], 4));
      expect(min[1]).toBeCloseTo(0, 6);
      expect(min[0] + max[0]).toBeCloseTo(0, 6);
      expect(min[2] + max[2]).toBeCloseTo(0, 6);
    }
  });

  test("a plane with no size (or fit) takes the size the server fitted it to", () => {
    const def = primitiveOf("plane");
    for (const size of [undefined, "fit"]) {
      const { size: got } = boundsOf(def.build({ id: "f", shape: "plane", size }, { ext: [7.1, 0.02, 4.5] }, ctx()));
      [7.1, 0.02, 4.5].forEach((v, i) => expect(got[i]).toBeCloseTo(v, 4));
    }
    const { size: given } = boundsOf(def.build({ id: "f", shape: "plane", size: [8, 5] }, { ext: [7.1, 0.02, 4.5] }, ctx()));
    expect(given[0]).toBeCloseTo(8, 4);
  });

  test("text3d takes its width from the solved extent (Python measured it) and its height", () => {
    const def = primitiveOf("text3d");
    const { size, min } = boundsOf(def.build({ id: "t", shape: "text3d", text: "payload 1.2 kg", height: 0.08 }, { ext: [0.6, 0.08, 0.01] }, ctx()));
    expect(size[0]).toBeCloseTo(0.6, 6);
    expect(size[1]).toBeCloseTo(0.08, 6);
    expect(min[1]).toBeCloseTo(0, 6);
  });

  test("an arrow runs between its solved points; a group draws nothing", () => {
    const arrow = primitiveOf("arrow3d").build({ id: "a", shape: "arrow3d" }, { points: [[0, 0, 0], [2, 0, 0]] }, ctx());
    const { min, max } = boundsOf(arrow);
    expect(min[0]).toBeCloseTo(0, 3);
    expect(max[0]).toBeCloseTo(2, 3);
    expect(boundsOf(primitiveOf("group").build({ id: "g", shape: "group" }, null, ctx())).size).toEqual([0, 0, 0]);
  });

  test("a glTF object without its model draws its proxy box at the solved extent", () => {
    const g = primitiveOf("gltf").build({ id: "arm", shape: "gltf", src: "models/arm.glb" }, { ext: [0.4, 0.8, 0.3], asset: "0".repeat(32) + ".glb" }, ctx());
    const proxy = g.getObjectByName("proxy");
    expect(proxy.visible).toBe(true);
    expect(boundsOf(g).size.map((v) => Math.round(v * 1000) / 1000)).toEqual([0.4, 0.8, 0.3]);
  });

  test("an unknown shape draws as a box", () => {
    expect(primitiveOf("teapot").name).toBe("box");
  });

  test("one module adds a primitive (the page half of G8)", async () => {
    const torus = (await import("./primitives/__fixtures__/torus.js")).default;
    const off = registerPrimitive(torus);
    try {
      expect(primitiveNames()).toContain("torus");
      const { size, min } = boundsOf(primitiveOf("torus").build({ id: "t", shape: "torus", radius: 1, tube: 0.25 }, null, ctx()));
      expect(size[0]).toBeCloseTo(2.5, 3);
      expect(size[2]).toBeCloseTo(2.5, 3);
      expect(size[1]).toBeCloseTo(0.5, 3);
      expect(min[1]).toBeCloseTo(0, 6);
    } finally {
      off();
    }
    expect(primitiveNames()).not.toContain("torus");
  });
});
