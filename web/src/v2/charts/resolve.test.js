// resolve.js: a stored option made drawable (canvas-v2-phase3-4.md 2.6), over this folder's option
// fixtures and the Python side's (tests/fixtures/charts/options/*.json) when they exist.
import { describe, expect, test } from "vitest";
import { fallbackPalette } from "../theme/palette.js";
import { datasetsOf, resolveOption, themeOfPalette, TOKEN_REF } from "./resolve.js";
import { chartPalette, SANS_FAMILY, MONO_FAMILY } from "./theme.js";
import { optionFixtures } from "./__fixtures__/fixtures.js";

const HEX = /^#[0-9a-f]{6}$/;
const LIGHT = fallbackPalette("light");
const DARK = fallbackPalette("dark");

// Every string in a value, with the key it sits under.
function* strings(value, key = "") {
  if (Array.isArray(value)) for (const v of value) yield* strings(v, key);
  else if (value && typeof value === "object") for (const [k, v] of Object.entries(value)) yield* strings(v, k);
  else if (typeof value === "string") yield [key, value];
}

function* objects(value) {
  if (Array.isArray(value)) for (const v of value) yield* objects(v);
  else if (value && typeof value === "object") {
    yield value;
    for (const v of Object.values(value)) yield* objects(v);
  }
}

describe("chart palettes", () => {
  test.each([["light"], ["dark"]])("%s has every chart ref as a hex", (theme) => {
    const p = chartPalette(fallbackPalette(theme), theme);
    for (const key of ["chart.paper", "chart.ink", "chart.muted", "chart.axis", "chart.gridline", "chart.highlight", "chart.dim"]) expect(p[key]).toMatch(HEX);
    for (let i = 0; i < 10; i += 1) expect(p[`chart.cat.${i}`]).toMatch(HEX);
    for (let i = 0; i < 9; i += 1) {
      expect(p[`chart.seq.${i}`]).toMatch(HEX);
      expect(p[`chart.div.${i}`]).toMatch(HEX);
    }
    expect(p["chart.paper"]).toBe(fallbackPalette(theme)["base.surface"]);
  });
  test.each([["light"], ["dark"]])("%s takes the token file's chart and mat blocks when the palette lacks them", async (theme) => {
    const tokens = (await import("@synapse/tokens")).default;
    const p = chartPalette({ ...fallbackPalette(theme) }, theme);
    const flat = { ...(tokens.chart?.[theme] || {}), ...(tokens.mat?.[theme] || {}) };
    const own = fallbackPalette(theme);
    if (tokens.chart) expect(Object.keys(tokens.chart[theme] || {}).length).toBeGreaterThan(30);
    for (const [ref, hex] of Object.entries(flat)) if (/^(chart|mat)\./.test(ref) && HEX.test(hex) && !HEX.test(String(own[ref] || ""))) expect(p[ref], ref).toBe(hex);
  });
  test("the display list's own chart keys win", () => {
    const own = { ...LIGHT, "chart.cat.0": "#123456" };
    expect(chartPalette(own, "light")["chart.cat.0"]).toBe("#123456");
  });
  test("the theme is read off a palette", () => {
    expect(themeOfPalette(LIGHT)).toBe("light");
    expect(themeOfPalette(DARK)).toBe("dark");
    expect(themeOfPalette(null)).toBe("light");
  });
});

