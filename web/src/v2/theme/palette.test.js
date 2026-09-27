// Paint resolution (canvas-v2-phase1.md 1.3, 1.4, D1): token references looked up in the theme's
// palette, literals kept, hatch wrapped, unknown references drawn as base.ink with one warning.
import fs from "node:fs";
import path from "node:path";
import { describe, expect, test, vi } from "vitest";
import { Defs, THEMES, fallbackPalette, paletteOf, resolvePaint, shadowsOf } from "./palette.js";

const sample = JSON.parse(fs.readFileSync(path.resolve(import.meta.dirname, "../render/__fixtures__/sample.json"), "utf8"));

describe("resolvePaint", () => {
  test("references resolve per theme; the list's own palettes win", () => {
    expect(resolvePaint("tone.info.fill", "light", sample)).toBe("#e8f2fe");
    expect(resolvePaint("tone.info.fill", "dark", sample)).toBe("#122640");
    expect(resolvePaint("base.canvas", "dark", sample)).toBe("#111214");
  });

  test("a palette object may stand for the theme", () => {
    expect(resolvePaint("base.ink", { "base.ink": "#123456" })).toBe("#123456");
  });

  test("null and undefined are no paint", () => {
    expect(resolvePaint(null, "light", sample)).toBeNull();
    expect(resolvePaint(undefined, "dark", sample)).toBeNull();
  });

  test("a literal hex is the same in both themes, lower case", () => {
    expect(resolvePaint("#AABBCC", "light", sample)).toBe("#aabbcc");
    expect(resolvePaint("#AABBCC", "dark", sample)).toBe("#aabbcc");
  });

  test("hatch wraps a reference or a hex", () => {
    expect(resolvePaint({ hatch: "base.ink" }, "dark", sample)).toEqual({ hatch: "#edeef0" });
    expect(resolvePaint({ hatch: "#00FF00" }, "light", sample)).toEqual({ hatch: "#00ff00" });
  });

  test("unknown references and malformed values draw as base.ink and warn once each", () => {
    const warn = vi.spyOn(console, "warn").mockImplementation(() => {});
    expect(resolvePaint("tone.nope.fill", "light", sample)).toBe("#1c2024");
    expect(resolvePaint("tone.nope.fill", "dark", sample)).toBe("#edeef0");
    expect(resolvePaint("#12345", "light", sample)).toBe("#1c2024");
    expect(resolvePaint(42, "light", sample)).toBe("#1c2024");
    expect(resolvePaint({ gradient: 1 }, "light", sample)).toBe("#1c2024");
    const unknown = warn.mock.calls.filter(([m]) => String(m).includes("tone.nope.fill"));
    expect(unknown).toHaveLength(1);
  });
});

describe("palettes", () => {
  test("the bundled fallback has every role for both themes", () => {
    for (const theme of THEMES) {
      const p = fallbackPalette(theme);
      for (const ref of ["base.canvas", "base.surface", "base.ink", "base.ink_muted", "base.line", "base.grid", "base.selection", "tone.neutral.fill", "tone.info.stroke", "tone.idea.sticky", "chip.0.bg", "chip.7.fg", "chip.human.bg"]) {
        expect(p[ref], `${theme} ${ref}`).toMatch(/^#[0-9a-f]{6}$/);
      }
    }
    expect(fallbackPalette("light")["base.canvas"]).not.toBe(fallbackPalette("dark")["base.canvas"]);
  });

  test("a list without palettes falls back to the bundled tokens", () => {
    expect(paletteOf({}, "dark")).toBe(fallbackPalette("dark"));
    expect(paletteOf(null, "sepia")).toBe(fallbackPalette("light"));
    expect(paletteOf(sample, "light")).toBe(sample.palettes.light);
  });

  test("shadows come from the list, else the tokens", () => {
    expect(shadowsOf(sample, "light")).toEqual({});
    expect(Array.isArray(shadowsOf(null, "light")["1"])).toBe(true);
  });
});

describe("Defs", () => {
  test("ids are handed out in order of first use and reused", () => {
    const defs = new Defs({ 1: [{ x: 0, y: 1, blur: 2, color: "#000000", alpha: 0.1 }] });
    expect(defs.empty).toBe(true);
    expect(defs.hatch("#111111")).toBe("synapse-hatch-0");
    expect(defs.hatch("#222222")).toBe("synapse-hatch-1");
    expect(defs.hatch("#111111")).toBe("synapse-hatch-0");
    expect(defs.clip("0 0 10 10")).toBe("synapse-clip-0");
    expect(defs.elevation(1)).toBe("synapse-elev-1");
    expect(defs.elevation(2)).toBeNull(); // no shadow for level 2 in this theme
    expect(defs.elevation(7)).toBeNull();
    expect(defs.empty).toBe(false);
  });
});
