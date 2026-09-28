// Scene cameras from the tokens (canvas-v2-phase3-4.md 3.5): presets iso, front, top, side and
// orbit, each framing the solved bounds with a 6 % margin; `zoom` scales that fit and `target` (an
// object id or a point) re-centres it.
//
// Angles, the same convention as the server projections (canvas_scene3d._project): y is up, x right,
// z toward the viewer. The camera looks at the target from the direction
//   (sin az · cos el, sin el, cos az · cos el)
// so az 0, el 0 is the front view (from +z), az 90 the side (from +x) and el 90 the top.
import { OrthographicCamera, PerspectiveCamera, Vector3 } from "three";
import tokens from "@synapse/tokens";

export const DEFAULT_CAMERAS = {
  iso: { projection: "ortho", az: 45, el: 35.264 },
  front: { projection: "ortho", az: 0, el: 0 },
  top: { projection: "ortho", az: 0, el: 90 },
  side: { projection: "ortho", az: 90, el: 0 },
  orbit: { projection: "persp", az: 35, el: 25, fov: 35 },
};
export const MARGIN = 0.06;
// The still views, and what a camera preset maps to for stills (3.5).
export const STILL_VIEWS = ["iso", "front", "top"];

export function presets() {
  const own = tokens?.scene3d?.cameras;
  return own && typeof own === "object" ? { ...DEFAULT_CAMERAS, ...own } : DEFAULT_CAMERAS;
}

const RAD = Math.PI / 180;
const finite = (v, d) => (Number.isFinite(Number(v)) ? Number(v) : d);

/** The element's camera setting as {preset, projection, az, el, fov, zoom, target}. */
export function cameraSpec(setting, overrides = null) {
  const all = presets();
  const raw = typeof setting === "string" ? { preset: setting } : setting && typeof setting === "object" ? setting : {};
  const name = typeof raw.preset === "string" && all[raw.preset] ? raw.preset : "iso";
  const base = all[name];
  const spec = {
    preset: name,
    projection: base.projection === "persp" ? "persp" : "ortho",
    az: finite(raw.az, finite(base.az, 45)),
    el: Math.max(-90, Math.min(90, finite(raw.el, finite(base.el, 35.264)))),
    fov: finite(raw.fov, finite(base.fov, 35)),
    zoom: Math.max(0.05, Math.min(20, finite(raw.zoom, 1))),
    target: raw.target ?? null,
  };
  return overrides ? { ...spec, ...overrides } : spec;
}

/** The unit vector from the target toward the camera. */
export function viewDir(az, el) {
  const a = az * RAD;
  const e = el * RAD;
  return [Math.sin(a) * Math.cos(e), Math.sin(e), Math.cos(a) * Math.cos(e)];
}

// The camera's up vector: world y, except looking straight down or up (then -z / +z, so the top
// view has +x to the right and +z toward the bottom of the picture, like a map of the front).
function upFor(el) {
  if (el >= 89.999) return [0, 0, -1];
  if (el <= -89.999) return [0, 0, 1];
  return [0, 1, 0];
}

function bboxCorners(b) {
  const out = [];
  for (const x of [b[0], b[3]]) for (const y of [b[1], b[4]]) for (const z of [b[2], b[5]]) out.push([x, y, z]);
  return out;
}

function sub(a, b) {
  return [a[0] - b[0], a[1] - b[1], a[2] - b[2]];
}
function dot(a, b) {
  return a[0] * b[0] + a[1] * b[1] + a[2] * b[2];
}
function cross(a, b) {
  return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]];
}
function norm(a) {
  const l = Math.hypot(a[0], a[1], a[2]) || 1;
  return [a[0] / l, a[1] / l, a[2] / l];
}

/** A usable bounds [x0, y0, z0, x1, y1, z1] (at least a small cube). */
export function safeBounds(bounds) {
  const b = Array.isArray(bounds) && bounds.length === 6 && bounds.every((v) => Number.isFinite(Number(v))) ? bounds.map(Number) : [-1, 0, -1, 1, 1, 1];
  for (let i = 0; i < 3; i += 1) {
    if (b[i + 3] - b[i] < 1e-3) {
      b[i] -= 0.5;
      b[i + 3] += 0.5;
    }
  }
  return b;
}

