// The entry index and culling (canvas-v2-phase1.md 4.2, D3). A board holds at most about 2,000
// entries, so culling is a linear scan over a packed array of boxes: microseconds per query. If
// boards ever grow past that, an R-tree can replace the scan behind these same two functions.

function rectOf(entry) {
  const b = entry?.bbox;
  if (Array.isArray(b) && b.length === 4 && b.every(Number.isFinite)) return b;
  const box = entry?.hit?.box;
  if (Array.isArray(box) && box.length === 4 && box.every(Number.isFinite)) return [box[0], box[1], box[0] + box[2], box[1] + box[3]];
  return null;
}

/** The index of one display list: entries in render order, by id, and their bounds packed. */
export function createIndex(dl) {
  const entries = Array.isArray(dl?.entries) ? dl.entries.filter((e) => e && typeof e === "object") : [];
  const byId = new Map();
  const boxes = new Float64Array(entries.length * 4);
  const known = new Uint8Array(entries.length);
  entries.forEach((e, i) => {
    byId.set(e.id, e);
    const r = rectOf(e);
    if (r) {
      boxes.set(r, i * 4);
      known[i] = 1;
    }
  });
  return { dl, entries, byId, boxes, known };
}

export function entryOf(index, id) {
  return index?.byId.get(id) || null;
}

/**
 * The entries whose bounds meet world `rect` [x0, y0, x1, y1], in render order, plus every entry
 * named in `always` (selected or previewed). An entry without bounds is always kept.
 */
export function cull(index, rect, always = null) {
  const out = [];
  if (!index) return out;
  const { entries, boxes, known } = index;
  const [x0, y0, x1, y1] = rect;
  for (let i = 0; i < entries.length; i += 1) {
    const o = i * 4;
    if (!known[i] || (boxes[o] <= x1 && boxes[o + 2] >= x0 && boxes[o + 1] <= y1 && boxes[o + 3] >= y0) || (always && always.has(entries[i].id))) {
      out.push(entries[i]);
    }
  }
  return out;
}

// `rect` grown by `margin` of its own size on every side (one screen of margin is 1).
export function grow(rect, margin = 1) {
  const w = (rect[2] - rect[0]) * margin;
  const h = (rect[3] - rect[1]) * margin;
  return [rect[0] - w, rect[1] - h, rect[2] + w, rect[3] + h];
}

export function contains(outer, inner) {
  return !!outer && inner[0] >= outer[0] && inner[1] >= outer[1] && inner[2] <= outer[2] && inner[3] <= outer[3];
}
