// Build of the whiteboard page (web/dist). The Python server serves dist/ under a strict CSP:
// no inline script, no remote origin, so everything here is bundled or copied locally.
import crypto from "node:crypto";
import fs from "node:fs";
import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";
import { PYTHON_ONLY_TOKEN_BLOCKS } from "./scripts/python-only-tokens.mjs";

const NODE_MODULES = "node_modules/";
const REPO = path.resolve(import.meta.dirname, "..");

// The design tokens the page is coloured from: the first of these files that holds the full
// token document (theme.light and theme.dark). herdr_team/canvas_tokens.json is the designer's
// source of truth; assets/canvas/tokens.json is where canvas_theme --write puts its copy.
const TOKEN_FILES = ["assets/canvas/tokens.json", "herdr_team/canvas_tokens.json"];

function tokenFile() {
  for (const rel of TOKEN_FILES) {
    const file = path.join(REPO, rel);
    try {
      const doc = JSON.parse(fs.readFileSync(file, "utf8"));
      if (doc?.theme?.light?.tone && doc?.theme?.dark?.base && doc?.phase0_excalidraw) return file;
    } catch {
      // missing or another shape: try the next
    }
  }
  throw new Error(`no design token file with theme.light, theme.dark and phase0_excalidraw in ${TOKEN_FILES.join(" or ")}`);
}

// The bundled fonts src/fonts.css imports (assets/fonts/, written by the Python side). A missing
// one fails the build here with its name rather than as an unresolved url() in the CSS.
const FONT_FILES = ["inter/Inter-Regular.ttf", "inter/Inter-Medium.ttf", "inter/Inter-SemiBold.ttf", "inter/Inter-Bold.ttf", "geist-mono/GeistMono-Regular.ttf"];
for (const rel of FONT_FILES) {
  if (!fs.existsSync(path.join(REPO, "assets", "fonts", rel))) throw new Error(`assets/fonts/${rel} is missing: the page bundles it (src/fonts.css)`);
}

// The sha256 of the font metrics Python measures with (assets/fonts/font-metrics.json). The v2
// board sends it with every POST /measure, so the server drops corrections a page built against
// other metrics made (canvas-v2-phase1.md 3.3).
const FONT_METRICS = path.join(REPO, "assets", "fonts", "font-metrics.json");
if (!fs.existsSync(FONT_METRICS)) throw new Error("assets/fonts/font-metrics.json is missing: python3 -m herdr_team.canvas_fontgen --write");
const FONT_METRICS_SHA = crypto.createHash("sha256").update(fs.readFileSync(FONT_METRICS)).digest("hex");

// Records every npm package whose code ends up in the output chunks, so postbuild can
// ship each one's licence (web/dist/licenses/) and version (MANIFEST.json "packages").
function bundledPackages() {
  return {
    name: "synapse-bundled-packages",
    generateBundle(_options, bundle) {
      const dirs = new Map();
      for (const item of Object.values(bundle)) {
        if (item.type !== "chunk") continue;
        const ids = item.moduleIds || Object.keys(item.modules || {});
        for (const raw of ids) {
          const id = raw.replace(/^\0/, "").split("?")[0].split(path.sep).join("/");
          const at = id.lastIndexOf(NODE_MODULES);
          if (at < 0) continue;
          const rest = id.slice(at + NODE_MODULES.length).split("/");
          const name = rest[0].startsWith("@") ? `${rest[0]}/${rest[1]}` : rest[0];
          dirs.set(id.slice(0, at + NODE_MODULES.length) + name, name);
        }
      }
      const out = [...dirs.entries()].map(([dir, name]) => ({ name, dir })).sort((a, b) => a.dir.localeCompare(b.dir));
      fs.writeFileSync(path.resolve(NODE_MODULES, ".synapse-bundled.json"), JSON.stringify(out, null, 1));
    },
  };
}

// The token document as the page bundles it: every block but those only the Python side reads
// (scripts/python-only-tokens.mjs says which, and why each one never reaches the browser). Leaving
// them out keeps the first-load chunk inside its budget (9.2, canvas-v2-phase6.md D4).
function pageTokens() {
  const file = tokenFile().split(path.sep).join("/");
  return {
    name: "synapse-page-tokens",
    enforce: "pre",
    transform(code, id) {
      if (id.split("?")[0].split(path.sep).join("/") !== file) return null;
      const doc = JSON.parse(code);
      for (const key of PYTHON_ONLY_TOKEN_BLOCKS) delete doc[key];
      return { code: JSON.stringify(doc), map: null };
    },
  };
}

