// Exact exports on the v2 board (canvas-v2-phase1.md 3.4 and 4.5): the server's export_request
// (for `canvas look --exact`) is answered with the display list drawn by toSVGString, in the
// light theme, rasterised here and POSTed to /exports/<request_id>.
//
// An SVG drawn as an image cannot reach the page's web fonts, so the export embeds the bundled
// faces it uses as data: URLs; the id badges and the labelled grid the request asks for are
// drawn on the raster afterwards, as the v1 page does.
import interRegular from "../../../../assets/fonts/inter/Inter-Regular.ttf?url";
import interMedium from "../../../../assets/fonts/inter/Inter-Medium.ttf?url";
import interSemiBold from "../../../../assets/fonts/inter/Inter-SemiBold.ttf?url";
import interBold from "../../../../assets/fonts/inter/Inter-Bold.ttf?url";
import geistMono from "../../../../assets/fonts/geist-mono/GeistMono-Regular.ttf?url";

export const EXPORT_MAX_PX = 2048;
export const PAGE_FAMILIES = { sans: '"Synapse Sans"', mono: '"Synapse Mono"' };
const FACES = {
  sans: { 400: interRegular, 500: interMedium, 600: interSemiBold, 700: interBold },
  mono: { 400: geistMono },
};
const MARK_FONT = '600 12px "Synapse Sans", sans-serif';

// The faces the list's text uses: [{font, weight}].
export function facesUsed(dl) {
  const seen = new Map();
  const walk = (items) => {
    for (const p of items || []) {
      if (!p || typeof p !== "object") continue;
      if (p.k === "text") {
        const font = p.font === "mono" ? "mono" : "sans";
        const table = FACES[font];
        const weights = Object.keys(table).map(Number);
        const weight = weights.reduce((best, w) => (Math.abs(w - (p.weight || 400)) < Math.abs(best - (p.weight || 400)) ? w : best), weights[0]);
        seen.set(`${font}-${weight}`, { font, weight });
      }
      if (p.k === "group") walk(p.items);
      if (p.k === "slot") walk(p.fallback);
    }
  };
  for (const entry of (dl && dl.entries) || []) walk(entry.items);
  return [...seen.values()];
}

async function dataURL(url, fetchImpl) {
  const res = await fetchImpl(url);
  if (!res.ok) throw new Error(`font ${url}: HTTP ${res.status}`);
  const bytes = new Uint8Array(await res.arrayBuffer());
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return `data:font/ttf;base64,${btoa(binary)}`;
}

const fontCache = new Map();

// <style> with an @font-face per face used, inserted right after the opening <svg> tag.
export async function embedFonts(svg, dl, { fetchImpl = fetch } = {}) {
  const rules = [];
  for (const { font, weight } of facesUsed(dl)) {
    const url = FACES[font][weight];
    if (!fontCache.has(url)) fontCache.set(url, dataURL(url, fetchImpl).catch(() => null));
    const src = await fontCache.get(url);
    if (src) rules.push(`@font-face{font-family:${PAGE_FAMILIES[font]};font-weight:${weight};src:url(${src}) format("truetype");}`);
  }
  if (!rules.length) return svg;
  const at = svg.indexOf(">", svg.indexOf("<svg"));
  if (at < 0) return svg;
  return `${svg.slice(0, at + 1)}<style>${rules.join("")}</style>${svg.slice(at + 1)}`;
}

function svgDataURL(svg) {
  const bytes = new TextEncoder().encode(svg);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return `data:image/svg+xml;base64,${btoa(binary)}`;
}

function loadImage(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("the export did not load"));
    image.src = url;
  });
}

// Answers one export request. deps: {toSVGString, post(requestId, blob), urls, families, scene()}.
export async function answerExport(request, dl, deps) {
  if (!request || !Array.isArray(request.region) || !dl) return false;
  const [x0, y0, x1, y1] = request.region;
  const width = Math.max(1, x1 - x0);
  const height = Math.max(1, y1 - y0);
  const svg = deps.toSVGString(dl, { theme: "light", region: request.region, maxPx: EXPORT_MAX_PX, families: deps.families || PAGE_FAMILIES, urls: deps.urls });
  const image = await loadImage(svgDataURL(await embedFonts(svg, dl)));
  const scale = Math.min(2, EXPORT_MAX_PX / Math.max(width, height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(width * scale));
  canvas.height = Math.max(1, Math.round(height * scale));
  const context = canvas.getContext("2d");
  const background = (dl.palettes && dl.palettes.light && dl.palettes.light["base.canvas"]) || "#ffffff";
  context.fillStyle = background;
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.drawImage(image, 0, 0, canvas.width, canvas.height);
  const toPx = (x, y) => [(x - x0) * scale, (y - y0) * scale];
  const ink = (dl.palettes && dl.palettes.light && dl.palettes.light["base.ink"]) || "#1c2024";
  if (request.grid) {
    context.fillStyle = "rgba(73,80,87,0.55)";
    context.font = "10px sans-serif";
    for (let gx = Math.ceil(x0 / 100) * 100; gx <= x1; gx += 100) {
      for (let gy = Math.ceil(y0 / 100) * 100; gy <= y1; gy += 100) {
        const [px, py] = toPx(gx, gy);
        context.fillRect(px - 1.5, py - 1.5, 3, 3);
        if (gx % 200 === 0 && gy % 200 === 0) context.fillText(`c${gx / 20}r${gy / 20}`, px + 3, py - 3);
      }
    }
  }
  if (request.marks) {
    context.font = MARK_FONT;
    for (const el of (deps.scene() && deps.scene().elements) || []) {
      if (el.x > x1 || el.y > y1 || el.x + (el.w || 1) < x0 || el.y + (el.h || 1) < y0) continue;
      const [px, py] = toPx(Math.max(el.x, x0), Math.max(el.y, y0));
      const w = context.measureText(el.id).width + 8;
      context.fillStyle = ink;
      context.fillRect(px, py, w, 16);
      context.fillStyle = background;
      context.fillText(el.id, px + 4, py + 12);
    }
  }
  const blob = await new Promise((resolve) => canvas.toBlob(resolve, "image/png"));
  if (!blob) return false;
  await deps.post(request.request_id, blob);
  return true;
}
