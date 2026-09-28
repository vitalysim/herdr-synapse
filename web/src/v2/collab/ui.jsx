// The collaboration layer's chrome, split from the board's first load (canvas-v2-phase5.md 12.6):
// the panel, the cards, the queue bar, the notices, the confirm dialog and the presence halos.
// index.js loads it lazily (the board draws first; the halos come once someone is present), and
// hands each part the hook's `ctx`, from which the part builds its actions (actions.js).
import React, { useMemo } from "react";
import { createActions } from "./actions.js";
import Panel from "./CollabPanel.jsx";
import { FreezeCard as FreezeCardView, Notices as NoticesView } from "./Dialogs.jsx";
import ReviewCardView from "./ReviewCard.jsx";

export { ConfirmDialog, QueueBar } from "./Dialogs.jsx";
export { default as Halos } from "./Halos.jsx";

const useActions = (ctx) => useMemo(() => createActions(ctx), [ctx]);

export function CollabPanel({ ctx, ...props }) {
  const actions = useActions(ctx);
  return <Panel {...props} actions={actions} />;
}

export function ReviewCard({ ctx, id, ...props }) {
  const actions = useActions(ctx);
  return (
    <ReviewCardView
      {...props}
      id={id}
      onAccept={() => actions.accept(id)}
      onReject={(note) => actions.reject(id, note)}
      onAsk={(text) => actions.ask(id, text)}
      onZoom={() => actions.zoomTo(id)}
    />
  );
}

export function FreezeCard({ ctx, ...props }) {
  const actions = useActions(ctx);
  return <FreezeCardView {...props} onThaw={() => actions.thaw(props.freeze.id)} />;
}

// A notice with a `batch` offers Revert: undo {batch}.
export function Notices({ ctx, notices, onDismiss }) {
  const actions = useActions(ctx);
  const list = useMemo(() => notices.map((n) => (n.batch ? { ...n, action: { label: "Revert", run: () => actions.revertBatch(n.batch) } } : n)), [notices, actions]);
  return <NoticesView notices={list} onDismiss={onDismiss} />;
}
