// The collaboration layer's actions (canvas-v2-phase5.md 12.3 to 12.5): what each button of the
// panel, the cards and the notices sends. Built in the lazily loaded chrome (ui.jsx) from the
// hook's `ctx` (index.js), so none of it weighs on the board's first load. Every action is an op
// through Board's sendOps; the server decides.
import { freezesOf, proposalsOf, reviewBox } from "./model.js";
import * as collabOps from "./ops.js";

const appliedOk = (result) => Boolean(result && (result.applied || []).length);

/**
 * createActions(ctx) -> {openProposal, accept, reject, ask, zoomTo, revert, saveCheckpoint,
 * restore, removeCheckpoint, setSettings, thaw, focusFreeze, revertBatch}
 *   ctx: {run(op, opts), live: {current: {dl, ...}}, openReview(id, {zoom}), nextAfterDecision(id),
 *   zoomToBox(bbox), toast(text, tone), elementOf(id), setConfirm, setFreezeId, setReviewId}
 */
export function createActions(ctx) {
  const { run, live, openReview, nextAfterDecision, zoomToBox, toast, elementOf, setConfirm, setFreezeId, setReviewId } = ctx;
  const firstTargetOf = (p) => {
    const id = (p && ((p.targets && p.targets[0]) || null)) || null;
    if (id && elementOf(id)) return id;
    if (p && Array.isArray(p.bbox)) return [Math.round((p.bbox[0] + p.bbox[2]) / 2), Math.round(p.bbox[1])];
    return null;
  };
  return {
    openProposal: (id) => openReview(id, { zoom: true }),
    accept: async (id) => {
      const result = await run(collabOps.buildAccept(id));
      if (appliedOk(result)) {
        toast(`${id} accepted`, "ok");
        nextAfterDecision(id);
      }
    },
    reject: async (id, note) => {
      const result = await run(collabOps.buildReject(id, note));
      if (appliedOk(result)) {
        toast(`${id} rejected`, "ok");
        nextAfterDecision(id);
      }
    },
    ask: async (id, text) => {
      const p = proposalsOf(live.current.dl).get(id);
      const at = firstTargetOf(p);
      if (!at || !String(text || "").trim()) return;
      const result = await run({ op: "comment", at, text: String(text).trim() });
      if (appliedOk(result)) toast(`asked ${p ? p.author : "the agent"}`, "ok");
    },
    zoomTo: (id) => {
      const box = reviewBox(live.current.dl, id);
      if (box) zoomToBox(box);
    },
    revert: async (author, since, label) => {
      const op = collabOps.buildUndoAuthor(author, since);
      if (!op) return;
      const result = await run(op, { isUndo: true });
      if (result && !appliedOk(result) && !(result.refused || []).length) toast(`nothing of ${author}'s to revert ${label || ""}`.trim(), "info");
    },
    saveCheckpoint: async (label) => {
      const result = await run(collabOps.buildCheckpoint(label));
      const made = result && (result.applied || []).find((a) => a && Array.isArray(a.ids) && a.ids.some((i) => /^V-/.test(i)));
      if (appliedOk(result)) toast(`checkpoint ${made ? made.ids.find((i) => /^V-/.test(i)) : "saved"}`, "ok");
    },
    restore: (id) =>
      setConfirm({
        title: `Restore ${id}?`,
        text: "A checkpoint of now is saved first, so you can come back. Comments stay as they are.",
        confirm: "Restore",
        run: () => run(collabOps.buildRestore(id)),
      }),
    removeCheckpoint: (id) => run(collabOps.buildCheckpointRemove(id)),
    setSettings: (patch) => run(collabOps.buildSettings(patch)),
    thaw: async (id) => {
      const result = await run(collabOps.buildThaw(id));
      if (appliedOk(result)) setFreezeId(null);
    },
    focusFreeze: (id) => {
      const f = freezesOf(live.current.dl).find((e) => e.id === id);
      if (!f) return;
      zoomToBox(f.bbox);
      setReviewId(null);
      setFreezeId(id);
    },
    revertBatch: (batch) => run(collabOps.buildUndoBatch(batch), { isUndo: true }),
  };
}
