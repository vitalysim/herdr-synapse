import { describe, expect, it } from "vitest";
import { commandOf, isTyping } from "./keymap.js";

const key = (k, extra = {}) => ({ type: "keydown", key: k, target: { tagName: "DIV" }, ...extra });

describe("keymap", () => {
  it("maps the tool letters", () => {
    const tools = { v: "select", h: "hand", r: "box", o: "ellipse", d: "diamond", n: "note", t: "text", a: "arrow", p: "pen", f: "frame" };
    for (const [letter, tool] of Object.entries(tools)) {
      expect(commandOf(key(letter))).toEqual({ command: "tool", tool });
      expect(commandOf(key(letter.toUpperCase()))).toEqual({ command: "tool", tool });
    }
  });
  it("maps the commands", () => {
    expect(commandOf(key("Escape"))).toEqual({ command: "cancel" });
    expect(commandOf(key("Enter"))).toEqual({ command: "edit" });
    expect(commandOf(key("Delete"))).toEqual({ command: "delete" });
    expect(commandOf(key("Backspace"))).toEqual({ command: "delete" });
    expect(commandOf(key("ArrowLeft"))).toEqual({ command: "nudge", key: "ArrowLeft", far: false });
    expect(commandOf(key("ArrowDown", { shiftKey: true }))).toEqual({ command: "nudge", key: "ArrowDown", far: true });
    expect(commandOf(key("z", { metaKey: true }))).toEqual({ command: "undo" });
    expect(commandOf(key("z", { ctrlKey: true }))).toEqual({ command: "undo" });
    expect(commandOf(key("Z", { metaKey: true, shiftKey: true }))).toEqual({ command: "redo" });
    expect(commandOf(key("a", { metaKey: true }))).toEqual({ command: "select_all" });
    expect(commandOf(key("0", { metaKey: true }))).toEqual({ command: "fit" });
    expect(commandOf(key("=", { metaKey: true }))).toEqual({ command: "zoom", factor: 1.25 });
    expect(commandOf(key("-", { metaKey: true }))).toEqual({ command: "zoom", factor: 0.8 });
    expect(commandOf(key(" "))).toEqual({ command: "pan_start" });
    expect(commandOf({ type: "keyup", key: " ", target: null })).toEqual({ command: "pan_end" });
    expect(commandOf(key("q"))).toBeNull();
  });
  it("does nothing while typing", () => {
    for (const target of [{ tagName: "TEXTAREA" }, { tagName: "INPUT", type: "text" }, { tagName: "SELECT" }, { tagName: "DIV", isContentEditable: true }]) {
      expect(isTyping(target)).toBe(true);
      for (const k of ["v", "Delete", "Backspace", "ArrowLeft", "Enter", " "]) expect(commandOf(key(k, { target }))).toBeNull();
      expect(commandOf(key("z", { target, metaKey: true }))).toBeNull();
    }
    expect(isTyping({ tagName: "INPUT", type: "checkbox" })).toBe(false);
    expect(isTyping({ tagName: "BUTTON" })).toBe(false);
  });
  it("keeps only viewing on a read-only page", () => {
    const ro = { writable: false };
    for (const k of ["Delete", "Backspace", "ArrowLeft", "Enter", "r", "o", "d", "n", "t", "a", "p", "f"]) expect(commandOf(key(k), ro)).toBeNull();
    expect(commandOf(key("z", { metaKey: true }), ro)).toBeNull();
    expect(commandOf(key("v"), ro)).toEqual({ command: "tool", tool: "select" });
    expect(commandOf(key("h"), ro)).toEqual({ command: "tool", tool: "hand" });
    expect(commandOf(key("0", { metaKey: true }), ro)).toEqual({ command: "fit" });
    expect(commandOf(key("Escape"), ro)).toEqual({ command: "cancel" });
  });
});
