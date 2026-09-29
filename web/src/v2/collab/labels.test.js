// The presence layer's own labels (QA phase 5 L10): the board's label boxes on screen, and a pill or
// cursor label placed clear of every one of them and of each other, on the collab scene's golden.
import fs from "node:fs";
import path from "node:path";
import { describe, expect, test } from "vitest";
import { haloShapes } from "./Halos.jsx";
import { CURSOR_LABEL, PILL_H, labelBoxes, pillWidth, placeLabel, textLines } from "./labels.js";

const GOLDEN = JSON.parse(fs.readFileSync(path.resolve(import.meta.dirname, "../../../../tests/fixtures/display/collab.json"), "utf8"));
const VIEWPORT = { w: 1400, h: 900 };
const AT_ONE = { x: -40, y: -60, scale: 1 };
const bboxOf = (id) => (GOLDEN.entries.find((e) => e.id === id) || {}).bbox || null;
const chipOf = (name) => ({ bg: "#30a46c", fg: "#ffffff", initials: name.slice(0, 2).toUpperCase() });
const meets = (a, b) => a[0] < b[2] && a[2] > b[0] && a[1] < b[3] && a[3] > b[1];
const at = (x, y, cam = AT_ONE) => [(x - cam.x) * cam.scale, (y - cam.y) * cam.scale];

describe("label boxes", () => {
  const lines = textLines(GOLDEN);

  test("once per display list", () => {
    expect(textLines(GOLDEN)).toBe(lines);
    expect(textLines(null)).toEqual([]);
  });

  test("an element's line is its ink, centred on its anchor, in screen px", () => {
    const boxes = labelBoxes(lines, AT_ONE, VIEWPORT);
    // E-4 "Checkout": middle-anchored at x 820, 94.85 wide, baseline 87.28, size 20.
    const [x0, y0] = at(820 - 94.85 / 2, 87.28 - 0.97 * 20);
    const found = boxes.find((b) => Math.abs(b[0] - x0) < 0.01 && Math.abs(b[1] - y0) < 0.01);
    expect(found).toBeTruthy();
    expect(found[2] - found[0]).toBeCloseTo(94.85, 2);
  });

  test("a screen-anchored label keeps its px size at any zoom; its chip counts too", () => {
    for (const scale of [0.5, 1, 2]) {
      const cam = { x: 0, y: 0, scale };
      const boxes = labelBoxes(lines, cam, VIEWPORT);
      // X-1's label: end-anchored 4 px left of its region's top-right [324, 436], 156.09 px wide.
      const [ax, ay] = at(324, 436, cam);
      const freeze = boxes.find((b) => Math.abs(b[2] - (ax - 4)) < 0.01 && b[3] <= ay);
      expect(freeze, `scale ${scale}`).toBeTruthy();
      expect(freeze[2] - freeze[0]).toBeCloseTo(156.09, 2);
      // P-2's chip: 393.76 px from its anchor [314, 234] (drawn at 0.35 and up).
      const [px, py] = at(314, 234, cam);
      const chip = boxes.find((b) => Math.abs(b[0] - px) < 0.01 && Math.abs(b[1] - (py - 24)) < 0.01);
      expect(chip, `scale ${scale}`).toBeTruthy();
      expect(chip[2] - chip[0]).toBeCloseTo(393.76, 2);
    }
  });

  test("a clipped label (a claim beside another, QA phase 5 L8) is only as wide as its clip", () => {
    const label = { k: "group", screen: [0, 0], items: [{ k: "text", x: 4, anchor: "start", size: 12, lh: 15, lines: [{ t: "K-1 you: a long label", y: 14, w: 200 }] }] };
    const dl = { entries: [{ id: "K-1", items: [{ k: "group", clip: [0, 0, 96, 300], items: [label] }] }, { id: "K-2", items: [label] }] };
    const [clipped, free] = labelBoxes(textLines(dl), { x: 0, y: 0, scale: 0.5 }, VIEWPORT);
    expect(clipped[2]).toBe(48); // 96 world units at half scale
    expect(free[2]).toBe(204);
  });

  test("what a lod hides is no obstacle", () => {
    const near = labelBoxes(lines, { x: 0, y: 0, scale: 0.5 }, VIEWPORT).length;
    const far = labelBoxes(lines, { x: 0, y: 0, scale: 0.1 }, VIEWPORT).length;
    expect(far).toBeLessThan(near);
  });

  test("a growing title's box grows with it; a transformed group's box follows its matrix", () => {
    const dl = {
      entries: [
        { id: "F", items: [{ k: "text", x: 0, anchor: "start", size: 10, lh: 12.5, zoom: { min_px: 20, grow: "up", bottom: 0 }, lines: [{ t: "Title", y: -2, w: 30 }] }] },
        { id: "G", items: [{ k: "group", t: [2, 0, 0, 2, 100, 0], items: [{ k: "text", x: 0, anchor: "start", size: 10, lh: 12.5, lines: [{ t: "x", y: 10, w: 10 }] }] }] },
      ],
    };
    const [title, scaled] = labelBoxes(textLines(dl), { x: 0, y: 0, scale: 1 }, VIEWPORT);
    expect(title[2] - title[0]).toBeCloseTo(60, 5); // 20 px of a 10-unit size: twice as wide
    expect(title[3]).toBeLessThanOrEqual(0.24 * 20 + 1e-9);
    expect(scaled[0]).toBe(100);
    expect(scaled[2] - scaled[0]).toBe(20);
  });
});

