// Presence on the v2 board (canvas-v2-phase5.md 9 and 12.2): the poster that tells the server what
// the operator is looking at, and the parser for what the server says everyone is doing.
//
// Presence is a hint, never authority: a post that fails is dropped silently, and the server only
// ever uses the operator's entry to refuse an agent's edit to what she is typing into (D12).
//
// The poster's promises (I-12): at most 4 posts a second; the viewport posts on a trailing 250 ms
// debounce, the cursor at most every 250 ms, a selection or editor change at once (within the
// cap); a heartbeat every 10 s while the page is visible; `away: true` when it is hidden. A
// `presence_rate` answer pauses posting for its `retry_after`. Pure apart from the timers and
// `post`, which are injected.

export const MIN_GAP_MS = 250; // 4 posts a second
export const DEBOUNCE_MS = 250;
export const HEARTBEAT_MS = 10000;
export const FADE_SHARE = 0.2; // a halo fades over the last 20 % of its TTL
export const MAX_HALOS = 16;
export const MAX_INTENT = 60;
const STATUSES = ["reading", "drawing", "waiting", "blocked", "idle"];
const PAGE_RE = /^[0-9a-f]{16}$/;
const ID_RE = /^[A-Z]-[1-9][0-9]*$/;

// 16 hex characters from the browser's CSPRNG (a page id is not a secret, but it must not collide).
export function newPageId(cryptoImpl = typeof crypto !== "undefined" ? crypto : null) {
  const bytes = new Uint8Array(8);
  if (cryptoImpl && typeof cryptoImpl.getRandomValues === "function") cryptoImpl.getRandomValues(bytes);
  else for (let i = 0; i < bytes.length; i += 1) bytes[i] = Math.floor(Math.random() * 256);
  return [...bytes].map((b) => b.toString(16).padStart(2, "0")).join("");
}

const round = (v) => Math.round(v * 10) / 10;
const isRect = (r) => Array.isArray(r) && r.length === 4 && r.every(Number.isFinite) && r[0] <= r[2] && r[1] <= r[3];
const isPoint = (p) => Array.isArray(p) && p.length === 2 && p.every(Number.isFinite);

// The body a post carries: only well-formed keys, rounded to a tenth of a unit.
export function presenceBody(page, state) {
  const body = { page };
  if (isRect(state.viewport)) body.viewport = state.viewport.map(round);
  if (Array.isArray(state.selection)) body.selection = state.selection.filter((id) => ID_RE.test(String(id))).slice(0, 50);
  if (state.editing === null || (typeof state.editing === "string" && ID_RE.test(state.editing))) body.editing = state.editing;
  if (state.cursor === null) body.cursor = null;
  else if (isPoint(state.cursor)) body.cursor = state.cursor.map(round);
  body.away = Boolean(state.away);
  return body;
}

const sameValue = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/**
 * createPresencePoster({post, page, now, setTimer, clearTimer}) -> {update(patch, how), away(),
 * back(), heartbeat(), stop(), state()}
 *   post(body, {keepalive}) -> Promise: one POST /presence; a rejection with code presence_rate and
 *   details/body retry_after (seconds) pauses posting.
 *   update(patch, "debounce" | "throttle" | "now"): merge {viewport, selection, editing, cursor}.
 */
