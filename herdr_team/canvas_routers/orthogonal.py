"""``orthogonal`` (alias ``elbow``): axis-aligned routes around obstacles, drawn with rounded elbows.

1. **Obstacles** are inflated by ``clearance``; the ends and their containers
   are never obstacles (the caller leaves them out).
2. **Ports**: an end leaves from the middle of a side, the one its ``side``
   names or the one facing the other end along the dominant axis. Several
   edges on one side spread at ``(k + 1) / (n + 1)`` along it, at least
   ``port_spacing`` apart. Each end first runs straight out of its side for
   ``clearance / 2`` (the stub; ``HEAD_ROOM`` more at ``b``, where the head is)
   before it may turn.
3. **Grid**: a sparse visibility grid (``_grid``) over a window around the
   stubs and waypoints (``4 x clearance`` past them), grown once when nothing
   is found.
4. **Search**: A* over ``(point, heading)`` (``_astar``): length, a turn costs
   ``2 x clearance``, running alongside a placed route costs three times its
   length. Each waypoint is its own leg.
5. **Budget**: 20,000 expanded states per leg; past that the straight route
   comes back with ``blocked``.
6. **Clean-up**: collinear points merged, zero-length pieces dropped.
7. **Label**: the centre of the longest inner piece, slid along it (or onto
   the next-longest piece) until the pill meets no obstacle.
8. **Self-loop**: out of ``e``, up and over, into ``n``, in five points.
"""
from __future__ import annotations

import math
from typing import List, Optional, Sequence, Tuple

from herdr_team.canvas_routers import END_GAP, Box, End, Point, Route, RouteRequest, Router, anchor, facing, straight_points
from herdr_team.canvas_routers import _astar
from herdr_team.canvas_routers._grid import Grid

#: Its place in the registration order.
ORDER = 30
#: The most states one leg's search expands.
BUDGET = 20_000
#: A self-loop reaches this far out of its node.
LOOP_OUT, LOOP_UP = 40.0, 20.0
NORMALS = {"n": (0.0, -1.0), "e": (1.0, 0.0), "s": (0.0, 1.0), "w": (-1.0, 0.0)}
#: How much longer the last straight piece is than the first: room for an arrow head before the elbow.
HEAD_ROOM = 12.0
HEADING = {"e": 0, "s": 1, "w": 2, "n": 3}


def side_of(end: End, toward: Point) -> str:
    return end.side if end.side is not None else facing(end.box, toward)


def _outline_offset(end: End, side: str, along: float) -> float:
    """How far inside the box's side the outline sits at ``along`` (0 for a rectangle)."""
    x0, y0, x1, y1 = end.box
    if end.outline == "rect":
        return 0.0
    horizontal_side = side in ("n", "s")
    half = (x1 - x0) / 2.0 if horizontal_side else (y1 - y0) / 2.0
    depth = (y1 - y0) / 2.0 if horizontal_side else (x1 - x0) / 2.0
    centre = (x0 + x1) / 2.0 if horizontal_side else (y0 + y1) / 2.0
    t = min(1.0, abs(along - centre) / max(half, 1e-6))
    if end.outline == "ellipse":
        return depth * (1.0 - math.sqrt(max(0.0, 1.0 - t * t)))
    return depth * t  # diamond


def port(end: End, side: str, slot: Tuple[int, int], spacing: float, along: Optional[float] = None) -> Point:
    """Where a route meets ``end`` on ``side``: its slot's spot along the side (or ``along``), on the outline, ``END_GAP`` out."""
    x0, y0, x1, y1 = end.box
    k, n = slot
    horizontal_side = side in ("n", "s")
    lo, hi = (x0, x1) if horizontal_side else (y0, y1)
    length = hi - lo
    step = max(length / float(n + 1), spacing) if n > 1 else length / 2.0
    span = step * (n - 1)
    at = (lo + hi) / 2.0 - span / 2.0 + step * k if n > 1 else (lo + hi) / 2.0
    if along is not None:
        at = along
    at = min(max(at, lo + min(4.0, length / 2.0)), hi - min(4.0, length / 2.0))
    inset = _outline_offset(end, side, at)
    nx, ny = NORMALS[side]
    if horizontal_side:
        edge = y1 if side == "s" else y0
        return at, edge - ny * inset + ny * END_GAP
    edge = x1 if side == "e" else x0
    return edge - nx * inset + nx * END_GAP, at


#: Two ends that face each other and overlap by this much along their sides meet in one straight line.
ALIGN_MARGIN = 12.0
OPPOSITE = {("s", "n"), ("n", "s"), ("e", "w"), ("w", "e")}


