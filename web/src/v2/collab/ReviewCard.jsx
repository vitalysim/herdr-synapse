// The review card (canvas-v2-phase5.md 12.3): what an agent suggests, in its words and the
// server's summary, beside the ghost the server drew. On a writable page (the operator in person)
// it offers Accept, Reject (with an optional note), Ask (a comment to the proposer on the first
// target) and Zoom to; a read-only page shows the same card without the buttons. It only calls
// back: the ops are Board's, and the server decides.
import React, { useEffect, useRef, useState } from "react";
import { MAX_LABEL_CHARS } from "./ops.js";

export const CARD_W = 320;
const GAP = 12;
const MARGIN = 8;

// The words for a proposal's reason (2.3). `owner` is whose work or lane it is, when known.
export function reasonWords(reason, owner = null) {
  const whose = owner && owner !== "human" ? `${owner}'s` : "another agent's";
  switch (reason) {
    case "human_made":
      return "changes your marks";
    case "peer":
      return `edits ${whose} work`;
    case "foreign_lane":
      return `in ${whose} lane`;
    case "frozen":
      return "in a frozen area";
    default:
      return reason ? String(reason).replace(/_/g, " ") : "";
  }
}

/**
 * Where the card goes: beside the entry's screen box {x, y, w, h}, on the right when it fits, else
 * on the left, else below; kept inside the viewport {w, h}. -> {left, top, side}
 */
export function cardPosition(box, viewport, { w = CARD_W, h = 240 } = {}) {
  const vw = Math.max(0, Number(viewport && viewport.w) || 0);
  const vh = Math.max(0, Number(viewport && viewport.h) || 0);
  const clampY = (y) => Math.max(MARGIN, Math.min(y, vh - h - MARGIN));
  const clampX = (x) => Math.max(MARGIN, Math.min(x, vw - w - MARGIN));
  if (!box) return { left: clampX((vw - w) / 2), top: clampY(MARGIN * 8), side: "center" };
  if (box.x + box.w + GAP + w <= vw - MARGIN) return { left: box.x + box.w + GAP, top: clampY(box.y), side: "right" };
  if (box.x - GAP - w >= MARGIN) return { left: box.x - GAP - w, top: clampY(box.y), side: "left" };
  const below = box.y + box.h + GAP;
  if (below + h <= vh - MARGIN) return { left: clampX(box.x), top: below, side: "below" };
  return { left: clampX(box.x), top: clampY(box.y - GAP - h), side: "above" };
}

export default function ReviewCard({ id, proposal, box, viewport, writable, chip, owner = null, busy = false, onAccept, onReject, onAsk, onZoom, onClose }) {
  const [mode, setMode] = useState(null); // null | "reject" | "ask"
  const [note, setNote] = useState("");
  const inputRef = useRef(null);
  useEffect(() => {
    setMode(null);
    setNote("");
  }, [id]);
  useEffect(() => {
    if (mode && inputRef.current) inputRef.current.focus();
  }, [mode]);
  if (!proposal) return null;
  const author = proposal.author || "an agent";
  const outdated = Boolean(proposal.outdated);
  const pos = cardPosition(box, viewport);
  const summary = Array.isArray(proposal.summary) ? proposal.summary : [];
  const baseNote = Array.isArray(proposal.base_note) ? proposal.base_note : [];
  const words = reasonWords(proposal.reason, owner);
  const open = (next) => {
    setMode(next);
    setNote(next === "ask" ? `@${author} ` : "");
  };
  const submit = () => {
    if (mode === "reject") onReject && onReject(note);
    else if (mode === "ask" && note.trim()) onAsk && onAsk(note);
    setMode(null);
    setNote("");
  };
  return (
    <div
      className={`cv2-card cv2-review${outdated ? " cv2-outdated" : ""}`}
      role="dialog"
      aria-label={`Proposal ${id} by ${author}`}
      data-proposal={id}
      data-side={pos.side}
      style={{ left: pos.left, top: pos.top, width: CARD_W }}
      onPointerDown={(e) => e.stopPropagation()}
    >
      <div className="cv2-card-head">
        <span className="author-chip" style={{ background: chip && chip.bg, color: chip && chip.fg }} aria-hidden="true">
          {(chip && chip.initials) || author.slice(0, 2).toUpperCase()}
        </span>
        <b>{author} suggests</b>
        <span className="muted"> {id}</span>
        <button type="button" className="close" aria-label="Close" onClick={onClose}>
          ×
        </button>
      </div>
      {proposal.intent ? <div className="cv2-card-intent">{proposal.intent}</div> : null}
      {summary.length ? (
        <ul className="cv2-card-lines">
          {summary.map((line, i) => (
            <li key={i}>{line}</li>
          ))}
        </ul>
      ) : null}
      {words ? <div className="cv2-card-reason">{words}</div> : null}
      {baseNote.length ? (
        <div className="cv2-card-note" data-base-note="">
          {baseNote.map((line, i) => (
            <div key={i}>{line}</div>
          ))}
        </div>
      ) : null}
      {outdated ? <div className="cv2-card-warn">Outdated: what it changes has changed since. Reject it, or ask {author} for a new one.</div> : null}
      {writable && mode ? (
        <div className="field-row cv2-card-input">
          <input
            ref={inputRef}
            type="text"
            value={note}
            maxLength={mode === "reject" ? MAX_LABEL_CHARS : 2000}
            placeholder={mode === "reject" ? "why (optional)" : "your question"}
            aria-label={mode === "reject" ? "Reject note" : "Question"}
            onChange={(e) => setNote(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter") {
                e.preventDefault();
                submit();
              } else if (e.key === "Escape") {
                e.preventDefault();
                e.stopPropagation();
                setMode(null);
              }
            }}
          />
          <button type="button" onClick={submit} disabled={busy || (mode === "ask" && !note.trim())}>
            {mode === "reject" ? "Reject" : "Send"}
          </button>
        </div>
      ) : null}
      <div className="cv2-card-actions">
        {writable ? (
          <>
            <button type="button" className="cv2-accept" disabled={busy || outdated} title={outdated ? "It is outdated: reject it or ask for a new one" : "Apply exactly what the ghost shows"} onClick={onAccept}>
              Accept
            </button>
            <button type="button" className="cv2-reject" disabled={busy} onClick={() => open("reject")}>
              Reject
            </button>
            <button type="button" className="cv2-ask" disabled={busy} onClick={() => open("ask")}>
              Ask
            </button>
          </>
        ) : null}
        <button type="button" className="cv2-zoom" onClick={onZoom}>
          Zoom to
        </button>
      </div>
    </div>
  );
}
