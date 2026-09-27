// The canonical scene (contract 5.7) as the page knows it: loaded from `scene` events or
// GET /scene, and kept current by folding `ops` events (contract 5.8). Excalidraw never
// owns this; the canvas adapter only draws it. Other tabs (Diagrams) read it too.

const EMPTY = Object.freeze({
  v: 1,
  team: null,
  version: 0,
  elements: [],
  claims: [],
  locks: [],
  legend: [],
  homes: {},
  authors: {},
  batches: {},
  counters: {},
});

function listPut(list, id, value) {
  const at = list.findIndex((item) => item && item.id === id);
  if (value === null || value === undefined) {
    if (at >= 0) list.splice(at, 1);
  } else if (at >= 0) {
    list[at] = value;
  } else {
    list.push(value);
  }
}

// Elements stay in z order: an update that changes `z` re-sorts, an add goes on top.
function sortByZ(elements) {
  elements.sort((a, b) => (a.z ?? 0) - (b.z ?? 0));
}

export function foldEvents(scene, events) {
  const next = {
    ...scene,
    elements: scene.elements.slice(),
    claims: scene.claims.slice(),
    locks: scene.locks.slice(),
    legend: scene.legend.slice(),
    homes: { ...scene.homes },
    authors: { ...scene.authors },
    batches: { ...scene.batches },
  };
  let resort = false;
  for (const event of events) {
    if (!event || typeof event !== "object") continue;
    for (const change of event.changes || []) {
      const { target, action, id } = change;
      const value = action === "delete" ? null : change.value;
      if (target === "element") {
        const before = next.elements.find((el) => el.id === id);
        listPut(next.elements, id, value);
        if (value && (!before || before.z !== value.z)) resort = true;
      } else if (target === "claim") {
        listPut(next.claims, id, value);
      } else if (target === "lock") {
        listPut(next.locks, id, value);
      } else if (target === "legend") {
        listPut(next.legend, id, value);
      } else if (target === "home") {
        if (value === null) delete next.homes[id];
        else next.homes[id] = value;
      } else if (target === "author") {
        if (value === null) delete next.authors[id];
        else next.authors[id] = value;
      }
    }
    if (event.batch) {
      const current = next.batches[event.batch];
      next.batches[event.batch] = {
        author: event.author?.name ?? current?.author ?? null,
        first_seq: current ? current.first_seq : event.seq,
        last_seq: event.seq,
        at: event.ts ?? current?.at ?? null,
        undone: current ? current.undone : false,
      };
    }
    if (Number.isFinite(event.seq)) next.version = Math.max(next.version, event.seq);
    next.updated_at = event.ts ?? next.updated_at;
  }
  if (resort) sortByZ(next.elements);
  return next;
}

// Claims expire lazily on the server; the page hides them at their expiry time.
export function activeClaims(scene, now = Date.now()) {
  return (scene.claims || []).filter((claim) => {
    const expires = Date.parse(claim.expires_at || "");
    return !Number.isFinite(expires) || expires > now;
  });
}

export function createSceneStore() {
  let scene = EMPTY;
  let events = [];
  const listeners = new Set();

  const emit = (detail) => {
    for (const fn of listeners) fn(scene, detail);
  };

  return {
    get() {
      return scene;
    },
    // Recent events (newest last), for the history panel's intents and summaries.
    recentEvents() {
      return events;
    },
    subscribe(fn) {
      listeners.add(fn);
      return () => listeners.delete(fn);
    },
    replace(next, detail = { kind: "scene" }) {
      scene = { ...EMPTY, ...next };
      emit(detail);
    },
    applyEvents(list, detail = {}) {
      const fresh = (list || []).filter((event) => Number(event?.seq) > scene.version || event?.op === "clear");
      if (!fresh.length) return;
      events = events.concat(fresh).slice(-400);
      scene = foldEvents(scene, fresh);
      emit({ kind: "ops", events: fresh, ...detail });
    },
    rememberEvents(list) {
      events = events.concat(list || []).slice(-400);
    },
    reset() {
      scene = EMPTY;
      events = [];
      emit({ kind: "reset" });
    },
  };
}
