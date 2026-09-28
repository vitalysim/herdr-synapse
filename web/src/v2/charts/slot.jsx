// slot "chart" (canvas-v2-phase3-4.md 2.10, D4, D17). An svg-mode slot: it shows the display
// list's fallback at once (the Python drawing of the chart, or its still), then the static ECharts
// picture as an SVG image once the chart chunk has drawn it (the fallback stays beneath, hidden). The image is crisp at every zoom and
// adds no DOM per mark; a live chart (tooltips, hover) exists only while the chart is entered
// (live.jsx, mounted by the Surface in the HTML layer).
//
// Writable pages post one still per (id, v), always drawn in the LIGHT theme whatever the page
// shows (D17: a dark agent picture uses the Python drawing), rasterised at the slot box ×2 and
// capped at 1024 px. A picture that cannot be drawn leaves the fallback in place and logs once:
// the chart is never blank.
import React, { useEffect, useState } from "react";
import { rasterize } from "../../canvas/renderers.js";
import { fallbackPalette, warnOnce } from "../theme/palette.js";
import { cachedPicture, engineOf, loadRender, pictureKey, themeOfPalette, typeOf } from "./picture.js";
import { reportChart } from "./qa.js";

const posted = new Set();

/** The picture of `element` for one theme, through the cache. */
export function pictureFor(args) {
  const { team, element, theme, w, h } = args;
  return cachedPicture(pictureKey(team, element, theme, w, h), () => loadRender().then((m) => m.pictureOf(args)));
}

/**
 * Posts the light still of (entryId, version) once for the page's life. `light` resolves to the
 * light picture. Returns the promise of the post (or null when nothing is posted).
 */
export function postStillOnce({ entryId, version, onStill, light, w, h }) {
  if (!onStill || !entryId) return null;
  const key = `${entryId}@${version}`;
  if (posted.has(key)) return null;
  posted.add(key);
  return light()
    .then((picture) => rasterize(picture.dataURL, w, h, 1024))
    .then((blob) => onStill(entryId, version, blob))
    .catch(() => posted.delete(key));
}

/** Test hook. */
export function resetPostedStills() {
  posted.clear();
}

export default function ChartSlot({ prim, entryId, version, element, team = "", paper, edge, fallback = null, onStill, palette, palettes, theme, writable, entered = false }) {
  const [state, setState] = useState(null);
  const x = Number(prim?.x) || 0;
  const y = Number(prim?.y) || 0;
  const w = Math.max(1, Math.round(Number(prim?.w) || 0));
  const h = Math.max(1, Math.round(Number(prim?.h) || 0));
  const shown = theme === "dark" || theme === "light" ? theme : themeOfPalette(palette);
  const key = element ? pictureKey(team, element, shown, w, h) : null;

  useEffect(() => {
    if (!element || !key) return undefined;
    let live = true;
    const id = entryId || element.id;
    reportChart(id, { engine: engineOf(element), type: typeOf(element), rendered: false, failed: null });
    const ownPalette = palette || fallbackPalette(shown);
    pictureFor({ team, element, prim, theme: shown, palette: ownPalette, w, h })
      .then((pic) => {
        if (!live) return;
        setState({ key, pic });
        reportChart(id, { rendered: true, labelOverlaps: pic.labelOverlaps, texts: Array.isArray(pic.texts) ? pic.texts.slice(0, 400) : null, failed: null });
      })
      .catch((err) => {
        const reason = String(err && err.message ? err.message : err).slice(0, 160);
        warnOnce(`chart:${key}`, `chart ${id} shows its fallback: ${reason}`);
        if (live) reportChart(id, { rendered: false, failed: reason });
      });
    if (writable !== false && onStill) {
      const lightPalette = shown === "light" ? ownPalette : (palettes && palettes.light) || fallbackPalette("light");
      postStillOnce({
        entryId: id,
        version,
        onStill,
        w,
        h,
        light: () => pictureFor({ team, element, prim, theme: "light", palette: lightPalette, w, h }),
      });
    }
    return () => {
      live = false;
    };
    // The key holds what the picture depends on: team, id, version, theme and box.
  }, [key, version]); // eslint-disable-line react-hooks/exhaustive-deps

  const picture = state && state.key === key ? state.pic : null;
  if (!picture) return fallback;
  // Vega-Lite draws with the light tokens only (canvas/renderers.js), so on a dark board it sits on
  // a light paper card, as Phase 1 drew every chart; ECharts pictures follow the theme.
  const card = picture.engine === "vega-lite" && shown === "dark";
  // The picture is an image, so its labels are not page text. The fallback (the Python drawing the
  // server's SVG writer draws too) stays beneath it, hidden: the page's text lines match the
  // server's whether or not the picture has arrived (canvas_qa's line comparison), and the image
  // carries the chart's gist for assistive technology.
  const label = Array.isArray(element?.gist) ? element.gist.map(String).join(". ").slice(0, 600) : undefined;
  return (
    <g data-slot="chart" data-chart-engine={picture.engine} data-entered={entered ? "1" : undefined}>
      {card ? <rect x={x} y={y} width={w} height={h} rx={8} fill={paper || "#ffffff"} stroke={edge || "none"} strokeWidth={1} /> : null}
      <image data-chart-picture="" x={x} y={y} width={w} height={h} preserveAspectRatio="none" href={picture.dataURL} role="img" aria-label={label} />
      {fallback ? (
        <g data-chart-fallback="" visibility="hidden" aria-hidden="true" pointerEvents="none">
          {fallback}
        </g>
      ) : null}
    </g>
  );
}
