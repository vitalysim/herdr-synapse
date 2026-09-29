"""Readability of a drawn graph, as numbers: the definition behind "unclear".

Why this module exists. The owner looked at a board their own agents drew and
rejected it on sight - long detours, wire crossing wire, labels adrift, a third
of the frame empty - while ``canvas check`` called the same board clean. A
layout a person calls unreadable that the feedback loop calls fine is the bug
behind the bug, so readability stopped being an opinion and became one measured
quantity with one definition, in one place, used by three callers:

* ``canvas_kinds.graph.route_edges`` asks ``edge_quality`` whether a stored
  route is still worth keeping (legal is not the same as good);
* ``graph.py``'s ``crossings_high``, ``routes_tangled`` and ``labels_adrift``
  checks report on ``measure`` so the agent is told what a reader sees;
* ``tests/layout_conformance.py`` and ``tests/router_conformance.py`` gate the
  pipeline on ``measure`` over a committed corpus of boards.

One definition means the gate and the check can never drift apart. They differ
only in threshold: the gate holds the pipeline to what it can achieve, the check
must not cry wolf on a board a person would call fine.

Every number here goes **down** when the picture gets clearer, except
``ink_fill``, ``bbox_fill``, ``screen_use`` and ``monotone``, which go up. Each
metric's own comment says why that direction is the clear one.

Pure geometry over what is actually on the board - the node boxes and the edge
polylines as the router left them - so the same code measures a stored scene, a
layout result before routing, and a live block. Pure Python, stdlib only, 3.9;
nothing here imports ``canvas``, a kind or a renderer.

Cost. ``measure`` is O(E^2) in segment pairs and is never on a render or a parse
path; its callers bound it with ``MAX_NODES`` and ``MAX_EDGES`` and memoise per
block version. ``edge_quality`` is O(1) in one route's own points and is the
only variant the router's keep decision calls, once per edge.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

Point = Tuple[float, float]
Box = Tuple[float, float, float, float]  # (x0, y0, x1, y1)
#: One drawn edge: the two node ids it joins, its polyline, its label centre and pill size, and a name for messages.
Edge = Tuple[str, str, Sequence[Point], Optional[Point], Optional[Tuple[float, float]], str]

#: Past this many nodes a caller does not measure a whole board (the crossing scan is quadratic).
MAX_NODES = 80
#: Past this many edges a caller does not measure a whole board; ``edge_quality`` is still cheap per edge.
MAX_EDGES = 200
#: Two routes that share a node cross "where a reader sees it" only this far out from that node: closer than this the
#: two wires simply leave the same box, which nobody reads as a crossing.
SHARED_CLEAR = 24.0
#: Two axis-aligned pieces this close on their shared coordinate are drawn on top of each other.
OVERLAP_TOL = 6.0
#: Two axis-aligned pieces this close read as one thick line rather than two wires.
BUNDLE_TOL = 16.0
#: A route passing this near a box it does not join looks like it touches it.
NEAR_NODE = 12.0
#: The aspect a drawing is fitted to on screen (16:9): ``screen_use`` measures against it.
SCREEN_ASPECT = 16.0 / 9.0


# --------------------------------------------------------------------------
# geometry


def seg_len(a: Sequence[float], b: Sequence[float]) -> float:
    return math.hypot(b[0] - a[0], b[1] - a[1])


def poly_len(points: Sequence[Sequence[float]]) -> float:
    return math.fsum(seg_len(points[i], points[i + 1]) for i in range(len(points) - 1))


def _orient(a: Sequence[float], b: Sequence[float], c: Sequence[float]) -> int:
    value = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return 0 if abs(value) < 1e-9 else (1 if value > 0 else -1)


def segments_cross(p1: Sequence[float], p2: Sequence[float], p3: Sequence[float], p4: Sequence[float]) -> bool:
    """A proper crossing: each piece's ends lie strictly on both sides of the other. Touching is not crossing."""
    return (_orient(p1, p2, p3) * _orient(p1, p2, p4) < 0
            and _orient(p3, p4, p1) * _orient(p3, p4, p2) < 0)


def cut_point(p1: Sequence[float], p2: Sequence[float], p3: Sequence[float],
              p4: Sequence[float]) -> Optional[Point]:
    """Where two crossing pieces meet, or None when they are parallel."""
    d1 = (p2[0] - p1[0], p2[1] - p1[1])
    d2 = (p4[0] - p3[0], p4[1] - p3[1])
    den = d1[0] * d2[1] - d1[1] * d2[0]
    if abs(den) < 1e-9:
        return None
    t = ((p3[0] - p1[0]) * d2[1] - (p3[1] - p1[1]) * d2[0]) / den
    return p1[0] + d1[0] * t, p1[1] + d1[1] * t


def point_to_seg(p: Sequence[float], a: Sequence[float], b: Sequence[float]) -> float:
    dx, dy = b[0] - a[0], b[1] - a[1]
    n = dx * dx + dy * dy
    if n < 1e-12:
        return math.hypot(p[0] - a[0], p[1] - a[1])
    t = max(0.0, min(1.0, ((p[0] - a[0]) * dx + (p[1] - a[1]) * dy) / n))
    return math.hypot(p[0] - a[0] - t * dx, p[1] - a[1] - t * dy)


def point_to_poly(p: Sequence[float], points: Sequence[Sequence[float]]) -> float:
    if len(points) < 2:
        return math.inf if not points else math.hypot(p[0] - points[0][0], p[1] - points[0][1])
    return min(point_to_seg(p, points[i], points[i + 1]) for i in range(len(points) - 1))


def point_to_box(p: Sequence[float], box: Sequence[float]) -> float:
    """How far ``p`` lies outside ``box`` (0 inside it)."""
    dx = max(box[0] - p[0], 0.0, p[0] - box[2])
    dy = max(box[1] - p[1], 0.0, p[1] - box[3])
    return math.hypot(dx, dy)


