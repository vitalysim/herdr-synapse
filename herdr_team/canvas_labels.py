"""Where an arrow's label goes (canvas v2, QA R-3).

An arrow label is drawn in a pill (design spec 6.1). Centred on the arrow's
midpoint, the pill ran into the shapes the arrow joins whenever the arrow was
shorter than its label, and into other labels on a busy row. This module picks
the spot instead, as a pure function of the route, the pill's size and what is
already drawn:

1. the spot the label already has, while it is still on the route and clear
   (labels do not jump when something far away changes);
2. on the route, nearest its middle, clear of every solid mark's outline and
   every other label's pill by ``CLEARANCE``;
3. beside the route, a little further out each step, nearest the middle;
4. failing all of that, the spot that covers least.

``canvas`` stores the centre it chose on the arrow as ``label_at`` so the agent's
picture and the page draw the label in the same place, and makes room for a new
arrow's label (it moves the arrow's later end away) before it settles for 3 or 4.

Pure: no I/O, and no import of ``canvas`` (which imports this module).
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from herdr_team import canvas_kinds as _kinds

Box = Tuple[float, float, float, float]
Point = Tuple[float, float]
#: An obstacle: its box and its outline inside that box (``canvas_kinds.OUTLINES``).
Obstacle = Tuple[Box, str]

#: Units kept between a pill and any mark or other pill.
CLEARANCE = 4.0
#: Candidate spots along a route are this far apart, or closer on a long route (at most ``MAX_STOPS``).
STEP = 8.0
MAX_STOPS = 61
#: Spots beside the route: this many on each side, ``STEP`` apart, the first just clear of the line.
SIDE_STEPS = 12
#: A stored spot this close to the route still counts as on it (rounding of stored points).
ON_ROUTE = 1.5


# --------------------------------------------------------------------------
# geometry


def pill_box(center: Point, size: Tuple[float, float]) -> Box:
    cx, cy = center
    w, h = size
    return cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0


def _grown(box: Box, by: float) -> Box:
    return box[0] - by, box[1] - by, box[2] + by, box[3] + by


def hits(obstacle: Obstacle, rect: Box, clearance: float = CLEARANCE) -> bool:
    """Whether ``rect`` grown by ``clearance`` reaches into the obstacle's outline (``canvas_kinds.outline_meets``)."""
    box, outline = obstacle
    return _kinds.outline_meets(box, outline, _grown(rect, clearance))


def obstacle_of(el: Dict[str, Any]) -> Obstacle:
    """An element as an obstacle: its box and the outline its kind draws."""
    x, y = float(el.get("x") or 0), float(el.get("y") or 0)
    box = (x, y, x + max(1.0, float(el.get("w") or 1)), y + max(1.0, float(el.get("h") or 1)))
    return box, _kinds.outline(el)


def _segments(points: Sequence[Point]) -> List[Tuple[Point, Point, float]]:
    return [(a, b, math.hypot(b[0] - a[0], b[1] - a[1])) for a, b in zip(points, points[1:])]


def length(points: Sequence[Point]) -> float:
    return sum(seg[2] for seg in _segments(points))


def at(points: Sequence[Point], s: float) -> Tuple[float, float, float, float]:
    """The point ``s`` units along the polyline and the unit direction there: ``(x, y, ux, uy)``."""
    segments = [seg for seg in _segments(points) if seg[2] > 0]
    if not segments:
        x, y = points[0] if points else (0.0, 0.0)
        return float(x), float(y), 1.0, 0.0
    for a, b, seg_len in segments:
        if s <= seg_len:
            t = max(0.0, s) / seg_len
            return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t, (b[0] - a[0]) / seg_len, (b[1] - a[1]) / seg_len
        s -= seg_len
    a, b, seg_len = segments[-1]
    return b[0], b[1], (b[0] - a[0]) / seg_len, (b[1] - a[1]) / seg_len


def distance(points: Sequence[Point], x: float, y: float) -> float:
    """How far ``(x, y)`` is from the polyline."""
    best = float("inf")
    for a, b, seg_len in _segments(points):
        if seg_len == 0:
            best = min(best, math.hypot(x - a[0], y - a[1]))
            continue
        t = max(0.0, min(1.0, ((x - a[0]) * (b[0] - a[0]) + (y - a[1]) * (b[1] - a[1])) / (seg_len * seg_len)))
        best = min(best, math.hypot(x - (a[0] + (b[0] - a[0]) * t), y - (a[1] + (b[1] - a[1]) * t)))
    if best == float("inf") and points:
        best = math.hypot(x - points[0][0], y - points[0][1])
    return best


def extent(size: Tuple[float, float], ux: float, uy: float) -> Tuple[float, float]:
    """Half the pill's reach along the direction ``(ux, uy)`` and across it."""
    w, h = size
    return abs(ux) * w / 2.0 + abs(uy) * h / 2.0, abs(uy) * w / 2.0 + abs(ux) * h / 2.0


# --------------------------------------------------------------------------
# placement


