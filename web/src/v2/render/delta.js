// Display list documents and deltas (canvas-v2-phase1.md 1.1 and 3.2).
import { DL_SUPPORTED } from "./version.js";

export const LAYERS = ["zones", "marks", "labels", "overlays"];

export function isSupported(dl) {
  return !!dl && typeof dl === "object" && Number.isInteger(dl.dl) && dl.dl >= DL_SUPPORTED[0] && dl.dl <= DL_SUPPORTED[1] && Array.isArray(dl.entries);
}

function idNumber(id) {
  const m = /-(\d+)$/.exec(String(id));
  return m ? Number(m[1]) : Number.MAX_SAFE_INTEGER;
}

// Render order: layer, then z, then the id's number (then the id itself, so ties never flip).
export function compareEntries(layers = LAYERS) {
  const rank = (layer) => {
    const at = layers.indexOf(layer);
    return at < 0 ? layers.indexOf("marks") : at;
  };
  return (a, b) =>
    rank(a.layer) - rank(b.layer) ||
    (Number(a.z) || 0) - (Number(b.z) || 0) ||
    idNumber(a.id) - idNumber(b.id) ||
    (String(a.id) < String(b.id) ? -1 : String(a.id) > String(b.id) ? 1 : 0);
}

function same(a, b) {
  if (a === b) return true;
  if (!a || !b || a.id !== b.id || a.v !== b.v) return false;
  return JSON.stringify(a) === JSON.stringify(b);
}

/**
 * The display list after a delta: a new object, with every entry the delta did not change kept by
 * identity (so memoised entries do not redraw). A full document replaces the list, still reusing
 * the old object of any entry that is equal.
 */
export function applyDelta(dl, delta) {
  if (!delta || typeof delta !== "object") return dl;
  if (delta.full || !dl) {
    const old = new Map((dl?.entries || []).map((e) => [e.id, e]));
    const entries = (delta.entries || []).map((e) => (same(old.get(e.id), e) ? old.get(e.id) : e));
    const next = { ...(dl || {}), ...delta, entries };
    delete next.full;
    delete next.since;
    delete next.upserts;
    delete next.removes;
    return next;
  }
  const removes = new Set(delta.removes || []);
  const upserts = new Map((delta.upserts || []).map((e) => [e.id, e]));
  const current = dl.entries || [];
  const byId = new Map(current.map((e) => [e.id, e]));
  // In place when every upsert replaces an entry at the same layer and z: the server's order holds.
  let inPlace = true;
  for (const [id, e] of upserts) {
    const was = byId.get(id);
    if (!was || was.layer !== e.layer || (Number(was.z) || 0) !== (Number(e.z) || 0)) {
      inPlace = false;
      break;
    }
  }
  let entries;
  if (inPlace) {
    entries = [];
    for (const e of current) {
      if (removes.has(e.id)) continue;
      const up = upserts.get(e.id);
      entries.push(up && !same(e, up) ? up : e);
    }
  } else {
    entries = current.filter((e) => !removes.has(e.id) && !upserts.has(e.id));
    for (const [id, e] of upserts) {
      if (!removes.has(id)) entries.push(same(byId.get(id), e) ? byId.get(id) : e);
    }
    entries.sort(compareEntries(dl.layers || LAYERS));
  }
  return { ...dl, version: delta.version ?? dl.version, bbox: delta.bbox || dl.bbox, entries };
}
