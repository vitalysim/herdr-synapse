"""``layers`` (aliases ``flow`` and ``layered``): the Sugiyama layered layout, a port of dagre's algorithm (MIT).

1. **Cycles**: the greedy feedback arc set picks the edges to reverse; they
   are still drawn in their own direction (``_rank.acyclic``).
2. **Ranks**: ``same_rank`` sets share one ranking node; longest path, then
   network simplex (``_rank.rank``). With any labelled edge every ``minlen``
   doubles and ``rank_gap`` halves, and each labelled edge gets a dummy of its
   label's size on its middle rank, so a label never lands on a node. An edge
   inside a ``same_rank`` set is drawn flat (noted ``same_rank_flat a b``).
3. **Dummies**: an edge across several ranks becomes a chain of dummies; each
   group gets a left and a right border item on every rank it spans.
4. **Order**: a depth-first first order (or the seeds' order, incremental),
   then barycentre sweeps with transpose, groups kept contiguous, ``order``
   lists honoured, the best order by crossings kept (``_order``).
5. **Positions**: Brandes-Koepf along the ranks (per-node widths, ``gap``
   apart, group pads), then the separation constraints; each rank as thick as
   its thickest item, ``rank_gap`` apart, plus room for group bands
   (``_position``). An incremental run keeps seeded nodes where they were and
   only pushes what must make room.
6. **Stability**: incremental, each connected component is translated so the
   median displacement of its seeded, unpinned nodes is 0. When every node is
   seeded and the seeds already are this layout of this graph, the result is
   the seeds exactly (the fixed point).
7. **Direction**: ``right``, ``up`` and ``left`` turn the ``down`` drawing.
8. **Pins**: a pinned node sits on its pin; the others move along their rank to
   clear it (``_util.settle``).

The result carries each edge's dummy positions as router ``hints``, its label
spot, and its preferred ``ports`` (the forward side at ``a``, the backward side
at ``b``; a self-loop leaves ``e`` and enters ``n``).
"""
from __future__ import annotations

import math

from typing import Dict, List, Mapping, Optional, Sequence, Set, Tuple

from herdr_team.canvas_layouts import MAX_SIZE, LEdge, LNode, Layout, LayoutRequest, LayoutResult, Point
from herdr_team.canvas_layouts import _order, _position, _rank, _util

#: Its place in the registration order.
ORDER = 10
#: A seeded node that ends further than this from its seed counts as moved (``stats.moved``).
MOVED = 20.0
#: The closest ranks come when a drawing would otherwise run past ``MAX_SIZE`` along its ranks.
MIN_RANK_GAP = 40.0


class _Graph:
    """The proper layered graph in the ``down`` frame."""

    def __init__(self) -> None:
        self.w: Dict[str, float] = {}
        self.h: Dict[str, float] = {}
        self.rank: Dict[str, int] = {}
        self.kind: Dict[str, str] = {}  # node | dummy | label | border
        self.group: Dict[str, Optional[str]] = {}
        self.tie: Dict[str, int] = {}
        self.up: Dict[str, List[Tuple[str, float]]] = {}
        self.down: Dict[str, List[Tuple[str, float]]] = {}
        self.border_of: Dict[str, Tuple[str, str]] = {}  # key -> (group, "left" | "right")

    def add(self, key: str, w: float, h: float, rank: int, kind: str, group: Optional[str]) -> None:
        self.w[key], self.h[key], self.rank[key], self.kind[key], self.group[key] = w, h, rank, kind, group
        self.tie[key] = len(self.tie)
        self.up.setdefault(key, [])
        self.down.setdefault(key, [])

    def link(self, a: str, b: str, weight: float) -> None:
        self.down[a].append((b, weight))
        self.up[b].append((a, weight))


def _find(parent: Dict[str, str], n: str) -> str:
    while parent[n] != n:
        parent[n] = parent[parent[n]]
        n = parent[n]
    return n


