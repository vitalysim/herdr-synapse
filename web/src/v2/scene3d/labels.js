// Text in a scene: canvas textures drawn in "Synapse Sans" (the page's bundled Inter). Object
// labels are billboards kept at a constant 12 px on screen (tokens scene3d.label.size_px): the
// renderer places and rescales them for each viewport before it draws (sizeLabels). Where there is
// no DOM (node tests) the texture is null and the label draws as a plain sprite.
//
// Placement is the server projection's (canvas_scene3d._project, canvas-v2-phase3-4.md 3.8 point 5,
// QA phase34 N1), in screen pixels: above the object's projected top, lifted in steps past other
// labels (with a leader line back once it is well clear), then just under its outline, then beside
// it on a leader line, right then left. A spot that covers no other object (what holds the labelled
// one up aside), leader included, wins over one that does; with none, the spot that covers the least
// of them. Only other labels and the viewport's edge rule a spot out. A label that meets another
// label everywhere is not drawn (labels: all draws it anyway, at its first spot). So a label never
// sits on another label, and on another object only where every spot would.
import { BufferGeometry, CanvasTexture, Float32BufferAttribute, LinearFilter, Line, LineBasicMaterial, SRGBColorSpace, Sprite, SpriteMaterial, Vector3 } from "three";
import tokens from "@synapse/tokens";

const FONT = '"Synapse Sans", "Inter", system-ui, sans-serif';
const PX = 48; // texture px per text line (drawn big, shown small: crisp at 2x DPR and zoom)
const GLYPH = 0.72; // the glyphs' share of the texture's height
const PAD_EM = 0.2; // the texture's side padding, in texture heights, each side

// The server projection's label geometry, in units of a 12 px label (scaled to the label's size):
// the pill's height (canvas_text.line_height(12)) and its side padding, the step a lifted label
// moves by, how many lifts it tries, and the leader line's length.
export const LABEL = { size: 12, height: 15, pad: 4, step: 8, tries: 4, leader: 16 };

export function labelSizePx() {
  const px = Number(tokens?.scene3d?.label?.size_px);
  return Number.isFinite(px) && px > 0 ? px : 12;
}

function canvas2d(w, h) {
  if (typeof document === "undefined") return null;
  const c = document.createElement("canvas");
  c.width = w;
  c.height = h;
  const ctx = c.getContext("2d");
  return ctx ? { c, ctx } : null;
}

/** A texture of one line of text, `aspect` (w / h) wide when given (else as the text measures). */
export function textTexture(text, { color = "#1c2024", aspect = null, weight = 500, halo = null } = {}) {
  const probe = canvas2d(4, 4);
  if (!probe) return null;
  probe.ctx.font = `${weight} ${PX * GLYPH}px ${FONT}`;
  const measured = Math.ceil(probe.ctx.measureText(text).width) + PX * PAD_EM * 2;
  const w = Math.max(8, Math.min(4096, Math.round(aspect ? PX * aspect : measured)));
  const drawn = canvas2d(w, PX);
  if (!drawn) return null;
  const { c, ctx } = drawn;
  ctx.font = `${weight} ${PX * GLYPH}px ${FONT}`;
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  if (halo) {
    ctx.lineWidth = PX * 0.16;
    ctx.lineJoin = "round";
    ctx.strokeStyle = halo;
    ctx.strokeText(text, w / 2, PX / 2, w - 4);
  }
  ctx.fillStyle = color;
  ctx.fillText(text, w / 2, PX / 2, w - 4);
  const texture = new CanvasTexture(c);
  texture.colorSpace = SRGBColorSpace;
  texture.minFilter = LinearFilter;
  texture.generateMipmaps = false;
  texture.userData.aspect = w / PX;
  return texture;
}

/** A billboard label for an object or a link; the renderer places and scales it (sizeLabels). */
export function labelSprite(text, palette) {
  const ink = palette?.["base.ink"] || "#1c2024";
  const halo = palette?.["base.surface"] || "#ffffff";
  const shown = String(text).slice(0, 80);
  const map = textTexture(shown, { color: ink, halo });
  const sprite = new Sprite(new SpriteMaterial({ map, transparent: true, depthTest: false, depthWrite: false }));
  sprite.center.set(0.5, 0.5);
  sprite.renderOrder = 10;
  sprite.userData.label = String(text);
  sprite.userData.aspect = map ? map.userData.aspect : Math.max(1, shown.length * 0.55 * GLYPH + PAD_EM * 2);
  // The text's own width in ems of its size (without the texture's padding).
  sprite.userData.textEm = Math.max(0.5, (sprite.userData.aspect - PAD_EM * 2) / GLYPH);
  return sprite;
}

