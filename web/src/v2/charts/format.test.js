// format.js: the fixed $fmt formatters (canvas-v2-phase3-4.md 2.6), held to _format.py through
// the Python side's tests/fixtures/charts/format-vectors.json, and through this folder's snapshot
// of _format.vectors() until that file exists.
import { describe, expect, test } from "vitest";
import { MINUS, civil, formatNumber, formatterFor, parseFmtId, validUnit } from "./format.js";
import { formatVectors } from "./__fixtures__/fixtures.js";

const { local, python } = formatVectors();

// A vector's fields: {v, fmt, unit, out} (_format.vectors()).
function check(v) {
  expect(formatNumber(v.v, v.fmt, v.unit), `${JSON.stringify(v.v)} ${v.fmt}|${v.unit}`).toBe(v.out);
}

describe("the snapshot of _format.vectors()", () => {
  test("is not empty", () => expect(local.length).toBeGreaterThan(1000));
  test.each(local.map((v, i) => [i, `${JSON.stringify(v.v)} ${v.fmt}|${v.unit}`, v]))("%s: %s", (_i, _name, v) => check(v));
});

describe.skipIf(!python)("the Python side's vectors (tests/fixtures/charts/format-vectors.json)", () => {
  test.each((python || []).map((v, i) => [i, v]))("vector %s", (_i, v) => check(v));
});

describe("formatNumber", () => {
  test("rounds half away from zero, like _format._rounded", () => {
    expect(formatNumber(2.5, "0")).toBe("3");
    expect(formatNumber(-2.5, "0")).toBe(`${MINUS}3`);
    expect(formatNumber(0.125, "0.00")).toBe("0.13");
  });
  test("prints Python's digits past 2^53", () => {
    expect(formatNumber(7e17, "integer")).toBe("700,000,000,000,000,000");
    expect(formatNumber(2 ** 60, "0")).toBe("1,152,921,504,606,846,976");
  });
  test("units: currency prefix, % without a space, others after one", () => {
    expect(formatNumber(4200000, "compact", "$")).toBe("$4.2M");
    expect(formatNumber(-4200000, "compact", "$")).toBe(`${MINUS}$4.2M`);
    expect(formatNumber(12.5, "auto", "%")).toBe("12.5%");
    expect(formatNumber(412, "auto", "ms")).toBe("412 ms");
    expect(formatNumber(0.125, "percent", "ms")).toBe("12.5%");
  });
  test("dates are day numbers", () => {
    expect(formatNumber(20361, "date")).toBe("2025-09-30");
    expect(formatNumber(20361, "month")).toBe("Sep 2025");
    expect(formatNumber(20361, "year")).toBe("2025");
    expect(civil(0)).toEqual([1970, 1, 1]);
    expect(civil(-1)).toEqual([1969, 12, 31]);
  });
  test("anything but a finite number is empty", () => {
    for (const v of [null, undefined, "12", true, NaN, Infinity, {}, []]) expect(formatNumber(v)).toBe("");
  });
});

describe("fmt ids", () => {
  test("parse like _format.parse_fmt_id", () => {
    expect(parseFmtId("compact|$")).toEqual(["compact", "$"]);
    expect(parseFmtId("0.00|%")).toEqual(["0.00", "%"]);
    expect(parseFmtId("percent|")).toEqual(["percent", ""]);
    expect(parseFmtId("alert(1)")).toEqual(["auto", ""]);
    expect(parseFmtId("0.0000|x")).toEqual(["auto", "x"]);
    expect(parseFmtId("auto|a b")).toEqual(["auto", ""]);
    expect(parseFmtId("auto|<img>")).toEqual(["auto", ""]);
  });
  test("valid units", () => {
    expect(validUnit("ms")).toBe(true);
    expect(validUnit("€")).toBe(true);
    expect(validUnit("")).toBe(false);
    expect(validUnit("123456789")).toBe(false);
    expect(validUnit("a|b")).toBe(false);
  });
});

describe("formatterFor", () => {
  test("a fmt id selects a fixed function", () => {
    expect(formatterFor("compact|$")(4200000)).toBe("$4.2M");
    expect(formatterFor("0.0|ms")(12.345)).toBe("12.3 ms");
    expect(formatterFor("nonsense")(1234.5)).toBe("1,234.5");
  });
  test("axis labels pass (value, index)", () => {
    expect(formatterFor("date|")(20361, 3)).toBe("2025-09-30");
  });
  test("label and tooltip params are read for their value", () => {
    expect(formatterFor("compact|")({ value: 1500 })).toBe("1.5K");
    // a dataset row: the encoded value dimension (vertical bars: y; horizontal bars: x)
    expect(formatterFor("compact|")({ value: ["2026-01", 1500, 2500], encode: { x: [0], y: [2] } })).toBe("2.5K");
    expect(formatterFor("compact|")({ value: ["2026-01", 1500, 2500], encode: { y: [0], x: [1] } })).toBe("1.5K");
    // a tuple without encode: its last number (heatmap [x, y, v])
    expect(formatterFor("integer|")({ value: ["a", "b", 7.6] })).toBe("8");
    expect(formatterFor("integer|")({ value: ["a", "b"] })).toBe("");
  });
});