def _stops(total: float) -> List[float]:
    """Distances along the route to try, nearest the middle first."""
    if total <= 0:
        return [0.0]
    step = max(STEP, total / (MAX_STOPS - 1))
    count = int(total // step)
    stops = [min(total, i * step) for i in range(count + 1)] + [total / 2.0]
    return sorted(set(round(s, 3) for s in stops), key=lambda s: (abs(s - total / 2.0), s))


def candidates(points: Sequence[Point], size: Tuple[float, float]) -> Iterator[Tuple[Point, int]]:
    """``((cx, cy), ring)``: on the route first (ring 0), then beside it, one ``STEP`` further out per ring."""
    total = length(points)
    stops = _stops(total)
    frames = [at(points, s) for s in stops]
    for x, y, _ux, _uy in frames:
        yield (x, y), 0
    for ring in range(SIDE_STEPS):
        for x, y, ux, uy in frames:
            _along, across = extent(size, ux, uy)
            reach = across + CLEARANCE + ring * STEP
            # Above (or left of) the line first, then below: the normal (-uy, ux) points down a rightward line.
            for sign in (-1.0, 1.0):
                yield (x - uy * reach * sign, y + ux * reach * sign), ring + 1


def clear(center: Point, size: Tuple[float, float], obstacles: Iterable[Obstacle], pills: Iterable[Box]) -> bool:
    rect = pill_box(center, size)
    return not any(hits(ob, rect) for ob in obstacles) and not any(hits((pill, "rect"), rect) for pill in pills)


def _cover(center: Point, size: Tuple[float, float], obstacles: Sequence[Obstacle], pills: Sequence[Box]) -> float:
    """How much of the pill lands on marks and pills (overlap area of the boxes of those it hits)."""
    rect = pill_box(center, size)
    total = 0.0
    for box, outline in list(obstacles) + [(pill, "rect") for pill in pills]:
        if hits((box, outline), rect, 0.0):
            total += max(0.0, min(rect[2], box[2]) - max(rect[0], box[0])) * max(0.0, min(rect[3], box[3]) - max(rect[1], box[1]))
    return total


def on_route(points: Sequence[Point], center: Point) -> bool:
    return distance(points, center[0], center[1]) <= ON_ROUTE


def place(points: Sequence[Point], size: Tuple[float, float], obstacles: Sequence[Obstacle], pills: Sequence[Box],
          current: Optional[Point] = None) -> Tuple[Point, int]:
    """The centre for a pill of ``size`` on the route ``points``, and its ring (0 on the route, -1 when nothing is clear).

    ``obstacles`` are the solid marks near the route (the arrow's own ends included: a label on a
    shape it joins covers that shape), ``pills`` the other labels already placed.
    """
    if len(points) < 2:
        return (float(points[0][0]), float(points[0][1])) if points else (0.0, 0.0), -1
    if current is not None and on_route(points, current) and clear(current, size, obstacles, pills):
        return (float(current[0]), float(current[1])), 0
    tried: List[Point] = []
    for center, ring in candidates(points, size):
        if clear(center, size, obstacles, pills):
            return center, ring
        tried.append(center)
    # Nothing is clear: the spot that covers least, the earliest (nearest the middle, on the route) on a tie.
    best = min(range(len(tried)), key=lambda i: (_cover(tried[i], size, obstacles, pills), i))
    return tried[best], -1


def blocked_on_route(points: Sequence[Point], size: Tuple[float, float], obstacles: Sequence[Obstacle], pills: Sequence[Box]) -> bool:
    """Whether no spot on the route itself is clear (the label would have to sit beside the line)."""
    return not any(clear(center, size, obstacles, pills) for center, ring in candidates(points, size) if ring == 0)


def room_at_end(points: Sequence[Point], size: Tuple[float, float], obstacles: Sequence[Obstacle], pills: Sequence[Box],
                at_start: bool) -> float:
    """How far one end of the route must move away along it for the label to sit on the line next to that end.

    ``obstacles`` leave out the mark at that end, which is what moves. Spots are tried on the route and on
    its extension past that end (where the moved mark was); the one reaching furthest inward that is
    clear of everything else decides: the end moves until the label there keeps its reach along the route
    plus ``CLEARANCE`` from the moved mark, with one ``STEP`` of slack for a round or pointed end. 0 when
    no spot on the route or its extension is clear (the line is blocked in its middle).
    """
    total = length(points)
    if total <= 0:
        return 0.0
    x, y, ux, uy = at(points, 0.0 if at_start else total)
    inward = (ux, uy) if at_start else (-ux, -uy)
    along, _across = extent(size, ux, uy)
    room = along + CLEARANCE + STEP
    best: Optional[float] = None
    d = -(2.0 * room + total)
    while d <= total:
        center = (x + inward[0] * d, y + inward[1] * d) if d < 0 else at(points, d if at_start else total - d)[:2]
        if clear(center, size, obstacles, pills):
            best = d
        d += 2.0
    return 0.0 if best is None else max(0.0, room - best)