/**
 * The framing of `bounds` for a camera spec and an aspect (w / h): {eye, target, up, half: [hw, hh],
 * near, far} for an orthographic camera (half sizes of the view in world units), or {eye, target, up,
 * fov, near, far} for a perspective one. Pure: shared by the page renderer, stills and tests.
 *
 * An orthographic view fits `points` (the objects' box corners, as the server projections fit
 * theirs, canvas_scene3d._project.View.fit) when given, else the corners of `bounds`, and centres
 * on what it fits unless the spec names a target.
 */
export function frame(bounds, spec, aspect, centreOf = null, points = null) {
  const b = safeBounds(bounds);
  let target = [(b[0] + b[3]) / 2, (b[1] + b[4]) / 2, (b[2] + b[5]) / 2];
  let aimed = false;
  if (Array.isArray(spec.target) && spec.target.length === 3 && spec.target.every((v) => Number.isFinite(Number(v)))) {
    target = spec.target.map(Number);
    aimed = true;
  } else if (typeof spec.target === "string" && centreOf && centreOf(spec.target)) {
    target = centreOf(spec.target);
    aimed = true;
  }
  const dir = viewDir(spec.az, spec.el);
  const up = upFor(spec.el);
  const right = norm(cross(up, dir));
  const camUp = norm(cross(dir, right));
  const radius = Math.hypot(b[3] - b[0], b[4] - b[1], b[5] - b[2]) / 2;
  const a = Number.isFinite(aspect) && aspect > 0 ? aspect : 1;
  if (spec.projection === "persp") {
    const fov = Math.max(5, Math.min(120, spec.fov));
    const vHalf = (fov * RAD) / 2;
    const hHalf = Math.atan(Math.tan(vHalf) * a);
    let fit = radius * (1 + MARGIN * 2) / Math.sin(Math.min(vHalf, hHalf));
    if (Array.isArray(points) && points.length) {
      // The nearest eye distance at which every point is inside the frustum (with the margin),
      // looking at the points' middle across the view unless the spec names a target.
      if (!aimed) {
        let x0 = Infinity;
        let x1 = -Infinity;
        let y0 = Infinity;
        let y1 = -Infinity;
        for (const c of points) {
          const d = sub(c, target);
          x0 = Math.min(x0, dot(d, right));
          x1 = Math.max(x1, dot(d, right));
          y0 = Math.min(y0, dot(d, camUp));
          y1 = Math.max(y1, dot(d, camUp));
        }
        target = [0, 1, 2].map((i) => target[i] + right[i] * ((x0 + x1) / 2) + camUp[i] * ((y0 + y1) / 2));
      }
      const tanH = Math.tan(hHalf);
      const tanV = Math.tan(vHalf);
      let need = 0;
      for (const c of points) {
        const d = sub(c, target);
        const lateral = Math.max(Math.abs(dot(d, right)) / tanH, Math.abs(dot(d, camUp)) / tanV) * (1 + MARGIN * 2);
        need = Math.max(need, dot(d, dir) + lateral);
      }
      if (need > 0) fit = need;
    }
    const dist = fit / spec.zoom;
    const eye = [target[0] + dir[0] * dist, target[1] + dir[1] * dist, target[2] + dir[2] * dist];
    return { projection: "persp", eye, target, up: camUp, fov, aspect: a, near: Math.max(0.01, dist - radius * 4), far: dist + radius * 4 };
  }
  // Orthographic: the points' spread across the view plane, around the target (or around their
  // own middle, which then becomes the target).
  const pts = Array.isArray(points) && points.length ? points : bboxCorners(b);
  let x0 = Infinity;
  let x1 = -Infinity;
  let y0 = Infinity;
  let y1 = -Infinity;
  for (const c of pts) {
    const d = sub(c, target);
    const rx = dot(d, right);
    const ry = dot(d, camUp);
    x0 = Math.min(x0, rx);
    x1 = Math.max(x1, rx);
    y0 = Math.min(y0, ry);
    y1 = Math.max(y1, ry);
  }
  let hw;
  let hh;
  if (aimed) {
    hw = Math.max(Math.abs(x0), Math.abs(x1));
    hh = Math.max(Math.abs(y0), Math.abs(y1));
  } else {
    const mx = (x0 + x1) / 2;
    const my = (y0 + y1) / 2;
    target = [0, 1, 2].map((i) => target[i] + right[i] * mx + camUp[i] * my);
    hw = (x1 - x0) / 2;
    hh = (y1 - y0) / 2;
  }
  hw = (Math.max(hw, 1e-3) * (1 + MARGIN * 2)) / spec.zoom;
  hh = (Math.max(hh, 1e-3) * (1 + MARGIN * 2)) / spec.zoom;
  // `fit` is the content's own half size; `half` widens it to the aspect (fitAspect starts again
  // from `fit`, so a framing made for one aspect fits another as tightly).
  const fit = [hw, hh];
  if (hw / hh > a) hh = hw / a;
  else hw = hh * a;
  const dist = radius * 4 + 1;
  const eye = [target[0] + dir[0] * dist, target[1] + dir[1] * dist, target[2] + dir[2] * dist];
  return { projection: "ortho", eye, target, up: camUp, half: [hw, hh], fit, near: 0.01, far: dist + radius * 4 + 1 };
}