def layers(request: LayoutRequest) -> LayoutResult:
    direction = request.direction
    nodes = _util.nodes_in_order(request)
    edges = _util.edges_in_order(request)
    by_id = {n.id: n for n in nodes}
    notes: List[str] = []
    group_parent = {g.id: g.parent for g in request.groups}
    pads = {g.id: _util.pad_in_frame(direction, g.pad) for g in request.groups}

    def chain(gid: Optional[str]) -> List[str]:
        return _util.ancestry(request.groups, gid)[::-1]  # outermost first

    # -- ranks -------------------------------------------------------------------------------------------
    rep = {n.id: n.id for n in nodes}
    for members in request.same_rank:
        present = [m for m in members if m in rep]
        for m in present[1:]:
            ra, rb = _find(rep, present[0]), _find(rep, m)
            if ra != rb:
                if by_id[ra].order <= by_id[rb].order:
                    rep[rb] = ra
                else:
                    rep[ra] = rb
    head = {n.id: _find(rep, n.id) for n in nodes}
    labelled = any(e.label is not None and e.label[0] > 0 for e in edges)
    factor = 2 if labelled else 1
    rank_gap = request.rank_gap / factor
    loops = [e for e in edges if e.a == e.b]
    flat = [e for e in edges if e.a != e.b and head[e.a] == head[e.b]]
    for e in flat:
        notes.append("same_rank_flat {} {}".format(e.a, e.b))
    ranked = [e for e in edges if e.a != e.b and head[e.a] != head[e.b]]
    reps = [n.id for n in nodes if head[n.id] == n.id]
    # Cycles are broken on the graph of ranking nodes, one edge per ordered pair (``same_rank`` merges and parallel edges
    # would otherwise weigh a node up), so ties fall back on input order.
    pairs: Dict[Tuple[str, str], float] = {}
    for e in ranked:
        key = (head[e.a], head[e.b])
        pairs[key] = max(pairs.get(key, 0.0), e.weight)
    pair_list = list(pairs.items())
    flipped = {pair_list[i][0] for i in _rank.acyclic(reps, [(a, b, w) for (a, b), w in pair_list])}
    reversed_ids = {e.id for e in ranked if (head[e.a], head[e.b]) in flipped}
    oriented = [(head[e.b], head[e.a], e.minlen * factor, e.weight) if e.id in reversed_ids else (head[e.a], head[e.b], e.minlen * factor, e.weight)
                for e in ranked]
    rep_rank = _rank.rank(reps, oriented)
    rank = {n.id: rep_rank[head[n.id]] for n in nodes}

    # -- the proper graph ----------------------------------------------------------------------------------
    graph = _Graph()
    size: Dict[str, Tuple[float, float]] = {n.id: _util.frame_size(direction, n.w, n.h) for n in nodes}
    for n in nodes:
        graph.add(n.id, size[n.id][0], size[n.id][1], rank[n.id], "node", n.group)
    chains: Dict[str, List[str]] = {}
    label_key: Dict[str, str] = {}
    for e in ranked:
        low, high = (e.b, e.a) if rank[e.a] > rank[e.b] else (e.a, e.b)
        if rank[low] == rank[high]:
            flat.append(e)
            continue
        ca, cb = chain(by_id[e.a].group), chain(by_id[e.b].group)
        common = [g for g, h in zip(ca, cb) if g == h]
        dummy_group = common[-1] if common else None
        span = rank[high] - rank[low]
        middle = rank[low] + span // 2
        previous = low
        keys: List[str] = []
        for r in range(rank[low] + 1, rank[high]):
            key = "\x00d\x00{}\x00{}".format(e.id, r)
            if e.label is not None and e.label[0] > 0 and r == middle:
                lw, lh = _util.frame_size(direction, e.label[0], e.label[1])
                graph.add(key, lw, lh, r, "label", dummy_group)
                label_key[e.id] = key
            else:
                graph.add(key, 0.0, 0.0, r, "dummy", dummy_group)
            graph.link(previous, key, e.weight)
            keys.append(key)
            previous = key
        graph.link(previous, high, e.weight)
        chains[e.id] = keys
    # Group spans and their borders.
    span_of: Dict[str, Tuple[int, int]] = {}
    for n in nodes:
        for g in chain(n.group):
            lo, hi = span_of.get(g, (rank[n.id], rank[n.id]))
            span_of[g] = (min(lo, rank[n.id]), max(hi, rank[n.id]))
    borders: Dict[str, Dict[int, Tuple[str, str]]] = {}
    for g in [g.id for g in request.groups if g.id in span_of]:
        lo, hi = span_of[g]
        per: Dict[int, Tuple[str, str]] = {}
        before: Optional[Tuple[str, str]] = None
        for r in range(lo, hi + 1):
            pair = ("\x00bl\x00{}\x00{}".format(g, r), "\x00br\x00{}\x00{}".format(g, r))
            for key, side in zip(pair, ("left", "right")):
                graph.add(key, 0.0, 0.0, r, "border", g)
                graph.border_of[key] = (g, side)
            if before is not None:
                graph.link(before[0], pair[0], 1.0)
                graph.link(before[1], pair[1], 1.0)
            per[r] = pair
            before = pair
        borders[g] = per
    depth = max(graph.rank.values()) + 1
    base_layers: List[List[str]] = [[] for _ in range(depth)]
    for key in sorted(graph.rank, key=lambda k: graph.tie[k]):
        base_layers[graph.rank[key]].append(key)
    model = _order.Model(base_layers, graph.up, graph.down, graph.group, group_parent, borders, graph.tie, request.order)

    # -- wanted coordinates of an incremental run ---------------------------------------------------------------
    seeds: Dict[str, Point] = {}
    if request.incremental:
        for n in nodes:
            corner = n.pin if n.pin is not None else n.seed
            if corner is not None:
                seeds[n.id] = _util.to_frame(direction, corner[0], corner[1], size[n.id][0], size[n.id][1])
    wanted = _wanted(graph, chains, seeds) if seeds else None
    coords = {k: x + graph.w[k] / 2.0 for k, x in wanted.items()} if wanted else None
    first = _order.initial(model, [], coords)

    def place(order_layers: List[List[str]]) -> Tuple[Dict[str, float], Dict[str, float]]:
        return _place(graph, order_layers, request, rank_gap, pads, chain, wanted, seeds)

    sweeps = 0
    if seeds and _util.all_seeded(request):
        xs, ys = place(first)
        if _seeds_hold(request, graph, seeds) or \
                all(abs(xs[n] - seeds[n][0]) < 0.005 and abs(ys[n] - seeds[n][1]) < 0.005 for n in seeds):
            return _result(request, graph, first, xs, ys, chains, label_key, flat, loops, reversed_ids, notes, 0,
                           _order.cross_count(first, graph.down), fixed_point=True)
    best, crossings, sweeps = _order.improve(model, first, incremental=bool(seeds))
    xs, ys = place(best)
    # A long chain of ranks (200 nodes in a chain, drawn to the right) can run past the drawing limit: the ranks close up
    # toward ``MIN_RANK_GAP`` until it fits, or as far as they can (QA phase 2, R4).
    extent = max(ys[k] + graph.h[k] for k in ys) - min(ys.values()) if ys else 0.0
    if extent > MAX_SIZE and depth > 1 and rank_gap > MIN_RANK_GAP:
        squeezed = max(MIN_RANK_GAP, rank_gap - (extent - MAX_SIZE) / float(depth - 1))
        ys = _rank_tops(graph, best, request, squeezed, pads, chain, seeds)  # only the ranks move: along them nothing changes
        notes.append("ranks_closed_up {:g}: ranks {:g} apart instead of {:g}, to keep the drawing within {}".format(
            squeezed, squeezed, rank_gap, MAX_SIZE))
    return _result(request, graph, best, xs, ys, chains, label_key, flat, loops, reversed_ids, notes, sweeps, crossings)


