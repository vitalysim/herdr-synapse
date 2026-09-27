"""Graph layout for the canvas's ``graph`` operation and Mermaid flowcharts (0.21).

Pure functions over node ids and edges (contract section 6, ``graph``). A
layout returns the top-left corner of every fixed-size node box relative to
``(0, 0)``; ``canvas`` places the whole drawing and turns nodes and edges
into native shapes and bound arrows. Everything is deterministic: the force
layout is seeded, so the same graph always draws the same picture and a
test can pin it.

* ``layered``: longest-path layers from the sources (cycles broken at their
  back edges), then three barycentre sweeps to reduce crossings.
* ``radial``: breadth-first rings around the first source.
* ``force``: Fruchterman-Reingold, 200 iterations, kept inside its frame
  (the classic W x L bound, with a little gravity so unconnected nodes do
  not all end on its edge), then each box snapped to the nearest free cell
  of a grid, so none overlap and the drawing stays about the frame's size.
* ``grid``: row-major in input order.
"""
from __future__ import annotations

import math
import random
from typing import Dict, List, Optional, Sequence, Tuple

NODE_W, NODE_H = 160, 60
#: Between neighbours in one layer, and between layers.
SPACING = 60
LAYER_GAP = 100
LAYOUTS = ("layered", "radial", "force", "grid")
#: ``down`` and ``right`` are what agents ask for; ``up`` and ``left`` come from Mermaid's ``BT`` and ``RL``.
DIRECTIONS = ("down", "right", "up", "left")
SWEEPS = 3
FORCE_ITERATIONS = 200
FORCE_SEED = 20260926
#: Pull toward the frame's centre per unit of distance (the force layout's gravity).
GRAVITY = 0.05

Position = Tuple[float, float]


def layout(nodes: Sequence[str], edges: Sequence[Tuple[str, str]], algorithm: str = "layered", direction: str = "down",
           node_w: float = NODE_W, node_h: float = NODE_H) -> Dict[str, Position]:
    """Top-left corners of every node box, normalised so the smallest x and y are 0."""
    ids = list(dict.fromkeys(str(n) for n in nodes))
    known = set(ids)
    pairs = [(str(a), str(b)) for a, b in edges if str(a) in known and str(b) in known]
    if not ids:
        return {}
    if algorithm == "radial":
        raw = _radial(ids, pairs, node_w, node_h)
    elif algorithm == "force":
        raw = _force(ids, pairs, node_w, node_h)
    elif algorithm == "grid":
        raw = _grid(ids, node_w, node_h)
    else:
        raw = _layered(ids, pairs, direction, node_w, node_h)
    return _normalise(raw)


def _normalise(raw: Dict[str, Position]) -> Dict[str, Position]:
    min_x = min(x for x, _ in raw.values())
    min_y = min(y for _, y in raw.values())
    return {key: (round(x - min_x, 2), round(y - min_y, 2)) for key, (x, y) in raw.items()}


# --------------------------------------------------------------------------
# layered


def _acyclic(ids: List[str], pairs: List[Tuple[str, str]]) -> List[Tuple[str, str]]:
    """The edges without self-loops and without the back edges of a depth-first walk in input order."""
    succ: Dict[str, List[str]] = {n: [] for n in ids}
    for a, b in pairs:
        if a != b and b not in succ[a]:
            succ[a].append(b)
    state: Dict[str, int] = {}
    kept: List[Tuple[str, str]] = []
    for root in ids:
        if root in state:
            continue
        # Iterative DFS: (node, next child index).
        stack: List[Tuple[str, int]] = [(root, 0)]
        state[root] = 1
        while stack:
            node, index = stack[-1]
            children = succ[node]
            if index >= len(children):
                state[node] = 2
                stack.pop()
                continue
            stack[-1] = (node, index + 1)
            child = children[index]
            seen = state.get(child)
            if seen == 1:
                continue  # a back edge closes a cycle: drop it
            kept.append((node, child))
            if seen is None:
                state[child] = 1
                stack.append((child, 0))
    return kept


