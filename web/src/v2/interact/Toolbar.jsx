// The board's tool bar (writable pages) and view controls (every page). Its buttons are the
// page's select and hand tools, then each kind's tool from the registry (toolset.js).
import React from "react";
import { TOOLSET } from "./toolset.js";

// [id, title, key, glyph] per button, in tool bar order.
export const TOOL_BUTTONS = TOOLSET.all.map((t) => [t.id, t.title, t.key.toUpperCase(), t.glyph]);

export default function Toolbar({ tool, onTool, writable }) {
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
