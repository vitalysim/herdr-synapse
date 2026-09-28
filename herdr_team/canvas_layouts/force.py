"""``force``: Fruchterman-Reingold, then every box snapped to its own cell of a grid.

The simulation is seeded (``FORCE_SEED`` plus the node count), so the same
graph always draws the same picture. It runs ``iterations`` rounds (200 by
default) inside the classic W x L frame, with a little gravity so unconnected
nodes do not end on its edge. Then each node takes the free grid cell nearest
to where it settled, the most central first; a cell is as big as the largest
node plus ``gap``, so no two boxes meet.

Pins stay fixed through the simulation and take the cells their boxes cover.
An incremental run starts from the seeds and runs 50 cool rounds; when every
node is seeded and the seeds already keep ``gap`` apart, they are the result.
"""
from __future__ import annotations

import math
import random
from typing import Dict, List, Optional, Sequence, Set, Tuple

from herdr_team.canvas_layouts import LNode, Layout, LayoutError, LayoutRequest, LayoutResult, Point
from herdr_team.canvas_layouts import _util

#: Its place in the registration order.
ORDER = 30
FORCE_SEED = 20260926
ITERATIONS = 200
INCREMENTAL_ITERATIONS = 50
MAX_ITERATIONS = 2000
#: How far a seeded node may drift per round of an incremental run (it keeps its cell).
SETTLE_STEP = 2.0
#: Pull toward the frame's centre per unit of distance.
GRAVITY = 0.05
#: Above this many nodes, repulsion reaches only the nodes within ``2k`` (the grid variant); below, every pair.
GRID_FROM = 120