def _facing_line(request: RouteRequest, side_a: Optional[str], side_b: Optional[str]) -> Optional[float]:
    """The coordinate along the sides where two facing ends, alone on their sides, can meet in one straight piece:
    as near ``a``'s middle as the overlap of the two sides allows (None when they do not overlap enough)."""
    a, b = request.a, request.b
    if request.via or a.is_point or b.is_point or (side_a, side_b) not in OPPOSITE or request.slot[1] > 1 or request.slot_b[1] > 1:
        return None
    horizontal = side_a in ("n", "s")
    alo, ahi = (a.box[0], a.box[2]) if horizontal else (a.box[1], a.box[3])
    blo, bhi = (b.box[0], b.box[2]) if horizontal else (b.box[1], b.box[3])
    lo, hi = max(alo, blo) + ALIGN_MARGIN, min(ahi, bhi) - ALIGN_MARGIN
    if lo > hi:
        return None
    at = min(max((alo + ahi) / 2.0, lo), hi)
    for end, elo, ehi in ((a, alo, ahi), (b, blo, bhi)):
        if end.outline != "rect" and abs(at - (elo + ehi) / 2.0) > (ehi - elo) / 8.0:
            return None
    return at


def _toward_via(end: End, side: Optional[str], slot: Tuple[int, int], via: Optional[Point]) -> Optional[float]:
    """An end alone on its side whose nearest waypoint lies in front of that side leaves straight toward it."""
    if via is None or side is None or end.is_point or slot[1] > 1:
        return None
    horizontal = side in ("n", "s")
    lo, hi = (end.box[0], end.box[2]) if horizontal else (end.box[1], end.box[3])
    at = via[0] if horizontal else via[1]
    if not lo + ALIGN_MARGIN <= at <= hi - ALIGN_MARGIN:
        return None
    if end.outline != "rect" and abs(at - (lo + hi) / 2.0) > (hi - lo) / 8.0:
        return None
    return at


def _self_loop(end: End) -> List[Point]:
    """Today's graph self-loop: out of ``e``, up and over, into ``n``."""
    x0, y0, x1, y1 = end.box
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    return [(x1 + END_GAP, cy), (x1 + LOOP_OUT, cy), (x1 + LOOP_OUT, y0 - LOOP_UP), (cx, y0 - LOOP_UP), (cx, y0 - END_GAP)]


def _clean(points: Sequence[Point]) -> List[Point]:
    out: List[Point] = []
    for p in points:
        p = (round(p[0], 2) + 0.0, round(p[1], 2) + 0.0)
        if out and abs(out[-1][0] - p[0]) < 1e-6 and abs(out[-1][1] - p[1]) < 1e-6:
            continue
        out.append(p)
    changed = True
    while changed and len(out) > 2:
        changed = False
        for i in range(1, len(out) - 1):
            a, b, c = out[i - 1], out[i], out[i + 1]
            if (abs(a[0] - b[0]) < 1e-6 and abs(b[0] - c[0]) < 1e-6) or (abs(a[1] - b[1]) < 1e-6 and abs(b[1] - c[1]) < 1e-6):
                del out[i]
                changed = True
                break
    return out


def _inflate(box: Box, by: float) -> Box:
    return box[0] - by, box[1] - by, box[2] + by, box[3] + by


def _inside(point: Point, box: Box) -> bool:
    return box[0] + 1e-6 < point[0] < box[2] - 1e-6 and box[1] + 1e-6 < point[1] < box[3] - 1e-6


def _window(points: Sequence[Point], margin: float) -> Box:
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin


def _leg(start: Point, start_heading: int, goal: Point, goal_heading: int, obstacles: Sequence[Box], clearance: float,
         others: Sequence[Sequence[Point]]) -> Optional[Tuple[List[Point], int]]:
    """One leg: a straight, L or Z shape when one is clear (most legs between a layout's hints are), else A*."""
    margin = 4 * clearance
    bend = 2 * clearance
    for grow in (1.0, 2.0):
        window = _window([start, goal], margin)
        if grow > 1.0:
            w, h = window[2] - window[0], window[3] - window[1]
            window = (window[0] - w / 2.0, window[1] - h / 2.0, window[2] + w / 2.0, window[3] + h / 2.0)
        near = [b for b in obstacles if b[0] < window[2] and window[0] < b[2] and b[1] < window[3] and window[1] < b[3]]
        alongside = _astar.Alongside([r for r in others if _meets_window(r, window)])
        if grow == 1.0:
            quick = _simple(start, start_heading, goal, goal_heading, near, bend, alongside)
            if quick is not None:
                return quick
        grid = Grid(window, near, (start[0], goal[0]), (start[1], goal[1]))
        found = _astar.search(grid, start, start_heading, goal, goal_heading, bend, alongside, BUDGET)
        if found is not None:
            points, heading, _cost, _expanded = found
            return points, heading
    return None


