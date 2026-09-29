// The presence layer (canvas-v2-phase5.md 12.2): a halo per agent at work, in its chip colour, with
// a pill naming it and what it says it is doing; and, for the operator's other pages, her viewport
// as a dotted rect and her cursor as a dot with a label. Pills and the cursor label never cover a
// label the board draws or each other (QA phase 5 L10, labels.js): a pill goes under its halo, else
// above it, else inside it, else further out; when no place is clear it shrinks to its chip, and
// then (like a cursor label with no clear place) it is left out. Screen space, above the drawing,
// never takes a pointer event, and never part of the display list, so it reaches no still, export
// or agent picture. Every string is React text: an agent's intent can never become markup.
import React, { useMemo } from "react";
import { CHIP_W, CURSOR_LABEL, PILL_H, labelBoxes, placeLabel, pillWidth, textLines } from "./labels.js";
import { MAX_HALOS, MAX_INTENT, clipText } from "./presence.js";

const PAD_PX = 6;

const inter = (a, b) => a[0] < b[2] && a[2] > b[0] && a[1] < b[3] && a[3] > b[1];

function unionOf(boxes) {
  const list = boxes.filter((b) => Array.isArray(b) && b.length === 4 && b.every(Number.isFinite));
  if (!list.length) return null;
  return [Math.min(...list.map((b) => b[0])), Math.min(...list.map((b) => b[1])), Math.max(...list.map((b) => b[2])), Math.max(...list.map((b) => b[3]))];
}

// Where a pill of `w` may go around a halo `h`, best first.
function pillSpots(h, w) {
  const below = h.y + h.h + 2;
  const above = h.y - 2 - PILL_H;
  const right = h.x + h.w - w;
  const spots = [[h.x, below], [right, below], [h.x, above], [right, above], [h.x + 6, h.y + h.h - PILL_H - 6], [h.x + 6, h.y + 6]];
  for (let k = 1; k <= 3; k += 1) spots.push([h.x, below + k * (PILL_H + 2)], [h.x, above - k * (PILL_H + 2)]);
  return spots;
}

// Where the cursor label may go around the cursor `c`, best first.
function cursorSpots(c) {
  const [w, h] = CURSOR_LABEL;
  const spots = [[c[0] + 8, c[1] + 6], [c[0] + 8, c[1] - 6 - h], [c[0] - 8 - w, c[1] + 6], [c[0] - 8 - w, c[1] - 6 - h]];
  for (let k = 1; k <= 2; k += 1) spots.push([c[0] + 8, c[1] + 6 + k * (h + 2)], [c[0] + 8, c[1] - 6 - h - k * (h + 2)]);
  return spots;
}

/**
 * haloShapes({members, operators, camera, viewport, bboxOf, chipOf, labels}) -> {halos, operators}
 *   halos: [{name, x, y, w, h, dashed, opacity, stroke, initials, fg, label, pill}] in screen px,
 *   culled against the viewport and capped at MAX_HALOS (the newest win: members come newest first).
 *   pill: {x, y, compact} (compact: the chip alone) or null (no clear place).
 *   operators: [{page, viewport, cursor, label, opacity}], label {x, y} or null.
 *   bboxOf(id) -> [x0, y0, x1, y1] | null (the display list's entry bbox); chipOf(name) -> {bg, fg, initials};
 *   labels: the screen boxes of the board's own labels (labelBoxes), which nothing here covers.
 */
export function haloShapes({ members = [], operators = [], camera, viewport, bboxOf = () => null, chipOf = () => ({ bg: "#888888", fg: "#ffffff", initials: "?" }), labels = [] }) {
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
  // Pills in order (the newest first), each clear of the board's labels and of the pills before it.
  const taken = [...labels];
  for (const h of out.halos) {
    const w = pillWidth(h.label);
    let spot = placeLabel(pillSpots(h, w), [w, PILL_H], taken, viewport);
    let size = [w, PILL_H];
    if (!spot) {
      size = [CHIP_W, PILL_H];
      spot = placeLabel(pillSpots(h, CHIP_W), size, taken, viewport);
    }
    h.pill = spot ? { x: spot.x, y: spot.y, compact: size[0] === CHIP_W } : null;
    if (spot) taken.push([spot.x, spot.y, spot.x + size[0], spot.y + size[1]]);
  }
  for (const o of operators) {
    const vp = o.viewport ? screenRect(o.viewport) : null;
    const cursor = o.cursor ? [(o.cursor[0] - camera.x) * s, (o.cursor[1] - camera.y) * s] : null;
    const shownVp = vp && inter(vp, view) ? { x: vp[0], y: vp[1], w: vp[2] - vp[0], h: vp[3] - vp[1] } : null;
    const shownCursor = cursor && cursor[0] >= 0 && cursor[1] >= 0 && cursor[0] <= viewport.w && cursor[1] <= viewport.h ? cursor : null;
    let label = null;
    if (shownCursor) {
      label = placeLabel(cursorSpots(shownCursor), CURSOR_LABEL, taken, viewport);
      if (label) taken.push([label.x, label.y, label.x + CURSOR_LABEL[0], label.y + CURSOR_LABEL[1]]);
    }
    if (shownVp || shownCursor) out.operators.push({ page: o.page, viewport: shownVp, cursor: shownCursor, label, opacity: o.opacity });
  }
  return out;
}

export default function Halos({ presence, camera, viewport, bboxOf, chipOf, dl = null, theme = "light", neutral = "#8b8d98", layerRef = null }) {
  const labels = useMemo(() => labelBoxes(textLines(dl), camera, viewport), [dl, camera, viewport]);
  const shapes = useMemo(
    () => haloShapes({ members: presence ? presence.members : [], operators: presence ? presence.operators : [], camera, viewport, bboxOf, chipOf, labels }),
    [presence, camera, viewport, bboxOf, chipOf, labels],
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
      {shapes.halos.map((h) =>
        h.pill ? (
          <div
            key={`pill:${h.name}`}
            className={h.pill.compact ? "cv2-halo-pill cv2-halo-pill-compact" : "cv2-halo-pill"}
            data-name={h.name}
            style={{ left: h.pill.x, top: h.pill.y, opacity: h.opacity, borderColor: h.stroke }}
            title={h.label}
          >
            <span className="cv2-halo-chip" style={{ background: h.stroke, color: h.fg }}>
              {h.initials}
            </span>
            {h.pill.compact ? null : <span className="cv2-halo-text">{h.label}</span>}
          </div>
        ) : null,
      )}
      {shapes.operators.map((o) =>
        o.cursor && o.label ? (
          <div key={`curl:${o.page}`} className="cv2-operator-label" style={{ left: o.label.x, top: o.label.y, opacity: o.opacity }}>
            operator
          </div>
        ) : null,
      )}
    </div>
  );
}
