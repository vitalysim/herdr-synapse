// buildScene (canvas-v2-phase3-4.md 3.7): a scene3d element as a three.js scene. It reads the solved
// transforms the server computed (`solved`, docs/scene3d.md) and the object specs (`objects`), builds
// each object through its primitive, the links as arrows, the ground grid and the lights, and frames
// the camera. There is no layout here: every position comes from the server.
import { BufferGeometry, Box3, Float32BufferAttribute, Group, LineSegments, Scene, Vector3 } from "three";
import { arrowBetween } from "./primitives/arrow3d.js";
import { primitiveOf } from "./primitives/index.js";
import { cameraSpec, frame, makeCamera, safeBounds } from "./cameras.js";
import { buildLights } from "./lights.js";
import { createMaterials } from "./materials.js";
import { labelSprite } from "./labels.js";

const RAD = Math.PI / 180;
const isNum = (v) => Number.isFinite(Number(v));
const vec = (v, n) => (Array.isArray(v) && v.length === n && v.every(isNum) ? v.map(Number) : null);

// The stored (compact) solved scene, docs/scene3d.md (canvas_scene3d._solver.compact):
//   objects: rows [id, pos, rot, ext, parent, rel, {points}?]   (pos: footprint centre at the base)
//   models:  {id: {asset: "<32hex>.glb", tris, ...}}
//   links:   rows [key, from, to, points, label, tone]
// The dict form `solve` returns ({objects: {id: {pos, rot, ext, aabb, ...}}, links: [{key, points, ...}]})
// is read too.
const OBJECT_FIELDS = ["id", "pos", "rot", "ext", "parent", "rel"];
const LINK_FIELDS = ["key", "from", "to", "points", "label", "tone"];

function asRecord(item, fields) {
  if (Array.isArray(item)) {
    const out = Object.fromEntries(fields.map((f, i) => [f, item[i] ?? null]));
    const extra = item[fields.length];
    if (extra && typeof extra === "object" && !Array.isArray(extra)) Object.assign(out, extra);
    return out;
  }
  return item && typeof item === "object" ? item : null;
}

// canvas_scene3d._vec.box_at: the box of an object whose footprint centre is (x, z) at base y.
function boxAt(pos, ext) {
  return [pos[0] - ext[0] / 2, pos[1], pos[2] - ext[2] / 2, pos[0] + ext[0] / 2, pos[1] + ext[1], pos[2] + ext[2] / 2];
}

/**
 * The element's solved scene as {bounds, objects: Map id -> {pos, rot, ext, aabb, rel, parent, asset,
 * tris, points}, links: [{key, points, label, tone}], notes, conflicts}. Accepts the dict form
 * (3.3) and the compact list form, with an optional `fields` / `link_fields` header naming the order.
 */
export function readSolved(element) {
  const s = element && typeof element.solved === "object" && element.solved ? element.solved : {};
  const objects = new Map();
  const objFields = Array.isArray(s.fields) ? s.fields : Array.isArray(s.object_fields) ? s.object_fields : OBJECT_FIELDS;
  const linkFields = Array.isArray(s.link_fields) ? s.link_fields : LINK_FIELDS;
  if (Array.isArray(s.objects)) {
    for (const item of s.objects) {
      const r = asRecord(item, objFields);
      if (r && r.id != null) objects.set(String(r.id), r);
    }
  } else if (s.objects && typeof s.objects === "object") {
    for (const [id, r] of Object.entries(s.objects)) if (r && typeof r === "object") objects.set(id, Array.isArray(r) ? asRecord([id, ...r], objFields) : { id, ...r });
  }
  const models = s.models && typeof s.models === "object" && !Array.isArray(s.models) ? s.models : {};
  for (const [id, r] of objects) {
    const model = models[id] && typeof models[id] === "object" ? models[id] : {};
    const pos = vec(r.pos, 3) || [0, 0, 0];
    const ext = vec(r.ext, 3);
    const asset = typeof r.asset === "string" ? r.asset : typeof model.asset === "string" ? model.asset : null;
    const tris = isNum(r.tris) ? Number(r.tris) : isNum(model.tris) ? Number(model.tris) : null;
    objects.set(id, {
      id,
      pos,
      rot: typeof r.rot === "number" ? [0, Number(r.rot), 0] : vec(r.rot, 3) || [0, 0, 0],
      ext,
      aabb: vec(r.aabb, 6) || (ext ? boxAt(pos, ext) : null),
      rel: typeof r.rel === "string" && r.rel ? r.rel : null,
      parent: r.parent == null ? null : String(r.parent),
      asset,
      tris,
      points: Array.isArray(r.points) ? r.points : null,
    });
  }
  const links = [];
  for (const item of Array.isArray(s.links) ? s.links : []) {
    const r = asRecord(item, linkFields);
    if (!r || !Array.isArray(r.points) || r.points.length < 2) continue;
    links.push({ key: String(r.key ?? ""), points: r.points.map((p) => vec(p, 3)).filter(Boolean), label: typeof r.label === "string" ? r.label : null, tone: typeof r.tone === "string" ? r.tone : "neutral" });
  }
  return {
    bounds: vec(s.bounds, 6),
    objects,
    links,
    notes: Array.isArray(s.notes) ? s.notes : [],
    conflicts: Array.isArray(s.conflicts) ? s.conflicts : [],
  };
}

