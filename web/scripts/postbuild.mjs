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
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";
import { build as rolldownBuild } from "rolldown";

const WEB = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "..");
const DIST = path.join(WEB, "dist");
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
  const head = `${item.name} ${item.version}\nlicense: ${item.license}\n${item.pkg.homepage ? `homepage: ${item.pkg.homepage}\n` : ""}\n`;
  const body = text || `This package ships no licence file. Its package.json declares: ${item.license}.\n${item.pkg.author ? `Author: ${typeof item.pkg.author === "string" ? item.pkg.author : item.pkg.author.name}\n` : ""}`;
  fs.writeFileSync(path.join(LICENSES, `${fileBase}.txt`), head + body);
  packages[nameCount.get(item.name) > 1 ? `${item.name}@${item.version}` : item.name] = item.version;
  summary.push(`${item.name} ${item.version}  ${item.license}  licenses/${fileBase}.txt`);
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
fs.writeFileSync(path.join(LICENSES, "fonts.txt"), fonts);
summary.push("fonts in dist/fonts  OFL-1.1 / MIT  licenses/fonts.txt");

fs.writeFileSync(
  path.join(LICENSES, "THIRD_PARTY.txt"),
  "Third-party software in the herdr-synapse whiteboard page (web/dist).\n" +
    "Every package's licence is in this directory. p5 (LGPL-2.1) is the unmodified distributed file\n" +
    "viz-lib/p5.min.js; three is re-bundled and minified into viz-lib/three.module.min.js.\n\n" +
    summary.join("\n") +
    "\n",
);

// -- 4. hash lists -----------------------------------------------------------------------

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
