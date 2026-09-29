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

from dataclasses import replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from herdr_team.canvas_layouts import MAX_SIZE, LEdge, LNode, Layout, LayoutRequest, LayoutResult, Point
from herdr_team.canvas_layouts import _budget, _fold, _order, _position, _rank, _util

#: Its place in the registration order.
ORDER = 10
#: A seeded node that ends further than this from its seed counts as moved (``stats.moved``).
MOVED = 20.0
#: The closest ranks come when a drawing would otherwise run past ``MAX_SIZE`` along its ranks.
MIN_RANK_GAP = 40.0
#: Past this many nodes (or ranked edges) an incremental run does not also try the fresh order and compare the two
#: drawings: it is two more order passes and two quadratic crossing counts, and a graph that big is on a budget.
WIRE_CHOICE_MAX = 60


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


#: How much bigger a folded drawing's boxes must render for the fold to be taken (``_folded``). A fold rearranges
#: the whole drawing, so a few per cent is not worth it; a quarter again is.
FOLD_GAIN = 1.25
#: How close to the best shape a fold has to come to stay in the running once the wire is compared. Shape is not the
#: only thing a fold decides: the forty-step board of the corpus carries six edges that skip five steps each, and the
#: column count that fitted a view best put every one of those skips inside a single column, where it had to be
#: routed down past four boxes - four reversals and a route that crossed itself. One column narrower draws the same
#: skips as one straight rank-to-rank step. So the widest shape within this much of the best wins on its wire.
FOLD_SHAPE_TOL = 0.85


def layers(request: LayoutRequest) -> LayoutResult:
    """The layered layout, and then - for a graph whose ranks hold one node each - the same graph re-cut into lanes
    when that draws its boxes materially bigger (``_fold``)."""
    result, rank = _layered(request)
    return _folded(request, rank, result) or result


def _render_scale(request: LayoutRequest, result: LayoutResult) -> float:
    """How big this drawing's boxes render once it is fitted to a 16:9 view: ``min(view_w / W, view_h / H)``.

    The one number "the drawing is small" is a complaint about, and not the same number as ``screen_use``. A drawing
    wider than 16:9 is fitted by its width, so its boxes render at ``view_w / W`` whatever its height is: making it
    taller raises ``screen_use`` and enlarges nothing. Only a shorter long axis makes a box bigger, which is why the
    only pass that can answer a 16:1 drawing is one that shortens the flow.
    """
    size = {n.id: (n.w, n.h) for n in request.nodes}
    places = [(x, y, size[k][0], size[k][1]) for k, (x, y) in result.positions.items() if k in size]
    if not places:
        return 0.0
    across = max(x + w for x, _y, w, _h in places) - min(x for x, _y, _w, _h in places)
    along = max(y + h for _x, y, _w, h in places) - min(y for _x, y, _w, _h in places)
    return min(SCREEN_ASPECT / max(across, 1.0), 1.0 / max(along, 1.0))


def _folded(request: LayoutRequest, rank: Mapping[str, int], drawn: LayoutResult) -> Optional[LayoutResult]:
    """The same graph with its path of ranks re-cut into lanes, when that is a materially bigger drawing.

    A fold is only ever **chosen** on a fresh layout and only ever **kept** on an incremental one, which is what
    makes it stable: a board drawn as a line stays a line when a step is added to it, a board drawn folded stays
    folded with the new step joining the column it follows, and neither rearranges itself under its author.

    Each candidate is laid out in full and measured, rather than estimated: a column's thickness is its thickest
    box, the ranks between columns hold the labels of the steps that cross them, and no closed form for that was
    worth trusting. The recursion terminates because a folded request declares ``same_rank``, which is the first
    thing this refuses to fold again.
    """
    if request.same_rank or request.order or request.groups or not request.nodes:
        return None  # the author has arranged this drawing; a fold would be the layout overruling them
    seeds: Dict[str, Point] = {}
    heights: Dict[str, float] = {}
    if request.incremental:
        for node in request.nodes:
            corner = node.pin if node.pin is not None else node.seed
            if corner is None:
                continue
            fw, fh = _util.frame_size(request.direction, node.w, node.h)
            seeds[node.id] = _util.to_frame(request.direction, corner[0], corner[1], fw, fh)
            heights[node.id] = fh
    if seeds:
        # A drawn board keeps the fold it has, whatever the graph has become since. This is the whole of the
        # stability story and it is asked of the **seeds**, never of the graph: a step added to a folded pipeline
        # gives some rank two nodes, at which point the graph is no longer a path and a fold chosen from the graph
        # would vanish - every box back into a line, which is the churn that stopped the first attempt at this from
        # shipping. The new step joins the column its predecessor is in and nothing else moves.
        ranks = {n: rank[n] for n in seeds if n in rank}
        if len(set(ranks.values())) != len(ranks) or len(ranks) < _fold.FROM_RANKS or len(ranks) > _fold.MAX_NODES:
            return None  # two boxes already share a rank: this board was never a folded line
        found = _fold.from_seeds(sorted(ranks, key=lambda n: (ranks[n], n)), seeds, heights)
        if found is None:
            return None
        kept, _kept_rank = _layered(replace(request, same_rank=found[0], order=found[1]))
        return kept
    path = _fold.path_of_ranks(rank, {n.id: n.order for n in request.nodes})
    if path is None:
        return None
    budget = _budget.active()
    tried: List[Tuple[float, Tuple[int, float], int, LayoutResult]] = []
    pairs = [(e.a, e.b) for e in request.edges if e.a != e.b]
    sizes = [_util.frame_size(request.direction, n.w, n.h) for n in request.nodes if n.id in path]
    labelled = any(e.label is not None and e.label[0] > 0 for e in request.edges)
    hint = _fold.lanes_for(len(path), _util.median([s[0] for s in sizes]), _util.median([s[1] for s in sizes]),
                           request.gap, request.rank_gap / (2 if labelled else 1),
                           request.direction in ("down", "up"), SCREEN_ASPECT)
    for lanes, (sets, ordered) in _fold.candidates(path, pairs, hint):
        if budget.over():
            break
        found_result, _found_rank = _layered(replace(request, same_rank=sets, order=ordered))
        tried.append((_render_scale(request, found_result), _wire_cost_of(request, found_result), lanes, found_result))
    if not tried:
        return None
    was = _render_scale(request, drawn)
    top = max(score for score, _wire, _lanes, _found in tried)
    if top < was * FOLD_GAIN:
        return None
    score, _wire, lanes, result = min((entry for entry in tried if entry[0] >= top * FOLD_SHAPE_TOL),
                                      key=lambda entry: (entry[1], -entry[0], entry[2]))
    # The fold's own same-rank edges are not the author's doing, so they are not reported to them: forty steps in
    # seven lanes is thirty-three of these notes, and the one note that says what happened is ``shape_folded``.
    notes = tuple(note for note in result.notes if not note.startswith("same_rank_flat"))
    return replace(result, notes=notes + (
        "shape_folded {}: the drawing is a line of {} steps, so it is drawn as {} lanes; its boxes come out {:.0%} "
        "the size they would in one line".format(lanes, len(path), lanes, score / max(was, 1e-9)),),
                   stats=dict(result.stats, folded=float(lanes)))


