// Stills of a scene (canvas-v2-phase3-4.md 3.7, D12, D17): the iso, front and top views rendered
// with the light palette at the slot box ×2 (1,024 px or less a side), read back and posted as PNGs
// so agents' pictures show the page's rendering. At most one per (id, v, view), writable pages only;
// queued, one per frame, and paused during pointer gestures.
//
// A still is drawn off screen, into a render target of its own (multisampled; float where the GPU
// can read floats back), never into the visible canvas, so no frame can ever show it. three.js
// leaves a render target's pixels linear, so the readback is encoded to sRGB here, as the canvas
// output would be.
import { FloatType, UnsignedByteType, WebGLRenderTarget } from "three";
import { stillSize } from "./viewport.js";
import { STILL_VIEWS } from "./cameras.js";

export { STILL_VIEWS };

/** A FIFO of still jobs keyed "id@v@view"; a key is queued once for the page's life. */
export function createStillQueue() {
  const seen = new Set();
  const jobs = [];
  let paused = false;
  return {
    add(key, job) {
      if (seen.has(key)) return false;
      seen.add(key);
      jobs.push({ key, job });
      return true;
    },
    has(key) {
      return seen.has(key);
    },
    /** Forgets a key whose job failed, so a later render may try it again. */
    forget(key) {
      seen.delete(key);
    },
    next() {
      return paused ? null : jobs.shift() || null;
    },
    get size() {
      return jobs.length;
    },
    pause(on) {
      paused = !!on;
    },
    get paused() {
      return paused;
    },
    drop(prefix) {
      for (let i = jobs.length - 1; i >= 0; i -= 1) if (jobs[i].key.startsWith(prefix)) jobs.splice(i, 1);
    },
  };
}

/** Rows of RGBA bytes bottom-up (readPixels) into top-down ImageData bytes. */
export function flipRows(pixels, w, h) {
  const out = new Uint8ClampedArray(w * h * 4);
  const row = w * 4;
  for (let y = 0; y < h; y += 1) out.set(pixels.subarray((h - 1 - y) * row, (h - y) * row), y * row);
  return out;
}

function toBlob(bytes, w, h) {
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  ctx.putImageData(new ImageData(bytes, w, h), 0, 0);
  return new Promise((resolve, reject) => canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("toBlob failed"))), "image/png"));
}

/** A linear [0, 1] channel as an sRGB byte. */
export function srgbByte(linear) {
  const c = linear <= 0 ? 0 : linear >= 1 ? 1 : linear;
  const v = c <= 0.0031308 ? c * 12.92 : 1.055 * c ** (1 / 2.4) - 0.055;
  return Math.round(v * 255);
}

/** Linear pixels (floats in [0, 1], or bytes) as a new array of opaque sRGB bytes. */
export function encodeSRGB(pixels, isFloat) {
  const out = new Uint8ClampedArray(pixels.length);
  const lut = isFloat ? null : Array.from({ length: 256 }, (_, i) => srgbByte(i / 255));
  for (let i = 0; i < pixels.length; i += 4) {
    for (let k = 0; k < 3; k += 1) out[i + k] = isFloat ? srgbByte(pixels[i + k]) : lut[pixels[i + k]];
    out[i + 3] = 255;
  }
  return out;
}

/**
 * Renders `scene` with `camera` off screen at `box` (w, h world units, ×2, 1,024 px or less a side)
 * on `paper`, and returns a PNG Blob. The canvas's own frame is untouched.
 */
export function captureStill(renderer, scene, camera, box, paper, { fitCamera = null, sizeLabels = null, labelPx = 12 } = {}) {
  const [w, h] = stillSize(box[0], box[1]);
  if (fitCamera) fitCamera(camera, w / h);
  if (sizeLabels) sizeLabels(scene, camera, h, labelPx * (h / Math.max(1, box[1])));
  const isFloat = !!renderer.extensions?.has?.("EXT_color_buffer_float");
  const target = new WebGLRenderTarget(w, h, { type: isFloat ? FloatType : UnsignedByteType, samples: 4, depthBuffer: true });
  const previous = renderer.getRenderTarget();
  let pixels;
  try {
    renderer.setRenderTarget(target);
    renderer.setClearColor(paper, 1);
    renderer.clear(true, true, true);
    renderer.render(scene, camera);
    pixels = isFloat ? new Float32Array(w * h * 4) : new Uint8Array(w * h * 4);
    renderer.readRenderTargetPixels(target, 0, 0, w, h, pixels);
  } finally {
    renderer.setRenderTarget(previous);
    target.dispose();
  }
  return toBlob(flipRows(encodeSRGB(pixels, isFloat), w, h), w, h);
}
