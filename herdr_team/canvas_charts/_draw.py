"""The Python drawing of a chart (canvas v2 phase 3, D2): display-list primitives an agent's picture shows with no page.

Every built-in 2D chart type draws itself here as ``rect``, ``line``, ``path``
and ``text`` primitives into its slot's ``fallback`` (``drawn: true``), from the
same ``Frame`` the ECharts option follows, so an agent sees a real chart (its
axes, ticks, bars, lines and legend) even when no page was ever opened, and
the page shows the same drawing at once while ECharts loads.

Paints are token references (``chart.cat.3``, ``chart.gridline``), so one
drawing serves both themes. Axis and value labels carry ``lod`` [titles, null]
and ticks and gridlines [overview, null] (semantic zoom). A drawing holds at
most ``MAX_PRIMS`` primitives; past that the rest is left out and says so.

Pure, stdlib only.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team import canvas_display as D
from herdr_team import canvas_text
from herdr_team.canvas_charts import _frame as FR
from herdr_team.canvas_charts._frame import Frame

MAX_PRIMS = 1500
#: How far past its radius a drawn arc's control points reach (45-degree Béziers): a round mark leaves this much room.
ARC_OVERSHOOT = 1.035
INK = "chart.ink"
MUTED = "chart.muted"
AXIS = "chart.axis"
GRID = "chart.gridline"
PAPER = "chart.paper"
TEXT_LOD = list(D.LOD_BODY)
LINE_LOD = list(D.LOD_LABEL)


def _n(value: float) -> float:
    return float(round(float(value), 2))


class Pen:
    """Collects a drawing's primitives in world units, offset to the slot's corner, up to ``MAX_PRIMS``."""

    def __init__(self, ox: float, oy: float, cap: int = MAX_PRIMS) -> None:
        self.ox = float(ox)
        self.oy = float(oy)
        self.items: List[Dict[str, Any]] = []
        self.cap = cap
        self.dropped = 0

    def room(self) -> int:
        return self.cap - len(self.items)

    def add(self, prim: Dict[str, Any]) -> None:
        if len(self.items) >= self.cap:
            self.dropped += 1
            return
        self.items.append(prim)

    def rect(self, x: float, y: float, w: float, h: float, fill: Any, stroke: Any = None, sw: Optional[float] = None, r: float = 0.0,
             op: Optional[float] = None, lod: Optional[List[Any]] = None) -> None:
        if w <= 0 or h <= 0:
            return
        prim: Dict[str, Any] = {"k": "rect", "x": _n(self.ox + x), "y": _n(self.oy + y), "w": _n(w), "h": _n(h), "fill": fill}
        if stroke is not None:
            prim["stroke"] = stroke
            prim["sw"] = sw if sw is not None else 1
        if r:
            prim["r"] = _n(r)
        if op is not None:
            prim["op"] = op
        if lod is not None:
            prim["lod"] = lod
        self.add(prim)

    def line(self, points: Sequence[Sequence[float]], stroke: Any, sw: float = 1.0, dash: Optional[List[float]] = None,
             lod: Optional[List[Any]] = None, op: Optional[float] = None) -> None:
        prim: Dict[str, Any] = {"k": "line", "points": [[_n(self.ox + p[0]), _n(self.oy + p[1])] for p in points], "stroke": stroke, "sw": sw}
        if dash:
            prim["dash"] = dash
        if lod is not None:
            prim["lod"] = lod
        if op is not None:
            prim["op"] = op
        self.add(prim)

    def path(self, d: str, fill: Any = None, stroke: Any = None, sw: Optional[float] = None, op: Optional[float] = None,
             lod: Optional[List[Any]] = None) -> None:
        prim: Dict[str, Any] = {"k": "path", "d": d, "fill": fill}
        if stroke is not None:
            prim["stroke"] = stroke
            prim["sw"] = sw if sw is not None else 1
        if op is not None:
            prim["op"] = op
        if lod is not None:
            prim["lod"] = lod
        self.add(prim)

    def pt(self, x: float, y: float) -> str:
        return "{} {}".format(D.fmt(self.ox + x), D.fmt(self.oy + y))

    def text(self, t: str, x: float, y_mid: float, anchor: str = "start", fill: Any = MUTED, size: float = FR.FONT, weight: int = 400,
             lod: Optional[List[Any]] = TEXT_LOD, room: Optional[float] = None) -> None:
        """One line of text vertically centred on ``y_mid``, anchored at ``x``."""
        if not t:
            return
        lh = canvas_text.line_height(size)
        width = FR.text_w(t, size, weight)
        left = x if anchor == "start" else (x - width / 2.0 if anchor == "middle" else x - width)
        box = (self.ox + left, self.oy + y_mid - lh / 2.0, room if room is not None else width, lh)
        prim = D.text_prim([t], self.ox + x, self.oy + y_mid - lh / 2.0, size, {}, fill, anchor, box, weight)
        if lod is not None:
            prim["lod"] = lod
        self.add(prim)

    def rotated(self, t: str, x: float, y: float, degrees: float, fill: Any = MUTED, size: float = FR.FONT) -> None:
        """One line of text ending at ``(x, y)``, rotated ``degrees`` counter-clockwise (an axis label under its tick)."""
        if not t:
            return
        lh = canvas_text.line_height(size)
        width = FR.text_w(t, size)
        theta = -math.radians(degrees)
        c, s = math.cos(theta), math.sin(theta)
        inner = D.text_prim([t], 0, -lh / 2.0, size, {}, fill, "end", (-width, -lh / 2.0, width, lh), 400)
        self.add({"k": "group", "t": [round(c, 6), round(s, 6), round(-s, 6), round(c, 6), _n(self.ox + x), _n(self.oy + y)],
                  "items": [inner], "lod": TEXT_LOD})


# --------------------------------------------------------------------------
# axes and legend


def band_centre(index: int, count: int, start: float, length: float) -> float:
    step = length / max(1, count)
    return start + step * (index + 0.5)


def value_pos(axis: Mapping[str, Any], value: float, start: float, length: float, flip: bool) -> float:
    lo, hi = float(axis.get("min", 0.0)), float(axis.get("max", 1.0))
    t = 0.0 if hi == lo else (float(value) - lo) / (hi - lo)
    return start + (1.0 - t) * length if flip else start + t * length


def axes(pen: Pen, frame: Frame) -> None:
    """Gridlines, axis lines, ticks and labels of an x/y frame (both orientations), and the y axis title."""
    px, py, pw, ph = frame.plot
    x_axis, y_axis = frame.axes.get("x") or {}, frame.axes.get("y") or {}
    left = y_axis if y_axis.get("pos") == "left" else x_axis
    bottom = x_axis if x_axis.get("pos") == "bottom" else y_axis
    if left.get("kind") == "value":
        for tick in left.get("ticks") or []:
            y = value_pos(left, tick["v"], py, ph, True)
            pen.line([[px, y], [px + pw, y]], GRID, 1, lod=LINE_LOD)
            pen.text(tick["t"], px - FR.TICK - FR.GAP, y, "end")
    if bottom.get("kind") == "value":
        for tick in bottom.get("ticks") or []:
            x = value_pos(bottom, tick["v"], px, pw, False)
            if bottom.get("time"):
                pen.line([[x, py + ph], [x, py + ph + FR.TICK]], AXIS, 1, lod=LINE_LOD)
            else:
                pen.line([[x, py], [x, py + ph]], GRID, 1, lod=LINE_LOD)
            pen.text(tick["t"], _inside(tick["t"], x, frame), py + ph + FR.TICK + FR.GAP + FR.LH / 2.0, "middle")
    if left.get("kind") == "category":
        labels = left.get("labels") or []
        every = int(left.get("interval") or 0) + 1
        for item in labels:
            if int(item["v"]) % every:
                continue
            y = band_centre(int(item["v"]), len(labels), py, ph)
            pen.text(item["t"], px - FR.TICK - FR.GAP, y, "end")
        pen.line([[px, py], [px, py + ph]], AXIS, 1, lod=LINE_LOD)
    if bottom.get("kind") == "category":
        labels = bottom.get("labels") or []
        every = int(bottom.get("interval") or 0) + 1
        rotate = float(bottom.get("rotate") or 0)
        base = py + ph
        zero = _zero_line(left, py, ph)
        if zero is not None:
            base = zero
        for item in labels:
            index = int(item["v"])
            if index % every:
                continue
            x = band_centre(index, len(labels), px, pw)
            pen.line([[x, py + ph], [x, py + ph + FR.TICK]], AXIS, 1, lod=LINE_LOD)
            if rotate:
                pen.rotated(item["t"], x + (FR.LH * 0.25 if rotate == 90 else 0.0), py + ph + FR.TICK + FR.GAP, rotate)
            else:
                pen.text(item["t"], _inside(item["t"], x, frame), py + ph + FR.TICK + FR.GAP + FR.LH / 2.0, "middle")
        pen.line([[px, base], [px + pw, base]], AXIS, 1, lod=LINE_LOD)
    elif bottom.get("kind") == "value":
        pen.line([[px, py + ph], [px + pw, py + ph]], AXIS, 1, lod=LINE_LOD)
    title = left.get("title")
    if title:
        pen.text(str(title), px, py - FR.NAME_GAP - FR.LH / 2.0, "start", MUTED)


def _inside(text: str, x: float, frame: Frame) -> float:
    """A label's centre moved in so the label stays inside the slot (the first and last tick of a bottom axis)."""
    half = FR.text_w(text) / 2.0
    return min(max(x, half), max(half, float(frame.box[0]) - half))


