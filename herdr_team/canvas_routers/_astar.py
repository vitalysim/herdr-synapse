"""A* over the orthogonal grid, with states ``(point, heading)``.

The cost of a route is its length, plus ``bend`` for every turn, plus three
times the length it runs within 4 units alongside a route already placed
(``others``), so parallel edges spread out instead of stacking. The heuristic
is the Manhattan distance plus the turns still needed, which never
overestimates. Ties break on the larger ``g`` first (the deeper state: on a
grid, many routes cost the same, and this walks one of them instead of all),
then ``(x, y, heading)``, so the search is deterministic.

Pure.
"""
from __future__ import annotations

import heapq
from typing import Dict, List, Optional, Sequence, Tuple

from herdr_team.canvas_routers._grid import Grid

Point = Tuple[float, float]
#: Headings: 0 east (+x), 1 south (+y), 2 west, 3 north; -1 none yet.
STEPS = ((1, 0), (0, 1), (-1, 0), (0, -1))
#: A piece within this distance of a placed route runs alongside it.
NEAR = 4.0
ALONGSIDE_COST = 3.0


def heading_of(dx: float, dy: float) -> int:
    if abs(dx) >= abs(dy):
        return 0 if dx > 0 else 2
    return 1 if dy > 0 else 3


class Alongside:
    """The axis-aligned pieces of the routes already placed, bucketed by their coordinate."""

    def __init__(self, routes: Sequence[Sequence[Point]]) -> None:
        self._near: Dict[Tuple[bool, float], List[Tuple[float, float]]] = {}
        self.h: Dict[int, List[Tuple[float, float, float]]] = {}
        self.v: Dict[int, List[Tuple[float, float, float]]] = {}
        for route in routes:
            for (ax, ay), (bx, by) in zip(route, route[1:]):
                if abs(ay - by) < 1e-6 and abs(ax - bx) > 1e-6:
                    self.h.setdefault(int(ay // NEAR), []).append((ay, min(ax, bx), max(ax, bx)))
                elif abs(ax - bx) < 1e-6 and abs(ay - by) > 1e-6:
                    self.v.setdefault(int(ax // NEAR), []).append((ax, min(ay, by), max(ay, by)))

    def overlap(self, horizontal: bool, at: float, lo: float, hi: float) -> float:
        table = self.h if horizontal else self.v
        if not table:
            return 0.0
        key = (horizontal, at)
        near = self._near.get(key)
        if near is None:
            bucket = int(at // NEAR)
            near = [(s0, s1) for b in (bucket - 1, bucket, bucket + 1) for c, s0, s1 in table.get(b, ()) if abs(c - at) <= NEAR]
            self._near[key] = near
        total = 0.0
        for s0, s1 in near:
            if s1 > lo and s0 < hi:
                total += min(hi, s1) - max(lo, s0)
        return total


def _turns(heading: int, dx: float, dy: float) -> int:
    """The fewest turns from ``heading`` to reach a point ``(dx, dy)`` away."""
    if abs(dx) < 1e-9 and abs(dy) < 1e-9:
        return 0
    if heading < 0:
        return 0 if abs(dx) < 1e-9 or abs(dy) < 1e-9 else 1
    sx, sy = STEPS[heading]
    along = dx * sx + dy * sy
    if abs(dx) < 1e-9 or abs(dy) < 1e-9:
        if along > 1e-9:
            return 0
        perpendicular = (abs(dx) > 1e-9 and sx == 0) or (abs(dy) > 1e-9 and sy == 0)
        return 1 if perpendicular else 2
    return 1 if along > 1e-9 else 2


def search(grid: Grid, start: Point, start_heading: int, goal: Point, goal_heading: int, bend: float, alongside: Alongside,
           budget: int) -> Optional[Tuple[List[Point], int, float, int]]:
    """The cheapest route from ``start`` to ``goal`` on grid lines: ``(points, arrival heading, cost, expanded)``, or
    None when none exists within ``budget`` expanded states. ``goal_heading`` (or -1) is the heading the route must
    arrive with; arriving otherwise costs the turns it would take."""
    si, sj = grid.index_x(start[0]), grid.index_y(start[1])
    gi, gj = grid.index_x(goal[0]), grid.index_y(goal[1])
    if not (0 <= si < len(grid.xs) and 0 <= sj < len(grid.ys) and 0 <= gi < len(grid.xs) and 0 <= gj < len(grid.ys)):
        return None
    gx, gy = grid.xs[gi], grid.ys[gj]

    def h(i: int, j: int, heading: int) -> float:
        dx, dy = gx - grid.xs[i], gy - grid.ys[j]
        turns = _turns(heading, dx, dy)
        return abs(dx) + abs(dy) + bend * turns

    start_state = (si, sj, start_heading)
    best: Dict[Tuple[int, int, int], float] = {start_state: 0.0}
    came: Dict[Tuple[int, int, int], Tuple[int, int, int]] = {}
    heap: List[Tuple[float, float, float, float, int, int, int]] = [
        (h(si, sj, start_heading), -0.0, grid.xs[si], grid.ys[sj], start_heading, si, sj)]
    expanded = 0
    finish: Optional[Tuple[Tuple[int, int, int], float]] = None
    while heap:
        f, neg_g, _x, _y, heading, i, j = heapq.heappop(heap)
        g = -neg_g
        state = (i, j, heading)
        if g > best.get(state, float("inf")) + 1e-9:
            continue
        if finish is not None and f >= finish[1] - 1e-9:
            break
        if i == gi and j == gj:
            extra = 0.0
            if goal_heading >= 0 and heading != goal_heading:
                extra = bend * (2 if heading >= 0 and (heading + 2) % 4 == goal_heading else 1)
            if finish is None or g + extra < finish[1] - 1e-9:
                finish = (state, g + extra)
            continue
        expanded += 1
        if expanded > budget:
            break
        for step, (sx, sy) in enumerate(STEPS):
            if heading >= 0 and (heading + 2) % 4 == step:
                continue  # never straight back
            ni, nj = i + sx, j + sy
            if not (0 <= ni < len(grid.xs) and 0 <= nj < len(grid.ys)):
                continue
            if sx:
                if grid.blocked_h(min(i, ni), j):
                    continue
            elif grid.blocked_v(i, min(j, nj)):
                continue
            if grid.blocked_point(ni, nj) and not (ni == gi and nj == gj):
                continue
            length = abs(grid.xs[ni] - grid.xs[i]) + abs(grid.ys[nj] - grid.ys[j])
            cost = length
            if sx:
                cost += ALONGSIDE_COST * alongside.overlap(True, grid.ys[j], min(grid.xs[i], grid.xs[ni]), max(grid.xs[i], grid.xs[ni]))
            else:
                cost += ALONGSIDE_COST * alongside.overlap(False, grid.xs[i], min(grid.ys[j], grid.ys[nj]), max(grid.ys[j], grid.ys[nj]))
            if heading >= 0 and heading != step:
                cost += bend
            ng = g + cost
            nstate = (ni, nj, step)
            if ng < best.get(nstate, float("inf")) - 1e-9:
                best[nstate] = ng
                came[nstate] = state
                heapq.heappush(heap, (ng + h(ni, nj, step), -ng, grid.xs[ni], grid.ys[nj], step, ni, nj))
    if finish is None:
        return None
    state = finish[0]
    points: List[Point] = []
    while True:
        points.append((grid.xs[state[0]], grid.ys[state[1]]))
        if state == start_state:
            break
        state = came[state]
    points.reverse()
    return points, finish[0][2], finish[1], expanded