describe("resolveOption", () => {
  const option = {
    color: ["chart.cat.0", "chart.cat.1"],
    textStyle: { fontFamily: "$sans" },
    legend: { textStyle: { fontFamily: "$mono", color: "chart.ink" } },
    tooltip: { renderMode: "html", extraCssText: "x", formatter: "{b}" },
    toolbox: { show: true },
    title: { text: "no" },
    graphic: [{ type: "image" }],
    animation: true,
    yAxis: { axisLabel: { formatter: { $fmt: "compact|$" } } },
    series: [{ type: "pie", data: { $doc: "slices" }, itemStyle: { borderColor: "tone.accent.solid" } }],
  };
  const doc = { refs: { slices: [{ name: "chart.cat.0", value: 3, itemStyle: { color: "chart.cat.2" } }] } };

  test("token refs, fonts and formatters", () => {
    const out = resolveOption(option, doc, LIGHT, "light");
    const p = chartPalette(LIGHT, "light");
    expect(out.color).toEqual([p["chart.cat.0"], p["chart.cat.1"]]);
    expect(out.textStyle.fontFamily).toBe(SANS_FAMILY);
    expect(out.legend.textStyle.fontFamily).toBe(MONO_FAMILY);
    expect(out.legend.textStyle.color).toBe(p["chart.ink"]);
    expect(out.series[0].itemStyle.borderColor).toBe(LIGHT["tone.accent.solid"]);
    expect(out.yAxis.axisLabel.formatter(4200000)).toBe("$4.2M");
  });
  test("$doc data: colour keys resolve, names never do", () => {
    const out = resolveOption(option, doc, LIGHT, "light");
    const p = chartPalette(LIGHT, "light");
    expect(out.series[0].data).toEqual([{ name: "chart.cat.0", value: 3, itemStyle: { color: p["chart.cat.2"] } }]);
    // the doc itself is not changed
    expect(doc.refs.slices[0].itemStyle.color).toBe("chart.cat.2");
  });
  test("the fixed keys", () => {
    const out = resolveOption(option, doc, LIGHT, "light");
    expect(out.animation).toBe(false);
    expect(out.tooltip).toEqual({ renderMode: "richText", confine: true, formatter: "{b}" });
    expect(out.toolbox).toBeUndefined();
    expect(out.title).toBeUndefined();
    expect(out.graphic).toBeUndefined();
  });
  test("dark resolves to the dark palette", () => {
    const light = resolveOption(option, doc, LIGHT, "light");
    const dark = resolveOption(option, doc, DARK);
    expect(dark.color[0]).toBe(chartPalette(DARK, "dark")["chart.cat.0"]);
    expect(dark.color[0]).not.toBe(light.color[0]);
  });
  test("datasets come from the doc when the option names none", () => {
    const d = { datasets: [{ id: "d0", dimensions: ["m", "v"], source: [["2026-01", 1]] }] };
    const out = resolveOption({ series: [{ type: "bar", datasetIndex: 0 }] }, d, LIGHT);
    expect(out.dataset).toEqual([{ id: "d0", dimensions: ["m", "v"], source: [["2026-01", 1]] }]);
    expect(datasetsOf(null)).toEqual([]);
    // data strings that look like refs stay data
    const tricky = { datasets: [{ source: [["chart.cat.0", 1]] }] };
    expect(resolveOption({}, tricky, LIGHT).dataset[0].source[0][0]).toBe("chart.cat.0");
  });
  test("an unknown ref draws as chart.ink", () => {
    const out = resolveOption({ color: ["chart.nope"] }, null, LIGHT, "light");
    expect(out.color[0]).toBe(chartPalette(LIGHT, "light")["chart.ink"]);
  });
  test("pure: the same input gives the same output and the input is untouched", () => {
    const copy = JSON.parse(JSON.stringify(option));
    resolveOption(option, doc, LIGHT, "light");
    expect(JSON.parse(JSON.stringify(option))).toEqual(copy);
  });
});

describe.each(optionFixtures().filter((f) => f.element.option).map((f) => [f.name, f]))("fixture %s", (_name, f) => {
  test.each([["light", LIGHT], ["dark", DARK]])("resolves fully in %s", (theme, palette) => {
    const out = resolveOption(f.element.option, f.doc, palette, theme);
    for (const [key, value] of strings(out)) {
      expect(TOKEN_REF.test(value) && /colou?r$/i.test(key), `${key}: ${value}`).toBe(false);
      expect(value === "$sans" || value === "$mono", `${key}: ${value}`).toBe(false);
    }
    for (const obj of objects(out)) {
      expect(Object.keys(obj)).not.toContain("$doc");
      expect(Object.keys(obj)).not.toContain("$fmt");
    }
    expect(out.animation).toBe(false);
    if (out.tooltip) expect(out.tooltip.renderMode).toBe("richText");
  });
});

describe("names are data", () => {
  test("legend, axis and series names that look like refs stay names", () => {
    const out = resolveOption(
      { legend: { data: ["chart.cat.0", "EMEA"] }, xAxis: { data: ["base.ink"] }, series: [{ type: "bar", name: "tone.info.fill", label: { formatter: "chart.ink" } }] },
      null,
      LIGHT,
      "light",
    );
    expect(out.legend.data).toEqual(["chart.cat.0", "EMEA"]);
    expect(out.xAxis.data).toEqual(["base.ink"]);
    expect(out.series[0].name).toBe("tone.info.fill");
    expect(out.series[0].label.formatter).toBe("chart.ink");
  });
  test("attribute-bound strings lose quotes and brackets", () => {
    const out = resolveOption({ textStyle: { fontFamily: 'A"B<c>' }, series: [{ type: "line", symbol: 'path://M0 0"/>', itemStyle: { color: 'red"' } }] }, null, LIGHT, "light");
    expect(out.textStyle.fontFamily).toBe("A'Bc");
    expect(out.series[0].symbol).toBe("path://M0 0'/");
    expect(out.series[0].itemStyle.color).toBe("red'");
  });
});