/** The element's settings (camera, lights, ground, labels, units), wherever the kind keeps them. */
export function settingsOf(element) {
  const s = element?.settings && typeof element.settings === "object" ? element.settings : {};
  const pick = (k, d) => (s[k] !== undefined ? s[k] : element?.[k] !== undefined ? element[k] : d);
  return { camera: pick("camera", "iso"), lights: pick("lights", "studio"), ground: pick("ground", true) !== false, labels: pick("labels", "auto"), units: pick("units", "m") };
}

/** The object specs by id (element.objects is a list of {id, shape, ...}, or a map). */
export function objectsOf(element) {
  const raw = element?.objects;
  const out = new Map();
  if (Array.isArray(raw)) for (const o of raw) if (o && o.id != null) out.set(String(o.id), o);
  if (raw && !Array.isArray(raw) && typeof raw === "object") for (const [id, o] of Object.entries(raw)) if (o && typeof o === "object") out.set(id, { id, ...o });
  return out;
}

/** The GLB asset names a scene needs. */
export function assetsOf(element) {
  const out = new Set();
  for (const o of readSolved(element).objects.values()) if (o.asset) out.add(o.asset);
  return [...out];
}

function boundsOf(solved, objects) {
  if (solved.bounds) return solved.bounds;
  const b = [Infinity, Infinity, Infinity, -Infinity, -Infinity, -Infinity];
  for (const o of objects.values()) {
    const a = o.aabb || (o.ext ? [o.pos[0] - o.ext[0] / 2, o.pos[1], o.pos[2] - o.ext[2] / 2, o.pos[0] + o.ext[0] / 2, o.pos[1] + o.ext[1], o.pos[2] + o.ext[2] / 2] : null);
    if (!a) continue;
    for (let i = 0; i < 3; i += 1) {
      b[i] = Math.min(b[i], a[i]);
      b[i + 3] = Math.max(b[i + 3], a[i + 3]);
    }
  }
  return b.every(Number.isFinite) ? b : null;
}

// A round grid step for a footprint: about 10 cells along the longer side.
export function gridStep(span) {
  const raw = Math.max(span, 1e-3) / 10;
  const p = 10 ** Math.floor(Math.log10(raw));
  for (const m of [1, 2, 5, 10]) if (raw <= m * p) return m * p;
  return 10 * p;
}

function groundGrid(b, material) {
  const pad = Math.max(b[3] - b[0], b[5] - b[2]) * 0.06;
  const x0 = b[0] - pad;
  const x1 = b[3] + pad;
  const z0 = b[2] - pad;
  const z1 = b[5] + pad;
  const step = gridStep(Math.max(x1 - x0, z1 - z0));
  const y = b[1] - 1e-3;
  const pts = [];
  for (let x = Math.ceil(x0 / step) * step; x <= x1 + 1e-9; x += step) pts.push(x, y, z0, x, y, z1);
  for (let z = Math.ceil(z0 / step) * step; z <= z1 + 1e-9; z += step) pts.push(x0, y, z, x1, y, z);
  const geo = new BufferGeometry();
  geo.setAttribute("position", new Float32BufferAttribute(pts, 3));
  const grid = new LineSegments(geo, material);
  grid.name = "ground";
  grid.raycast = () => {};
  return grid;
}

