// text3d: text (80 characters or fewer), height (0.3), billboard (true). A canvas-texture label in
// "Synapse Sans", `height` world units tall; its width is the solved extent's (Python measured the
// text with the same font metrics), else an estimate.
import { Group, Mesh, MeshBasicMaterial, PlaneGeometry, Sprite, SpriteMaterial } from "three";
import { num } from "./_util.js";
import { textTexture } from "../labels.js";

export default {
  name: "text3d",
  build(obj, solved, ctx) {
    const text = String(obj?.text ?? obj?.label ?? "").slice(0, 80);
    const h = num(obj?.height, 0.3);
    const ext = Array.isArray(solved?.ext) ? solved.ext : null;
    const w = ext && Number(ext[0]) > 0 ? Number(ext[0]) : Math.max(h, text.length * h * 0.55);
    const ink = ctx.palette?.[`tone.${obj?.tone || "neutral"}.text`] || ctx.palette?.["base.ink"] || "#1c2024";
    const map = textTexture(text, { color: ink, aspect: w / h });
    const group = new Group();
    group.name = `${obj?.id ?? "text"}:text3d`;
    if (obj?.billboard === false) {
      const geo = new PlaneGeometry(w, h);
      geo.translate(0, h / 2, 0);
      group.add(new Mesh(geo, new MeshBasicMaterial({ map, transparent: true, depthWrite: false })));
    } else {
      const sprite = new Sprite(new SpriteMaterial({ map, transparent: true, depthWrite: false }));
      sprite.position.set(0, h / 2, 0);
      sprite.scale.set(w, h, 1);
      group.add(sprite);
    }
    return group;
  },
};
