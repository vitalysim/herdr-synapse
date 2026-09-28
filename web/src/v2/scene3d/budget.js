// The page's live WebGL budget (canvas-v2-phase3-4.md 3.12). Every live WebGL user takes a lease of
// its kind: the shared scene renderer ("shared", 1), echarts-gl charts ("echarts-gl", 2) and the
// sealed viz frames ("viz", 6), so the page never holds more than 9 contexts, well under the
// browser's own cap (Chrome drops the oldest context past 16).
//
// Past a kind's limit the least recently touched lease of that kind with an onDemote is demoted: its
// holder is told to let go (a viz frame shows its still, a live GL chart returns to static) and
// the lease is released. A request made with {queue: true} never demotes anyone: it waits its turn
// in a FIFO and resolves once a lease of its kind is released (a static GL chart render).

export const GL_LIMITS = Object.freeze({ shared: 1, "echarts-gl": 2, viz: 6 });

/**
 * A budget over `limits` ({kind: n}). `now` is the clock touches are ordered by (a counter by
 * default, so tests are deterministic).
 */
export function createBudget(limits = GL_LIMITS) {
  const live = new Map(); // kind -> Set<Lease>
  const queues = new Map(); // kind -> [{id, opts, resolve}]
  const demoted = new Map(); // kind -> count
  let clock = 0;

  const setOf = (kind) => {
    if (!live.has(kind)) live.set(kind, new Set());
    return live.get(kind);
  };
  const limitOf = (kind) => (Number.isFinite(limits[kind]) ? limits[kind] : 1);

  function grant(kind, id, { priority = 0, onDemote = null } = {}) {
    const lease = { kind, id, priority, onDemote, at: ++clock, released: false };
    setOf(kind).add(lease);
    return lease;
  }

  function drop(lease) {
    lease.released = true;
    const set = live.get(lease.kind);
    if (set) set.delete(lease);
  }

  function release(lease) {
    if (!lease || lease.released) return;
    drop(lease);
    const queue = queues.get(lease.kind);
    while (queue && queue.length && setOf(lease.kind).size < limitOf(lease.kind)) {
      const next = queue.shift();
      next.resolve(grant(lease.kind, next.id, next.opts));
    }
  }

  function demoteOne(kind, priority) {
    let victim = null;
    for (const lease of setOf(kind)) {
      if (typeof lease.onDemote !== "function" || lease.priority > priority) continue;
      if (!victim || lease.priority < victim.priority || (lease.priority === victim.priority && lease.at < victim.at)) victim = lease;
    }
    if (!victim) return false;
    const { onDemote } = victim;
    demoted.set(kind, (demoted.get(kind) || 0) + 1);
    // Dropped, not released: the freed place goes to the request that demoted it, not to the queue.
    drop(victim);
    try {
      onDemote();
    } catch {
      // a holder that fails to let go still lost its lease
    }
    return true;
  }

  return {
    limits,
    /**
     * A lease of `kind` for `id`. Under the limit: the lease. At it: with {queue: true}, a Promise of
     * the lease (FIFO); otherwise the least recently touched demotable lease of no higher priority is
     * demoted and the lease granted, or null when there is none.
     */
    request(kind, id, opts = {}) {
      const { queue = false, ...rest } = opts;
      if (setOf(kind).size < limitOf(kind)) return queue ? Promise.resolve(grant(kind, id, rest)) : grant(kind, id, rest);
      if (queue) {
        return new Promise((resolve) => {
          if (!queues.has(kind)) queues.set(kind, []);
          queues.get(kind).push({ id, opts: rest, resolve });
        });
      }
      if (!demoteOne(kind, Number(rest.priority) || 0)) return null;
      return grant(kind, id, rest);
    },
    release,
    /** Marks a lease as just used: selection, hover and entering touch. */
    touch(lease) {
      if (lease && !lease.released) lease.at = ++clock;
    },
    /** {kind: {live, queued, demoted}} */
    stats() {
      const out = {};
      for (const kind of new Set([...Object.keys(limits), ...live.keys(), ...queues.keys()])) {
        out[kind] = { live: setOf(kind).size, queued: (queues.get(kind) || []).length, demoted: demoted.get(kind) || 0 };
      }
      return out;
    },
    /** How many leases are live in total (the page's WebGL contexts in use). */
    total() {
      let n = 0;
      for (const set of live.values()) n += set.size;
      return n;
    },
  };
}

/** The page's one budget. */
export const glBudget = createBudget(GL_LIMITS);
