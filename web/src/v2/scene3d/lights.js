// Scene lights from the tokens (canvas-v2-phase3-4.md 3.5): presets studio, soft, day, night and
// flat, each a list of {type: hemisphere | directional | ambient | point, ...}. The server
// projections ignore lights (flat three-tone shading); only the page lights a scene.
import { AmbientLight, DirectionalLight, Group, HemisphereLight, PointLight } from "three";
import tokens from "@synapse/tokens";

export const DEFAULT_LIGHTS = {
  studio: [
    { type: "hemisphere", sky: "#ffffff", ground: "#d9d9e0", i: 0.9 },
    { type: "directional", color: "#ffffff", i: 1.4, dir: [-0.5, -1, -0.35] },
  ],
  soft: [
    { type: "hemisphere", sky: "#ffffff", ground: "#e8e8ec", i: 1.2 },
    { type: "directional", color: "#ffffff", i: 0.7, dir: [-0.3, -1, -0.5] },
  ],
  day: [
    { type: "hemisphere", sky: "#dbeafe", ground: "#e9e3d5", i: 1.0 },
    { type: "directional", color: "#fff6e5", i: 1.8, dir: [-0.6, -1, -0.2] },
  ],
  night: [
    { type: "hemisphere", sky: "#8da4ef", ground: "#1f2d5c", i: 0.6 },
    { type: "directional", color: "#c7d2fe", i: 0.8, dir: [0.4, -1, -0.6] },
  ],
  flat: [{ type: "ambient", color: "#ffffff", i: 1.0 }],
};

const HEX = /^#[0-9a-fA-F]{6}$/;
const colour = (v, d) => (typeof v === "string" && HEX.test(v) ? v : d);
const num = (v, d) => (Number.isFinite(Number(v)) ? Number(v) : d);

export function lightPreset(name) {
  const own = tokens?.scene3d?.lights;
  const all = own && typeof own === "object" ? { ...DEFAULT_LIGHTS, ...own } : DEFAULT_LIGHTS;
  return Array.isArray(all[name]) ? all[name] : all.studio;
}

/** A group of three.js lights for a preset, sized to a scene of `radius` around `centre`. */
export function buildLights(name, centre = [0, 0, 0], radius = 5) {
  const group = new Group();
  group.name = `lights:${name}`;
  for (const spec of lightPreset(name)) {
    if (!spec || typeof spec !== "object") continue;
    // Token intensities are in the classic (non-physical) scale the look was tuned in; three's
    // physically based lighting divides diffuse light by π, so they are scaled back up here.
    const i = num(spec.i, 1) * Math.PI;
    if (spec.type === "hemisphere") {
      group.add(new HemisphereLight(colour(spec.sky, "#ffffff"), colour(spec.ground, "#d9d9e0"), i));
    } else if (spec.type === "ambient") {
      group.add(new AmbientLight(colour(spec.color, "#ffffff"), i));
    } else if (spec.type === "directional") {
      const light = new DirectionalLight(colour(spec.color, "#ffffff"), i);
      const d = Array.isArray(spec.dir) && spec.dir.length === 3 ? spec.dir.map((v) => num(v, 0)) : [-0.5, -1, -0.35];
      const l = Math.hypot(...d) || 1;
      const far = Math.max(1, radius) * 3;
      light.position.set(centre[0] - (d[0] / l) * far, centre[1] - (d[1] / l) * far, centre[2] - (d[2] / l) * far);
      light.target.position.set(...centre);
      group.add(light, light.target);
    } else if (spec.type === "point") {
      const light = new PointLight(colour(spec.color, "#ffffff"), i);
      const p = Array.isArray(spec.pos) && spec.pos.length === 3 ? spec.pos.map((v) => num(v, 0)) : [0, radius * 2, 0];
      light.position.set(...p);
      group.add(light);
    }
  }
  return group;
}
