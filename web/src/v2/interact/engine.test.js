import { describe, expect, it } from "vitest";
import { DEV_KEY, ENGINE_KEY, engineOf, engineToggleVisible, setEngine, withEngine } from "./engine.js";

function storage(initial = {}) {
  const data = { ...initial };
  return { data, getItem: (k) => (k in data ? data[k] : null), setItem: (k, v) => { data[k] = String(v); } };
}
const throwing = { getItem() { throw new Error("blocked"); }, setItem() { throw new Error("blocked"); } };

describe("engine", () => {
  it("prefers the query, then the stored choice, then v1", () => {
    expect(engineOf({ search: "?engine=v2" }, storage({ [ENGINE_KEY]: "v1" }))).toBe("v2");
    expect(engineOf({ search: "?engine=v1" }, storage({ [ENGINE_KEY]: "v2" }))).toBe("v1");
    expect(engineOf({ search: "" }, storage({ [ENGINE_KEY]: "v2" }))).toBe("v2");
    expect(engineOf({ search: "?x=1" }, storage())).toBe("v1");
    expect(engineOf(null, null)).toBe("v1");
  });
  it("ignores values it does not know", () => {
    expect(engineOf({ search: "?engine=v3" }, storage({ [ENGINE_KEY]: "excalidraw" }))).toBe("v1");
    expect(engineOf({ search: "?engine=<script>" }, storage({ [ENGINE_KEY]: "v2" }))).toBe("v2");
  });
  it("survives blocked storage", () => {
    expect(engineOf({ search: "" }, throwing)).toBe("v1");
    expect(setEngine("v2", throwing)).toBe("v2");
  });
  it("stores the choice", () => {
    const s = storage();
    setEngine("v2", s);
    expect(s.data[ENGINE_KEY]).toBe("v2");
    expect(() => setEngine("v9", s)).toThrow();
  });
  it("keeps the query in step and leaves the hash alone", () => {
    expect(withEngine("http://127.0.0.1:9/?engine=v2#team=a&tab=canvas", "v1")).toBe("http://127.0.0.1:9/#team=a&tab=canvas");
    expect(withEngine("http://127.0.0.1:9/#team=a", "v2")).toBe("http://127.0.0.1:9/?engine=v2#team=a");
  });
  it("shows the v2 toggle only to who asked for v2 or set the developer flag (carried item 16)", () => {
    expect(engineToggleVisible({ search: "" }, storage())).toBe(false);
    expect(engineToggleVisible({ search: "?engine=v1" }, storage())).toBe(false);
    expect(engineToggleVisible(null, null)).toBe(false);
    expect(engineToggleVisible({ search: "" }, throwing)).toBe(false);
    expect(engineToggleVisible({ search: "?engine=v2" }, storage())).toBe(true);
    expect(engineToggleVisible({ search: "" }, storage({ [ENGINE_KEY]: "v2" }))).toBe(true); // a way back
    expect(engineToggleVisible({ search: "" }, storage({ [DEV_KEY]: "1" }))).toBe(true);
    expect(engineToggleVisible({ search: "" }, storage({ [DEV_KEY]: "0" }))).toBe(false);
    expect(engineToggleVisible({ search: "" }, storage(), { dev: true })).toBe(true);
  });
});
