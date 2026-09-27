// slot "viz": the agent's live HTML visual, in a sandboxed iframe (viz/VizFrame.jsx) placed in the
// Surface's HTML layer over the SVG. It takes pointer events only while its entry is selected, so
// a click on it selects it first. The frame's own still (a data: URL it posts) reaches onStill as
// a PNG blob once per (id, v).
import React, { useCallback } from "react";
import VizFrame from "../../../viz/VizFrame.jsx";

// Decoded here rather than with fetch(dataURL): the page's CSP (connect-src 'self') refuses data:.
async function dataURLToBlob(url) {
  const m = /^data:([^;,]+)(;base64)?,(.*)$/s.exec(String(url));
  if (!m) throw new Error("not a data URL");
  const raw = m[2] ? atob(m[3]) : decodeURIComponent(m[3]);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
  return new Blob([bytes], { type: m[1] });
}

export default function VizSlot({ prim, entryId, version, element, team, selected, onStill }) {
  const still = useCallback(
    (_element, dataURL) => {
      if (!onStill) return;
      dataURLToBlob(dataURL)
        .then((blob) => onStill(entryId, version, blob))
        .catch(() => undefined);
    },
    [entryId, version, onStill],
  );
  return (
    <div
      className="sv2-viz"
      data-slot="viz"
      data-id={entryId}
      style={{ left: prim.x, top: prim.y, width: prim.w, height: prim.h, pointerEvents: selected ? "auto" : "none" }}
    >
      <VizFrame team={team} element={element} onStill={still} />
    </div>
  );
}
