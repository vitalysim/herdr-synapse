// buildScene over solved scenes (canvas-v2-phase3-4.md 3.7, 8.3): every solved object is built
// where the server put it (its world box equals the solved AABB), links become arrows, the ground
// grid and lights are there, and the camera frames the bounds. Runs over the Python solved fixtures
// (tests/fixtures/scene3d/solved/*.json, written by 3D-PY) and an inline topology.
import fs from "node:fs";
import path from "node:path";
import { describe, expect, test } from "vitest";
import { Box3, Vector3 } from "three";
import { assetsOf, buildScene, gridStep, objectsOf, readSolved, settingsOf } from "./scene.js";

const SOLVED_DIR = path.resolve(import.meta.dirname, "../../../../tests/fixtures/scene3d/solved");

const TOPO = {
  id: "E-60",
  type: "scene3d",
  settings: { units: "m", camera: "iso", lights: "studio", ground: true, labels: "auto" },
  objects: [
    { id: "base", shape: "plane", size: [8, 5], tone: "neutral" },
    { id: "lb", shape: "box", size: [1.2, 0.4, 1.2], tone: "info", label: "Load balancer", on: "base" },
    { id: "db", shape: "cylinder", radius: 0.6, height: 1.4, tone: "success", label: "Postgres", on: "base" },
    { id: "cache", shape: "sphere", radius: 0.35, tone: "warning", label: "Redis" },
    { id: "turned", shape: "box", size: [2, 0.5, 1], rotate: 90 },
  ],
  solved: {
    bounds: [-4, 0, -2.5, 4, 2.4, 2.5],
    objects: {
      base: { pos: [0, 0, 0], rot: [0, 0, 0], ext: [8, 0.02, 5], aabb: [-4, 0, -2.5, 4, 0.02, 2.5], rel: null, parent: null },
      lb: { pos: [-3, 0.02, 0], rot: [0, 0, 0], ext: [1.2, 0.4, 1.2], aabb: [-3.6, 0.02, -0.6, -2.4, 0.42, 0.6], rel: "on base", parent: null },
      db: { pos: [2, 0.02, 0], rot: [0, 0, 0], ext: [1.2, 1.4, 1.2], aabb: [1.4, 0.02, -0.6, 2.6, 1.42, 0.6], rel: "on base", parent: null },
      cache: { pos: [0, 1.7, 0], rot: [0, 0, 0], ext: [0.7, 0.7, 0.7], aabb: [-0.35, 1.7, -0.35, 0.35, 2.4, 0.35], rel: "above", parent: null },
      turned: { pos: [0, 0.02, 1.5], rot: [0, 90, 0], ext: [1, 0.5, 2], aabb: [-0.5, 0.02, 0.5, 0.5, 0.52, 2.5], rel: null, parent: null },
    },
    links: [{ key: "lb->db", points: [[-2.4, 0.22, 0], [1.4, 0.72, 0]], label: "SQL", tone: "neutral" }],
    notes: [],
    conflicts: [],
  },
  updated_seq: 7,
};

function fixtures() {
  const out = [["inline topology", TOPO]];
  if (!fs.existsSync(SOLVED_DIR)) return out;
  for (const name of fs.readdirSync(SOLVED_DIR).filter((n) => n.endsWith(".json")).sort()) {
    const doc = JSON.parse(fs.readFileSync(path.join(SOLVED_DIR, name), "utf8"));
    out.push([name, doc.element || doc]);
  }
  return out;
}

function worldBox(node) {
  node.updateMatrixWorld(true);
  const box = new Box3();
  node.traverse((n) => {
    if (n.isMesh && n.visible !== false) box.expandByObject(n);
  });
  return box;
}

