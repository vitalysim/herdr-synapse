// The display-list client (canvas-v2-phase1.md 4.5, D4): GET /api/teams/<t>/display in full on
// load and after a reset, then GET ?since=<version held> whenever the scene store is ahead.
// At most one request is in flight; calls made meanwhile coalesce into one more. The page
// never edits the list; it only replaces what it holds with what the server answers.
//
// Injected: fetchJSON (api.js getJSON), applyDelta and isSupported (render/index.js), so this
// module is pure enough to test with a fake fetch.
//
// migration() is the canvas v2 migration notice the server sends a writable page (canvas-v2-phase6.md
// 2.4): a whole list carries it only while it is pending, a delta carries it or null once settled,
// and a delta without it leaves it as it was.

export function displayPath(team, since = 0) {
  const base = `/api/teams/${encodeURIComponent(team)}/display`;
  return since > 0 ? `${base}?since=${since}` : base;
}

export function createDisplayClient({ team, fetchJSON, applyDelta, isSupported = () => true, onError = () => {} }) {
  let dl = null;
  let inflight = null;
  let again = false;
  let wantFull = false;
  let closed = false;
  let migration = null;
  const listeners = new Set();
  const noteMigration = (doc) => {
    if ("migration" in doc) migration = doc.migration || null;
    else if (doc.full !== false) migration = null;
  };

  const emit = () => {
    for (const fn of listeners) fn(dl);
  };

  // A delta has no `entries` of its own, so only its version is checked; a whole list is checked whole.
  const supported = (doc) => (doc.full === false ? isSupported({ dl: doc.dl, entries: [] }) : isSupported(doc));

  function accept(doc, forced) {
    if (!doc || typeof doc !== "object") return false;
    if (!supported(doc)) {
      dl = doc;
      emit();
      return true;
    }
    if (doc.full === false) {
      if (!dl || doc.since !== dl.version) {
        // A delta against something this page does not hold: ask for the whole list.
        wantFull = true;
        return false;
      }
      dl = applyDelta(dl, doc);
    } else if (dl && doc.full === true && Array.isArray(doc.entries)) {
      dl = applyDelta(dl, doc);
    } else {
      // An older whole list than the one held (a slow answer) is dropped, unless asked for (a reset).
      if (!forced && dl && Number.isFinite(doc.version) && Number.isFinite(dl.version) && doc.version < dl.version) return false;
      dl = doc;
    }
    noteMigration(doc);
    emit();
    return true;
  }

  async function run() {
    do {
      again = false;
      const full = wantFull || !dl || !isSupported(dl);
      wantFull = false;
      const since = full ? 0 : dl.version;
      try {
        const doc = await fetchJSON(displayPath(team, since));
        if (closed) return;
        accept(doc, full);
      } catch (err) {
        if (closed) return;
        onError(err);
        return;
      }
    } while (again || wantFull);
  }

  function schedule() {
    if (closed) return Promise.resolve(dl);
    if (inflight) {
      again = true;
      return inflight;
    }
    inflight = run().finally(() => {
      inflight = null;
    });
    return inflight.then(() => dl);
  }

  return {
    current: () => dl,
    version: () => (dl && Number.isFinite(dl.version) ? dl.version : 0),
    migration: () => migration,
    // The whole list (mount, and after a `scene` event: a reset).
    load() {
      wantFull = true;
      return schedule();
    },
    // Catch up with a delta from what the page holds (the store moved past it).
    sync() {
      return schedule();
    },
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    close() {
      closed = true;
      listeners.clear();
    },
  };
}
