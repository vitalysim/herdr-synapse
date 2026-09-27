// The renderer's one import surface (canvas-v2-phase1.md 6.2). INTERACTION imports only this file
// from render/; the renderer never imports interact/, api.js or the store.
//
// Types:
/** @typedef {[number, number]} Pt                         world units unless named screen */
/** @typedef {[number, number, number, number]} Rect        [x0, y0, x1, y1] */
/** @typedef {{x: number, y: number, scale: number}} Camera  screen = (world - [x, y]) * scale */
/** @typedef {object} DisplayList    canvas-v2-phase1.md 1.1 (docs/display-list.md) */
/** @typedef {object} Entry          1.2 */
/** @typedef {object} Primitive      1.3 */
/**
 * @typedef {{
 *   type: "down"|"move"|"up"|"cancel"|"dblclick"|"hover"|"leave",
 *   world: Pt, screen: Pt, scale: number,
 *   button: number, buttons: number, shift: boolean, alt: boolean, meta: boolean, ctrl: boolean,
 *   pointerId: number, pointerType: "mouse"|"pen"|"touch",
 *   hit: string|null,
 *   handle: {id: string, handle: string}|null,
 *   connector: {id: string, side: string, point: Pt}|null
 * }} SurfacePointer
 */
/**
 * Surface props: dl, theme ("light"|"dark"), camera, onCamera(Camera), onViewport({w, h}),
 * selection (id[]), hover (id|null), preview ({move?: {ids, by}, boxes?: {id: [x,y,w,h]},
 * hide?: id[], ghost?: Primitive[]} | null), panMode, showChips, writable, vizOn, team,
 * urls ({asset(name), still(name), viz(id), artifact(rel)}), elementOf(id), onPointer(SurfacePointer),
 * onStill(id, version, pngBlob), onRendered({version, root}), children (screen-space HTML).
 */

export { DL_SUPPORTED, SLOT_KINDS } from "./version.js";
export { camera, MIN_SCALE, MAX_SCALE } from "./camera.js";
export { isSupported, applyDelta, LAYERS } from "./delta.js";
export { createIndex, entryOf, cull } from "./cull.js";
export { hitTest, queryRect, handleAt, connectorAt, boxOf, handlePoints, connectorPoints } from "./hit.js";
export { lodVisible, textLayout } from "./lod.js";
export { Surface } from "./Surface.jsx";
export { CANONICAL_FAMILIES, PAGE_FAMILIES } from "./svgAttrs.js";
export { toSVGString, CANONICAL_URLS } from "./svgString.js";
export { fmt } from "./fmt.js";
export { verifyText } from "./measure.js";
export { installQAHook } from "./qa.js";
export { resolvePaint, paletteOf } from "../theme/palette.js";
