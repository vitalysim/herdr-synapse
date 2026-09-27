// The browser check (canvas-v2-phase1.md 3.3 and 4.2): after the fonts load, measure every drawn
// text line with the page's own fonts and report the lines the server measured narrower than the
// browser draws them. The page posts these to /measure; the server only ever widens.
import { fmt } from "./fmt.js";
import { PAGE_FAMILIES } from "./svgAttrs.js";

const SLACK = 0.5;

function textItems(items, out) {
  for (const p of Array.isArray(items) ? items : []) {
    if (!p || typeof p !== "object") continue;
    if (p.k === "text") out.push(p);
    // Screen-anchored groups (pin numbers, region labels) are page chrome at a fixed pixel size,
    // never fitted to a box; slots' fallbacks are placeholders. Neither is checked.
    else if (p.k === "group" && !p.screen) textItems(p.items, out);
  }
  return out;
}

function defaultContext() {
  if (typeof OffscreenCanvas !== "undefined") return new OffscreenCanvas(8, 8).getContext("2d");
  if (typeof document !== "undefined") return document.createElement("canvas").getContext("2d");
  return null;
}

/**
 * The drawn lines wider than the server's `w + 0.5`, as [{id, font, weight, size, t, w}], `w` being
 * the browser's width. Only entries on screen under `rootSvg` (a [data-id] group) are measured.
 * `options.context` (a 2D context) and `options.fontsReady` (a promise) are for tests.
 */
export async function verifyText(rootSvg, dl, options = {}) {
  const fontsReady = options.fontsReady ?? (typeof document !== "undefined" && document.fonts ? document.fonts.ready : Promise.resolve());
  await fontsReady;
  const context = options.context || defaultContext();
  if (!context || !dl || !Array.isArray(dl.entries)) return [];
  const families = options.families || PAGE_FAMILIES;
  let drawn = null;
  if (rootSvg && typeof rootSvg.querySelectorAll === "function") {
    drawn = new Set([...rootSvg.querySelectorAll("[data-id]")].map((node) => node.getAttribute("data-id")));
  }
  const seen = new Set();
  const out = [];
  for (const entry of dl.entries) {
    if (!entry || (drawn && !drawn.has(entry.id))) continue;
    for (const p of textItems(entry.items, [])) {
      const size = Number(p.size);
      const weight = Number(p.weight) || 400;
      const font = p.font === "mono" ? "mono" : "sans";
      if (!(size > 0)) continue;
      context.font = `${weight} ${size}px ${families[font]}`;
      for (const line of Array.isArray(p.lines) ? p.lines : []) {
        const t = String(line?.t ?? "");
        const server = Number(line?.w);
        if (!t.trim() || !Number.isFinite(server)) continue;
        const key = `${entry.id}|${font}|${weight}|${size}|${t}`;
        if (seen.has(key)) continue;
        seen.add(key);
        const w = context.measureText(t).width;
        if (w > server + SLACK) out.push({ id: entry.id, font, weight, size, t, w: Number(fmt(w)) });
      }
    }
  }
  return out;
}