def _zero_line(axis: Mapping[str, Any], start: float, length: float) -> Optional[float]:
    if axis.get("kind") != "value":
        return None
    lo, hi = float(axis.get("min", 0)), float(axis.get("max", 1))
    if lo < 0 < hi:
        return value_pos(axis, 0.0, start, length, True)
    return None


def legend(pen: Pen, frame: Frame) -> None:
    placed = frame.legend
    if not placed:
        return
    x0, y0 = float(placed["box"][0]), float(placed["box"][1])
    items = placed.get("items") or []
    for row_index, row in enumerate(placed.get("rows") or []):
        x = x0
        y = y0 + row_index * FR.LEGEND_ROW + FR.LEGEND_ROW / 2.0
        for index in row:
            item = items[index]
            pen.rect(x, y - FR.SWATCH / 2.0, FR.SWATCH, FR.SWATCH, item["color"], r=2)
            pen.text(item["name"], x + FR.SWATCH + FR.SWATCH_GAP, y, "start", INK, FR.LEGEND_FONT)
            x += float(item["w"]) + FR.ITEM_GAP


def value_labels(pen: Pen, labels: Sequence[Tuple[float, float, str, str]], frame: Optional[Frame] = None) -> int:
    """Value labels ``(x, y_mid, text, anchor)``, each left out when it would touch one already drawn (and moved inside the
    slot, when the frame is given); how many drew."""
    taken: List[Tuple[float, float, float, float]] = []
    drawn = 0
    for x, y, text, anchor in labels:
        width = FR.text_w(text)
        left = x if anchor == "start" else (x - width / 2.0 if anchor == "middle" else x - width)
        if frame is not None:
            shift = min(0.0, float(frame.box[0]) - (left + width)) or max(0.0, -left)
            x, left = x + shift, left + shift
            y = min(max(y, FR.LH / 2.0), float(frame.box[1]) - FR.LH / 2.0)
        box = (left - 2, y - FR.LH / 2.0, left + width + 2, y + FR.LH / 2.0)
        if any(box[0] < b[2] and b[0] < box[2] and box[1] < b[3] and b[1] < box[3] for b in taken):
            continue
        taken.append(box)
        pen.text(text, x, y, anchor, INK)
        drawn += 1
    return drawn


