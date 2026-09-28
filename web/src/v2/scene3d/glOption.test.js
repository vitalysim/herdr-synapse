// The surface grid order echarts-gl needs (glOption.js).
import { describe, expect, test } from "vitest";
import { surfaceGrid } from "./glOption.js";

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