def _seeds_hold(request: LayoutRequest, graph: _Graph, seeds: Mapping[str, Point]) -> bool:
    """Whether the seeds already are a layered drawing of this graph: each rank's nodes centred on one line, the ranks
    in order, nothing closer than ``gap`` in a rank, and every group's box holding only its own nodes."""
    centre: Dict[int, float] = {}
    for n, (x, y) in seeds.items():
        c = y + graph.h[n] / 2.0
        r = graph.rank[n]
        if r in centre and abs(centre[r] - c) > 0.5:
            return False
        centre.setdefault(r, c)
    ranks = sorted(centre)
    if any(centre[a] >= centre[b] for a, b in zip(ranks, ranks[1:])):
        return False
    by_rank: Dict[int, List[str]] = {}
    for n in seeds:
        by_rank.setdefault(graph.rank[n], []).append(n)
    for members in by_rank.values():
        members.sort(key=lambda n: seeds[n][0])
        for a, b in zip(members, members[1:]):
            if seeds[a][0] + graph.w[a] + request.gap > seeds[b][0] + 0.01:
                return False
    if request.groups:
        world = {n.id: _util.box_of(n.pin or n.seed, (n.w, n.h)) for n in request.nodes}  # type: ignore[arg-type]
        boxes = _util.group_boxes(request.groups, {n.id: n.group for n in request.nodes}, world)
        for g, box in boxes.items():
            for n in request.nodes:
                inside = g in _util.ancestry(request.groups, n.group)
                if not inside and _util.meets(box, world[n.id]):
                    return False
    return True


