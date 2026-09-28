// Data only: what this page's renderer understands. scripts/postbuild.mjs reads this file into
// web/dist/kinds.json ("display_list" and "slots"), and the server's test_web_contract holds the
// registry's slot kinds equal to SLOT_KINDS (canvas-v2-phase1.md 6.3 J6).
//
// DL_SUPPORTED is the inclusive range of display list versions ("dl") the renderer draws. A new
// primitive or optional field keeps "dl"; only a breaking change bumps it (docs/display-list.md).
// SLOT_KINDS are the slot renderers in render/slots/: adding a browser-drawn kind is one file there
// plus its name here (2.1).
export const DL_SUPPORTED = [1, 1];
export const SLOT_KINDS = ["chart", "mermaid", "scene3d", "viz"];
