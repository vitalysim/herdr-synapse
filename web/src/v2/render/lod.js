// The zoom rules the display list carries (canvas-v2-phase1.md 1.3 and 1.6). Shared by the SVG
// mapping, hit testing and the browser check, so they agree on what is drawn at a scale.

// `lod: [min, max]` draws a primitive only when min <= scale < max; either end may be null.
export function lodVisible(p, scale) {
  const lod = p?.lod;
  if (!Array.isArray(lod)) return true;
  const [min, max] = lod;
  if (typeof min === "number" && !(scale >= min)) return false;
  if (typeof max === "number" && !(scale < max)) return false;
  return true;
}

/**
 * The effective size and baselines of a text primitive at `scale`. A text with
 * `zoom: {min_px, grow: "up", bottom}` (the title above a frame) keeps at least min_px on screen:
 * size_eff = max(size, min_px / scale), and its block grows upward from `bottom`, each line
 * lh/size * size_eff tall with its baseline base_ratio * size_eff below the line top. Any other
 * text keeps its own size and baselines.
 */
export function textLayout(p, scale) {
  const size = Number(p.size);
  const lines = Array.isArray(p.lines) ? p.lines : [];
  const zoom = p.zoom;
  if (!zoom || typeof zoom !== "object" || !(size > 0)) {
    return { size, lh: Number(p.lh) || size * 1.25, ys: lines.map((l) => Number(l.y)), zoomed: false, top: null };
  }
  const minPx = Number(zoom.min_px) || 0;
  const sizeEff = Math.max(size, scale > 0 ? minPx / scale : size);
  const lhRatio = (Number(p.lh) || size * 1.25) / size;
  const baseRatio = Number.isFinite(Number(p.base_ratio)) ? Number(p.base_ratio) : 0.95;
  const lhEff = lhRatio * sizeEff;
  const bottom = Number(zoom.bottom) || 0;
  const top = bottom - lines.length * lhEff;
  return { size: sizeEff, lh: lhEff, ys: lines.map((_, i) => top + baseRatio * sizeEff + i * lhEff), zoomed: true, top };
}

// Whether anything an entry draws depends on the camera scale (lod, zoom text, screen-px strokes
// or screen-anchored groups): only those entries redraw while zooming.
const scaleCache = new WeakMap();
export function dependsOnScale(entry) {
  if (!entry || typeof entry !== "object") return false;
  if (scaleCache.has(entry)) return scaleCache.get(entry);
  const walk = (items) =>
    Array.isArray(items) &&
    items.some(
      (p) =>
        p && typeof p === "object" && (p.lod || p.zoom || p.elev || p.sw_px !== undefined || p.screen || walk(p.items) || walk(p.fallback)),
    );
  const result = walk(entry.items);
  scaleCache.set(entry, result);
  return result;
}
