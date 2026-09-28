// Label placement on the page (canvas-v2-phase3-4.md 3.8 point 5, QA phase34 N1): the page places a
// scene's labels as the server projection does, in screen pixels. No two labels overlap, and a
// label never sits on another object when a spot clear of it exists: in a stack, each label is on
// its own box (under it, or beside it on a leader line), never on the box above it.
import fs from "node:fs";
import path from "node:path";
import { describe, expect, test } from "vitest";
import { buildScene, labelOwners } from "./scene.js";
import { LABEL, bottomUnder, labelSpots, layoutLabels, placeLabels, segmentMeets, sizeLabels } from "./labels.js";

const SOLVED_DIR = path.resolve(import.meta.dirname, "../../../../tests/fixtures/scene3d/solved");
const fixture = (name) => JSON.parse(fs.readFileSync(path.join(SOLVED_DIR, `${name}.json`), "utf8")).element;
const meets = (a, b) => a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3];

// A 560 × 360 px slot at the board's zoom 1 (a 640 × 420 card less its title): labels are 12 px.
const VW = 560;
const VH = 360;

function laidOut(element) {
  const built = buildScene(element, new Map(), {});
  const camera = built.fit(VW / VH, VH);
  return { built, camera, found: layoutLabels(built.scene, camera, VH, 12) };
}

// Every solid's screen box, by id, as layoutLabels sees them.
function screenBoxes(built, camera) {
  const out = new Map();
  for (const o of built.scene.userData.plan.objects) {
    const a = o.aabb;
    const r = [Infinity, Infinity, -Infinity, -Infinity];
    for (const x of [a[0], a[3]]) for (const y of [a[1], a[4]]) for (const z of [a[2], a[5]]) {
      const v = camera.position.clone().set(x, y, z).project(camera);
      const sx = ((v.x + 1) / 2) * VW;
      const sy = ((1 - v.y) / 2) * VH;
      r[0] = Math.min(r[0], sx);
      r[1] = Math.min(r[1], sy);
      r[2] = Math.max(r[2], sx);
      r[3] = Math.max(r[3], sy);
    }
    out.set(o.id, { rect: r, solid: o.solid });
  }
  return out;
}

function expectClean(element) {
  const { built, camera, found } = laidOut(element);
  const boxes = screenBoxes(built, camera);
  const own = built.scene.userData.plan.own;
  const byLabel = new Map(built.scene.userData.plan.labels.map((e) => [e.sprite, e]));
  const rects = [];
  for (const f of found) {
    const entry = byLabel.get(f.sprite);
    const name = f.sprite.userData.label;
    expect(f.rect, `${name} is placed`).not.toBeNull();
    // Inside the viewport.
    expect(f.rect[0]).toBeGreaterThanOrEqual(-0.01);
    expect(f.rect[1]).toBeGreaterThanOrEqual(-0.01);
    expect(f.rect[2]).toBeLessThanOrEqual(VW + 0.01);
    expect(f.rect[3]).toBeLessThanOrEqual(VH + 0.01);
    // Never on another label.
    for (const [other, r] of rects) expect(meets(f.rect, r), `${name} on ${other}`).toBe(false);
    rects.push([name, f.rect]);
    if (entry.id == null) continue;
    // Never on an object other than its own (what holds it up, what it holds), leader included.
    for (const [id, { rect: r, solid }] of boxes) {
      if (!solid || own.get(entry.id).has(id)) continue;
      expect(meets(f.rect, r), `${name} sits on ${id}`).toBe(false);
      if (f.leader) expect(segmentMeets(f.leader[0], f.leader[1], r), `${name}'s leader crosses ${id}`).toBe(false);
    }
    // And at its own object: just over or under it, or on a leader line that starts on it.
    const mine = boxes.get(entry.id).rect;
    if (f.leader) {
      const [x, y] = f.leader[0];
      expect(x >= mine[0] - 1 && x <= mine[2] + 1 && y >= mine[1] - 1 && y <= mine[3] + 1, `${name}'s leader starts on it`).toBe(true);
    } else {
      expect(f.rect[0] < mine[2] && mine[0] < f.rect[2], `${name} is over or under its object`).toBe(true);
      const gap = Math.min(Math.abs(mine[1] - f.rect[3]), Math.abs(f.rect[1] - mine[3]));
      expect(gap, `${name} touches its object`).toBeLessThanOrEqual(LABEL.pad + 2 * LABEL.step + 0.5);
    }
  }
  return { built, camera, found, boxes };
}

