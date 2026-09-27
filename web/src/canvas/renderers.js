// Turns canonical elements that are pictures (path, svg, mermaid, chart, image, and the
// placeholder of a switched-off viz) into data URLs Excalidraw can show as image files.
// Everything renders locally: Mermaid with securityLevel "strict" and no HTML labels,
// Vega-Lite through vega-interpreter (no eval) with a loader that refuses every URL, so a
// chart only ever sees the data the page hands it from the team's artifacts.
import { blobToDataURL, getBlob, getJSON, getText, teamPath } from "../api.js";

const cache = new Map();

export function cached(key, make) {
  if (!cache.has(key)) {
    const promise = make().catch((err) => {
      cache.delete(key);
      throw err;
    });
    cache.set(key, promise);
  }
  return cache.get(key);
}

// Base64, not percent-encoded: Excalidraw decodes image files with atob().
export function svgToDataURL(svg) {
  const bytes = new TextEncoder().encode(svg);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 0x8000) binary += String.fromCharCode(...bytes.subarray(i, i + 0x8000));
  return `data:image/svg+xml;base64,${btoa(binary)}`;
}

// An SVG at its own size (from its viewBox), for an <img> that should not stretch to its box.
export function naturalSVG(svgText) {
  const doc = new DOMParser().parseFromString(svgText, "image/svg+xml");
  const root = doc.documentElement;
  const box = (root.getAttribute("viewBox") || "").split(/[\s,]+/).map(Number);
  if (box.length === 4 && box.every(Number.isFinite)) {
    root.setAttribute("width", String(Math.ceil(box[2])));
    root.setAttribute("height", String(Math.ceil(box[3])));
  }
  root.removeAttribute("style");
  return new XMLSerializer().serializeToString(root);
}

