// sanitize.js: the page's copy of the raw ECharts sanitiser (canvas-v2-phase3-4.md 2.8), held to
// _sanitize.py through the Python side's tests/fixtures/charts/sanitize-vectors.json, and through
// this folder's snapshot of _sanitize.vectors() until that file exists.
import fs from "node:fs";
import path from "node:path";
import { describe, expect, test } from "vitest";
import { ALLOW, ALLOW_FROM_FILE, DEFAULT_ALLOW, GL_SERIES, needsGL, normalizeAllow, sanitize } from "./sanitize.js";
import { BUNDLED_SERIES } from "./components.js";
import { sanitizeVectors } from "./__fixtures__/fixtures.js";

const { local, python } = sanitizeVectors();
const ALLOW_FILE = path.resolve(import.meta.dirname, "../../../../assets/canvas/echarts-allow.json");

// A vector: {name, input, output, stripped, refused: null | a refusal code}.
function check(v) {
  const input = JSON.parse(JSON.stringify(v.input));
  const got = sanitize(input);
  const refused = v.refused !== null && v.refused !== undefined && v.refused !== false;
  expect(got.refused !== null, `${v.name}: ${got.refused}`).toBe(refused);
  expect(input, `${v.name}: the input is unchanged`).toEqual(v.input);
  if (refused) {
    expect(got.option).toBe(null);
    return;
  }
  expect(got.option).toEqual(v.output);
  expect(got.stripped).toEqual(v.stripped);
}

describe("the snapshot of _sanitize.vectors()", () => {
  test("is not empty", () => expect(local.length).toBeGreaterThan(20));
  test.each(local.map((v) => [v.name, v]))("%s", (_name, v) => check(v));
});

describe.skipIf(!python)("the Python side's vectors (tests/fixtures/charts/sanitize-vectors.json)", () => {
  test.each((python || []).map((v, i) => [v.name || String(i), v]))("%s", (_name, v) => check(v));
});

describe("the allow table", () => {
  test("is the generated file when it exists", () => {
    if (fs.existsSync(ALLOW_FILE)) {
      const file = JSON.parse(fs.readFileSync(ALLOW_FILE, "utf8"));
      expect(ALLOW_FROM_FILE).toEqual(file);
      expect(ALLOW).toEqual(normalizeAllow(file));
      // the fallback copy stays the Python table, so a checkout without the file behaves the same
      for (const key of ["top", "series", "deny_keys", "deny_values", "limits"]) expect(DEFAULT_ALLOW[key], key).toEqual(file[key]);
    } else {
      expect(ALLOW).toEqual(normalizeAllow(DEFAULT_ALLOW));
    }
  });
  test("allows only series the page draws (the SVG chunks, or echarts-gl)", () => {
    for (const type of ALLOW.series) expect([...BUNDLED_SERIES, ...GL_SERIES], type).toContain(type);
  });
  test("never allows a key that carries HTML or images", () => {
    for (const key of ["title", "toolbox", "graphic", "backgroundColor", "brush", "timeline", "geo"]) expect(ALLOW.top).not.toContain(key);
  });
});

describe("behaviour", () => {
  test("a single series object becomes a list, and is checked", () => {
    expect(sanitize({ series: { type: "map" } }).refused).toMatch(/series\[0\] is a "map" series/);
    expect(sanitize({ series: { type: "bar" } }).option.series).toEqual([{ type: "bar" }]);
  });
  test("no series, or an empty list, is refused", () => {
    expect(sanitize({}).refused).toMatch(/no series/);
    expect(sanitize({ series: [] }).refused).toMatch(/list of series/);
  });
  test("the refusal names the path", () => {
    expect(sanitize({ series: [{ type: "bar", data: [{ name: "https://x" }] }] }).refused).toMatch(/^echarts\.series\[0\]\.data\[0\]\.name loads/);
  });
  test("javascript: anywhere in a string is refused", () => {
    expect(sanitize({ series: [{ type: "bar", name: "x javascript:alert(1)" }] }).refused).toMatch(/loads something/);
  });
  test("tooltips are always rich text and confined", () => {
    expect(sanitize({ series: [{ type: "bar" }] }).option.tooltip).toEqual({ renderMode: "richText", confine: true });
    expect(sanitize({ tooltip: [{ renderMode: "html" }], series: [{ type: "bar" }] }).option.tooltip).toEqual({ renderMode: "richText", confine: true });
  });
  test("a link's window target goes, a sankey node target stays", () => {
    const { option, stripped } = sanitize({
      series: [{ type: "sankey", links: [{ source: "a", target: "b" }, { source: "a", target: "_blank" }], data: [{ name: "a", link: "x", target: "b" }] }],
    });
    expect(option.series[0].links).toEqual([{ source: "a", target: "b" }, { source: "a" }]);
    expect(option.series[0].data).toEqual([{ name: "a" }]);
    expect(stripped).toEqual(["series[0].links[1].target", "series[0].data[0].link", "series[0].data[0].target"]);
  });
  test("strings count characters, not UTF-16 units", () => {
    expect(sanitize({ series: [{ type: "bar", name: "😀".repeat(1000) }] }).refused).toBe(null);
    expect(sanitize({ series: [{ type: "bar", name: "😀".repeat(1001) }] }).refused).toMatch(/1001 characters/);
  });
  test("GL series are recognised", () => {
    expect(needsGL(sanitize({ series: [{ type: "bar3D", data: [[0, 0, 1]] }] }).option)).toBe(true);
    expect(needsGL(sanitize({ series: [{ type: "bar", data: [1] }] }).option)).toBe(false);
    expect(needsGL(null)).toBe(false);
  });
});
