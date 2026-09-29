// The presence layer (canvas-v2-phase5.md 12.2): a halo per agent at work, in its chip colour, with
// a pill naming it and what it says it is doing; and, for the operator's other pages, her viewport
// as a dotted rect and her cursor as a dot with a label.
//
// Pills and the cursor label never cover the board's work (QA phase 5 L10 for its labels; the canvas
// v2 demo, V3, for its ink: a chip lay across the middle of a diagram, over nodes and edges). The
// obstacles are therefore every label the board draws *and* the box of every content entry, plus the
// pills already placed. A pill goes under its halo, else above it, else inside it, else further out;
// then in the nearest clear margin outside the whole drawing, with a leader back to its halo; then
// it shrinks to its chip; and if nothing is clear it is left out, like a cursor label with no place.
// The halo rectangle itself is unchanged: it is an outline round the work and is meant to be seen.
//
// Screen space, above the drawing, never takes a pointer event, and never part of the display list,
// so it reaches no still, export or agent picture. Every string is React text: an agent's intent can
// never become markup.
import React, { useMemo } from "react";
import { CHIP_W, CURSOR_LABEL, PILL_H, entryBoxes, labelBoxes, placeLabel, pillWidth, textLines } from "./labels.js";
import { MAX_HALOS, MAX_INTENT, clipText } from "./presence.js";

const PAD_PX = 6;

const inter = (a, b) => a[0] < b[2] && a[2] > b[0] && a[1] < b[3] && a[3] > b[1];
const clamp = (v, lo, hi) => (hi < lo ? v : Math.min(Math.max(v, lo), hi));

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

// Screen px kept between a pill in the margin and the drawing it stands clear of.
const MARGIN_PX = 8;

// Where a pill of `w` may go outside the whole drawing `union`, nearest the halo `h` first: along the
// halo's own row or column, so the leader back to it is short and the eye follows it.
function marginSpots(h, w, union, viewport) {
  if (!union) return [];
  const cx = h.x + h.w / 2;
  const cy = h.y + h.h / 2;
  const spots = [
    [clamp(h.x, 0, viewport.w - w), union[3] + MARGIN_PX],
    [clamp(h.x, 0, viewport.w - w), union[1] - MARGIN_PX - PILL_H],
    [union[2] + MARGIN_PX, clamp(cy - PILL_H / 2, 0, viewport.h - PILL_H)],
    [union[0] - MARGIN_PX - w, clamp(cy - PILL_H / 2, 0, viewport.h - PILL_H)],
  ];
  return spots.sort((a, b) => Math.hypot(a[0] - cx, a[1] - cy) - Math.hypot(b[0] - cx, b[1] - cy));
}

// A 1 px leader from a pill in the margin to the nearest point of its halo, so the pill still says
// whose it is. From the pill's own nearest edge, never across the pill.
function leaderTo(spot, w, h) {
  const from = [clamp(h.x + h.w / 2, spot[0], spot[0] + w), clamp(h.y + h.h / 2, spot[1], spot[1] + PILL_H)];
  return [from[0], from[1], clamp(from[0], h.x, h.x + h.w), clamp(from[1], h.y, h.y + h.h)];
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
export function haloShapes({ members = [], operators = [], camera, viewport, bboxOf = () => null, chipOf = () => ({ bg: "#888888", fg: "#ffffff", initials: "?" }), labels = [], ink = [] }) {
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
  // Pills in order (the newest first), each clear of the board's labels and ink and of the pills before it.
  const taken = [...labels, ...ink];
  const union = unionOf(ink);
  for (const h of out.halos) {
    const w = pillWidth(h.label);
    let leader = null;
    let spot = placeLabel(pillSpots(h, w), [w, PILL_H], taken, viewport);
    let size = [w, PILL_H];
    if (!spot) {
      const found = placeLabel(marginSpots(h, w, union, viewport), size, taken, viewport);
      if (found) {
        spot = found;
        leader = leaderTo([found.x, found.y], w, h);
      }
    }
    if (!spot) {
      size = [CHIP_W, PILL_H];
      spot = placeLabel(pillSpots(h, CHIP_W), size, taken, viewport);
    }
    h.pill = spot ? { x: spot.x, y: spot.y, compact: size[0] === CHIP_W, leader } : null;
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
  const ink = useMemo(() => entryBoxes(dl, camera, viewport), [dl, camera, viewport]);
  const shapes = useMemo(
    () => haloShapes({ members: presence ? presence.members : [], operators: presence ? presence.operators : [], camera, viewport, bboxOf, chipOf, labels, ink }),
    [presence, camera, viewport, bboxOf, chipOf, labels, ink],
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
        {shapes.halos.map((h) =>
          h.pill && h.pill.leader ? (
            <line
              key={`lead:${h.name}`}
              className="cv2-halo-leader"
              x1={h.pill.leader[0]}
              y1={h.pill.leader[1]}
              x2={h.pill.leader[2]}
              y2={h.pill.leader[3]}
              stroke={h.stroke}
              strokeWidth={1}
              opacity={h.opacity}
            />
          ) : null,
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