def _layered(request: LayoutRequest) -> Tuple[LayoutResult, Dict[str, int]]:
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
    model = _order.Model(base_layers, graph.up, graph.down, graph.group, group_parent, borders, graph.tie, request.order,
                         [g.id for g in request.groups])

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

    # Which connected run every item belongs to, nodes and the dummy chains and label spots of their edges alike:
    # an incremental run lines each run up with its own seeds (``_place``).
    component_of: Dict[str, int] = {}
    for index, members in enumerate(_util.components([n.id for n in nodes], [(e.a, e.b) for e in edges if e.a != e.b])):
        for member in members:
            component_of[member] = index
    for e in ranked:
        for key in chains.get(e.id, ()):
            if e.a in component_of:
                component_of[key] = component_of[e.a]

    def place_with(order_layers: List[List[str]], gap: float, ranks: float) -> Tuple[Dict[str, float], Dict[str, float]]:
        return _place(graph, order_layers, replace(request, gap=gap), ranks, pads, chain, wanted, seeds, component_of,
                      wire_gap=request.gap)

    def place_fresh(order_layers: List[List[str]], gap: float, ranks: float) -> Tuple[Dict[str, float], Dict[str, float]]:
        """The same order laid out from scratch: the shape the graph *would* have, whatever the seeds say."""
        return _place(graph, order_layers, replace(request, gap=gap), ranks, pads, chain, None, {}, component_of,
                      wire_gap=request.gap)

    best, crossings, sweeps = _order.improve(model, first, incremental=bool(seeds))
    # How far apart the lanes are. A fresh layout picks the spacing that makes the drawing a shape a view can fit;
    # an incremental one recovers the spacing the board was already drawn with, from the seeds themselves. Both
    # matter: picking it fresh every time re-spread a twelve-node pipeline the moment one node was added (every box
    # moved, which is the churn the seeds exist to prevent), and ignoring it on a redraw laid the wire out against a
    # spacing the boxes do not have, which pushed them off their seeds through the separation pass.
    xs, ys = place_with(best, request.gap, rank_gap)
    lane_gap = request.gap * (_seeded_lane_scale(place_fresh, best, request, rank_gap, graph, seeds) if seeds
                              else _lane_scale(place_fresh, best, request, rank_gap, graph, notes, xs, ys))
    if lane_gap != request.gap:
        xs, ys = place_with(best, lane_gap, rank_gap)
    if not seeds and len(nodes) <= WIRE_CHOICE_MAX:
        best, xs, ys = _align_entries(place_with, best, request, graph, chains, lane_gap, rank_gap, xs, ys, notes)
    if seeds and len(nodes) <= WIRE_CHOICE_MAX and len(ranked) <= WIRE_CHOICE_MAX:
        # An incremental run derives its first order from the seeds, which is right for the boxes and can be badly
        # wrong for the wire. The boxes are held on their seeds either way, so the ordering pass's own crossing count
        # over dummy chains stops describing the picture, and it can settle in a local optimum whose long edges run
        # between the nodes where a fresh drawing would have run them past the side.
        #
        # So two more candidates are tried, both with the same node order (it must stay the seeds' order, or the
        # boxes would be pushed off their seeds): the dummy chains slotted by the depth-first walk instead of by
        # interpolation, which is where a fresh drawing starts from, and the seed order before the sweeps touched it.
        # The wire that crosses less - then, at equal crossings, the shorter one - wins. Nothing here moves a box.
        node_coords = {k: c for k, c in (coords or {}).items() if graph.kind[k] == "node"}
        hybrid, hybrid_crossings, hybrid_sweeps = _order.improve(model, _order.initial(model, [], node_coords))
        sweeps += hybrid_sweeps

        def cost_of(order_layers: List[List[str]], found_xs: Dict[str, float], found_ys: Dict[str, float]) -> Tuple[int, float]:
            """What this candidate's wire costs in the result the caller will actually get.

            Measured on the finished ``LayoutResult``, not on the frame coordinates: the component shift and the pin
            settle in ``_result`` can still move a dummy chain by a rank, and a candidate chosen on the coordinates
            before them was not always the drawing that came out (1 crossing became 2 on the nested-group sample)."""
            done = _result(request, graph, order_layers, found_xs, found_ys, chains, label_key, flat, loops,
                           reversed_ids, list(notes), sweeps, 0.0)
            return _wire_cost_of(request, done)

        cost = cost_of(best, xs, ys)
        for candidate, candidate_crossings in ((hybrid, hybrid_crossings), (first, _order.cross_count(first, graph.down))):
            found_xs, found_ys = place_with(candidate, lane_gap, rank_gap)
            found = cost_of(candidate, found_xs, found_ys)
            if found < cost:
                best, xs, ys, crossings, cost = candidate, found_xs, found_ys, candidate_crossings, found
                notes = [n for n in notes if not n.startswith("wire_rerouted")]
                notes.append("wire_rerouted: the drawing keeps every box where it was and slots its long edges the "
                             "way a fresh layout would, because the stored order's wire crossed more")
    if seeds and _util.all_seeded(request):
        # Nothing new and nothing moved: the boxes stay exactly on their seeds. The order and the wire are still the
        # improved ones, not the seed order's - a re-draw of an unchanged graph used to return the seeds with the
        # dummy chains of the *unimproved* first order, so re-issuing the same drawing kept the boxes and made the
        # wire worse (2 crossings became 9 on the conformance sample). The shortcut is about not moving boxes.
        if _seeds_hold(request, graph, seeds) or \
                all(abs(xs[n] - seeds[n][0]) < 0.005 and abs(ys[n] - seeds[n][1]) < 0.005 for n in seeds):
            return _result(request, graph, best, xs, ys, chains, label_key, flat, loops, reversed_ids, notes, sweeps,
                           crossings, fixed_point=True), rank
    # A long chain of ranks (200 nodes in a chain, drawn to the right) can run past the drawing limit: the ranks close up
    # toward ``MIN_RANK_GAP`` until it fits, or as far as they can (QA phase 2, R4).
    extent = max(ys[k] + graph.h[k] for k in ys) - min(ys.values()) if ys else 0.0
    if extent > MAX_SIZE and depth > 1 and rank_gap > MIN_RANK_GAP:
        squeezed = max(MIN_RANK_GAP, rank_gap - (extent - MAX_SIZE) / float(depth - 1))
        ys = _rank_tops(graph, best, replace(request, gap=lane_gap), squeezed, pads, chain, seeds)  # only the ranks move: along them nothing changes
        notes.append("ranks_closed_up {:g}: ranks {:g} apart instead of {:g}, to keep the drawing within {}".format(
            squeezed, squeezed, rank_gap, MAX_SIZE))
    return _result(request, graph, best, xs, ys, chains, label_key, flat, loops, reversed_ids, notes, sweeps, crossings), rank


