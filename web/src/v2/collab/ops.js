// The collaboration ops the v2 board sends (canvas-v2-phase5.md 11.2 I-2), shaped here and only
// here. Pure: each builder returns a plain op object, or null when there is nothing to send. The
// server decides who may run them (accept, reject, freeze, thaw, settings, restore and a forced
// undo are the operator's in person); a button on the page is only an affordance.

export const HUMAN_EDITS = ["propose", "live"];
export const FROZEN = ["propose", "refuse"];
export const DEFAULT_SETTINGS = Object.freeze({ human_edits: "propose", frozen: "propose" });
// canvas.MAX_LABEL_CHARS: labels and notes are one line of at most this many characters.
export const MAX_LABEL_CHARS = 120;

const PROPOSAL_ID = /^P-[1-9][0-9]*$/;
export const CHECKPOINT_ID = /^V-[1-9][0-9]*$/;
const FREEZE_ID = /^X-[1-9][0-9]*$/;
const BATCH_ID = /^B-[1-9][0-9]*$/;
const ELEMENT_ID = /^(E|C)-[1-9][0-9]*$/;
const AUTHOR = /^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$/;

// One line, trimmed and capped; "" when nothing is left.
export function oneLine(text, max = MAX_LABEL_CHARS) {
  const line = String(text ?? "").replace(/[\r\n\t]+/g, " ").replace(/\s+/g, " ").trim();
  return [...line].slice(0, max).join("");
}

function withNote(op, note) {
  const line = oneLine(note);
  if (line) op.note = line;
  return op;
}

export function buildAccept(id, note = "") {
  return PROPOSAL_ID.test(String(id)) ? withNote({ op: "accept", id }, note) : null;
}

export function buildReject(id, note = "") {
  return PROPOSAL_ID.test(String(id)) ? withNote({ op: "reject", id }, note) : null;
}

export function buildWithdraw(id) {
  return PROPOSAL_ID.test(String(id)) ? { op: "withdraw", id } : null;
}

const isRegion = (r) => Array.isArray(r) && r.length === 4 && r.every(Number.isFinite) && r[2] > r[0] && r[3] > r[1];

// freeze {ids, label?}: the selected elements (and what they hold). Overlays are never frozen.
export function buildFreezeIds(ids, label = "") {
  const list = [...new Set((ids || []).filter((id) => ELEMENT_ID.test(String(id))))];
  if (!list.length) return null;
  const op = { op: "freeze", ids: list };
  const line = oneLine(label);
  if (line) op.label = line;
  return op;
}

// freeze {region, label?}: a world rect [x0, y0, x1, y1], normalised and rounded outwards.
export function buildFreezeRegion(rect, label = "") {
  if (!Array.isArray(rect) || rect.length !== 4 || !rect.every(Number.isFinite)) return null;
  const region = [Math.floor(Math.min(rect[0], rect[2])), Math.floor(Math.min(rect[1], rect[3])), Math.ceil(Math.max(rect[0], rect[2])), Math.ceil(Math.max(rect[1], rect[3]))];
  if (!isRegion(region)) return null;
  const op = { op: "freeze", region };
  const line = oneLine(label);
  if (line) op.label = line;
  return op;
}

export function buildThaw(id) {
  return FREEZE_ID.test(String(id)) ? { op: "thaw", id } : null;
}

export function buildThawIds(ids) {
  const list = [...new Set((ids || []).filter((id) => ELEMENT_ID.test(String(id))))];
  return list.length ? { op: "thaw", ids: list } : null;
}

// settings {human_edits?, frozen?}: only the keys given, and only known values.
export function buildSettings(patch) {
  const op = { op: "settings" };
  if (patch && HUMAN_EDITS.includes(patch.human_edits)) op.human_edits = patch.human_edits;
  if (patch && FROZEN.includes(patch.frozen)) op.frozen = patch.frozen;
  return Object.keys(op).length > 1 ? op : null;
}

export function buildCheckpoint(label) {
  const line = oneLine(label);
  return line ? { op: "checkpoint", label: line } : null;
}

export function buildCheckpointRemove(id) {
  return CHECKPOINT_ID.test(String(id)) ? { op: "checkpoint", remove: id } : null;
}

export function buildRestore(id) {
  return CHECKPOINT_ID.test(String(id)) ? { op: "restore", id } : null;
}

// undo {batch, force?}: one batch (Cmd-Z, a Revert toast).
export function buildUndoBatch(batch, { force = false } = {}) {
  if (!BATCH_ID.test(String(batch))) return null;
  return force ? { op: "undo", batch, force: true } : { op: "undo", batch };
}

// undo {author, since?, force?}: every batch of one author after version `since` (all of them
// when `since` is null).
export function buildUndoAuthor(author, since = null, { force = false } = {}) {
  const name = String(author ?? "");
  if (name !== "human" && !AUTHOR.test(name)) return null;
  const op = { op: "undo", author: name };
  if (since !== null && since !== undefined) {
    if (!Number.isInteger(since) || since < 0) return null;
    op.since = since;
  }
  if (force) op.force = true;
  return op;
}

// The toast a revert or undo result earns (6.3): "3 of 7 reverted; E-12 edited by you later".
export function undoSummary(undo, who = (name) => name) {
  if (!undo || typeof undo !== "object") return "";
  const restored = Number(undo.restored) || 0;
  const of = Number(undo.of) || restored;
  const skipped = Array.isArray(undo.skipped) ? undo.skipped : [];
  const head = `${restored} of ${of} reverted`;
  if (!skipped.length) return head;
  const first = skipped[0];
  const more = skipped.length > 1 ? ` (and ${skipped.length - 1} more)` : "";
  if (first.reason === "frozen") return `${head}; ${first.id} is frozen${more}`;
  const by = first.by === "human" ? "you" : who(first.by || "someone");
  return `${head}; ${first.id} edited by ${by} later${more}`;
}

// The toast a restore result earns (7).
export function restoreSummary(restore) {
  if (!restore || typeof restore !== "object") return "";
  const n = (k) => Number(restore[k]) || 0;
  return `restored: ${n("added")} put back, ${n("changed")} changed back, ${n("deleted")} removed`;
}
