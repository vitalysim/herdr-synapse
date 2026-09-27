// Undo on the v2 board: the batches this page applied, newest last. Cmd/Ctrl-Z sends
// {op: "undo", batch} for the newest one not undone yet. There is no redo: the server undoes a
// batch by applying its inverse as a new batch, which this stack never records.

export const NO_REDO = "undo works per batch; redo is not available";

export function createUndoStack({ limit = 200 } = {}) {
  let batches = [];
  const undone = new Set();
  return {
    // A batch the page's own ops produced (not the batch of an undo op).
    push(batch) {
      if (typeof batch !== "string" || !batch || batches.includes(batch)) return;
      batches = batches.concat(batch).slice(-limit);
    },
    // The newest batch not undone yet, or null. `isUndone(batch)` lets the scene store say
    // that someone else (the side panel, an agent) already undid it.
    peek(isUndone = () => false) {
      for (let i = batches.length - 1; i >= 0; i -= 1) {
        const batch = batches[i];
        if (!undone.has(batch) && !isUndone(batch)) return batch;
      }
      return null;
    },
    markUndone(batch) {
      undone.add(batch);
    },
    // A refused undo puts the batch back.
    restore(batch) {
      undone.delete(batch);
    },
    list() {
      return batches.slice();
    },
  };
}