def _wire_cost_of(request: LayoutRequest, result: LayoutResult) -> Tuple[int, float]:
    """``(crossings a reader sees, total length)`` of a finished result's wire, as ``canvas_readability`` sees it."""
    from herdr_team import canvas_readability

    drawn = canvas_readability.from_layout(request, result)
    return (int(canvas_readability.count_crossings(drawn)["crossings_seen"]),
            math.fsum(canvas_readability.poly_len(e[2]) for e in drawn.usable()))


def _wire_cost(request: LayoutRequest, graph: _Graph, chains: Mapping[str, List[str]], xs: Mapping[str, float],
               ys: Mapping[str, float]) -> Tuple[int, float]:
    """``(crossings a reader sees, total length)`` of the drawing on the lines a router will follow: the wire's cost.

    In the ``down`` frame, which is enough: a crossing survives the rotation into any direction, and a length is a
    length. These are the numbers the picture has, as opposed to the ordering pass's count over its own dummy chains
    and group borders, which stops describing the picture as soon as the boxes are held on seeds instead of placed by
    the order. Crossings first, then wire: a reader forgives a longer line sooner than a line through another line.
    """
    from herdr_team import canvas_readability

    boxes = {n.id: (xs[n.id], ys[n.id], xs[n.id] + graph.w[n.id], ys[n.id] + graph.h[n.id]) for n in request.nodes}

    def centre(key: str) -> Point:
        return xs[key] + graph.w[key] / 2.0, ys[key] + graph.h[key] / 2.0

    drawn = []
    for e in request.edges:
        if e.a == e.b or e.a not in boxes or e.b not in boxes:
            continue
        keys = chains.get(e.id) or []
        via = [centre(k) for k in keys]
        if keys and graph.up[keys[0]][0][0] != e.a:
            via = via[::-1]
        drawn.append((e.a, e.b, [centre(e.a)] + via + [centre(e.b)], None, None, e.id))
    found = canvas_readability.Drawn(boxes, drawn)
    # ``crossings_seen``, not the strict count: two wires that leave the same node and cross each other further out
    # are a crossing to the reader, and nine of the eleven on the rejected board were of exactly that kind.
    return (int(canvas_readability.count_crossings(found)["crossings_seen"]),
            math.fsum(canvas_readability.poly_len(e[2]) for e in drawn))


