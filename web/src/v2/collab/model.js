// Pure readers over the display list and the change stream for the collaboration layer
// (canvas-v2-phase5.md 12): the proposal and freeze entries, the review queue, the freeze rim a
// click can open, the region-freeze gesture, and the Revert notices for an agent's live change to
// the operator's marks. No DOM, no network.
import { ghostRect, dragged } from "../interact/tools/common.js";
import { buildFreezeRegion } from "./ops.js";

export const isProposalId = (id) => typeof id === "string" && /^P-[1-9][0-9]*$/.test(id);
const num = (id) => Number(String(id).replace(/^\D+-/, "")) || 0;

export function isProposalEntry(entry) {
  return Boolean(entry && isProposalId(entry.id) && (entry.kind === "proposal" || entry.proposal));
}

export function isFreezeEntry(entry) {
  return Boolean(entry && typeof entry.id === "string" && /^X-[1-9][0-9]*$/.test(entry.id) && entry.kind === "freeze");
}

// Ids the page must never send as element targets (select-all, marquee, delete, nudge, edit).
export function isOverlayId(id) {
  return typeof id === "string" && /^(P|X|K)-[1-9][0-9]*$/.test(id);
}

export function withoutOverlays(ids) {
  return (ids || []).filter((id) => !isOverlayId(id));
}

// id -> the entry's `proposal` object (with the entry's bbox), for the open proposals in the list.
export function proposalsOf(dl) {
  const out = new Map();
  for (const e of (dl && dl.entries) || []) {
    if (isProposalEntry(e)) out.set(e.id, { ...(e.proposal || {}), author: (e.proposal && e.proposal.author) || e.author, bbox: e.bbox, v: e.v });
  }
  return out;
}

export function freezesOf(dl) {
  return ((dl && dl.entries) || []).filter(isFreezeEntry);
}

// The review queue: open proposals, oldest first (by the version they were made at, then id).
export function reviewQueue(dl, scene = null) {
  const seqs = new Map(((scene && scene.proposals) || []).map((p) => [p.id, Number(p.seq) || 0]));
  return [...proposalsOf(dl).entries()]
    .map(([id, p]) => ({ id, seq: seqs.get(id) || Number(p.v) || num(id) }))
    .sort((a, b) => a.seq - b.seq || num(a.id) - num(b.id))
    .map((row) => row.id);
}

// What reviewing a proposal should show: its ghost and what it changes as it stands now (a move's
// before and after), as one world box.
export function reviewBox(dl, id) {
  const entries = (dl && dl.entries) || [];
  const p = entries.find((e) => e.id === id);
  if (!p || !Array.isArray(p.bbox)) return null;
  const targets = new Set(((p.proposal && p.proposal.targets) || []).concat((p.proposal && p.proposal.deleted) || []));
  const boxes = [p.bbox, ...entries.filter((e) => targets.has(e.id) && Array.isArray(e.bbox)).map((e) => e.bbox)];
  return [Math.min(...boxes.map((b) => b[0])), Math.min(...boxes.map((b) => b[1])), Math.max(...boxes.map((b) => b[2])), Math.max(...boxes.map((b) => b[3]))];
}

// A world box [x0, y0, x1, y1] on screen, as {x, y, w, h}.
export function screenBox(bbox, camera) {
  if (!Array.isArray(bbox) || bbox.length !== 4 || !camera) return null;
  const s = camera.scale;
  return { x: (bbox[0] - camera.x) * s, y: (bbox[1] - camera.y) * s, w: (bbox[2] - bbox[0]) * s, h: (bbox[3] - bbox[1]) * s };
}

// The camera that shows world box `bbox` in `viewport`, zoomed in no further than `maxScale`.
export function cameraFor(bbox, viewport, { pad = 120, maxScale = 1.25 } = {}) {
  if (!Array.isArray(bbox) || !viewport || !(viewport.w > 0)) return null;
  const w = Math.max(1, bbox[2] - bbox[0]) + 2 * pad;
  const h = Math.max(1, bbox[3] - bbox[1]) + 2 * pad;
  const scale = Math.max(0.05, Math.min(maxScale, viewport.w / w, viewport.h / h));
  const cx = (bbox[0] + bbox[2]) / 2;
  const cy = (bbox[1] + bbox[3]) / 2;
  return { x: cx - viewport.w / (2 * scale), y: cy - viewport.h / (2 * scale), scale };
}

// The freeze whose rim (within `rimPx` screen px of its outline) or label (the band above or
// below its top-right corner, where the server draws it) is under world point `pt`, or null. Freeze overlays take no hit of
// their own (they must not swallow clicks on what they cover), so this is how a click opens one.
// With `rim: false` only the label counts (a press on an element: an id-freeze's outline hugs its
// elements, and their edges and handles must stay theirs).
export function freezeAt(dl, pt, scale, { rimPx = 6, labelPx = [180, 20], rim = true } = {}) {
  if (!Array.isArray(pt) || !(scale > 0)) return null;
  const tol = rimPx / scale;
  const list = freezesOf(dl);
  for (let i = list.length - 1; i >= 0; i -= 1) {
    const e = list[i];
    const b = e.bbox;
    if (!Array.isArray(b) || b.length !== 4) continue;
    const inOuter = pt[0] >= b[0] - tol && pt[0] <= b[2] + tol && pt[1] >= b[1] - tol && pt[1] <= b[3] + tol;
    const inInner = pt[0] > b[0] + tol && pt[0] < b[2] - tol && pt[1] > b[1] + tol && pt[1] < b[3] - tol;
    if (rim && inOuter && !inInner) return e.id;
    const lw = labelPx[0] / scale;
    const lh = labelPx[1] / scale;
    if (pt[0] >= b[2] - lw && pt[0] <= b[2] && ((pt[1] >= b[1] - lh && pt[1] <= b[1]) || (pt[1] >= b[1] && pt[1] <= b[1] + lh))) return e.id;
  }
  return null;
}