describe("readSolved", () => {
  test("reads the dict form and the compact list form alike", () => {
    const a = readSolved(TOPO);
    expect([...a.objects.keys()]).toEqual(["base", "lb", "db", "cache", "turned"]);
    expect(a.objects.get("turned").rot).toEqual([0, 90, 0]);
    const compact = {
      solved: {
        bounds: TOPO.solved.bounds,
        objects: [
          ["lb", [-3, 0.02, 0], [0, 0, 0], [1.2, 0.4, 1.2], null, "on base"],
          ["arm", [0, 0.9, 0], [0, 0, 0], [0.4, 0.8, 0.3], "grp", "on bench"],
          ["arr", [0, 0, 0], [0, 0, 0], [0.08, 0.08, 0.08], null, "", { points: [[0, 0, 0], [1, 1, 1]] }],
        ],
        models: { arm: { asset: `${"d".repeat(32)}.glb`, tris: 1200 } },
        links: [["lb->db", "lb", "db", [[0, 0, 0], [1, 0, 0]], "SQL", "info"]],
      },
    };
    const b = readSolved(compact);
    expect(b.objects.get("lb")).toMatchObject({ pos: [-3, 0.02, 0], ext: [1.2, 0.4, 1.2], rel: "on base" });
    b.objects.get("lb").aabb.forEach((v, i) => expect(v).toBeCloseTo([-3.6, 0.02, -0.6, -2.4, 0.42, 0.6][i], 9));
    expect(b.objects.get("arm")).toMatchObject({ parent: "grp", asset: `${"d".repeat(32)}.glb`, tris: 1200 });
    expect(b.objects.get("arr").points).toEqual([[0, 0, 0], [1, 1, 1]]);
    expect(b.links[0]).toMatchObject({ key: "lb->db", label: "SQL", tone: "info" });
    expect(readSolved({}).objects.size).toBe(0);
  });

  test("settings, objects and assets", () => {
    expect(settingsOf(TOPO)).toEqual({ camera: "iso", lights: "studio", ground: true, labels: "auto", units: "m" });
    expect(settingsOf({ camera: "front" }).camera).toBe("front");
    expect([...objectsOf(TOPO).keys()]).toContain("cache");
    expect(assetsOf({ solved: { objects: { m: { pos: [0, 0, 0], asset: `${"c".repeat(32)}.glb` } } } })).toEqual([`${"c".repeat(32)}.glb`]);
  });

  test("grid steps are round", () => {
    expect(gridStep(8.96)).toBe(1);
    expect(gridStep(20)).toBe(2);
    expect(gridStep(0.4)).toBeCloseTo(0.05, 9);
  });
});

describe("buildScene", () => {
  for (const [name, element] of fixtures()) {
    test(`${name}: every solid sits on its solved AABB, and the camera frames every object`, () => {
      const built = buildScene(element, new Map(), {});
      const solved = readSolved(element);
      for (const [id, entry] of solved.objects) {
        const node = built.objects.get(id);
        expect(node, id).toBeTruthy();
        const spec = objectsOf(element).get(id) || {};
        if (!entry.aabb || ["group", "text3d", "arrow3d", "gltf"].includes(spec.shape)) continue;
        const box = worldBox(node);
        if (box.isEmpty()) continue;
        const got = [...box.min.toArray(), ...box.max.toArray()];
        got.forEach((v, i) => expect(Math.abs(v - entry.aabb[i]), `${id} aabb[${i}] ${got} vs ${entry.aabb}`).toBeLessThan(0.02));
      }
      expect(built.links.length).toBe(solved.links.length);
      expect(built.tris).toBeGreaterThan(0);
      expect(built.scene.getObjectByName("ground") !== undefined).toBe(settingsOf(element).ground);
      const c = built.camera;
      // The orthographic views fit the objects' box corners (as the server projections do).
      expect(built.fitPoints.length).toBeGreaterThanOrEqual(8);
      for (const pt of built.fitPoints) {
        const p = new Vector3(...pt).project(c);
        expect(Math.abs(p.x)).toBeLessThanOrEqual(1.0001);
        expect(Math.abs(p.y)).toBeLessThanOrEqual(1.0001);
      }
      built.dispose();
    });
  }

  test("labels: auto shows given labels, none shows none, all adds ids", () => {
    expect(buildScene(TOPO, new Map(), {}).labels.map((s) => s.userData.label)).toEqual(["Load balancer", "Postgres", "Redis", "SQL"]);
    expect(buildScene({ ...TOPO, settings: { ...TOPO.settings, labels: "none" } }, new Map(), {}).labels).toEqual([]);
    expect(buildScene({ ...TOPO, settings: { ...TOPO.settings, labels: "all" } }, new Map(), {}).labels.map((s) => s.userData.label)).toContain("base");
  });

  test("tones colour from the palette", () => {
    const built = buildScene(TOPO, new Map(), { "tone.success.solid": "#30a46c" });
    const db = built.objects.get("db");
    let colour = null;
    db.traverse((n) => {
      if (n.isMesh && !colour) colour = n.material.color.getHexString();
    });
    expect(colour).toBe("30a46c");
  });

  test("a model past the triangle budget draws its proxy", () => {
    const built = buildScene(TOPO, new Map(), {});
    expect(() => built.proxyModels(true)).not.toThrow();
  });
});

describe("materials (QA phase34 L8)", () => {
  test("a tone starts from mat.<tone>.base when the palette has it, else its solid", async () => {
    const { toneColours } = await import("./materials.js");
    const dark = { "tone.neutral.solid": "#c9ccd2", "mat.neutral.base": "#696b70", "mat.neutral.edge": "#8e9094", "tone.info.solid": "#6aa8f5" };
    expect(toneColours(dark, "neutral")).toEqual({ solid: "#696b70", edge: "#8e9094" });
    expect(toneColours(dark, "info").solid).toBe("#6aa8f5");
  });
});