#: The shape a drawing is fitted to on screen. A 16:9 view fits a 16:9 drawing biggest, and every step away from it
#: costs size on both axes, so this is what "the drawing is small" is measured against.
SCREEN_ASPECT = 16.0 / 9.0
#: Past this ratio the lanes are worth spreading (below it the drawing already fits a view well enough).
BALANCE_FROM = 2.5
#: How much more wire a swap that puts an entry point on its own line may cost. A reader follows a drawing from its
#: entry points, so their first step running straight is worth a few per cent of wire - and never a crossing.
ENTRY_WIRE_SLACK = 0.12
#: The furthest an entry point is pulled onto its successors' line, in lane gaps. A nudge is a refinement; a long
#: haul is a re-ordering in disguise, and on a 30-node graph it moved every box on the next redraw.
SOURCE_PULL_MAX = 3.0
#: How much of its own density a drawing may give up to gain a better shape. Below this the lanes stop spreading.
#:
#: Chosen by looking at the pictures, not only at the number. At 0.55 the owner's flow scores better on shape (68 %
#: of a view against 53 %) and reads worse: the boxes shrink, two routes end up sharing 23 units of line instead of
#: 8, and the two-band version of the same graph becomes a good shape made mostly of the corridor between its bands.
FILL_KEEP = 0.75
#: Past this many nodes the shape is left alone: each candidate spacing is another coordinate pass, and a graph that
#: big is already against its time budget (a 500-node layout has 2 s, and six more passes do not fit in it).
BALANCE_MAX_NODES = 120
#: How wide a stretch across the flow may be left with nothing in it, as a share of the drawing's width across the
#: flow, before spreading the lanes stops. Whichever is larger: this, or the empty stretch the drawing already had.
#:
#: Density (``_fill``) was the only guard on spreading, and it is the wrong shape of guard for a drawing whose empty
#: space is all in one place. Asked for the owner's flow as two labelled bands, the lanes went to 1.5x, every unit
#: of the room went into the one corridor between the two band frames - not into the boxes - and the corridor went
#: from 27 % of the frame to 32 %, which is the owner's own complaint ("roughly the top third of the frame is
#: empty") made worse by the pass that exists to answer it. Overall density barely moved, because the corridor is a
#: small part of a big drawing, so ``_fill`` let it through.
EMPTY_BAND_KEEP = 0.22
#: How far the lanes may be spread from the spacing the theme asked for. Beyond this the drawing stops looking like
#: the rest of the canvas, which is a worse problem than a long drawing.
#:
#: Only **wider**, and only across the flow. Closing the ranks up was tried and does not hold: the rank tops of a
#: drawn board are what an incremental redraw seeds its ranks from, so a drawing laid out with closer ranks came
#: back with its ranks pushed apart again on the next redraw - the fixed point broke, which is the one thing a
#: layout may not do. And scaling the lane gap *down* put two boxes closer than ``gap``, which is the separation
#: every caller relies on. Widening the lanes has neither failure mode: it only ever adds room.
BALANCE_STEPS = (1.0, 1.25, 1.5, 2.0, 2.5, 3.0)


def _seeded_lane_scale(place_fresh, order_layers: List[List[str]], request: LayoutRequest, rank_gap: float,
                       graph: _Graph, seeds: Mapping[str, Point]) -> float:
    """Which lane spacing the seeded board was drawn with, recovered by laying each candidate out and comparing.

    A board carries its own spacing in its boxes, but not in a form that can be read off directly: the closest two
    neighbours in a rank are not reliably one lane gap apart, because coordinate assignment spreads them further to
    straighten the long edges. Reading the smallest gap and rounding it came out at 100 units for a board drawn at
    80 and the separation pass then shoved four boxes off their seeds.

    So the candidates are laid out and measured instead: the one whose drawing is the same width across the flow as
    the seeds are is the one the board was drawn with. That is exact while the graph is unchanged - which is the case
    that has to be exact, because it is the fixed point - and it is the nearest fit once a node has been added.
    """
    if not graph.rank or len(set(graph.rank.values())) < 2 or len(request.nodes) > BALANCE_MAX_NODES:
        return 1.0
    keys = [n.id for n in request.nodes if n.id in seeds]
    if len(keys) < 2:
        return 1.0
    drawn = max(seeds[k][0] + graph.w[k] for k in keys) - min(seeds[k][0] for k in keys)
    best = (float("inf"), 1.0)
    budget = _budget.active()
    for gap_scale in BALANCE_STEPS:
        if budget.over():
            break
        found_xs, _found_ys = place_fresh(order_layers, request.gap * gap_scale, rank_gap)
        placed = [k for k in keys if k in found_xs]
        if not placed:
            continue
        across = max(found_xs[k] + graph.w[k] for k in placed) - min(found_xs[k] for k in placed)
        found = abs(across - drawn)
        if found < best[0] - 1e-9:
            best = (found, gap_scale)
    return best[1]


