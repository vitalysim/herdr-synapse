// After `vite build`: copy what the page loads at runtime that Vite does not bundle,
// ship a licence for every third-party package in dist/, and write the hash lists the
// Python test checks (dist/MANIFEST.json and dist/manifest.sha256).
//
//   dist/fonts/          Excalidraw's fonts (window.EXCALIDRAW_ASSET_PATH = "/"), minus the
//                        12 MB Xiaolai CJK fallback: CJK text falls back to system fonts.
//   dist/viz-lib/        what a sealed live-visual frame may load: synapse-viz.js, d3 (UMD),
//                        three (one minified ES module), p5 (the unmodified distributed file).
//   dist/licenses/       <package>.txt for every bundled or vendored package, fonts.txt,
//                        and THIRD_PARTY.txt, the summary.
//   dist/kinds.json      the canvas kinds the v1 page draws (src/canvas/kinds/names.js), and the
//                        display-list versions and slot kinds the v2 renderer draws
//                        (src/v2/render/version.js); a Python test holds it equal to
//                        herdr_team/canvas_kinds.
//   dist/charts.json     the ECharts modules the chart chunk bundles (src/v2/charts/components.js),
//                        the echarts-gl ones the GL chunk bundles, and the scene3d primitives and
//                        loaders (src/v2/scene3d/manifest.js); the Python tests hold every chart type
//                        and 3D primitive against it (canvas-v2-phase3-4.md D19).
//
// Gates: no emitted JS chunk may call `new Function(` or `eval(` (the page's CSP refuses both; this
// also proves ECharts' geo module stayed out and claygl's size expressions were patched), and the
// lazy chunk sizes are printed (gzip -9) against the phase 3-4 budgets.
// The bundled Inter and Geist Mono (assets/fonts/) are not copied here: src/fonts.css imports them
// and Vite hashes them into dist/assets/. This script checks they arrived byte for byte.
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath, pathToFileURL } from "node:url";
import zlib from "node:zlib";
import { build as rolldownBuild } from "rolldown";

const WEB = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
// SYNAPSE_DIST_DIR builds somewhere other than web/dist (a build under test, served to the e2e runs
// with SYNAPSE_E2E_DIST); vite.config.js reads the same variable.
const DIST = process.env.SYNAPSE_DIST_DIR ? path.resolve(process.env.SYNAPSE_DIST_DIR) : path.join(WEB, "dist");
const REPO_FONTS = path.join(WEB, "..", "assets", "fonts");
// The fonts src/fonts.css bundles: [licence heading, directory under assets/fonts, files].
const BUNDLED_FONTS = [
  ["Inter 4.1 (canvas labels as Excalidraw's \"Helvetica\" slot, and the page chrome as Synapse Sans)", "inter",
    ["Inter-Regular.ttf", "Inter-Medium.ttf", "Inter-SemiBold.ttf", "Inter-Bold.ttf"]],
  ["Geist Mono (the page chrome's code as Synapse Mono)", "geist-mono", ["GeistMono-Regular.ttf"]],
];
const NM = path.join(WEB, "node_modules");
const EXCALIDRAW = path.join(NM, "@excalidraw", "excalidraw");
const SKIPPED_FONTS = new Set(["Xiaolai"]);
const LICENSE_NAMES = ["LICENSE", "LICENSE.md", "LICENSE.txt", "LICENCE", "LICENCE.md", "license", "license.md", "license.txt", "LICENSE-MIT", "COPYING"];

function copyDir(from, to, skip = () => false) {
  fs.mkdirSync(to, { recursive: true });
  for (const entry of fs.readdirSync(from, { withFileTypes: true })) {
    if (skip(entry.name)) continue;
    const source = path.join(from, entry.name);
    const target = path.join(to, entry.name);
    if (entry.isDirectory()) copyDir(source, target, skip);
    else if (entry.isFile()) fs.copyFileSync(source, target);
  }
}

function readPackage(dir) {
  return JSON.parse(fs.readFileSync(path.join(dir, "package.json"), "utf8"));
}

function licenseText(dir) {
  for (const name of LICENSE_NAMES) {
    const file = path.join(dir, name);
    if (fs.existsSync(file) && fs.statSync(file).isFile()) return fs.readFileSync(file, "utf8");
  }
  for (const name of fs.readdirSync(dir)) {
    if (/^licen[cs]e/i.test(name) && fs.statSync(path.join(dir, name)).isFile()) return fs.readFileSync(path.join(dir, name), "utf8");
  }
  return null;
}