const escapeXML = (text) =>
  String(text ?? "").replace(/[<>&"']/g, (ch) => ({ "<": "&lt;", ">": "&gt;", "&": "&amp;", '"': "&quot;", "'": "&#39;" })[ch]);

// Fit an SVG document into a w×h box keeping its aspect ratio, so Excalidraw (which
// stretches images to the element's box) draws it undistorted.
export function fitSVG(svgText, w, h) {
  const doc = new DOMParser().parseFromString(svgText, "image/svg+xml");
  const root = doc.documentElement;
  if (!root || root.nodeName.toLowerCase() !== "svg" || doc.querySelector("parsererror")) {
    throw new Error("not an SVG document");
  }
  if (!root.getAttribute("viewBox")) {
    const width = parseFloat(root.getAttribute("width")) || w;
    const height = parseFloat(root.getAttribute("height")) || h;
    root.setAttribute("viewBox", `0 0 ${width} ${height}`);
  }
  root.setAttribute("xmlns", "http://www.w3.org/2000/svg");
  root.setAttribute("width", String(Math.max(1, Math.round(w))));
  root.setAttribute("height", String(Math.max(1, Math.round(h))));
  root.setAttribute("preserveAspectRatio", "xMidYMid meet");
  root.removeAttribute("style");
  return new XMLSerializer().serializeToString(root);
}

// A card for things that are not drawn here: a switched-off live visual, a failed render.
export function placeholderSVG(w, h, title, line, tone = "#868e96") {
  const width = Math.max(40, Math.round(w));
  const height = Math.max(30, Math.round(h));
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" width="${width}" height="${height}" viewBox="0 0 ${width} ${height}">` +
    `<rect x="1" y="1" width="${width - 2}" height="${height - 2}" rx="8" fill="#f8f9fa" stroke="${tone}" stroke-dasharray="6 4"/>` +
    `<text x="${width / 2}" y="${height / 2 - 6}" text-anchor="middle" font-family="sans-serif" font-size="16" fill="#1e1e1e">${escapeXML(
      String(title || "").slice(0, 60),
    )}</text>` +
    `<text x="${width / 2}" y="${height / 2 + 16}" text-anchor="middle" font-family="sans-serif" font-size="12" fill="${tone}">${escapeXML(
      String(line || "").slice(0, 80),
    )}</text></svg>`
  );
}

// -- Mermaid ----------------------------------------------------------------------------

let mermaidReady = null;
let mermaidCounter = 0;

function loadMermaid() {
  if (!mermaidReady) {
    mermaidReady = import("mermaid").then(({ default: mermaid }) => {
      mermaid.initialize({
        startOnLoad: false,
        securityLevel: "strict",
        htmlLabels: false,
        flowchart: { htmlLabels: false },
        theme: "default",
        fontFamily: "sans-serif",
      });
      return mermaid;
    });
  }
  return mermaidReady;
}

// Mermaid renders one diagram at a time; queue them.
let mermaidQueue = Promise.resolve();

export function renderMermaid(source) {
  const run = async () => {
    const mermaid = await loadMermaid();
    mermaidCounter += 1;
    const { svg } = await mermaid.render(`synapse-mermaid-${mermaidCounter}`, String(source || ""));
    return svg;
  };
  const next = mermaidQueue.then(run, run);
  mermaidQueue = next.catch(() => undefined);
  return next;
}

// -- Vega-Lite --------------------------------------------------------------------------

let vegaReady = null;

function loadVega() {
  if (!vegaReady) {
    vegaReady = Promise.all([import("vega"), import("vega-lite"), import("vega-interpreter")]).then(([vega, vegaLite, interp]) => ({
      vega,
      vegaLite,
      expressionInterpreter: interp.expressionInterpreter,
    }));
  }
  return vegaReady;
}

const refuseLoader = {
  load: () => Promise.reject(new Error("charts read data only through the page")),
  sanitize: () => Promise.reject(new Error("charts read data only through the page")),
  http: () => Promise.reject(new Error("no network")),
  file: () => Promise.reject(new Error("no files")),
};

export function parseDelimited(text, sep) {
  const rows = [];
  let row = [];
  let field = "";
  let quoted = false;
  for (let i = 0; i < text.length; i += 1) {
    const ch = text[i];
    if (quoted) {
      if (ch === '"' && text[i + 1] === '"') {
        field += '"';
        i += 1;
      } else if (ch === '"') {
        quoted = false;
      } else {
        field += ch;
      }
    } else if (ch === '"') {
      quoted = true;
    } else if (ch === sep) {
      row.push(field);
      field = "";
    } else if (ch === "\n" || ch === "\r") {
      if (ch === "\r" && text[i + 1] === "\n") i += 1;
      row.push(field);
      rows.push(row);
      row = [];
      field = "";
    } else {
      field += ch;
    }
  }
  if (field !== "" || row.length) {
    row.push(field);
    rows.push(row);
  }
  const header = rows.shift() || [];
  return rows
    .filter((r) => r.length > 1 || (r.length === 1 && r[0] !== ""))
    .map((r) => {
      const obj = {};
      header.forEach((name, i) => {
        const raw = r[i] ?? "";
        const num = raw.trim() !== "" && Number.isFinite(Number(raw)) ? Number(raw) : null;
        obj[name] = num === null ? raw : num;
      });
      return obj;
    });
}

// Chart and viz data from the team's artifacts dir, parsed by extension.
export function loadArtifactData(team, rel) {
  return cached(`artifact:${team}:${rel}`, async () => {
    const text = await getText(`${teamPath(team, "artifact")}?path=${encodeURIComponent(rel)}`);
    const lower = rel.toLowerCase();
    if (lower.endsWith(".json")) return JSON.parse(text);
    return parseDelimited(text, lower.endsWith(".tsv") ? "\t" : ",");
  });
}

function stripURLs(value) {
  if (Array.isArray(value)) return value.map(stripURLs);
  if (value && typeof value === "object") {
    const out = {};
    for (const [key, inner] of Object.entries(value)) {
      if (key === "url" || key === "href") continue;
      out[key] = stripURLs(inner);
    }
    return out;
  }
  return value;
}

export async function renderChart(team, element, w, h) {
  const { vega, vegaLite, expressionInterpreter } = await loadVega();
  const spec = stripURLs(await getJSON(teamPath(team, `assets/${element.spec_asset}`)));
  if (element.data) {
    spec.data = { values: await loadArtifactData(team, element.data) };
  }
  if (spec.width === undefined) spec.width = Math.max(80, Math.round(w) - 60);
  if (spec.height === undefined) spec.height = Math.max(60, Math.round(h) - 60);
  const compiled = vegaLite.compile(spec).spec;
  const view = new vega.View(vega.parse(compiled, null, { ast: true }), {
    expr: expressionInterpreter,
    renderer: "none",
    loader: refuseLoader,
  });
  try {
    await view.runAsync();
    return await view.toSVG();
  } finally {
    view.finalize();
  }
}

// -- svg blocks and paths ---------------------------------------------------------------

export async function sketchySVG(svgText) {
  const { Svg2Roughjs, OutputType } = await import("svg2roughjs");
  const host = document.createElement("div");
  host.style.cssText = "position:absolute;left:-10000px;top:0;width:0;height:0;overflow:hidden";
  document.body.appendChild(host);
  try {
    const doc = new DOMParser().parseFromString(svgText, "image/svg+xml");
    const target = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    host.appendChild(target);
    const converter = new Svg2Roughjs(target, OutputType.SVG);
    converter.svg = document.importNode(doc.documentElement, true);
    await converter.sketch();
    return new XMLSerializer().serializeToString(target);
  } finally {
    host.remove();
  }
}

// A `path` element's `d` (already sanitised server-side: commands and numbers only),
// drawn with its style and fitted to the element's box through its own bounding box.
export function pathSVG(element) {
  const style = element.style || {};
  const stroke = style.stroke || "#1e1e1e";
  const fill = style.fill || "none";
  const width = style.width || 2;
  const probe = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  probe.setAttribute("style", "position:absolute;left:-10000px;top:0;width:10px;height:10px");
  const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
  path.setAttribute("d", String(element.d || ""));
  probe.appendChild(path);
  document.body.appendChild(probe);
  let box = { x: 0, y: 0, width: element.w || 1, height: element.h || 1 };
  try {
    const measured = path.getBBox();
    if (measured.width > 0 || measured.height > 0) box = measured;
  } catch {
    // an unmeasurable path keeps the element's own box
  } finally {
    probe.remove();
  }
  const pad = width;
  const viewBox = `${box.x - pad} ${box.y - pad} ${Math.max(1, box.width) + pad * 2} ${Math.max(1, box.height) + pad * 2}`;
  const dash = style.dash === "dashed" ? ' stroke-dasharray="8 6"' : style.dash === "dotted" ? ' stroke-dasharray="2 5"' : "";
  return (
    `<svg xmlns="http://www.w3.org/2000/svg" viewBox="${viewBox}" width="${Math.max(1, element.w)}" height="${Math.max(1, element.h)}" preserveAspectRatio="xMidYMid meet">` +
    `<path d="${escapeXML(element.d)}" fill="${escapeXML(fill)}" stroke="${escapeXML(stroke)}" stroke-width="${Number(width) || 2}"${dash} ` +
    `stroke-linecap="round" stroke-linejoin="round" opacity="${(style.opacity ?? 100) / 100}"/></svg>`
  );
}

// -- rasterising (stills and exports) -----------------------------------------------------

export function loadImage(url) {
  return new Promise((resolve, reject) => {
    const image = new Image();
    image.onload = () => resolve(image);
    image.onerror = () => reject(new Error("image failed to load"));
    image.src = url;
  });
}

export async function rasterize(url, w, h, maxPx = 1024) {
  const image = await loadImage(url);
  const width = Math.max(1, Math.round(w || image.naturalWidth || 1));
  const height = Math.max(1, Math.round(h || image.naturalHeight || 1));
  const scale = Math.min(2, maxPx / Math.max(width, height));
  const canvas = document.createElement("canvas");
  canvas.width = Math.max(1, Math.round(width * scale));
  canvas.height = Math.max(1, Math.round(height * scale));
  const context = canvas.getContext("2d");
  context.fillStyle = "#ffffff";
  context.fillRect(0, 0, canvas.width, canvas.height);
  context.drawImage(image, 0, 0, canvas.width, canvas.height);
  return new Promise((resolve, reject) => canvas.toBlob((blob) => (blob ? resolve(blob) : reject(new Error("no PNG"))), "image/png"));
}

// -- one file per picture element ---------------------------------------------------------

export function fileIdFor(element, vizOn) {
  const seq = element.updated_seq ?? 0;
  switch (element.type) {
    case "image":
      return element.asset ? `asset:${element.asset}` : null;
    case "svg":
      return element.asset ? `${element.sketchy ? "sketchy" : "asset"}:${element.asset}:${Math.round(element.w)}x${Math.round(element.h)}` : null;
    case "path":
      return `path:${element.id}:${seq}`;
    case "mermaid":
      return `mermaid:${element.id}:${seq}`;
    case "chart":
      return `chart:${element.id}:${seq}`;
    case "viz":
      return vizOn ? null : `vizph:${element.id}:${seq}`;
    default:
      return null;
  }
}

// Resolves to {svg?, dataURL, mimeType}; `svg` is kept for stills of mermaid and chart.
export function renderFile(team, element, fileId) {
  return cached(`${team}|${fileId}`, async () => {
    const w = element.w || 160;
    const h = element.h || 120;
    try {
      switch (element.type) {
        case "image": {
          const blob = await getBlob(teamPath(team, `assets/${element.asset}`));
          return { dataURL: await blobToDataURL(blob), mimeType: element.mime || blob.type || "image/png" };
        }
        case "svg": {
          const raw = await getText(teamPath(team, `assets/${element.asset}`));
          let svg = raw;
          if (element.sketchy) {
            try {
              svg = await sketchySVG(raw);
            } catch {
              svg = raw;
            }
          }
          return { dataURL: svgToDataURL(fitSVG(svg, w, h)), mimeType: "image/svg+xml" };
        }
        case "path":
          return { dataURL: svgToDataURL(pathSVG(element)), mimeType: "image/svg+xml" };
        case "mermaid": {
          const svg = fitSVG(await renderMermaid(element.source), w, h);
          return { svg, dataURL: svgToDataURL(svg), mimeType: "image/svg+xml" };
        }
        case "chart": {
          const svg = fitSVG(await renderChart(team, element, w, h), w, h);
          return { svg, dataURL: svgToDataURL(svg), mimeType: "image/svg+xml" };
        }
        case "viz":
          return {
            dataURL: svgToDataURL(placeholderSVG(w, h, element.text || "live visual", "live visuals are off for this team")),
            mimeType: "image/svg+xml",
          };
        default:
          throw new Error(`nothing to render for ${element.type}`);
      }
    } catch (err) {
      const line = `${element.type} could not be drawn: ${String(err && err.message ? err.message : err).slice(0, 60)}`;
      return { dataURL: svgToDataURL(placeholderSVG(w, h, element.text || element.id, line, "#e03131")), mimeType: "image/svg+xml", failed: true };
    }
  });
}
