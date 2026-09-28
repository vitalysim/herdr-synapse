// slot "scene3d" (canvas-v2-phase3-4.md 3.7): a placeholder div in the Surface's HTML layer at the
// slot box. The shared renderer (./renderer.js, a lazy chunk with three.js) draws the scene into the
// placeholder's rectangle on its one canvas; until it has, and whenever it cannot (too small, too
// many scenes, WebGL lost or missing), the SVG beneath shows the slot's still or drawing (the slot is
// an `underlay` slot), so the placeholder itself is transparent.
//
// Entered (a double-click on the scene, WW-3): pointer events reach the placeholder, OrbitControls
// turn this scene's own camera (listening on an orbit layer inside the placeholder, beside the bar,
// so a press on a button is never captured by the controls), and a "Save view" button (writable pages only) sends
// patch {id, set: {camera: {preset: "orbit", az, el, zoom}}, if_version}. Esc leaves (the Board).
import React, { useCallback, useEffect, useRef, useState } from "react";
import { sceneLabel } from "./aria.js";
import { noteMounted, noteUnmounted } from "./qa.js";

let chunk = null;
function loadRenderer() {
  if (!chunk) chunk = import("./renderer.js");
  return chunk;
}

function stop(e) {
  e.stopPropagation();
}

export default function Scene3DSlot({ prim, entryId, version, element, team, onStill, entered = false, onExit, palette, stillPalette, theme = "light", urls, writable = false, onOps }) {
  const ref = useRef(null);
  const orbitRef = useRef(null);
  const handle = useRef(null); // {renderer, key}
  const [state, setState] = useState({ drawn: false, failed: false });
  const key = `${team}:${entryId}`;
  const w = Number(prim.w);
  const h = Number(prim.h);
  const paperOf = (p) => (p && (p["chart.paper"] || p["base.surface"])) || "#ffffff";
  const props = {
    id: entryId,
    version,
    element,
    theme,
    palette,
    stillPalette: stillPalette || palette,
    paper: paperOf(palette),
    stillPaper: paperOf(stillPalette || palette),
    box: [w, h],
    urls,
    writable,
    onStill: writable ? onStill : null,
  };
  const latest = useRef(props);
  latest.current = props;

  useEffect(() => {
    let live = true;
    let off = () => {};
    noteMounted(key, entryId, version);
    loadRenderer()
      .then(({ rendererFor }) => {
        if (!live || !ref.current) return;
        const host = ref.current.closest(".sv2-surface")?.querySelector("[data-gl-host]");
        if (!host) throw new Error("no GL host in the Surface");
        const renderer = rendererFor(host);
        renderer.register(key, { ...latest.current, el: ref.current });
        handle.current = { renderer, key };
        off = renderer.subscribe(key, (s) => setState((prev) => (prev.drawn === s.drawn ? prev : { ...prev, drawn: s.drawn })));
      })
      .catch((err) => {
        if (live) setState((prev) => ({ ...prev, failed: true }));
        console.warn(`[synapse v2] ${entryId}: the 3D renderer is unavailable (${err && err.message}); the scene shows its still`);
      });
    return () => {
      live = false;
      off();
      noteUnmounted(key);
      if (handle.current) handle.current.renderer.unregister(key);
      handle.current = null;
    };
  }, [key]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    noteMounted(key, entryId, version);
    if (handle.current) handle.current.renderer.update(key, { ...props, el: ref.current });
  }, [version, element, theme, palette, stillPalette, w, h, writable]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const hd = handle.current;
    if (!hd) return undefined;
    if (entered) hd.renderer.enter(key, orbitRef.current);
    else hd.renderer.exit(key);
    return undefined;
  }, [entered, state.drawn, key]);

  // While entered, wheel and pointer gestures belong to the orbit controls, not the board.
  useEffect(() => {
    const node = ref.current;
    if (!node || !entered) return undefined;
    node.addEventListener("wheel", stop, { passive: true });
    node.addEventListener("pointerdown", stop);
    node.addEventListener("dblclick", stop);
    return () => {
      node.removeEventListener("wheel", stop, { passive: true });
      node.removeEventListener("pointerdown", stop);
      node.removeEventListener("dblclick", stop);
    };
  }, [entered]);

  const saveView = useCallback(
    (e) => {
      e.stopPropagation();
      const hd = handle.current;
      const view = hd ? hd.renderer.view(hd.key) : null;
      if (!view || !onOps) return;
      onOps([{ op: "patch", id: entryId, set: { camera: view }, if_version: version }]);
    },
    [entryId, version, onOps],
  );

  return (
    <div
      ref={ref}
      className={`sv2-scene3d${entered ? " sv2-entered" : ""}`}
      data-slot="scene3d"
      data-id={entryId}
      data-drawn={state.drawn ? "1" : "0"}
      role="img"
      aria-label={sceneLabel(element)}
      style={{ left: prim.x, top: prim.y, width: w, height: h, pointerEvents: entered ? "auto" : "none", touchAction: entered ? "none" : undefined }}
    >
      {entered ? <div ref={orbitRef} className="sv2-orbit" /> : null}
      {entered ? (
        <div className="sv2-enter-bar" onPointerDown={stop}>
          <span className="sv2-enter-hint">drag to orbit · scroll to zoom · Esc to leave</span>
          {writable && onOps ? (
            <button type="button" className="sv2-save-view" onClick={saveView}>
              Save view
            </button>
          ) : null}
          {onExit ? (
            <button type="button" className="sv2-leave" onClick={(e) => { e.stopPropagation(); onExit(); }}>
              Done
            </button>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
