// Hit testing (canvas-v2-phase1.md 1.2 hit/handles/connect, 6.2): over the render fixtures, and
// over CORE's house and arrow-labels goldens once they exist, at three scales.
import fs from "node:fs";
import path from "node:path";
import { describe, expect, test } from "vitest";
import { createIndex } from "./cull.js";
import { boxOf, connectorAt, connectorPoints, handleAt, handlePoints, hitTest, queryRect } from "./hit.js";

const load = (file) => JSON.parse(fs.readFileSync(file, "utf8"));
const sample = load(path.resolve(import.meta.dirname, "__fixtures__/sample.json"));
const sink = load(path.resolve(import.meta.dirname, "__fixtures__/kitchen-sink.json"));
const GOLDENS = path.resolve(import.meta.dirname, "../../../../tests/fixtures/display");
const SCALES = [0.5, 1, 2.5];

describe("hitTest on the sample", () => {
  const index = createIndex(sample);
  for (const scale of SCALES) {
    test(`scale ${scale}`, () => {
      expect(hitTest(index, [180, 200], scale)).toBe("E-2"); // box over its frame
      expect(hitTest(index, [480, 200], scale)).toBe("E-3");
      expect(hitTest(index, [330, 201], scale)).toBe("E-4"); // arrow shaft
      // Inside the frame but on no child: the frame does not take the click (1.2 frame hit).
      // (The rim is 8 px on screen: 16 units at scale 0.5, so stay 20 units clear of the bottom.)
      expect(hitTest(index, [340, 238], scale)).toBeNull();
      // Its title band and its rim do.
      expect(hitTest(index, [300, 110], scale)).toBe("E-1");
      expect(hitTest(index, [101, 200], scale)).toBe("E-1");
      expect(hitTest(index, [5000, 5000], scale)).toBeNull();
    });
  }

  test("line tolerance is in screen pixels", () => {
    // 5 units off the shaft: 5 px on screen at scale 1 (inside tol_px 6), 10 px at scale 2 (outside).
    expect(hitTest(index, [330, 205], 1)).toBe("E-4");
    expect(hitTest(index, [330, 205], 2)).toBeNull();
  });

  test("an arrow's label pill is above every mark, as drawn", () => {
    const dl = structuredClone(sample);
    const arrow = dl.entries.find((e) => e.id === "E-4");
    arrow.hit.box = [308, 188, 64, 24];
    // A later box under the pill: the pill still takes the click, the box takes the rest.
    dl.entries.push({ id: "E-9", kind: "box", layer: "marks", z: 9, v: 1, bbox: [300, 180, 380, 260], hit: { shape: "rect", box: [300, 180, 80, 80] }, items: [] });
    const index = createIndex(dl);
    expect(hitTest(index, [340, 192], 1)).toBe("E-4");
    expect(hitTest(index, [340, 250], 1)).toBe("E-9");
  });

  test("skip passes a click through", () => {
    expect(hitTest(index, [200, 200], 1, { skip: ["E-2"] })).toBeNull();
  });

  test("the title above a frame is part of the frame when zoomed out", () => {
    // At scale 0.5 the title is 24 units tall, above y = 100.
    expect(hitTest(index, [110, 90], 0.5)).toBe("E-1");
    expect(hitTest(index, [110, 90], 1)).toBeNull();
  });
});

describe("hit shapes in the kitchen sink", () => {
  const index = createIndex(sink);
  test("ellipse and diamond exclude their corners", () => {
    expect(hitTest(index, [160, 140], 1)).toBe("E-2");
    // The ellipse's box corner is outside the ellipse, and inside its frame away from band and rim.
    expect(hitTest(index, [83, 102], 1)).toBeNull();
    expect(hitTest(index, [380, 150], 1)).toBe("E-3");
    expect(hitTest(index, [305, 105], 1)).toBeNull(); // the diamond's corner
  });
  test("pins, and entries with hit none never hit", () => {
    expect(hitTest(index, [203, 103], 1)).toBe("C-1");
    expect(hitTest(index, [880, 390], 1)).toBeNull(); // the lock
  });
  test("a claim past its until never hits", () => {
    const dl = structuredClone(sink);
    const claim = dl.entries.find((e) => e.id === "K-1");
    claim.hit = { shape: "rect", box: [780, 200, 200, 120] };
    claim.until = "2999-01-01T00:00:00Z";
    expect(hitTest(createIndex(dl), [880, 260], 1)).toBe("K-1");
    claim.until = "2001-01-01T00:00:00Z";
    expect(hitTest(createIndex(dl), [880, 260], 1)).toBeNull();
  });
});

describe("queryRect", () => {
  const index = createIndex(sample);
  test("contain mode needs the whole box inside; overlays never count", () => {
    expect(queryRect(index, [110, 150, 580, 250])).toEqual(["E-2", "E-3", "E-4"]);
    expect(queryRect(index, [90, 90, 600, 280])).toEqual(["E-1", "E-2", "E-3", "E-4"]);
    expect(queryRect(index, [580, 250, 110, 150])).toEqual(["E-2", "E-3", "E-4"]); // corners in any order
  });
  test("intersect mode takes anything touching", () => {
    expect(queryRect(index, [150, 150, 160, 170], { mode: "intersect" })).toEqual(["E-1", "E-2"]);
  });
  test("overlays are left out", () => {
    const ids = queryRect(createIndex(sink), [-1000, -1000, 5000, 5000]);
    expect(ids.some((id) => /^(K|X|C)-/.test(id))).toBe(false);
  });
});