def polyline(pen: Pen, points: Sequence[Tuple[float, float]], stroke: str, sw: float = 2.0, smooth: bool = False) -> None:
    """A series line; ``smooth`` draws it as Catmull-Rom Béziers through the points."""
    pts = [p for p in points if p is not None]
    if len(pts) < 2:
        return
    pen.path(line_d(pen, pts, smooth), None, stroke, sw)


def line_d(pen: Pen, pts: Sequence[Tuple[float, float]], smooth: bool) -> str:
    parts = ["M " + pen.pt(*pts[0])]
    if smooth and len(pts) > 2:
        for i in range(len(pts) - 1):
            p0 = pts[i - 1] if i > 0 else pts[i]
            p1, p2 = pts[i], pts[i + 1]
            p3 = pts[i + 2] if i + 2 < len(pts) else p2
            c1 = (p1[0] + (p2[0] - p0[0]) / 6.0, p1[1] + (p2[1] - p0[1]) / 6.0)
            c2 = (p2[0] - (p3[0] - p1[0]) / 6.0, p2[1] - (p3[1] - p1[1]) / 6.0)
            parts.append("C {} {} {}".format(pen.pt(*c1), pen.pt(*c2), pen.pt(*p2)))
    else:
        parts += ["L " + pen.pt(*p) for p in pts[1:]]
    return " ".join(parts)


