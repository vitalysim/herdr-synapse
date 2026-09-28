// The surface grid order echarts-gl needs and the corner label it leaves out (glOption.js).
import { describe, expect, test } from "vitest";
import { categoryLabels, cornerLabels, surfaceGrid } from "./glOption.js";

describe("surfaceGrid", () => {
  test("points with x outer become rows of increasing x, one per y, with their dataShape", () => {
    const series = { type: "surface", data: [[1, 16, 0.9], [1, 32, 0.8], [2, 16, 0.7], [2, 32, 0.5], [3, 16, 0.4], [3, 32, 0.2]] };
    const out = surfaceGrid(series);
    expect(out.dataShape).toEqual([2, 3]);
    expect(out.data).toEqual([[1, 16, 0.9], [2, 16, 0.7], [3, 16, 0.4], [1, 32, 0.8], [2, 32, 0.5], [3, 32, 0.2]]);
    expect(series.dataShape).toBeUndefined();
  });

  test("a series with a dataShape, an incomplete grid, or another type is left alone", () => {
    const shaped = { type: "surface", dataShape: [1, 2], data: [[2, 0, 1], [1, 0, 1]] };
    expect(surfaceGrid(shaped)).toBe(shaped);
    const ragged = { type: "surface", data: [[1, 1, 0], [2, 1, 0], [1, 2, 0]] };
    expect(surfaceGrid(ragged)).toBe(ragged);
    const bars = { type: "bar3D", data: [[1, 1, 1]] };
    expect(surfaceGrid(bars)).toBe(bars);
  });
});

describe("cornerLabels (QA phase34 L7)", () => {
  test("the value axis's lowest label is blank; the others go through its formatter", () => {
    const option = { zAxis3D: { type: "value", min: -2, max: 8, axisLabel: { formatter: (v) => `${v} ms` } } };
    const out = cornerLabels(option);
    const f = out.zAxis3D.axisLabel.formatter;
    expect([-2, 0, 2, 8].map((v, i) => f(v, i))).toEqual(["", "0 ms", "2 ms", "8 ms"]);
    expect(option.zAxis3D.axisLabel.formatter(-2)).toBe("-2 ms");
    expect(cornerLabels({ zAxis3D: { min: 0 } }).zAxis3D.axisLabel.formatter(100)).toBe("100");
  });

  test("an axis with no numeric min, a string formatter or hidden labels is left alone", () => {
    for (const option of [{}, { zAxis3D: { max: 3 } }, { zAxis3D: { min: 0, axisLabel: { formatter: "{value}" } } }, { zAxis3D: { min: 0, axisLabel: { show: false } } }]) {
      expect(cornerLabels(option)).toBe(option);
    }
  });
});

describe("categoryLabels (QA phase34 low)", () => {
  test("a thinned category axis shows every k-th label and blanks the rest, the last one included", () => {
    const hours = ["8", "9", "10", "11", "12", "13", "14", "15", "16", "17", "18", "19"];
    const option = { xAxis3D: { type: "category", data: hours, axisLabel: { interval: 1 } }, yAxis3D: { type: "category", data: ["Mon", "Tue"], axisLabel: { interval: 0 } } };
    const out = categoryLabels(option);
    const f = out.xAxis3D.axisLabel.formatter;
    expect(out.xAxis3D.axisLabel.interval).toBe(0);
    expect(hours.map((h, i) => f(h, i)).filter(Boolean)).toEqual(["8", "10", "12", "14", "16", "18"]);
    expect(out.yAxis3D).toBe(option.yAxis3D);
    expect(option.xAxis3D.axisLabel).toEqual({ interval: 1 });
  });

  test("a value axis, an axis with its own formatter or hidden labels is left alone", () => {
    for (const option of [{}, { xAxis3D: { type: "value", axisLabel: { interval: 1 } } }, { xAxis3D: { type: "category", axisLabel: { interval: 1, formatter: "{value}" } } },
      { yAxis3D: { type: "category", axisLabel: { interval: 2, show: false } } }]) {
      expect(categoryLabels(option)).toBe(option);
    }
  });
});
