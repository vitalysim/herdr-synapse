"""The orthogonal router's sparse visibility grid: candidate lines, and which points and pieces obstacles block.

The candidate lines are the inflated obstacles' edges, the midlines between
neighbouring edges (so a route can take the middle of a corridor; left out in
a crowded window), and the extra coordinates a leg needs (its ends and stubs).
Because every obstacle edge is a line, a piece between two neighbouring lines
is either wholly inside an obstacle's span or wholly outside it, so blocking is
decided per cell.

Pure.
"""
from __future__ import annotations

import bisect
from typing import Dict, List, Sequence, Tuple

Box = Tuple[float, float, float, float]


#: Past this many obstacles in a window, corridors are not given midlines (the grid would grow four times for little gain).
MIDLINES_UP_TO = 40


def _lines(values: Sequence[float], lo: float, hi: float, midlines: bool = True) -> List[float]:
    found = sorted({round(v, 3) for v in values if lo - 1e-9 <= v <= hi + 1e-9} | {round(lo, 3), round(hi, 3)})
    out = list(found)
    if midlines:
        for a, b in zip(found, found[1:]):
            if b - a > 2.0:
                out.append(round((a + b) / 2.0, 3))
    return sorted(set(out))


class Grid:
    """Lines inside ``window`` and the obstacles (already inflated) that meet it."""

    def __init__(self, window: Box, obstacles: Sequence[Box], extra_x: Sequence[float], extra_y: Sequence[float],
                 midlines: bool = True) -> None:
        x0, y0, x1, y1 = window
        self.boxes = [b for b in obstacles if b[0] < x1 and x0 < b[2] and b[1] < y1 and y0 < b[3]]
        midlines = midlines and len(self.boxes) <= MIDLINES_UP_TO
        self.xs = _lines([v for b in self.boxes for v in (b[0], b[2])] + list(extra_x), x0, x1, midlines)
        self.ys = _lines([v for b in self.boxes for v in (b[1], b[3])] + list(extra_y), y0, y1, midlines)
        # Per cell between neighbouring lines: the obstacles whose span covers it.
        self.x_cells: List[List[int]] = [[] for _ in range(max(0, len(self.xs) - 1))]
        self.y_cells: List[List[int]] = [[] for _ in range(max(0, len(self.ys) - 1))]
        for index, (bx0, by0, bx1, by1) in enumerate(self.boxes):
            for i in range(bisect.bisect_left(self.xs, bx0 - 1e-6), bisect.bisect_left(self.xs, bx1 - 1e-6)):
                if 0 <= i < len(self.x_cells) and bx0 - 1e-6 <= self.xs[i] and self.xs[i + 1] <= bx1 + 1e-6:
                    self.x_cells[i].append(index)
            for j in range(bisect.bisect_left(self.ys, by0 - 1e-6), bisect.bisect_left(self.ys, by1 - 1e-6)):
                if 0 <= j < len(self.y_cells) and by0 - 1e-6 <= self.ys[j] and self.ys[j + 1] <= by1 + 1e-6:
                    self.y_cells[j].append(index)
        # Per line: the obstacles whose span holds it strictly inside (the cells on both sides of it are theirs).
        self._x_in: List[frozenset] = [self._strictly(self.x_cells, i, 0) for i in range(len(self.xs))]
        self._y_in: List[frozenset] = [self._strictly(self.y_cells, j, 1) for j in range(len(self.ys))]
        self._point: Dict[Tuple[int, int], bool] = {}
        self._h: Dict[Tuple[int, int], bool] = {}
        self._v: Dict[Tuple[int, int], bool] = {}

    def index_x(self, x: float) -> int:
        return bisect.bisect_left(self.xs, round(x, 3) - 1e-6)

    def index_y(self, y: float) -> int:
        return bisect.bisect_left(self.ys, round(y, 3) - 1e-6)

    def _strictly(self, cells: List[List[int]], index: int, axis: int) -> frozenset:
        """The obstacles whose span holds line ``index`` strictly inside. Inside the grid that is the obstacles that
        cover the cells on both sides of it; on the first or last line, those reaching past it."""
        lines = self.xs if axis == 0 else self.ys
        if 0 < index < len(lines) - 1:
            return frozenset(cells[index - 1]).intersection(cells[index])
        at = lines[index]
        return frozenset(k for k, b in enumerate(self.boxes) if b[axis] + 1e-6 < at < b[axis + 2] - 1e-6)

    def blocked_point(self, i: int, j: int) -> bool:
        """Whether the point is strictly inside an obstacle."""
        key = (i, j)
        found = self._point.get(key)
        if found is None:
            found = not self._x_in[i].isdisjoint(self._y_in[j])
            self._point[key] = found
        return found

    def blocked_h(self, i: int, j: int) -> bool:
        """Whether the piece from line ``i`` to line ``i + 1`` at y line ``j`` runs through an obstacle."""
        key = (i, j)
        found = self._h.get(key)
        if found is None:
            y = self.ys[j]
            found = any(self.boxes[k][1] + 1e-6 < y < self.boxes[k][3] - 1e-6 for k in self.x_cells[i])
            self._h[key] = found
        return found

    def blocked_v(self, i: int, j: int) -> bool:
        """Whether the piece from y line ``j`` to ``j + 1`` at x line ``i`` runs through an obstacle."""
        key = (i, j)
        found = self._v.get(key)
        if found is None:
            x = self.xs[i]
            found = any(self.boxes[k][0] + 1e-6 < x < self.boxes[k][2] - 1e-6 for k in self.y_cells[j])
            self._v[key] = found
        return found
