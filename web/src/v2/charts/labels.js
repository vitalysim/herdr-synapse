// Label collisions in a drawn chart (canvas-v2-phase3-4.md 2.10 qa.js, K1 and K4): the text boxes
// ECharts laid out, read off its zrender scene right after the static render, as oriented
// rectangles (a 45° axis label is a rotated box, not its bounding box), and the pairs that
// intersect by more than a hair.

const EPS = 0.5; // px each box is shrunk by: labels that only touch do not count

function corners(rect, m) {
  const pts = [
    [rect.x + EPS, rect.y + EPS],
    [rect.x + rect.width - EPS, rect.y + EPS],
    [rect.x + rect.width - EPS, rect.y + rect.height - EPS],
    [rect.x + EPS, rect.y + rect.height - EPS],
  ];
  if (!m) return pts;
  return pts.map(([x, y]) => [m[0] * x + m[2] * y + m[4], m[1] * x + m[3] * y + m[5]]);
}

/**
 * [{text, pts}] for every visible text span in an ECharts instance (after a render), in scene
 * order. `pts` are the four corners in chart pixels.
 */
export function textBoxes(chart, limit = 400) {
  const zr = chart && typeof chart.getZr === "function" ? chart.getZr() : null;
  const list = zr && zr.storage && typeof zr.storage.getDisplayList === "function" ? zr.storage.getDisplayList(true) : [];
  const out = [];
  for (const el of list) {
    if (out.length >= limit) break;
    const style = el && el.style;
    if (!style || typeof style.text !== "string" || !style.text.trim() || el.invisible || el.ignore) continue;
    if (style.opacity === 0) continue;
    const rect = typeof el.getBoundingRect === "function" ? el.getBoundingRect() : null;
    if (!rect || !(rect.width > 2 * EPS) || !(rect.height > 2 * EPS)) continue;
    out.push({ text: style.text, pts: corners(rect, el.transform) });
  }
  return out;
}

function project(pts, ax, ay) {
  let min = Infinity;
  let max = -Infinity;
  for (const [x, y] of pts) {
    const d = x * ax + y * ay;
    if (d < min) min = d;
    if (d > max) max = d;
  }
  return [min, max];
}

/** True when two convex quads intersect (separating axis test over both quads' edges). */
export function quadsIntersect(a, b) {
  for (const poly of [a, b]) {
    for (let i = 0; i < poly.length; i += 1) {
      const [x1, y1] = poly[i];
      const [x2, y2] = poly[(i + 1) % poly.length];
      const ax = y1 - y2;
      const ay = x2 - x1;
      if (ax === 0 && ay === 0) continue;
      const [amin, amax] = project(a, ax, ay);
      const [bmin, bmax] = project(b, ax, ay);
      if (amax <= bmin || bmax <= amin) return false;
    }
  }
  return true;
}

/** The number of intersecting pairs among the boxes. */
export function countOverlaps(boxes) {
  let n = 0;
  for (let i = 0; i < boxes.length; i += 1) {
    for (let j = i + 1; j < boxes.length; j += 1) {
      if (quadsIntersect(boxes[i].pts, boxes[j].pts)) n += 1;
    }
  }
  return n;
}

/** Axis-aligned boxes: {x, y, w, h} as a quad. */
export function rectQuad(x, y, w, h) {
  return { pts: corners({ x, y, width: w, height: h }, null) };
}
