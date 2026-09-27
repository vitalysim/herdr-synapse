// The slot renderers, keyed by the display list's `slot` field (canvas-v2-phase1.md 2.1, 4.2).
// A new browser-drawn kind is one file here, one line in this table and its name in
// ../version.js SLOT_KINDS (a test holds the two equal). A slot this table does not know draws its
// still or its fallback, like a written picture.
//
// mode "svg": the component returns SVG inside the entry's group (props: prim, entryId, version,
//   element, team, paper, edge, fallback, onStill).
// mode "html": the component is placed in the HTML layer above the SVG, in world coordinates
//   (props: prim, entryId, version, element, team, selected, onStill); the SVG shows nothing.
import ChartSlot from "./chart.jsx";
import MermaidSlot from "./mermaid.jsx";
import VizSlot from "./viz.jsx";

export const SLOT_RENDERERS = {
  chart: { mode: "svg", Component: ChartSlot },
  mermaid: { mode: "svg", Component: MermaidSlot },
  viz: { mode: "html", Component: VizSlot },
};

export function slotRenderer(kind) {
  return Object.prototype.hasOwnProperty.call(SLOT_RENDERERS, kind) ? SLOT_RENDERERS[kind] : null;
}