def layer_of(ids: Sequence[str], pairs: Sequence[Tuple[str, str]]) -> Dict[str, int]:
    """Longest-path layer of every node (sources are layer 0)."""
    dag = _acyclic(list(ids), list(pairs))
    indegree = {n: 0 for n in ids}
    succ: Dict[str, List[str]] = {n: [] for n in ids}
    for a, b in dag:
        succ[a].append(b)
        indegree[b] += 1
    layer = {n: 0 for n in ids}
    queue = [n for n in ids if indegree[n] == 0]
    head = 0
    while head < len(queue):
        node = queue[head]
        head += 1
        for child in succ[node]:
            layer[child] = max(layer[child], layer[node] + 1)
            indegree[child] -= 1
            if indegree[child] == 0:
                queue.append(child)
    return layer


def _layered(ids: List[str], pairs: List[Tuple[str, str]], direction: str, node_w: float, node_h: float) -> Dict[str, Position]:
    layer = layer_of(ids, pairs)
    depth = max(layer.values()) + 1
    rows: List[List[str]] = [[] for _ in range(depth)]
    for node in ids:
        rows[layer[node]].append(node)
    neighbours_up: Dict[str, List[str]] = {n: [] for n in ids}
    neighbours_down: Dict[str, List[str]] = {n: [] for n in ids}
    for a, b in pairs:
        if a == b:
            continue
        if layer[a] < layer[b]:
            neighbours_up[b].append(a)
            neighbours_down[a].append(b)
        elif layer[b] < layer[a]:
            neighbours_up[a].append(b)
            neighbours_down[b].append(a)
    for sweep in range(SWEEPS):
        downward = sweep % 2 == 0
        order = range(1, depth) if downward else range(depth - 2, -1, -1)
        for index in order:
            position = {n: i for row in rows for i, n in enumerate(row)}
            row = rows[index]
            keyed = []
            for current, node in enumerate(row):
                others = neighbours_up[node] if downward else neighbours_down[node]
                key = sum(position[o] for o in others) / len(others) if others else float(current)
                keyed.append((key, current, node))
            keyed.sort()
            rows[index] = [node for _k, _c, node in keyed]
    horizontal = direction in ("right", "left")
    along = node_h if horizontal else node_w  # the size of a node along its layer
    across = node_w if horizontal else node_h  # the size of a node from layer to layer
    widest = max(len(row) for row in rows)
    span = widest * along + (widest - 1) * SPACING
    out: Dict[str, Position] = {}
    for index, row in enumerate(rows):
        width = len(row) * along + (len(row) - 1) * SPACING
        offset = (span - width) / 2.0
        step = depth - 1 - index if direction in ("up", "left") else index
        for order_index, node in enumerate(row):
            secondary = offset + order_index * (along + SPACING)
            primary = step * (across + LAYER_GAP)
            out[node] = (primary, secondary) if horizontal else (secondary, primary)
    return out


# --------------------------------------------------------------------------
# radial, force, grid


def _radial(ids: List[str], pairs: List[Tuple[str, str]], node_w: float, node_h: float) -> Dict[str, Position]:
    adjacent: Dict[str, List[str]] = {n: [] for n in ids}
    incoming = {n: 0 for n in ids}
    for a, b in pairs:
        if a == b:
            continue
        adjacent[a].append(b)
        adjacent[b].append(a)
        incoming[b] += 1
    root = next((n for n in ids if incoming[n] == 0), ids[0])
    depth: Dict[str, int] = {}
    for start in [root] + ids:
        if start in depth:
            continue
        # Another component starts one ring further out, so it never lands on the centre.
        base = 0 if not depth else max(depth.values()) + 1
        depth[start] = base
        queue = [start]
        head = 0
        while head < len(queue):
            node = queue[head]
            head += 1
            for other in adjacent[node]:
                if other not in depth:
                    depth[other] = depth[node] + 1
                    queue.append(other)
    rings: Dict[int, List[str]] = {}
    for node in ids:
        rings.setdefault(depth[node], []).append(node)
    step = max(node_w, node_h) + LAYER_GAP
    out: Dict[str, Position] = {}
    previous_radius = 0.0
    for level in sorted(rings):
        members = rings[level]
        if level == 0 and len(members) == 1:
            radius = 0.0
        else:
            # Far enough out for the ring's boxes to fit side by side, and a full step past the last ring.
            needed = len(members) * (node_w + SPACING) / (2 * math.pi)
            radius = max(level * step, needed, previous_radius + step)
        previous_radius = radius
        for index, node in enumerate(members):
            # Odd rings turn by half a slot so a chain does not stack on one vertical line.
            angle = -math.pi / 2 + 2 * math.pi * (index + 0.5 * (level % 2)) / len(members)
            cx, cy = radius * math.cos(angle), radius * math.sin(angle)
            out[node] = (cx - node_w / 2.0, cy - node_h / 2.0)
    return out


