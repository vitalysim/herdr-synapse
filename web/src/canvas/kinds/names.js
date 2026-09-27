// The canvas kinds this page draws, by canonical element type (herdr_team/canvas_kinds). Data
// only, no imports: scripts/postbuild.mjs reads it to write dist/kinds.json, which a Python test
// holds equal to canvas_kinds.names(page_only=True). Adding a page kind: its module in this
// directory, registered in index.js, and its name here.
export const PAGE_KINDS = [
  "arrow",
  "box",
  "chart",
  "comment",
  "diamond",
  "ellipse",
  "frame",
  "image",
  "mermaid",
  "note",
  "path",
  "pen",
  "svg",
  "text",
  "viz",
];

// The display-list versions the page draws (canvas v2 phase 1); none yet.
export const DISPLAY_LIST = null;
