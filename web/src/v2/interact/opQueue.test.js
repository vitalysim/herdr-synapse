// The op queue (QA phase 1, V-3): one POST at a time, and a second drag released while the first
// POST is in flight chains on it instead of being refused as canvas_stale.
import { describe, expect, test } from "vitest";
import { createOpQueue, rebaseOp, targetsOf } from "./opQueue.js";

// A fake server with the real if_version rule (canvas._targets): every target must be at exactly
// if_version; an applied op moves its targets to the answer's version.
function fakeServer(start = { "E-6": 9, "E-7": 9 }) {
  const at = new Map(Object.entries(start));
  let version = Math.max(...at.values());
  const posts = [];
  const gates = [];
  return {
    at,
    posts,
    bump(id) {
      version += 1;
      at.set(id, version);
    },
    // Each post waits until release() lets it answer, so the test decides what is in flight.
    post(ops) {
      posts.push(ops);
      return new Promise((resolve) => {
        gates.push(() => {
          const applied = [];
          const refused = [];
          for (const op of ops) {
            const ids = targetsOf(op);
            const stale = Number.isFinite(op.if_version) && ids.find((id) => at.get(id) !== op.if_version);
            if (stale) {
              refused.push({ op: op.op, code: "canvas_stale", message: `${stale} changed since v${op.if_version}` });
              continue;
            }
            version += 1;
            for (const id of ids) at.set(id, version);
            applied.push({ op: op.op, ids });
          }
          resolve({ applied, refused, version });
        });
      });
    },
    async release() {
      await Promise.resolve();
      const next = gates.shift();
      if (next) next();
      for (let i = 0; i < 5; i += 1) await Promise.resolve();
    },
  };
}

const move = (id, dx, v) => ({ op: "move", ids: [id], by: [dx, 0], if_version: v });

describe("rebaseOp", () => {
  test("moves if_version up to what an op ahead left its targets at", () => {
    expect(rebaseOp(move("E-6", 40, 9), new Map([["E-6", 11]]))).toEqual(move("E-6", 40, 11));
    expect(rebaseOp({ op: "edit", id: "E-6", text: "x", if_version: 9 }, new Map([["E-6", 11]])).if_version).toBe(11);
  });
  test("leaves alone what it cannot rebase safely", () => {
    const op = move("E-6", 40, 9);
    expect(rebaseOp(op, new Map())).toBe(op);
    expect(rebaseOp(op, new Map([["E-7", 11]]))).toBe(op);
    expect(rebaseOp(op, new Map([["E-6", 8]]))).toBe(op); // never down
    const noVersion = { op: "move", ids: ["E-6"], by: [1, 0] };
    expect(rebaseOp(noVersion, new Map([["E-6", 11]]))).toBe(noVersion);
    const create = { op: "shape", kind: "box", at: [0, 0] };
    expect(rebaseOp(create, new Map([["E-6", 11]]))).toBe(create);
    // Two targets that would end at different versions: one number cannot say that.
    const both = { op: "move", ids: ["E-6", "E-7"], by: [1, 0], if_version: 9 };
    expect(rebaseOp(both, new Map([["E-6", 11]]))).toBe(both);
    expect(rebaseOp(both, new Map([["E-6", 11], ["E-7", 11]])).if_version).toBe(11);
  });
});

describe("createOpQueue", () => {
  test("a second drag released while the first POST is in flight is sent after it, rebased, and applied", async () => {
    const server = fakeServer();
    const queue = createOpQueue({ post: server.post });
    const first = queue.send([move("E-6", 40, 9)]);
    const second = queue.send([move("E-6", 40, 9)]); // built before the first answer: still v9
    await Promise.resolve();
    expect(server.posts.length).toBe(1); // one POST at a time
    expect(queue.depth()).toBe(2);
    await server.release();
    const a = await first;
    expect(a.applied.length).toBe(1);
    expect(server.posts.length).toBe(2);
    expect(server.posts[1][0].if_version).toBe(a.version);
    await server.release();
    const b = await second;
    expect(b.refused).toEqual([]);
    expect(b.applied.length).toBe(1);
    expect(queue.idle()).toBe(true);
  });

  test("three drags in a row chain, each on the one before", async () => {
    const server = fakeServer();
    const queue = createOpQueue({ post: server.post });
    const sends = [queue.send([move("E-6", 20, 9)]), queue.send([move("E-6", 20, 9)]), queue.send([move("E-6", 20, 9)])];
    for (let i = 0; i < 3; i += 1) await server.release();
    const results = await Promise.all(sends);
    expect(results.map((r) => r.refused.length)).toEqual([0, 0, 0]);
    expect(server.posts.map((p) => p[0].if_version)).toEqual([9, 10, 11]);
  });

  test("someone else's change in between is still refused as stale", async () => {
    const server = fakeServer();
    const queue = createOpQueue({ post: server.post });
    const first = queue.send([move("E-6", 40, 9)]);
    const second = queue.send([move("E-6", 40, 9)]);
    await server.release();
    await first;
    server.bump("E-6"); // another writer, after our first op and before our second arrives
    await server.release();
    const b = await second;
    expect(b.applied).toEqual([]);
    expect(b.refused[0].code).toBe("canvas_stale");
  });

  test("a refused first op rebases nothing", async () => {
    const server = fakeServer();
    server.bump("E-6"); // E-6 is at v10; the page still thinks v9
    const queue = createOpQueue({ post: server.post });
    const first = queue.send([move("E-6", 40, 9)]);
    const second = queue.send([move("E-6", 40, 9)]);
    await server.release();
    expect((await first).refused.length).toBe(1);
    expect(server.posts[1][0].if_version).toBe(9);
    await server.release();
    expect((await second).refused.length).toBe(1);
  });

  test("an op on another element is not rebased, and a send queued after an answer is not either", async () => {
    const server = fakeServer();
    const queue = createOpQueue({ post: server.post });
    const first = queue.send([move("E-6", 40, 9)]);
    const other = queue.send([move("E-7", 40, 9)]);
    await server.release();
    await first;
    await server.release();
    expect((await other).applied.length).toBe(1);
    expect(server.posts[1][0].if_version).toBe(9);
    // Queued after both answered: the page's own versions (ops.versionOf) already say v10, no rebase.
    const later = queue.send([move("E-6", 10, 10)]);
    await server.release();
    expect((await later).applied.length).toBe(1);
    expect(server.posts[2][0].if_version).toBe(10);
  });

  test("a failed POST rejects its own send and the queue carries on", async () => {
    let calls = 0;
    const queue = createOpQueue({
      post: async (ops) => {
        calls += 1;
        if (calls === 1) throw new Error("network");
        return { applied: [{ op: ops[0].op, ids: targetsOf(ops[0]) }], refused: [], version: 12 };
      },
    });
    const first = queue.send([move("E-6", 40, 9)]);
    const second = queue.send([move("E-6", 40, 9)]);
    await expect(first).rejects.toThrow("network");
    expect((await second).version).toBe(12);
    expect(calls).toBe(2);
    expect(queue.idle()).toBe(true);
  });
});
