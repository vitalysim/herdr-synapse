import { describe, expect, it } from "vitest";
import {
  ALWAYS_SHOW_ENGINE_TOGGLE,
  DEFAULT_ENGINE,
  DEV_KEY,
  ENGINE_KEY,
  LEGACY_ENGINE_KEY,
  engineOf,
  engineToggleVisible,
  setEngine,
  withEngine,
} from "./engine.js";

function storage(initial = {}) {
  const data = { ...initial };
  return {
    data,
    getItem: (k) => (k in data ? data[k] : null),
    setItem: (k, v) => {
      data[k] = String(v);
    },
    removeItem: (k) => {
      delete data[k];
    },
  };
}
const throwing = {
  getItem() {
    throw new Error("blocked");
  },
  setItem() {
    throw new Error("blocked");
  },
  removeItem() {
    throw new Error("blocked");
  },
};

describe("engine (canvas-v2-phase6.md 1.1)", () => {
  it("defaults to v2 and keeps its choice under a new key", () => {
    expect(DEFAULT_ENGINE).toBe("v2");
    expect(ENGINE_KEY).toBe("synapse-engine-v022");
    expect(LEGACY_ENGINE_KEY).toBe("synapse-engine");
    expect(engineOf({ search: "" }, storage())).toBe("v2");
    expect(engineOf({ search: "?x=1" }, storage())).toBe("v2");
    expect(engineOf(null, null)).toBe("v2");
  });
  it("prefers the query, then the stored choice, then v2", () => {
    expect(engineOf({ search: "?engine=v1" }, storage({ [ENGINE_KEY]: "v2" }))).toBe("v1");
    expect(engineOf({ search: "?engine=v2" }, storage({ [ENGINE_KEY]: "v1" }))).toBe("v2");
    expect(engineOf({ search: "" }, storage({ [ENGINE_KEY]: "v1" }))).toBe("v1"); // a choice made on 0.22 is kept
    expect(engineOf("?engine=v1", storage())).toBe("v1");
  });
  it("ignores the choice stored before 0.22 and removes it (D2)", () => {
    const s = storage({ [LEGACY_ENGINE_KEY]: "v1" });
    expect(engineOf({ search: "" }, s)).toBe("v2");
    expect(LEGACY_ENGINE_KEY in s.data).toBe(false);
    const both = storage({ [LEGACY_ENGINE_KEY]: "v2", [ENGINE_KEY]: "v1" });
    expect(engineOf({ search: "" }, both)).toBe("v1");
    expect(LEGACY_ENGINE_KEY in both.data).toBe(false);
  });
  it("ignores values it does not know", () => {
    expect(engineOf({ search: "?engine=v3" }, storage({ [ENGINE_KEY]: "excalidraw" }))).toBe("v2");
    expect(engineOf({ search: "?engine=<script>" }, storage({ [ENGINE_KEY]: "v1" }))).toBe("v1");
  });
  it("survives blocked storage", () => {
    expect(engineOf({ search: "" }, throwing)).toBe("v2");
    expect(engineOf({ search: "?engine=v1" }, throwing)).toBe("v1");
    expect(setEngine("v1", throwing)).toBe("v1");
  });
  it("stores the choice under the new key", () => {
    const s = storage();
    setEngine("v1", s);
    expect(s.data[ENGINE_KEY]).toBe("v1");
    expect(LEGACY_ENGINE_KEY in s.data).toBe(false);
    expect(() => setEngine("v9", s)).toThrow();
  });
  it("puts ?engine=v1 in the URL for v1 and drops the query for the default, leaving the hash alone", () => {
    expect(withEngine("http://127.0.0.1:9/#team=a&tab=canvas", "v1")).toBe("http://127.0.0.1:9/?engine=v1#team=a&tab=canvas");
    expect(withEngine("http://127.0.0.1:9/?engine=v1#team=a", "v2")).toBe("http://127.0.0.1:9/#team=a");
    expect(withEngine("http://127.0.0.1:9/?engine=v2#team=a", "v2")).toBe("http://127.0.0.1:9/#team=a");
    const there = withEngine("http://127.0.0.1:9/#team=a", "v1");
    expect(withEngine(there, "v2")).toBe("http://127.0.0.1:9/#team=a"); // a round trip
  });
  it("shows the engine chip to everyone while both engines ship (D3)", () => {
    expect(ALWAYS_SHOW_ENGINE_TOGGLE).toBe(true);
    expect(engineToggleVisible({ search: "" }, storage())).toBe(true);
    expect(engineToggleVisible(null, null)).toBe(true);
    expect(engineToggleVisible({ search: "" }, throwing)).toBe(true);
    expect(engineToggleVisible({ search: "" }, storage({ [DEV_KEY]: "0" }))).toBe(true);
  });
});