def force(request: LayoutRequest) -> LayoutResult:
    nodes = _util.nodes_in_order(request)
    iterations = request.options.get("iterations", ITERATIONS)
    if not isinstance(iterations, int) or isinstance(iterations, bool) or not 1 <= iterations <= MAX_ITERATIONS:
        raise LayoutError("options.iterations", "iterations is a whole number from 1 to {}".format(MAX_ITERATIONS))
    gap = request.gap
    if request.incremental and _util.all_seeded(request) and _apart(nodes, gap):
        return LayoutResult(positions={n.id: (n.pin or n.seed) for n in nodes}, notes=tuple(_util.pin_overlaps(nodes)))  # type: ignore[misc]
    count = len(nodes)
    cell_w = max(n.w for n in nodes) + gap
    cell_h = max(n.h for n in nodes) + gap
    if count == 1:
        only = nodes[0]
        return LayoutResult(positions={only.id: (only.pin or only.seed or (0.0, 0.0))})  # type: ignore[dict-item]
    seeded = request.incremental and any(n.seed is not None for n in nodes)
    rng = random.Random(FORCE_SEED + count)
    side = math.sqrt(count) * max(cell_w, cell_h)
    xs = [rng.uniform(0, side) for _ in nodes]
    ys = [rng.uniform(0, side) for _ in nodes]
    fixed = [n.pin is not None for n in nodes]
    # Seeds and pins come in as centres, in the simulation's frame: the pins' (or seeds') own positions.
    for i, n in enumerate(nodes):
        corner = n.pin if n.pin is not None else (n.seed if seeded else None)
        if corner is not None:
            xs[i], ys[i] = corner[0] + n.w / 2.0, corner[1] + n.h / 2.0
    index = {n.id: i for i, n in enumerate(nodes)}
    links = [(index[e.a], index[e.b]) for e in _util.edges_in_order(request) if e.a != e.b]
    # An incremental run: seeded nodes only settle (a few units a round); a new node starts among its seeded neighbours.
    settled = [seeded and n.seed is not None for n in nodes]
    if seeded:
        for i, n in enumerate(nodes):
            if n.seed is not None or n.pin is not None:
                continue
            near = [j for a, b in links for j in ((b,) if a == i else (a,) if b == i else ()) if settled[j] or fixed[j]]
            if near:
                xs[i] = math.fsum(xs[j] for j in near) / len(near) + rng.uniform(-cell_w / 2.0, cell_w / 2.0)
                ys[i] = math.fsum(ys[j] for j in near) / len(near) + rng.uniform(-cell_h / 2.0, cell_h / 2.0)
    k = math.sqrt(side * side / count)
    k2 = k * k
    cx = math.fsum(xs) / count
    cy = math.fsum(ys) / count
    start_temperature = side / 10.0
    temperature = start_temperature
    rounds = INCREMENTAL_ITERATIONS if seeded else iterations
    # A fresh run stays inside its W x L frame (the classic bound); pins and seeds set their own frame.
    bounded = not seeded and not any(fixed)
    near_only = count > GRID_FROM
    for iteration in range(rounds):
        disp_x = [0.0] * count
        disp_y = [0.0] * count
        if near_only:
            _repel_near(xs, ys, disp_x, disp_y, k, k2)
        for i in range(count):
            xi, yi = xs[i], ys[i]
            fx = fy = 0.0
            if not near_only:
                for j in range(i + 1, count):
                    dx = xi - xs[j]
                    dy = yi - ys[j]
                    d2 = dx * dx + dy * dy or 0.0001
                    push = k2 / d2
                    fx += dx * push
                    fy += dy * push
                    disp_x[j] -= dx * push
                    disp_y[j] -= dy * push
            disp_x[i] += fx + (cx - xi) * GRAVITY
            disp_y[i] += fy + (cy - yi) * GRAVITY
        for a, b in links:
            dx = xs[a] - xs[b]
            dy = ys[a] - ys[b]
            pull = math.sqrt(dx * dx + dy * dy) / k
            disp_x[a] -= dx * pull
            disp_y[a] -= dy * pull
            disp_x[b] += dx * pull
            disp_y[b] += dy * pull
        for i in range(count):
            if fixed[i]:
                continue
            dx, dy = disp_x[i], disp_y[i]
            length = math.sqrt(dx * dx + dy * dy)
            if length > 0:
                step = min(length, SETTLE_STEP if settled[i] else temperature) / length
                xs[i] = xs[i] + dx * step
                ys[i] = ys[i] + dy * step
                if bounded:
                    xs[i] = min(side, max(0.0, xs[i]))
                    ys[i] = min(side, max(0.0, ys[i]))
        temperature = max(start_temperature * (1 - (iteration + 1) / float(rounds)), 0.5)
    origin = (0.0, 0.0)
    if seeded:
        # The cells of an incremental run line up with the seeds' own, so a node that barely moved keeps its cell.
        first = next(n for n in nodes if n.seed is not None and n.pin is None) if any(n.seed is not None and n.pin is None for n in nodes) else None
        if first is not None:
            origin = ((first.seed[0] - (cell_w - first.w) / 2.0) % cell_w, (first.seed[1] - (cell_h - first.h) / 2.0) % cell_h)  # type: ignore[index]
    positions = _snap(nodes, xs, ys, cell_w, cell_h, gap, origin)
    if not any(fixed) and not seeded:
        lo_x = min(x for x, _y in positions.values())
        lo_y = min(y for _x, y in positions.values())
        positions = {n: (x - lo_x, y - lo_y) for n, (x, y) in positions.items()}
    return LayoutResult(positions=positions, notes=tuple(_util.pin_overlaps(nodes)))