def _lane_scale(place_fresh, order_layers: List[List[str]], request: LayoutRequest, rank_gap: float,
                graph: _Graph, notes: List[str], base_xs: Mapping[str, float],
                base_ys: Mapping[str, float]) -> float:
    """How much to spread the lanes so the drawing is a shape a screen can fit: 1.0 when it already is.

    Nothing in the pipeline bounded the drawing's shape: ``gap`` and ``rank_gap`` were fixed, so a graph only ever
    grew along the flow. The owner's flow came out 4.8:1 and the page fitted it at 51%, which is the whole of "the
    drawing is small" - every box is drawn at half the size it could have been, and the reader is the one who pays.

    Wider lanes only, never closer ranks, and never narrower lanes. Closing the ranks up was tried and does not
    hold: the rank tops of a drawn board are what an incremental redraw seeds its ranks from, so a drawing laid out
    with closer ranks came back with its ranks pushed apart again on the next redraw, and the fixed point broke,
    which is the one thing a layout may not do. Narrowing the lanes put two boxes closer than ``gap``, which is the
    separation every caller relies on. Widening has neither failure mode: it only ever adds room.

    Only a drawing that is long *along its own flow* is spread. A shape gate alone would widen a square four-node
    diamond toward 16:9 and give it 180-unit lanes, which is not what "the drawing is small" meant: the complaint is
    a graph that grew along the flow and nowhere else, and the lanes are the only room there is to give it.

    The shape is measured on a **fresh** placement of this order, never on the seeded one, so an incremental redraw
    of a board spreads its lanes exactly as the board was drawn and the same graph keeps redrawing the same way.

    The smallest scale that reaches the target wins, not the biggest gain: spreading further than the shape needs
    lengthens every cross-flow wire, and there is nothing to buy once a view fits the drawing.
    """
    if not graph.rank or len(set(graph.rank.values())) < 2 or len(request.nodes) > BALANCE_MAX_NODES:
        return 1.0
    if _flow_aspect(request, graph, base_xs, base_ys) <= BALANCE_FROM:
        return 1.0
    good = _screen_use_at(BALANCE_FROM)
    was = _screen_use(request, graph, base_xs, base_ys)
    if was >= good:
        return 1.0  # already a shape a view fits: leave it exactly as it was
    floor = _fill(request, graph, base_xs, base_ys) * FILL_KEEP
    room = max(_empty_band(request, graph, base_xs), EMPTY_BAND_KEEP)
    best = (was, 1.0)
    budget = _budget.active()
    for gap_scale in BALANCE_STEPS:
        if gap_scale == 1.0 or budget.over():
            continue
        found_xs, found_ys = place_fresh(order_layers, request.gap * gap_scale, rank_gap)
        if _fill(request, graph, found_xs, found_ys) < floor:
            break  # past here the drawing is a better shape made of more empty space
        if _empty_band(request, graph, found_xs) > room:
            break  # and past here the empty space is all in one corridor, which is what the owner objected to
        score = _screen_use(request, graph, found_xs, found_ys)
        if score > best[0] + 1e-6:
            best = (score, gap_scale)
        if score >= good:
            break
    score, gap_scale = best
    if gap_scale != 1.0:
        notes.append("shape_balanced: the lanes are {:g}x their usual spacing, so the drawing fits a view at "
                     "{:.0%} of it instead of {:.0%}".format(gap_scale, score, was))
    return gap_scale


def _flow_aspect(request: LayoutRequest, graph: _Graph, xs: Mapping[str, float], ys: Mapping[str, float]) -> float:
    """How much longer the drawing is along its flow than across it: what spreading the lanes can shorten."""
    keys = [n.id for n in request.nodes if n.id in xs]
    if not keys:
        return 1.0
    across = max(xs[k] + graph.w[k] for k in keys) - min(xs[k] for k in keys)
    along = max(ys[k] + graph.h[k] for k in keys) - min(ys[k] for k in keys)
    return along / max(across, 1.0)


def _fill(request: LayoutRequest, graph: _Graph, xs: Mapping[str, float], ys: Mapping[str, float]) -> float:
    """How much of the drawing's own box the boxes cover: the guard on spreading the lanes.

    ``screen_use`` measures the drawing's shape and nothing else, so spreading the lanes always "improves" it - a
    square of white space with eight boxes in it scores beautifully. On the two-band version of the owner's board
    that is exactly what happened: the corridor between the bands, which is the cross axis, tripled and the drawing
    became a good shape made of empty space. Density is the other half of the answer.
    """
    keys = [n.id for n in request.nodes if n.id in xs]
    if not keys:
        return 1.0
    area = math.fsum(graph.w[k] * graph.h[k] for k in keys)
    across = max(xs[k] + graph.w[k] for k in keys) - min(xs[k] for k in keys)
    along = max(ys[k] + graph.h[k] for k in keys) - min(ys[k] for k in keys)
    return area / max(across * along, 1.0)


