"""``radial``: rings around a centre node.

A tree (every node reached once from the root through the tree structure of
``tree.structure``, and no other edges) gets a radial tidy tree: each subtree
has a wedge of the circle proportional to its leaves, and a node sits in the
middle of its wedge. Any other graph gets breadth-first rings around its first
source, spaced evenly. Either way a ring's radius is the previous radius plus
the ring's largest extent plus ``rank_gap``, and at least what the ring's
boxes need side by side around the circumference.

Pins are fixed; other nodes in their way move around their ring to clear them
(and outward when the ring is full).
"""
from __future__ import annotations

import math
from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team.canvas_layouts import LNode, Layout, LayoutRequest, LayoutResult, Point
from herdr_team.canvas_layouts import _util
from herdr_team.canvas_layouts.tree import structure

#: Its place in the registration order.
ORDER = 25


def _is_tree(request: LayoutRequest, parent: Mapping[str, Optional[str]], roots: Sequence[str]) -> bool:
    if len(roots) != 1:
        return False
    for e in request.edges:
        if e.a == e.b:
            continue
        if parent.get(e.b) != e.a and parent.get(e.a) != e.b:
            return False
    return True


def radial(request: LayoutRequest) -> LayoutResult:
    nodes = _util.nodes_in_order(request)
    by_id = {n.id: n for n in nodes}
    roots, children, parent = structure(request)
    extent = {n.id: math.sqrt(n.w * n.w + n.h * n.h) / 2.0 for n in nodes}
    centre: Dict[str, Point] = {}
    if _is_tree(request, parent, roots):
        rings = _depths(roots[0], children)
        radius = _radii(rings, extent, request)
        leaves: Dict[str, int] = {}
        for n in reversed(_preorder(roots[0], children)):
            kids = children.get(n, ())
            leaves[n] = 1 if not kids else sum(leaves[k] for k in kids)
        wedge: Dict[str, Tuple[float, float]] = {roots[0]: (-math.pi / 2, 3 * math.pi / 2)}
        for n in _preorder(roots[0], children):
            lo, hi = wedge[n]
            mid = (lo + hi) / 2.0
            depth = _depth_of(n, rings)
            r = radius[depth]
            centre[n] = (r * math.cos(mid), r * math.sin(mid))
            kids = children.get(n, ())
            total = float(sum(leaves[k] for k in kids)) or 1.0
            at = lo
            for k in kids:
                share = (hi - lo) * leaves[k] / total
                wedge[k] = (at, at + share)
                at += share
    else:
        rings = _bfs_rings(nodes, request)
        radius = _radii(rings, extent, request)
        for level, members in enumerate(rings):
            r = radius[level]
            for index, n in enumerate(members):
                if r == 0:
                    centre[n] = (0.0, 0.0)
                    continue
                # Odd rings turn by half a slot so a chain does not stack on one line.
                angle = -math.pi / 2 + 2 * math.pi * (index + 0.5 * (level % 2)) / len(members)
                centre[n] = (r * math.cos(angle), r * math.sin(angle))
    position = {n: (centre[n][0] - by_id[n].w / 2.0, centre[n][1] - by_id[n].h / 2.0) for n in by_id}
    pinned = [n.id for n in nodes if n.pin is not None]
    if pinned:
        anchors = [n for n in pinned]
        dx = _util.median([by_id[n].pin[0] - position[n][0] for n in anchors])  # type: ignore[index]
        dy = _util.median([by_id[n].pin[1] - position[n][1] for n in anchors])  # type: ignore[index]
        position = {n: (x + dx, y + dy) for n, (x, y) in position.items()}
        for n in pinned:
            position[n] = (float(by_id[n].pin[0]), float(by_id[n].pin[1]))  # type: ignore[index]
        position = _clear_around(nodes, position, pinned, request.gap)
    else:
        # Rings are as close as the ring sizes allow, but a box at an angle can still reach a neighbour: settle.
        position, _moved = _util.settle([n.id for n in nodes], position, {n.id: (n.w, n.h) for n in nodes}, {}, request.gap / 2.0)
        lo_x = min(x for x, _y in position.values())
        lo_y = min(y for _x, y in position.values())
        position = {n: (x - lo_x, y - lo_y) for n, (x, y) in position.items()}
    return LayoutResult(positions=position, notes=tuple(_util.pin_overlaps(nodes)))