/** Triangles drawn by every mesh under `root` (index count / 3, else position count / 3). */
export function triangles(root) {
  let n = 0;
  root.traverse((node) => {
    if (!node.isMesh || !node.geometry || node.visible === false) return;
    const g = node.geometry;
    const count = g.index ? g.index.count : g.attributes?.position?.count || 0;
    n += Math.floor(count / 3) * (node.isInstancedMesh ? node.count : 1);
  });
  return n;
}

// Places a built object: its native box's centre on the solved AABB's centre, rotated by `rot`.
function place(built, entry, def) {
  if (def.absolute) return built;
  const native = new Box3().setFromObject(built);
  const pivot = new Group();
  pivot.name = `${entry.id}:pivot`;
  if (!native.isEmpty()) {
    const c = native.getCenter(new Vector3());
    built.position.sub(c);
  }
  pivot.add(built);
  const [rx, ry, rz] = entry.rot;
  pivot.rotation.set(rx * RAD, ry * RAD, rz * RAD);
  const ext = entry.ext || (native.isEmpty() ? [0, 0, 0] : native.getSize(new Vector3()).toArray());
  const a = entry.aabb;
  const centre = a ? [(a[0] + a[3]) / 2, (a[1] + a[4]) / 2, (a[2] + a[5]) / 2] : [entry.pos[0], entry.pos[1] + ext[1] / 2, entry.pos[2]];
  pivot.position.set(...centre);
  return pivot;
}

function topOf(entry) {
  if (entry.aabb) return [(entry.aabb[0] + entry.aabb[3]) / 2, entry.aabb[4], (entry.aabb[2] + entry.aabb[5]) / 2];
  const h = entry.ext ? entry.ext[1] : 1;
  return [entry.pos[0], entry.pos[1] + h, entry.pos[2]];
}

/**
 * The scene for an element: {scene, camera, framing, spec, bounds, tris, objects: Map id -> Object3D,
 * models: [{id, node}], labels, dispose()}. `assets` maps a GLB asset name to its loaded model (a
 * three Group); a missing one draws its proxy box. `palette` is the page's flat {ref: hex}.
 */
