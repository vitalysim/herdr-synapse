// toSVGString: the canonical SVG form (canvas-v2-phase1.md 1.8) and parity with the Python writer.
//
// - Parity with CORE's goldens: for every tests/fixtures/display/<scene>.json, toSVGString(dl,
//   {theme}) equals <scene>.<theme>.svg byte for byte (gate 8.1.6). Skipped until C1 lands them.
// - The render fixtures (__fixtures__/<name>.json) have their canonical SVGs next to them
//   (<name>.<theme>.svg), so a change to the JS writer shows as a diff, and CORE can hold
//   canvas_svg.write to the same files before the goldens exist. Regenerate them with
//   SYNAPSE_WRITE_FIXTURES=1 npx vitest run src/v2/render/svgString.test.js
import fs from "node:fs";
import path from "node:path";
import { describe, expect, test } from "vitest";
import { normalizeBox, pixelSize, toSVGString } from "./svgString.js";

const HERE = import.meta.dirname;
const FIXTURES = path.join(HERE, "__fixtures__");
const GOLDENS = path.resolve(HERE, "../../../../tests/fixtures/display");
const WRITE = process.env.SYNAPSE_WRITE_FIXTURES === "1";
const THEMES = ["light", "dark"];

const load = (file) => JSON.parse(fs.readFileSync(file, "utf8"));
// A golden file is the writer's string plus one end-of-file newline (canvas_display --write-goldens
// saves write(...) + "\n"); the writers themselves end with </svg>.
const svgFile = (file) => fs.readFileSync(file, "utf8").replace(/\n$/, "");
const sample = load(path.join(FIXTURES, "sample.json"));
const sink = load(path.join(FIXTURES, "kitchen-sink.json"));

function goldenScenes() {
  if (!fs.existsSync(GOLDENS)) return [];
  return fs
    .readdirSync(GOLDENS)
    .filter((f) => f.endsWith(".json") && f !== "fmt-vectors.json")
    .map((f) => f.slice(0, -5))
    .filter((scene) => THEMES.some((t) => fs.existsSync(path.join(GOLDENS, `${scene}.${t}.svg`))))
    .sort();
}

describe("parity with CORE's goldens (tests/fixtures/display)", () => {
  const scenes = goldenScenes();
  test.skipIf(scenes.length)("goldens not generated yet (C1)", () => {});
  test.runIf(scenes.length)("the goldens cover the scenes the hit tests use", () => {
    expect(scenes).toEqual(expect.arrayContaining(["house", "arrow-labels"]));
  });
  for (const scene of scenes) {
    for (const theme of THEMES) {
      const svg = path.join(GOLDENS, `${scene}.${theme}.svg`);
      test.runIf(fs.existsSync(svg))(`${scene}.${theme}.svg`, () => {
        const dl = load(path.join(GOLDENS, `${scene}.json`));
        expect(toSVGString(dl, { theme })).toBe(svgFile(svg));
      });
    }
  }
});

describe("render fixtures keep their canonical SVG", () => {
  for (const name of ["sample", "kitchen-sink"]) {
    for (const theme of THEMES) {
      test(`${name}.${theme}.svg`, () => {
        const dl = load(path.join(FIXTURES, `${name}.json`));
        const out = toSVGString(dl, { theme });
        const file = path.join(FIXTURES, `${name}.${theme}.svg`);
        if (WRITE || !fs.existsSync(file)) fs.writeFileSync(file, `${out}\n`);
        expect(out).toBe(svgFile(file));
      });
    }
  }
});

