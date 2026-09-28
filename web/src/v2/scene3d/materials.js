// Scene materials (canvas-v2-phase3-4.md 3.5): a tone is its `mat.<t>.base` colour in the page's
// palette (the tone's solid; in dark, the neutral one toned down so a ground plane does not glare,
// QA phase34 L8), else `tone.<t>.solid`; a finish its roughness, metalness and opacity from the tokens. Edges use the tone's
// `mat.<t>.edge` shade when the palette has it. Cached per palette, so every object of one tone and
// finish shares a material.
import { Color, LineBasicMaterial, MeshStandardMaterial } from "three";
import tokens from "@synapse/tokens";

export const TONES = ["neutral", "info", "success", "warning", "danger", "accent", "idea", "decision"];
export const DEFAULT_FINISH = {
  matte: { roughness: 0.9, metalness: 0 },
  glossy: { roughness: 0.35, metalness: 0 },
  metal: { roughness: 0.3, metalness: 0.8 },
  glass: { roughness: 0.1, metalness: 0, opacity: 0.35 },
};
const HEX = /^#[0-9a-fA-F]{6}$/;

export function finishOf(name) {
  const own = tokens?.scene3d?.finish;
  const all = own && typeof own === "object" ? { ...DEFAULT_FINISH, ...own } : DEFAULT_FINISH;
  return all[name] || all.matte;
}

export function toneOf(obj) {
  return TONES.includes(obj?.tone) ? obj.tone : "neutral";
}

function hexOf(palette, ref, fallback) {
  const v = palette && palette[ref];
  return typeof v === "string" && HEX.test(v) ? v : fallback;
}

/** The colours a tone uses in a palette: {solid, edge}. */
export function toneColours(palette, tone) {
  const solid = hexOf(palette, `mat.${tone}.base`, hexOf(palette, `tone.${tone}.solid`, "#8b8d98"));
  const edge = hexOf(palette, `mat.${tone}.edge`, hexOf(palette, `tone.${tone}.stroke`, solid));
  return { solid, edge };
}

/** A material factory over one palette: surface(obj), edge(obj), line(ref), dispose(). */
export function createMaterials(palette) {
  const cache = new Map();
  const get = (key, make) => {
    if (!cache.has(key)) cache.set(key, make());
    return cache.get(key);
  };
  return {
    surface(obj) {
      const tone = toneOf(obj);
      const finishName = typeof obj?.finish === "string" ? obj.finish : "matte";
      const finish = finishOf(finishName);
      const own = Number(obj?.opacity);
      const opacity = Math.max(0.2, Math.min(1, Number.isFinite(own) ? own : Number.isFinite(finish.opacity) ? finish.opacity : 1));
      return get(`s:${tone}:${finishName}:${opacity}`, () => {
        const m = new MeshStandardMaterial({
          color: new Color(toneColours(palette, tone).solid),
          roughness: Number.isFinite(finish.roughness) ? finish.roughness : 0.9,
          metalness: Number.isFinite(finish.metalness) ? finish.metalness : 0,
        });
        if (opacity < 1) {
          m.transparent = true;
          m.opacity = opacity;
          m.depthWrite = false;
        }
        return m;
      });
    },
    edge(obj, { dashed = false } = {}) {
      const tone = toneOf(obj);
      return get(`e:${tone}:${dashed}`, () => new LineBasicMaterial({ color: new Color(toneColours(palette, tone).edge), transparent: dashed, opacity: dashed ? 0.8 : 1 }));
    },
    line(ref, fallback = "#8b8d98") {
      return get(`l:${ref}`, () => new LineBasicMaterial({ color: new Color(hexOf(palette, ref, fallback)) }));
    },
    solid(ref, fallback = "#8b8d98") {
      return get(`m:${ref}`, () => new MeshStandardMaterial({ color: new Color(hexOf(palette, ref, fallback)), roughness: 0.8, metalness: 0 }));
    },
    dispose() {
      for (const m of cache.values()) m.dispose();
      cache.clear();
    },
  };
}
