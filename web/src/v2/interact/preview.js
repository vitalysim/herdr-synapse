// The only local state the board draws: previews. A gesture's live preview, the previews of ops
// sent but not yet reflected in the display list, and the editor's. Surface takes one merged
// `preview` prop (canvas-v2-phase1.md 6.2) and keeps none of its own.

const EMPTY = null;

// Merge previews: the last `move` wins, boxes merge, hide and ghost concatenate.
export function combinePreviews(list) {
  let out = EMPTY;
  for (const p of list) {
    if (!p) continue;
    out = out ? { ...out } : {};
    if (p.move && p.move.ids && p.move.ids.length) out.move = p.move;
    if (p.boxes) out.boxes = { ...(out.boxes || {}), ...p.boxes };
    if (p.hide && p.hide.length) out.hide = [...(out.hide || []), ...p.hide];
    if (p.ghost && p.ghost.length) out.ghost = [...(out.ghost || []), ...p.ghost];
  }
  if (out && !out.move && !out.boxes && !out.hide && !out.ghost) return EMPTY;
  return out;
}

// Previews of ops in flight. Each clears when the display list reaches the op's result version,
// at once on a refusal or failure, and after `timeoutMs` whatever happens (a lost update must
// never leave a ghost on the board).
export function createPendingPreviews({ onChange = () => {}, timeoutMs = 6000, now = () => Date.now() } = {}) {
  let items = [];
  let seq = 0;
  const changed = () => onChange(items.map((item) => item.preview));
  return {
    add(preview) {
      if (!preview) return null;
      seq += 1;
      items = items.concat({ key: seq, preview, version: Infinity, at: now() });
      changed();
      return seq;
    },
    // The server answered: keep the preview until the display list holds `version`.
    settle(key, version, dlVersion = 0) {
      const item = items.find((it) => it.key === key);
      if (!item) return;
      if (!Number.isFinite(version) || version <= dlVersion) this.drop(key);
      else {
        items = items.map((it) => (it.key === key ? { ...it, version } : it));
        changed();
      }
    },
    drop(key) {
      const before = items.length;
      items = items.filter((it) => it.key !== key);
      if (items.length !== before) changed();
    },
    // The display list moved to `version`: clear what it now shows, and anything too old.
    reached(version) {
      const t = now();
      const before = items.length;
      items = items.filter((it) => it.version > version && t - it.at < timeoutMs);
      if (items.length !== before) changed();
    },
    list: () => items.map((item) => item.preview),
    clear() {
      if (items.length) {
        items = [];
        changed();
      }
    },
  };
}