describe("placing", () => {
  test("the first clear candidate, pulled into the view first; null when none is clear", () => {
    const obstacles = [[0, 0, 100, 20]];
    expect(placeLabel([[10, 5], [10, 40]], [50, 10], obstacles)).toEqual({ x: 10, y: 40 });
    expect(placeLabel([[-60, 40], [10, 40]], [50, 10], obstacles, VIEWPORT)).toEqual({ x: 0, y: 40 });
    // Pulled in, it would cover a label: as it is, then.
    expect(placeLabel([[-60, 40]], [50, 10], [[0, 30, 5, 60]], VIEWPORT)).toEqual({ x: -60, y: 40 });
    expect(placeLabel([[10, 5], [20, 10]], [50, 10], obstacles)).toBeNull();
  });

  test("a pill under its halo when that is clear (the default)", () => {
    const { halos } = haloShapes({ members: [{ name: "a", status: "drawing", region: [0, 0, 100, 50], ids: [], opacity: 1 }], camera: { x: 0, y: 0, scale: 1 }, viewport: VIEWPORT, bboxOf, chipOf });
    expect(halos[0].pill).toEqual({ x: 0, y: 50 + 6 + 2, compact: false }); // under it, pulled 6 px into the view
  });

  test("a pill goes where no label is, then shrinks to its chip, then is left out", () => {
    const member = { name: "agent", status: "drawing", region: [100, 100, 300, 200], ids: [], intent: "", opacity: 1 };
    const camera = { x: 0, y: 0, scale: 1 };
    const w = pillWidth("agent · drawing");
    const under = [94, 208, 94 + w, 208 + PILL_H];
    let { halos } = haloShapes({ members: [member], camera, viewport: VIEWPORT, labels: [under] });
    expect(halos[0].pill.compact).toBe(false);
    expect(meets([halos[0].pill.x, halos[0].pill.y, halos[0].pill.x + w, halos[0].pill.y + PILL_H], under)).toBe(false);
    // Everything around the halo but the halo's own inside corner is taken: the chip fits there.
    const around = [[0, 0, 1400, 96], [0, 200, 1400, 900], [0, 100, 94, 200], [306, 100, 1400, 200], [124, 100, 306, 200], [100, 130, 124, 200]];
    ({ halos } = haloShapes({ members: [member], camera, viewport: VIEWPORT, labels: around }));
    expect(halos[0].pill).toEqual({ x: 100, y: 100, compact: true });
    ({ halos } = haloShapes({ members: [member], camera, viewport: VIEWPORT, labels: [[-5000, -5000, 5000, 5000]] }));
    expect(halos[0].pill).toBeNull();
  });

  test("the cursor label moves off a label, and is left out when it has no clear place", () => {
    const camera = { x: 0, y: 0, scale: 1 };
    const cursor = [760, 60];
    const text = [760, 60, 900, 90];
    let { operators } = haloShapes({ operators: [{ page: "p", viewport: null, cursor, opacity: 1 }], camera, viewport: VIEWPORT, labels: [text] });
    const box = [operators[0].label.x, operators[0].label.y, operators[0].label.x + CURSOR_LABEL[0], operators[0].label.y + CURSOR_LABEL[1]];
    expect(meets(box, text)).toBe(false);
    ({ operators } = haloShapes({ operators: [{ page: "p", viewport: null, cursor, opacity: 1 }], camera, viewport: VIEWPORT, labels: [[-5000, -5000, 5000, 5000]] }));
    expect(operators[0].cursor).toEqual(cursor);
    expect(operators[0].label).toBeNull();
  });

  test("on the collab board at many cameras, no pill or cursor label covers a label or another pill", () => {
    const members = [
      { name: "qa-drawer", status: "drawing", region: null, ids: ["E-2", "E-3"], intent: "laying out the pricing table", opacity: 1 },
      { name: "qa-peer", status: "waiting", region: null, ids: ["P-2", "E-3"], intent: "needs the numbers", opacity: 1 },
      { name: "qa-third", status: "drawing", region: [1500, 0, 2200, 200], ids: [], intent: "the funnel", opacity: 1 },
    ];
    const operators = [{ page: "p", viewport: null, cursor: [760, 60], opacity: 1 }, { page: "q", viewport: null, cursor: [20, 220], opacity: 1 }];
    const lines = textLines(GOLDEN);
    let placed = 0;
    for (const scale of [0.25, 0.4, 0.6, 1, 1.5, 2.5]) {
      for (const [x, y] of [[-40, -60], [200, 0], [600, -100], [-300, 300]]) {
        const camera = { x, y, scale };
        const labels = labelBoxes(lines, camera, VIEWPORT);
        const shapes = haloShapes({ members, operators: operators.map((o) => ({ ...o })), camera, viewport: VIEWPORT, bboxOf, chipOf, labels });
        const mine = [];
        for (const h of shapes.halos) {
          if (!h.pill) continue;
          const w = h.pill.compact ? 20 : pillWidth(h.label);
          mine.push([h.pill.x, h.pill.y, h.pill.x + w, h.pill.y + PILL_H]);
        }
        for (const o of shapes.operators) {
          if (o.label) mine.push([o.label.x, o.label.y, o.label.x + CURSOR_LABEL[0], o.label.y + CURSOR_LABEL[1]]);
        }
        placed += mine.length;
        mine.forEach((box, i) => {
          for (const label of labels) expect(meets(box, label), `scale ${scale} at ${x},${y}`).toBe(false);
          mine.slice(i + 1).forEach((other) => expect(meets(box, other)).toBe(false));
        });
      }
    }
    expect(placed).toBeGreaterThan(40);
  });

  test("the QA case: her cursor on Checkout puts its label clear of it", () => {
    const camera = { x: 0, y: 0, scale: 1 };
    const labels = labelBoxes(textLines(GOLDEN), camera, VIEWPORT);
    const { operators } = haloShapes({ operators: [{ page: "p", viewport: null, cursor: [760, 60], opacity: 1 }], camera, viewport: VIEWPORT, labels });
    const checkout = labels.find((b) => b[0] > 760 && b[0] < 790 && b[1] > 60 && b[1] < 75);
    expect(checkout).toBeTruthy();
    const { x, y } = operators[0].label;
    expect(meets([x, y, x + CURSOR_LABEL[0], y + CURSOR_LABEL[1]], checkout)).toBe(false);
    expect([x, y]).not.toEqual([768, 66]); // where it sat before (QA phase 5 L10)
  });
});