def _meets_window(route: Sequence[Point], window: Box) -> bool:
    xs = [p[0] for p in route]
    ys = [p[1] for p in route]
    return bool(route) and min(xs) <= window[2] and window[0] <= max(xs) and min(ys) <= window[3] and window[1] <= max(ys)


def _clear(a: Point, b: Point, obstacles: Sequence[Box]) -> bool:
    """Whether the axis-aligned piece from ``a`` to ``b`` stays out of every obstacle's inside."""
    x0, x1 = min(a[0], b[0]), max(a[0], b[0])
    y0, y1 = min(a[1], b[1]), max(a[1], b[1])
    for bx0, by0, bx1, by1 in obstacles:
        if x0 == x1:
            if bx0 + 1e-6 < x0 < bx1 - 1e-6 and y0 < by1 - 1e-6 and by0 + 1e-6 < y1:
                return False
        elif by0 + 1e-6 < y0 < by1 - 1e-6 and x0 < bx1 - 1e-6 and bx0 + 1e-6 < x1:
            return False
    return True


def _simple(start: Point, start_heading: int, goal: Point, goal_heading: int, obstacles: Sequence[Box], bend: float,
            alongside: _astar.Alongside) -> Optional[Tuple[List[Point], int]]:
    """The cheapest clear route of at most two bends (straight, L, or Z at a half or a quarter of the way), or None."""
    sx, sy = start
    gx, gy = goal
    shapes: List[List[Point]] = []
    if abs(sx - gx) < 1e-6 or abs(sy - gy) < 1e-6:
        shapes.append([start, goal])
    shapes.append([start, (gx, sy), goal])
    shapes.append([start, (sx, gy), goal])
    for t in (0.5, 0.25, 0.75):
        mx, my = sx + (gx - sx) * t, sy + (gy - sy) * t
        shapes.append([start, (mx, sy), (mx, gy), goal])
        shapes.append([start, (sx, my), (gx, my), goal])
    best: Optional[Tuple[float, List[Point], int]] = None
    for shape in shapes:
        points = [p for i, p in enumerate(shape) if i == 0 or abs(p[0] - shape[i - 1][0]) > 1e-6 or abs(p[1] - shape[i - 1][1]) > 1e-6]
        if len(points) < 2:
            continue
        headings = [_astar.heading_of(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:])]
        if start_heading >= 0 and headings[0] == (start_heading + 2) % 4:
            continue
        if goal_heading >= 0 and headings[-1] != goal_heading:
            continue
        if any((h + 2) % 4 == g for h, g in zip(headings, headings[1:])):
            continue
        if not all(_clear(a, b, obstacles) for a, b in zip(points, points[1:])):
            continue
        turns = sum(1 for h, g in zip(headings, headings[1:]) if h != g) + (1 if start_heading >= 0 and headings[0] != start_heading else 0)
        cost = math.fsum(abs(b[0] - a[0]) + abs(b[1] - a[1]) for a, b in zip(points, points[1:])) + bend * turns
        for a, b in zip(points, points[1:]):
            horizontal = abs(a[1] - b[1]) < 1e-6
            cost += _astar.ALONGSIDE_COST * alongside.overlap(horizontal, a[1] if horizontal else a[0], min(a[0], b[0]) if horizontal else min(a[1], b[1]),
                                                             max(a[0], b[0]) if horizontal else max(a[1], b[1]))
        if best is None or cost < best[0] - 1e-9:
            best = (cost, points, headings[-1])
    if best is None:
        return None
    return best[1], best[2]


