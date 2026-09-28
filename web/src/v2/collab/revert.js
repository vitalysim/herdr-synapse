// The revert menu's choices (canvas-v2-phase5.md 12.4). Its own module: only the panel (ui.jsx,
// loaded lazily) uses it.
import { CHECKPOINT_ID } from "./ops.js";

// The revert menu's "in the last 10 minutes".
export const RECENT_MS = 10 * 60 * 1000;

// The revert menu's `since` choices for one author (12.4), from the scene's batches and
// checkpoints: [{key, label, since}] where since is null for "everything". The recent choice is
// the version just before the author's first batch in the last 10 minutes (a batch's `at` is when
// it started); it is left out when the author has none then.
export function revertChoices(scene, author, now = Date.now()) {
  const out = [];
  const batches = Object.entries((scene && scene.batches) || {})
    .map(([id, b]) => ({ id, ...b }))
    .filter((b) => b.author === author && !b.undone && Number.isFinite(b.first_seq));
  const cutoff = now - RECENT_MS;
  const recent = batches.filter((b) => Number.isFinite(Date.parse(b.at || "")) && Date.parse(b.at) >= cutoff);
  if (recent.length) {
    const first = Math.min(...recent.map((b) => b.first_seq));
    out.push({ key: "recent", label: "in the last 10 minutes", since: Math.max(0, first - 1), count: recent.length });
  }
  const checkpoints = ((scene && scene.checkpoints) || []).filter((c) => c && CHECKPOINT_ID.test(String(c.id)) && Number.isInteger(c.version));
  for (const c of checkpoints.slice().sort((a, b) => b.version - a.version).slice(0, 5)) {
    const count = batches.filter((b) => b.first_seq > c.version).length;
    out.push({ key: `since:${c.id}`, label: `since checkpoint ${c.id}`, since: c.version, count, checkpoint: c.id });
  }
  out.push({ key: "all", label: "everything", since: null, count: batches.length });
  return out;
}

