// The v2 board's keyboard (canvas-v2-phase1.md 4.3): one table from a key to a command. Board
// runs the command; this file only decides which one. Nothing fires while a text field has focus.
import { TOOLSET } from "./toolset.js";

// Key -> tool: select (v) and hand (h), then each kind's tool key from the registry (toolset.js).
export const TOOL_KEYS = TOOLSET.keys;

// Esc with one block member selected selects its block (canvas-v2-phase2.md 6.3 W-g); an Esc on
// the block an Esc just selected (`parented`), or on anything else, clears the selection.
// escapeSelection(selection, entryOf, parented) -> {select: ids, parented: id | null}
export function escapeSelection(selection, entryOf, parented = null) {
  if (Array.isArray(selection) && selection.length === 1 && selection[0] !== parented) {
    const entry = entryOf(selection[0]);
    const parent = entry && typeof entry.block === "string" && entry.block !== entry.id ? entry.block : null;
    if (parent && entryOf(parent)) return { select: [parent], parented: parent };
  }
  return { select: [], parented: null };
}

// Tools a read-only page keeps.
export const READ_ONLY_TOOLS = new Set(["select", "hand"]);

// Whether the key event comes from somewhere the user types.
export function isTyping(target) {
  if (!target || typeof target !== "object") return false;
  const tag = String(target.tagName || "").toLowerCase();
  if (tag === "textarea" || tag === "select") return true;
  if (tag === "input") {
    const type = String(target.type || "text").toLowerCase();
    return !["button", "checkbox", "radio", "range", "color", "submit", "reset"].includes(type);
  }
  return Boolean(target.isContentEditable);
}

// commandOf(event, {writable, editing}) -> {command, ...args} | null
//   tool {tool}, cancel, edit, select_all, fit, zoom {factor}, delete, nudge {key, far}, undo,
//   pan_start, pan_end.
export function commandOf(event, { writable = true } = {}) {
  if (!event || isTyping(event.target)) return null;
  const key = event.key;
  const mod = Boolean(event.metaKey || event.ctrlKey);
  if (event.type === "keyup") return key === " " ? { command: "pan_end" } : null;
  if (key === "Escape") return { command: "cancel" };
  if (key === " " && !mod) return { command: "pan_start" };
  if (mod) {
    const lower = String(key).toLowerCase();
    if (lower === "z" && !event.shiftKey) return writable ? { command: "undo" } : null;
    if (lower === "z" && event.shiftKey) return writable ? { command: "redo" } : null;
    if (lower === "a") return { command: "select_all" };
    if (key === "0") return { command: "fit" };
    if (key === "=" || key === "+") return { command: "zoom", factor: 1.25 };
    if (key === "-" || key === "_") return { command: "zoom", factor: 0.8 };
    return null;
  }
  if (event.altKey) return null;
  if (key === "Enter") return writable ? { command: "edit" } : null;
  if (key === "Delete" || key === "Backspace") return writable ? { command: "delete" } : null;
  if (key === "ArrowLeft" || key === "ArrowRight" || key === "ArrowUp" || key === "ArrowDown") {
    return writable ? { command: "nudge", key, far: Boolean(event.shiftKey) } : null;
  }
  const tool = TOOL_KEYS[String(key).toLowerCase()];
  if (tool && !event.shiftKey && (writable || READ_ONLY_TOOLS.has(tool))) return { command: "tool", tool };
  return null;
}