// The ids of the collaboration overlays that take a hit of their own (proposal ghosts, freeze
// outlines): they open cards, and are never an element a gesture moves or resizes.
export function collabOverlayIds(dl) {
  return ((dl && dl.entries) || []).filter((e) => isProposalEntry(e) || isFreezeEntry(e)).map((e) => e.id);
}

/**
 * What a press lands on once the collaboration overlays are set aside: `ev` itself when its hit is
 * not one of them, else `ev` with `hit` set to what lies beneath (an element, or null).
 *   hitBelow(ev, skip) -> id | null: the renderer's hitTest over the list, skipping `skip`.
 */
export function beneath(ev, dl, hitBelow) {
  if (!ev || !ev.hit) return ev;
  const skip = collabOverlayIds(dl);
  if (!skip.includes(ev.hit)) return ev;
  return { ...ev, hit: hitBelow(ev, skip) || null };
}

// The freeze tool's drag: a rubber band; its rect becomes freeze {region}. A click without a drag
// does nothing (and says how to use the tool).
export function beginFreezeRegion(event) {
  const start = event.world;
  const startScreen = event.screen;
  let rect = null;
  return {
    update(ev) {
      if (!rect && !dragged(startScreen, ev)) return null;
      rect = [start[0], start[1], ev.world[0], ev.world[1]];
      return { ghost: ghostRect(rect) };
    },
    finish(ev) {
      if (!rect) return { toast: "drag over the area to freeze, or select marks and press Freeze" };
      const op = buildFreezeRegion([start[0], start[1], ev.world[0], ev.world[1]]);
      return op ? { ops: [op] } : null;
    },
    cancel() {},
  };
}

function labelOf(el) {
  if (!el) return null;
  const text = typeof el.text === "string" && el.text.trim() ? el.text : typeof el.label === "string" && el.label.trim() ? el.label : typeof el.title === "string" ? el.title : "";
  const line = text.replace(/\s+/g, " ").trim();
  return line ? ([...line].length > 24 ? `${[...line].slice(0, 23).join("")}…` : line) : null;
}

/**
 * The Revert notices a batch of `ops` events earns (12.5): one per batch whose events carry
 * `touched_human` and whose author is a member. -> [{batch, author, ids, text}]
 *   elementOf(id) -> the element (for its label); seen: a Set of batches already noticed (updated).
 */
export function touchedNotices(events, { elementOf = () => null, seen = new Set() } = {}) {
  const byBatch = new Map();
  for (const event of events || []) {
    if (!event || !Array.isArray(event.touched_human) || !event.touched_human.length) continue;
    const author = event.author && typeof event.author === "object" ? event.author : { name: event.author };
    if (!author || !author.name || author.name === "human" || author.kind === "human") continue;
    const batch = typeof event.batch === "string" ? event.batch : null;
    if (!batch || seen.has(batch)) continue;
    const row = byBatch.get(batch) || { batch, author: author.name, ids: [] };
    for (const id of event.touched_human) if (typeof id === "string" && !row.ids.includes(id)) row.ids.push(id);
    byBatch.set(batch, row);
  }
  const out = [];
  for (const row of byBatch.values()) {
    seen.add(row.batch);
    const first = labelOf(elementOf(row.ids[0]));
    const what = row.ids.length === 1 ? (first ? `your '${first}'` : `your ${row.ids[0]}`) : `${row.ids.length} of your marks (${row.ids.slice(0, 3).join(", ")}${row.ids.length > 3 ? ", …" : ""})`;
    out.push({ ...row, text: `${row.author} changed ${what}` });
  }
  return out;
}

// What a /ops answer says for the collaboration layer, as toast lines (12.5): proposals made,
// undo skips, restore counts. The refusals and warnings are Board's (refusalText, info toasts).
export function resultLines(result, { undoSummary, restoreSummary }) {
  const lines = [];
  for (const p of (result && result.proposed) || []) lines.push({ text: `#${(p.index ?? 0) + 1} ${p.op} became proposal ${p.proposal}: it waits for review`, tone: "info" });
  for (const a of (result && result.applied) || []) {
    if (a && a.undo) {
      const skipped = Array.isArray(a.undo.skipped) && a.undo.skipped.length;
      lines.push({ text: undoSummary(a.undo), tone: skipped ? "warn" : "ok" });
    }
    if (a && a.restore) lines.push({ text: restoreSummary(a.restore), tone: "ok" });
  }
  return lines;
}
