"""What every layout shares: canonical input order, direction transforms, group boxes and the pin rules.

Pure helpers over the registry's dataclasses; no layout is registered here.
"""
from __future__ import annotations

import heapq
from typing import Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from herdr_team.canvas_layouts import Box, LEdge, LGroup, LNode, LayoutRequest, Point

Size = Tuple[float, float]


def nodes_in_order(request: LayoutRequest) -> List[LNode]:
    """The nodes by ``order``, then ``id``: shuffling the input changes nothing."""
    return sorted(request.nodes, key=lambda n: (n.order, n.id))


def edges_in_order(request: LayoutRequest) -> List[LEdge]:
    """The edges by their ends' ``order``, then ``id``."""
    rank = {n.id: (n.order, n.id) for n in request.nodes}
    return sorted(request.edges, key=lambda e: (rank[e.a], rank[e.b], e.id))


def all_seeded(request: LayoutRequest) -> bool:
    return bool(request.nodes) and all(n.seed is not None or n.pin is not None for n in request.nodes)


# --------------------------------------------------------------------------
# directions: a layout works in the ``down`` frame (x along a rank, y from rank to rank) and turns the result


def frame_size(direction: str, w: float, h: float) -> Size:
    """A node's size in the ``down`` frame: ``right`` and ``left`` swap the axes."""
    return (h, w) if direction in ("right", "left") else (w, h)


def to_world(direction: str, x: float, y: float, fw: float, fh: float) -> Point:
    """A top-left corner in the ``down`` frame (node size ``fw`` x ``fh`` there) as a world top-left corner."""
    if direction == "up":
        return x, -(y + fh)
    if direction == "right":
        return y, x
    if direction == "left":
        return -(y + fh), x
    return x, y


def to_frame(direction: str, x: float, y: float, fw: float, fh: float) -> Point:
    """The inverse of ``to_world``: a world top-left corner in the ``down`` frame."""
    if direction == "up":
        return x, -y - fh
    if direction == "right":
        return y, x
    if direction == "left":
        return y, -x - fh
    return x, y


def point_to_world(direction: str, x: float, y: float) -> Point:
    return to_world(direction, x, y, 0.0, 0.0)


def pad_in_frame(direction: str, pad: Sequence[float]) -> Tuple[float, float, float, float]:
    """A group's world pad (left, top, right, bottom) as (along-before, rank-before, along-after, rank-after)."""
    left, top, right, bottom = pad
    if direction == "up":
        return left, bottom, right, top
    if direction == "right":
        return top, left, bottom, right
    if direction == "left":
        return top, right, bottom, left
    return left, top, right, bottom


_SIDES = {
    "down": {"n": "n", "s": "s", "e": "e", "w": "w"},
    "up": {"n": "s", "s": "n", "e": "e", "w": "w"},
    "right": {"n": "w", "s": "e", "e": "s", "w": "n"},
    "left": {"n": "e", "s": "w", "e": "s", "w": "n"},
}


def side_to_world(direction: str, side: str) -> str:
    return _SIDES[direction][side]


# --------------------------------------------------------------------------
# boxes


def box_of(position: Point, size: Size) -> Box:
    return position[0], position[1], position[0] + size[0], position[1] + size[1]


def meets(a: Box, b: Box, gap: float = 0.0) -> bool:
    """Whether ``a`` and ``b`` come closer than ``gap`` on both axes (touching at exactly ``gap`` is apart)."""
    eps = 1e-6
    return a[0] < b[2] + gap - eps and b[0] < a[2] + gap - eps and a[1] < b[3] + gap - eps and b[1] < a[3] + gap - eps


def group_boxes(groups: Sequence[LGroup], members: Mapping[str, Optional[str]], boxes: Mapping[str, Box],
                pad_of=None) -> Dict[str, Box]:
    """Every group's box: the union of its member nodes' and child groups' boxes plus its pad, innermost first.
    A group with nothing in it gets no box. ``pad_of(group)`` overrides the pad (left, top, right, bottom)."""
    parent = {g.id: g.parent for g in groups}
    depth: Dict[str, int] = {}
    for g in groups:
        d, cursor = 0, g.parent
        while cursor is not None:
            d += 1
            cursor = parent.get(cursor)
        depth[g.id] = d
    out: Dict[str, Box] = {}
    for g in sorted(groups, key=lambda g: (-depth[g.id], g.id)):
        inside = [boxes[n] for n, gid in members.items() if gid == g.id and n in boxes]
        inside += [out[c.id] for c in groups if c.parent == g.id and c.id in out]
        if not inside:
            continue
        left, top, right, bottom = pad_of(g) if pad_of is not None else g.pad
        out[g.id] = (min(b[0] for b in inside) - left, min(b[1] for b in inside) - top,
                     max(b[2] for b in inside) + right, max(b[3] for b in inside) + bottom)
    return out


