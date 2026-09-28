// @vitest-environment jsdom
// The presence layer (canvas-v2-phase5.md 12.2): halo geometry from a region or from the union of
// its ids' boxes, dashes by status, the fade, culling and the cap, the operator's other pages, and
// text-only rendering (a hostile intent stays text).
import fs from "node:fs";
import path from "node:path";
import React, { act } from "react";
import { createRoot } from "react-dom/client";
import { afterEach, beforeAll, describe, expect, test } from "vitest";
import Halos, { haloShapes } from "./Halos.jsx";
import { MAX_HALOS, parsePresence } from "./presence.js";

const load = (name) => JSON.parse(fs.readFileSync(path.resolve(import.meta.dirname, "__fixtures__", name), "utf8"));
const DL = load("display.json");
const PRESENCE = load("presence.json");
const NOW = Date.parse("2026-09-28T10:00:00Z");
const bboxOf = (id) => (DL.entries.find((e) => e.id === id) || {}).bbox || null;
const chipOf = (name) => ({ bg: name === "drawer" ? "#e5484d" : "#30a46c", fg: "#ffffff", initials: name.slice(0, 2).toUpperCase() });
const VIEWPORT = { w: 1000, h: 700 };

beforeAll(() => {
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
});

describe("geometry", () => {
  const parsed = parsePresence(PRESENCE, { now: NOW, page: "3f9a0c1b2d4e5f60" });

  test("a region, padded by 6 screen px; else the union of its ids' boxes", () => {
    const { halos } = haloShapes({ members: parsed.members, camera: { x: 0, y: 0, scale: 1 }, viewport: VIEWPORT, bboxOf, chipOf });
    const drawer = halos.find((h) => h.name === "drawer");
    expect([drawer.x, drawer.y, drawer.w, drawer.h]).toEqual([274, -26, 212, 132]);
    const peer = halos.find((h) => h.name === "peer");
    // E-1 [0,0,160,80] and E-2 [300,0,460,80]
    expect([peer.x, peer.y, peer.w, peer.h]).toEqual([-6, -6, 472, 92]);
  });

  test("screen space follows the camera", () => {
    const { halos } = haloShapes({ members: parsed.members.slice(0, 1), camera: { x: 100, y: -50, scale: 2 }, viewport: VIEWPORT, bboxOf, chipOf });
    expect([halos[0].x, halos[0].y]).toEqual([(280 - 100) * 2 - 6, (-20 + 50) * 2 - 6]);
    expect(halos[0].w).toBe(200 * 2 + 12);
  });

  test("dashed while waiting or blocked; the label names status and intent", () => {
    const { halos } = haloShapes({ members: parsed.members, camera: { x: 0, y: 0, scale: 1 }, viewport: VIEWPORT, bboxOf, chipOf });
    expect(halos.find((h) => h.name === "drawer").dashed).toBe(false);
    expect(halos.find((h) => h.name === "peer").dashed).toBe(true);
    expect(halos.find((h) => h.name === "drawer").label).toBe("drawer · drawing · laying out the pricing table");
    expect(halos.find((h) => h.name === "drawer").stroke).toBe("#e5484d");
  });

  test("off-screen halos are culled; at most 16 are drawn", () => {
    const off = haloShapes({ members: parsed.members, camera: { x: 5000, y: 5000, scale: 1 }, viewport: VIEWPORT, bboxOf, chipOf });
    expect(off.halos).toHaveLength(0);
    const many = Array.from({ length: 30 }, (_, i) => ({ name: `m${i}`, status: "drawing", region: [i * 10, 0, i * 10 + 5, 5], ids: [], intent: "", opacity: 1 }));
    expect(haloShapes({ members: many, camera: { x: 0, y: 0, scale: 1 }, viewport: VIEWPORT, bboxOf, chipOf }).halos).toHaveLength(MAX_HALOS);
  });

  test("a member with neither region nor known ids draws nothing", () => {
    const { halos } = haloShapes({ members: [{ name: "x", status: "drawing", region: null, ids: ["E-99"], opacity: 1 }], camera: { x: 0, y: 0, scale: 1 }, viewport: VIEWPORT, bboxOf, chipOf });
    expect(halos).toHaveLength(0);
  });

  test("the operator's other pages: the viewport and the cursor", () => {
    const { operators } = haloShapes({ members: [], operators: [{ page: "00112233445566aa", viewport: [0, 0, 400, 300], cursor: [100, 50], opacity: 1 }], camera: { x: 0, y: 0, scale: 1 }, viewport: VIEWPORT });
    expect(operators).toEqual([{ page: "00112233445566aa", viewport: { x: 0, y: 0, w: 400, h: 300 }, cursor: [100, 50], opacity: 1 }]);
  });
});

