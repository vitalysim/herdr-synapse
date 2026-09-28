// Scene cameras (canvas-v2-phase3-4.md 3.5): presets, the shared angle convention, framing with the
// 6 % margin, zoom and target.
import { describe, expect, test } from "vitest";
import { Vector3 } from "three";
import { DEFAULT_CAMERAS, MARGIN, cameraSpec, fitAspect, frame, makeCamera, orbitOf, viewDir } from "./cameras.js";

const B = [-4, 0, -2.5, 4, 2.4, 2.5];
const close = (a, b, eps = 1e-6) => expect(Math.abs(a - b)).toBeLessThan(eps);

describe("cameras", () => {
  test("the presets are the tokens' (iso 45/35.264, front, top, side, orbit)", () => {
    expect(DEFAULT_CAMERAS.iso).toEqual({ projection: "ortho", az: 45, el: 35.264 });
    expect(cameraSpec("front")).toMatchObject({ preset: "front", projection: "ortho", az: 0, el: 0, zoom: 1 });
    expect(cameraSpec({ preset: "orbit", az: 30, el: 20, zoom: 1.2 })).toMatchObject({ projection: "persp", az: 30, el: 20, zoom: 1.2 });
    expect(cameraSpec("nonsense").preset).toBe("iso");
    expect(cameraSpec({ preset: "iso", zoom: 999 }).zoom).toBe(20);
  });

  test("az 0 el 0 looks from +z, az 90 from +x, el 90 from above", () => {
    const [x0, y0, z0] = viewDir(0, 0);
    close(x0, 0);
    close(y0, 0);
    close(z0, 1);
    const [x1, , z1] = viewDir(90, 0);
    close(x1, 1);
    close(z1, 0);
    close(viewDir(0, 90)[1], 1);
  });

  test("the front view frames the bounds with a 6 % margin, widened to the aspect", () => {
    const f = frame(B, cameraSpec("front"), 2);
    // front: x across (8 wide), y up (2.4 tall); with the margin, 8 * 1.12 / 2 across
    close(f.half[0], (8 / 2) * (1 + 2 * MARGIN));
    close(f.half[1], f.half[0] / 2);
    expect(f.target).toEqual([0, 1.2, 0]);
    const tall = frame(B, cameraSpec("front"), 0.5);
    close(tall.half[0] / tall.half[1], 0.5);
  });

  test("the top view looks down with +x to the right and +z toward the bottom", () => {
    const f = frame(B, cameraSpec("top"), 1);
    const cam = makeCamera(f);
    const right = new Vector3(1, 0, 0).project(cam);
    const near = new Vector3(0, 0, 2).project(cam);
    expect(right.x).toBeGreaterThan(0);
    expect(near.y).toBeLessThan(0);
  });

  test("zoom scales the fit, a target re-centres it", () => {
    const one = frame(B, cameraSpec("iso"), 1.5);
    const two = frame(B, cameraSpec({ preset: "iso", zoom: 2 }), 1.5);
    close(two.half[0], one.half[0] / 2);
    const aimed = frame(B, cameraSpec({ preset: "iso", target: [1, 0, 1] }), 1.5);
    expect(aimed.target).toEqual([1, 0, 1]);
    const byId = frame(B, cameraSpec({ preset: "iso", target: "db" }), 1.5, (id) => (id === "db" ? [3, 0.7, 0] : null));
    expect(byId.target).toEqual([3, 0.7, 0]);
  });

  test("every corner of the bounds projects inside the view", () => {
    for (const preset of ["iso", "front", "top", "side", "orbit"]) {
      const f = frame(B, cameraSpec(preset), 1.6);
      const cam = makeCamera(f);
      cam.aspect = 1.6;
      cam.updateProjectionMatrix();
      for (const x of [B[0], B[3]]) for (const y of [B[1], B[4]]) for (const z of [B[2], B[5]]) {
        const p = new Vector3(x, y, z).project(cam);
        expect(Math.abs(p.x), `${preset} x`).toBeLessThanOrEqual(1.0001);
        expect(Math.abs(p.y), `${preset} y`).toBeLessThanOrEqual(1.0001);
      }
    }
  });

  test("an orthographic view fits the points it is given, centred on them, at any aspect", () => {
    // Four small boxes' corners on a ring: much tighter than the ring's bounds.
    const pts = [];
    for (const [cx, cz] of [[-2, 0], [2, 0], [0, -2], [0, 2]]) for (const dx of [-0.3, 0.3]) for (const y of [0, 0.6]) for (const dz of [-0.3, 0.3]) pts.push([cx + dx, y, cz + dz]);
    const spec = cameraSpec("front");
    const f = frame([-3, 0, -3, 3, 0.6, 3], spec, 2, null, pts);
    close(f.fit[0], 2.3 * (1 + 2 * MARGIN));
    close(f.fit[1], 0.3 * (1 + 2 * MARGIN));
    close(f.target[1], 0.3);
    const cam = makeCamera(f);
    for (const aspect of [0.5, 1, 3]) {
      fitAspect(cam, f, aspect);
      const tight = [];
      for (const pt of pts) {
        const p = new Vector3(...pt).project(cam);
        expect(Math.abs(p.x)).toBeLessThanOrEqual(1.0001);
        expect(Math.abs(p.y)).toBeLessThanOrEqual(1.0001);
        tight.push(Math.max(Math.abs(p.x), Math.abs(p.y)));
      }
      // One axis is filled to the margin, whatever the aspect.
      close(Math.max(...tight), 1 / (1 + 2 * MARGIN), 1e-6);
    }
  });

  test("orbitOf reads a camera back as az, el and zoom", () => {
    const f = frame(B, cameraSpec({ preset: "orbit", az: 60, el: 30 }), 1);
    const cam = makeCamera(f);
    const dist = Math.hypot(...f.eye.map((v, i) => v - f.target[i]));
    const view = orbitOf(cam, f.target, dist);
    expect(view).toEqual({ az: 60, el: 30, zoom: 1 });
  });
});