/** The thin line from a label placed away from its object back to it (hidden until placed). */
export function leaderLine(palette) {
  const geometry = new BufferGeometry();
  geometry.setAttribute("position", new Float32BufferAttribute([0, 0, 0, 0, 0, 0], 3));
  // Ink, not the muted ink the server's pills use: a leader crosses floors and objects, where a
  // mid grey line would vanish.
  const colour = palette?.["base.ink"] || "#1c2024";
  const line = new Line(geometry, new LineBasicMaterial({ color: colour, transparent: true, depthTest: false, depthWrite: false }));
  line.renderOrder = 9;
  line.visible = false;
  line.frustumCulled = false;
  line.raycast = () => {};
  return line;
}

// -- placement (pure, screen px, y down) --------------------------------------------------------------

const meets = (a, b) => a[0] < b[2] && b[0] < a[2] && a[1] < b[3] && b[1] < a[3];

/** Whether the segment a–b passes through `rect` shrunk by `inset` (a leader may start at its own object's edge). */
export function segmentMeets(a, b, rect, inset = 2) {
  const x0 = rect[0] + inset;
  const y0 = rect[1] + inset;
  const x1 = rect[2] - inset;
  const y1 = rect[3] - inset;
  if (x0 >= x1 || y0 >= y1) return false;
  const dx = b[0] - a[0];
  const dy = b[1] - a[1];
  let t0 = 0;
  let t1 = 1;
  for (const [p, q] of [[-dx, a[0] - x0], [dx, x1 - a[0]], [-dy, a[1] - y0], [dy, y1 - a[1]]]) {
    if (Math.abs(p) < 1e-12) {
      if (q < 0) return false;
      continue;
    }
    const t = q / p;
    if (p < 0) t0 = Math.max(t0, t);
    else t1 = Math.min(t1, t);
    if (t0 > t1) return false;
  }
  return true;
}

/**
 * The lowest point on screen (largest y) of the outline of `corners` (a convex solid's projected
 * corners) between x0 and x1, or null where it does not reach: a label under its object sits just
 * under the outline, not under its bounding box (QA phase34 L11).
 */
export function bottomUnder(corners, x0, x1) {
  if (!corners || corners.length < 2) return null;
  let lo = Infinity;
  let hi = -Infinity;
  for (const c of corners) {
    lo = Math.min(lo, c[0]);
    hi = Math.max(hi, c[0]);
  }
  const a = Math.max(x0, lo);
  const b = Math.min(x1, hi);
  if (a > b) return null;
  const xs = [a, b, ...corners.map((c) => c[0]).filter((x) => a < x && x < b)];
  let best = null;
  for (let i = 0; i < corners.length; i += 1) {
    for (let j = i + 1; j < corners.length; j += 1) {
      const [left, right] = corners[i][0] <= corners[j][0] ? [corners[i], corners[j]] : [corners[j], corners[i]];
      for (const x of xs) {
        if (left[0] - 1e-9 <= x && x <= right[0] + 1e-9) {
          const span = right[0] - left[0];
          const y = span < 1e-9 ? Math.max(left[1], right[1]) : left[1] + ((right[1] - left[1]) * (x - left[0])) / span;
          if (best === null || y > best) best = y;
        }
      }
    }
  }
  return best;
}

/** The spots a label of `w` × `h` px tries, in order, each with its leader line (or null). */
export function labelSpots(w, h, anchor, box, outline, corners, k = 1) {
  const pad = LABEL.pad * k;
  const step = LABEL.step * k;
  const leader = LABEL.leader * k;
  const bx0 = box[0];
  const bx1 = box[0] + box[2];
  let first = [anchor[0] - w / 2, anchor[1] - pad - h, anchor[0] + w / 2, anchor[1] - pad];
  // Kept inside the box sideways before anything else: a label near an edge slides in.
  const shift = Math.max(0, bx0 - first[0]) - Math.max(0, first[2] - bx1);
  first = [first[0] + shift, first[1], first[2] + shift, first[3]];
  const out = [];
  for (let i = 0; i <= LABEL.tries; i += 1) {
    const rect = [first[0], first[1] - i * step, first[2], first[3] - i * step];
    out.push({ rect, leader: i >= 2 ? [anchor, [(rect[0] + rect[2]) / 2, rect[3]]] : null });
  }
  const [ox0, oy0, ox1, oy1] = outline || [anchor[0], anchor[1], anchor[0], anchor[1]];
  const found = bottomUnder(corners, first[0], first[2]);
  const under = found === null ? oy1 : Math.min(oy1, found);
  for (let i = 0; i < 3; i += 1) {
    const top = under + pad + i * step;
    out.push({ rect: [first[0], top, first[2], top + h], leader: null });
  }
  const mid = (oy0 + oy1) / 2;
  for (const dy of [0, -step, step]) {
    const cy = mid + dy;
    const rect = [ox1 + leader, cy - h / 2, ox1 + leader + w, cy + h / 2];
    out.push({ rect, leader: [[ox1 - Math.min(leader, (ox1 - ox0) / 2), cy], [rect[0], cy]] });
  }
  for (const dy of [0, -step, step]) {
    const cy = mid + dy;
    const rect = [ox0 - leader - w, cy - h / 2, ox0 - leader, cy + h / 2];
    out.push({ rect, leader: [[ox0 + Math.min(leader, (ox1 - ox0) / 2), cy], [rect[2], cy]] });
  }
  return out;
}

