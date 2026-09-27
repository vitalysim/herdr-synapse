// Build of the whiteboard page (web/dist). The Python server serves dist/ under a strict CSP:
// no inline script, no remote origin, so everything here is bundled or copied locally.
import fs from "node:fs";
import path from "node:path";
import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

const NODE_MODULES = "node_modules/";

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

export default defineConfig({
  base: "/",
  plugins: [react(), bundledPackages(), noRemoteFontFallback()],
  define: {
    // Excalidraw's documented Vite setting.
    "process.env.IS_PREACT": JSON.stringify("false"),
  },
  build: {
    outDir: "dist",
    emptyOutDir: true,
    assetsDir: "assets",
    sourcemap: false,
    target: "es2022",
    chunkSizeWarningLimit: 8000,
    modulePreload: { polyfill: false },
  },
});
