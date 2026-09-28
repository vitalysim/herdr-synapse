// Presence on the page (canvas-v2-phase5.md 12.2, I-12): the poster's cap, debounce, throttle,
// heartbeat, away and rate back-off with fake timers; the page id; the parser's freshness, fade,
// cap, and that it never draws this page's own entry.
import fs from "node:fs";
import path from "node:path";
import { afterEach, beforeEach, describe, expect, test, vi } from "vitest";
import { DEBOUNCE_MS, FADE_SHARE, MAX_HALOS, MIN_GAP_MS, createPresencePoster, newPageId, parsePresence, presenceBody } from "./presence.js";

const FIXTURE = JSON.parse(fs.readFileSync(path.resolve(import.meta.dirname, "__fixtures__", "presence.json"), "utf8"));
// The server's own fixture (SERVER, tests/fixtures/collab/presence.json), when it exists.
const SHARED = path.resolve(import.meta.dirname, "../../../../tests/fixtures/collab/presence.json");

function rig() {
  const posts = [];
  let fail = null;
  const poster = createPresencePoster({
    page: "0123456789abcdef",
    now: () => Date.now(),
    post: async (body, opts) => {
      posts.push({ body, opts, at: Date.now() });
      if (fail) {
        const err = fail;
        fail = null;
        throw err;
      }
      return { ok: true, ttl_s: 30 };
    },
  });
  return { poster, posts, failNext: (err) => (fail = err) };
}

beforeEach(() => {
  vi.useFakeTimers();
  vi.setSystemTime(Date.parse("2026-09-28T10:00:00Z"));
});
afterEach(() => vi.useRealTimers());

describe("the page id", () => {
  test("16 hex characters from getRandomValues", () => {
    const fake = { getRandomValues: (a) => a.fill(0xab) };
    expect(newPageId(fake)).toBe("abababababababab");
    expect(newPageId()).toMatch(/^[0-9a-f]{16}$/);
    expect(newPageId()).not.toBe(newPageId());
  });
});

describe("the body", () => {
  test("only well-formed keys, rounded", () => {
    expect(presenceBody("0123456789abcdef", { viewport: [0.123, 1, 800.66, 600], selection: ["E-1", "P-3", "bad", 7], editing: "E-1", cursor: [10.04, 20.06], away: false })).toEqual({
      page: "0123456789abcdef",
      viewport: [0.1, 1, 800.7, 600],
      selection: ["E-1", "P-3"],
      editing: "E-1",
      cursor: [10, 20.1],
      away: false,
    });
    expect(presenceBody("0123456789abcdef", { viewport: [5, 5, 1, 1], selection: null, editing: "<b>", cursor: [NaN, 1] })).toEqual({ page: "0123456789abcdef", away: false });
  });
});

describe("the poster", () => {
  test("a selection posts at once; the viewport waits for a 250 ms lull", async () => {
    const { poster, posts } = rig();
    poster.update({ selection: ["E-1"] }, "now");
    await vi.advanceTimersByTimeAsync(0);
    expect(posts).toHaveLength(1);
    expect(posts[0].body.selection).toEqual(["E-1"]);
    for (let i = 0; i < 5; i += 1) {
      poster.update({ viewport: [i, 0, 800 + i, 600] }, "debounce");
      await vi.advanceTimersByTimeAsync(100);
    }
    expect(posts).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(DEBOUNCE_MS);
    expect(posts).toHaveLength(2);
    expect(posts[1].body.viewport).toEqual([4, 0, 804, 600]);
  });

  test("the cursor is throttled, and nothing ever posts more than 4 times a second", async () => {
    const { poster, posts } = rig();
    for (let i = 0; i < 100; i += 1) {
      poster.update({ cursor: [i, i] }, "throttle");
      if (i % 7 === 0) poster.update({ selection: [`E-${i + 1}`] }, "now");
      await vi.advanceTimersByTimeAsync(20);
    }
    await vi.advanceTimersByTimeAsync(1000);
    const times = posts.map((p) => p.at);
    for (let i = 1; i < times.length; i += 1) expect(times[i] - times[i - 1]).toBeGreaterThanOrEqual(MIN_GAP_MS);
    expect(posts.length).toBeLessThanOrEqual(Math.ceil((100 * 20 + 1000) / MIN_GAP_MS) + 1);
    // The last post carries the last values.
    expect(posts[posts.length - 1].body.cursor).toEqual([99, 99]);
  });

  test("nothing new, nothing posted; a heartbeat posts anyway", async () => {
    const { poster, posts } = rig();
    poster.update({ selection: ["E-1"] });
    await vi.advanceTimersByTimeAsync(300);
    poster.update({ selection: ["E-1"] });
    await vi.advanceTimersByTimeAsync(300);
    expect(posts).toHaveLength(1);
    poster.heartbeat();
    await vi.advanceTimersByTimeAsync(0);
    expect(posts).toHaveLength(2);
  });

  test("away posts at once with keepalive, whatever the cap; back resumes", async () => {
    const { poster, posts } = rig();
    poster.update({ selection: ["E-1"] });
    await vi.advanceTimersByTimeAsync(0);
    poster.away();
    await vi.advanceTimersByTimeAsync(0);
    expect(posts).toHaveLength(2);
    expect(posts[1].body.away).toBe(true);
    expect(posts[1].opts.keepalive).toBe(true);
    poster.heartbeat(); // hidden: no heartbeat
    await vi.advanceTimersByTimeAsync(1000);
    expect(posts).toHaveLength(2);
    poster.back();
    await vi.advanceTimersByTimeAsync(300);
    expect(posts).toHaveLength(3);
    expect(posts[2].body.away).toBe(false);
  });

  test("a presence_rate answer pauses posting for retry_after; other failures are silent", async () => {
    const { poster, posts, failNext } = rig();
    failNext(Object.assign(new Error("slow down"), { code: "presence_rate", body: { details: { retry_after: 2 } } }));
    poster.update({ selection: ["E-1"] });
    await vi.advanceTimersByTimeAsync(0);
    expect(posts).toHaveLength(1);
    poster.update({ selection: ["E-2"] });
    await vi.advanceTimersByTimeAsync(1500);
    expect(posts).toHaveLength(1);
    await vi.advanceTimersByTimeAsync(600);
    expect(posts).toHaveLength(2);
    expect(posts[1].body.selection).toEqual(["E-2"]);
    failNext(Object.assign(new Error("boom"), { code: "http_500" }));
    poster.update({ selection: ["E-3"] });
    await vi.advanceTimersByTimeAsync(300);
    expect(posts).toHaveLength(3);
  });

  test("stop ends every timer", async () => {
    const { poster, posts } = rig();
    poster.update({ viewport: [0, 0, 1, 1] }, "debounce");
    poster.stop();
    await vi.advanceTimersByTimeAsync(5000);
    expect(posts).toHaveLength(0);
  });
});

