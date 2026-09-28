// The collaboration layer's small pieces of chrome: the freeze card (a freeze's label and Thaw),
// the confirm dialog (Restore), the review queue bar (Prev and Next through open proposals) and
// the notices that carry an action (a Revert on an agent's live change to the operator's marks).
// Each only calls back; every string is React text.
import React, { useEffect, useRef } from "react";
import { cardPosition } from "./ReviewCard.jsx";

export function FreezeCard({ freeze, box, viewport, writable, onThaw, onClose }) {
  if (!freeze) return null;
  const pos = cardPosition(box, viewport, { w: 260, h: 110 });
  const what = freeze.region ? "this area" : `${(freeze.ids || []).join(", ")}`;
  return (
    <div className="cv2-card cv2-freeze-card" role="dialog" aria-label={`Freeze ${freeze.id}`} data-freeze={freeze.id} style={{ left: pos.left, top: pos.top, width: 260 }} onPointerDown={(e) => e.stopPropagation()}>
      <div className="cv2-card-head">
        <b>{freeze.id} frozen</b>
        <button type="button" className="close" aria-label="Close" onClick={onClose}>
          ×
        </button>
      </div>
      <div className="cv2-card-intent">{freeze.label || "frozen"}</div>
      <div className="muted">Agents' changes to {what} become proposals or are refused, as the settings say. You can still edit it.</div>
      <div className="cv2-card-actions">
        {writable ? (
          <button type="button" className="cv2-thaw" onClick={onThaw}>
            Thaw
          </button>
        ) : null}
      </div>
    </div>
  );
}

export function ConfirmDialog({ open, title, text, confirm = "OK", onConfirm, onCancel }) {
  const okRef = useRef(null);
  useEffect(() => {
    if (open && okRef.current) okRef.current.focus();
  }, [open]);
  if (!open) return null;
  return (
    <div className="cv2-backdrop" onPointerDown={(e) => e.target === e.currentTarget && onCancel()}>
      <div
        className="cv2-dialog"
        role="alertdialog"
        aria-modal="true"
        aria-label={title}
        onKeyDown={(e) => {
          if (e.key === "Escape") {
            e.stopPropagation();
            onCancel();
          }
        }}
      >
        <div className="cv2-dialog-title">{title}</div>
        <div className="cv2-dialog-text">{text}</div>
        <div className="cv2-card-actions">
          <button type="button" onClick={onCancel}>
            Cancel
          </button>
          <button type="button" ref={okRef} className="cv2-accept" onClick={onConfirm}>
            {confirm}
          </button>
        </div>
      </div>
    </div>
  );
}

export function QueueBar({ queue, at, onPrev, onNext, onClose }) {
  if (!queue) return null;
  const n = queue.length;
  return (
    <div className="cv2-queue" role="group" aria-label="Review queue" data-queue={n}>
      <span>{n ? `Review ${Math.min(at + 1, n)} of ${n}` : "Nothing to review"}</span>
      <button type="button" disabled={n < 2} title="Previous ([)" onClick={onPrev}>
        ‹ Prev
      </button>
      <button type="button" disabled={n < 2} title="Next (])" onClick={onNext}>
        Next ›
      </button>
      <button type="button" className="close" aria-label="Close the review queue" onClick={onClose}>
        ×
      </button>
    </div>
  );
}

// Notices with an action: [{id, text, action: {label, run}}]. They stay until used or dismissed.
export function Notices({ notices, onDismiss }) {
  if (!notices || !notices.length) return null;
  return (
    <div className="cv2-notices" role="status">
      {notices.map((n) => (
        <div key={n.id} className={`cv2-notice ${n.tone || "info"}`} data-notice={n.kind || ""}>
          <span>{n.text}</span>
          {n.action ? (
            <button
              type="button"
              className="cv2-notice-action"
              onClick={() => {
                n.action.run();
                onDismiss(n.id);
              }}
            >
              {n.action.label}
            </button>
          ) : null}
          <button type="button" className="cv2-notice-close" aria-label="Dismiss" onClick={() => onDismiss(n.id)}>
            ×
          </button>
        </div>
      ))}
    </div>
  );
}