def _repel_near(xs: List[float], ys: List[float], disp_x: List[float], disp_y: List[float], k: float, k2: float) -> None:
    """Fruchterman and Reingold's grid variant: repulsion only between nodes closer than ``2k``, found through a grid
    of ``2k`` cells, so a round costs about ``n`` rather than ``n^2`` (QA phase 2, F12)."""
    cell = 2.0 * k
    reach2 = cell * cell
    buckets: Dict[Tuple[int, int], List[int]] = {}
    for i in range(len(xs)):
        buckets.setdefault((int(math.floor(xs[i] / cell)), int(math.floor(ys[i] / cell))), []).append(i)
    for (bx, by), members in buckets.items():
        near: List[int] = []
        for ox in (-1, 0, 1):
            for oy in (-1, 0, 1):
                near.extend(buckets.get((bx + ox, by + oy), ()))
        for i in members:
            xi, yi = xs[i], ys[i]
            fx = fy = 0.0
            for j in near:
                if j <= i:
                    continue
                dx = xi - xs[j]
                dy = yi - ys[j]
                d2 = dx * dx + dy * dy or 0.0001
                if d2 > reach2:
                    continue
                push = k2 / d2
                fx += dx * push
                fy += dy * push
                disp_x[j] -= dx * push
                disp_y[j] -= dy * push
            disp_x[i] += fx
            disp_y[i] += fy


def _apart(nodes: Sequence[LNode], gap: float) -> bool:
    boxes = [_util.box_of(n.pin or n.seed, (n.w, n.h)) for n in nodes]  # type: ignore[arg-type]
    for i, a in enumerate(boxes):
        for b in boxes[i + 1:]:
            if _util.meets(a, b, gap):
                return False
    return True


def _snap(nodes: Sequence[LNode], xs: List[float], ys: List[float], cell_w: float, cell_h: float, gap: float,
          origin: Tuple[float, float] = (0.0, 0.0)) -> Dict[str, Point]:
    """Each unpinned node centred in the free cell nearest its centre, the most central first; pins take their cells.
    Cells start at ``origin``."""
    ox, oy = origin
    xs = [x - ox for x in xs]
    ys = [y - oy for y in ys]
    count = len(nodes)
    cx = math.fsum(xs) / count
    cy = math.fsum(ys) / count
    taken: Set[Tuple[int, int]] = set()
    out: Dict[str, Point] = {}
    for n in nodes:
        if n.pin is None:
            continue
        out[n.id] = (float(n.pin[0]), float(n.pin[1]))
        x0, y0 = n.pin[0] - ox - gap / 2.0, n.pin[1] - oy - gap / 2.0
        x1, y1 = n.pin[0] - ox + n.w + gap / 2.0, n.pin[1] - oy + n.h + gap / 2.0
        for c in range(int(math.floor(x0 / cell_w)), int(math.floor(x1 / cell_w)) + 1):
            for r in range(int(math.floor(y0 / cell_h)), int(math.floor(y1 / cell_h)) + 1):
                if c * cell_w < x1 and x0 < (c + 1) * cell_w and r * cell_h < y1 and y0 < (r + 1) * cell_h:
                    taken.add((c, r))
    order = sorted((i for i, n in enumerate(nodes) if n.pin is None), key=lambda i: ((xs[i] - cx) ** 2 + (ys[i] - cy) ** 2, i))
    for i in order:
        want_c, want_r = xs[i] / cell_w - 0.5, ys[i] / cell_h - 0.5
        col, row = int(round(want_c)), int(round(want_r))
        radius = 0
        best: Optional[Tuple[float, int, int]] = None
        while best is None:
            for c in range(col - radius, col + radius + 1):
                for r in range(row - radius, row + radius + 1):
                    if max(abs(c - col), abs(r - row)) != radius or (c, r) in taken:
                        continue
                    distance = ((c - want_c) * cell_w) ** 2 + ((r - want_r) * cell_h) ** 2
                    if best is None or (distance, c, r) < best:
                        best = (distance, c, r)
            radius += 1
        taken.add((best[1], best[2]))
        n = nodes[i]
        out[n.id] = (ox + best[1] * cell_w + (cell_w - n.w) / 2.0, oy + best[2] * cell_h + (cell_h - n.h) / 2.0)
    return out


LAYOUTS = (
    Layout(name="force", run=force, options=("iterations",), router="straight",
           doc="a force-directed drawing (seeded), snapped to a grid so no boxes meet"),
)
