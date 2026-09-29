// The canvas v2 migration notice (canvas-v2-phase6.md 2.4): shown on the operator's writable page when
// the board was drawn before canvas v2 and she has not answered yet. Board loads this chunk only then,
// so it costs the first load nothing (its few style rules are in interact/board.css).
//
// "Fix sizes" sends one `migrate` op (apply): the old labels are sized again and the marks in 0.21's
// sketch style (rough strokes, the hand font) drawn clean, in one batch undo takes back. "Dismiss" records the answer and changes nothing. "Not now" hides
// the notice for this page visit. "Details" says what is counted and what v2 draws differently.
import React, { useState } from "react";

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`;
const num = (v) => (Number.isFinite(v) ? v : 0);

// "14 labels need resizing and 3 marks in the old sketch style are now drawn clean" from the server's
// counts. 0.21 drew every mark sketchy by default, so that count is usually the whole board: the words
// name the style, never a hand (herdr_team/canvas_migrate.py is_sketch).
export function noticeParts(info) {
  const parts = [];
  const overflow = num(info && info.overflow);
  const refit = num(info && info.refit);
  const sketch = num(info && info.sketch);
  if (overflow) parts.push(`${plural(overflow, "label needs", "labels need")} resizing`);
  else if (refit) parts.push(`${plural(refit, "label is", "labels are")} sized for the old fonts`);
  if (sketch) parts.push(`${plural(sketch, "mark in the old sketch style is", "marks in the old sketch style are")} now drawn clean`);
  return parts;
}

export function noticeText(info) {
  const parts = noticeParts(info);
  return `This board was drawn before canvas v2.${parts.length ? ` ${parts.join(" and ")}.` : ""}`;
}

// The details, in plain words: what is counted (2.3) and what v2 draws differently (2.2).
export function detailLines(info) {
  const i = info || {};
  return [
    `${plural(num(i.pre_022), "label", "labels")} from before canvas v2; Fix sizes sizes ${num(i.refit)} of them again (they only grow, and what sits beside them makes room).`,
    `${plural(num(i.overflow), "label does", "labels do")} not fit its shape with today's fonts; until it is fixed it is drawn cut to its shape.`,
    `${plural(num(i.sketch), "mark is", "marks are")} in 0.21's sketch style, which it used by default (rough strokes or the hand font); canvas v2 draws them clean, and Fix sizes stores them that way.`,
    `${plural(num(i.legacy_colour), "mark keeps", "marks keep")} its old colour as a tone; ${plural(num(i.unknown_hex), "colour is", "colours are")} drawn exactly as given; ${plural(num(i.vega_lite), "Vega-Lite chart is", "Vega-Lite charts are")} still drawn.`,
    "Not on canvas v2: rough strokes and the hand font (the classic canvas still draws them), and what Excalidraw kept in this browser (its library, the last scroll and zoom, zen and grid mode).",
    "Nothing stored is lost. Fix sizes is one batch: undo takes it back.",
  ];
}

// What the server did, from the applied entry's `migrate` record.
export function resultText(entry) {
  const m = entry && entry.migrate;
  if (!m) return null;
  if (m.action === "dismiss") return "Migration notice dismissed; nothing on the board changed.";
  return `Migrated: ${num(m.refitted)} refitted (${num(m.grew)} grew, ${num(m.moved)} moved to fit), ${num(m.clean)} drawn clean; undo ${m.batch || "the batch"} to take it back.`;
}

export const MIGRATE_INTENT = { apply: "migrate to canvas v2", dismiss: "dismiss the canvas v2 migration notice" };

export default function MigrationBanner({ info, send, toast = () => {} }) {
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  // "Not now": hidden for this page visit (Board keeps this component mounted while the notice is pending).
  const [later, setLater] = useState(false);
  if (later) return null;
  const run = async (action) => {
    if (busy || !send) return;
    setBusy(true);
    try {
      const result = await send([{ op: "migrate", action, intent: MIGRATE_INTENT[action] }]);
      const entry = result && (result.applied || []).find((e) => e && e.op === "migrate");
      const text = resultText(entry);
      if (text) toast(text, "ok");
    } finally {
      setBusy(false);
    }
  };
  return (
    <div className="cv2-migrate" role="status" data-migration="pending">
      <div className="cv2-migrate-row">
        <span className="cv2-migrate-text">{noticeText(info)}</span>
        <button type="button" className="chip on" disabled={busy} onClick={() => run("apply")} data-action="apply">
          Fix sizes
        </button>
        <button type="button" className="chip" aria-expanded={open} onClick={() => setOpen((v) => !v)} data-action="details">
          Details
        </button>
        <button type="button" className="chip" onClick={() => setLater(true)} data-action="later">
          Not now
        </button>
        <button type="button" className="chip" disabled={busy} onClick={() => run("dismiss")} data-action="dismiss">
          Dismiss
        </button>
      </div>
      {open ? (
        <ul className="cv2-migrate-details">
          {detailLines(info).map((line) => (
            <li key={line}>{line}</li>
          ))}
        </ul>
      ) : null}
    </div>
  );
}
