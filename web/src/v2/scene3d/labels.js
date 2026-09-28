// Text in a scene: canvas textures drawn in "Synapse Sans" (the page's bundled Inter). Object
// labels are billboards kept at a constant 12 px on screen (tokens scene3d.label.size_px): the
// renderer rescales them for each viewport before it draws (sizeLabels). Where there is no DOM
// (node tests) the texture is null and the label draws as a plain sprite.
import { Box3, CanvasTexture, LinearFilter, SRGBColorSpace, Sprite, SpriteMaterial, Vector3 } from "three";
import tokens from "@synapse/tokens";

const FONT = '"Synapse Sans", "Inter", system-ui, sans-serif';
const PX = 48; // texture px per text line (drawn big, shown small: crisp at 2x DPR and zoom)

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
  probe.ctx.font = `${weight} ${PX * 0.72}px ${FONT}`;
  const measured = Math.ceil(probe.ctx.measureText(text).width) + PX * 0.4;
  const w = Math.max(8, Math.min(4096, Math.round(aspect ? PX * aspect : measured)));
  const drawn = canvas2d(w, PX);
  if (!drawn) return null;
  const { c, ctx } = drawn;
  ctx.font = `${weight} ${PX * 0.72}px ${FONT}`;
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

/** A billboard label for an object; the renderer sets its scale (sizeLabels). */
export function labelSprite(text, palette) {
  const ink = palette?.["base.ink"] || "#1c2024";
  const halo = palette?.["base.surface"] || "#ffffff";
  const map = textTexture(String(text).slice(0, 80), { color: ink, halo });
  const sprite = new Sprite(new SpriteMaterial({ map, transparent: true, depthTest: false, depthWrite: false }));
  sprite.center.set(0.5, 0);
  sprite.renderOrder = 10;
  sprite.userData.label = String(text);
  sprite.userData.aspect = map ? map.userData.aspect : Math.max(1, String(text).length * 0.55);
  return sprite;
}

const MAX_LIFTS = 4;

// The screen box (px, y up, around the viewport's centre) of an object's world box.
function screenBox(node, camera, viewportHeightPx, aspect) {
  const box = new Box3().setFromObject(node);
  if (box.isEmpty()) return null;
  const out = [Infinity, Infinity, -Infinity, -Infinity];
  const p = new Vector3();
  for (const x of [box.min.x, box.max.x]) for (const y of [box.min.y, box.max.y]) for (const z of [box.min.z, box.max.z]) {
    p.set(x, y, z).project(camera);
    const sx = (p.x * viewportHeightPx * aspect) / 2;
    const sy = (p.y * viewportHeightPx) / 2;
    out[0] = Math.min(out[0], sx);
    out[1] = Math.min(out[1], sy);
    out[2] = Math.max(out[2], sx);
    out[3] = Math.max(out[3], sy);
  }
  return out;
}

/**
 * Scales every label sprite in `scene` to `px` screen pixels tall for a camera and a viewport of
 * `viewportHeightPx`, then lifts labels that would cover an earlier one (in scene order) or a text
 * object (`scene.userData.texts`, the text3d primitives) by a label's height, up to 4 times, as
 * the server projections do (canvas-v2-phase3-4.md 3.8).
 */
export function sizeLabels(scene, camera, viewportHeightPx, px = labelSizePx()) {
  const labels = scene?.userData?.labels;
  if (!Array.isArray(labels) || !labels.length || !(viewportHeightPx > 0)) return;
  let unitsPerPx;
  if (camera.isOrthographicCamera) {
    unitsPerPx = (camera.top - camera.bottom) / (camera.zoom || 1) / viewportHeightPx;
  }
  camera.updateMatrixWorld();
  const up = new Vector3(0, 1, 0).applyQuaternion(camera.quaternion);
  const aspect = camera.isOrthographicCamera ? (camera.right - camera.left) / Math.max(1e-9, camera.top - camera.bottom) : camera.aspect || 1;
  const at = new Vector3();
  const placed = [];
  for (const node of scene.userData.texts || []) {
    const box = screenBox(node, camera, viewportHeightPx, aspect);
    if (box) placed.push(box);
  }
  for (const sprite of labels) {
    if (!sprite.userData.base) sprite.userData.base = sprite.position.clone();
    if (sprite.visible === false) continue;
    sprite.position.copy(sprite.userData.base);
    let upp = unitsPerPx;
    if (!Number.isFinite(upp)) {
      const d = camera.position.distanceTo(sprite.getWorldPosition(at));
      upp = (2 * Math.tan(((camera.fov || 35) * Math.PI) / 360) * d) / viewportHeightPx;
    }
    // The texture's glyphs are 0.72 of its height (textTexture): the text itself is px tall.
    const h = (px / 0.72) * upp;
    sprite.scale.set(h * sprite.userData.aspect, h, 1);
    // Screen boxes in px (the viewport's height scale on both axes): lift past earlier labels.
    const hPx = px / 0.72;
    const wPx = hPx * sprite.userData.aspect;
    let box = null;
    for (let lift = 0; lift <= MAX_LIFTS; lift += 1) {
      const p = sprite.position.clone().project(camera);
      const cx = (p.x * viewportHeightPx * aspect) / 2;
      const cy = (p.y * viewportHeightPx) / 2;
      box = [cx - wPx / 2 + 1, cy + 1, cx + wPx / 2 - 1, cy + hPx * 0.8];
      const hit = placed.some((b) => box[0] < b[2] && b[0] < box[2] && box[1] < b[3] && b[1] < box[3]);
      if (!hit || lift === MAX_LIFTS) break;
      sprite.position.addScaledVector(up, h * 0.8);
    }
    placed.push(box);
  }
}