// Packages whose declared licence and licence file disagree, or that ship notices besides the licence:
// the licence file is what ships (canvas-v2-phase3-4.md 9.1).
const LICENSE_NOTES = {
  "echarts-gl": "package.json says MIT; its LICENSE file is BSD-3-Clause (Baidu), which is the text shipped here",
  claygl: "package.json has no licence field; its LICENSE file is BSD-2-Clause",
};

// A package's NOTICE file and licenses/ directory (Apache-2.0 packages such as echarts), appended
// after its licence.
function noticeText(dir) {
  let out = "";
  for (const name of ["NOTICE", "NOTICE.md", "NOTICE.txt"]) {
    const file = path.join(dir, name);
    if (fs.existsSync(file) && fs.statSync(file).isFile()) out += `\n\n===== ${name} =====\n\n${fs.readFileSync(file, "utf8").trim()}\n`;
  }
  const extra = path.join(dir, "licenses");
  if (fs.existsSync(extra) && fs.statSync(extra).isDirectory()) {
    for (const name of fs.readdirSync(extra).sort()) {
      const file = path.join(extra, name);
      if (fs.statSync(file).isFile()) out += `\n\n===== licenses/${name} =====\n\n${fs.readFileSync(file, "utf8").trim()}\n`;
    }
  }
  return out;
}

function licenseField(pkg, text) {
  if (typeof pkg.license === "string") return pkg.license;
  if (pkg.license && pkg.license.type) return pkg.license.type;
  if (Array.isArray(pkg.licenses)) return pkg.licenses.map((item) => item.type || item).join(" OR ");
  // Some packages ship only the file; name what it says.
  const known = [[/MIT License/i, "MIT"], [/ISC License/i, "ISC"], [/Apache License/i, "Apache-2.0"], [/BSD 3-Clause|Redistributions in binary form/i, "BSD-3-Clause"]];
  for (const [pattern, name] of known) if (text && pattern.test(text)) return `${name} (from its licence file)`;
  return "UNKNOWN";
}

// -- 1. fonts --------------------------------------------------------------------------

copyDir(path.join(EXCALIDRAW, "dist", "prod", "fonts"), path.join(DIST, "fonts"), (name) => SKIPPED_FONTS.has(name));

// -- 2. viz-lib ------------------------------------------------------------------------

const VIZ = path.join(DIST, "viz-lib");
fs.mkdirSync(VIZ, { recursive: true });
fs.copyFileSync(path.join(WEB, "src", "viz", "synapse-viz.js"), path.join(VIZ, "synapse-viz.js"));
fs.copyFileSync(path.join(NM, "d3", "dist", "d3.min.js"), path.join(VIZ, "d3.min.js"));
fs.copyFileSync(path.join(NM, "p5", "lib", "p5.min.js"), path.join(VIZ, "p5.min.js"));
// three ships only unminified modules split in two; frames import "three" through an import
// map, so bundle it into one minified ES module with rolldown (Vite's own bundler).
await rolldownBuild({
  input: path.join(NM, "three", "build", "three.module.js"),
  logLevel: "warn",
  output: { file: path.join(VIZ, "three.module.min.js"), format: "es", minify: true, sourcemap: false },
});

// -- 3. licences ------------------------------------------------------------------------

const LICENSES = path.join(DIST, "licenses");
fs.rmSync(LICENSES, { recursive: true, force: true });
fs.mkdirSync(LICENSES, { recursive: true });

const bundled = JSON.parse(fs.readFileSync(path.join(NM, ".synapse-bundled.json"), "utf8"));
const vendored = ["d3", "three", "p5", "@excalidraw/excalidraw"].map((name) => ({ name, dir: path.join(NM, ...name.split("/")) }));
const seen = new Map();
for (const { name, dir } of [...bundled, ...vendored]) {
  const full = path.isAbsolute(dir) ? dir : path.join(WEB, dir);
  if (!fs.existsSync(path.join(full, "package.json"))) continue;
  const pkg = readPackage(full);
  const key = `${name}@${pkg.version}`;
  if (!seen.has(key)) seen.set(key, { name, version: pkg.version, license: licenseField(pkg, licenseText(full)), dir: full, pkg });
}

