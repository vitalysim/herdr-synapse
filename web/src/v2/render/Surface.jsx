// Surface: the v2 canvas renderer (canvas-v2-phase1.md 4.2 and 6.2). One <svg> filling its
// container; a world group transformed by the camera holds the four layers of the display list and
// then the UI (selection, handles, hover, chips, connection points, ghost). Entries are memoised by
// identity (applyDelta keeps unchanged entries' objects), so a delta redraws only what changed and a
// pan redraws nothing but the world transform.
//
// Surface is controlled: it never moves its own camera (every change goes out through onCamera),
// never keeps selection or preview state, never touches the network except through `urls`, and
// never handles keys. Camera gestures (wheel, pinch, middle drag, panMode left drag) are its own;
// every other pointer event reaches onPointer as a SurfacePointer.
import React, { memo, useCallback, useEffect, useLayoutEffect, useMemo, useReducer, useRef, useState } from "react";
import { camera as cameraMath } from "./camera.js";
import { contains, createIndex, cull, grow } from "./cull.js";
import { boxOf, connectorAt, connectorPoints, handleAt, handlePoints, hitTest, partAt, partOf } from "./hit.js";
import { dependsOnScale, lodVisible } from "./lod.js";
import { LAYER_NAMES, PAGE_FAMILIES, entryLayers, entryNodes, defsNodes, primitiveNode, slotNode } from "./svgAttrs.js";
import { urlResolver } from "./svgString.js";
import { slotRenderer } from "./slots/index.js";
import { Defs, paletteOf, resolvePaint, shadowsOf } from "../theme/palette.js";
import "./surface.css";

const GRID_STEP = 20;
const GRID_MIN_SCALE = 0.6;
const CULL_THROTTLE_MS = 100;
const HANDLE_PX = 8;
const SELECT_GAP_PX = 4;
// The pin glyph (canvas-v2-phase2.md 6.3 W-b): 12 screen px at the hit box's top-right corner.
const PIN_PX = 12;
// A tip (an element's `detail`) is at most this long in the display list (6.2); anything longer is cut.
const TIP_MAX = 500;
// A wheel or pinch zoom is presented by the compositor until it has been quiet this long (see
// `drawn` below), then drawn once at the new scale.
const ZOOM_SETTLE_MS = 150;
const nowMs = () => (typeof performance !== "undefined" ? performance.now() : Date.now());

// -- node -> React ---------------------------------------------------------------------------------

function propName(name) {
  if (name.startsWith("data-")) return name;
  if (name === "xml:space") return "xmlSpace";
  return name.replace(/-([a-z])/g, (_, c) => c.toUpperCase());
}

function toReact(node, key, env) {
  if (node[0] === "$slot") return env.slot(node[1], key);
  const [tag, attrs, kids] = node;
  const props = { key };
  for (const [name, value] of attrs) props[propName(name)] = value;
  let children = null;
  if (typeof kids === "string") children = kids;
  else if (Array.isArray(kids)) children = kids.map((kid, i) => toReact(kid, i, env));
  return React.createElement(tag, props, children);
}

// A list of ids as one string, for memoising on content ("\n" never appears in an id).
function idsKey(ids) {
  return Array.isArray(ids) && ids.length ? ids.join("\n") : "";
}

// -- per-entry facts, cached by entry identity -------------------------------------------------

const layerCache = new WeakMap();
function layersOf(entry) {
  if (!layerCache.has(entry)) layerCache.set(entry, entryLayers(entry));
  return layerCache.get(entry);
}

const slotCache = new WeakMap();
function hasSlot(entry) {
  if (!slotCache.has(entry)) {
    const walk = (items) => Array.isArray(items) && items.some((p) => p && (p.k === "slot" || (p.k === "group" && walk(p.items))));
    slotCache.set(entry, walk(entry.items));
  }
  return slotCache.get(entry);
}

const htmlSlotCache = new WeakMap();
function htmlSlots(entry) {
  if (htmlSlotCache.has(entry)) return htmlSlotCache.get(entry);
  const out = [];
  const walk = (items) => {
    for (const p of Array.isArray(items) ? items : []) {
      if (!p) continue;
      if (p.k === "slot" && slotRenderer(p.slot)?.mode === "html") out.push(p);
      else if (p.k === "group" && !p.t && !p.screen) walk(p.items);
    }
  };
  walk(entry.items);
  htmlSlotCache.set(entry, out);
  return out;
}

// An HTML-layer slot is drawn when its renderer does not wait for the team's live visuals (viz), or
// when they are on.
function htmlSlotOn(renderer, vizOn) {
  return !!renderer && renderer.mode === "html" && (renderer.underlay || vizOn);
}

// The first slot of an entry whose renderer has a live mode (Live), for the entered entry.
const liveSlotCache = new WeakMap();
function liveSlotOf(entry) {
  if (liveSlotCache.has(entry)) return liveSlotCache.get(entry);
  let found = null;
  const walk = (items) => {
    for (const p of Array.isArray(items) ? items : []) {
      if (found || !p) continue;
      if (p.k === "slot" && slotRenderer(p.slot)?.Live) found = p;
      else if (p.k === "group" && !p.t && !p.screen) walk(p.items);
    }
  };
  walk(entry.items);
  liveSlotCache.set(entry, found);
  return found;
}

