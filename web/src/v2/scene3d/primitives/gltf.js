// gltf: a model from the team's artifacts, packed into one GLB asset by the server (solved.asset).
// The loaded model (loaders/gltf.js) is scaled uniformly to the solved extent and re-centred with its
// base at y = 0; `tone` overrides the base colour of every mesh. Until it loads, or when it cannot
// (a failed load, or past the frame's triangle budget), its proxy box draws with dashed edges.
import { Box3, EdgesGeometry, Group, LineDashedMaterial, LineSegments, Vector3 } from "three";
import { boxGeometry } from "./box.js";
import { sizeOf } from "./_util.js";

function extOf(obj, solved) {
  const ext = Array.isArray(solved?.ext) && solved.ext.length === 3 && solved.ext.every((v) => Number(v) > 0) ? solved.ext.map(Number) : null;
  return ext || sizeOf(obj?.size, [1, 1, 1]);
}

/** The labelled proxy: the model's box as dashed edges. */
export function proxyBox(ext, colour) {
  const geo = boxGeometry(...ext);
  const lines = new LineSegments(new EdgesGeometry(geo), new LineDashedMaterial({ color: colour, dashSize: Math.max(...ext) / 20, gapSize: Math.max(...ext) / 30 }));
  lines.computeLineDistances();
  lines.name = "proxy";
  geo.dispose();
  return lines;
}

/** A model group fitted to `ext` (uniform scale, x/z centred, base at y = 0). */
export function fitModel(model, ext) {
  const box = new Box3().setFromObject(model);
  const size = box.getSize(new Vector3());
  const ratios = [ext[0] / size.x, ext[1] / size.y, ext[2] / size.z].filter((r) => Number.isFinite(r) && r > 0);
  const s = ratios.length ? Math.min(...ratios) : 1;
  const holder = new Group();
  model.scale.multiplyScalar(s);
  model.updateMatrixWorld(true);
  const fitted = new Box3().setFromObject(model);
  const centre = fitted.getCenter(new Vector3());
  model.position.x -= centre.x;
  model.position.z -= centre.z;
  model.position.y -= fitted.min.y;
  holder.add(model);
  return holder;
}

export default {
  name: "gltf",
  loader: "gltf",
  build(obj, solved, ctx) {
    const ext = extOf(obj, solved);
    const group = new Group();
    group.name = `${obj?.id ?? "model"}:gltf`;
    const edge = ctx.palette?.[`mat.${obj?.tone || "neutral"}.edge`] || ctx.palette?.["base.ink_muted"] || "#5b616b";
    const proxy = proxyBox(ext, edge);
    group.add(proxy);
    const source = solved && typeof solved.asset === "string" ? ctx.assets?.get?.(solved.asset) : null;
    if (source) {
      const model = source.clone(true);
      if (obj?.tone) {
        const surface = ctx.materials.surface(obj);
        model.traverse((node) => {
          if (node.isMesh) node.material = surface;
        });
      }
      const fitted = fitModel(model, ext);
      fitted.name = "model";
      group.add(fitted);
      proxy.visible = false;
      group.userData.model = fitted;
      group.userData.proxy = proxy;
      group.userData.tris = source.userData?.tris || 0;
    }
    return group;
  },
};