const packages = {};
const summary = [];
const nameCount = new Map();
for (const item of seen.values()) nameCount.set(item.name, (nameCount.get(item.name) || 0) + 1);
for (const item of [...seen.values()].sort((a, b) => a.name.localeCompare(b.name) || a.version.localeCompare(b.version))) {
  const fileBase = item.name.replace(/^@/, "").replace(/\//g, "__") + (nameCount.get(item.name) > 1 ? `@${item.version}` : "");
  const text = licenseText(item.dir);
  const note = LICENSE_NOTES[item.name] ? `note: ${LICENSE_NOTES[item.name]}\n` : "";
  const head = `${item.name} ${item.version}\nlicense: ${item.license}\n${item.pkg.homepage ? `homepage: ${item.pkg.homepage}\n` : ""}${note}\n`;
  let body = text || `This package ships no licence file. Its package.json declares: ${item.license}.\n${item.pkg.author ? `Author: ${typeof item.pkg.author === "string" ? item.pkg.author : item.pkg.author.name}\n` : ""}`;
  body += noticeText(item.dir);
  fs.writeFileSync(path.join(LICENSES, `${fileBase}.txt`), head + body);
  packages[nameCount.get(item.name) > 1 ? `${item.name}@${item.version}` : item.name] = item.version;
  summary.push(`${item.name} ${item.version}  ${item.license}  licenses/${fileBase}.txt${LICENSE_NOTES[item.name] ? `  (${LICENSE_NOTES[item.name]})` : ""}`);
}

// Font licences: Excalidraw ships its font metadata (with the full OFL text) in its sources.
function excalidrawFontLicenses() {
  const out = new Map();
  const devDir = path.join(EXCALIDRAW, "dist", "dev");
  for (const file of fs.readdirSync(devDir).filter((name) => name.endsWith(".js.map"))) {
    const map = JSON.parse(fs.readFileSync(path.join(devDir, file), "utf8"));
    (map.sources || []).forEach((source, i) => {
      const content = (map.sourcesContent || [])[i];
      const match = /fonts\/([A-Za-z]+)\/index\.ts$/.exec(source);
      if (!match || !content || !/licen[cs]e/i.test(content)) return;
      const comments = content.match(/\/\*[\s\S]*?\*\//g) || [];
      const block = comments.find((comment) => /licen[cs]e/i.test(comment));
      if (block) out.set(match[1], block.replace(/^\/\*+|\*+\/$/g, "").trim());
    });
  }
  return out;
}

const fontNotes = excalidrawFontLicenses();
const shippedFonts = fs.readdirSync(path.join(DIST, "fonts")).sort();
// Licences only; each font's own metadata (authors, copyright) follows where Excalidraw ships it.
const FONT_FACTS = {
  Assistant: "Assistant (Excalidraw's interface font): SIL Open Font License 1.1",
  Cascadia: "Cascadia Code: SIL Open Font License 1.1",
  ComicShanns: "Comic Shanns: MIT, see its metadata below",
  Excalifont: "Excalifont: SIL Open Font License 1.1, full text in its metadata below",
  Liberation: "Liberation Sans: SIL Open Font License 1.1",
  Lilita: "Lilita One: SIL Open Font License 1.1",
  Nunito: "Nunito: SIL Open Font License 1.1",
  Virgil: "Virgil: SIL Open Font License 1.1",
};
let fonts = "Fonts shipped in dist/fonts/ (copied unmodified from @excalidraw/excalidraw).\n";
fonts += "Xiaolai (the CJK fallback, 12 MB) is not shipped; CJK text uses system fonts.\n\n";
for (const name of shippedFonts) fonts += `- ${name}: ${FONT_FACTS[name] || "see @excalidraw/excalidraw"}\n`;
for (const [name, note] of fontNotes) {
  if (!shippedFonts.includes(name)) continue;
  fonts += `\n\n===== ${name} =====\n\n${note}\n`;
}
// The bundled fonts, each unmodified in dist/assets/ under a hashed name, with its OFL.
const sha = (file) => crypto.createHash("sha256").update(fs.readFileSync(file)).digest("hex");
const hashedAssets = new Map(fs.readdirSync(path.join(DIST, "assets")).map((name) => [sha(path.join(DIST, "assets", name)), name]));
fonts += "\n\nFonts bundled in dist/assets/ (copied unmodified from assets/fonts/, SIL Open Font License 1.1):\n";
for (const [, dir, files] of BUNDLED_FONTS) {
  for (const file of files) {
    const hashed = hashedAssets.get(sha(path.join(REPO_FONTS, dir, file)));
    if (!hashed) throw new Error(`postbuild: assets/fonts/${dir}/${file} is not in dist/assets byte for byte (is it imported in src/fonts.css?)`);
    fonts += `- assets/${hashed}: ${file}\n`;
  }
}
for (const [heading, dir] of BUNDLED_FONTS) {
  fonts += `\n\n===== ${heading} =====\n\n${fs.readFileSync(path.join(REPO_FONTS, dir, "OFL.txt"), "utf8").trim()}\n`;
}
fs.writeFileSync(path.join(LICENSES, "fonts.txt"), fonts);
summary.push("fonts in dist/fonts  OFL-1.1 / MIT  licenses/fonts.txt");
summary.push("Inter 4.1 and Geist Mono in dist/assets  OFL-1.1  licenses/fonts.txt");

fs.writeFileSync(
  path.join(LICENSES, "THIRD_PARTY.txt"),
  "Third-party software in the herdr-synapse whiteboard page (web/dist).\n" +
    "Every package's licence is in this directory. p5 (LGPL-2.1) is the unmodified distributed file\n" +
    "viz-lib/p5.min.js; three is re-bundled and minified into viz-lib/three.module.min.js.\n\n" +
    summary.join("\n") +
    "\n",
);

// -- 4. the page's canvas kinds ------------------------------------------------------------

// kinds: what the v1 (Excalidraw) page has a builder for (src/canvas/kinds/names.js).
// display_list and slots: what the v2 renderer draws (src/v2/render/version.js, data only):
// the display-list versions it supports and the slot kinds it draws in the browser.
const { PAGE_KINDS } = await import(pathToFileURL(path.join(WEB, "src", "canvas", "kinds", "names.js")).href);
const RENDER_VERSION = path.join(WEB, "src", "v2", "render", "version.js");
const { DL_SUPPORTED, SLOT_KINDS } = await import(pathToFileURL(RENDER_VERSION).href);
if (!Array.isArray(DL_SUPPORTED) || DL_SUPPORTED.length !== 2 || !Array.isArray(SLOT_KINDS)) {
  throw new Error("postbuild: src/v2/render/version.js must export DL_SUPPORTED [min, max] and SLOT_KINDS");
}
fs.writeFileSync(
  path.join(DIST, "kinds.json"),
  `${JSON.stringify({ v: 1, kinds: [...PAGE_KINDS].sort(), display_list: DL_SUPPORTED, slots: [...SLOT_KINDS].sort() })}\n`,
);

// -- 4b. the page's charts and 3D ------------------------------------------------------------

// charts.json (D19): what the page can draw, as data. Neither file imports anything.
const { ECHARTS_MODULES } = await import(pathToFileURL(path.join(WEB, "src", "v2", "charts", "components.js")).href);
const { PRIMITIVES, LOADERS, GL_MODULES } = await import(pathToFileURL(path.join(WEB, "src", "v2", "scene3d", "manifest.js")).href);
for (const [name, list] of [["ECHARTS_MODULES", ECHARTS_MODULES], ["GL_MODULES", GL_MODULES], ["PRIMITIVES", PRIMITIVES], ["LOADERS", LOADERS]]) {
  if (!Array.isArray(list) || !list.every((item) => typeof item === "string")) throw new Error(`postbuild: ${name} must be a list of names`);
}
fs.writeFileSync(
  path.join(DIST, "charts.json"),
  `${JSON.stringify({ v: 1, echarts: [...new Set(ECHARTS_MODULES)].sort(), gl: [...new Set(GL_MODULES)].sort(), scene3d: { primitives: [...new Set(PRIMITIVES)].sort(), loaders: [...new Set(LOADERS)].sort() } })}\n`,
);

// The eval gate: the page's CSP (script-src 'self') refuses `new Function` and eval, so no chunk
// may call either. The allowlist is empty. A method named eval (`eval(e, t) {` in vega's dataflow)
// is a definition, not a call.
const EVAL_CALL = /(?<![\w$.])eval\s*\((?![^()]*\)\s*\{)/g;
const NEW_FUNCTION = /\bnew\s+Function\s*\(/g;
const chunks = walk(path.join(DIST, "assets")).filter((rel) => rel.endsWith(".js"));
const offenders = [];
for (const rel of chunks) {
  const code = fs.readFileSync(path.join(DIST, "assets", rel), "utf8");
  const found = [...code.matchAll(NEW_FUNCTION), ...code.matchAll(EVAL_CALL)];
  for (const m of found) offenders.push(`assets/${rel}: ${code.slice(Math.max(0, m.index - 60), m.index + 60).replace(/\s+/g, " ")}`);
}
if (offenders.length) throw new Error(`postbuild: the page's CSP refuses new Function and eval, found:\n  ${offenders.join("\n  ")}`);
console.log(`postbuild: eval gate clean (${chunks.length} chunks, no new Function( or eval( call)`);

// The bundle table (canvas-v2-phase3-4.md 9.2, gzip -9): the v2 first load (the entry and every
// chunk it imports statically), then each lazy part by the chunk that holds its marker module.
const gz = (rel) => zlib.gzipSync(fs.readFileSync(path.join(DIST, "assets", rel)), { level: 9 }).length;
const indexHTML = fs.readFileSync(path.join(DIST, "index.html"), "utf8");
const entries = [...indexHTML.matchAll(/(?:src|href)="\/?assets\/([^"]+\.js)"/g)].map((m) => m[1]);
const staticImports = (rel, seen = new Set()) => {
  if (seen.has(rel) || !fs.existsSync(path.join(DIST, "assets", rel))) return seen;
  seen.add(rel);
  const code = fs.readFileSync(path.join(DIST, "assets", rel), "utf8");
  for (const m of code.matchAll(/(?:^|[;\s}])import\s*(?:[\w$*{}\s,]+from\s*)?["']\.\/([^"']+\.js)["']/g)) staticImports(m[1], seen);
  return seen;
};
const firstLoad = new Set();
for (const rel of entries) for (const dep of staticImports(rel)) firstLoad.add(dep);
const boardChunk = chunks.find((rel) => /^Board-/.test(rel));
if (boardChunk) for (const dep of staticImports(boardChunk)) firstLoad.add(dep);
const sum = (list) => list.reduce((n, rel) => n + gz(rel), 0);
const kb = (n) => `${(n / 1024).toFixed(1)} KB`;
// A lazy part: the chunks its entry chunk pulls in statically that are not loaded already.
const closureOf = (pattern, loaded) => {
  const out = new Set();
  for (const rel of chunks.filter((c) => pattern.test(c))) for (const dep of staticImports(rel)) if (!loaded.has(dep)) out.add(dep);
  return out;
};
const chartsPart = closureOf(/^echartsCore-/, firstLoad);
const scenePart = closureOf(/^renderer-/, firstLoad);
const glPart = closureOf(/^glCharts-/, new Set([...firstLoad, ...chartsPart]));
const table = [
  // Raised from 115.2 KB for Phase 5 collaboration (presence, proposals, freeze), which the page needs on load (2026-09-28).
  ["v2 first load (the entry and the Board, static imports)", sum([...firstLoad]), 121.6 * 1024],
  ["charts (echarts core, SVGRenderer, chart modules)", sum([...chartsPart]), 280 * 1024],
  ["scene3d (three subset, GLTFLoader, OrbitControls, renderer)", sum([...scenePart]), 190 * 1024],
  ["glcharts (CanvasRenderer, echarts-gl subset, claygl; after charts)", sum([...glPart]), 330 * 1024],
];
console.log("postbuild: bundle (gzip -9)");
for (const [name, size, budget] of table) console.log(`  ${name.padEnd(64)} ${kb(size).padStart(10)}  budget ${kb(budget)}${size > budget ? "  OVER" : ""}`);

// -- 5. hash lists -----------------------------------------------------------------------

function walk(dir, base = dir) {
  const out = [];
  for (const entry of fs.readdirSync(dir, { withFileTypes: true })) {
    const full = path.join(dir, entry.name);
    if (entry.isDirectory()) out.push(...walk(full, base));
    else if (entry.isFile()) out.push(path.relative(base, full).split(path.sep).join("/"));
  }
  return out;
}

const files = {};
for (const rel of walk(DIST).sort()) {
  if (rel === "MANIFEST.json" || rel === "manifest.sha256") continue;
  files[rel] = crypto.createHash("sha256").update(fs.readFileSync(path.join(DIST, rel))).digest("hex");
}
const own = readPackage(WEB);
fs.writeFileSync(
  path.join(DIST, "MANIFEST.json"),
  `${JSON.stringify({ v: 1, page: own.name, version: own.version, files, packages }, null, 1)}\n`,
);
fs.writeFileSync(
  path.join(DIST, "manifest.sha256"),
  Object.entries(files)
    .map(([rel, hash]) => `${hash}  ${rel}`)
    .join("\n") + "\n",
);
const total = Object.keys(files).reduce((sum, rel) => sum + fs.statSync(path.join(DIST, rel)).size, 0);
console.log(`postbuild: ${Object.keys(files).length} files, ${(total / 1048576).toFixed(1)} MB, ${Object.keys(packages).length} packages`);