describe("parsing", () => {
  const now = Date.parse("2026-09-28T10:00:00Z");

  test("fresh members newest first; stale and away entries dropped; own page apart", () => {
    const parsed = parsePresence(FIXTURE, { now, page: "3f9a0c1b2d4e5f60" });
    expect(parsed.members.map((m) => [m.name, m.status])).toEqual([
      ["drawer", "drawing"],
      ["peer", "waiting"],
    ]);
    expect(parsed.self && parsed.self.page).toBe("3f9a0c1b2d4e5f60");
    expect(parsed.operators.map((o) => o.page)).toEqual(["00112233445566aa"]);
  });

  test("without a page id every operator page is someone else's", () => {
    expect(parsePresence(FIXTURE, { now }).operators).toHaveLength(2);
  });

  test("the fade: full until the last 20 % of the TTL, then linear", () => {
    const doc = { entries: [{ kind: "member", name: "a", at: "2026-09-28T10:00:00Z", ttl_s: 100, status: "drawing" }] };
    const at = (s) => parsePresence(doc, { now: now + s * 1000 }).members[0];
    expect(at(10).opacity).toBe(1);
    expect(at(100 * (1 - FADE_SHARE)).opacity).toBe(1);
    expect(at(90).opacity).toBeCloseTo(0.5, 5);
    expect(at(100)).toBeUndefined();
  });

  test("malformed entries are dropped, strings stay text, the count is capped", () => {
    const entries = [
      null,
      { kind: "member", name: "", at: "2026-09-28T10:00:00Z", ttl_s: 60 },
      { kind: "member", name: "x", at: "nope", ttl_s: 60 },
      { kind: "member", name: "x", at: "2026-09-28T10:00:00Z", ttl_s: -1 },
      { kind: "human", page: "../../etc", at: "2026-09-28T10:00:00Z", ttl_s: 30 },
    ];
    for (let i = 0; i < 30; i += 1) entries.push({ kind: "member", name: `m${i}`, at: new Date(now - i * 1000).toISOString(), ttl_s: 90, status: "haunting", intent: "<img src=x onerror=alert(1)>\n", ids: ["E-1", "javascript:1"] });
    const parsed = parsePresence({ entries }, { now });
    expect(parsed.members).toHaveLength(MAX_HALOS);
    expect(parsed.members[0].name).toBe("m0");
    expect(parsed.members[0].status).toBe("idle");
    expect(parsed.members[0].intent).toBe("<img src=x onerror=alert(1)>");
    expect(parsed.members[0].ids).toEqual(["E-1"]);
    expect(parsed.operators).toHaveLength(0);
  });

  test.runIf(fs.existsSync(SHARED))("the server's presence fixture (I-5) parses: its SSE data and its GET answer", () => {
    const doc = JSON.parse(fs.readFileSync(SHARED, "utf8"));
    for (const payload of [doc.sse_data, doc.get_answer].filter(Boolean)) {
      const parsed = parsePresence(payload, { now: Date.parse(payload.at) });
      const members = payload.entries.filter((e) => e.kind === "member" && !e.away);
      expect(parsed.members.map((m) => m.name).sort()).toEqual(members.map((m) => m.name).sort());
      for (const m of parsed.members) expect(["reading", "drawing", "waiting", "blocked", "idle"]).toContain(m.status);
      // The operator's page is someone else's page here (this page's id is not given).
      expect(parsed.operators.length).toBe(payload.entries.filter((e) => e.kind === "human" && !e.away).length);
    }
    expect(doc.statuses).toEqual(["reading", "drawing", "waiting", "blocked", "idle"]);
    // The page's promise (I-12) sits inside the server's cap.
    expect(1000 / MIN_GAP_MS).toBeLessThanOrEqual(doc.limits.posts_per_second);
    if (doc.post_body) expect(Object.keys(doc.post_body)).toContain("page");
  });
});
