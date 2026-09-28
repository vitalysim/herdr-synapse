"""``tree``: a tidy tree for boxes of any size (``_tidy``), levels by depth.

The parent of a node is its ``data["parent"]`` when any node carries one (a
node with ``parent`` None is a root), else the first edge that reaches it in a
breadth-first walk from the roots (the nodes without incoming edges, then the
first unreached node in order). Edges that are not tree edges still route;
they just do not shape the tree.

A level is as thick as its largest node, levels ``rank_gap`` apart; siblings
sit ``gap`` apart and cousins ``1.5 x gap``. ``side`` grows the tree to one
side of its root (``right``, the default, or ``left``) or to both, the root's
children split between the two sides balanced by leaf count, greedily in input
order (a mind map). ``direction`` turns the whole drawing as elsewhere.

A pinned node anchors its whole subtree: the rest of the tree lays out without
it, the subtree is drawn around the pin, and nodes in its way move along the
sibling axis to clear it.
"""
from __future__ import annotations

from typing import Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team.canvas_layouts import LNode, Layout, LayoutError, LayoutRequest, LayoutResult, Point
from herdr_team.canvas_layouts import _tidy, _util

#: Its place in the registration order.
ORDER = 20
SIDES = ("right", "left", "both")
#: Cousins (nodes of different subtrees on one level) keep this many gaps apart.
SUBTREE_GAP = 1.5


def structure(request: LayoutRequest) -> Tuple[List[str], Dict[str, List[str]], Dict[str, Optional[str]]]:
    """``(roots, children, parent)`` in input order."""
    nodes = _util.nodes_in_order(request)
    ids = [n.id for n in nodes]
    known = set(ids)
    parent: Dict[str, Optional[str]] = {}
    if any("parent" in n.data for n in nodes):
        for n in nodes:
            p = n.data.get("parent")
            parent[n.id] = p if isinstance(p, str) and p in known and p != n.id else None
        # A parent cycle leaves its members without a root: cut it at the first member in order.
        for n in ids:
            seen = [n]
            cursor = parent[n]
            while cursor is not None and cursor not in seen:
                seen.append(cursor)
                cursor = parent[cursor]
            if cursor is not None:
                parent[min(seen, key=ids.index)] = None
    else:
        edges = _util.edges_in_order(request)
        succ: Dict[str, List[str]] = {n: [] for n in ids}
        incoming = {n: 0 for n in ids}
        for e in edges:
            if e.a != e.b:
                succ[e.a].append(e.b)
                incoming[e.b] += 1
        starts = [n for n in ids if incoming[n] == 0] + ids
        for start in starts:
            if start in parent:
                continue
            parent[start] = None
            queue = [start]
            head = 0
            while head < len(queue):
                node = queue[head]
                head += 1
                for child in succ[node]:
                    if child not in parent:
                        parent[child] = node
                        queue.append(child)
    children: Dict[str, List[str]] = {n: [] for n in ids}
    for n in ids:
        if parent[n] is not None:
            children[parent[n]].append(n)  # type: ignore[index]
    roots = [n for n in ids if parent[n] is None]
    return roots, children, parent


def _leaves(node: str, children: Mapping[str, Sequence[str]]) -> int:
    kids = children.get(node, ())
    return 1 if not kids else sum(_leaves(k, children) for k in kids)


def tree(request: LayoutRequest) -> LayoutResult:
    side = request.options.get("side", "right")
    if side not in SIDES:
        raise LayoutError("options.side", "side is one of {}".format(", ".join(SIDES)))
    nodes = _util.nodes_in_order(request)
    by_id = {n.id: n for n in nodes}
    roots, children, parent = structure(request)
    pinned = {n.id for n in nodes if n.pin is not None}
    # The rest of the tree lays out without the pinned subtrees; each is then drawn around its pin.
    anchored: Dict[str, str] = {}  # node -> the pinned ancestor (or itself) its subtree hangs from
    for n in [n.id for n in nodes]:
        cursor: Optional[str] = n
        while cursor is not None:
            if cursor in pinned:
                anchored[n] = cursor
                break
            cursor = parent[cursor]
    free_children = {n: [k for k in kids if k not in pinned] for n, kids in children.items()}
    free_roots = [r for r in roots if r not in pinned]
    position = _draw(request, free_roots, free_children, by_id, side)
    for pin_root in [n.id for n in nodes if n.id in pinned and (parent[n.id] is None or anchored.get(parent[n.id]) is None)]:
        sub = _draw(request, [pin_root], {n: [k for k in kids if k not in pinned or k == pin_root] for n, kids in children.items()},
                    by_id, _side_of(pin_root, parent, children, roots, side))
        corner = by_id[pin_root].pin
        dx, dy = corner[0] - sub[pin_root][0], corner[1] - sub[pin_root][1]  # type: ignore[index]
        for n, (x, y) in sub.items():
            position[n] = (x + dx, y + dy)
    # Nested pins inside a pinned subtree: their own subtrees hang from them too.
    for n in [n.id for n in nodes if n.id in pinned and n.id not in position]:
        sub = _draw(request, [n], children, by_id, side)
        corner = by_id[n].pin
        dx, dy = corner[0] - sub[n][0], corner[1] - sub[n][1]  # type: ignore[index]
        for m, (x, y) in sub.items():
            position[m] = (x + dx, y + dy)
    for n in pinned:
        position[n] = (float(by_id[n].pin[0]), float(by_id[n].pin[1]))  # type: ignore[index]
    if pinned:
        fixed = {n: _util.box_of(position[n], (by_id[n].w, by_id[n].h)) for n in pinned}
        order = [n.id for n in nodes if n.id not in pinned and n.id in anchored] + [n.id for n in nodes if n.id not in anchored]
        axes = "y" if request.direction in ("right", "left") else "x"
        settled, _moved = _util.settle(order, position, {n.id: (n.w, n.h) for n in nodes}, fixed, request.gap, axes)
        position.update(settled)
    else:
        lo_x = min(x for x, _y in position.values())
        lo_y = min(y for _x, y in position.values())
        position = {n: (x - lo_x, y - lo_y) for n, (x, y) in position.items()}
    ports: Dict[str, Tuple[str, str]] = {}
    growth = {n: _side_of(n, parent, children, roots, side) for n in by_id}
    for e in _util.edges_in_order(request):
        if parent.get(e.b) == e.a:
            flip = growth[e.b] == "left"
            direction = _flipped(request.direction) if flip else request.direction
            ports[e.id] = (_util.side_to_world(direction, "s"), _util.side_to_world(direction, "n"))
    notes = _util.pin_overlaps(nodes)
    return LayoutResult(positions=position, ports=ports, notes=tuple(notes))