def _wanted(graph: _Graph, chains: Mapping[str, List[str]], seeds: Mapping[str, Point]) -> Dict[str, float]:
    """Wanted left edges for an incremental run: seeded nodes at their seeds, dummies between their ends, new
    nodes at the barycentre of their placed neighbours, anything else next to its rank's neighbours."""
    centre: Dict[str, float] = {k: seeds[k][0] + graph.w[k] / 2.0 for k in seeds}
    for keys in chains.values():
        if not keys:
            continue
        low = graph.up[keys[0]][0][0]
        high = graph.down[keys[-1]][0][0]
        if low in centre and high in centre:
            steps = len(keys) + 1
            for i, key in enumerate(keys, 1):
                centre[key] = centre[low] + (centre[high] - centre[low]) * i / float(steps)
    for _pass in range(3):
        for key in sorted(graph.w, key=lambda k: graph.tie[k]):
            if key in centre or graph.kind[key] == "border":
                continue
            near = [centre[w] for w, _ in graph.up[key] + graph.down[key] if w in centre]
            if near:
                centre[key] = math.fsum(near) / len(near)
    out = {k: c - graph.w[k] / 2.0 for k, c in centre.items()}
    # Whatever is still unplaced goes right of everything in its rank.
    by_rank: Dict[int, float] = {}
    for k, x in out.items():
        by_rank[graph.rank[k]] = max(by_rank.get(graph.rank[k], float("-inf")), x + graph.w[k])
    for key in sorted(graph.w, key=lambda k: graph.tie[k]):
        if key in out or graph.kind[key] == "border":
            continue
        r = graph.rank[key]
        x = by_rank.get(r, 0.0)
        x = 0.0 if x == float("-inf") else x + 40.0
        out[key] = x
        by_rank[r] = x + graph.w[key]
    return out