describe("placeLabels (pure)", () => {
  // Four 100 × 20 px boxes stacked 5 px apart, bottom one first, each labelled: the stack of the
  // rack (QA phase34 N1). A label may not sit on a neighbour, so only the top box keeps its label
  // above; the bottom one goes under, the middle ones beside on leaders.
  const boxes = [0, 1, 2, 3].map((i) => [100, 300 - i * 25 - 20, 200, 300 - i * 25]);
  const items = boxes.map((b, i) => ({
    w: 60,
    h: LABEL.height,
    anchor: [(b[0] + b[2]) / 2, b[1]],
    outline: b,
    corners: [[b[0], b[1]], [b[2], b[1]], [b[2], b[3]], [b[0], b[3]]],
    avoid: boxes.filter((_, j) => j !== i),
    keep: false,
  }));
  const placed = placeLabels(items, [0, 0, 400, 400]);

  test("a 4-box stack: each label on its own box, none on a neighbour or another label", () => {
    placed.forEach((p, i) => {
      expect(p.rect, `label ${i}`).not.toBeNull();
      boxes.forEach((b, j) => {
        if (j !== i) expect(meets(p.rect, b), `label ${i} on box ${j}`).toBe(false);
      });
      placed.forEach((q, j) => {
        if (j !== i) expect(meets(p.rect, q.rect), `label ${i} on label ${j}`).toBe(false);
      });
    });
    expect(placed[0].rect[1]).toBeGreaterThanOrEqual(boxes[0][3], "the bottom box's label is under it");
    expect(placed[0].leader).toBeNull();
    expect(placed[3].rect[3]).toBeLessThanOrEqual(boxes[3][1], "the top box's label is over it");
    for (const i of [1, 2]) {
      expect(placed[i].leader, `label ${i} is on a leader`).not.toBeNull();
      const [x, y] = placed[i].leader[0];
      expect(x >= boxes[i][0] && x <= boxes[i][2] && y >= boxes[i][1] && y <= boxes[i][3]).toBe(true);
    }
  });

  test("with no room clear of other objects a label still avoids other labels; with none at all it is dropped", () => {
    const one = { w: 60, h: LABEL.height, anchor: [50, 50], outline: [20, 50, 80, 70], corners: [], avoid: [[0, 0, 400, 400]], keep: false };
    const [got] = placeLabels([one], [0, 0, 400, 400]);
    expect(got.rect).not.toBeNull();
    // Room for one label only (above the object, inside a 100 × 27 box).
    const crowd = placeLabels([{ ...one, avoid: [] }, { ...one, avoid: [] }], [0, 20, 100, 27]);
    expect(crowd[0].rect).toEqual([20, 31, 80, 46]);
    expect(crowd[1].rect).toBeNull();
    expect(placeLabels([{ ...one, avoid: [] }, { ...one, avoid: [], keep: true }], [0, 20, 100, 27])[1]).toMatchObject({ crowded: true });
  });

  test("the spots are the server's, scaled with the label", () => {
    const spots = labelSpots(60, 15, [100, 100], [0, 0, 400, 400], [70, 100, 130, 140], [], 2);
    expect(spots.length).toBe(5 + 3 + 6);
    expect(spots[0].rect).toEqual([70, 100 - 8 - 15, 130, 92]);
    expect(spots[1].rect[1]).toBe(spots[0].rect[1] - 16);
    expect(spots[5].rect[1]).toBe(140 + 8);
    expect(spots[8].rect[0]).toBe(130 + 32);
    expect(bottomUnder([[0, 0], [10, 10], [20, 0]], 0, 20)).toBe(10);
    expect(bottomUnder([[0, 0], [10, 10]], 30, 40)).toBeNull();
  });
});

describe("scene labels (QA phase34 N1)", () => {
  test("the rack: every label on its own box, gpu-3 (degraded) beside the warning box", () => {
    const element = fixture("rack");
    const { built, found, boxes } = expectClean(element);
    const at = new Map(found.map((f) => [f.sprite.userData.label, f]));
    expect([...at.keys()].sort()).toEqual(["R12", "ToR switch", "gpu-1", "gpu-2", "gpu-3 (degraded)"]);
    const n1 = boxes.get("n1").rect;
    expect(at.get("gpu-1").rect[1]).toBeGreaterThanOrEqual(n1[3] - 0.01, "gpu-1 under the bottom box");
    for (const [label, id] of [["gpu-2", "n2"], ["gpu-3 (degraded)", "n3"]]) {
      const [, y] = at.get(label).leader[0];
      const own = boxes.get(id).rect;
      expect(y > own[1] && y < own[3], `${label}'s leader is level with ${id}`).toBe(true);
    }
    built.dispose();
  });

  test("the topology, hub and robot scenes: no label on another label or on another object", () => {
    for (const name of ["topology", "hub", "robot"]) expectClean(fixture(name)).built.dispose();
  });

  test("labels: all keeps every label, overlaps or not", () => {
    const { built, found } = laidOut(fixture("shapes"));
    expect(found.every((f) => f.rect)).toBe(true);
    built.dispose();
  });

  test("sizeLabels puts each sprite on its spot and draws the leaders", () => {
    const element = fixture("rack");
    const built = buildScene(element, new Map(), {});
    const camera = built.fit(VW / VH, VH);
    sizeLabels(built.scene, camera, VH, 12);
    const plan = built.scene.userData.plan;
    const found = layoutLabels(built.scene, camera, VH, 12);
    for (const f of found) {
      const p = f.sprite.position.clone().project(camera);
      expect(((p.x + 1) / 2) * VW).toBeCloseTo((f.rect[0] + f.rect[2]) / 2, 3);
      expect(((1 - p.y) / 2) * VH).toBeCloseTo((f.rect[1] + f.rect[3]) / 2, 3);
      const line = plan.labels.find((e) => e.sprite === f.sprite).line;
      expect(line.visible).toBe(!!f.leader);
    }
    built.dispose();
  });

  test("a label may cover what holds its object up and what it holds, and nothing else", () => {
    const specs = new Map(Object.entries({ cab: {}, nodes: { inside: "cab" }, n1: { in: "nodes" }, n2: { in: "nodes" }, table: {}, cup: { on: "table" } }));
    const own = labelOwners(specs, [...specs.keys()]);
    expect([...own.get("n1")].sort()).toEqual(["cab", "n1", "nodes"]);
    expect([...own.get("nodes")].sort()).toEqual(["cab", "n1", "n2", "nodes"]);
    expect([...own.get("cup")].sort()).toEqual(["cup", "table"]);
    expect([...own.get("cab")]).toEqual(["cab"]);
  });
});
