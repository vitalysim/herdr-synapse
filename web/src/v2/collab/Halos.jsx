// The presence layer (canvas-v2-phase5.md 12.2): a halo per agent at work, in its chip colour, with
// a pill under it naming it and what it says it is doing (under, where no claim label is: QA phase
// 5 L10); and, for the operator's other pages, her viewport
// as a dotted rect and her cursor as a dot. Screen space, above the drawing, never takes a pointer
// event, and never part of the display list, so it reaches no still, export or agent picture.
// Every string is React text: an agent's intent can never become markup.
import React, { useMemo } from "react";
import { MAX_HALOS, MAX_INTENT, clipText } from "./presence.js";

const PAD_PX = 6;

const inter = (a, b) => a[0] < b[2] && a[2] > b[0] && a[1] < b[3] && a[3] > b[1];

function unionOf(boxes) {
  const list = boxes.filter((b) => Array.isArray(b) && b.length === 4 && b.every(Number.isFinite));
  if (!list.length) return null;
  return [Math.min(...list.map((b) => b[0])), Math.min(...list.map((b) => b[1])), Math.max(...list.map((b) => b[2])), Math.max(...list.map((b) => b[3]))];
}

/**
 * haloShapes({members, operators, camera, viewport, bboxOf, chipOf}) -> {halos, operators}
 *   halos: [{name, x, y, w, h, dashed, opacity, stroke, initials, fg, label}] in screen px, culled
 *   against the viewport and capped at MAX_HALOS (the newest win: members come newest first).
 *   bboxOf(id) -> [x0, y0, x1, y1] | null (the display list's entry bbox); chipOf(name) -> {bg, fg, initials}.
 */
export function haloShapes({ members = [], operators = [], camera, viewport, bboxOf = () => null, chipOf = () => ({ bg: "#888888", fg: "#ffffff", initials: "?" }) }) {
  const out = { halos: [], operators: [] };
  if (!camera || !viewport || !(viewport.w > 0) || !(viewport.h > 0)) return out;
  const s = camera.scale;
  const screenRect = (r) => [(r[0] - camera.x) * s, (r[1] - camera.y) * s, (r[2] - camera.x) * s, (r[3] - camera.y) * s];
  const view = [0, 0, viewport.w, viewport.h];
  for (const m of members) {
    if (out.halos.length >= MAX_HALOS) break;
    const world = m.region || unionOf((m.ids || []).map((id) => bboxOf(id)));
    if (!world) continue;
    const r = screenRect(world);
    const box = [r[0] - PAD_PX, r[1] - PAD_PX, r[2] + PAD_PX, r[3] + PAD_PX];
    if (!inter(box, view)) continue;
    const chip = chipOf(m.name) || {};
    const intent = m.intent ? ` · ${clipText(m.intent, MAX_INTENT)}` : "";
    out.halos.push({
      name: m.name,
      x: box[0],
      y: box[1],
      w: box[2] - box[0],
      h: box[3] - box[1],
      dashed: m.status === "waiting" || m.status === "blocked",
      opacity: m.opacity,
      stroke: chip.bg || "#888888",
      fg: chip.fg || "#ffffff",
      initials: chip.initials || m.name.slice(0, 2).toUpperCase(),
      label: `${m.name} · ${m.status}${intent}`,
      status: m.status,
    });
  }
  for (const o of operators) {
    const vp = o.viewport ? screenRect(o.viewport) : null;
    const cursor = o.cursor ? [(o.cursor[0] - camera.x) * s, (o.cursor[1] - camera.y) * s] : null;
    const shownVp = vp && inter(vp, view) ? { x: vp[0], y: vp[1], w: vp[2] - vp[0], h: vp[3] - vp[1] } : null;
    const shownCursor = cursor && cursor[0] >= 0 && cursor[1] >= 0 && cursor[0] <= viewport.w && cursor[1] <= viewport.h ? cursor : null;
    if (shownVp || shownCursor) out.operators.push({ page: o.page, viewport: shownVp, cursor: shownCursor, opacity: o.opacity });
  }
  return out;
}

export default function Halos({ presence, camera, viewport, bboxOf, chipOf, theme = "light", neutral = "#8b8d98", layerRef = null }) {
  const shapes = useMemo(
    () => haloShapes({ members: presence ? presence.members : [], operators: presence ? presence.operators : [], camera, viewport, bboxOf, chipOf }),
    [presence, camera, viewport, bboxOf, chipOf],
  );
  const fillAlpha = theme === "dark" ? 0.1 : 0.06;
  return (
    <div ref={layerRef} className="cv2-halos" data-halos={shapes.halos.length} aria-hidden="true">
      <svg className="cv2-halos-svg" width="100%" height="100%">
        {shapes.halos.map((h) => (
          <rect
            key={`halo:${h.name}`}
            className="cv2-halo"
            data-name={h.name}
            data-status={h.status}
            x={h.x}
            y={h.y}
            width={Math.max(0, h.w)}
            height={Math.max(0, h.h)}
            rx={10}
            fill={h.stroke}
            fillOpacity={fillAlpha}
            stroke={h.stroke}
            strokeWidth={2}
            strokeDasharray={h.dashed ? "6 4" : undefined}
            opacity={h.opacity}
          />
        ))}
        {shapes.operators.map((o) =>
          o.viewport ? (
            <rect key={`vp:${o.page}`} className="cv2-operator-view" x={o.viewport.x} y={o.viewport.y} width={o.viewport.w} height={o.viewport.h} fill="none" stroke={neutral} strokeWidth={1.5} strokeDasharray="2 4" opacity={o.opacity} />
          ) : null,
        )}
        {shapes.operators.map((o) =>
          o.cursor ? <circle key={`cur:${o.page}`} className="cv2-operator-cursor" cx={o.cursor[0]} cy={o.cursor[1]} r={4} fill={neutral} opacity={o.opacity} /> : null,
        )}
      </svg>
      {shapes.halos.map((h) => (
        <div key={`pill:${h.name}`} className="cv2-halo-pill" style={{ left: h.x, top: h.y + h.h + 2, opacity: h.opacity, borderColor: h.stroke }} title={h.label}>
          <span className="cv2-halo-chip" style={{ background: h.stroke, color: h.fg }}>
            {h.initials}
          </span>
          <span className="cv2-halo-text">{h.label}</span>
        </div>
      ))}
      {shapes.operators.map((o) =>
        o.cursor ? (
          <div key={`curl:${o.page}`} className="cv2-operator-label" style={{ left: o.cursor[0] + 8, top: o.cursor[1] + 6, opacity: o.opacity }}>
            operator
          </div>
        ) : null,
      )}
    </div>
  );
}
