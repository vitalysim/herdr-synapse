"""Graph layout for the canvas: the compatibility facade over the layout registry (canvas v2 phase 2).

Since phase 2 every layout is one module of ``canvas_layouts`` (``layers``,
``tree``, ``radial``, ``force``, ``grid``, ``row``/``column``), run through
``canvas_layouts.run``. This module keeps the 0.21 functions with their
signatures for the callers that still use them (``canvas`` pre-layout, tests):
``layout`` and ``plan`` take node ids, ``(a, b)`` pairs and optional per-node
sizes, and return top-left corners normalised so the smallest x and y are 0
(and, from ``plan``, the bend points of every edge that crosses ranks, by the
edge's index). ``layered`` is the ``layers`` layout; an unknown name falls back
to it. Everything is deterministic.
"""
from __future__ import annotations

from typing import Dict, List, Optional, Sequence, Tuple

from herdr_team import canvas_layouts
from herdr_team.canvas_layouts import LEdge, LNode, LayoutRequest

NODE_W, NODE_H = 160, 60
#: Between neighbours in one layer, and between layers.
SPACING = 60
LAYER_GAP = 100
#: ``down`` and ``right`` are what agents ask for; ``up`` and ``left`` come from Mermaid's ``BT`` and ``RL``.
DIRECTIONS = canvas_layouts.DIRECTIONS
FORCE_SEED = 20260926

Position = Tuple[float, float]
Size = Tuple[float, float]


def _names() -> Tuple[str, ...]:
    """Every layout a graph may name: the ones that use edges, then ``grid``."""
    found = [name for layout in canvas_layouts.layouts() if layout.edges for name in (layout.name,) + tuple(layout.aliases)]
    return tuple(found + (["grid"] if "grid" not in found and canvas_layouts.get("grid") is not None else []))


def __getattr__(name: str) -> object:  # PEP 562: ``LAYOUTS`` follows the registry
    if name == "LAYOUTS":
        return _names()
    raise AttributeError(name)


def layout(nodes: Sequence[str], edges: Sequence[Tuple[str, str]], algorithm: str = "layered", direction: str = "down",
           node_w: float = NODE_W, node_h: float = NODE_H, sizes: Optional[Dict[str, Size]] = None) -> Dict[str, Position]:
    """Top-left corners of every node box, normalised so the smallest x and y are 0.

    ``sizes`` gives a node its own ``(w, h)``; a node it leaves out is ``node_w`` x ``node_h``.
    """
    return plan(nodes, edges, algorithm, direction, node_w, node_h, sizes)[0]


def plan(nodes: Sequence[str], edges: Sequence[Tuple[str, str]], algorithm: str = "layered", direction: str = "down",
         node_w: float = NODE_W, node_h: float = NODE_H,
         sizes: Optional[Dict[str, Size]] = None) -> Tuple[Dict[str, Position], Dict[int, List[Position]]]:
    """``layout``'s corners, and the bend points of each edge that crosses layers, by the edge's index in
    ``edges``, in order from its first node to its second, in the corners' coordinates."""
    ids = list(dict.fromkeys(str(n) for n in nodes))
    if not ids:
        return {}, {}
    known = set(ids)
    size = {n: (float((sizes or {}).get(n, (node_w, node_h))[0]), float((sizes or {}).get(n, (node_w, node_h))[1])) for n in ids}
    request = LayoutRequest(
        nodes=tuple(LNode(id=n, w=size[n][0], h=size[n][1], order=i) for i, n in enumerate(ids)),
        edges=tuple(LEdge(id=str(index), a=str(a), b=str(b)) for index, (a, b) in enumerate(edges) if str(a) in known and str(b) in known),
        direction=direction if direction in DIRECTIONS else "down", gap=SPACING, rank_gap=LAYER_GAP, incremental=False)
    name = "layers" if algorithm in ("layered", "layers") or algorithm not in _names() else algorithm
    chosen = canvas_layouts.get(name)
    if chosen is not None and request.direction not in chosen.directions:
        request = LayoutRequest(nodes=request.nodes, edges=request.edges, gap=SPACING, rank_gap=LAYER_GAP, incremental=False)
    result = canvas_layouts.run(name, request)
    min_x = min(x for x, _ in result.positions.values())
    min_y = min(y for _, y in result.positions.values())
    corners = {n: (_r2(result.positions[n][0] - min_x), _r2(result.positions[n][1] - min_y)) for n in ids}
    bends = {int(eid): [(_r2(x - min_x), _r2(y - min_y)) for x, y in points] for eid, points in result.hints.items() if points}
    return corners, dict(sorted(bends.items()))


def _r2(value: float) -> float:
    return round(float(value), 2) + 0.0


# --------------------------------------------------------------------------
# layer_of: the 0.21 longest-path layers (kept for callers and tests)


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