export function createPresencePoster({ post, page = newPageId(), now = () => Date.now(), setTimer = setTimeout, clearTimer = clearTimeout } = {}) {
  let state = { viewport: null, selection: [], editing: null, cursor: null, away: false };
  let lastSent = -Infinity;
  let sentBody = null;
  let timer = null;
  let dueAt = null;
  let pausedUntil = 0;
  let stopped = false;
  let inFlight = false;
  let again = false;

  function clear() {
    if (timer !== null) clearTimer(timer);
    timer = null;
    dueAt = null;
  }

  function schedule(at) {
    if (stopped) return;
    const when = Math.max(at, lastSent + MIN_GAP_MS, pausedUntil);
    if (timer !== null && dueAt !== null && dueAt <= when) return;
    clear();
    dueAt = when;
    timer = setTimer(flush, Math.max(0, when - now()));
  }

  async function flush({ force = false, keepalive = false } = {}) {
    clear();
    if (stopped && !keepalive) return;
    const t = now();
    if (!keepalive && t < pausedUntil) {
      schedule(pausedUntil);
      return;
    }
    if (!keepalive && t < lastSent + MIN_GAP_MS) {
      schedule(lastSent + MIN_GAP_MS);
      return;
    }
    const body = presenceBody(page, state);
    if (!force && sentBody && sameValue(body, sentBody)) return;
    if (inFlight && !keepalive) {
      again = true;
      return;
    }
    lastSent = t;
    sentBody = body;
    inFlight = true;
    try {
      await post(body, { keepalive });
    } catch (err) {
      const code = err && err.code;
      if (code === "presence_rate") {
        const details = (err.body && (err.body.details || err.body)) || {};
        const s = Number(details.retry_after ?? err.retry_after);
        pausedUntil = now() + (Number.isFinite(s) && s > 0 ? s * 1000 : 1000);
        sentBody = null; // what it carried did not land: send it again when the pause ends
        schedule(pausedUntil);
      }
      // Anything else: presence is a hint, and the next change or heartbeat tries again.
    } finally {
      inFlight = false;
    }
    if (again) {
      again = false;
      schedule(now());
    }
  }

  return {
    update(patch, how = "now") {
      if (stopped || !patch) return;
      const next = { ...state, ...patch, away: false };
      if (sameValue(next, state)) return;
      state = next;
      const t = now();
      if (how === "debounce") {
        // Trailing: every new value pushes the post back.
        clear();
        schedule(t + DEBOUNCE_MS);
      } else if (how === "throttle") {
        schedule(t);
      } else {
        schedule(t);
      }
    },
    heartbeat() {
      if (stopped || state.away) return;
      flush({ force: true });
    },
    // The page is hidden or going away: one post now, with keepalive, whatever the cap.
    away() {
      if (stopped) return;
      state = { ...state, away: true };
      clear();
      flush({ force: true, keepalive: true });
    },
    back() {
      if (stopped || !state.away) return;
      state = { ...state, away: false };
      schedule(now());
    },
    stop() {
      stopped = true;
      clear();
    },
    state: () => ({ ...state }),
    page: () => page,
  };
}

// -- reading -----------------------------------------------------------------------------------

function freshness(entry, now) {
  const at = Date.parse(entry.at || "");
  const ttl = Number(entry.ttl_s);
  if (!Number.isFinite(at) || !(ttl > 0)) return null;
  const left = at + ttl * 1000 - now;
  if (left <= 0) return null;
  const fadeMs = ttl * 1000 * FADE_SHARE;
  return { left, opacity: left >= fadeMs ? 1 : Math.max(0, left / fadeMs), at };
}

const text = (v, max) => {
  const line = String(v ?? "").replace(/[\u0000-\u001f\u007f]+/g, " ").trim();
  const chars = [...line];
  return chars.length > max ? `${chars.slice(0, max - 1).join("")}…` : line;
};

/**
 * parsePresence(doc, {now, page}) -> {members, operators, self}
 *   doc: the SSE `presence` data or GET /presence ({at, entries}).
 *   members: fresh agent entries [{name, status, region, ids, intent, opacity, at}], newest first,
 *   at most MAX_HALOS. operators: fresh operator entries from other pages [{page, viewport, cursor,
 *   opacity}]. self: this page's own entry, when the server has it (never drawn).
 * Anything malformed is dropped; strings are plain text (never HTML).
 */
export function parsePresence(doc, { now = Date.now(), page = null } = {}) {
  const out = { members: [], operators: [], self: null };
  const entries = doc && Array.isArray(doc.entries) ? doc.entries : [];
  for (const raw of entries) {
    if (!raw || typeof raw !== "object" || raw.away) continue;
    const fresh = freshness(raw, now);
    if (!fresh) continue;
    if (raw.kind === "human") {
      if (typeof raw.page !== "string" || !PAGE_RE.test(raw.page)) continue;
      const entry = { page: raw.page, viewport: isRect(raw.viewport) ? raw.viewport : null, cursor: isPoint(raw.cursor) ? raw.cursor : null, opacity: fresh.opacity, at: fresh.at };
      if (page && raw.page === page) out.self = entry;
      else out.operators.push(entry);
      continue;
    }
    if (raw.kind !== "member" || typeof raw.name !== "string" || !raw.name) continue;
    out.members.push({
      name: text(raw.name, 64),
      agent: typeof raw.agent === "string" ? text(raw.agent, 32) : null,
      status: STATUSES.includes(raw.status) ? raw.status : "idle",
      region: isRect(raw.region) ? raw.region : null,
      ids: Array.isArray(raw.ids) ? raw.ids.filter((id) => ID_RE.test(String(id))).slice(0, 50) : [],
      intent: text(raw.intent, 240),
      opacity: fresh.opacity,
      at: fresh.at,
      left: fresh.left,
    });
  }
  out.members.sort((a, b) => b.at - a.at || (a.name < b.name ? -1 : 1));
  out.members = out.members.slice(0, MAX_HALOS);
  return out;
}

// The soonest moment the parsed presence changes by itself (a fade step or an expiry), so the
// page re-reads it then: null when nothing is fresh.
export function nextTick(parsed, stepMs = 1000) {
  const any = parsed && (parsed.members.length || parsed.operators.length);
  return any ? stepMs : null;
}

export { text as clipText };