// A preview's move or resize of an entry as a CSS transform for its HTML-layer slots (the SVG
// entries get the same one as an SVG transform: entryTransform).
function cssTransform(entry, preview) {
  if (!preview) return undefined;
  const parts = [];
  if (preview.move && Array.isArray(preview.move.ids) && preview.move.ids.includes(entry.id)) {
    const [dx, dy] = preview.move.by || [0, 0];
    parts.push(`translate(${Number(dx) || 0}px, ${Number(dy) || 0}px)`);
  }
  const box = preview.boxes && preview.boxes[entry.id];
  if (Array.isArray(box) && box.length === 4) {
    const [x, y, w, h] = boxOf(entry);
    parts.push(`translate(${box[0]}px, ${box[1]}px) scale(${w ? box[2] / w : 1}, ${h ? box[3] / h : 1}) translate(${-x}px, ${-y}px)`);
  }
  return parts.length ? parts.join(" ") : undefined;
}

// Every def a list refers to, registered up front so <defs> holds them wherever the camera is.
function collectDefs(dl, palette, defs) {
  const walk = (items) => {
    for (const p of Array.isArray(items) ? items : []) {
      if (!p || typeof p !== "object") continue;
      for (const field of ["fill", "stroke"]) {
        const paint = p[field];
        if (paint && typeof paint === "object" && "hatch" in paint) {
          const resolved = resolvePaint(paint, palette);
          if (resolved && typeof resolved === "object") defs.hatch(resolved.hatch);
        }
      }
      if (p.k === "group") walk(p.items);
      if (p.k === "slot") walk(p.fallback);
    }
  };
  for (const entry of dl?.entries || []) walk(entry?.items);
}

function entryTransform(entry, preview) {
  if (!preview) return "";
  const parts = [];
  if (preview.move && Array.isArray(preview.move.ids) && preview.move.ids.includes(entry.id)) {
    const [dx, dy] = preview.move.by || [0, 0];
    parts.push(`translate(${Number(dx) || 0} ${Number(dy) || 0})`);
  }
  const box = preview.boxes && preview.boxes[entry.id];
  if (Array.isArray(box) && box.length === 4) {
    const [x, y, w, h] = boxOf(entry);
    const sx = w ? box[2] / w : 1;
    const sy = h ? box[3] / h : 1;
    parts.push(`translate(${box[0]} ${box[1]}) scale(${sx} ${sy}) translate(${-x} ${-y})`);
  }
  return parts.join(" ");
}

// The box an entry is drawn in under the preview (for selection outlines and handles).
function previewBox(entry, preview) {
  let [x, y, w, h] = boxOf(entry);
  const box = preview?.boxes?.[entry.id];
  if (Array.isArray(box) && box.length === 4) [x, y, w, h] = box;
  if (preview?.move && Array.isArray(preview.move.ids) && preview.move.ids.includes(entry.id)) {
    x += Number(preview.move.by?.[0]) || 0;
    y += Number(preview.move.by?.[1]) || 0;
  }
  return [x, y, w, h];
}

function previewShift(entry, preview) {
  if (preview?.move && Array.isArray(preview.move.ids) && preview.move.ids.includes(entry.id)) {
    return [Number(preview.move.by?.[0]) || 0, Number(preview.move.by?.[1]) || 0];
  }
  return [0, 0];
}

// -- one entry in one layer ------------------------------------------------------------------------

// An entry's tip, on the first layer it draws in only (one tooltip per entry).
function tipOf(entry, layer) {
  if (typeof entry.tip !== "string" || !entry.tip.trim()) return null;
  const first = LAYER_NAMES.find((l) => layersOf(entry).has(l));
  if (first !== layer) return null;
  return entry.tip.length > TIP_MAX ? `${entry.tip.slice(0, TIP_MAX - 1)}…` : entry.tip;
}

const EntryView = memo(
  function EntryView({ entry, layer, ctx, env, transform }) {
    let nodes = entryNodes(entry, layer, ctx);
    if (!nodes.length && layer === (LAYER_NAMES.includes(entry.layer) ? entry.layer : "marks")) {
      // No lost element: an entry that draws nothing anywhere shows its hit box, dashed.
      const others = LAYER_NAMES.some((l) => l !== layer && entryNodes(entry, l, ctx).length);
      const lodHidden = (entry.items || []).some((p) => p && p.lod);
      if (!others && !lodHidden) {
        const [x, y, w, h] = boxOf(entry);
        nodes = [
          [
            "rect",
            [
              ["x", String(x)],
              ["y", String(y)],
              ["width", String(Math.max(1, w))],
              ["height", String(Math.max(1, h))],
              ["fill", "none"],
              ["stroke", ctx.palette["base.ink_muted"] || "#5b616b"],
              ["stroke-width", String(1 / ctx.scale)],
              ["stroke-dasharray", `${4 / ctx.scale} ${3 / ctx.scale}`],
            ],
            null,
          ],
        ];
      }
    }
    if (!nodes.length) return null;
    const slotEnv = { ...env, entry };
    // The tip (W-d) is an SVG <title> on the entry's first drawn group: the browser's own tooltip.
    const tip = tipOf(entry, layer);
    return (
      <g data-id={entry.id} transform={transform || undefined}>
        {tip ? <title>{tip}</title> : null}
        {nodes.map((node, i) => toReact(node, i, slotEnv))}
      </g>
    );
  },
  (a, b) =>
    a.entry === b.entry &&
    a.layer === b.layer &&
    a.transform === b.transform &&
    a.ctx.theme === b.ctx.theme &&
    a.ctx.defs === b.ctx.defs &&
    (!dependsOnScale(b.entry) || a.ctx.scale === b.ctx.scale) &&
    (!hasSlot(b.entry) || a.env.slotKey === b.env.slotKey),
);

