// synapseExpr (canvas-v2-phase3-4.md 3.11, 8.3): claygl's compositor sizes without `new Function`.
// Every expression echarts-gl ships evaluates as JavaScript would; a fuzz over the safe grammar
// agrees with JavaScript's own evaluation (run here in node, never on the page); anything outside
// the grammar throws.
import fs from "node:fs";
import path from "node:path";
import { describe, expect, test } from "vitest";
import { synapseExpr } from "./expr.js";

const js = (source) => new Function("width", "height", "dpr", `return ${source}`); // eslint-disable-line no-new-func

function shipped() {
  const root = path.resolve(import.meta.dirname, "../../../node_modules/echarts-gl/lib");
  const found = new Set();
  const walk = (dir) => {
    for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
      const full = path.join(dir, entry.name);
      if (entry.isDirectory()) walk(full);
      else if (entry.name.endsWith(".js")) {
        for (const m of fs.readFileSync(full, "utf8").matchAll(/expr\(([^"']*)\)/g)) found.add(m[1]);
      }
    }
  };
  if (fs.existsSync(root)) walk(root);
  return [...found];
}

describe("synapseExpr", () => {
  test("every size expression echarts-gl ships agrees with JavaScript", () => {
    const list = shipped();
    expect(list.length).toBeGreaterThan(10);
    for (const source of list) {
      for (const [w, h, d] of [[640, 400, 2], [1, 1, 1], [333, 17, 1.5]]) {
        expect(synapseExpr(source)(w, h, d), source).toEqual(js(source)(w, h, d));
      }
    }
  });

  test("the grammar: numbers, names, precedence, unary, parentheses and arrays", () => {
    expect(synapseExpr("1 + 2 * 3")(0, 0, 0)).toBe(7);
    expect(synapseExpr("(1 + 2) * 3")(0, 0, 0)).toBe(9);
    expect(synapseExpr("-width / 2")(10, 0, 0)).toBe(-5);
    expect(synapseExpr("- -height")(0, 4, 0)).toBe(4);
    expect(synapseExpr("[width * dpr, height * dpr]")(3, 4, 2)).toEqual([6, 8]);
    expect(synapseExpr(".5e1 + 1.")(0, 0, 0)).toBe(6);
    expect(synapseExpr("width")(1, 1)).toBe(1); // claygl's trial call passes no dpr
  });

  test("anything outside the grammar throws while compiling", () => {
    const hostile = [
      "alert(1)",
      "width; fetch('/x')",
      "this",
      "window",
      "constructor",
      "width.constructor",
      "`x`",
      "'a'",
      "width = 1",
      "width ** 2",
      "width % 2",
      "(width",
      "[width,",
      "",
      "width height",
      "1 +",
      "x".repeat(300),
      "(".repeat(40) + "1" + ")".repeat(40),
    ];
    for (const source of hostile) expect(() => synapseExpr(source), source).toThrow();
  });

  test("a fuzz over the safe grammar agrees with JavaScript", () => {
    let seed = 12345;
    const rand = () => {
      seed = (seed * 1103515245 + 12345) % 2147483648;
      return seed / 2147483648;
    };
    const pick = (list) => list[Math.floor(rand() * list.length)];
    const gen = (depth) => {
      const r = rand();
      if (depth > 4 || r < 0.3) return pick(["width", "height", "dpr", String(Math.floor(rand() * 100)), (rand() * 10).toFixed(2)]);
      // (unary minus over a parenthesis: JavaScript reads "--x" as a decrement, not two signs)
      if (r < 0.4) return `-(${gen(depth + 1)})`;
      if (r < 0.55) return `(${gen(depth + 1)})`;
      return `${gen(depth + 1)} ${pick(["+", "-", "*", "/"])} ${gen(depth + 1)}`;
    };
    for (let i = 0; i < 500; i += 1) {
      const source = rand() < 0.2 ? `[${gen(0)}, ${gen(0)}]` : gen(0);
      const args = [Math.floor(rand() * 2000) + 1, Math.floor(rand() * 2000) + 1, pick([1, 1.5, 2])];
      expect(synapseExpr(source)(...args), source).toEqual(js(source)(...args));
    }
  });
});
