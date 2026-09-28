// The v2 board's tools (QA phase 1, V-2): select and hand are the page's own; every create tool
// comes from the kind registry, as the `tools` list herdr_team/canvas_theme writes into
// assets/canvas/tokens.json (one per kind with a canvas_kinds.Tool: its key, title, glyph, gesture
// and whether it is one-shot). A new kind whose tool uses one of the gestures below needs no edit
// here, in the tool bar, the keymap or the gesture switch.
import tokens from "@synapse/tokens";

// The page's own tools, before the kinds' ones.
export const PAGE_TOOLS = [
  { id: "select", title: "Select", key: "v", glyph: "↖", gesture: null, one_shot: false },
  { id: "hand", title: "Hand", key: "h", glyph: "✋", gesture: null, one_shot: false },
];

// How a kind's tool makes one (canvas_kinds.GESTURES); gesture.js maps each to its tools/*.js.
// "block" sends the tool's template op at a click or over a dragged box (canvas-v2-phase2.md 6.3 W-e).
export const GESTURE_NAMES = ["shape", "text", "connect", "pen", "frame", "block"];

// A tool's `template` (6.4): the op it sends, without placement; an object, or its JSON text.
export function templateOf(value) {
  let t = value;
  if (typeof t === "string") {
    try {
      t = JSON.parse(t);
    } catch {
      return null;
    }
  }
  return t && typeof t === "object" && !Array.isArray(t) && typeof t.op === "string" && t.op ? t : null;
}

const isTool = (t) =>
  t &&
  typeof t === "object" &&
  typeof t.id === "string" &&
  t.id &&
  typeof t.kind === "string" &&
  typeof t.key === "string" &&
  /^[a-z]$/.test(t.key) &&
  GESTURE_NAMES.includes(t.gesture) &&
  // A template that is present must be an op; the block gesture has nothing to send without one.
  (t.template === undefined || t.template === null ? t.gesture !== "block" : Boolean(templateOf(t.template)));

// toolsetFrom(manifest) -> the tool tables, from a `tools` list (tokens.json). Entries this page
// cannot use (an unknown gesture from a newer server, a key already taken) are left out.
export function toolsetFrom(manifest) {
  const all = [...PAGE_TOOLS];
  const keys = new Set(PAGE_TOOLS.map((t) => t.key));
  const ids = new Set(PAGE_TOOLS.map((t) => t.id));
  // The manifest lists its tools in tool bar order (canvas_theme writes them sorted by `order`).
  for (const t of Array.isArray(manifest) ? manifest : []) {
    if (!isTool(t) || keys.has(t.key) || ids.has(t.id)) continue;
    keys.add(t.key);
    ids.add(t.id);
    const tool = { id: t.id, kind: t.kind, title: String(t.title || t.kind), key: t.key, glyph: String(t.glyph || t.kind.slice(0, 1).toUpperCase()), gesture: t.gesture, one_shot: t.one_shot !== false };
    const template = templateOf(t.template);
    if (template) tool.template = template;
    all.push(tool);
  }
  const byId = new Map(all.map((t) => [t.id, t]));
  return {
    all,
    // Tool ids in tool bar order.
    ids: all.map((t) => t.id),
    // Tools that go back to select after one use.
    oneShot: new Set(all.filter((t) => t.one_shot).map((t) => t.id)),
    // Key (lower case) -> tool id.
    keys: Object.fromEntries(all.map((t) => [t.key, t.id])),
    // Tool id -> the kind the shape op makes (the "shape" gesture's tools without a template).
    shapes: Object.fromEntries(all.filter((t) => t.gesture === "shape" && !t.template).map((t) => [t.id, t.kind])),
    get: (id) => byId.get(id) || null,
  };
}

export const TOOLSET = toolsetFrom(tokens.tools);