def _flipped(direction: str) -> str:
    return {"down": "up", "up": "down", "right": "left", "left": "right"}[direction]


def _side_of(node: str, parent: Mapping[str, Optional[str]], children: Mapping[str, Sequence[str]], roots: Sequence[str], side: str) -> str:
    """Which side of its root a node grows to: the root's own split for ``both``."""
    if side != "both":
        return side
    chain = [node]
    while parent.get(chain[-1]) is not None:
        chain.append(parent[chain[-1]])  # type: ignore[arg-type]
    if len(chain) < 2:
        return "right"
    root, first = chain[-1], chain[-2]
    return _split(root, children).get(first, "right")


def _split(root: str, children: Mapping[str, Sequence[str]]) -> Dict[str, str]:
    """The root's children split into right and left, balanced by leaf count, greedily in input order."""
    counts = {"right": 0, "left": 0}
    out: Dict[str, str] = {}
    for kid in children.get(root, ()):
        chosen = "right" if counts["right"] <= counts["left"] else "left"
        out[kid] = chosen
        counts[chosen] += _leaves(kid, children)
    return out


def _draw(request: LayoutRequest, roots: Sequence[str], children: Mapping[str, Sequence[str]], by_id: Mapping[str, LNode],
          side: str) -> Dict[str, Point]:
    """World corners of the trees under ``roots`` (``side`` right, left or both), in the drawing's own frame."""
    out: Dict[str, Point] = {}
    cursor = 0.0
    for root in roots:
        if side == "both":
            split = _split(root, children)
            halves = {"right": [k for k in children.get(root, ()) if split.get(k) == "right"],
                      "left": [k for k in children.get(root, ()) if split.get(k) == "left"]}
            parts = {}
            for half, kids in halves.items():
                local = dict(children)
                local[root] = kids
                parts[half] = _one(request, root, local, by_id, request.direction if half == "right" else _flipped(request.direction))
            # Both halves share the root: line the left half's root up with the right half's.
            rx, ry = parts["right"][root]
            lx, ly = parts["left"][root]
            drawn = dict(parts["right"])
            for n, (x, y) in parts["left"].items():
                drawn[n] = (x - lx + rx, y - ly + ry)
        else:
            drawn = _one(request, root, children, by_id, request.direction if side == "right" else _flipped(request.direction))
        # Trees of a forest stand side by side along the sibling axis.
        lo = min(x for x, _y in drawn.values()) if request.direction in ("down", "up") else min(y for _x, y in drawn.values())
        hi = max(x + by_id[n].w for n, (x, _y) in drawn.items()) if request.direction in ("down", "up") else \
            max(y + by_id[n].h for n, (_x, y) in drawn.items())
        for n, (x, y) in drawn.items():
            out[n] = (x - lo + cursor, y) if request.direction in ("down", "up") else (x, y - lo + cursor)
        cursor += hi - lo + request.gap * SUBTREE_GAP
    return out


def _one(request: LayoutRequest, root: str, children: Mapping[str, Sequence[str]], by_id: Mapping[str, LNode], direction: str) -> Dict[str, Point]:
    """One tree grown in ``direction``, its root's top-left at the origin of its own frame."""
    members: List[str] = []
    depth: Dict[str, int] = {root: 0}
    stack = [root]
    while stack:
        node = stack.pop()
        members.append(node)
        for kid in children.get(node, ()):
            depth[kid] = depth[node] + 1
            stack.append(kid)
    size = {n: _util.frame_size(direction, by_id[n].w, by_id[n].h) for n in members}
    centres = _tidy.tidy([root], {n: [k for k in children.get(n, ())] for n in members}, {n: size[n][0] for n in members},
                         request.gap, request.gap * SUBTREE_GAP)
    levels = max(depth.values()) + 1
    thick = [0.0] * levels
    for n in members:
        thick[depth[n]] = max(thick[depth[n]], size[n][1])
    tops = [0.0] * levels
    for d in range(1, levels):
        tops[d] = tops[d - 1] + thick[d - 1] + request.rank_gap
    out: Dict[str, Point] = {}
    for n in members:
        fx = centres[n] - size[n][0] / 2.0
        fy = tops[depth[n]] + (thick[depth[n]] - size[n][1]) / 2.0
        out[n] = _util.to_world(direction, fx, fy, size[n][0], size[n][1])
    rx, ry = out[root]
    return {n: (x - rx, y - ry) for n, (x, y) in out.items()}


LAYOUTS = (
    Layout(name="tree", run=tree, options=("side",), router="curved",
           doc="a tidy tree: levels by depth, siblings gap apart; side right, left or both (a mind map)"),
)