def _empty_band(request: LayoutRequest, graph: _Graph, xs: Mapping[str, float]) -> float:
    """The widest stretch across the flow with no box in it, as a share of the drawing's width across the flow.

    Measured on the boxes alone, which is the point: the wire that will be drawn through a corridor does not make
    the corridor somewhere a reader's eye has anything to rest on, and the layout has no routes yet anyway.
    """
    keys = [n.id for n in request.nodes if n.id in xs]
    if not keys:
        return 0.0
    spans = sorted((xs[k], xs[k] + graph.w[k]) for k in keys)
    hi = max(x1 for _x0, x1 in spans)
    widest, cursor = 0.0, spans[0][0]
    for x0, x1 in spans:
        widest = max(widest, x0 - cursor)
        cursor = max(cursor, x1)
    return widest / max(hi - spans[0][0], 1.0)


def _screen_use_at(aspect: float) -> float:
    """How much of a 16:9 view a drawing of this aspect fills once it is fitted to it."""
    if aspect <= 1e-9:
        return 0.0
    return min(1.0, SCREEN_ASPECT / aspect) * min(1.0, aspect / SCREEN_ASPECT)


def _screen_use(request: LayoutRequest, graph: _Graph, xs: Mapping[str, float], ys: Mapping[str, float]) -> float:
    """How much of a 16:9 view this drawing would fill: the number "the drawing is small" is a complaint about."""
    keys = [n.id for n in request.nodes if n.id in xs]
    if not keys:
        return 1.0
    across = max(xs[k] + graph.w[k] for k in keys) - min(xs[k] for k in keys)
    along = max(ys[k] + graph.h[k] for k in keys) - min(ys[k] for k in keys)
    if request.direction in ("right", "left"):
        along, across = across, along  # the frame's rank axis is the world's x
    return _screen_use_at(across / max(along, 1.0))