def area(pen: Pen, top: Sequence[Tuple[float, float]], bottom: Sequence[Tuple[float, float]], fill: str, op: float = 0.25,
         smooth: bool = False) -> None:
    """The band between ``top`` (left to right) and ``bottom`` (left to right)."""
    if len(top) < 2:
        return
    d = line_d(pen, list(top), smooth)
    back = list(reversed(bottom))
    d += " " + " ".join("L " + pen.pt(*p) for p in back) + " Z"
    pen.path(d, fill, None, None, op)


def arc_path(pen: Pen, cx: float, cy: float, r0: float, r1: float, a0: float, a1: float) -> str:
    """A ring sector (a pie slice when ``r0`` is 0) from angle ``a0`` to ``a1`` (radians, clockwise from 12 o'clock), as
    Béziers of at most 45 degrees each (their control points reach ``ARC_OVERSHOOT`` of the radius)."""
    def point(r: float, a: float) -> Tuple[float, float]:
        return cx + r * math.sin(a), cy - r * math.cos(a)

    def arc(r: float, start: float, end: float) -> List[str]:
        out = []
        # 45 degrees a curve at most: the control points stay within 3.5 % of the radius (bounds hold the drawing).
        steps = max(1, int(math.ceil(abs(end - start) / (math.pi / 4) - 1e-9)))
        delta = (end - start) / steps
        k = 4.0 / 3.0 * math.tan(delta / 4.0)
        a = start
        for _ in range(steps):
            b = a + delta
            p0, p3 = point(r, a), point(r, b)
            c1 = (p0[0] + k * r * math.cos(a), p0[1] + k * r * math.sin(a))
            c2 = (p3[0] - k * r * math.cos(b), p3[1] - k * r * math.sin(b))
            out.append("C {} {} {}".format(pen.pt(*c1), pen.pt(*c2), pen.pt(*p3)))
            a = b
        return out

    start = point(r1, a0)
    parts = ["M " + pen.pt(*start)] + arc(r1, a0, a1)
    if r0 > 0:
        parts.append("L " + pen.pt(*point(r0, a1)))
        parts += arc(r0, a1, a0)
    else:
        parts.append("L " + pen.pt(cx, cy))
    parts.append("Z")
    return " ".join(parts)


def annotation_x(pen: Pen, x: float, frame: Frame, text: str) -> None:
    px, py, pw, ph = frame.plot
    pen.line([[x, py], [x, py + ph]], INK, 1, dash=[4, 3], lod=LINE_LOD, op=0.7)
    if text:
        pen.text(FR.fit_text(text, 120), x + 4, py + FR.LH / 2.0, "start", INK)


def annotation_y(pen: Pen, y: float, frame: Frame, text: str) -> None:
    px, py, pw, ph = frame.plot
    pen.line([[px, y], [px + pw, y]], INK, 1, dash=[4, 3], lod=LINE_LOD, op=0.7)
    if text:
        pen.text(FR.fit_text(text, 160), px + pw - 2, y - FR.LH / 2.0 - 1, "end", INK)


def note(pen: Pen, frame: Frame, text: str) -> None:
    """A muted line in the middle of the plot (an empty chart, a drawing that stops early)."""
    px, py, pw, ph = frame.plot
    pen.text(FR.fit_text(text, pw), px + pw / 2.0, py + ph / 2.0, "middle", MUTED)
