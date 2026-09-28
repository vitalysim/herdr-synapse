// window.__synapseV2.scene3d() (canvas-v2-phase3-4.md 3.7): what the page's 3D renderer did, for
// tools/canvas_qa.py and the e2e runs. This module is in the page's first chunk; the renderer (a lazy
// chunk) plugs its reader in with setActive once it exists.
//   {contexts, renderer: {calls, triangles, frames, lost, failed, restores} | null,
//    scenes: [{id, version, visible, rendered, renders, reason, tris, models, loading, failed,
//              proxied, entered, modelTris, expectTris, stills: [view]}], budget: glBudget.stats()}
// window.__synapseV2.scene3dProbe() measures what each drawn scene put on screen (next frame).
import { registerQA } from "../render/qa.js";
import { glBudget } from "./budget.js";

let active = null;
let prober = null;
const mounted = new Map(); // key -> {id, version}: scenes whose slot is mounted (before the chunk too)

export function setActive(fn, probe = null) {
  active = typeof fn === "function" ? fn : null;
  prober = typeof probe === "function" ? probe : null;
}

export function noteMounted(key, id, version) {
  mounted.set(key, { id, version });
}

export function noteUnmounted(key) {
  mounted.delete(key);
}

export function scene3dQA() {
  const live = active ? active() : null;
  const scenes = live ? live.scenes.slice() : [];
  const seen = new Set(scenes.map((s) => s.id));
  for (const { id, version } of mounted.values()) {
    if (!seen.has(id)) scenes.push({ id, version, visible: false, rendered: false, renders: 0, reason: "loading", tris: null, models: 0, loading: true, failed: null, proxied: false, entered: false, stills: [] });
  }
  return { contexts: glBudget.total(), renderer: live ? live.renderer : null, scenes, budget: glBudget.stats() };
}

/** Promise<{id: share of the scene's pixels that differ from its paper}> after the next frame (e2e S1). */
export function scene3dProbe() {
  return prober ? prober() : Promise.resolve({});
}

registerQA("scene3d", scene3dQA);
registerQA("scene3dProbe", scene3dProbe);
