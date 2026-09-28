// One POST /ops at a time (QA phase 1, V-3). A gesture that ends while an earlier POST is still in
// flight built its if_version from what the page knew then: the version before the earlier op
// landed. Sent as it was, the server refuses it as canvas_stale, although the only change it missed
// is this page's own. So every batch waits behind the one ahead of it, and when its turn comes the
// if_version of each op whose targets an op ahead of it changed is moved up to the version that
// op left them at. A change by anyone else is still refused: the server then holds a newer version
// than the one the page's own op reported. Pure apart from the promise chain; `post` does the I/O.
import { appliedVersions } from "./ops.js";

// The ids an op names (`id` or `ids`); if_version is checked against every one of them.
export function targetsOf(op) {
  if (!op || typeof op !== "object") return [];
  if (Array.isArray(op.ids)) return op.ids.filter((id) => typeof id === "string");
  return typeof op.id === "string" ? [op.id] : [];
}

// `op` with its if_version moved up to what `changed` ({id: version}, from ops sent ahead of it)
// says its targets are now at. It is left alone when it has no if_version, when none of its targets
// changed, or when its targets would end up at different versions (some changed, some not: one
// number cannot say that, and the server then refuses it, as it would have anyway).
export function rebaseOp(op, changed) {
  if (!op || !Number.isFinite(op.if_version) || !changed) return op;
  const ids = targetsOf(op);
  if (!ids.length) return op;
  let next = null;
  let moved = false;
  for (const id of ids) {
    const v = changed.get(id);
    const at = Number.isFinite(v) && v > op.if_version ? v : op.if_version;
    if (at !== op.if_version) moved = true;
    if (next !== null && at !== next) return op;
    next = at;
  }
  return moved ? { ...op, if_version: next } : op;
}

export function rebaseOps(list, changed) {
  return (list || []).map((op) => rebaseOp(op, changed));
}

// A batch's `base` (canvas-v2-phase5.md 3.1: the canvas version its author last read) rides along
// unchanged. It is not moved up over the sends ahead: the author's own changes never make an op
// stale, and the versions those sends report may include someone else's change the author has not
// seen, which is exactly what `base` is there to say.
export function baseOf(value) {
  return Number.isInteger(value) && value >= 0 ? value : null;
}

// createOpQueue({ post }) -> { send(ops, {base}) -> Promise<result>, idle() -> boolean, depth() -> number }
//   post(ops, {base}) -> Promise<result>: one POST /ops; its result is an /ops answer ({applied,
//   version, ...}). `base` is null when the send had none (or one that is not a version).
// send runs `post` after every earlier send has settled, with its ops rebased over the versions the
// sends that were waiting or in flight when it was queued left their targets at. A failed post
// rejects its own send only; the queue carries on.
export function createOpQueue({ post }) {
  let tail = Promise.resolve();
  let depth = 0;
  // The versions each queued send has seen changed since it was queued (one Map per waiting send).
  const waiting = new Set();

  function record(result) {
    const versions = appliedVersions(result);
    const ids = Object.keys(versions);
    if (!ids.length) return;
    for (const changed of waiting) {
      for (const id of ids) if (!(changed.get(id) > versions[id])) changed.set(id, versions[id]);
    }
  }

  function send(list, { base = null } = {}) {
    const at = baseOf(base);
    const changed = new Map();
    waiting.add(changed);
    depth += 1;
    const run = tail.then(async () => {
      waiting.delete(changed);
      const result = await post(rebaseOps(list, changed), { base: at });
      record(result);
      return result;
    });
    const settle = () => {
      waiting.delete(changed);
      depth -= 1;
    };
    // The next send waits for this one whatever its outcome.
    tail = run.then(settle, settle);
    return run;
  }

  return { send, idle: () => depth === 0, depth: () => depth };
}
