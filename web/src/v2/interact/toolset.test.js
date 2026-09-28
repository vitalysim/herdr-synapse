// The board's tools come from the kind registry (QA phase 1, V-2): conformance tests that hold for
// any registered kind, plus a kind this test makes up to show a new one needs no page edit.
import fs from "node:fs";
import path from "node:path";
import tokens from "@synapse/tokens";
import { describe, expect, it } from "vitest";
import { GESTURES, ONE_SHOT, TOOLS, beginGesture } from "./gesture.js";
import { commandOf, TOOL_KEYS } from "./keymap.js";
import { SHAPE_TOOLS, buildShape } from "./ops.js";
import { TOOL_BUTTONS } from "./Toolbar.jsx";
import { GESTURE_NAMES, PAGE_TOOLS, TOOLSET, toolsetFrom } from "./toolset.js";

const REGISTRY_SOURCE = path.resolve(import.meta.dirname, "../../../../herdr_team/canvas_kinds/__init__.py");
const down = { type: "down", world: [10, 20], screen: [10, 20], scale: 1, button: 0, buttons: 1 };
const ctx = { model: null, selection: [], writable: true, query: () => [], setSelection() {} };

describe("the tool manifest (assets/canvas/tokens.json tools)", () => {
  const manifest = tokens.tools;

  it("is there, and every entry is usable by this page", () => {
    expect(Array.isArray(manifest) && manifest.length > 0).toBe(true);
    expect(TOOLSET.all.length).toBe(PAGE_TOOLS.length + manifest.length); // nothing dropped
  });

  it("names only the gestures the registry allows, and the page has a handler for each", () => {
    const registry = fs.readFileSync(REGISTRY_SOURCE, "utf8").match(/^GESTURES = \(([^)]*)\)/m);
    expect(registry, "canvas_kinds.GESTURES").not.toBeNull();
    const names = [...registry[1].matchAll(/"([a-z]+)"/g)].map((m) => m[1]);
    expect(GESTURE_NAMES).toEqual(names);
    for (const name of GESTURE_NAMES) expect(typeof GESTURES[name]).toBe("function");
  });

  it("holds for every tool: unique id and key, a key that selects it, a button, a gesture", () => {
    expect(new Set(TOOLS).size).toBe(TOOLS.length);
    expect(new Set(Object.keys(TOOL_KEYS)).size).toBe(TOOLS.length);
    expect(TOOLS.slice(0, 2)).toEqual(["select", "hand"]);
    expect(TOOL_BUTTONS.map((b) => b[0])).toEqual(TOOLS);
    for (const t of manifest) {
      expect(TOOLS).toContain(t.id);
      expect(TOOL_KEYS[t.key]).toBe(t.id);
      expect(commandOf({ type: "keydown", key: t.key })).toEqual({ command: "tool", tool: t.id });
      expect(commandOf({ type: "keydown", key: t.key }, { writable: false })).toBeNull();
      expect(ONE_SHOT.has(t.id)).toBe(t.one_shot);
      expect(beginGesture(t.id, down, ctx), `${t.id} starts a gesture`).toBeTruthy();
      expect(beginGesture(t.id, down, { ...ctx, writable: false })?.finish?.(down)?.ops).toBeFalsy();
      if (t.gesture === "shape") {
        expect(SHAPE_TOOLS[t.id]).toBe(t.kind);
        expect(buildShape(t.kind, { point: [0, 0] })).toMatchObject({ op: "shape", kind: t.kind });
      } else expect(SHAPE_TOOLS[t.id]).toBeUndefined();
    }
  });

  it("keeps today's tools, in today's order, whatever kinds are added", () => {
    // Phase 2 (6.4): N is the sticky (the note keeps no tool), then the card and section tools.
    const today = ["select", "hand", "box", "ellipse", "diamond", "sticky", "card", "text", "arrow", "pen", "frame", "section"];
    let at = 0;
    for (const id of TOOLS) if (id === today[at]) at += 1;
    expect(at, `${JSON.stringify(today)} is an ordered subsequence of ${JSON.stringify(TOOLS)}`).toBe(today.length);
    for (const id of ["box", "ellipse", "diamond", "sticky", "card", "text", "arrow", "frame", "section"]) expect(ONE_SHOT.has(id)).toBe(true);
    expect(ONE_SHOT.has("pen")).toBe(false);
  });
});

describe("toolsetFrom", () => {
  it("takes a new kind's tool with no page edit", () => {
    const set = toolsetFrom([...tokens.tools, { id: "stamp", kind: "stamp", key: "k", title: "Stamp", glyph: "S", gesture: "shape", one_shot: true }]);
    expect(set.ids.at(-1)).toBe("stamp");
    expect(set.keys.k).toBe("stamp");
    expect(set.shapes.stamp).toBe("stamp");
    expect(set.oneShot.has("stamp")).toBe(true);
    const g = beginGesture("stamp", down, ctx, set);
    expect(g && typeof g.finish).toBe("function");
  });

  it("leaves out what this page cannot use", () => {
    const set = toolsetFrom([
      { id: "a", kind: "a", key: "q", title: "A", glyph: "A", gesture: "teleport" }, // gesture from a newer server
      { id: "b", kind: "b", key: "v", title: "B", glyph: "B", gesture: "shape" }, // select's key
      { id: "c", kind: "c", key: "c", title: "C", glyph: "C", gesture: "pen" },
      { id: "d", kind: "d", key: "c", title: "D", glyph: "D", gesture: "pen" }, // key taken
      { id: "hand", kind: "e", key: "e", title: "E", glyph: "E", gesture: "pen" }, // id taken
      { id: "f", kind: "f", key: "F", title: "F", glyph: "F", gesture: "pen" }, // not lower case
      null,
      "x",
    ]);
    expect(set.ids).toEqual(["select", "hand", "c"]);
    expect(set.oneShot.has("c")).toBe(true);
    expect(toolsetFrom(undefined).ids).toEqual(["select", "hand"]);
  });
});
