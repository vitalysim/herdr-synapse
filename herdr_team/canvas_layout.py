"""Graph layout for the canvas's ``graph`` operation and Mermaid flowcharts (0.21).

Pure functions over node ids and edges (contract section 6, ``graph``). A
layout returns the top-left corner of every node box relative to ``(0, 0)``;
``canvas`` places the whole drawing and turns nodes and edges into native
shapes and bound arrows. Since 0.22 every node may have its own size
(``sizes``: ``canvas`` measures each label first), so a long label never runs
into its neighbour. Everything is deterministic: the force layout is seeded,
so the same graph always draws the same picture and a test can pin it.

* ``layered``: longest-path layers from the sources (cycles broken at their
  back edges), then three barycentre sweeps to reduce crossings. Each layer
  is as thick as its largest node; nodes sit side by side at their own sizes,
  centred in their layer. An edge across several layers gets a slot in each
  layer it crosses (Sugiyama's virtual nodes), so the sweeps keep nodes out
  of its way; ``plan`` returns those slots as the edge's bend points, and a
  long edge bends through them instead of cutting through the boxes between.
* ``radial``: breadth-first rings around the first source.
* ``force``: Fruchterman-Reingold, 200 iterations, kept inside its frame
  (the classic W x L bound, with a little gravity so unconnected nodes do
  not all end on its edge), then each box snapped to the nearest free cell
  of a grid, so none overlap and the drawing stays about the frame's size.
* ``grid``: row-major in input order.

``radial``, ``force`` and ``grid`` use one cell the size of the largest node
and centre each node in its cell: simple, and never overlapping.
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
Size = Tuple[float, float]


def layout(nodes: Sequence[str], edges: Sequence[Tuple[str, str]], algorithm: str = "layered", direction: str = "down",
           node_w: float = NODE_W, node_h: float = NODE_H, sizes: Optional[Dict[str, Size]] = None) -> Dict[str, Position]:
    """Top-left corners of every node box, normalised so the smallest x and y are 0.

    ``sizes`` gives a node its own ``(w, h)``; a node it leaves out is ``node_w`` x ``node_h``.
    """
    return plan(nodes, edges, algorithm, direction, node_w, node_h, sizes)[0]


def plan(nodes: Sequence[str], edges: Sequence[Tuple[str, str]], algorithm: str = "layered", direction: str = "down",
         node_w: float = NODE_W, node_h: float = NODE_H,
         sizes: Optional[Dict[str, Size]] = None) -> Tuple[Dict[str, Position], Dict[int, List[Position]]]:
    """``layout``'s corners, and the bend points of each edge that crosses layers (``layered`` only), by the
    edge's index in ``edges``, in order from its first node to its second, in the corners' coordinates."""
    ids = list(dict.fromkeys(str(n) for n in nodes))
    known = set(ids)
    indexed = [(index, str(a), str(b)) for index, (a, b) in enumerate(edges) if str(a) in known and str(b) in known]
    pairs = [(a, b) for _index, a, b in indexed]
    if not ids:
        return {}, {}
    size = {n: (float((sizes or {}).get(n, (node_w, node_h))[0]), float((sizes or {}).get(n, (node_w, node_h))[1])) for n in ids}
    if algorithm == "layered" or algorithm not in LAYOUTS:
        corners, bends = _layered(ids, pairs, direction, size, [index for index, _a, _b in indexed])
        return _normalise(corners, bends)
    cell_w, cell_h = max(w for w, _ in size.values()), max(h for _, h in size.values())
    if algorithm == "radial":
        raw = _radial(ids, pairs, cell_w, cell_h)
    elif algorithm == "force":
        raw = _force(ids, pairs, cell_w, cell_h)
    else:
        raw = _grid(ids, cell_w, cell_h)
    # Each node centred in the cell the largest node would fill.
    return _normalise({n: (x + (cell_w - size[n][0]) / 2.0, y + (cell_h - size[n][1]) / 2.0) for n, (x, y) in raw.items()})


def _normalise(raw: Dict[str, Position], bends: Optional[Dict[int, List[Position]]] = None
               ) -> Tuple[Dict[str, Position], Dict[int, List[Position]]]:
    """Corners moved so the smallest x and y are 0, and the bend points moved with them."""
    min_x = min(x for x, _ in raw.values())
    min_y = min(y for _, y in raw.values())
    return ({key: (round(x - min_x, 2), round(y - min_y, 2)) for key, (x, y) in raw.items()},
            {index: [(round(x - min_x, 2), round(y - min_y, 2)) for x, y in points] for index, points in (bends or {}).items()})


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


def _layered(ids: List[str], pairs: List[Tuple[str, str]], direction: str, size: Dict[str, Size],
             edge_ids: Optional[List[int]] = None) -> Tuple[Dict[str, Position], Dict[int, List[Position]]]:
    """Corners of the real nodes, and the slots each long edge bends through (by ``edge_ids``, else its index)."""
    layer = layer_of(ids, pairs)
    depth = max(layer.values()) + 1
    rows: List[List[str]] = [[] for _ in range(depth)]
    for node in ids:
        rows[layer[node]].append(node)
    size = dict(size)
    # A virtual node (a point: no size) in every layer a long edge crosses; the edge is a chain through them.
    chains: Dict[int, List[str]] = {}
    links: List[Tuple[str, str]] = []
    for number, (a, b) in enumerate(pairs):
        low, high = (a, b) if layer[a] <= layer[b] else (b, a)
        if layer[high] - layer[low] <= 1:
            links.append((a, b))
            continue
        chain, previous = [], low
        for level in range(layer[low] + 1, layer[high]):
            virtual = "\x00{}:{}".format(number, level)  # never a node id: those are validated names
            size[virtual], layer[virtual] = (0.0, 0.0), level
            rows[level].append(virtual)
            links.append((previous, virtual))
            chain.append(virtual)
            previous = virtual
        links.append((previous, high))
        chains[edge_ids[number] if edge_ids is not None else number] = chain if low == a else chain[::-1]
    everyone = [n for row in rows for n in row]
    neighbours_up: Dict[str, List[str]] = {n: [] for n in everyone}
    neighbours_down: Dict[str, List[str]] = {n: [] for n in everyone}
    for a, b in links:
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

    def along(node: str) -> float:  # the size of a node along its layer
        return size[node][1] if horizontal else size[node][0]

    def across(node: str) -> float:  # the size of a node from layer to layer
        return size[node][0] if horizontal else size[node][1]

    widths = [sum(along(n) for n in row) + (len(row) - 1) * SPACING for row in rows]
    span = max(widths)
    thickness = [max((across(n) for n in row), default=0.0) for row in rows]
    order = list(range(depth - 1, -1, -1)) if direction in ("up", "left") else list(range(depth))
    start: Dict[int, float] = {}
    edge = 0.0
    for index in order:
        start[index] = edge
        edge += thickness[index] + LAYER_GAP
    out: Dict[str, Position] = {}
    for index, row in enumerate(rows):
        secondary = (span - widths[index]) / 2.0
        for node in row:
            primary = start[index] + (thickness[index] - across(node)) / 2.0
            out[node] = (primary, secondary) if horizontal else (secondary, primary)
            secondary += along(node) + SPACING
    bends = {edge: [out[virtual] for virtual in chain] for edge, chain in chains.items()}
    return {node: out[node] for node in ids}, bends


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