# A serpentine fold for a path - nine ranks drawn as three rows, alternate rows reversed - is **not shipped**, and
# this is the record of why rather than a gap nobody wrote down. It was built and measured: it takes the nine-step
# pipeline of the readability corpus from 24.7:1 and 7% of a view to 1.1:1 and 63%, with 0 crossings, 0 misattributed
# labels and a third of the wire, which is a large win on exactly the complaint it addresses.
#
# It does not ship because it cannot be stable. A fold is a different drawing, not a nudge: the moment one more node
# joins the chain and some rank holds two nodes, no fold exists any more and every box has to go back into a line.
# The stability contract (``test_layout_layers`` G8: adding one node moves at most a quarter of the boxes) then fails
# outright - twelve of twelve boxes moved on the ``pipeline-crossed`` fixture - and a drawing that rearranges itself
# whenever its author adds a step is a worse thing to look at than a long one.
#
# What would make it shippable is recognising a folded board from its own boxes and extending the serpentine by one
# slot instead of unfolding it, which is a real piece of work and not a tuning change. Until then a long chain is
# drawn as a long chain, and ``graph_thin`` does not ship either, because a check whose fix does not work is worse
# than no check.


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
    nodes at the barycentre of their placed neighbours, anything else next to its rank's neighbours.

    Only the node entries are used as wants. A dummy's place comes from Brandes-Koepf instead (``_place``), because
    interpolating a long edge between its two ends gives a staircase where the coordinate pass gives a straight
    line; the interpolation stays because ``_order.initial`` orders a rank by these coordinates."""
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
           seeds: Mapping[str, Point], component_of: Optional[Mapping[str, int]] = None,
           wire_gap: Optional[float] = None) -> Tuple[Dict[str, float], Dict[str, float]]:
    """Left edges and tops of every item in the ``down`` frame.

    ``wire_gap`` is how much room a *wire* lane gets across the flow, which is the theme's own spacing even when the
    lanes have been spread for the shape (``_lane_scale``). Spreading exists to give the boxes room; giving the same
    multiple to every dummy lane only stretches wire, and on a banded drawing it lands entirely in the corridor
    between the bands - which is the empty space the spread was supposed to take away.
    """
    gap = request.gap
    wire = gap if wire_gap is None else min(gap, wire_gap)

    def spacing(k: str) -> float:
        return wire / 2.0 if graph.kind[k] in ("dummy", "border") else gap

    def offset(u: str, v: str) -> float:
        # A group's border keeps its pad from what is inside it and ``gap`` from what is outside it, wire included.
        #
        # Halving that clearance for wire was tried, because the corridor between two bands is made of the lanes of
        # the wire crossing it: it takes the owner's two-band board from a 188-unit corridor (27 % of the drawing) to
        # 148 (23 %), and costs more than it buys. The drawing is 3.2:1 *wide*, so its empty corridor is also the
        # only thing making its shape fit a screen at all, and a shorter corridor took it to 3.7:1 and 48 % of a
        # view from 54 %. Every unit of corridor is either empty space or shape here; there is no third thing to
        # spend it on until a band with one lane in it can be made thicker.
        bu, bv = graph.border_of.get(u), graph.border_of.get(v)
        if bu is not None:
            return pads[bu[0]][0] if bu[1] == "left" else gap
        if bv is not None:
            return pads[bv[0]][2] if bv[1] == "right" else gap
        return (spacing(u) + spacing(v)) / 2.0

    unify = {k: "\x01{}\x00{}".format(side, g) for k, (g, side) in graph.border_of.items()}
    centres = _position.brandes_koepf(order_layers, graph.up, graph.down, graph.w,
                                      {k: graph.kind[k] in ("dummy", "border", "label") for k in graph.w},
                                      lambda u, v: graph.w[u] / 2.0 + graph.w[v] / 2.0 + offset(u, v),
                                      {k: side for k, (_g, side) in graph.border_of.items()})
    straight = {k: c - graph.w[k] / 2.0 for k, c in centres.items() if graph.kind[k] != "border"}

    if wanted is None:
        want = straight
    else:
        # Incremental: the nodes hold their seeds, and the wire between them is drawn as straight as if the whole
        # graph had been laid out fresh. Interpolating a long edge's dummies between its two ends - which is what
        # this used to do - gives a staircase where Brandes-Koepf gives one straight line, so an incremental redraw
        # lost the straightening of exactly the long edges that cross the picture.
        #
        # The fresh drawing of this order is lined up with the seeds and its dummy and label spots become what the
        # fill-in aims for. The lining up is **per component**, because ``_result`` anchors each connected run on its own seeds: one
        # median over the whole drawing put the dummies of every run but one in the wrong place, which on the grouped
        # conformance sample turned 13 crossings into 44. When the seeds already are this layout every shift is 0.
        fresh = _position.constrain(order_layers, graph.w, straight, offset, unify)
        by_component: Dict[Any, List[float]] = {}
        for k in seeds:
            if k in fresh and k in wanted:
                by_component.setdefault((component_of or {}).get(k), []).append(wanted[k] - fresh[k])
        shifts = {which: _util.median(found) for which, found in by_component.items()}
        every = [d for found in by_component.values() for d in found]
        default = _util.median(every) if every else 0.0
        want = {k: x for k, x in wanted.items() if graph.kind[k] == "node"}
        aim = {k: fresh[k] + shifts.get((component_of or {}).get(k), default)
               for k in straight if graph.kind[k] != "node"}
        # A dummy asks for its straightened spot only as far as the room between its rank neighbours' own wanted
        # places: past that it would push the node after it off its seed, and adding one node to a graph then moves
        # boxes the author was not asking about.
        for layer in order_layers:
            for index, key in enumerate(layer):
                if key not in aim:
                    continue
                low = float("-inf")
                if index:
                    before = layer[index - 1]
                    low = want.get(before, aim.get(before, float("-inf"))) + graph.w[before] + offset(before, key)
                high = float("inf")
                if index + 1 < len(layer):
                    after = layer[index + 1]
                    high = want.get(after, aim.get(after, float("inf"))) - offset(key, after) - graph.w[key]
                if low <= high:
                    want[key] = min(max(aim[key], low), high)
    xs = _position.constrain(order_layers, graph.w, want, offset, unify)
    if wanted is not None:
        _settle_soft(graph, order_layers, xs, want, offset)
    _settle_sources(graph, order_layers, xs, request, offset)
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


def _align_entries(place_with, order_layers: List[List[str]], request: LayoutRequest, graph: _Graph,
                   chains: Mapping[str, List[str]], lane_gap: float, rank_gap: float, xs: Dict[str, float],
                   ys: Dict[str, float], notes: List[str]) -> Tuple[List[List[str]], Dict[str, float], Dict[str, float]]:
    """Swap an entry point past the neighbour standing between it and the line its own first step continues.

    ``_settle_sources`` can only spend the slack the order leaves, and when two nodes in one rank feed the same
    successor there is none: one of them gets the line and the other is pushed a whole lane off it. On the owner's
    board that is "Paste long URL sits above the flow it starts, with its edge reaching down and across". Which of
    the two gets the line is an *ordering* question, and the ordering pass has no reason to care - both orders cross
    the same number of times - so it is settled here, on the one thing that distinguishes them.

    A swap is kept only when it crosses no more than before, costs at most ``ENTRY_WIRE_SLACK`` more wire, and leaves
    every entry point measurably nearer its own line. Anything else is left exactly as the ordering pass left it: a
    little wire is worth paying for the first arrow of a drawing running straight, a crossing is not.
    """
    sources = _entry_offsets(request, graph, xs)
    if not sources:
        return order_layers, xs, ys
    slot = {k: (r, i) for r, layer in enumerate(order_layers) for i, k in enumerate(layer)}
    cost = _wire_cost(request, graph, chains, xs, ys)
    worst = max(offset for _node, offset in sources)
    for node, offset in sorted(sources, key=lambda item: -item[1]):
        if offset <= lane_gap / 2.0 or node not in slot:
            continue
        r, i = slot[node]
        layer = order_layers[r]
        aim = _entry_aim(request, graph, xs, node)
        if aim is None:
            continue
        step = -1 if aim < xs[node] else 1
        j = i + step
        if not 0 <= j < len(layer) or graph.kind[layer[j]] != "node":
            continue
        candidate = [list(one) for one in order_layers]
        candidate[r][i], candidate[r][j] = candidate[r][j], candidate[r][i]
        found_xs, found_ys = place_with(candidate, lane_gap, rank_gap)
        found_cost = _wire_cost(request, graph, chains, found_xs, found_ys)
        found_worst = max([o for _n, o in _entry_offsets(request, graph, found_xs)] or [0.0])
        if found_cost[0] <= cost[0] and found_cost[1] <= cost[1] * (1.0 + ENTRY_WIRE_SLACK) and found_worst < worst - 1.0:
            notes.append("entry_aligned {}: it swapped places with {} so its first step runs straight".format(
                node, layer[j]))
            order_layers, xs, ys, cost, worst = candidate, found_xs, found_ys, found_cost, found_worst
            slot = {k: (rr, ii) for rr, one in enumerate(order_layers) for ii, k in enumerate(one)}
    return order_layers, xs, ys


def _entry_aim(request: LayoutRequest, graph: _Graph, xs: Mapping[str, float], node: str) -> Optional[float]:
    """The left edge an entry point wants: the median of its direct successors' centres, less half its own width."""
    successors = [e.b for e in request.edges if e.a == node and e.b != node and e.b in xs]
    if not successors:
        return None
    return _util.median([xs[t] + graph.w[t] / 2.0 for t in successors]) - graph.w[node] / 2.0