def _place(graph: _Graph, order_layers: List[List[str]], request: LayoutRequest, rank_gap: float,
           pads: Mapping[str, Tuple[float, float, float, float]], chain, wanted: Optional[Mapping[str, float]],
           seeds: Mapping[str, Point]) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Left edges and tops of every item in the ``down`` frame."""
    gap = request.gap

    def spacing(k: str) -> float:
        return gap / 2.0 if graph.kind[k] in ("dummy", "border") else gap

    def offset(u: str, v: str) -> float:
        bu, bv = graph.border_of.get(u), graph.border_of.get(v)
        if bu is not None:
            return pads[bu[0]][0] if bu[1] == "left" else gap
        if bv is not None:
            return pads[bv[0]][2] if bv[1] == "right" else gap
        return (spacing(u) + spacing(v)) / 2.0

    unify = {k: "\x01{}\x00{}".format(side, g) for k, (g, side) in graph.border_of.items()}
    if wanted is None:
        centres = _position.brandes_koepf(order_layers, graph.up, graph.down, graph.w,
                                          {k: graph.kind[k] in ("dummy", "border", "label") for k in graph.w},
                                          lambda u, v: graph.w[u] / 2.0 + graph.w[v] / 2.0 + offset(u, v),
                                          {k: side for k, (_g, side) in graph.border_of.items()})
        want = {k: c - graph.w[k] / 2.0 for k, c in centres.items() if graph.kind[k] != "border"}
    else:
        # Incremental: nodes hold their seeds; dummies and label spots only fill in (they never push a node).
        want = {k: x for k, x in wanted.items() if graph.kind[k] == "node"}
    xs = _position.constrain(order_layers, graph.w, want, offset, unify)
    if wanted is not None:
        _settle_soft(graph, order_layers, xs, wanted, offset)
    return xs, _rank_tops(graph, order_layers, request, rank_gap, pads, chain, seeds)


def _rank_tops(graph: _Graph, order_layers: List[List[str]], request: LayoutRequest, rank_gap: float,
               pads: Mapping[str, Tuple[float, float, float, float]], chain, seeds: Mapping[str, Point]) -> Dict[str, float]:
    """The top of every item in the ``down`` frame: its rank's top, the item centred in the rank's thickness."""
    gap = request.gap
    pinned = {n.id for n in request.nodes if n.pin is not None}
    # Ranks: as thick as their thickest item, rank_gap apart, plus the bands of groups that end or start between.
    thickness = [max((graph.h[k] for k in layer), default=0.0) for layer in order_layers]
    spans: Dict[str, Tuple[int, int]] = {}
    for k, (g, _side) in graph.border_of.items():
        lo, hi = spans.get(g, (graph.rank[k], graph.rank[k]))
        spans[g] = (min(lo, graph.rank[k]), max(hi, graph.rank[k]))
    start_extra = [0.0] * len(order_layers)
    end_extra = [0.0] * len(order_layers)
    for r, layer in enumerate(order_layers):
        for k in layer:
            groups = chain(graph.group[k])
            starts = math.fsum(pads[g][1] for g in groups if spans[g][0] == r)
            ends = math.fsum(pads[g][3] for g in groups if spans[g][1] == r)
            start_extra[r] = max(start_extra[r], starts)
            end_extra[r] = max(end_extra[r], ends)
    between = [0.0] * len(order_layers)
    for r in range(1, len(order_layers)):
        bands = end_extra[r - 1] + start_extra[r]
        between[r] = max(rank_gap, bands + gap if bands else rank_gap)
    wanted_tops: Optional[List[Optional[float]]] = None
    if seeds:
        wanted_tops = []
        for r, layer in enumerate(order_layers):
            # Seeded nodes say where their rank was; a pin says only where one node must be.
            found = [seeds[k][1] - (thickness[r] - graph.h[k]) / 2.0 for k in layer if k in seeds and k not in pinned]
            wanted_tops.append(_util.median(found) if found else None)
    tops = _position.rank_tops(thickness, between, wanted_tops)
    return {k: tops[r] + (thickness[r] - graph.h[k]) / 2.0 for r, layer in enumerate(order_layers) for k in layer}


def _settle_soft(graph: _Graph, order_layers: List[List[str]], xs: Dict[str, float], wanted: Mapping[str, float], offset) -> None:
    """Each dummy (and label spot) at its wanted place, as far as its neighbours in the rank leave room for it."""
    for layer in order_layers:
        for i, k in enumerate(layer):
            if graph.kind[k] not in ("dummy", "label") or k not in wanted:
                continue
            lo = xs[layer[i - 1]] + graph.w[layer[i - 1]] + offset(layer[i - 1], k) if i > 0 else float("-inf")
            hi = xs[layer[i + 1]] - offset(k, layer[i + 1]) - graph.w[k] if i + 1 < len(layer) else float("inf")
            if lo <= hi:
                xs[k] = min(max(wanted[k], lo), hi)


def _result(request: LayoutRequest, graph: _Graph, order_layers: List[List[str]], xs: Dict[str, float], ys: Dict[str, float],
            chains: Mapping[str, List[str]], label_key: Mapping[str, str], flat: Sequence[LEdge], loops: Sequence[LEdge],
            reversed_ids: Set[str], notes: List[str], sweeps: int, crossings: float, fixed_point: bool = False) -> LayoutResult:
    direction = request.direction
    nodes = _util.nodes_in_order(request)
    by_id = {n.id: n for n in nodes}
    position: Dict[str, Point] = {}
    for n in nodes:
        position[n.id] = _util.to_world(direction, xs[n.id], ys[n.id], graph.w[n.id], graph.h[n.id])

    def centre_of(key: str) -> Point:
        return _util.point_to_world(direction, xs[key] + graph.w[key] / 2.0, ys[key] + graph.h[key] / 2.0)

    hints: Dict[str, List[Point]] = {}
    for e in _util.edges_in_order(request):
        keys = chains.get(e.id)
        if not keys:
            continue
        points = [centre_of(k) for k in keys]
        low = graph.up[keys[0]][0][0]
        hints[e.id] = points if low == e.a else points[::-1]
    labels = {eid: centre_of(key) for eid, key in label_key.items()}
    if fixed_point:
        # The seeds already are this layout: return them exactly.
        for n in nodes:
            corner = n.pin if n.pin is not None else n.seed
            position[n.id] = (float(corner[0]), float(corner[1]))  # type: ignore[index]
        dx = dy = 0.0
    # Components: seeded runs line up with their seeds, pinned ones with their pins; the rest start at (0, 0).
    pairs = [(e.a, e.b) for e in request.edges if e.a != e.b]
    ids = [n.id for n in nodes]
    comps = _util.components(ids, pairs)
    shift: Dict[str, Point] = {}
    anchored = False
    if not fixed_point:
        world_box = _world_extent(position, by_id, request, labels, hints)
        for comp in comps:
            anchors = [n for n in comp if by_id[n].pin is None and by_id[n].seed is not None and request.incremental]
            if not anchors:
                anchors = [n for n in comp if by_id[n].pin is not None]
            if anchors:
                anchored = True
                dx = _util.median([(by_id[n].pin or by_id[n].seed)[0] - position[n][0] for n in anchors])  # type: ignore[index]
                dy = _util.median([(by_id[n].pin or by_id[n].seed)[1] - position[n][1] for n in anchors])  # type: ignore[index]
            else:
                dx, dy = -world_box[0], -world_box[1]
            for n in comp:
                shift[n] = (dx, dy)
        for n in nodes:
            sx, sy = shift[n.id]
            position[n.id] = (position[n.id][0] + sx, position[n.id][1] + sy)
        for e in request.edges:
            if e.id in hints:
                sx, sy = shift[e.a]
                hints[e.id] = [(x + sx, y + sy) for x, y in hints[e.id]]
            if e.id in labels:
                sx, sy = shift[e.a]
                labels[e.id] = (labels[e.id][0] + sx, labels[e.id][1] + sy)
    # Pins, and components that landed on each other: settle along the ranks.
    moved: List[str] = []
    if not fixed_point and (anchored or any(n.pin is not None for n in nodes)):
        fixed = {n.id: _util.box_of(n.pin, (n.w, n.h)) for n in nodes if n.pin is not None}
        for n in nodes:
            if n.pin is not None:
                position[n.id] = (float(n.pin[0]), float(n.pin[1]))
        rank_of = graph.rank
        slot = {k: i for layer in order_layers for i, k in enumerate(layer)}
        order = sorted((n.id for n in nodes if n.pin is None), key=lambda k: (rank_of[k], slot[k]))
        axes = "x" if direction in ("down", "up") else "y"
        settled, moved = _util.settle(order, position, {n.id: (n.w, n.h) for n in nodes}, fixed, request.gap, axes)
        position.update(settled)
        touched = set(moved) | set(fixed)
        for e in request.edges:
            if e.a in touched or e.b in touched:
                hints.pop(e.id, None)
                labels.pop(e.id, None)
    notes += _util.pin_overlaps(nodes)
    # Groups hug their members.
    boxes = {n.id: _util.box_of(position[n.id], (n.w, n.h)) for n in nodes}
    groups = _util.group_boxes(request.groups, {n.id: n.group for n in nodes}, boxes)
    ports: Dict[str, Tuple[str, str]] = {}
    for e in request.edges:
        if e.a == e.b:
            ports[e.id] = ("e", "n")
        elif graph.rank[e.a] == graph.rank[e.b]:
            left = xs[e.a] <= xs[e.b]
            ports[e.id] = (_util.side_to_world(direction, "e" if left else "w"), _util.side_to_world(direction, "w" if left else "e"))
        elif graph.rank[e.a] < graph.rank[e.b]:
            ports[e.id] = (_util.side_to_world(direction, "s"), _util.side_to_world(direction, "n"))
        else:
            ports[e.id] = (_util.side_to_world(direction, "n"), _util.side_to_world(direction, "s"))
    moved_count = sum(1 for n in nodes if n.seed is not None and n.pin is None and
                      max(abs(position[n.id][0] - n.seed[0]), abs(position[n.id][1] - n.seed[1])) > MOVED)
    return LayoutResult(positions=position, groups=groups, hints=hints, ports=ports, labels=labels, notes=tuple(notes),
                        stats={"crossings": float(crossings), "moved": float(moved_count), "iterations": float(sweeps)})


def _world_extent(position: Mapping[str, Point], by_id: Mapping[str, LNode], request: LayoutRequest,
                  labels: Mapping[str, Point], hints: Mapping[str, List[Point]]) -> Tuple[float, float]:
    """The top-left of everything drawn (nodes, and the group boxes around them) before any shift."""
    boxes = {n: _util.box_of(p, (by_id[n].w, by_id[n].h)) for n, p in position.items()}
    groups = _util.group_boxes(request.groups, {n: by_id[n].group for n in position}, boxes)
    xs = [b[0] for b in boxes.values()] + [b[0] for b in groups.values()]
    ys = [b[1] for b in boxes.values()] + [b[1] for b in groups.values()]
    return min(xs), min(ys)


LAYOUTS = (
    Layout(name="layers", run=layers, aliases=("flow", "layered"), groups=True, router="orthogonal", crossings=True,
           doc="layered (Sugiyama): ranks by network simplex, fewer crossings, straight long edges, groups and pins"),
)