describe("handles", () => {
  const index = createIndex(sample);
  test("eight for box, two ends for an arrow", () => {
    expect(handlePoints(index.byId.get("E-2")).map((h) => h.handle)).toEqual(["nw", "n", "ne", "e", "se", "s", "sw", "w"]);
    expect(handlePoints(index.byId.get("E-4"))).toEqual([
      { handle: "start", point: [284, 200] },
      { handle: "end", point: [396, 200] },
    ]);
    expect(handlePoints({ handles: "width", hit: { shape: "rect", box: [0, 0, 100, 20] } }).map((h) => h.handle)).toEqual(["w", "e"]);
    expect(handlePoints({ ...index.byId.get("E-2"), locked: true })).toEqual([]);
  });
  for (const scale of SCALES) {
    test(`handleAt at scale ${scale}`, () => {
      expect(handleAt(index, ["E-2"], [280 + 3 / scale, 240], scale)).toEqual({ id: "E-2", handle: "se" });
      expect(handleAt(index, ["E-2"], [280 + 20 / scale, 240], scale)).toBeNull();
      expect(handleAt(index, ["E-4"], [396, 201], scale)).toEqual({ id: "E-4", handle: "end" });
      expect(handleAt(index, ["E-2", "E-3"], [280, 240], scale)).toBeNull(); // handles for one entry only
    });
  }
});

describe("connectors", () => {
  const index = createIndex(sample);
  test("side midpoints of connectable entries only", () => {
    expect(connectorPoints(index.byId.get("E-2")).map((c) => c.side)).toEqual(["n", "e", "s", "w"]);
    expect(connectorPoints(index.byId.get("E-1"))).toEqual([]);
    expect(connectorPoints(index.byId.get("E-4"))).toEqual([]);
  });
  test("a shape small on screen offers no connection point across its short side (QA 1 finding 2)", () => {
    const e2 = index.byId.get("E-2"); // 160 x 80
    expect(connectorPoints(e2, 1).map((c) => c.side)).toEqual(["n", "e", "s", "w"]);
    expect(connectorPoints(e2, 0.2).map((c) => c.side)).toEqual(["e", "w"]); // 32 x 16 px
    expect(connectorPoints(e2, 0.1)).toEqual([]); // 16 x 8 px
    for (const scale of [0.1, 0.134, 0.18]) {
      // A press on the middle of the shape selects or moves it, never starts an arrow.
      expect(hitTest(index, [180, 200], scale)).toBe("E-2");
      expect(connectorAt(index, [200, 200], scale, { hover: "E-2" })).toBeNull();
      expect(connectorAt(index, [180, 205], scale, { hover: "E-2" })).toBeNull();
    }
  });
  for (const scale of SCALES) {
    test(`connectorAt at scale ${scale}`, () => {
      expect(connectorAt(index, [280 + 4 / scale, 200], scale)).toEqual({ id: "E-2", side: "e", point: [280, 200] });
      expect(connectorAt(index, [200, 160 - 2 / scale], scale, { hover: "E-2" })).toEqual({ id: "E-2", side: "n", point: [200, 160] });
      expect(connectorAt(index, [200, 200], scale)).toBeNull();
      // E-3's west point sits under the end of the arrow E-4 (bound there): it is still found.
      expect(connectorAt(index, [400 - 3 / scale, 200], scale)).toEqual({ id: "E-3", side: "w", point: [400, 200] });
    });
  }
});

describe("CORE goldens (house, arrow-labels)", () => {
  for (const scene of ["house", "arrow-labels"]) {
    const file = path.join(GOLDENS, `${scene}.json`);
    test.runIf(fs.existsSync(file))(`${scene}: every entry's hit box centre hits it or something above it`, () => {
      const dl = load(file);
      const index = createIndex(dl);
      const order = new Map(index.entries.map((e, i) => [e.id, i]));
      for (const scale of SCALES) {
        for (const entry of index.entries) {
          const shape = entry.hit?.shape;
          if (!["rect", "ellipse", "diamond"].includes(shape)) continue;
          const [x, y, w, h] = boxOf(entry);
          const got = hitTest(index, [x + w / 2, y + h / 2], scale);
          expect(got, `${scene} ${entry.id} at ${scale}`).not.toBeNull();
          expect(order.get(got), `${scene} ${entry.id} at ${scale}`).toBeGreaterThanOrEqual(order.get(entry.id));
        }
        // Handles of each box-handled entry are found where handlePoints puts them.
        for (const entry of index.entries.filter((e) => e.handles === "box" && !e.locked)) {
          for (const { handle, point } of handlePoints(entry)) {
            expect(handleAt(index, [entry.id], point, scale)?.handle).toBe(handle);
          }
        }
      }
    });
  }
});
