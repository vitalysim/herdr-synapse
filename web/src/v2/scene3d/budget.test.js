// glBudget (canvas-v2-phase3-4.md 3.12, 8.3): limits, LRU demotion, priorities and the FIFO queue.
import { describe, expect, test, vi } from "vitest";
import { GL_LIMITS, createBudget, glBudget } from "./budget.js";

describe("glBudget", () => {
  test("the page's limits: one shared renderer, 2 GL charts, 6 viz frames (9 contexts at most)", () => {
    expect(GL_LIMITS).toEqual({ shared: 1, "echarts-gl": 2, viz: 6 });
    expect(Object.values(GL_LIMITS).reduce((a, b) => a + b, 0)).toBeLessThanOrEqual(9);
    expect(glBudget.limits).toBe(GL_LIMITS);
  });

  test("past a kind's limit the least recently touched lease is demoted", () => {
    const b = createBudget({ viz: 2 });
    const demote = { a: vi.fn(), b: vi.fn(), c: vi.fn() };
    const a = b.request("viz", "a", { onDemote: demote.a });
    const bb = b.request("viz", "b", { onDemote: demote.b });
    b.touch(a); // b is now the least recently touched
    const c = b.request("viz", "c", { onDemote: demote.c });
    expect(c).toBeTruthy();
    expect(demote.b).toHaveBeenCalledTimes(1);
    expect(demote.a).not.toHaveBeenCalled();
    expect(bb.released).toBe(true);
    expect(b.stats().viz).toEqual({ live: 2, queued: 0, demoted: 1 });
    // A released lease frees its place without demoting anyone.
    b.release(a);
    expect(b.request("viz", "d", { onDemote: vi.fn() })).toBeTruthy();
    expect(demote.c).not.toHaveBeenCalled();
    expect(b.total()).toBe(2);
  });

  test("kinds are counted apart, and a lease with no onDemote or a higher priority is never demoted", () => {
    const b = createBudget({ shared: 1, viz: 1 });
    expect(b.request("shared", "renderer")).toBeTruthy();
    expect(b.request("shared", "again")).toBeNull();
    expect(b.request("viz", "v1", { onDemote: vi.fn(), priority: 2 })).toBeTruthy();
    expect(b.request("viz", "v2", { onDemote: vi.fn(), priority: 1 })).toBeNull();
    expect(b.request("viz", "v3", { onDemote: vi.fn(), priority: 2 })).toBeTruthy();
    expect(b.stats()).toMatchObject({ shared: { live: 1 }, viz: { live: 1, demoted: 1 } });
  });

  test("a queued request waits in FIFO order and never demotes", async () => {
    const b = createBudget({ "echarts-gl": 1 });
    const onDemote = vi.fn();
    const first = await b.request("echarts-gl", "live", { onDemote, queue: true });
    const second = b.request("echarts-gl", "s1", { queue: true });
    const third = b.request("echarts-gl", "s2", { queue: true });
    expect(b.stats()["echarts-gl"]).toEqual({ live: 1, queued: 2, demoted: 0 });
    const order = [];
    second.then((l) => order.push(l.id));
    third.then((l) => order.push(l.id));
    b.release(first);
    await Promise.resolve();
    expect(order).toEqual(["s1"]);
    b.release(await second);
    await third;
    expect(order).toEqual(["s1", "s2"]);
    expect(onDemote).not.toHaveBeenCalled();
  });

  test("a demotion hands the freed place to the demoting request, not to the queue", async () => {
    const b = createBudget({ "echarts-gl": 1 });
    b.request("echarts-gl", "live", { onDemote: () => {} });
    const waiting = b.request("echarts-gl", "static", { queue: true });
    const entered = b.request("echarts-gl", "entered", { onDemote: () => {} });
    expect(entered).toBeTruthy();
    expect(b.stats()["echarts-gl"]).toEqual({ live: 1, queued: 1, demoted: 1 });
    b.release(entered);
    expect((await waiting).id).toBe("static");
  });

  test("releasing twice, or a null lease, is harmless", () => {
    const b = createBudget({ viz: 1 });
    const lease = b.request("viz", "a");
    b.release(lease);
    b.release(lease);
    b.release(null);
    expect(b.stats().viz.live).toBe(0);
  });
});
