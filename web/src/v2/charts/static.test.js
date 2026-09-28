// The static render in node (canvas-v2-phase3-4.md 8.3): echarts.init(null, …, {ssr: true}) over
// every option fixture, in both themes, gives an SVG of the slot's size with no NaN geometry, every
// label inside the box, and (for this folder's fixtures, laid out with room) no label collisions.
import { describe, expect, test } from "vitest";
import { fallbackPalette } from "../theme/palette.js";
import { echarts } from "./echartsCore.js";
import "./echartsRaw.js";
import { textBoxes } from "./labels.js";
import { NAN_ATTR, echartsSVG, optionOf } from "./render.js";
import { resolveOption } from "./resolve.js";
import { ensureTheme } from "./theme.js";
import { optionFixtures } from "./__fixtures__/fixtures.js";

const drawable = optionFixtures().filter((f) => !f.element.chart?.gl);
// zrender in node has no canvas to measure text and estimates widths from the font size, while the
// Python frame places labels by Inter's real metrics: a label may overhang its box by a few px here
// that it does not in the browser (the e2e run checks the real widths).
const TOL = 4;

describe.each(drawable.map((f) => [f.name, f]))("fixture %s", (_name, f) => {
  test.each([["light"], ["dark"]])("draws in %s", (theme) => {
    const palette = fallbackPalette(theme);
    const option = optionOf(f.element, f.doc);
    const raw = f.element.engine === "echarts-raw";
    const { svg, labelOverlaps } = echartsSVG(echarts, option, raw ? null : f.doc, { w: f.box.w, h: f.box.h, theme, palette });
    expect(svg.startsWith("<svg")).toBe(true);
    expect(svg).toContain(`width="${Math.round(f.box.w)}"`);
    expect(svg).toContain(`height="${Math.round(f.box.h)}"`);
    expect(NAN_ATTR.test(svg)).toBe(false);
    expect(svg).not.toMatch(/<(?:img|script|foreignObject|iframe)\b/i);
    expect(svg).not.toMatch(/<[a-z][^>]*\son[a-z]+=/i);
    // The raw radar carries a deliberately long hostile label; the flat fixtures are laid out with room.
    if (f.source === "local" && f.element.engine === "echarts") expect(labelOverlaps).toBe(0);
    else expect(Number.isInteger(labelOverlaps)).toBe(true);

    // every label inside the box
    const chart = echarts.init(null, ensureTheme(echarts, theme, palette), { renderer: "svg", ssr: true, width: f.box.w, height: f.box.h });
    try {
      chart.setOption(resolveOption(option, raw ? null : f.doc, palette, theme));
      chart.renderToSVGString();
      const boxes = textBoxes(chart);
      expect(boxes.length).toBeGreaterThan(0);
      for (const { text, pts } of boxes) {
        for (const [x, y] of pts) {
          expect(x, `${text} x`).toBeGreaterThanOrEqual(-TOL);
          expect(x, `${text} x`).toBeLessThanOrEqual(f.box.w + TOL);
          expect(y, `${text} y`).toBeGreaterThanOrEqual(-TOL);
          expect(y, `${text} y`).toBeLessThanOrEqual(f.box.h + TOL);
        }
      }
    } finally {
      chart.dispose();
    }
  });
});

describe("hostile text in a raw option", () => {
  test("a formatter with markup draws as escaped text", () => {
    const f = drawable.find((x) => x.name === "local/raw-radar");
    const { svg } = echartsSVG(echarts, optionOf(f.element, f.doc), null, { w: 616, h: 330, theme: "light", palette: fallbackPalette("light") });
    expect(svg).not.toContain("<img");
    expect(svg).toContain("&lt;img");
  });
});

describe("attribute-bound strings", () => {
  test("quotes and brackets in fonts, colours and symbols never break the SVG", async () => {
    const { JSDOM } = await import("jsdom");
    const { DOMParser: Parser } = new JSDOM("").window;
    const { wellFormed } = await import("./render.js");
    const option = {
      textStyle: { fontFamily: 'Evil" onload="x', fontWeight: 'bold"><x' },
      xAxis: { type: "category", data: ["a", "b"], axisLabel: { color: '#123456" x="' } },
      yAxis: { type: "value" },
      series: [{ type: "line", symbol: 'circle"><script>', data: [1, 2], itemStyle: { color: 'red"/>' }, label: { show: true, formatter: '"<b>"{c}' } }],
    };
    const { svg } = echartsSVG(echarts, option, null, { w: 300, h: 200, theme: "light", palette: fallbackPalette("light") });
    expect(wellFormed(svg, Parser)).toBe(true);
    expect(svg).not.toMatch(/<script|<x\b|<b>/);
    expect(wellFormed('<svg><text style="font-family:"A""></text></svg>', Parser)).toBe(false);
  });
});

// QA phase34 M5: the axis title (the measure and its unit) that Python put in the agent's drawing is
// in the page's picture too, as the y axis's name, and every tick label the frame fixed is drawn.
describe.each(drawable.filter((f) => f.source === "python" && f.element.engine === "echarts" && f.element.chart_frame).map((f) => [f.name, f]))(
  "python fixture %s",
  (_name, f) => {
    test("draws its frame's axis title and ticks", () => {
      const palette = fallbackPalette("light");
      const { texts } = echartsSVG(echarts, optionOf(f.element, f.doc), f.doc, { w: f.box.w, h: f.box.h, theme: "light", palette });
      const axes = Object.values(f.element.chart_frame.axes || {});
      for (const axis of axes) {
        if (axis.title) expect(texts, `${f.name} y title`).toContain(axis.title);
        for (const tick of axis.ticks || []) expect(texts, `${f.name} tick`).toContain(tick.t);
      }
    });
  },
);