def _clear_around(nodes: Sequence[LNode], position: Dict[str, Point], pinned: Sequence[str], gap: float) -> Dict[str, Point]:
    """Unpinned nodes that meet a pin move around the centre (angularly) until clear, else aside."""
    by_id = {n.id: n for n in nodes}
    fixed = {n: _util.box_of(position[n], (by_id[n].w, by_id[n].h)) for n in pinned}
    cx = math.fsum(position[n.id][0] + n.w / 2.0 for n in nodes) / len(nodes)
    cy = math.fsum(position[n.id][1] + n.h / 2.0 for n in nodes) / len(nodes)
    placed: List[Tuple[float, float, float, float]] = list(fixed.values())
    out = dict(position)
    for n in nodes:
        if n.id in fixed:
            continue
        x, y = out[n.id]
        box = _util.box_of((x, y), (n.w, n.h))
        if any(_util.meets(box, other, gap) for other in placed):
            px, py = x + n.w / 2.0 - cx, y + n.h / 2.0 - cy
            r = math.sqrt(px * px + py * py)
            base = math.atan2(py, px)
            found = None
            for step in range(1, 73):
                for sign in (1, -1):
                    angle = base + sign * step * math.pi / 72
                    nx, ny = cx + r * math.cos(angle) - n.w / 2.0, cy + r * math.sin(angle) - n.h / 2.0
                    if not any(_util.meets(_util.box_of((nx, ny), (n.w, n.h)), other, gap) for other in placed):
                        found = (nx, ny)
                        break
                if found is not None:
                    break
            if found is None:
                settled, _moved = _util.settle([n.id], {n.id: (x, y)}, {n.id: (n.w, n.h)},
                                               {str(i): b for i, b in enumerate(placed)}, gap)
                found = settled[n.id]
            out[n.id] = found
            box = _util.box_of(found, (n.w, n.h))
        placed.append(box)
    return out


def _preorder(root: str, children: Mapping[str, Sequence[str]]) -> List[str]:
    out: List[str] = []
    stack = [root]
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(reversed(list(children.get(n, ()))))
    return out


def _depths(root: str, children: Mapping[str, Sequence[str]]) -> List[List[str]]:
    rings: List[List[str]] = [[root]]
    while True:
        nxt = [k for n in rings[-1] for k in children.get(n, ())]
        if not nxt:
            return rings
        rings.append(nxt)


def _depth_of(node: str, rings: Sequence[Sequence[str]]) -> int:
    for depth, members in enumerate(rings):
        if node in members:
            return depth
    return 0


def _bfs_rings(nodes: Sequence[LNode], request: LayoutRequest) -> List[List[str]]:
    ids = [n.id for n in nodes]
    adjacent: Dict[str, List[str]] = {n: [] for n in ids}
    incoming = {n: 0 for n in ids}
    for e in _util.edges_in_order(request):
        if e.a == e.b:
            continue
        adjacent[e.a].append(e.b)
        adjacent[e.b].append(e.a)
        incoming[e.b] += 1
    root = next((n for n in ids if incoming[n] == 0), ids[0])
    depth: Dict[str, int] = {}
    for start in [root] + ids:
        if start in depth:
            continue
        # Another component starts one ring further out, so it never lands on the centre.
        depth[start] = 0 if not depth else max(depth.values()) + 1
        queue = [start]
        head = 0
        while head < len(queue):
            node = queue[head]
            head += 1
            for other in adjacent[node]:
                if other not in depth:
                    depth[other] = depth[node] + 1
                    queue.append(other)
    rings: List[List[str]] = [[] for _ in range(max(depth.values()) + 1)]
    for n in ids:
        rings[depth[n]].append(n)
    return [ring for ring in rings if ring]


def _radii(rings: Sequence[Sequence[str]], extent: Mapping[str, float], request: LayoutRequest) -> List[float]:
    out: List[float] = []
    previous_radius, previous_extent = 0.0, 0.0
    for level, members in enumerate(rings):
        biggest = max(extent[n] for n in members)
        if level == 0 and len(members) == 1:
            radius = 0.0
        else:
            around = math.fsum(2 * extent[n] + request.gap for n in members) / (2 * math.pi)
            radius = max(previous_radius + previous_extent + biggest + request.rank_gap, around)
        out.append(radius)
        previous_radius, previous_extent = radius, biggest
    return out


LAYOUTS = (
    Layout(name="radial", run=radial, router="straight", directions=("down", "right", "up", "left"),
           doc="rings around a centre: a tree's subtrees get wedges by leaf count, any other graph breadth-first rings"),
)