def box_gap(a: Sequence[float], b: Sequence[float]) -> float:
    """Centre to centre between two boxes: the span an edge between them has to cover."""
    return math.hypot((a[0] + a[2]) / 2.0 - (b[0] + b[2]) / 2.0, (a[1] + a[3]) / 2.0 - (b[1] + b[3]) / 2.0)


def seg_in_box(a: Sequence[float], b: Sequence[float], box: Sequence[float], inset: float = 2.0) -> bool:
    """Whether the piece from ``a`` to ``b`` enters the inside of ``box`` (Liang-Barsky against the inset box)."""
    x0, y0, x1, y1 = box[0] + inset, box[1] + inset, box[2] - inset, box[3] - inset
    if x1 <= x0 or y1 <= y0:
        return False
    px, py = a[0], a[1]
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for d, lo, hi, p in ((dx, x0, x1, px), (dy, y0, y1, py)):
        if abs(d) < 1e-12:
            if p < lo or p > hi:
                return False
            continue
        a0, a1 = (lo - p) / d, (hi - p) / d
        if a0 > a1:
            a0, a1 = a1, a0
        t0, t1 = max(t0, a0), min(t1, a1)
        if t0 > t1:
            return False
    return True


def overlap_len(a: Sequence[float], b: Sequence[float], c: Sequence[float], d: Sequence[float],
                tol: float = OVERLAP_TOL) -> float:
    """The length over which two axis-aligned pieces are collinear within ``tol``: wire drawn on wire."""
    ah, bh = abs(a[1] - b[1]) < 1e-6, abs(c[1] - d[1]) < 1e-6
    av, bv = abs(a[0] - b[0]) < 1e-6, abs(c[0] - d[0]) < 1e-6
    if ah and bh and abs(a[1] - c[1]) <= tol:
        lo = max(min(a[0], b[0]), min(c[0], d[0]))
        hi = min(max(a[0], b[0]), max(c[0], d[0]))
        return max(0.0, hi - lo)
    if av and bv and abs(a[0] - c[0]) <= tol:
        lo = max(min(a[1], b[1]), min(c[1], d[1]))
        hi = min(max(a[1], b[1]), max(c[1], d[1]))
        return max(0.0, hi - lo)
    return 0.0


