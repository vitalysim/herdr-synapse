// The design-token blocks the page does not bundle (vite.config.js pageTokens, and the test that
// holds this list honest, src/theme/tokens.test.js).
//
// assets/canvas/tokens.json is one document for both sides. The Python side lays the board out with
// it (it measures text, fits labels, sizes and pads every kind) and sends the page a display list
// with the geometry and the palettes already resolved, so these blocks never reach the browser:
//
//   mat          3D projection shades; the display list carries mat.<tone>.<shade> resolved, and the
//                page's own materials colour from tone.<t>.solid (canvas-v2-phase3-4.md 1.5)
//   type         the type scale (font sizes and weights per role): the display list carries the size
//   padding      per-kind label padding, used while fitting
//   size_min     the smallest box each kind may be fitted to
//   fonts        the faces Python measures with (src/fonts.css names them for the browser)
//   space        the layout grid's gaps
//   layout       automatic-layout defaults (columns, flow direction)
//   lod          the semantic-zoom thresholds the server folds into the display list
//   source       where the document came from, for the designer
//   line_height  the line height Python measures with
//
// A block listed here is absent from `TOKENS` in the browser, so page code may only read it
// optional-chained with a fallback (`tokens?.mat?.[t]`). Dropping them keeps the v2 first load well
// inside its budget (canvas-v2-phase6.md D4); measured 2.5 KB of JSON, 0.9 KB gzipped.
export const PYTHON_ONLY_TOKEN_BLOCKS = ["mat", "type", "padding", "size_min", "fonts", "space", "layout", "lod", "source", "line_height"];

//: What the page does read, so the trim above cannot take a block the browser needs.
export const PAGE_TOKEN_BLOCKS = ["theme", "tones", "kinds", "kind_groups", "defaults", "tools", "chart", "scene3d", "legacy", "radius", "phase0_excalidraw"];