describe("drawing", () => {
  let mounted = [];
  afterEach(() => {
    for (const { root, node } of mounted) {
      act(() => root.unmount());
      node.remove();
    }
    mounted = [];
  });
  const mount = (props) => {
    const node = document.createElement("div");
    document.body.appendChild(node);
    const root = createRoot(node);
    act(() => root.render(React.createElement(Halos, { camera: { x: 0, y: 0, scale: 1 }, viewport: VIEWPORT, bboxOf, chipOf, ...props })));
    mounted.push({ root, node });
    return node;
  };

  test("one rect and one pill per halo, with the fill alpha of the theme and the fade", () => {
    const presence = parsePresence(PRESENCE, { now: NOW + 80 * 1000, page: "3f9a0c1b2d4e5f60" });
    const node = mount({ presence, theme: "dark" });
    const rects = node.querySelectorAll("rect.cv2-halo");
    expect(rects).toHaveLength(1);
    expect(rects[0].getAttribute("fill-opacity")).toBe("0.1");
    // drawer: ttl 90, 80 s in: 10 s left of an 18 s fade.
    expect(Number(rects[0].getAttribute("opacity"))).toBeCloseTo(10 / 18, 3);
    expect(node.querySelectorAll(".cv2-halo-pill")).toHaveLength(1);
    // The pill sits under its halo, clear of the claim label along a claim's top edge (QA phase 5 L10).
    const rect = rects[0];
    const pillTop = parseFloat(node.querySelector(".cv2-halo-pill").style.top);
    expect(pillTop).toBeGreaterThan(Number(rect.getAttribute("y")) + Number(rect.getAttribute("height")));
    const light = mount({ presence: parsePresence(PRESENCE, { now: NOW, page: "3f9a0c1b2d4e5f60" }), theme: "light" });
    expect(light.querySelector("rect.cv2-halo").getAttribute("fill-opacity")).toBe("0.06");
    expect(light.querySelector('rect.cv2-halo[data-name="peer"]').getAttribute("stroke-dasharray")).toBe("6 4");
    expect(light.querySelectorAll(".cv2-operator-view")).toHaveLength(1);
    expect(light.querySelectorAll(".cv2-operator-cursor")).toHaveLength(0); // that page has no cursor
  });

  test("a hostile intent or name stays text", () => {
    const presence = { members: [{ name: "<b>x</b>", status: "drawing", region: [0, 0, 10, 10], ids: [], intent: '<img src=x onerror="window.__pwned=1">', opacity: 1 }], operators: [] };
    const node = mount({ presence });
    expect(node.querySelector("img")).toBeNull();
    expect(node.querySelector("b")).toBeNull();
    expect(node.querySelector(".cv2-halo-text").textContent).toContain('<img src=x onerror="window.__pwned=1">');
    expect(window.__pwned).toBeUndefined();
  });

  test("the layer never takes a pointer event (CSS) and says how many halos it drew", () => {
    const node = mount({ presence: parsePresence(PRESENCE, { now: NOW }) });
    const layer = node.querySelector(".cv2-halos");
    expect(layer.getAttribute("data-halos")).toBe("2");
    expect(layer.getAttribute("aria-hidden")).toBe("true");
  });
});