def median(values: Sequence[float]) -> float:
    if not values:
        return 0.0
    s = sorted(values)
    n = len(s)
    return float(s[n // 2]) if n % 2 else (s[n // 2 - 1] + s[n // 2]) / 2.0


def kendall(order: Sequence[Any], drawn: Sequence[Any]) -> int:
    """How many pairs two orderings disagree about (Kendall's distance): 0 when the drawing reads in declared order."""
    place = {key: index for index, key in enumerate(drawn)}
    kept = [place[key] for key in order if key in place]
    return sum(1 for i in range(len(kept)) for j in range(i + 1, len(kept)) if kept[i] > kept[j])


# --------------------------------------------------------------------------
# the drawing under the microscope


class Drawn:
    """One drawn graph: node boxes by id, edges as polylines, and the frame and bands they were drawn in.

    ``bands`` are a graph's groups (their boxes), ``band_of`` says which band each node is in, and
    ``band_order`` is the order the author declared them in - the three together are what makes
    "the two flows read as two lanes" measurable.
    """

    def __init__(self, nodes: Mapping[str, Box], edges: Sequence[Edge], frame: Optional[Box] = None,
                 direction: str = "right", bands: Optional[Mapping[str, Box]] = None,
                 band_of: Optional[Mapping[str, Optional[str]]] = None,
                 band_order: Sequence[str] = (), names: Optional[Mapping[str, str]] = None) -> None:
        self.nodes: Dict[str, Box] = {k: tuple(float(v) for v in box) for k, box in nodes.items()}  # type: ignore[misc]
        self.edges: List[Edge] = list(edges)
        self.frame = tuple(float(v) for v in frame) if frame is not None else None
        self.direction = direction
        self.bands: Dict[str, Box] = dict(bands or {})
        self.band_of: Dict[str, Optional[str]] = dict(band_of or {})
        self.band_order: Tuple[str, ...] = tuple(band_order)
        #: What to call each node in a message: its own text where it has one, else its id.
        self.names: Dict[str, str] = dict(names or {})

    def name(self, nid: str) -> str:
        return self.names.get(nid) or nid

    @property
    def axis(self) -> int:
        """0 when the flow runs along x (``right``/``left``), 1 when it runs along y."""
        return 0 if self.direction in ("right", "left") else 1

    @property
    def sign(self) -> float:
        """+1 when the flow advances toward larger coordinates."""
        return 1.0 if self.direction in ("right", "down") else -1.0

    def usable(self) -> List[Edge]:
        """The edges that can be measured: two known ends and a polyline of at least two points."""
        return [e for e in self.edges if len(e[2]) >= 2 and e[0] in self.nodes and e[1] in self.nodes]


# --------------------------------------------------------------------------
# one route, on its own: the keep decision


def _reversals(points: Sequence[Sequence[float]]) -> int:
    """How often the route turns back on an axis it was already travelling: the wire that doubles back.

    A route with 0 reversals is monotone on both axes, which is what a reader can follow with one sweep of the eye.
    """
    signs = {0: 0, 1: 0}
    count = 0
    for k in range(len(points) - 1):
        for ax in (0, 1):
            delta = points[k + 1][ax] - points[k][ax]
            if abs(delta) < 1e-6:
                continue
            s = 1 if delta > 0 else -1
            if signs[ax] and signs[ax] != s:
                count += 1
            signs[ax] = s
    return count


def _bends(points: Sequence[Sequence[float]]) -> int:
    """How many corners the route turns: every one is a place the eye has to re-acquire the line."""
    count = 0
    for k in range(1, len(points) - 1):
        v1 = (points[k][0] - points[k - 1][0], points[k][1] - points[k - 1][1])
        v2 = (points[k + 1][0] - points[k][0], points[k + 1][1] - points[k][1])
        if abs(v1[0] * v2[1] - v1[1] * v2[0]) > 1e-6:
            count += 1
    return count


def edge_quality(points: Sequence[Sequence[float]], a_box: Sequence[float], b_box: Sequence[float],
                 label_at: Optional[Sequence[float]] = None,
                 others: Sequence[Sequence[float]] = ()) -> Dict[str, float]:
    """What one route costs a reader, without looking at any other route.

    This is the variant ``graph.route_edges`` calls per edge to decide whether a **stored** polyline is still worth
    keeping. A stored route that is merely legal - orthogonal, touching both ends, clear of every box - can still be
    twice as long as it needs to be, turn back on itself seven times and carry its label nearer a third node than to
    either of its own ends. Those are the four numbers here:

    * ``mdetour``: drawn length over the Manhattan span of its own two end points. An orthogonal route cannot do
      better than 1.00, so this is the route's own yardstick and needs no comparison with anything else.
    * ``reversals``: axis turn-backs (``_reversals``).
    * ``bends``: corners (``_bends``).
    * ``label_misattributed``: 1 when the pill is nearer a box in ``others`` than to either end's box - the "cached
      floats in mid-air next to the wrong node" symptom - else 0. ``others`` are the boxes of the nodes this edge does
      **not** join.

    ``length`` and ``span`` come back too, for the message a check prints.
    """
    pts = [(float(p[0]), float(p[1])) for p in points]
    out: Dict[str, float] = {"mdetour": 0.0, "reversals": 0.0, "bends": 0.0, "label_misattributed": 0.0,
                             "length": 0.0, "span": 0.0}
    if len(pts) < 2:
        return out
    length = poly_len(pts)
    need = abs(pts[-1][0] - pts[0][0]) + abs(pts[-1][1] - pts[0][1])
    out["length"] = length
    out["span"] = need
    out["mdetour"] = (length / need) if need > 1e-6 else math.inf
    out["reversals"] = float(_reversals(pts))
    out["bends"] = float(_bends(pts))
    if label_at is not None:
        own = min(point_to_box(label_at, a_box), point_to_box(label_at, b_box))
        out["label_own_dist"] = own
        nearest = min((point_to_box(label_at, box) for box in others), default=math.inf)
        out["label_misattributed"] = 1.0 if nearest < own - 1e-9 else 0.0
    return out


# --------------------------------------------------------------------------
# crossings, on their own (the cheapest question, and the one asked most)


def count_crossings(g: "Drawn", edges: Optional[Sequence[Edge]] = None) -> Dict[str, Any]:
    """``crossings`` and ``crossings_seen``, with the worst pairs named.

    ``crossings`` counts pairs of edges with four distinct ends whose drawn lines cross. That is the number a layered
    layout's ordering pass minimises, and on the owner's board it was 2 - which is why nothing complained.

    ``crossings_seen`` is the number a reader counts. Two wires that leave the same node and then cross each other
    ``SHARED_CLEAR`` units further out are a crossing to the eye, whatever the algorithm's bookkeeping says; nine of
    the eleven crossings on the rejected board were of exactly that kind, and every one of them was being skipped.
    """
    boxes = g.nodes
    edges = list(edges if edges is not None else g.usable())
    strict = 0
    seen = 0
    pairs: List[Tuple[str, str]] = []
    seen_pairs: List[Tuple[str, str]] = []
    spans = []
    for e in edges:
        pts = e[2]
        spans.append((min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)))
    for i in range(len(edges)):
        for j in range(i + 1, len(edges)):
            bi, bj = spans[i], spans[j]
            if bj[0] > bi[2] or bi[0] > bj[2] or bj[1] > bi[3] or bi[1] > bj[3]:
                continue  # the boxes do not meet: most pairs on a laid-out graph
            ea, eb = edges[i], edges[j]
            shared = {ea[0], ea[1]} & {eb[0], eb[1]}
            p, q = ea[2], eb[2]
            hit_strict = False
            hit_seen = False
            for k in range(len(p) - 1):
                for t in range(len(q) - 1):
                    if not segments_cross(p[k], p[k + 1], q[t], q[t + 1]):
                        continue
                    if not shared:
                        hit_strict = hit_seen = True
                        break
                    at = cut_point(p[k], p[k + 1], q[t], q[t + 1])
                    if at is not None and all(point_to_box(at, boxes[s]) > SHARED_CLEAR for s in shared):
                        hit_seen = True
                        break
                if hit_seen:
                    break
            if hit_strict:
                strict += 1
                pairs.append((ea[5], eb[5]))
            if hit_seen:
                seen += 1
                seen_pairs.append((ea[5], eb[5]))
    return {"crossings": float(strict), "crossing_pairs": pairs,
            "crossings_seen": float(seen), "crossings_seen_pairs": seen_pairs}


# --------------------------------------------------------------------------
# the keep decision: legal is not the same as good


#: How far outside a box a route's end point may sit and still count as touching it (``arrow.TOUCH``).
TOUCH = 5.0
#: What a stored route must still be worth to be kept rather than cut again.
#:
#: These are deliberately looser than what a fresh route achieves (the pipeline draws 1.00 detours with 0 or 1
#: reversals), because the point of keeping a stored route is stability: a wire that is a little longer than ideal is
#: better than a wire that moves every time a neighbour changes. They are tight enough to reject the five routes that
#: survived from the six-node era into the owner's ten-edge board, which is what made it unreadable.
KEEP_MDETOUR = 1.40
KEEP_REVERSALS = 1
KEEP_BENDS = 8


def route_legal(points: Sequence[Sequence[float]], a_box: Sequence[float], b_box: Sequence[float],
                obstacles: Sequence[Sequence[float]]) -> bool:
    """Whether a stored orthogonal route is still *legal*: axis-aligned, touching both ends, clear of every box.

    This is ``canvas_kinds.arrow._still_good`` stated over boxes rather than elements, for a caller that has no
    elements (the conformance harnesses). It is the test that was doing all the work before this module existed, and
    the whole point of ``route_good`` is that passing it is not enough.
    """
    pts = [(float(p[0]), float(p[1])) for p in points]
    if len(pts) < 2:
        return False
    for (ax, ay), (bx, by) in zip(pts, pts[1:]):
        if abs(ax - bx) > 1e-6 and abs(ay - by) > 1e-6:
            return False
    for point, box in ((pts[0], a_box), (pts[-1], b_box)):
        inside_x = box[0] - TOUCH <= point[0] <= box[2] + TOUCH
        inside_y = box[1] - TOUCH <= point[1] <= box[3] + TOUCH
        if not (inside_x and inside_y) or (box[0] + 1 < point[0] < box[2] - 1 and box[1] + 1 < point[1] < box[3] - 1):
            return False
    for box in obstacles:
        x0, y0, x1, y1 = (float(v) for v in box[:4])
        for (ax, ay), (bx, by) in zip(pts, pts[1:]):
            if min(ax, bx) < x1 and x0 < max(ax, bx) and min(ay, by) < y1 and y0 < max(ay, by):
                return False
            if (ax == bx and x0 < ax < x1 and min(ay, by) < y1 and y0 < max(ay, by)) or \
                    (ay == by and y0 < ay < y1 and min(ax, bx) < x1 and x0 < max(ax, bx)):
                return False
    return True


def route_good(points: Sequence[Sequence[float]], a_box: Sequence[float], b_box: Sequence[float],
               label_at: Optional[Sequence[float]] = None,
               others: Sequence[Sequence[float]] = ()) -> bool:
    """Whether a stored route is still worth keeping: legality is the caller's question, quality is this one.

    A route the graph re-draws around is kept only while it is short for the gap it spans, does not turn back on
    itself, does not wander through many corners, and still carries its label nearer its own ends than to any third
    node. Everything else falls through and is cut again with the rest of the batch. Five routes on the owner's board
    passed the legality test and failed every one of these.
    """
    q = edge_quality(points, a_box, b_box, label_at, others)
    return (q["mdetour"] <= KEEP_MDETOUR + 1e-9 and q["reversals"] <= KEEP_REVERSALS
            and q["bends"] <= KEEP_BENDS and not q["label_misattributed"])


# --------------------------------------------------------------------------
# the whole drawing


def measure(g: Drawn) -> Dict[str, Any]:
    """Every readability number for one drawn graph.

    The scalar entries are the metric; the ``*_worst``, ``*_pairs`` and ``*_detail`` entries are the evidence a check
    quotes in its message ("worst: create x lookup"), and are never gated. Ordered the way the complaint reads:
    crossings, detours, bends, labels, wire on ink, fill and shape, reading direction, bands, entry points.
    """
    boxes = g.nodes
    ids = sorted(boxes)
    edges = g.usable()
    m: Dict[str, Any] = {"nodes": float(len(boxes)), "edges": float(len(edges))}

    # 1. crossings (``count_crossings``): the strict count the ordering pass minimises, and the count a reader sees.
    m.update(count_crossings(g, edges))

    # 2. detours. ``detour`` measures against the straight centre-to-centre line (what the reader expects to see);
    # ``mdetour`` measures against the Manhattan span of the route's own ends, which is the best an orthogonal route
    # can do, so 1.00 is achievable and anything above it is wire the drawing did not need.
    ratios: List[float] = []
    lengths: List[float] = []
    detour_worst: List[Tuple[float, str, int, int]] = []
    mratios: List[Tuple[float, str, int, int]] = []
    revs: List[Tuple[int, str]] = []
    bends: List[Tuple[int, str]] = []
    for a, b, pts, _lat, _ls, name in edges:
        length = poly_len(pts)
        span = box_gap(boxes[a], boxes[b])
        ratios.append(length / span if span > 1e-6 else math.inf)
        lengths.append(length)
        detour_worst.append((round(ratios[-1], 2), name, int(round(length)), int(round(span))))
        need = abs(pts[-1][0] - pts[0][0]) + abs(pts[-1][1] - pts[0][1])
        mratios.append((round(length / need, 2) if need > 1e-6 else math.inf, name, int(round(length)), int(round(need))))
        revs.append((_reversals(pts), name))
        bends.append((_bends(pts), name))
    m["length_total"] = float(round(math.fsum(lengths)))
    m["detour_median"] = round(median(ratios), 2)
    m["detour_max"] = round(max(ratios), 2) if ratios else 0.0
    m["detour_worst"] = sorted(detour_worst, reverse=True)[:5]
    m["mdetour_median"] = median([r for r, _n, _l, _s in mratios])
    m["mdetour_max"] = max([r for r, _n, _l, _s in mratios]) if mratios else 0.0
    m["mdetour_over_1_5"] = float(sum(1 for r, _n, _l, _s in mratios if r > 1.5))
    m["mdetour_worst"] = sorted(mratios, reverse=True)[:5]
    m["reversals_max"] = float(max([n for n, _ in revs])) if revs else 0.0
    m["reversals_total"] = float(sum(n for n, _ in revs))
    m["reversals_worst"] = sorted(revs, reverse=True)[:5]

    # 3. bends: a route the eye has to follow round many corners.
    m["bends_median"] = median([n for n, _ in bends])
    m["bends_max"] = float(max([n for n, _ in bends])) if bends else 0.0
    m["bends_over_2"] = float(sum(1 for n, _ in bends if n > 2))
    m["bends_worst"] = sorted(bends, reverse=True)[:5]

    # 4. labels. Three separate failures: ``astray`` is a pill off its own line (the router must never do this);
    # ``orphan`` is a pill that sits far from both ends of its arrow as a fraction of the span (it reads as belonging
    # to nothing); ``misattributed`` is a pill nearer some third node than to either of its own ends (it reads as
    # belonging to the wrong thing, which is worse than belonging to nothing).
    astray: List[Tuple[float, str]] = []
    orphan: List[Tuple[float, str, int]] = []
    ambiguous: List[Tuple[float, str, str]] = []
    wrong: List[Tuple[str, str, int]] = []
    for a, b, pts, lat, _ls, name in edges:
        if lat is None:
            continue
        own = point_to_poly(lat, pts)
        astray.append((round(own, 1), name))
        near_end = min(point_to_box(lat, boxes[a]), point_to_box(lat, boxes[b]))
        span = box_gap(boxes[a], boxes[b])
        orphan.append((round(near_end / span, 2) if span > 1e-6 else 0.0, name, int(round(near_end))))
        best: Optional[Tuple[float, str]] = None
        for a2, b2, p2, _l2, _s2, n2 in edges:
            if n2 == name:
                continue
            d = point_to_poly(lat, p2)
            if best is None or d < best[0]:
                best = (d, n2)
        if best is not None:
            ambiguous.append((round(best[0] - own, 1), name, best[1]))
        nearest = min(boxes, key=lambda n: point_to_box(lat, boxes[n]))
        if nearest not in (a, b):
            wrong.append((name, g.name(nearest), int(round(point_to_box(lat, boxes[nearest])))))
    m["label_astray_max"] = max([d for d, _ in astray]) if astray else 0.0
    m["label_astray_over_4"] = float(sum(1 for d, _ in astray if d > 4.0))
    m["label_orphan_median"] = median([r for r, _n, _d in orphan])
    m["label_orphan_max"] = max([r for r, _n, _d in orphan]) if orphan else 0.0
    m["label_orphan_worst"] = sorted(orphan, reverse=True)[:5]
    m["label_crowded"] = float(sum(1 for d, _n, _o in ambiguous if d < 24.0))
    m["label_crowded_worst"] = sorted(ambiguous)[:5]
    m["label_misattributed"] = float(len(wrong))
    m["label_misattributed_worst"] = wrong[:6]

    # 5. wire over ink: a route through a node it does not join, past one it does not join, or drawn along another
    # route (two wires the eye reads as one) - and the softer version, running parallel close enough to bundle.
    over: List[Tuple[str, str]] = []
    near: List[Tuple[str, str]] = []
    for a, b, pts, _lat, _ls, name in edges:
        for nid, box in boxes.items():
            if nid in (a, b):
                continue
            if any(seg_in_box(pts[k], pts[k + 1], box) for k in range(len(pts) - 1)):
                over.append((name, g.name(nid)))
            grown = (box[0] - NEAR_NODE, box[1] - NEAR_NODE, box[2] + NEAR_NODE, box[3] + NEAR_NODE)
            if any(seg_in_box(pts[k], pts[k + 1], grown, inset=0.0) for k in range(len(pts) - 1)):
                near.append((name, g.name(nid)))
    m["edge_over_node"] = float(len(over))
    m["edge_over_node_worst"] = over[:6]
    m["edge_near_node"] = float(len(near))
    m["edge_near_node_worst"] = near[:8]
    shared_len = 0.0
    bundle = 0.0
    for i in range(len(edges)):
        for j in range(i + 1, len(edges)):
            p, q = edges[i][2], edges[j][2]
            for k in range(len(p) - 1):
                for t in range(len(q) - 1):
                    shared_len += overlap_len(p[k], p[k + 1], q[t], q[t + 1])
                    bundle += overlap_len(p[k], p[k + 1], q[t], q[t + 1], tol=BUNDLE_TOL)
    m["edge_on_edge_len"] = float(round(shared_len))
    m["parallel_bundle_len"] = float(round(bundle))

    # 6. fill and shape. An extreme aspect is why "the drawing is small": the page fits the content to the view, so a
    # 24:1 chain draws every box at a twentieth of the height it could have had. ``screen_use`` is the share of a 16:9
    # view the content covers once fitted, and it is the number the owner's "the drawing is small" actually names.
    content: List[Box] = [boxes[i] for i in ids]
    for e in edges:
        pts = e[2]
        content.append((min(p[0] for p in pts), min(p[1] for p in pts), max(p[0] for p in pts), max(p[1] for p in pts)))
    if content:
        cx0 = min(b[0] for b in content)
        cy0 = min(b[1] for b in content)
        cx1 = max(b[2] for b in content)
        cy1 = max(b[3] for b in content)
    else:
        cx0 = cy0 = cx1 = cy1 = 0.0
    node_area = math.fsum((boxes[i][2] - boxes[i][0]) * (boxes[i][3] - boxes[i][1]) for i in ids)
    frame = g.frame or (cx0, cy0, cx1, cy1)
    frame_area = max((frame[2] - frame[0]) * (frame[3] - frame[1]), 1.0)
    m["frame_aspect"] = round((frame[2] - frame[0]) / max(frame[3] - frame[1], 1.0), 2)
    m["ink_fill"] = round(node_area / frame_area, 3)
    aspect = (cx1 - cx0) / max(cy1 - cy0, 1.0)
    m["content_aspect"] = round(aspect, 2)
    m["screen_use"] = round(min(1.0, SCREEN_ASPECT / aspect) * min(1.0, aspect / SCREEN_ASPECT), 3) if aspect > 1e-9 else 0.0
    m["bbox_fill"] = round(((cx1 - cx0) * (cy1 - cy0)) / frame_area, 3)
    m["node_bbox"] = (round(cx0), round(cy0), round(cx1), round(cy1))
    # How much of that fitted view the **boxes** cover, which is the one shape number that cannot be bought with
    # empty space, and so the one that means "the drawing is small".
    #
    # ``screen_use`` can be. A drawing wider than 16:9 is fitted by its width, so its boxes render at exactly
    # ``view_width / content_width`` whatever its height is: adding empty space across the flow raises
    # ``screen_use`` and enlarges nothing. That is not a hypothetical - it is how the two-band form of the owner's
    # own board scored 0.64 with a corridor taking a third of it, against 0.54 with the corridor they did not
    # object to. ``screen_ink`` is ``screen_use`` times the share of the content box the boxes fill, which cancels
    # the padding exactly: it goes up only when the content box itself gets shorter along its long axis.
    m["screen_ink"] = round(m["screen_use"] * node_area / max((cx1 - cx0) * (cy1 - cy0), 1.0), 4)
    # The widest strip across the flow that the drawing does not use: the owner's "roughly the top third of the frame
    # is empty while the bottom is crowded".
    #
    # What counts as used is the node boxes, the label pills, and the edge pieces that **cross** the strip. A piece
    # that runs along the flow at one coordinate does not: a reader cannot read anything off a line, so a lane of
    # parallel wire is still empty space as far as this number is concerned. A piece that runs across the strip does:
    # that space is carrying the connection between two parts of the drawing. Counting the boxes alone made the
    # two-band drawing look half-empty when what filled the gap was seven cross-band wires; counting every piece
    # made every board read as full.
    across = 1 if g.direction in ("right", "left") else 0
    lo, hi = frame[across], frame[across + 2]
    spans = [(boxes[i][across], boxes[i][across + 2]) for i in ids]
    for e in edges:
        pts = e[2]
        for k in range(len(pts) - 1):
            a, b = pts[k][across], pts[k + 1][across]
            if abs(a - b) > 1e-6:
                spans.append((min(a, b), max(a, b)))
        if e[3] is not None and e[4] is not None:
            half = float(e[4][1] if across else e[4][0]) / 2.0
            spans.append((e[3][across] - half, e[3][across] + half))
    gap, at = 0.0, None
    cursor = lo
    for s0, s1 in sorted(spans) + [(hi, hi)]:
        if s0 - cursor > gap:
            gap, at = s0 - cursor, (cursor, s0)
        cursor = max(cursor, s1)
    m["empty_band"] = round(gap / max(hi - lo, 1.0), 3)
    m["empty_band_at"] = at
    # The same strip counting boxes only. A corridor a wire crosses is doing a job and ``empty_band`` is right to
    # call it used; a reader still has nothing to *read* in it, and when a corridor takes a third of the drawing
    # that is what the owner means by "roughly the top third of the frame is empty". Reported next to
    # ``empty_band`` rather than instead of it, because the two answer different questions and the corridor between
    # two separated components is honest space where the corridor inside one flow is not.
    widest, cursor = 0.0, lo
    for s0, s1 in sorted((boxes[i][across], boxes[i][across + 2]) for i in ids):
        widest = max(widest, s0 - cursor)
        cursor = max(cursor, s1)
    m["empty_band_boxes"] = round(max(widest, hi - cursor) / max(hi - lo, 1.0), 3)
    # An edge whose own extent covers most of the frame along the flow: the wire that crosses the whole picture.
    flow_len = max(frame[2] - frame[0] if g.direction in ("right", "left") else frame[3] - frame[1], 1.0)
    axis = g.axis
    spanning = [(round((max(p[axis] for p in e[2]) - min(p[axis] for p in e[2])) / flow_len, 2), e[5]) for e in edges]
    m["edge_span_max"] = max([s for s, _ in spanning]) if spanning else 0.0
    m["edge_span_over_half"] = float(sum(1 for s, _ in spanning if s > 0.5))
    m["edge_span_worst"] = sorted(spanning, reverse=True)[:5]

    # 7. reading direction: the share of edges that advance the way the drawing says it does. Goes up.
    sign = g.sign
    forward = 0
    for a, b, _pts, _lat, _ls, _n in edges:
        ca = (boxes[a][axis] + boxes[a][axis + 2]) / 2.0
        cb = (boxes[b][axis] + boxes[b][axis + 2]) / 2.0
        if (cb - ca) * sign > 1.0:
            forward += 1
    m["monotone"] = round(forward / max(len(edges), 1), 2)
    m["backward_edges"] = float(len(edges) - forward)

    # 8. components: two disconnected flows must not share cross-axis space, or they read as one tangled drawing.
    m.update(_components(g, edges))

    # 9. bands (a graph's groups): do they read as lanes, in the order the author declared, and do cross-band wires
    # stay out of the lanes they do not belong to.
    if g.bands:
        m.update(_bands(g, edges))

    # 10. entry points: a source has to sit at the head of its own flow, on the line its first step continues.
    m.update(_entries(g, edges))
    return m


def _components(g: Drawn, edges: Sequence[Edge]) -> Dict[str, Any]:
    """``component_interleave``: pairs of disconnected components whose spans across the flow overlap.

    Two flows that do not meet must not be drawn through each other. Today's contract only forbids node overlap,
    which lets two components share cross-axis space and read as one tangled picture.
    """
    boxes = g.nodes
    parent = {i: i for i in boxes}

    def find(n: str) -> str:
        while parent[n] != n:
            parent[n] = parent[parent[n]]
            n = parent[n]
        return n

    for a, b, _pts, _lat, _ls, _n in edges:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra
    groups: Dict[str, List[str]] = {}
    for n in sorted(boxes):
        groups.setdefault(find(n), []).append(n)
    cross = g.axis ^ 1
    spans = []
    for members in groups.values():
        spans.append((min(boxes[n][cross] for n in members), max(boxes[n][cross + 2] for n in members)))
    bad = 0
    for i in range(len(spans)):
        for j in range(i + 1, len(spans)):
            if spans[i][0] < spans[j][1] - 1e-6 and spans[j][0] < spans[i][1] - 1e-6:
                bad += 1
    return {"components": float(len(groups)), "component_interleave": float(bad)}


def _bands(g: Drawn, edges: Sequence[Edge]) -> Dict[str, Any]:
    """Band metrics: declared order against drawn order, wires through a foreign lane, and each band's own lanes."""
    boxes = g.nodes
    axis, cross = g.axis, g.axis ^ 1
    out: Dict[str, Any] = {"band_count": float(len(g.bands))}
    inside = sum(1 for a, b, _p, _l, _s, _n in edges if g.band_of.get(a) == g.band_of.get(b))
    out["band_internal_edges"] = float(inside)
    through: List[Tuple[str, str]] = []
    for a, b, pts, _lat, _ls, name in edges:
        own = {g.band_of.get(a), g.band_of.get(b)}
        for bid, box in g.bands.items():
            if bid in own:
                continue
            if any(seg_in_box(pts[k], pts[k + 1], box) for k in range(len(pts) - 1)):
                through.append((name, bid))
    out["edge_through_other_band"] = float(len(through))
    out["edge_through_other_band_worst"] = through[:6]
    lanes = []
    for bid in sorted(g.bands):
        members = [n for n, b in g.band_of.items() if b == bid and n in boxes]
        if len(members) < 2:
            continue
        along = len({round((boxes[n][axis] + boxes[n][axis + 2]) / 2.0) for n in members})
        across = len({round((boxes[n][cross] + boxes[n][cross + 2]) / 2.0) for n in members})
        lanes.append((bid, along, across))
    out["band_lanes"] = lanes
    # Declared against drawn: the bands sorted by where they sit across the flow.
    drawn = [bid for bid, _box in sorted(g.bands.items(), key=lambda item: item[1][cross] * g.sign)]
    declared = [bid for bid in g.band_order if bid in g.bands] or drawn
    out["band_order"] = float(kendall(declared, drawn))
    out["band_order_drawn"] = drawn
    return out


def _entries(g: Drawn, edges: Sequence[Edge]) -> Dict[str, Any]:
    """Entry-point metrics: a source drawn behind its own flow, and how far off its first step's line it sits.

    ``entry_cross_offset_max`` is the owner's "Paste long URL sits above the flow it starts": the source is not
    behind the flow, it is beside it, so the arrow that starts the drawing has to reach down and across.
    """
    boxes = g.nodes
    axis, cross = g.axis, g.axis ^ 1
    sign = g.sign
    ids = sorted(boxes)
    indeg = {i: 0 for i in ids}
    outs: Dict[str, List[str]] = {i: [] for i in ids}
    for a, b, _pts, _lat, _ls, _n in edges:
        indeg[b] = indeg.get(b, 0) + 1
        outs[a].append(b)
    sources = [i for i in ids if indeg.get(i, 0) == 0]
    detail: List[Tuple[str, bool, int]] = []
    for s in sources:
        seen, stack = {s}, [s]
        while stack:
            n = stack.pop()
            for t in outs.get(n, ()):
                if t not in seen:
                    seen.add(t)
                    stack.append(t)
        reach = [i for i in seen if i != s]
        if not reach:
            continue
        own = (boxes[s][axis] + boxes[s][axis + 2]) / 2.0
        ahead = min((boxes[i][axis] + boxes[i][axis + 2]) / 2.0 * sign for i in reach)
        behind = own * sign > ahead + 1.0
        firsts = outs.get(s) or []
        off = 0.0
        if firsts:
            c0 = (boxes[s][cross] + boxes[s][cross + 2]) / 2.0
            c1 = median([(boxes[t][cross] + boxes[t][cross + 2]) / 2.0 for t in firsts])
            off = abs(c1 - c0)
        detail.append((s, behind, int(round(off))))
    return {"sources": [s for s in sources],
            "entry_offside": float(sum(1 for _s, behind, _o in detail if behind)),
            "entry_cross_offset_max": float(max([o for _s, _b, o in detail])) if detail else 0.0,
            "entry_detail": detail}


def layout_stats(g: Drawn) -> Dict[str, float]:
    """The four figures a layout can answer about its own drawing, before any wire is routed.

    A layout places boxes and leaves bend hints; the router draws the wire. These are the numbers that are already
    decided once the boxes are placed - how many edges cross, whether the entry points sit on the reading line, what
    shape the drawing is, and whether the bands read in declared order - so they belong in ``LayoutResult.stats``
    where a caller can gate a layout without running a router. ``crossings`` here is the **geometric** count of the
    drawn lines, which is what ``crossings_high`` compares a stored drawing against; the ordering pass's own count
    over its dummy chains stays separate, under ``layer_crossings``.

    Deliberately not the whole of ``measure``: this runs inside ``canvas_layouts.run``, on the arrange path, so it
    buys only what is cheap and leaves the quadratic label and bundle scans to the callers that want them.
    """
    out = {"crossings": float(count_crossings(g)["crossings"])}
    out["entry_cross_offset_max"] = float(_entries(g, g.usable())["entry_cross_offset_max"])
    boxes = g.nodes
    if boxes:
        x0 = min(b[0] for b in boxes.values())
        y0 = min(b[1] for b in boxes.values())
        x1 = max(b[2] for b in boxes.values())
        y1 = max(b[3] for b in boxes.values())
        for e in g.usable():
            x0 = min(x0, min(p[0] for p in e[2]))
            y0 = min(y0, min(p[1] for p in e[2]))
            x1 = max(x1, max(p[0] for p in e[2]))
            y1 = max(y1, max(p[1] for p in e[2]))
        aspect = (x1 - x0) / max(y1 - y0, 1.0)
        out["content_aspect"] = round(aspect, 2)
        out["screen_use"] = round(min(1.0, SCREEN_ASPECT / aspect) * min(1.0, aspect / SCREEN_ASPECT), 3) if aspect > 1e-9 else 0.0
        area = math.fsum((b[2] - b[0]) * (b[3] - b[1]) for b in boxes.values())
        out["screen_ink"] = round(out["screen_use"] * area / max((x1 - x0) * (y1 - y0), 1.0), 4)
    if g.bands:
        cross = g.axis ^ 1
        drawn = [bid for bid, _box in sorted(g.bands.items(), key=lambda item: item[1][cross] * g.sign)]
        declared = [bid for bid in g.band_order if bid in g.bands] or drawn
        out["band_order"] = float(kendall(declared, drawn))
    return out


def length_ratio(build: Mapping[str, Any], fresh: Mapping[str, Any]) -> float:
    """How much more wire this drawing uses than a fresh one-op layout of the same graph.

    The headline gate is self-normalising on purpose: no absolute wire length can be a bound across eight boards of
    different sizes, but "this build against a fresh layout of the same graph" is comparable everywhere, and it is
    exactly what went wrong on the rejected board (1.68: two thirds more wire than the same graph needs).
    """
    base = float(fresh.get("length_total") or 0.0)
    if base <= 1e-6:
        return 1.0
    return round(float(build.get("length_total") or 0.0) / base, 3)


# --------------------------------------------------------------------------
# building a ``Drawn`` from what the callers have


def from_scene(scene: Mapping[str, Any], frame_id: str) -> Drawn:
    """The drawing of the graph block whose root element is ``frame_id``, from a stored scene."""
    elements = list(scene.get("elements") or ())
    root = next((e for e in elements if e.get("id") == frame_id), None)
    members = [e for e in elements if e.get("group") == frame_id]
    if root is None:
        raise KeyError(frame_id)
    return from_block(root, members)


def from_block(root: Mapping[str, Any], members: Sequence[Mapping[str, Any]]) -> Drawn:
    """The drawing of one graph block: its member node boxes, its arrows' polylines and its groups as bands.

    ``root`` is the block's frame element and ``members`` its members (``group == root["id"]``), which is exactly
    what a kind's ``checks`` and ``arrange`` already hold.
    """
    rid = str(root.get("id"))
    nodes: Dict[str, Box] = {}
    edges: List[Edge] = []
    bands: Dict[str, Box] = {}
    band_of: Dict[str, Optional[str]] = {}
    names: Dict[str, str] = {}
    for el in members:
        if el.get("group") != rid:
            continue
        eid = str(el.get("id"))
        box = _el_box(el)
        if el.get("type") == "frame" and not el.get("block"):
            bands[eid] = box
            continue
        if el.get("type") == "arrow":
            a, b = el.get("from"), el.get("to")
            if a is None or b is None:
                continue
            points = [(float(p[0]), float(p[1])) for p in (el.get("points") or ()) if len(p) >= 2]
            label_at = tuple(float(v) for v in el["label_at"]) if el.get("label_at") else None
            edges.append((str(a), str(b), points, label_at, None,  # type: ignore[arg-type]
                          (el.get("item") or {}).get("label") or el.get("text") or eid))
            continue
        if el.get("part"):
            nodes[eid] = box
            names[eid] = str((el.get("item") or {}).get("text") or el.get("text") or el.get("title") or eid)
            band_of[eid] = str(el.get("frame")) if isinstance(el.get("frame"), str) else None
    direction = (root.get("settings") or {}).get("direction") or "down"
    order = [str(i) for i in (root.get("order") or ()) if str(i) in bands]
    return Drawn(nodes, edges, _el_box(root), direction, bands, band_of, order or sorted(bands), names)


def _el_box(el: Mapping[str, Any]) -> Box:
    x, y = float(el.get("x") or 0.0), float(el.get("y") or 0.0)
    return x, y, x + float(el.get("w") or 0.0), y + float(el.get("h") or 0.0)


def from_layout(request: Any, result: Any) -> Drawn:
    """The drawing a layout result describes, with each edge as the polyline through its own hints.

    Before routing there is no wire, so this is the picture the layout is responsible for on its own: ranks, ordering
    and coordinate assignment. It is what ``LayoutResult.stats`` reports and what a layout can be gated on without
    running a router (``crossings``, ``entry_cross_offset_max``, ``content_aspect``, ``band_order``).
    """
    sizes = {n.id: (float(n.w), float(n.h)) for n in request.nodes}
    nodes: Dict[str, Box] = {}
    for nid, (x, y) in result.positions.items():
        w, h = sizes.get(nid, (0.0, 0.0))
        nodes[nid] = (float(x), float(y), float(x) + w, float(y) + h)
    edges: List[Edge] = []
    for e in request.edges:
        if e.a == e.b or e.a not in nodes or e.b not in nodes:
            continue
        ca = ((nodes[e.a][0] + nodes[e.a][2]) / 2.0, (nodes[e.a][1] + nodes[e.a][3]) / 2.0)
        cb = ((nodes[e.b][0] + nodes[e.b][2]) / 2.0, (nodes[e.b][1] + nodes[e.b][3]) / 2.0)
        via = [(float(p[0]), float(p[1])) for p in result.hints.get(e.id, ())]
        label_at = result.labels.get(e.id)
        edges.append((e.a, e.b, [ca] + via + [cb],
                      (float(label_at[0]), float(label_at[1])) if label_at is not None else None,
                      (float(e.label[0]), float(e.label[1])) if e.label is not None else None, e.id))
    bands = {gid: tuple(float(v) for v in box) for gid, box in (result.groups or {}).items()}
    band_of = {n.id: n.group for n in request.nodes}
    declared = [g.id for g in getattr(request, "groups", ())]
    return Drawn(nodes, edges, None, request.direction, bands, band_of, declared)  # type: ignore[arg-type]