// How much of the objects in `avoid` a label at a spot covers (canvas_scene3d._project._covered):
// the area its rectangle shares with each, and a quarter of its own area per object its leader crosses.
function covered({ rect, leader }, avoid) {
  let area = 0;
  for (const o of avoid) {
    const w = Math.min(rect[2], o[2]) - Math.max(rect[0], o[0]);
    const h = Math.min(rect[3], o[3]) - Math.max(rect[1], o[1]);
    if (w > 0 && h > 0) area += w * h;
    if (leader && segmentMeets(leader[0], leader[1], o)) area += 0.25 * (rect[2] - rect[0]) * (rect[3] - rect[1]);
  }
  return area;
}

/**
 * Places labels one after another (the server's order: objects, then links), each against the
 * labels placed before it and the objects it should keep clear of. `items`: [{w, h, anchor,
 * outline, corners, avoid: [rect], keep}] in screen px (y down), `box` [x, y, w, h] the viewport,
 * `k` the label's size over 12 px. Returns [{rect, leader, crowded}] with rect null for a label
 * that meets another label wherever it goes (unless `keep`: then its first spot, crowded).
 */
export function placeLabels(items, box, k = 1) {
  const [bx0, by0] = box;
  const bx1 = box[0] + box[2];
  const by1 = box[1] + box[3];
  const inside = (r) => r[0] >= bx0 - 0.01 && r[1] >= by0 - 0.01 && r[2] <= bx1 + 0.01 && r[3] <= by1 + 0.01;
  const placed = [];
  return items.map((item) => {
    const spots = labelSpots(item.w, item.h, item.anchor, box, item.outline, item.corners, k);
    const reach = [Infinity, Infinity, -Infinity, -Infinity];
    for (const { rect } of spots) {
      reach[0] = Math.min(reach[0], rect[0] - 1);
      reach[1] = Math.min(reach[1], rect[1] - 1);
      reach[2] = Math.max(reach[2], rect[2] + 1);
      reach[3] = Math.max(reach[3], rect[3] + 1);
    }
    const near = placed.filter((r) => meets(reach, r));
    const avoid = (item.avoid || []).filter((r) => meets(reach, r));
    // The first spot clear of every other object; with none, the one that covers the least of them.
    let best = null;
    let least = Infinity;
    for (const spot of spots) {
      if (!inside(spot.rect) || near.some((r) => meets(spot.rect, r))) continue;
      const cost = covered(spot, avoid);
      if (cost === 0) {
        best = spot;
        break;
      }
      if (cost < least) {
        least = cost;
        best = spot;
      }
    }
    if (best) {
      placed.push(best.rect);
      return { rect: best.rect, leader: best.leader, crowded: false };
    }
    if (item.keep) {
      placed.push(spots[0].rect);
      return { rect: spots[0].rect, leader: null, crowded: true };
    }
    return { rect: null, leader: null, crowded: false };
  });
}

// -- the scene's labels on screen ---------------------------------------------------------------------

function projector(camera, vw, vh) {
  const v = new Vector3();
  return {
    // A world point as [x, y] screen px (y down) and its depth in normalised device coordinates.
    at(p) {
      v.set(p[0], p[1], p[2]).project(camera);
      return [((v.x + 1) / 2) * vw, ((1 - v.y) / 2) * vh, v.z];
    },
    // A screen point back into the world at a depth.
    world(x, y, z) {
      return new Vector3((x / vw) * 2 - 1, 1 - (y / vh) * 2, z).unproject(camera);
    },
  };
}

function corners(a) {
  const out = [];
  for (const x of [a[0], a[3]]) for (const y of [a[1], a[4]]) for (const z of [a[2], a[5]]) out.push([x, y, z]);
  return out;
}

function boundsOf(points) {
  const r = [Infinity, Infinity, -Infinity, -Infinity];
  for (const p of points) {
    r[0] = Math.min(r[0], p[0]);
    r[1] = Math.min(r[1], p[1]);
    r[2] = Math.max(r[2], p[0]);
    r[3] = Math.max(r[3], p[1]);
  }
  return r;
}

/**
 * Where every label of a built scene goes for a camera and a viewport of `vh` px (its width from
 * the camera's aspect), labels `px` tall: [{sprite, rect, leader, depth}] in screen px, as
 * placeLabels found them. `scene.userData.plan` (scene.js) says what each label is for, which
 * objects are solid and what holds each one up.
 */