def ancestry(groups: Sequence[LGroup], gid: Optional[str]) -> List[str]:
    """``gid`` and its ancestors, innermost first."""
    parent = {g.id: g.parent for g in groups}
    out: List[str] = []
    while gid is not None and gid not in out:
        out.append(gid)
        gid = parent.get(gid)
    return out


# --------------------------------------------------------------------------
# pins


def pin_overlaps(nodes: Sequence[LNode], gap: float = 0.0) -> List[str]:
    """``pin_overlap a b`` for every pair of pinned nodes whose pins overlap: the overlap only a person can undo."""
    pinned = [n for n in nodes if n.pin is not None]
    notes = []
    for i, a in enumerate(pinned):
        for b in pinned[i + 1:]:
            if meets(box_of(a.pin, (a.w, a.h)), box_of(b.pin, (b.w, b.h))):  # type: ignore[arg-type]
                notes.append("pin_overlap {} {}".format(a.id, b.id))
    return notes


#: The most positions ``settle`` tries for one node before it gives up and moves it past everything.
SETTLE_BUDGET = 4000


def settle(order: Sequence[str], desired: Mapping[str, Point], sizes: Mapping[str, Size], fixed: Mapping[str, Box], gap: float,
           axes: str = "xy") -> Tuple[Dict[str, Point], List[str]]:
    """Each node of ``order`` at the free spot nearest its desired corner, ``gap`` clear of ``fixed`` boxes (pins)
    and of the nodes settled before it. A node already clear stays exactly where it is. ``axes`` limits the
    movement (``x`` moves along x only). Returns the corners and the ids that moved."""
    placed: List[Box] = list(fixed.values())
    out: Dict[str, Point] = {}
    moved: List[str] = []
    for nid in order:
        w, h = sizes[nid]
        start = desired[nid]
        spot = _free_spot(start, w, h, placed, gap, axes)
        out[nid] = spot
        if spot != start:
            moved.append(nid)
        placed.append((spot[0], spot[1], spot[0] + w, spot[1] + h))
    return out, moved


def _free_spot(start: Point, w: float, h: float, placed: Sequence[Box], gap: float, axes: str) -> Point:
    def hits(x: float, y: float) -> List[Box]:
        box = (x, y, x + w, y + h)
        return [b for b in placed if meets(box, b, gap)]

    if not hits(*start):
        return start
    frontier: List[Tuple[float, float, float]] = [(0.0, start[0], start[1])]
    seen: Set[Tuple[float, float]] = set()
    tries = 0
    while frontier and tries < SETTLE_BUDGET:
        dist, x, y = heapq.heappop(frontier)
        key = (round(x, 3), round(y, 3))
        if key in seen:
            continue
        seen.add(key)
        tries += 1
        blocking = hits(x, y)
        if not blocking:
            return x, y
        for b in blocking:
            steps: List[Point] = []
            if "x" in axes:
                steps += [(b[0] - gap - w, y), (b[2] + gap, y)]
            if "y" in axes:
                steps += [(x, b[1] - gap - h), (x, b[3] + gap)]
            for nx, ny in steps:
                if (round(nx, 3), round(ny, 3)) not in seen:
                    heapq.heappush(frontier, (abs(nx - start[0]) + abs(ny - start[1]), nx, ny))
    # Past everything along the first allowed axis: always free.
    if "x" in axes:
        return max(b[2] for b in placed) + gap, start[1]
    return start[0], max(b[3] for b in placed) + gap


def components(ids: Sequence[str], pairs: Iterable[Tuple[str, str]]) -> List[List[str]]:
    """Connected components, each in ``ids`` order, ordered by their first member."""
    parent = {n: n for n in ids}

    def find(n: str) -> str:
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for a, b in pairs:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    groups: Dict[str, List[str]] = {}
    for n in ids:
        groups.setdefault(find(n), []).append(n)
    return list(groups.values())


def median(values: Sequence[float]) -> float:
    ordered = sorted(values)
    mid = len(ordered) // 2
    return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) / 2.0
