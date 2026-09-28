// The entered chart (canvas-v2-phase3-4.md 2.10, D4, WW-3): mounted by the Surface in the HTML
// layer, in world coordinates over the static picture, only while its entry is entered (a
// double-click; Esc leaves). A live ECharts instance in SVG mode with the same resolved option,
// animation still off, tooltips in richText (drawn by zrender, never HTML) and hover. Disposed on
// exit. GL charts mount through scene3d/glCharts.js under the GL budget; a Vega-Lite chart has no
// live mode and shows its static picture.
import React, { useEffect, useRef, useState } from "react";
import { fallbackPalette, warnOnce } from "../theme/palette.js";
import { engineOf, loadRender, themeOfPalette } from "./picture.js";
import { pictureFor } from "./slot.jsx";

/** Mounts the live chart into `div`; resolves to {dispose} (render.js, loaded on first use). */
export function mountLiveChart(div, opts) {
  return loadRender().then((m) => m.mountLiveChart(div, opts));
}

export default function ChartLive({ prim, entryId, element, team = "", theme, palette, onExit }) {
  const host = useRef(null);
  const [image, setImage] = useState(null);
  const x = Number(prim?.x) || 0;
  const y = Number(prim?.y) || 0;
  const w = Math.max(1, Math.round(Number(prim?.w) || 0));
  const h = Math.max(1, Math.round(Number(prim?.h) || 0));
  const shown = theme === "dark" || theme === "light" ? theme : themeOfPalette(palette);
  const ownPalette = palette || fallbackPalette(shown);
  const paper = ownPalette["chart.paper"] || ownPalette["base.surface"] || (shown === "dark" ? "#1b1c20" : "#ffffff");
  const engine = element ? engineOf(element) : null;
  const seq = element ? element.updated_seq : null;

  // Focused on entry, so Esc reaches it (the Board's own Esc handling also exits).
  const outer = useRef(null);
  useEffect(() => {
    if (outer.current && typeof outer.current.focus === "function") outer.current.focus({ preventScroll: true });
  }, [entryId]);

  useEffect(() => {
    if (!element || !host.current) return undefined;
    let handle = null;
    let live = true;
    if (engine === "vega-lite") {
      pictureFor({ team, element, prim, theme: shown, palette: ownPalette, w, h })
        .then((pic) => live && setImage(pic.dataURL))
        .catch(() => undefined);
    } else {
      mountLiveChart(host.current, { element, prim, team, theme: shown, palette: ownPalette, w, h })
        .then((h2) => {
          if (live) handle = h2;
          else if (h2 && typeof h2.dispose === "function") h2.dispose();
        })
        .catch((err) => warnOnce(`chart-live:${entryId}`, `chart ${entryId} could not go live: ${String(err && err.message ? err.message : err).slice(0, 120)}`));
    }
    return () => {
      live = false;
      if (handle && typeof handle.dispose === "function") handle.dispose();
      handle = null;
    };
  }, [entryId, seq, shown, w, h, team]); // eslint-disable-line react-hooks/exhaustive-deps

  // The board keeps its gestures (pan, marquee, drag) off an entered chart; wheel zoom still reaches it.
  const stop = (e) => e.stopPropagation();
  const onKeyDown = (e) => {
    if (e.key === "Escape" && onExit) {
      e.stopPropagation();
      onExit();
    }
  };
  return (
    <div
      ref={outer}
      className="sv2-chart-live"
      data-slot="chart"
      data-id={entryId}
      data-live="1"
      tabIndex={-1}
      style={{ position: "absolute", left: x, top: y, width: w, height: h, background: paper, pointerEvents: "auto", outline: "none" }}
      onPointerDown={stop}
      onPointerUp={stop}
      onDoubleClick={stop}
      onKeyDown={onKeyDown}
    >
      {image ? <img alt="" src={image} width={w} height={h} draggable={false} /> : <div ref={host} style={{ width: w, height: h }} />}
    </div>
  );
}