export function layoutLabels(scene, camera, vh, px = labelSizePx()) {
  const plan = scene?.userData?.plan;
  const labels = scene?.userData?.labels;
  if (!plan || !Array.isArray(labels) || !labels.length || !(vh > 0)) return [];
  camera.updateMatrixWorld();
  const aspect = camera.isOrthographicCamera ? (camera.right - camera.left) / Math.max(1e-9, camera.top - camera.bottom) : camera.aspect || 1;
  const vw = vh * aspect;
  const k = px / LABEL.size;
  const to = projector(camera, vw, vh);
  const solids = [];
  const shapes = new Map(); // id -> {aabb, corners, outline} on screen
  for (const o of plan.objects) {
    const pts = corners(o.aabb).map((c) => to.at(c));
    const shape = { aabb: o.aabb, corners: pts, outline: boundsOf(pts) };
    shapes.set(o.id, shape);
    if (o.solid) solids.push([o.id, shape.outline]);
  }
  const items = [];
  const shown = [];
  for (const entry of plan.labels) {
    const sprite = entry.sprite;
    if (sprite.visible === false) continue;
    const w = sprite.userData.textEm * px + 2 * LABEL.pad * k;
    const h = LABEL.height * k;
    if (entry.id != null) {
      const shape = shapes.get(entry.id);
      if (!shape) continue;
      const a = shape.aabb;
      const top = to.at([(a[0] + a[3]) / 2, a[4], (a[2] + a[5]) / 2]);
      const own = plan.own.get(entry.id) || new Set([entry.id]);
      items.push({ w, h, anchor: [top[0], shape.outline[1]], outline: shape.outline, corners: shape.corners, avoid: solids.filter(([id]) => !own.has(id)).map(([, r]) => r), keep: plan.keep });
      shown.push({ sprite, depth: top[2] });
    } else {
      const [p, q] = [to.at(entry.points[0]), to.at(entry.points[entry.points.length - 1])];
      items.push({ w, h, anchor: [(p[0] + q[0]) / 2, (p[1] + q[1]) / 2 + (LABEL.height * k) / 2], outline: null, corners: null, avoid: [], keep: plan.keep });
      shown.push({ sprite, depth: (p[2] + q[2]) / 2 });
    }
  }
  const placed = placeLabels(items, [0, 0, vw, vh], k);
  return shown.map((s, i) => ({ ...s, ...placed[i] }));
}

/**
 * Places and scales every label sprite in `scene` for a camera and a viewport of `vh` px, labels
 * `px` screen pixels tall (layoutLabels), and draws the leader lines of those placed away from
 * their objects. A label that finds no spot is hidden for this frame.
 */
export function sizeLabels(scene, camera, vh, px = labelSizePx()) {
  const plan = scene?.userData?.plan;
  if (!plan) return;
  for (const entry of plan.labels) if (entry.line) entry.line.visible = false;
  const found = layoutLabels(scene, camera, vh, px);
  if (!found.length) return;
  const aspect = camera.isOrthographicCamera ? (camera.right - camera.left) / Math.max(1e-9, camera.top - camera.bottom) : camera.aspect || 1;
  const to = projector(camera, vh * aspect, vh);
  const hPx = px / GLYPH; // the texture's height on screen: its glyphs are px tall
  const lines = new Map(plan.labels.map((e) => [e.sprite, e.line]));
  for (const { sprite, rect, leader, depth } of found) {
    if (!rect) {
      sprite.visible = false;
      continue;
    }
    const at = to.world((rect[0] + rect[2]) / 2, (rect[1] + rect[3]) / 2, depth);
    sprite.position.copy(at);
    let upp;
    if (camera.isOrthographicCamera) upp = (camera.top - camera.bottom) / (camera.zoom || 1) / vh;
    else {
      // World units per screen pixel at the label's depth (a perspective camera).
      const d = at.clone().sub(camera.position).dot(camera.getWorldDirection(new Vector3()));
      upp = (2 * Math.tan(((camera.fov || 35) * Math.PI) / 360) * Math.max(1e-6, d)) / vh;
    }
    sprite.scale.set(hPx * upp * sprite.userData.aspect, hPx * upp, 1);
    const line = lines.get(sprite);
    if (line && leader) {
      const a = to.world(leader[0][0], leader[0][1], depth);
      const b = to.world(leader[1][0], leader[1][1], depth);
      const pos = line.geometry.attributes.position;
      pos.setXYZ(0, a.x, a.y, a.z);
      pos.setXYZ(1, b.x, b.y, b.z);
      pos.needsUpdate = true;
      line.geometry.computeBoundingSphere();
      line.visible = true;
    }
  }
}