describe("canonical form", () => {
  const light = toSVGString(sample, { theme: "light" });
  const dark = toSVGString(sample, { theme: "dark" });

  test("header, background and the four layers in order", () => {
    expect(light.startsWith('<svg xmlns="http://www.w3.org/2000/svg" width="1024" height="439" viewBox="60 60 560 240">')).toBe(true);
    expect(light).toContain('<rect x="60" y="60" width="560" height="240" fill="#f7f8fa"/>');
    const at = ["zones", "marks", "labels", "overlays"].map((l) => light.indexOf(`<g data-layer="${l}">`));
    expect(at.every((i) => i > 0)).toBe(true);
    expect([...at].sort((a, b) => a - b)).toEqual(at);
    expect(light.endsWith("</svg>")).toBe(true);
    expect(light).not.toMatch(/>\s+</);
  });

  test("attribute order: geometry, then fill stroke stroke-width [dash] caps", () => {
    expect(light).toContain('<rect x="120" y="160" width="160" height="80" rx="8" fill="#e8f2fe" stroke="#3f84d8" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"/>');
    expect(light).toContain(
      '<g font-size="20" font-family="Inter" font-weight="500" fill="#0b3a6e" text-anchor="middle"><text x="200" y="206.5" xml:space="preserve">Checkout API</text></g>',
    );
  });

  test("an arrow's pill and label draw in the labels layer, its shaft in marks", () => {
    const marks = light.slice(light.indexOf('<g data-layer="marks">'), light.indexOf('<g data-layer="labels">'));
    const labels = light.slice(light.indexOf('<g data-layer="labels">'), light.indexOf('<g data-layer="overlays">'));
    expect(marks).toContain('<g data-id="E-4"><g><path d="M284 200 L396 200" fill="none" stroke="#6b7079"');
    expect(marks).toContain('<polyline points="382,193 396,200 382,207" fill="none" stroke="#6b7079" stroke-width="2"');
    expect(labels).toContain('<g data-id="E-4"><rect x="308" y="188" width="64" height="24" rx="6" fill="#ffffff" stroke="#e3e5e9" stroke-width="1"');
    expect(labels).toContain(">writes</text>");
  });

  test("the dark theme is the dark palette, never a filter", () => {
    expect(dark).toContain('fill="#111214"/>');
    expect(dark).toContain('fill="#122640" stroke="#4a8fe0"');
    expect(dark).not.toContain("#e8f2fe");
    for (const svg of [light, dark]) {
      expect(svg).not.toMatch(/invert|hue-rotate|style=/);
      expect(svg).not.toContain("<defs>");
    }
  });

  test("the frame title follows the lod rule at the picture's scale", () => {
    // 560 units in 1024 px: scale 1.83, the band title (lod [0.75, null]) draws.
    expect(light).toContain('<text x="120" y="123.1" xml:space="preserve">Checkout</text>');
    expect(light).not.toContain('<text x="100" y="95.1">');
    // Squeezed to 200 px: scale 0.36, the title above the frame at max(16, 12 / 0.36) = 33.6.
    const small = toSVGString(sample, { theme: "light", maxPx: 200 });
    expect(small).toContain('font-size="33.6"');
    expect(small).not.toContain('<text x="120" y="123.1">');
  });

  test("a region crops, and entries outside it are left out", () => {
    const out = toSVGString(sample, { theme: "light", region: [390, 150, 570, 250] });
    expect(out).toContain('viewBox="390 150 180 100"');
    expect(out).toContain('data-id="E-3"');
    expect(out).not.toContain('data-id="E-2"');
    expect(out).toContain('data-id="E-1"');
  });

  test("families and urls are the caller's outside canonical mode", () => {
    const out = toSVGString(sink, {
      theme: "light",
      families: { sans: '"Synapse Sans"', mono: '"Synapse Mono"' },
      urls: { asset: (n) => `/a/${n}`, still: (n) => `/s/${n}` },
    });
    expect(out).toContain('font-family="&quot;Synapse Sans&quot;"');
    expect(out).toMatch(/href="\/a\/[0-9a-f]{32}\.png"/);
    expect(toSVGString(sink)).toMatch(/href="synapse-asset:[0-9a-f]{32}\.png"/);
  });

  test("the kitchen sink draws every primitive, hatch and elevation in defs", () => {
    const out = toSVGString(sink, { theme: "light" });
    for (const tag of ["<rect", "<ellipse", "<polygon", "<polyline", "<path", "<image", "<text", "<circle", 'data-slot="chart"', 'data-slot="viz"']) {
      expect(out, tag).toContain(tag);
    }
    expect(out).toMatch(/^<svg[^>]*><defs>/);
    expect(out).toContain('<pattern id="synapse-hatch-0"');
    expect(out).toContain('url(#synapse-hatch-0)');
    // E-12 holds an unknown primitive ("sparkle") and a malformed rect: neither draws, and a written
    // picture has no group for it (only the page draws such an entry's hit box, dashed).
    expect(out).not.toContain('data-id="E-12"');
    // Screen-anchored groups (the comment pin) and screen-px strokes follow the picture's scale.
    expect(out).toContain('<g transform="translate(200 100) scale(1)">');
    expect(out).toContain('stroke-width="1.99"');
  });

  test("a right-to-left line takes its base direction from its text, never direction=rtl", () => {
    const out = toSVGString(sink);
    expect(out).toContain('<text x="536" y="136" xml:space="preserve" unicode-bidi="plaintext">שלום עולם</text>');
    expect(out).not.toContain("direction=");
  });

  test("escaping and control characters", () => {
    const dl = structuredClone(sample);
    dl.entries[1].items[1].lines[0].t = 'a < b & "c" > d\u0007';
    expect(toSVGString(dl)).toContain(">a &lt; b &amp; &quot;c&quot; &gt; d</text>");
  });

  test("deterministic: the same list gives the same bytes", () => {
    expect(toSVGString(structuredClone(sink), { theme: "dark" })).toBe(toSVGString(sink, { theme: "dark" }));
  });
});

describe("picture size (canvas_render.pixel_size)", () => {
  test("longer side is maxPx, halves round to even", () => {
    expect(pixelSize([0, 0, 560, 240], 1024)).toEqual([1024, 439]);
    expect(pixelSize([0, 0, 100, 100], 1024)).toEqual([1024, 1024]);
    expect(pixelSize([0, 0, 2048, 1], 1024)).toEqual([1024, 1]);
  });

  test("normalizeBox orders corners and keeps a unit minimum", () => {
    expect(normalizeBox([10, 20, 0, 5])).toEqual([0, 5, 10, 20]);
    expect(normalizeBox([5, 5, 5, 5])).toEqual([5, 5, 6, 6]);
    expect(normalizeBox(null)).toEqual([-40, -40, 440, 340]);
  });
});