export function buildScene(element, assets, palette, { aspect = 1, camera: cameraOverride = null } = {}) {
  const solved = readSolved(element);
  const specs = objectsOf(element);
  const settings = settingsOf(element);
  const materials = createMaterials(palette || {});
  const scene = new Scene();
  scene.name = `scene3d:${element?.id ?? ""}`;
  scene.userData.labels = [];
  scene.userData.texts = [];
  const ctx = { palette: palette || {}, materials, assets: assets instanceof Map ? assets : new Map(), element };
  const nodes = new Map();
  const models = [];
  const bounds = safeBounds(boundsOf(solved, solved.objects));

  const ids = solved.objects.size ? [...solved.objects.keys()] : [...specs.keys()];
  for (const id of ids) {
    const spec = specs.get(id) || { id, shape: "box" };
    const entry = solved.objects.get(id) || { id, pos: [0, 0, 0], rot: [0, 0, 0], ext: null, aabb: null };
    const def = primitiveOf(spec.shape);
    let built;
    try {
      built = def.build(spec, entry, ctx);
    } catch (err) {
      built = primitiveOf("box").build({ ...spec, size: entry.ext || [1, 1, 1] }, entry, ctx);
    }
    const node = place(built, entry, def);
    node.userData.id = id;
    node.userData.shape = spec.shape;
    scene.add(node);
    nodes.set(id, node);
    if (spec.shape === "text3d") scene.userData.texts.push(node);
    if (def.name === "gltf" && built.userData.model) models.push({ id, node: built, tris: built.userData.tris || triangles(built.userData.model) });
    const wantLabel = settings.labels === "all" ? spec.label || id : settings.labels === "none" ? null : spec.label;
    if (wantLabel && spec.shape !== "text3d") {
      const sprite = labelSprite(wantLabel, palette);
      const top = topOf(entry);
      sprite.position.set(top[0], top[1] + (bounds[4] - bounds[1]) * 0.03, top[2]);
      sprite.userData.for = id;
      scene.add(sprite);
      scene.userData.labels.push(sprite);
    }
  }

  const linkNodes = [];
  for (const link of solved.links) {
    const pts = link.points;
    const material = materials.surface({ tone: link.tone || "neutral", finish: "matte" });
    const group = new Group();
    group.name = `link:${link.key}`;
    const span = Math.max(bounds[3] - bounds[0], bounds[5] - bounds[2], bounds[4] - bounds[1]);
    for (let i = 0; i + 1 < pts.length; i += 1) {
      const last = i + 2 === pts.length;
      const arrow = arrowBetween(pts[i], pts[i + 1], material, { radius: span * 0.004, head: last ? span * 0.025 : 0 });
      if (arrow) group.add(arrow);
    }
    scene.add(group);
    linkNodes.push(group);
    if (link.label && settings.labels !== "none") {
      const a = pts[Math.floor((pts.length - 1) / 2)];
      const b = pts[Math.floor((pts.length - 1) / 2) + 1] || a;
      const sprite = labelSprite(link.label, palette);
      sprite.position.set((a[0] + b[0]) / 2, (a[1] + b[1]) / 2, (a[2] + b[2]) / 2);
      scene.add(sprite);
      scene.userData.labels.push(sprite);
    }
  }

  if (settings.ground) scene.add(groundGrid(bounds, materials.line("base.grid", "#e3e5e9")));
  const centre = [(bounds[0] + bounds[3]) / 2, (bounds[1] + bounds[4]) / 2, (bounds[2] + bounds[5]) / 2];
  const radius = Math.hypot(bounds[3] - bounds[0], bounds[4] - bounds[1], bounds[5] - bounds[2]) / 2;
  scene.add(buildLights(typeof settings.lights === "string" ? settings.lights : "studio", centre, radius));

  const spec = cameraSpec(settings.camera, cameraOverride);
  const centreOf = (id) => {
    const e = solved.objects.get(id);
    return e ? (e.aabb ? [(e.aabb[0] + e.aabb[3]) / 2, (e.aabb[1] + e.aabb[4]) / 2, (e.aabb[2] + e.aabb[5]) / 2] : e.pos) : null;
  };
  // Labels sit above their objects: the view keeps room for them over the top of the bounds.
  const framed = scene.userData.labels.length ? [bounds[0], bounds[1], bounds[2], bounds[3], bounds[4] + Math.max(bounds[4] - bounds[1], bounds[3] - bounds[0], bounds[5] - bounds[2]) * 0.12, bounds[5]] : bounds;
  // What an orthographic view fits: every object's box corners and every label's anchor lifted by
  // a label's room (the server projections fit the same corners).
  const lift = (framed[4] - bounds[4]);
  const fitPoints = [];
  for (const entry of solved.objects.values()) {
    const a = entry.aabb;
    if (!a) continue;
    for (const x of [a[0], a[3]]) for (const y of [a[1], a[4]]) for (const z of [a[2], a[5]]) fitPoints.push([x, y, z]);
  }
  for (const sprite of scene.userData.labels) fitPoints.push([sprite.position.x, sprite.position.y + lift, sprite.position.z]);
  let framing = frame(framed, spec, aspect, centreOf, fitPoints);
  let framedAspect = aspect;
  const camera = makeCamera(framing);
  scene.updateMatrixWorld(true);
  const tris = triangles(scene);

  return {
    scene,
    camera,
    get framing() {
      return framing;
    },
    /** The camera framed for a viewport of `aspect` (w / h): framed again only when it changes. */
    fit(a) {
      if (Number.isFinite(a) && a > 0 && Math.abs(a - framedAspect) > 1e-3) {
        framedAspect = a;
        framing = frame(framed, spec, a, centreOf, fitPoints);
        makeCamera(framing, camera);
      }
      return camera;
    },
    spec,
    bounds,
    framed,
    fitPoints,
    centreOf,
    tris,
    objects: nodes,
    links: linkNodes,
    models,
    labels: scene.userData.labels,
    /** Draw every model as its proxy (past the frame's triangle budget) or as itself. */
    proxyModels(on) {
      for (const { node } of models) {
        if (node.userData.model) node.userData.model.visible = !on;
        if (node.userData.proxy) node.userData.proxy.visible = !!on;
      }
    },
    dispose() {
      scene.traverse((node) => {
        if (node.geometry && typeof node.geometry.dispose === "function") node.geometry.dispose();
        const mats = Array.isArray(node.material) ? node.material : node.material ? [node.material] : [];
        for (const m of mats) {
          if (m.map && typeof m.map.dispose === "function" && !m.map.userData?.shared) m.map.dispose();
        }
      });
      materials.dispose();
    },
  };
}
