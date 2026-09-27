// verifyText (canvas-v2-phase1.md 3.3, 4.2) with a stubbed 2D context: the lines the browser draws
// wider than the server's w + 0.5, measured in the page's own faces.
import fs from "node:fs";
import path from "node:path";
import { describe, expect, test } from "vitest";
import { verifyText } from "./measure.js";

const sample = JSON.parse(fs.readFileSync(path.resolve(import.meta.dirname, "__fixtures__/sample.json"), "utf8"));
const sink = JSON.parse(fs.readFileSync(path.resolve(import.meta.dirname, "__fixtures__/kitchen-sink.json"), "utf8"));

// A context whose width is `per(font)` per character, recording every font it was asked for.
function stubContext(per) {
  const fonts = [];
  return {
    fonts,
    font: "",
    measureText(t) {
      fonts.push(this.font);
      return { width: per(this.font, t) * [...t].length };
    },
  };
}

const ready = Promise.resolve();

describe("verifyText", () => {
  test("reports only lines wider than w + 0.5, with the browser's width", async () => {
    // "Checkout API" (12 chars, server 121.8): 10.2 per char = 122.4 > 122.3.
    // "Orders DB" (9 chars, server 98.4): 10.2 per char = 91.8, fine.
    const context = stubContext(() => 10.2);
    const out = await verifyText(null, sample, { context, fontsReady: ready });
    expect(out).toContainEqual({ id: "E-2", font: "sans", weight: 500, size: 20, t: "Checkout API", w: 122.4 });
    expect(out.find((l) => l.t === "Orders DB")).toBeUndefined();
  });

  test("the slack is exactly half a unit", async () => {
    const dl = structuredClone(sample);
    dl.entries = [dl.entries[1]];
    dl.entries[0].items[1].lines[0].w = 120;
    // 12 chars at 10.04 = 120.48 (not wider); at 10.05 = 120.6 (wider).
    expect(await verifyText(null, dl, { context: stubContext(() => 10.04), fontsReady: ready })).toEqual([]);
    expect(await verifyText(null, dl, { context: stubContext(() => 10.05), fontsReady: ready })).toHaveLength(1);
  });

  test("measures in the page faces, by weight and size", async () => {
    const context = stubContext(() => 1);
    await verifyText(null, sink, { context, fontsReady: ready });
    // The page faces, then the script faces resvg falls back to (QA 1 finding 8).
    expect(context.fonts).toContain('500 20px "Synapse Sans", "Arial Hebrew", "Geeza Pro"');
    expect(context.fonts).toContain('400 16px "Synapse Mono", "Arial Hebrew", "Geeza Pro"');
    expect(context.fonts).toContain('600 16px "Synapse Sans", "Arial Hebrew", "Geeza Pro"');
  });

  test("skips screen-anchored text, slot fallbacks and blank lines; measures clipped groups", async () => {
    const context = stubContext(() => 100);
    const out = await verifyText(null, sink, { context, fontsReady: ready });
    const texts = out.map((l) => l.t);
    expect(texts).not.toContain("1"); // the comment pin number
    expect(texts).not.toContain("K-1 beta: tidy"); // the claim label
    expect(texts).not.toContain("chart"); // a slot's placeholder
    expect(texts).not.toContain("");
    expect(texts).toContain("X-1 locked: hands off"); // a clip group is world text
  });

  test("only entries drawn under the root are measured, each line once", async () => {
    const root = { querySelectorAll: () => [{ getAttribute: () => "E-3" }, { getAttribute: () => "E-3" }] };
    const out = await verifyText(root, sample, { context: stubContext(() => 50), fontsReady: ready });
    expect(out.map((l) => l.id)).toEqual(["E-3"]);
  });

  test("waits for the fonts", async () => {
    let release;
    const fontsReady = new Promise((r) => {
      release = r;
    });
    let done = false;
    const pending = verifyText(null, sample, { context: stubContext(() => 50), fontsReady }).then(() => {
      done = true;
    });
    await Promise.resolve();
    expect(done).toBe(false);
    release();
    await pending;
    expect(done).toBe(true);
  });

  test("no list or no context: nothing", async () => {
    expect(await verifyText(null, null, { context: stubContext(() => 1), fontsReady: ready })).toEqual([]);
  });
});
