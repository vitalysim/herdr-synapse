// The shared renderer's viewport and scissor math (canvas-v2-phase3-4.md 3.7), pure so it is tested
// without WebGL. Rectangles are CSS px ({left, top, width, height}, as getBoundingClientRect gives);
// WebGL's origin is the canvas's bottom-left corner, so y flips.

export const MIN_SIDE_PX = 48; // below this on its shorter side a scene shows its still (LOD)
export const MAX_SCENES = 16; // scenes drawn in one frame; the rest show their stills
export const MAX_PIXELS = 4e6; // device pixels scissored in one frame
export const MAX_TRIANGLES = 1e6; // triangles drawn in one frame; models past it draw their proxy box
export const MAX_DPR = 2;

export function pixelRatio(dpr) {
  const d = Number(dpr);
  return Math.min(MAX_DPR, Number.isFinite(d) && d > 0 ? d : 1);
}

/**
 * Where a placeholder `rect` draws on a canvas covering `host`:
 * {viewport: [x, y, w, h], scissor: [x, y, w, h], visible, reason, pixels} in CSS px, y from the
 * bottom. The viewport is the whole rectangle (a scene partly off screen is not squashed); the
 * scissor is its part inside the host. `reason` says why a scene is not drawn: "off" (outside the
 * host) or "small" (under MIN_SIDE_PX on its shorter side).
 */
export function viewportFor(rect, host, dpr = 1) {
  const x = rect.left - host.left;
  const top = rect.top - host.top;
  const y = host.height - top - rect.height;
  const sx0 = Math.max(0, x);
  const sy0 = Math.max(0, y);
  const sx1 = Math.min(host.width, x + rect.width);
  const sy1 = Math.min(host.height, y + rect.height);
  const sw = Math.max(0, sx1 - sx0);
  const sh = Math.max(0, sy1 - sy0);
  const pr = pixelRatio(dpr);
  let reason = null;
  if (!(sw > 0 && sh > 0)) reason = "off";
  else if (Math.min(rect.width, rect.height) < MIN_SIDE_PX) reason = "small";
  return {
    viewport: [x, y, rect.width, rect.height],
    scissor: [sx0, sy0, sw, sh],
    visible: reason === null,
    reason,
    pixels: Math.round(sw * pr) * Math.round(sh * pr),
  };
}

/**
 * The frame plan for placeholders in DOM order: each {key, draw, reason} under the per-frame
 * limits (visibility, MAX_SCENES, MAX_PIXELS). `rects` is [{key, rect}].
 */
export function planFrame(rects, host, dpr = 1) {
  let scenes = 0;
  let pixels = 0;
  return rects.map(({ key, rect }) => {
    const vp = viewportFor(rect, host, dpr);
    if (!vp.visible) return { key, draw: false, reason: vp.reason, vp };
    if (scenes >= MAX_SCENES) return { key, draw: false, reason: "scenes", vp };
    if (pixels + vp.pixels > MAX_PIXELS) return { key, draw: false, reason: "pixels", vp };
    scenes += 1;
    pixels += vp.pixels;
    return { key, draw: true, reason: null, vp };
  });
}

/** True when two rects differ by more than a tenth of a pixel. */
export function moved(a, b) {
  if (!a || !b) return a !== b;
  return Math.abs(a.left - b.left) > 0.1 || Math.abs(a.top - b.top) > 0.1 || Math.abs(a.width - b.width) > 0.1 || Math.abs(a.height - b.height) > 0.1;
}

/** The size in device pixels of a still for a slot box (w, h world units): ×2, longer side 1024 at most. */
export function stillSize(w, h, cap = 1024) {
  const W = Math.max(1, Number(w) || 1) * 2;
  const H = Math.max(1, Number(h) || 1) * 2;
  const s = Math.min(1, cap / Math.max(W, H));
  return [Math.max(1, Math.round(W * s)), Math.max(1, Math.round(H * s))];
}