def route(request: RouteRequest) -> Route:
    a, b = request.a, request.b
    clearance = request.clearance
    if a.id is not None and a.id == b.id and not a.is_point:
        points = _self_loop(a)
        return Route(points=points, label_at=_label(points, request, []) if request.label else None, corner=request.radius)
    first_toward = request.via[0] if request.via else anchor(b)
    last_toward = request.via[-1] if request.via else anchor(a)
    raw = [box for _id, box, _outline in request.obstacles]
    start_side = None if a.is_point else side_of(a, first_toward)
    end_side = None if b.is_point else side_of(b, last_toward)
    along = _facing_line(request, start_side, end_side)
    along_a = along if along is not None else _toward_via(a, start_side, request.slot, request.via[0] if request.via else None)
    along_b = along if along is not None else _toward_via(b, end_side, request.slot_b, request.via[-1] if request.via else None)
    p0 = anchor(a) if a.is_point else port(a, start_side, request.slot, request.port_spacing, along_a)  # type: ignore[arg-type]
    pn = anchor(b) if b.is_point else port(b, end_side, request.slot_b, request.port_spacing, along_b)  # type: ignore[arg-type]
    stub = clearance / 2.0
    # The end with the head runs straight a little longer, so the head never sits on an elbow.
    stub_b = stub + HEAD_ROOM
    if start_side is not None and end_side is not None and (start_side, end_side) in OPPOSITE:
        nx, ny = NORMALS[start_side]
        room = (pn[0] - p0[0]) * nx + (pn[1] - p0[1]) * ny  # how far b's port lies ahead of a's, out of a's side
        if 0.0 < room < stub + stub_b:
            # Ends closer than their two stubs (neighbours in a stack): stubs that met would cross and hook back
            # into the source (QA phase 2, F5). Facing ends in line meet in one straight piece; otherwise each
            # stub takes half the room, and the turn falls in the middle.
            if abs((pn[0] - p0[0]) * ny) + abs((pn[1] - p0[1]) * nx) < 0.5 and _clear(p0, pn, [_inflate(b, 0.0) for b in raw]):
                points = [p0, pn]
                return Route(points=points, label_at=_label(points, request, raw) if request.label else None, corner=request.radius)
            stub = stub_b = room / 2.0
    s0 = p0 if start_side is None else (p0[0] + NORMALS[start_side][0] * stub, p0[1] + NORMALS[start_side][1] * stub)
    sn = pn if end_side is None else (pn[0] + NORMALS[end_side][0] * stub_b, pn[1] + NORMALS[end_side][1] * stub_b)
    targets = [s0] + [(float(x), float(y)) for x, y in request.via] + [sn]
    # An obstacle a stub or waypoint sits in (a crowded spot) keeps only its own box, and none if even that holds it.
    obstacles: List[Box] = []
    for box in raw:
        grown = _inflate(box, clearance)
        if any(_inside(t, grown) for t in targets):
            grown = box if not any(_inside(t, box) for t in targets) else None  # type: ignore[assignment]
        if grown is not None:
            obstacles.append(grown)
    # A route never runs back through its own ends: their boxes, grown by less than the stub, block the search.
    for end, side in ((a, start_side), (b, end_side)):
        if side is not None:
            own = _inflate(end.box, stub - 1.0)  # both stubs end outside it
            if not any(_inside(t, own) for t in targets):
                obstacles.append(own)
    heading = HEADING[start_side] if start_side is not None else -1
    path: List[Point] = [p0, s0] if start_side is not None else [p0]
    for index in range(1, len(targets)):
        goal_heading = (HEADING[end_side] + 2) % 4 if index == len(targets) - 1 and end_side is not None else -1
        found = _leg(targets[index - 1], heading, targets[index], goal_heading, obstacles, clearance, request.others)
        if found is None:
            points = straight_points(request)
            return Route(points=points, blocked=True, label_at=None)
        leg, heading = found
        path.extend(leg[1:])
    if end_side is not None:
        path.append(pn)
    points = _clean(path)
    if len(points) < 2:
        points = straight_points(request)
    return Route(points=points, label_at=_label(points, request, raw) if request.label else None, corner=request.radius)


def _label(points: Sequence[Point], request: RouteRequest, obstacles: Sequence[Box]) -> Optional[Point]:
    """The pill's centre: the middle of the longest inner piece, slid along it until the pill is clear."""
    if request.label is None:
        return None
    w, h = request.label
    pieces = list(zip(points, points[1:]))
    inner = pieces[1:-1] if len(pieces) > 2 else pieces
    ranked = sorted(range(len(inner)), key=lambda i: (-(abs(inner[i][1][0] - inner[i][0][0]) + abs(inner[i][1][1] - inner[i][0][1])), i))

    def clear(cx: float, cy: float) -> bool:
        pill = (cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)
        return not any(pill[0] < o[2] and o[0] < pill[2] and pill[1] < o[3] and o[1] < pill[3] for o in obstacles)

    for index in ranked:
        (ax, ay), (bx, by) = inner[index]
        mx, my = (ax + bx) / 2.0, (ay + by) / 2.0
        length = abs(bx - ax) + abs(by - ay)
        ux, uy = ((bx - ax) / length, (by - ay) / length) if length else (0.0, 0.0)
        for step in range(0, 21):
            for sign in ((1,) if step == 0 else (1, -1)):
                t = sign * step * length / 40.0
                cx, cy = mx + ux * t, my + uy * t
                if clear(cx, cy):
                    return cx, cy
    (ax, ay), (bx, by) = inner[ranked[0]] if ranked else (points[0], points[-1])
    return (ax + bx) / 2.0, (ay + by) / 2.0


ROUTERS = (Router(name="orthogonal", route=route, aliases=("elbow",),
                  doc="axis-aligned pieces around obstacles, with rounded elbows and spread ports"),)