def _force(ids: List[str], pairs: List[Tuple[str, str]], node_w: float, node_h: float) -> Dict[str, Position]:
    count = len(ids)
    if count == 1:
        return {ids[0]: (0.0, 0.0)}
    rng = random.Random(FORCE_SEED + count)
    side = math.sqrt(count) * (max(node_w, node_h) + SPACING)
    xs = [rng.uniform(0, side) for _ in ids]
    ys = [rng.uniform(0, side) for _ in ids]
    index = {node: i for i, node in enumerate(ids)}
    links = [(index[a], index[b]) for a, b in pairs if a != b]
    k = math.sqrt(side * side / count)
    k2 = k * k
    centre = side / 2.0
    temperature = side / 10.0
    # Plain lists and locals: this loop is O(n^2) per iteration, the only slow layout.
    for iteration in range(FORCE_ITERATIONS):
        disp_x = [0.0] * count
        disp_y = [0.0] * count
        for i in range(count):
            xi, yi = xs[i], ys[i]
            fx = fy = 0.0
            for j in range(i + 1, count):
                dx = xi - xs[j]
                dy = yi - ys[j]
                d2 = dx * dx + dy * dy or 0.0001
                push = k2 / d2  # repulsion k^2/d along the unit vector (dx, dy)/d
                fx += dx * push
                fy += dy * push
                disp_x[j] -= dx * push
                disp_y[j] -= dy * push
            # Gravity toward the centre, weak next to the springs of an edge, so a node
            # nothing pulls on settles near the rest instead of against the frame.
            disp_x[i] += fx + (centre - xi) * GRAVITY
            disp_y[i] += fy + (centre - yi) * GRAVITY
        for a, b in links:
            dx = xs[a] - xs[b]
            dy = ys[a] - ys[b]
            pull = math.hypot(dx, dy) / k  # attraction d^2/k along the unit vector
            disp_x[a] -= dx * pull
            disp_y[a] -= dy * pull
            disp_x[b] += dx * pull
            disp_y[b] += dy * pull
        for i in range(count):
            dx, dy = disp_x[i], disp_y[i]
            length = math.hypot(dx, dy)
            if length > 0:
                step = min(length, temperature) / length
                xs[i] = min(side, max(0.0, xs[i] + dx * step))
                ys[i] = min(side, max(0.0, ys[i] + dy * step))
        temperature = max(side / 10.0 * (1 - (iteration + 1) / FORCE_ITERATIONS), 0.5)
    return _snap(ids, xs, ys, node_w + SPACING, node_h + SPACING, centre)


def _snap(ids: List[str], xs: List[float], ys: List[float], cell_w: float, cell_h: float, centre: float) -> Dict[str, Position]:
    """Each node in the free grid cell nearest its position, the most central nodes first."""
    order = sorted(range(len(ids)), key=lambda i: ((xs[i] - centre) ** 2 + (ys[i] - centre) ** 2, i))
    taken = set()
    out: Dict[str, Position] = {}
    for i in order:
        want_c, want_r = xs[i] / cell_w, ys[i] / cell_h
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
        out[ids[i]] = (best[1] * cell_w, best[2] * cell_h)
    return out


def _grid(ids: List[str], node_w: float, node_h: float) -> Dict[str, Position]:
    columns = max(1, int(math.ceil(math.sqrt(len(ids)))))
    return {node: ((i % columns) * (node_w + SPACING), (i // columns) * (node_h + SPACING)) for i, node in enumerate(ids)}
