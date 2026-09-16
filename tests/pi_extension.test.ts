import { afterEach, beforeEach, expect, mock, spyOn, test } from "bun:test";
import { EventEmitter } from "node:events";

const reports: any[] = [];
const children: any[] = [];
mock.module("node:child_process", () => ({
  spawn: (_command: string, args: string[]) => {
    expect(args).toEqual(["pi-report"]);
    const child: any = new EventEmitter();
    child.stdin = new EventEmitter();
    child.stdin.end = (text: string) => reports.push(JSON.parse(text));
    child.kill = mock(() => child.emit("close"));
    children.push(child);
    return child;
  },
}));
(globalThis as any).__SYNAPSE_CLI__ = "/test/herdr-synapse";
const extension = (await import("../assets/pi-synapse.ts")).default;
let handlers: Record<string, Function>;
let heartbeat: Function;
let ctx: any;
let now: number;
let oldEnv: any;

beforeEach(() => {
  reports.length = children.length = 0;
  now = 100_000;
  spyOn(Date, "now").mockImplementation(() => now);
  spyOn(globalThis, "setInterval").mockImplementation(((fn: Function) => {
    heartbeat = fn;
    return { unref() {} };
  }) as any);
  spyOn(globalThis, "clearInterval").mockImplementation(() => {});
  oldEnv = { HERDR_ENV: process.env.HERDR_ENV, HERDR_PANE_ID: process.env.HERDR_PANE_ID };
  process.env.HERDR_ENV = "1";
  process.env.HERDR_PANE_ID = "w1:p1";
  handlers = {};
  ctx = { mode: "tui", model: { provider: "custom", id: "model-a", contextWindow: 123456 },
          thinkingLevel: "high", sessionManager: { getSessionFile: () => "/tmp/exact.jsonl" },
          getContextUsage: () => ({ tokens: 100, contextWindow: 123456 }) };
  extension({ on: (event: string, fn: Function) => { handlers[event] = fn; } });
});

afterEach(() => {
  handlers.session_shutdown?.();
  mock.restore();
  for (const [key, value] of Object.entries(oldEnv)) {
    if (value === undefined) delete process.env[key];
    else process.env[key] = value as string;
  }
});

test("RPC sessions never report or spawn a timer", () => {
  handlers.session_start({}, { ...ctx, mode: "rpc", hasUI: true });
  expect(reports).toHaveLength(0);
  expect(setInterval).not.toHaveBeenCalled();
});

test("ordinary Pi outside Herdr does not activate the extension", () => {
  delete process.env.HERDR_ENV;
  handlers.session_start({}, ctx);
  expect(reports).toHaveLength(0);
  expect(setInterval).not.toHaveBeenCalled();
});

test("reports contain runtime numbers, no conversation or credential fields", () => {
  handlers.session_start({}, ctx);
  expect(reports[0]).toMatchObject({ session: "/tmp/exact.jsonl", model: "model-a", used: 100, window: 123456 });
  expect(Object.keys(reports[0]).sort()).toEqual(["version", "mode", "instance", "session", "at", "provider", "model", "effort", "used", "window", "compact"].sort());
});

test("one child at a time, compaction survives heartbeat coalescing", () => {
  handlers.session_start({}, ctx);
  now += 3000;
  handlers.session_compact({ compactionEntry: { id: "compact-one" } }, ctx);
  heartbeat();
  expect(reports).toHaveLength(1);
  children[0].emit("close");
  ctx.getContextUsage = () => ({ tokens: null, contextWindow: 123456 });
  heartbeat();
  expect(reports[1].compact).toMatchObject({ id: "compact-one", status: "success" });
  expect(reports[1].used).toBeNull();
});

test("model selection and shutdown/reload use fresh runtime identity", () => {
  handlers.session_start({}, ctx);
  const first = reports[0].instance;
  children[0].emit("close");
  now += 3000;
  ctx.model = { provider: "other", id: "model-b", contextWindow: 987654 };
  ctx.getContextUsage = () => undefined;
  handlers.model_select({}, ctx);
  expect(reports[1]).toMatchObject({ model: "model-b", window: 987654, used: null });
  handlers.session_shutdown();
  heartbeat();
  expect(reports).toHaveLength(2);
  handlers.session_start({}, ctx);
  expect(reports[2].instance).not.toEqual(first);
  expect(reports[2].compact).toBeNull();
});

test("native compaction failure is retained and reported without error text", () => {
  handlers.session_start({}, ctx);
  children[0].emit("close");
  now += 3000;
  handlers.session_compact_failed({ error: "private provider error" }, ctx);
  expect(reports[1].compact.status).toBe("failed");
  expect(Object.keys(reports[1].compact).sort()).toEqual(["at", "id", "status"]);
});
