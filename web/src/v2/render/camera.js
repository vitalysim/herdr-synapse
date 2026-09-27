// The camera: pure math, no DOM (canvas-v2-phase1.md 4.2 and 6.2; D3: our own, no d3-zoom).
// A camera is {x, y, scale}: the world point at the screen's top-left corner, and screen pixels
// per world unit, so screen = (world - [x, y]) * scale. Every function returns a new object.

export const MIN_SCALE = 0.05;
export const MAX_SCALE = 8;

function clamp(v, lo, hi) {
  return Math.min(hi, Math.max(lo, v));
}

function num(v, fallback) {
  const n = Number(v);
  return Number.isFinite(n) ? n : fallback;
}

export const camera = {
  create(init = {}) {
    return { x: num(init.x, 0), y: num(init.y, 0), scale: clamp(num(init.scale, 1) || 1, MIN_SCALE, MAX_SCALE) };
  },

  toScreen(cam, pt) {
    return [(pt[0] - cam.x) * cam.scale, (pt[1] - cam.y) * cam.scale];
  },

  toWorld(cam, screenPt) {
    return [screenPt[0] / cam.scale + cam.x, screenPt[1] / cam.scale + cam.y];
  },

  // Zoom by `factor` keeping the world point under `screenPt` where it is.
  zoomAt(cam, factor, screenPt, { min = MIN_SCALE, max = MAX_SCALE } = {}) {
    const f = num(factor, 1);
    const scale = clamp(cam.scale * (f > 0 ? f : 1), min, max);
    if (scale === cam.scale) return { ...cam };
    const [wx, wy] = camera.toWorld(cam, screenPt);
    return { ...cam, x: wx - screenPt[0] / scale, y: wy - screenPt[1] / scale, scale };
  },

  // Move the picture by (dx, dy) screen pixels: dragging right shows what lies to the left.
  panBy(cam, dxScreen, dyScreen) {
    return { ...cam, x: cam.x - num(dxScreen, 0) / cam.scale, y: cam.y - num(dyScreen, 0) / cam.scale };
  },

  // The camera that shows world `rect` [x0, y0, x1, y1] centred in `viewport` {w, h}.
  fit(rect, viewport, { padPx = 48, maxScale = 1.5 } = {}) {
    const vw = Math.max(1, num(viewport?.w, 1));
    const vh = Math.max(1, num(viewport?.h, 1));
    if (!Array.isArray(rect) || rect.length !== 4 || !rect.every(Number.isFinite)) return camera.create();
    const x0 = Math.min(rect[0], rect[2]);
    const y0 = Math.min(rect[1], rect[3]);
    const rw = Math.max(1, Math.abs(rect[2] - rect[0]));
    const rh = Math.max(1, Math.abs(rect[3] - rect[1]));
    const pad = Math.min(padPx, vw / 4, vh / 4);
    const scale = clamp(Math.min((vw - 2 * pad) / rw, (vh - 2 * pad) / rh, maxScale), MIN_SCALE, MAX_SCALE);
    return { x: x0 + rw / 2 - vw / (2 * scale), y: y0 + rh / 2 - vh / (2 * scale), scale };
  },

  // The world rectangle [x0, y0, x1, y1] the viewport shows.
  viewRect(cam, viewport) {
    return [cam.x, cam.y, cam.x + num(viewport?.w, 0) / cam.scale, cam.y + num(viewport?.h, 0) / cam.scale];
  },

  // The SVG transform of the world group: matrix(s 0 0 s -x*s -y*s).
  matrix(cam) {
    const s = cam.scale;
    return `matrix(${s} 0 0 ${s} ${-cam.x * s} ${-cam.y * s})`;
  },

  equal(a, b) {
    return !!a && !!b && a.x === b.x && a.y === b.y && a.scale === b.scale;
  },
};
