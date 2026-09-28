// The glTF loader (canvas-v2-phase3-4.md 3.7, D10). A model is a GLB the server validated and packed
// into the team's asset store at op time (assets/<32hex>.glb); the page never reads artifacts/ for
// 3D. It is fetched from the page's own origin, then parsed with GLTFLoader.parse.
//
// Textures load through TextureLoader (an <img> from a blob: URL, allowed by img-src blob:), never
// ImageBitmapLoader, whose fetch(blob:) the page's connect-src 'self' refuses. No Draco, KTX2 or
// meshopt decoder is registered: the server refuses models that need one. Loaded models are cached
// by asset name (a name is content-addressed, so it never changes).
import { LoadingManager, TextureLoader } from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";

const ASSET_RE = /^[0-9a-f]{32}\.glb$/;
const cache = new Map(); // name -> Promise<Group>
const warned = new Set();

/** The GLTFLoader plugin that makes the parser load textures with TextureLoader (D10). */
export function textureLoaderPlugin(parser) {
  parser.textureLoader = new TextureLoader(parser.options.manager);
  return { name: "synapse_texture_loader" };
}

/** A GLTFLoader for asset GLBs: TextureLoader for images, no decoder extensions. */
export function makeLoader(manager = new LoadingManager()) {
  const loader = new GLTFLoader(manager);
  loader.register(textureLoaderPlugin);
  return loader;
}

function countTriangles(root) {
  let n = 0;
  root.traverse((node) => {
    if (!node.isMesh || !node.geometry) return;
    const g = node.geometry;
    const count = g.index ? g.index.count : g.attributes?.position?.count || 0;
    n += Math.floor(count / 3);
  });
  return n;
}

/** Parses GLB bytes into a model group (userData.tris: its triangle count). */
export function parseGLB(buffer, loader = makeLoader()) {
  return new Promise((resolve, reject) => {
    loader.parse(
      buffer,
      "",
      (gltf) => {
        const model = gltf.scene || (gltf.scenes && gltf.scenes[0]);
        if (!model) {
          reject(new Error("the model has no scene"));
          return;
        }
        model.userData.tris = countTriangles(model);
        resolve(model);
      },
      (err) => reject(err instanceof Error ? err : new Error(String(err))),
    );
  });
}

/**
 * The model in asset `name`: Promise<Group>, cached by name. `url(name)` is the page's asset URL.
 * `expectTris` is solved.tris; a model that differs logs once.
 */
export function loadGLB(name, url, { expectTris = null, fetchImpl = typeof fetch === "function" ? fetch : null } = {}) {
  if (!ASSET_RE.test(String(name))) return Promise.reject(new Error(`not a GLB asset name: ${String(name)}`));
  if (!cache.has(name)) {
    const pending = (async () => {
      if (!fetchImpl) throw new Error("no fetch");
      const res = await fetchImpl(url(name), { credentials: "same-origin" });
      if (!res.ok) throw new Error(`assets/${name}: HTTP ${res.status}`);
      return parseGLB(await res.arrayBuffer());
    })();
    cache.set(name, pending);
    pending.catch(() => cache.delete(name));
  }
  return cache.get(name).then((model) => {
    if (Number.isFinite(expectTris) && expectTris !== model.userData.tris && !warned.has(name)) {
      warned.add(name);
      console.warn(`[synapse v2] ${name}: ${model.userData.tris} triangles, the server counted ${expectTris}`);
    }
    return model;
  });
}

/** Test hook: forget every cached model. */
export function _clearCache() {
  cache.clear();
  warned.clear();
}