// -- UI: selection, hover, handles, chips, connection points ------------------------------------

function Chip({ entry, box, scale, palette, ctx }) {
  const chip = entry.chip || {};
  const bg = resolvePaint(chip.bg || "base.ink", palette);
  const fg = resolvePaint(chip.fg || "base.surface", palette);
  const label = [chip.initials, entry.id, entry.locked ? "locked" : null].filter(Boolean).join(" · ");
  const width = 16 + label.length * 6.4;
  return (
    <g className="sv2-chip" transform={`translate(${box[0]} ${box[1]}) scale(${ctx.scaleFmt(1 / scale)})`}>
      <rect x={0} y={-26} width={width} height={20} rx={10} fill={typeof bg === "string" ? bg : "#1c2024"} />
      <text x={8} y={-12} fontSize={11} fontWeight={600} fontFamily={PAGE_FAMILIES.sans} fill={typeof fg === "string" ? fg : "#ffffff"}>
        {label}
      </text>
    </g>
  );
}

function Outline({ entry, preview, scale, color, gapPx, widthPx }) {
  const hit = entry.hit || {};
  const [dx, dy] = previewShift(entry, preview);
  if (hit.shape === "line" && Array.isArray(hit.points) && !preview?.boxes?.[entry.id]) {
    const pts = hit.points.map((p) => `${p[0] + dx},${p[1] + dy}`).join(" ");
    return (
      <g>
        <polyline points={pts} fill="none" stroke={color} strokeWidth={widthPx} vectorEffect="non-scaling-stroke" strokeLinecap="round" strokeLinejoin="round" />
        {Array.isArray(hit.box) ? (
          <rect x={hit.box[0] + dx} y={hit.box[1] + dy} width={hit.box[2]} height={hit.box[3]} fill="none" stroke={color} strokeWidth={widthPx} vectorEffect="non-scaling-stroke" />
        ) : null}
      </g>
    );
  }
  if (hit.shape === "pin") {
    return <circle cx={Number(hit.x) + dx} cy={Number(hit.y) + dy} r={((Number(hit.r_px) || 10) + gapPx) / scale} fill="none" stroke={color} strokeWidth={widthPx} vectorEffect="non-scaling-stroke" />;
  }
  const [x, y, w, h] = previewBox(entry, preview);
  const g = gapPx / scale;
  return <rect x={x - g} y={y - g} width={w + 2 * g} height={h + 2 * g} fill="none" stroke={color} strokeWidth={widthPx} vectorEffect="non-scaling-stroke" />;
}

function Handles({ entry, preview, scale, color, surface }) {
  const [dx, dy] = previewShift(entry, preview);
  const box = preview?.boxes?.[entry.id];
  let points = handlePoints(entry, scale);
  if (Array.isArray(box) && entry.handles !== "ends") {
    const [x, y, w, h] = boxOf(entry);
    points = points.map(({ handle, point }) => ({
      handle,
      point: [box[0] + (w ? ((point[0] - x) / w) * box[2] : 0), box[1] + (h ? ((point[1] - y) / h) * box[3] : 0)],
    }));
  }
  const s = HANDLE_PX / scale;
  return (
    <g className="sv2-handles">
      {points.map(({ handle, point }) =>
        entry.handles === "ends" ? (
          <circle key={handle} data-handle={handle} cx={point[0] + dx} cy={point[1] + dy} r={s * 0.625} fill={surface} stroke={color} strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
        ) : (
          <rect key={handle} data-handle={handle} x={point[0] + dx - s / 2} y={point[1] + dy - s / 2} width={s} height={s} rx={s / 4} fill={surface} stroke={color} strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
        ),
      )}
    </g>
  );
}

// A pinned entry's glyph (W-b): a push pin in a small disc, drawn with plain SVG at a fixed screen
// size just outside the top-right corner of its box (clear of the corner's resize handle). A human pin is in the selection colour, an
// agent's in the muted ink.
function PinGlyph({ entry, preview, scale, color, surface }) {
  const [x, y, w] = previewBox(entry, preview);
  const k = 1 / scale;
  return (
    <g className="sv2-pin" data-pin={entry.pin} data-pin-for={entry.id} transform={`translate(${x + w} ${y}) scale(${k})`}>
      <circle cx={PIN_PX} cy={-PIN_PX} r={PIN_PX / 2 + 1.5} fill={surface} stroke={color} strokeWidth={1} />
      <g transform={`translate(${PIN_PX / 2} ${-PIN_PX * 1.5})`} fill="none" stroke={color} strokeWidth={1.5} strokeLinecap="round" strokeLinejoin="round">
        <path d="M4.5 1.5h3M5 1.5v3.2L3 7h6L7 4.7V1.5" fill={color} />
        <path d="M6 7v4" />
      </g>
    </g>
  );
}

// The outline of the hovered inline part (W-a), when it is drawn at this scale.
function PartOutline({ entry, part, preview, scale, color }) {
  if (!part || !lodVisible(part, scale)) return null;
  const hit = part.hit || {};
  const box = Array.isArray(hit.box) && hit.box.length === 4 ? hit.box : null;
  if (!box) return null;
  const [dx, dy] = previewShift(entry, preview);
  return (
    <rect className="sv2-part" data-part={part.part} x={box[0] + dx} y={box[1] + dy} width={box[2]} height={box[3]} rx={2 / scale} fill="none" stroke={color} strokeWidth={1} strokeDasharray="3 2" vectorEffect="non-scaling-stroke" />
  );
}

