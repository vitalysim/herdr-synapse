import { describe, expect, it } from "vitest";
import { createUndoStack } from "./undo.js";

describe("undo stack", () => {
  it("undoes the newest own batch first, each once", () => {
    const stack = createUndoStack();
    for (const batch of ["B-1", "B-2", "B-3"]) stack.push(batch);
    const order = [];
    for (let batch = stack.peek(); batch; batch = stack.peek()) {
      order.push(batch);
      stack.markUndone(batch);
    }
    expect(order).toEqual(["B-3", "B-2", "B-1"]);
  });
  it("skips what the scene says is already undone", () => {
    const stack = createUndoStack();
    stack.push("B-1");
    stack.push("B-2");
    expect(stack.peek((b) => b === "B-2")).toBe("B-1");
  });
  it("puts a refused undo back", () => {
    const stack = createUndoStack();
    stack.push("B-7");
    stack.markUndone("B-7");
    expect(stack.peek()).toBeNull();
    stack.restore("B-7");
    expect(stack.peek()).toBe("B-7");
  });
  it("ignores duplicates and non-batches, and keeps a bounded history", () => {
    const stack = createUndoStack({ limit: 3 });
    for (const batch of ["B-1", "B-1", null, "", "B-2", "B-3", "B-4"]) stack.push(batch);
    expect(stack.list()).toEqual(["B-2", "B-3", "B-4"]);
  });
});
