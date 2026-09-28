// The board's tool bar (writable pages) and view controls (every page). Its buttons are the
// page's select and hand tools, then each kind's tool from the registry (toolset.js), then the
// operator's collaboration buttons (canvas-v2-phase5.md 12.5): Freeze (the selection, or arms the
// freeze region tool) and Review (n), the open proposals' queue.
import React from "react";
import { TOOLSET } from "./toolset.js";

// [id, title, key, glyph] per button, in tool bar order.
export const TOOL_BUTTONS = TOOLSET.all.map((t) => [t.id, t.title, t.key.toUpperCase(), t.glyph]);

export const FREEZE_TOOL = "freeze";

export default function Toolbar({ tool, onTool, writable, onFreeze = null, review = null }) {
  if (!writable) return null;
  return (
    <div className="v2-toolbar" role="toolbar" aria-label="Tools">
      {TOOL_BUTTONS.map(([id, title, key, glyph]) => (
        <button
          key={id}
          type="button"
          data-tool={id}
          className={tool === id ? "v2-tool on" : "v2-tool"}
          title={`${title} (${key})`}
          aria-label={title}
          aria-pressed={tool === id}
          onClick={() => onTool(id)}
        >
          <span aria-hidden="true">{glyph}</span>
          <kbd>{key}</kbd>
        </button>
      ))}
      {onFreeze || review ? <span className="v2-tool-sep" aria-hidden="true" /> : null}
      {onFreeze ? (
        <button
          type="button"
          data-tool={FREEZE_TOOL}
          className={tool === FREEZE_TOOL ? "v2-tool on" : "v2-tool"}
          title="Freeze the selection, or drag over an area to freeze it"
          aria-label="Freeze"
          aria-pressed={tool === FREEZE_TOOL}
          onClick={onFreeze}
        >
          <span aria-hidden="true">❄</span>
        </button>
      ) : null}
      {review ? (
        <button
          type="button"
          data-review={review.count}
          className={`v2-tool v2-review${review.open ? " on" : ""}${review.count ? " has" : ""}`}
          title="Review the agents' proposals ([ and ] step through them)"
          aria-label={`Review (${review.count})`}
          aria-pressed={Boolean(review.open)}
          onClick={review.onToggle}
        >
          Review ({review.count})
        </button>
      ) : null}
    </div>
  );
}

export function ViewControls({ scale, onZoom, onFit }) {
  return (
    <div className="v2-view" role="group" aria-label="View">
      <button type="button" title="Zoom out (Cmd -)" aria-label="Zoom out" onClick={() => onZoom(0.8)}>
        −
      </button>
      <button type="button" className="v2-zoom" title="Fit to the board (Cmd 0)" aria-label="Fit to the board" onClick={onFit}>
        {Math.round((scale || 1) * 100)}%
      </button>
      <button type="button" title="Zoom in (Cmd =)" aria-label="Zoom in" onClick={() => onZoom(1.25)}>
        +
      </button>
    </div>
  );
}