/** A three.js camera for a framing (made new, or `camera` updated in place when it is the right type). */
export function makeCamera(f, camera = null) {
  let cam = camera;
  if (f.projection === "persp") {
    if (!cam || !cam.isPerspectiveCamera) cam = new PerspectiveCamera(f.fov, 1, f.near, f.far);
    cam.fov = f.fov;
    if (Number.isFinite(f.aspect) && f.aspect > 0) cam.aspect = f.aspect;
  } else {
    if (!cam || !cam.isOrthographicCamera) cam = new OrthographicCamera(-1, 1, 1, -1, f.near, f.far);
    cam.left = -f.half[0];
    cam.right = f.half[0];
    cam.top = f.half[1];
    cam.bottom = -f.half[1];
    cam.zoom = 1;
  }
  cam.near = f.near;
  cam.far = f.far;
  cam.position.set(...f.eye);
  cam.up.set(...f.up);
  cam.lookAt(new Vector3(...f.target));
  cam.updateProjectionMatrix();
  cam.updateMatrixWorld(true);
  return cam;
}

/** Frames a camera for an aspect again (a viewport of another shape): ortho widens, persp re-aspects. */
export function fitAspect(camera, f, aspect) {
  if (!camera) return;
  const a = Number.isFinite(aspect) && aspect > 0 ? aspect : 1;
  if (camera.isPerspectiveCamera) {
    camera.aspect = a;
  } else if (f && (f.fit || f.half)) {
    let [hw, hh] = f.fit || f.half;
    if (hw / hh > a) hh = hw / a;
    else hw = hh * a;
    camera.left = -hw;
    camera.right = hw;
    camera.top = hh;
    camera.bottom = -hh;
  }
  camera.updateProjectionMatrix();
}

/** A camera's az, el and zoom relative to a framing's target (what "Save view" sends). */
export function orbitOf(camera, target, baseDist = null) {
  const t = Array.isArray(target) ? target : [0, 0, 0];
  const d = [camera.position.x - t[0], camera.position.y - t[1], camera.position.z - t[2]];
  const dist = Math.hypot(d[0], d[1], d[2]) || 1;
  const el = Math.asin(Math.max(-1, Math.min(1, d[1] / dist))) / RAD;
  let az = Math.atan2(d[0], d[2]) / RAD;
  if (az < 0) az += 360;
  let zoom = camera.zoom || 1;
  if (camera.isPerspectiveCamera && baseDist) zoom = baseDist / dist;
  const round = (v, n) => Math.round(v * 10 ** n) / 10 ** n;
  return { az: round(az, 1), el: round(el, 1), zoom: round(Math.max(0.05, Math.min(20, zoom)), 2) };
}
