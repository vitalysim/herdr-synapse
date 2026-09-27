import fs from "node:fs";
import path from "node:path";
import { describe, expect, test } from "vitest";
import { fmt, roundHalfEven } from "./fmt.js";

// Computed with the Python reference of canvas-v2-phase1.md 1.7 (floor(abs(v)*100 + 0.5), ...).
const OWN = [[0, "0"], [-0.0, "0"], [1, "1"], [-1, "-1"], [0.5, "0.5"], [0.005, "0.01"], [-0.005, "-0.01"], [0.004, "0"], [-0.004, "0"], [0.015, "0.02"], [1.005, "1"], [2.675, "2.68"], [1.125, "1.13"], [-1.125, "-1.13"], [123.456, "123.46"], [-123.456, "-123.46"], [99.995, "100"], [99.994, "99.99"], [1e-9, "0"], [-1e-9, "0"], [0.1, "0.1"], [0.2, "0.2"], [0.30000000000000004, "0.3"], [1 / 3, "0.33"], [-2 / 3, "-0.67"], [1234567.891, "1234567.89"], [-0.05, "-0.05"], [0.999, "1"], [0.9949, "0.99"], [10, "10"], [100.1, "100.1"], [7.25, "7.25"], [-7.255, "-7.26"], [3.14159, "3.14"], [1e6, "1000000"], [-2.5, "-2.5"], [2.5, "2.5"]];

// CORE's shared vectors (J7). Any of [[v, s]], [{v, s}] / [{value, expected}], {vectors: [...]}.
const SHARED = path.resolve(import.meta.dirname, "../../../../tests/fixtures/display/fmt-vectors.json");
function sharedVectors() {
  if (!fs.existsSync(SHARED)) return null;
  let doc = JSON.parse(fs.readFileSync(SHARED, "utf8"));
  if (doc && !Array.isArray(doc)) doc = doc.vectors || Object.entries(doc).map(([s, v]) => [Number(v), s]);
  return doc.map((row) => (Array.isArray(row) ? row : [row.v ?? row.value ?? row.in, row.s ?? row.expected ?? row.out]));
}

describe("fmt", () => {
  test.each(OWN)("fmt(%s) = %s", (v, s) => {
    expect(fmt(v)).toBe(s);
  });

  test("non-finite prints 0, never -0", () => {
    expect(fmt(NaN)).toBe("0");
    expect(fmt(Infinity)).toBe("0");
    expect(fmt(-0.001)).toBe("0");
  });

  const shared = sharedVectors();
  test.runIf(shared)("tests/fixtures/display/fmt-vectors.json", () => {
    for (const [v, s] of shared) expect(fmt(v), `fmt(${v})`).toBe(s);
  });
});

describe("roundHalfEven (Python round)", () => {
  test.each([
    [0.5, 0],
    [1.5, 2],
    [2.5, 2],
    [438.5, 438],
    [439.5, 440],
    [2.4, 2],
    [2.6, 3],
  ])("%s -> %s", (v, r) => expect(roundHalfEven(v)).toBe(r));
});
