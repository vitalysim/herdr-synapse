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
from typing import AbstractSet, Dict, List, Optional, Sequence, Tuple

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
    """The axis-aligned pieces of the routes already placed, bucketed by their coordinate.

    Built once for all the routes a request sees; ``only`` gives a view that counts just some of them (the ones that
    meet a leg's window), sharing the buckets, so a route of many legs does not bucket every placed route per leg."""

    def __init__(self, routes: Sequence[Sequence[Point]]) -> None:
        self._near: Dict[Tuple[bool, float], List[Tuple[float, float]]] = {}
        self._only: Optional[AbstractSet[int]] = None
        self.h: Dict[int, List[Tuple[float, float, float, int]]] = {}
        self.v: Dict[int, List[Tuple[float, float, float, int]]] = {}
        for index, route in enumerate(routes):
            for (ax, ay), (bx, by) in zip(route, route[1:]):
                if abs(ay - by) < 1e-6 and abs(ax - bx) > 1e-6:
                    self.h.setdefault(int(ay // NEAR), []).append((ay, min(ax, bx), max(ax, bx), index))
                elif abs(ax - bx) < 1e-6 and abs(ay - by) > 1e-6:
                    self.v.setdefault(int(ax // NEAR), []).append((ax, min(ay, by), max(ay, by), index))

    def only(self, routes: AbstractSet[int]) -> "Alongside":
        """A view that counts only the routes with these indexes (in the order they were given)."""
        view = Alongside(())
        view.h, view.v, view._only = self.h, self.v, routes
        return view

    def overlap(self, horizontal: bool, at: float, lo: float, hi: float) -> float:
        table = self.h if horizontal else self.v
        if not table:
            return 0.0
        key = (horizontal, at)
        near = self._near.get(key)
        if near is None:
            bucket = int(at // NEAR)
            only = self._only
            near = [(s0, s1) for b in (bucket - 1, bucket, bucket + 1) for c, s0, s1, r in table.get(b, ())
                    if abs(c - at) <= NEAR and (only is None or r in only)]
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
           budget: int, weight: float = 1.0) -> Tuple[Optional[Tuple[List[Point], int, float]], int]:
    """``(found, expanded)``: ``found`` is the cheapest route from ``start`` to ``goal`` on grid lines as
    ``(points, arrival heading, cost)``, or None when none exists within ``budget`` expanded states; ``expanded`` is
    how many states the search took either way. ``goal_heading`` (or -1) is the heading the route must arrive with;
    arriving otherwise costs the turns it would take.

    ``weight`` above 1 makes the search greedy (the heuristic counts ``weight`` times): it takes the first route that
    reaches the goal, at most ``weight`` times the cheapest, for far fewer states (a detour on a budget)."""
    si, sj = grid.index_x(start[0]), grid.index_y(start[1])
    gi, gj = grid.index_x(goal[0]), grid.index_y(goal[1])
    if not (0 <= si < len(grid.xs) and 0 <= sj < len(grid.ys) and 0 <= gi < len(grid.xs) and 0 <= gj < len(grid.ys)):
        return None, 0
    gx, gy = grid.xs[gi], grid.ys[gj]

    def h(i: int, j: int, heading: int) -> float:
        dx, dy = gx - grid.xs[i], gy - grid.ys[j]
        turns = _turns(heading, dx, dy)
        return (abs(dx) + abs(dy) + bend * turns) * weight

    start_state = (si, sj, start_heading)
    best: Dict[Tuple[int, int, int], float] = {start_state: 0.0}
    came: Dict[Tuple[int, int, int], Tuple[int, int, int]] = {}
    heap: List[Tuple[float, float, float, float, int, int, int]] = [
        (h(si, sj, start_heading), -0.0, grid.xs[si], grid.ys[sj], start_heading, si, sj)]
    expanded = 0
    finish: Optional[Tuple[Tuple[int, int, int], float]] = None
    xs, ys = grid.xs, grid.ys
    nx, ny = len(xs), len(ys)
    inf = float("inf")
    # Per piece (from a point, one step one way): None when blocked, else where it leads and what it costs before any
    # turn. A point is expanded once per heading, so each piece is worked out once instead of up to three times.
    pieces: Dict[Tuple[int, int, int], Optional[Tuple[int, int, float]]] = {}

    def piece(i: int, j: int, step: int) -> Optional[Tuple[int, int, float]]:
        sx, sy = STEPS[step]
        ni, nj = i + sx, j + sy
        if not (0 <= ni < nx and 0 <= nj < ny):
            return None
        if sx:
            if grid.blocked_h(min(i, ni), j):
                return None
        elif grid.blocked_v(i, min(j, nj)):
            return None
        if grid.blocked_point(ni, nj) and not (ni == gi and nj == gj):
            return None
        length = abs(xs[ni] - xs[i]) + abs(ys[nj] - ys[j])
        cost = length
        if sx:
            cost += ALONGSIDE_COST * alongside.overlap(True, ys[j], min(xs[i], xs[ni]), max(xs[i], xs[ni]))
        else:
            cost += ALONGSIDE_COST * alongside.overlap(False, xs[i], min(ys[j], ys[nj]), max(ys[j], ys[nj]))
        return ni, nj, cost

    while heap:
        f, neg_g, _x, _y, heading, i, j = heapq.heappop(heap)
        g = -neg_g
        state = (i, j, heading)
        if g > best.get(state, inf) + 1e-9:
            continue
        if finish is not None and f >= finish[1] - 1e-9:
            break
        if i == gi and j == gj:
            extra = 0.0
            if goal_heading >= 0 and heading != goal_heading:
                extra = bend * (2 if heading >= 0 and (heading + 2) % 4 == goal_heading else 1)
            if finish is None or g + extra < finish[1] - 1e-9:
                finish = (state, g + extra)
            if weight > 1.0:
                break
            continue
        expanded += 1
        if expanded > budget:
            break
        back = (heading + 2) % 4 if heading >= 0 else -1
        for step in range(4):
            if step == back:
                continue  # never straight back
            key = (i, j, step)
            found = pieces.get(key, False)
            if found is False:
                found = pieces[key] = piece(i, j, step)
            if found is None:
                continue
            ni, nj, cost = found
            if heading >= 0 and heading != step:
                cost += bend
            ng = g + cost
            nstate = (ni, nj, step)
            if ng < best.get(nstate, inf) - 1e-9:
                best[nstate] = ng
                came[nstate] = state
                heapq.heappush(heap, (ng + h(ni, nj, step), -ng, xs[ni], ys[nj], step, ni, nj))
    if finish is None:
        return None, expanded
    state = finish[0]
    points: List[Point] = []
    while True:
        points.append((grid.xs[state[0]], grid.ys[state[1]]))
        if state == start_state:
            break
        state = came[state]
    points.reverse()
    return (points, finish[0][2], finish[1]), expanded
