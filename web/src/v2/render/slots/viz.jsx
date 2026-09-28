// slot "viz": the agent's live HTML visual, in a sandboxed iframe (viz/VizFrame.jsx) placed in the
// Surface's HTML layer over the SVG. It takes pointer events only while its entry is selected, so
// a click on it selects it first. The frame's own still (a data: URL it posts) reaches onStill as
// a PNG blob once per (id, v).
//
// Live frames are budgeted (canvas-v2-phase3-4.md 3.12): each mounted frame holds a "viz" lease of
// the page's glBudget (6 at most, since a frame may hold a WebGL context). Past the limit the least
// recently touched frame is demoted: its iframe unmounts and it shows its still (or a paused card)
// until it is selected again, which asks for a lease anew.
import React, { useCallback, useEffect, useRef, useState } from "react";
import VizFrame from "../../../viz/VizFrame.jsx";
import { glBudget } from "../../scene3d/budget.js";

// Decoded here rather than with fetch(dataURL): the page's CSP (connect-src 'self') refuses data:.
async function dataURLToBlob(url) {
  const m = /^data:([^;,]+)(;base64)?,(.*)$/s.exec(String(url));
  if (!m) throw new Error("not a data URL");
  const raw = m[2] ? atob(m[3]) : decodeURIComponent(m[3]);
  const bytes = new Uint8Array(raw.length);
  for (let i = 0; i < raw.length; i += 1) bytes[i] = raw.charCodeAt(i);
  return new Blob([bytes], { type: m[1] });
}

export default function VizSlot({ prim, entryId, version, element, team, selected, onStill, urls }) {
  const lease = useRef(null);
  const [live, setLive] = useState(false);

  const request = useCallback(() => {
    if (lease.current) return true;
    const granted = glBudget.request("viz", entryId, {
      priority: 0,
      onDemote: () => {
        lease.current = null;
        setLive(false);
      },
    });
    lease.current = granted;
    setLive(!!granted);
    return !!granted;
  }, [entryId]);

  useEffect(() => {
    request();
    return () => {
      if (lease.current) glBudget.release(lease.current);
      lease.current = null;
    };
  }, [request]);

  // Selecting a frame touches its lease (the most recently used is demoted last), and brings a
  // demoted one back.
  useEffect(() => {
    if (!selected) return;
    if (lease.current) glBudget.touch(lease.current);
    else request();
  }, [selected, request]);

  const still = useCallback(
    (_element, dataURL) => {
      if (!onStill) return;
      dataURLToBlob(dataURL)
        .then((blob) => onStill(entryId, version, blob))
        .catch(() => undefined);
    },
    [entryId, version, onStill],
  );
  const stillURL = !live && typeof prim.still === "string" && prim.still && urls && urls.still ? urls.still(prim.still) : null;
  return (
    <div
      className={`sv2-viz${live ? "" : " sv2-viz-paused"}`}
      data-slot="viz"
      data-id={entryId}
      data-live={live ? "1" : "0"}
      style={{ left: prim.x, top: prim.y, width: prim.w, height: prim.h, pointerEvents: selected ? "auto" : "none" }}
    >
      {live ? (
        <VizFrame team={team} element={element} onStill={still} />
      ) : stillURL ? (
        <img src={stillURL} alt="" draggable={false} style={{ objectFit: "contain" }} />
      ) : (
        <div className="sv2-viz-card">Live visual paused: select it to resume</div>
      )}
    </div>
  );
}