// -- the component ----------------------------------------------------------------------------------

export function Surface({
  dl,
  theme = "light",
  camera: cam,
  onCamera,
  onViewport,
  selection = [],
  hover = null,
  hoverPart = null,
  preview = null,
  panMode = false,
  showChips = false,
  writable = false,
  vizOn = true,
  team = "",
  urls,
  elementOf,
  onPointer,
  onStill,
  onRendered,
  entered = null,
  onEnter,
  onOps,
  screenOverlay = null,
  children,
}) {
  const wrapRef = useRef(null);
  const svgRef = useRef(null);
  const view = cam || cameraMath.create();

  // The camera the SVG world is drawn with. Normally the camera itself. While a wheel or pinch zoom
  // is live, the world keeps the last scale it was drawn at and the <svg> element is CSS-transformed
  // onto the current camera instead: a scale change makes the browser lay out every SVG text again
  // (about 20 ms for 2,000 labels), which is paid once when the gesture settles rather than on every
  // wheel event. Pointer events, hit testing, culling and the HTML layers always use the camera
  // itself, and a camera the parent sets (fit, keyboard zoom) is drawn at once.
  const zoomingUntil = useRef(0);
  const wheelState = useRef({ kind: null, at: 0 });
  const drawnRef = useRef(null);
  const [, settle] = useReducer((n) => n + 1, 0);
  const lazy = !!drawnRef.current && drawnRef.current.scale !== view.scale && nowMs() < zoomingUntil.current;
  const drawn = lazy ? drawnRef.current : view;
  drawnRef.current = drawn;
  useEffect(() => {
    if (!lazy) return undefined;
    // Wheel events at the zoom limit extend zoomingUntil without moving the camera, so nothing
    // re-runs this effect: the timer re-arms itself until the gesture has really settled, or the
    // board would stay a blurry bitmap (and the QA hook never ready) until the next camera change.
    let timer = null;
    const tick = () => {
      const left = zoomingUntil.current - nowMs();
      if (left > 0) timer = setTimeout(tick, left + 5);
      else settle();
    };
    timer = setTimeout(tick, Math.max(0, zoomingUntil.current - nowMs()) + 5);
    return () => clearTimeout(timer);
  }, [lazy, view.x, view.y, view.scale]);
  const [viewport, setViewport] = useState({ w: 0, h: 0 });

  // Every camera change, whatever moved it (a gesture, the keyboard, fit, a QA hook), is announced
  // on the GL host as a "sv2-camera" event after the placeholders have moved, so the shared 3D
  // renderer (scene3d/renderer.js) redraws its scenes at their new rectangles instead of leaving
  // the last frame painted where they were.
  const glHostRef = useRef(null);
  useLayoutEffect(() => {
    const host = glHostRef.current;
    if (host && typeof Event === "function") host.dispatchEvent(new Event("sv2-camera"));
  }, [view.x, view.y, view.scale]);

  // Latest props for event handlers attached once.
  const live = useRef({});
  const index = useMemo(() => createIndex(dl), [dl]);
  live.current = { cam: view, index, selection, hover, writable, panMode, onPointer, onCamera, viewport };

  // -- viewport ------------------------------------------------------------------------------
  const onViewportRef = useRef(onViewport);
  onViewportRef.current = onViewport;
  useLayoutEffect(() => {
    const node = wrapRef.current;
    if (!node) return undefined;
    const measure = () => {
      const r = node.getBoundingClientRect();
      const next = { w: Math.round(r.width), h: Math.round(r.height) };
      setViewport((prev) => (prev.w === next.w && prev.h === next.h ? prev : next));
      if (onViewportRef.current) onViewportRef.current(next);
    };
    measure();
    if (typeof ResizeObserver === "undefined") return undefined;
    const observer = new ResizeObserver(measure);
    observer.observe(node);
    return () => observer.disconnect();
  }, []);

  // -- culling, recomputed on camera settle (throttled) and on a new list ------------------------
  const measured = viewport.w > 0 && viewport.h > 0;
  const viewRect = cameraMath.viewRect(view, viewport);
  const [cullRect, setCullRect] = useState(null);
  const cullRef = useRef(null);
  cullRef.current = cullRect;
  const cullTimer = useRef(null);
  useEffect(() => {
    if (!measured) return undefined;
    const apply = () => {
      const { cam: c, viewport: v } = live.current;
      setCullRect(grow(cameraMath.viewRect(c, v), 1));
    };
    clearTimeout(cullTimer.current);
    if (!contains(cullRef.current, viewRect)) apply();
    else cullTimer.current = setTimeout(apply, CULL_THROTTLE_MS);
    return () => clearTimeout(cullTimer.current);
  }, [view.x, view.y, view.scale, viewport.w, viewport.h, measured]); // eslint-disable-line react-hooks/exhaustive-deps

  // Keyed by content, not identity: a parent that rebuilds its selection array on every camera
  // change must not re-cull and re-reconcile every entry on every pan frame.
  const alwaysKey = idsKey(selection) + "|" + idsKey(preview?.move?.ids) + "|" + idsKey(preview?.boxes ? Object.keys(preview.boxes) : null);
  const always = useMemo(() => new Set(alwaysKey.split(/[|\n]/).filter(Boolean)), [alwaysKey]);
  const hiddenKey = idsKey(preview?.hide);
  const hidden = useMemo(() => new Set(hiddenKey.split("\n").filter(Boolean)), [hiddenKey]);
  // While the view stays inside the cull rect the same rect object is used, so a pan re-culls
  // nothing and redraws nothing but the world transform.
  const effectiveCull = cullRect && contains(cullRect, viewRect) ? cullRect : grow(viewRect, 1);
  const visible = useMemo(() => {
    const now = Date.now();
    const list = measured ? cull(index, effectiveCull, always) : index.entries;
    // A claim past its `until` is hidden (as activeClaims does on v1); it goes on the next re-cull.
    return list.filter((e) => !hidden.has(e.id) && !(e.until && Date.parse(e.until) < now));
  }, [index, effectiveCull, measured, always, hidden]);

  // -- paint and defs ---------------------------------------------------------------------------
  const palette = useMemo(() => paletteOf(dl, theme), [dl, theme]);
  const shadows = useMemo(() => shadowsOf(dl, theme), [dl, theme]);
  const defsByTheme = useRef(new Map());
  const defs = useMemo(() => {
    // One Defs per theme for the Surface's life: ids already handed out never change, so a delta
    // does not redraw every entry.
    let d = defsByTheme.current.get(theme);
    if (!d || d.shadows !== shadows) {
      d = new Defs(shadows);
      [1, 2, 3].forEach((n) => d.elevation(n));
      defsByTheme.current.set(theme, d);
    }
    collectDefs(dl, palette, d);
    return d;
  }, [dl, palette, shadows, theme]);

  const scaleFmt = useCallback((v) => String(Math.round(v * 10000) / 10000), []);
  const ctx = useMemo(
    () => ({
      theme,
      palette,
      scale: drawn.scale,
      families: PAGE_FAMILIES,
      url: urlResolver(urls),
      defs,
      page: true,
      scaleFmt,
      slot: (p) => ["$slot", p],
    }),
    [theme, palette, drawn.scale, urls, defs, scaleFmt],
  );

  // -- slots --------------------------------------------------------------------------------------
  const stillSeen = useRef(new Set());
  const onStillRef = useRef(onStill);
  onStillRef.current = onStill;
  // One still per (id, v, view); `view` is one of the kind's still views ("" for a single still).
  const stillOnce = useCallback((id, v, blob, view = "") => {
    const key = `${id}@${v}@${view || ""}`;
    if (stillSeen.current.has(key) || !onStillRef.current) return;
    stillSeen.current.add(key);
    onStillRef.current(id, v, blob, view || "");
  }, []);
  const onEnterRef = useRef(onEnter);
  onEnterRef.current = onEnter;
  const exitEntered = useCallback(() => {
    if (onEnterRef.current) onEnterRef.current(null);
  }, []);
  const onOpsRef = useRef(onOps);
  onOpsRef.current = onOps;
  const sendOps = useCallback((ops) => (onOpsRef.current ? onOpsRef.current(ops) : null), []);
  const elementOfRef = useRef(elementOf);
  elementOfRef.current = elementOf;
  const selectionKey = idsKey(selection);
  const selectedSet = useMemo(() => new Set(selectionKey.split("\n").filter(Boolean)), [selectionKey]);

  const lightPalette = useMemo(() => paletteOf(dl, "light"), [dl]);
  const palettes = useMemo(() => ({ light: lightPalette, dark: paletteOf(dl, "dark") }), [dl, lightPalette]);
  const env = useMemo(() => {
    const paper = lightPalette["base.surface"] || "#ffffff";
    const edge = palette["base.grid"] || "#e3e5e9";
    return {
      slotKey: `${vizOn}|${team}|${theme}|${entered || ""}|${writable}`,
      slot(p, key) {
        const entry = this.entry;
        const renderer = slotRenderer(p.slot);
        // An underlay (a scene beneath the shared GL canvas) shows only where the canvas cannot draw
        // (too small, too many scenes, the context lost): its faithful drawing when it has one, as
        // it is exact at every size, else its still.
        const underlay = renderer && renderer.mode === "html" && renderer.underlay;
        const fallback = toReact(slotNode(p, ctx, underlay && p.drawn === true ? { still: false } : undefined), key, this);
        const element = elementOfRef.current ? elementOfRef.current(entry.id) : null;
        if (!renderer || !element) return fallback;
        // An HTML-layer slot draws above the SVG: an `underlay` one (scene3d) keeps its still or
        // drawing beneath, shown wherever the HTML layer does not paint.
        if (renderer.mode === "html") return underlay ? fallback : vizOn ? <g key={key} data-slot={p.slot} /> : fallback;
        const { Component } = renderer;
        return (
          <Component
            key={key}
            prim={p}
            entryId={entry.id}
            version={entry.v}
            element={element}
            team={team}
            theme={theme}
            palette={palette}
            palettes={palettes}
            writable={writable}
            paper={paper}
            edge={edge}
            fallback={fallback}
            onStill={stillOnce}
            entered={entered === entry.id}
            onExit={exitEntered}
          />
        );
      },
    };
  }, [dl, palette, lightPalette, palettes, vizOn, team, theme, ctx, stillOnce, entered, exitEntered, writable]);

  // -- rendered -----------------------------------------------------------------------------------
  const onRenderedRef = useRef(onRendered);
  onRenderedRef.current = onRendered;
  useEffect(() => {
    if (dl && onRenderedRef.current && svgRef.current) onRenderedRef.current({ version: dl.version, root: svgRef.current });
  }, [dl, dl?.version]);

  // -- pointer events -------------------------------------------------------------------------------
  const gesture = useRef(null);
  const hoverFrame = useRef(0);
  const pendingHover = useRef(null);

  const screenOf = (e) => {
    const r = wrapRef.current ? wrapRef.current.getBoundingClientRect() : { left: 0, top: 0 };
    return [e.clientX - r.left, e.clientY - r.top];
  };

  const pointerOf = (type, e, screen) => {
    const { cam: c, index: idx, selection: sel, hover: hov, writable: canWrite } = live.current;
    const world = cameraMath.toWorld(c, screen);
    const hit = hitTest(idx, world, c.scale);
    return {
      type,
      world,
      screen,
      scale: c.scale,
      button: e.button ?? 0,
      buttons: e.buttons ?? 0,
      shift: !!e.shiftKey,
      alt: !!e.altKey,
      meta: !!e.metaKey,
      ctrl: !!e.ctrlKey,
      pointerId: e.pointerId ?? 1,
      pointerType: e.pointerType || "mouse",
      hit,
      part: hit ? partAt(idx, hit, world, c.scale) : null,
      handle: canWrite ? handleAt(idx, sel, world, c.scale) : null,
      connector: canWrite ? connectorAt(idx, world, c.scale, { hover: hov }) : null,
    };
  };

  const emit = (type, e) => {
    const handler = live.current.onPointer;
    if (handler) handler(pointerOf(type, e, screenOf(e)));
  };

  const onPointerDown = (e) => {
    const { panMode: pan } = live.current;
    if (e.button === 1 || (pan && e.button === 0)) {
      e.preventDefault();
      gesture.current = { kind: "pan", pointerId: e.pointerId, last: [e.clientX, e.clientY] };
      if (e.currentTarget.setPointerCapture) e.currentTarget.setPointerCapture(e.pointerId);
      return;
    }
    if (e.button !== 0) return;
    gesture.current = { kind: "pointer", pointerId: e.pointerId };
    if (e.currentTarget.setPointerCapture) {
      try {
        e.currentTarget.setPointerCapture(e.pointerId);
      } catch {
        // a synthetic pointer id cannot be captured; the gesture still works inside the surface
      }
    }
    emit("down", e);
  };

  const onPointerMove = (e) => {
    const g = gesture.current;
    if (g && g.kind === "pan" && g.pointerId === e.pointerId) {
      const dx = e.clientX - g.last[0];
      const dy = e.clientY - g.last[1];
      g.last = [e.clientX, e.clientY];
      const { cam: c, onCamera: move } = live.current;
      if (move && (dx || dy)) move(cameraMath.panBy(c, dx, dy));
      return;
    }
    if (g && g.kind === "pointer" && g.pointerId === e.pointerId) {
      emit("move", e);
      return;
    }
    if (g) return;
    // Hover: one per animation frame.
    pendingHover.current = { e: { clientX: e.clientX, clientY: e.clientY, button: -1, buttons: e.buttons, shiftKey: e.shiftKey, altKey: e.altKey, metaKey: e.metaKey, ctrlKey: e.ctrlKey, pointerId: e.pointerId, pointerType: e.pointerType } };
    if (hoverFrame.current) return;
    const raf = typeof requestAnimationFrame === "function" ? requestAnimationFrame : (fn) => setTimeout(fn, 16);
    hoverFrame.current = raf(() => {
      hoverFrame.current = 0;
      if (pendingHover.current && !gesture.current) emit("hover", pendingHover.current.e);
      pendingHover.current = null;
    });
  };

  const endGesture = (e, type) => {
    const g = gesture.current;
    if (!g || g.pointerId !== e.pointerId) return;
    gesture.current = null;
    if (e.currentTarget?.releasePointerCapture) {
      try {
        e.currentTarget.releasePointerCapture(e.pointerId);
      } catch {
        // already released
      }
    }
    if (g.kind === "pointer") emit(type, e);
  };

  const onPointerLeave = (e) => {
    if (!gesture.current) emit("leave", e);
  };

  const onDoubleClick = (e) => emit("dblclick", { ...e, clientX: e.clientX, clientY: e.clientY, button: 0, buttons: 0, pointerId: 1, pointerType: "mouse", shiftKey: e.shiftKey, altKey: e.altKey, metaKey: e.metaKey, ctrlKey: e.ctrlKey });

  // Esc while a pointer gesture is live cancels it (Surface handles no other key).
  useEffect(() => {
    const onKey = (e) => {
      const g = gesture.current;
      if (e.key !== "Escape" || !g || g.kind !== "pointer") return;
      gesture.current = null;
      const handler = live.current.onPointer;
      const { cam: c } = live.current;
      if (handler) handler({ ...pointerOf("cancel", { pointerId: g.pointerId }, [0, 0]), world: cameraMath.toWorld(c, [0, 0]), hit: null, part: null, handle: null, connector: null });
    };
    window.addEventListener("keydown", onKey, true);
    return () => window.removeEventListener("keydown", onKey, true);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // Wheel: zoom about the cursor (mouse wheel, ctrl+wheel = trackpad pinch), pan (two-finger
  // scroll, shift+wheel horizontally). Attached natively: React's wheel listener is passive.
  useEffect(() => {
    const node = wrapRef.current;
    if (!node) return undefined;
    const onWheel = (e) => {
      e.preventDefault();
      const { cam: c, onCamera: move, viewport: v } = live.current;
      if (!move) return;
      let dx = e.deltaX;
      let dy = e.deltaY;
      if (e.deltaMode === 1) {
        dx *= 16;
        dy *= 16;
      } else if (e.deltaMode === 2) {
        dx *= v.w || 800;
        dy *= v.h || 600;
      }
      const screen = screenOf(e);
      if (e.ctrlKey || e.metaKey) {
        zoomingUntil.current = nowMs() + ZOOM_SETTLE_MS;
        move(cameraMath.zoomAt(c, Math.exp(-dy * 0.01), screen));
      } else if (e.shiftKey) {
        move(cameraMath.panBy(c, -(dx || dy), 0));
      } else if (wheelKind(wheelState.current, e, nowMs(), typeof window !== "undefined" ? window.devicePixelRatio || 1 : 1) === "mouse") {
        zoomingUntil.current = nowMs() + ZOOM_SETTLE_MS;
        move(cameraMath.zoomAt(c, Math.exp(-dy * 0.0015), screen));
      } else {
        move(cameraMath.panBy(c, -dx, -dy));
      }
    };
    node.addEventListener("wheel", onWheel, { passive: false });
    return () => node.removeEventListener("wheel", onWheel);
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

  // -- drawing ----------------------------------------------------------------------------------
  const canvasColor = palette["base.canvas"] || "#f7f8fa";
  const selectionColor = palette["base.selection"] || "#6e56cf";
  const surfaceColor = palette["base.surface"] || "#ffffff";
  const scale = drawn.scale;

  const layers = useMemo(
    () =>
      LAYER_NAMES.map((layer) => (
        <g key={layer} data-layer={layer}>
          {visible.map((entry) =>
            layersOf(entry).has(layer) ? (
              <EntryView key={entry.id} entry={entry} layer={layer} ctx={ctx} env={env} transform={entryTransform(entry, preview)} />
            ) : null,
          )}
        </g>
      )),
    [visible, ctx, env, preview],
  );

  const hoverEntry = hover ? index.byId.get(hover) : null;
  const selected = selection.map((id) => index.byId.get(id)).filter(Boolean);
  const single = selected.length === 1 ? selected[0] : null;
  const hoverPartEntry = hoverPart && hoverPart.id ? index.byId.get(hoverPart.id) : null;
  const hoveredPart = hoverPartEntry ? partOf(index, hoverPart.id, hoverPart.part) : null;
  const pinned = [...selected, ...(hoverEntry && !selectedSet.has(hoverEntry.id) ? [hoverEntry] : [])].filter((e) => e.pin === "human" || e.pin === "agent");
  const mutedColor = palette["base.ink_muted"] || "#5b616b";
  const ui = (
    <g data-layer="ui" className="sv2-ui">
      {hoverEntry && !selectedSet.has(hoverEntry.id) ? (
        <Outline entry={hoverEntry} preview={preview} scale={scale} color={selectionColor} gapPx={0} widthPx={1} />
      ) : null}
      {selected.map((entry) => (
        <Outline key={entry.id} entry={entry} preview={preview} scale={scale} color={selectionColor} gapPx={SELECT_GAP_PX} widthPx={1.5} />
      ))}
      {hoveredPart ? <PartOutline entry={hoverPartEntry} part={hoveredPart} preview={preview} scale={scale} color={selectionColor} /> : null}
      {pinned.map((entry) => (
        <PinGlyph key={`pin:${entry.id}`} entry={entry} preview={preview} scale={scale} color={entry.pin === "human" ? selectionColor : mutedColor} surface={surfaceColor} />
      ))}
      {writable && single && !single.locked ? <Handles entry={single} preview={preview} scale={scale} color={selectionColor} surface={surfaceColor} /> : null}
      {writable && hoverEntry && hoverEntry.connect && !hoverEntry.locked
        ? connectorPoints(hoverEntry, scale).map(({ side, point }) => (
            <circle key={side} className="sv2-connector" data-side={side} cx={point[0]} cy={point[1]} r={4 / scale} fill={surfaceColor} stroke={selectionColor} strokeWidth={1.5} vectorEffect="non-scaling-stroke" />
          ))
        : null}
      {hoverEntry ? <Chip entry={hoverEntry} box={previewBox(hoverEntry, preview)} scale={scale} palette={palette} ctx={ctx} /> : null}
      {showChips
        ? selected.filter((e) => e !== hoverEntry).map((entry) => <Chip key={entry.id} entry={entry} box={previewBox(entry, preview)} scale={scale} palette={palette} ctx={ctx} />)
        : null}
      {Array.isArray(preview?.ghost) && preview.ghost.length ? (
        <g className="sv2-ghost">{preview.ghost.map((p, i) => {
          const node = primitiveNode(p, { ...ctx, slot: undefined });
          return node ? toReact(node, i, env) : null;
        })}</g>
      ) : null}
    </g>
  );

  const htmlSlotNodes = visible.flatMap((entry) =>
    htmlSlots(entry).map((p, i) => {
      const element = elementOf ? elementOf(entry.id) : null;
      const renderer = slotRenderer(p.slot);
      if (!element || !htmlSlotOn(renderer, vizOn)) return null;
      const { Component } = renderer;
      const node = (
        <Component
          key={`${entry.id}:${i}`}
          prim={p}
          entryId={entry.id}
          version={entry.v}
          element={element}
          team={team}
          theme={theme}
          palette={palette}
          stillPalette={lightPalette}
          urls={urls}
          writable={writable}
          selected={selectedSet.has(entry.id)}
          onStill={stillOnce}
          entered={entered === entry.id}
          onExit={exitEntered}
          onOps={writable ? sendOps : null}
        />
      );
      const transform = cssTransform(entry, preview);
      return transform ? (
        <div key={`${entry.id}:${i}`} className="sv2-html-preview" style={{ transform, transformOrigin: "0 0" }}>
          {node}
        </div>
      ) : (
        node
      );
    }),
  ).filter(Boolean);
  // The entered entry's live view, for an SVG slot with a Live mode (a chart). Live places itself at
  // the slot box in the HTML layer's world units and keeps the board's gestures off itself.
  const enteredEntry = entered ? index.byId.get(entered) : null;
  const liveSlot = enteredEntry ? liveSlotOf(enteredEntry) : null;
  const liveElement = liveSlot && elementOf ? elementOf(enteredEntry.id) : null;
  if (liveSlot && liveElement) {
    const LiveView = slotRenderer(liveSlot.slot).Live;
    htmlSlotNodes.push(
      <LiveView
        key={`live:${enteredEntry.id}`}
        prim={liveSlot}
        entryId={enteredEntry.id}
        version={enteredEntry.v}
        element={liveElement}
        team={team}
        theme={theme}
        palette={palette}
        onExit={exitEntered}
      />,
    );
  }

  const gridOn = scale >= GRID_MIN_SCALE;
  const matrix = cameraMath.matrix(drawn);
  // Maps the world as drawn onto the current camera while a zoom settles (identity otherwise).
  const k = view.scale / drawn.scale;
  const svgStyle = lazy ? { transform: `matrix(${k}, 0, 0, ${k}, ${(drawn.x - view.x) * view.scale}, ${(drawn.y - view.y) * view.scale})`, transformOrigin: "0 0" } : undefined;
  return (
    <div
      ref={wrapRef}
      className={`sv2-surface${panMode ? " sv2-pan" : ""}`}
      data-theme={theme}
      style={{ background: canvasColor }}
      onPointerDown={onPointerDown}
      onPointerMove={onPointerMove}
      onPointerUp={(e) => endGesture(e, "up")}
      onPointerCancel={(e) => endGesture(e, "cancel")}
      onPointerLeave={onPointerLeave}
      onDoubleClick={onDoubleClick}
      onContextMenu={(e) => e.preventDefault()}
    >
      <div className="sv2-zoomer" style={svgStyle}>
      <svg ref={svgRef} className="sv2-svg" xmlns="http://www.w3.org/2000/svg" data-version={dl ? String(dl.version) : undefined} data-theme={theme} data-settled={lazy ? "0" : "1"}>
        <defs>
          {defsNodes(defs).map((node, i) => toReact(node, i, env))}
          {gridOn ? (
            <pattern id="sv2-grid" x={-GRID_STEP / 2} y={-GRID_STEP / 2} width={GRID_STEP} height={GRID_STEP} patternUnits="userSpaceOnUse">
              <circle cx={GRID_STEP / 2} cy={GRID_STEP / 2} r={1 / scale} fill={palette["base.grid"] || "#e3e5e9"} />
            </pattern>
          ) : null}
        </defs>
        <rect className="sv2-bg" x={0} y={0} width="100%" height="100%" fill={canvasColor} />
        <g className="sv2-world" transform={matrix}>
          {gridOn && measured ? (
            <rect className="sv2-grid" x={viewRect[0]} y={viewRect[1]} width={viewRect[2] - viewRect[0]} height={viewRect[3] - viewRect[1]} fill="url(#sv2-grid)" />
          ) : null}
          {layers}
          {ui}
        </g>
      </svg>
      </div>
      {screenOverlay ? <div className="sv2-screen">{screenOverlay}</div> : null}
      <div ref={glHostRef} className="sv2-gl-host" data-gl-host="" />
      {htmlSlotNodes.length ? (
        <div className="sv2-html" style={{ transform: `matrix(${view.scale}, 0, 0, ${view.scale}, ${-view.x * view.scale}, ${-view.y * view.scale})` }}>
          {htmlSlotNodes}
        </div>
      ) : null}
      <div className="sv2-overlay">{children}</div>
    </div>
  );
}

// A notched mouse wheel zooms; a trackpad's two-finger scroll pans. Chrome's legacy wheelDelta is a
// multiple of 120 per wheel notch, divided by the device pixel ratio where Chrome scales wheel
// deltas (it does under device emulation); without it, a line-mode or large whole-number vertical
// delta is a wheel.
export function isMouseWheel(e, dpr = 1) {
  if (e.deltaMode !== 0) return true;
  if (e.deltaX !== 0) return false;
  const wd = e.wheelDeltaY;
  if (typeof wd === "number" && wd !== 0) {
    const a = Math.abs(wd);
    return a % 120 === 0 || (dpr > 1 && Math.abs((a * dpr) % 120) < 1e-6);
  }
  return Number.isInteger(e.deltaY) && Math.abs(e.deltaY) * dpr >= 50;
}

// Wheel events in one burst (each within WHEEL_BURST_MS of the last) keep the kind the burst started
// as, once it has shown itself a trackpad: a scroll delta that happens to look like a notch never
// turns a two-finger scroll into a zoom halfway through.
const WHEEL_BURST_MS = 120;
export function wheelKind(state, e, now, dpr = 1) {
  const within = state.at && now - state.at < WHEEL_BURST_MS;
  const kind = within && state.kind === "trackpad" ? "trackpad" : isMouseWheel(e, dpr) ? "mouse" : "trackpad";
  state.kind = kind;
  state.at = now;
  return kind;
}

export default Surface;