// Excalidraw appends an esm.sh URL to every font face as a fallback. The page's CSP blocks it
// anyway (nothing loads from a CDN), but Chrome reports each blocked source; drop it so fonts
// come only from web/dist/fonts. Fails the build if a new Excalidraw changes the pattern.
function noRemoteFontFallback() {
  const pattern = /([\w$]+)\.push\(new URL\(([\w$]+),\s*([\w$]+)\.ASSETS_FALLBACK_URL\)\)/g;
  return {
    name: "synapse-no-remote-font-fallback",
    transform(code, id) {
      if (!id.includes("@excalidraw/excalidraw") || !code.includes("ASSETS_FALLBACK_URL")) return null;
      const next = code.replace(pattern, "void 0");
      if (next === code) this.error("Excalidraw's font fallback changed shape; update noRemoteFontFallback in vite.config.js");
      return { code: next, map: null };
    },
  };
}

// claygl (echarts-gl's WebGL engine) sizes its post-effect buffers with `new Function` in
// createCompositor.js tryConvertExpr, reached for every GL chart. The page's CSP (script-src 'self')
// refuses that, so the call is replaced with src/v2/scene3d/expr.js synapseExpr, an arithmetic-only
// evaluator of the same expressions (canvas-v2-phase3-4.md D8, 3.11). The build fails when the
// pattern is not there exactly once, or when claygl reaches the bundle unpatched; postbuild.mjs then
// fails on any `new Function(` or `eval(` left in an emitted chunk.
const CLAYGL_EXPR = "new Function('width', 'height', 'dpr', 'return ' + exprRes[1])";
const EXPR_MODULE = path.resolve(import.meta.dirname, "src/v2/scene3d/expr.js").split(path.sep).join("/");
function cspSafeClayglExpr() {
  const patched = new Set();
  const isCompositor = (id) => /[\\/]claygl[\\/]src[\\/]createCompositor\.js$/.test(id.split("?")[0]);
  return {
    name: "synapse-csp-safe-claygl-expr",
    enforce: "pre",
    transform(code, id) {
      if (!isCompositor(id)) return null;
      const count = code.split(CLAYGL_EXPR).length - 1;
      if (count !== 1) this.error(`claygl createCompositor.js: expected the new Function size expression exactly once, found ${count}; update cspSafeClayglExpr in vite.config.js`);
      patched.add(id);
      const next = `import { synapseExpr as __synapseExpr } from ${JSON.stringify(EXPR_MODULE)};\n${code.replace(CLAYGL_EXPR, "__synapseExpr(exprRes[1])")}`;
      return { code: next, map: null };
    },
    generateBundle(_options, bundle) {
      for (const item of Object.values(bundle)) {
        if (item.type !== "chunk") continue;
        for (const id of item.moduleIds || Object.keys(item.modules || {})) {
          if (isCompositor(id) && !patched.has(id)) this.error(`claygl ${id} reached the bundle without the CSP patch`);
        }
      }
    },
  };
}

export default defineConfig({
  base: "/",
  plugins: [react(), bundledPackages(), noRemoteFontFallback(), cspSafeClayglExpr(), pageTokens()],
  resolve: {
    alias: { "@synapse/tokens": tokenFile() },
  },
  // echarts-gl and claygl skip the dev server's dependency pre-bundling, so the claygl CSP patch
  // (cspSafeClayglExpr) applies in `vite` as it does in `vite build`; echarts and zrender skip it
  // too, so echarts-gl's deep imports (echarts/lib/...) and the charts' echarts/core share one copy.
  optimizeDeps: { exclude: ["echarts-gl", "claygl", "echarts", "zrender"] },
  // The dev server reads the fonts and tokens from outside web/.
  server: { fs: { allow: [".."] } },
  define: {
    // Excalidraw's documented Vite setting.
    "process.env.IS_PREACT": JSON.stringify("false"),
    __SYNAPSE_FONT_METRICS_SHA__: JSON.stringify(FONT_METRICS_SHA),
  },
  // vitest (npm test): the page's pure units run in node; a component test opts into jsdom
  // with a `// @vitest-environment jsdom` first line. The CDP interaction tests are separate
  // (npm run test:e2e, web/test/e2e/run.mjs).
  test: {
    environment: "node",
    include: ["src/**/*.test.{js,jsx}"],
    restoreMocks: true,
  },
  build: {
    // SYNAPSE_DIST_DIR: a build under test outside web/dist (scripts/postbuild.mjs reads it too).
    outDir: process.env.SYNAPSE_DIST_DIR ? path.resolve(process.env.SYNAPSE_DIST_DIR) : "dist",
    emptyOutDir: true,
    assetsDir: "assets",
    sourcemap: false,
    target: "es2022",
    chunkSizeWarningLimit: 8000,
    modulePreload: { polyfill: false },
    // Fonts stay files (hashed into dist/assets/, byte-identical to assets/fonts/), never data: URLs.
    assetsInlineLimit: (file) => (/\.(ttf|otf|woff2?)$/i.test(file) ? false : undefined),
  },
});