def _entry_offsets(request: LayoutRequest, graph: _Graph, xs: Mapping[str, float]) -> List[Tuple[str, float]]:
    """Each entry point and how far it sits off the line its own first step continues."""
    indegree = {n.id: 0 for n in request.nodes}
    for e in request.edges:
        if e.a != e.b:
            indegree[e.b] = indegree.get(e.b, 0) + 1
    out: List[Tuple[str, float]] = []
    for n in request.nodes:
        if indegree.get(n.id, 0) or n.pin is not None or n.id not in xs:
            continue
        aim = _entry_aim(request, graph, xs, n.id)
        if aim is not None:
            out.append((n.id, abs(aim - xs[n.id])))
    return out


def _settle_sources(graph: _Graph, order_layers: List[List[str]], xs: Dict[str, float], request: LayoutRequest,
                    offset) -> None:
    """Each entry point pulled onto the line its own first step continues, as far as its rank leaves room.

    The owner's "Paste long URL sits above the flow it starts": that node is not *behind* its flow, it is beside it,
    128 units off the line of its own first successor, so the arrow that starts the drawing has to reach down and
    across before it can go anywhere. Nothing in the pipeline was looking at that.

    Ordering is not touched: it decides who is beside whom, and this only spends the slack the rank already leaves
    between the neighbours the order chose, so it can neither introduce a crossing nor break the separation rule.
    Asking for the move *before* the separation pass instead was measured and is worse: ``constrain`` only ever
    pushes right, so a leftward aim was ignored and a rightward one shoved the whole rank along, which took the
    two-batch board from 1 crossing to 3.

    What it cannot fix, and the metric records rather than hides: when two nodes in one rank feed the same
    successor, only one of them can sit on its line. On the owner's board ``paste`` and ``counter`` both feed the
    API node, so ``paste`` ends 91 units off instead of 128 - better, and not 0.

    A pinned node is left out, because a pin is the operator's placement and holding it is the whole point.

    It runs on an incremental drawing as well as a fresh one, and it has to. A pass that only runs on one of them
    makes the two disagree: the fresh drawing pulls its entry point onto the line, the redraw leaves it where the
    separation pass puts it, the box drifts back, and the median its whole component is anchored on moves with it -
    which moved all thirty boxes of the 30-node conformance fixture when one node was added. The aim is the same on
    both paths (the median of the successors, which are on their seeds), so the box lands where it already is.
    """
    pinned = {n.id for n in request.nodes if n.pin is not None}
    indegree = {n.id: 0 for n in request.nodes}
    successors: Dict[str, List[str]] = {n.id: [] for n in request.nodes}
    for e in request.edges:
        if e.a == e.b:
            continue
        indegree[e.b] = indegree.get(e.b, 0) + 1
        successors[e.a].append(e.b)
    slot = {k: (r, i) for r, layer in enumerate(order_layers) for i, k in enumerate(layer)}
    for n in request.nodes:
        if indegree.get(n.id, 0) or n.id in pinned or not successors[n.id] or n.id not in xs:
            continue
        where = slot.get(n.id)
        centres = [xs[t] + graph.w[t] / 2.0 for t in successors[n.id] if t in xs]
        if where is None or not centres:
            continue
        r, i = where
        layer = order_layers[r]
        aim = _util.median(centres) - graph.w[n.id] / 2.0
        lo = xs[layer[i - 1]] + graph.w[layer[i - 1]] + offset(layer[i - 1], n.id) if i > 0 else float("-inf")
        hi = xs[layer[i + 1]] - offset(n.id, layer[i + 1]) - graph.w[n.id] if i + 1 < len(layer) else float("inf")
        if lo <= hi:
            found = min(max(aim, lo), hi)
            if abs(found - xs[n.id]) <= SOURCE_PULL_MAX * max(request.gap, 1.0):
                xs[n.id] = found


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
