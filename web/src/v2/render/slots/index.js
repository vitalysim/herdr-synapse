// The slot renderers, keyed by the display list's `slot` field (canvas-v2-phase1.md 2.1, 4.2;
// canvas-v2-phase3-4.md WW-2). A new browser-drawn kind is one entry in this table and its name in
// ../version.js SLOT_KINDS (a test holds the two equal). A slot this table does not know draws its
// still or its fallback, like a written picture.
//
// mode "svg": the component returns SVG inside the entry's group. Props: prim, entryId, version,
//   element, team, theme, palette, palettes ({light, dark}), writable, paper, edge, fallback,
//   onStill(id, v, blob, view?), entered,
//   onExit(). A renderer with `Live` also has a live mode: while its entry is entered, Surface mounts
//   Live in the HTML layer (world units); Live places itself at the slot box (prim) and keeps the
//   board's pointer gestures off itself. Its props are {prim, entryId, version, element, team,
//   theme, palette, onExit}.
// mode "html": the component is placed in the HTML layer above the SVG, in world coordinates.
//   Props: prim, entryId, version, element, team, theme, palette, stillPalette (the light one),
//   urls, writable, selected, onStill, entered, onExit(), onOps(ops) (writable pages only).
//   `underlay: true` keeps the slot's still or drawing in the SVG beneath (it shows wherever the
//   component does not paint); without it the SVG shows nothing there, and the slot draws only
//   while the team's live visuals are on (viz).
// `enter: "gl"`: a double-click on the slot enters it (as a slot with Live does), for an html slot
//   whose primitive carries gl.
import MermaidSlot from "./mermaid.jsx";
import VizSlot from "./viz.jsx";
import { QA as chartsQA, chartSlot } from "../../charts/index.js";
import { registerQA } from "../qa.js";
import { scene3dSlot } from "../../scene3d/index.js";

export const SLOT_RENDERERS = {
  chart: chartSlot,
  mermaid: { mode: "svg", Component: MermaidSlot },
  scene3d: scene3dSlot,
  viz: { mode: "html", Component: VizSlot },
};

// window.__synapseV2.charts() (WW-5); scene3d registers its own (scene3d/qa.js).
registerQA(chartsQA.name, chartsQA.read);

export function slotRenderer(kind) {
  return Object.prototype.hasOwnProperty.call(SLOT_RENDERERS, kind) ? SLOT_RENDERERS[kind] : null;
}

/** Whether a double-click on this slot primitive enters it (WW-3). */
export function enterable(p) {
  const renderer = p && p.k === "slot" ? slotRenderer(p.slot) : null;
  if (!renderer) return false;
  if (renderer.Live) return true;
  return renderer.mode === "html" && renderer.enter === "gl" && p.gl !== false;
}

/** The enterable slot of an entry whose box holds world point `pt` (or any, without one), or null. */
export function enterableSlotAt(entry, pt = null) {
  let found = null;
  const walk = (items) => {
    for (const p of Array.isArray(items) ? items : []) {
      if (found || !p) continue;
      if (p.k === "slot" && enterable(p)) {
        const inside = !pt || (pt[0] >= p.x && pt[0] <= p.x + p.w && pt[1] >= p.y && pt[1] <= p.y + p.h);
        if (inside) found = p;
      } else if (p.k === "group" && !p.t && !p.screen) walk(p.items);
    }
  };
  walk(entry && entry.items);
  return found;
}
