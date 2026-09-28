"""The contract every registered layout keeps (canvas v2 phase 2, 2.2): a mixin a test case runs over a layout.

``LayoutConformance.check_layout(name)`` runs the seven rules on one layout;
``tests/test_canvas_layouts.py`` runs it over every registered layout, and a
dropped-in layout module is held to it the same way. ``crossings`` is the
geometric crossing count the layered fixtures record (edge pairs with four
distinct ends whose polylines, centre to centre through the hints, cross).
"""
from __future__ import annotations

import random
from typing import Dict, List, Mapping, Sequence, Tuple

from herdr_team import canvas_layouts as CL
from herdr_team.canvas_layouts import LEdge, LGroup, LNode, LayoutRequest


def _orient(a, b, c) -> int:
    value = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
    return 0 if abs(value) < 1e-9 else (1 if value > 0 else -1)


def segments_cross(p1, p2, p3, p4) -> bool:
    """A proper crossing: each segment's ends lie strictly on both sides of the other."""
    return _orient(p1, p2, p3) * _orient(p1, p2, p4) < 0 and _orient(p3, p4, p1) * _orient(p3, p4, p2) < 0


def crossings(positions: Mapping[str, Sequence[float]], sizes: Mapping[str, Sequence[float]], edges: Sequence[Tuple[str, str]],
              hints: Mapping[int, Sequence[Sequence[float]]]) -> int:
    """How many pairs of edges with four distinct ends cross, each drawn centre to centre through its hints."""
    lines = []
    for index, (a, b) in enumerate(edges):
        if a == b:
            continue
        ca = (positions[a][0] + sizes[a][0] / 2.0, positions[a][1] + sizes[a][1] / 2.0)
        cb = (positions[b][0] + sizes[b][0] / 2.0, positions[b][1] + sizes[b][1] / 2.0)
        lines.append(((a, b), [ca] + [tuple(p) for p in hints.get(index, ())] + [cb]))
    count = 0
    for i in range(len(lines)):
        for j in range(i + 1, len(lines)):
            (a1, b1), p = lines[i]
            (a2, b2), q = lines[j]
            if {a1, b1} & {a2, b2}:
                continue
            if any(segments_cross(p[k], p[k + 1], q[m], q[m + 1]) for k in range(len(p) - 1) for m in range(len(q) - 1)):
                count += 1
    return count


def request_of(fixture: Mapping, **fields) -> LayoutRequest:
    """A layout request from a ``tests/fixtures/layouts`` graph (edge ids are their indexes)."""
    nodes = tuple(LNode(id=n["id"], w=float(n["w"]), h=float(n["h"]), order=i) for i, n in enumerate(fixture["nodes"]))
    edges = tuple(LEdge(id=str(i), a=a, b=b) for i, (a, b) in enumerate(fixture["edges"]))
    return LayoutRequest(nodes=nodes, edges=edges, **fields)


def boxes_of(request: LayoutRequest, positions: Mapping[str, Sequence[float]]) -> Dict[str, Tuple[float, float, float, float]]:
    return {n.id: (positions[n.id][0], positions[n.id][1], positions[n.id][0] + n.w, positions[n.id][1] + n.h) for n in request.nodes}


def close(a, b, gap: float) -> bool:
    """Whether two boxes come closer than ``gap`` on both axes (a little slack for rounding)."""
    eps = 0.011
    return a[0] < b[2] + gap - eps and b[0] < a[2] + gap - eps and a[1] < b[3] + gap - eps and b[1] < a[3] + gap - eps


def sample_graph(count: int, extra: int, seed: int, sized: bool = True) -> Tuple[List[LNode], List[LEdge]]:
    rng = random.Random(seed)
    nodes = [LNode(id="n{}".format(i), w=float(rng.choice((120, 160, 200, 280)) if sized else 160),
                   h=float(rng.choice((40, 60, 100)) if sized else 60), order=i) for i in range(count)]
    edges = []
    for i in range(1, count):
        edges.append(LEdge(id="t{}".format(i), a="n{}".format(rng.randrange(0, i)), b="n{}".format(i)))
    for j in range(extra):
        a, b = rng.sample(range(count), 2)
        edges.append(LEdge(id="x{}".format(j), a="n{}".format(a), b="n{}".format(b)))
    return nodes, edges


def grouped_sample(seed: int, direction: str = "down") -> LayoutRequest:
    """A random small graph with up to four groups, most nodes in one (the shape of QA phase 2's group fuzz)."""
    rng = random.Random(seed)
    count, groups = rng.randrange(4, 16), rng.randrange(1, 5)
    nodes = tuple(LNode(id="n{}".format(i), w=float(rng.choice((160, 200))), h=60.0, order=i,
                        group=("g{}".format(rng.randrange(groups)) if rng.random() < 0.7 else None)) for i in range(count))
    pairs = set()
    for _ in range(rng.randrange(count - 1, 2 * count)):
        a, b = rng.randrange(count), rng.randrange(count)
        if a != b:
            pairs.add((a, b))
    edges = tuple(LEdge(id="e{}".format(k), a="n{}".format(a), b="n{}".format(b)) for k, (a, b) in enumerate(sorted(pairs)))
    return LayoutRequest(nodes=nodes, edges=edges, groups=tuple(LGroup("g{}".format(k)) for k in range(groups)), direction=direction)


class LayoutConformance:
    """Mixin for ``unittest.TestCase``: the 2.2 contract for one layout."""

    def conformance_requests(self, layout: "CL.Layout") -> List[Tuple[str, LayoutRequest]]:
        nodes, edges = sample_graph(12, 6, 7)
        cases = [
            ("one", LayoutRequest(nodes=(LNode("a", 160, 60, 0),))),
            ("pair", LayoutRequest(nodes=(LNode("a", 160, 60, 0), LNode("b", 200, 80, 1)), edges=(LEdge("e", "a", "b"),))),
            ("disconnected", LayoutRequest(nodes=tuple(LNode("d{}".format(i), 100 + 20 * i, 60, i) for i in range(5)),
                                           edges=(LEdge("e0", "d0", "d1"), LEdge("e1", "d3", "d4")))),
            ("loops and duplicates", LayoutRequest(nodes=tuple(LNode(n, 160, 60, i) for i, n in enumerate("abc")),
                                                   edges=(LEdge("1", "a", "b"), LEdge("2", "a", "b"), LEdge("3", "b", "b"),
                                                          LEdge("4", "b", "c"), LEdge("5", "c", "a")))),
            ("sized", LayoutRequest(nodes=tuple(nodes), edges=tuple(edges))),
        ]
        for direction in layout.directions:
            if direction != "down":
                cases.append(("sized " + direction, LayoutRequest(nodes=tuple(nodes), edges=tuple(edges), direction=direction)))
        if layout.groups:
            grouped = tuple(LNode(n.id, n.w, n.h, n.order, group=("g1" if n.order % 3 == 0 else "g2" if n.order % 3 == 1 else None))
                            for n in nodes)
            cases.append(("groups", LayoutRequest(nodes=grouped, edges=tuple(edges), groups=(LGroup("g1"), LGroup("g2", parent=None)))))
            nested = tuple(LNode(n.id, n.w, n.h, n.order, group=("inner" if n.order < 3 else "outer" if n.order < 6 else None)) for n in nodes)
            cases.append(("nested groups", LayoutRequest(nodes=nested, edges=tuple(edges), groups=(LGroup("outer"), LGroup("inner", parent="outer")))))
            # Random small grouped graphs whose groups want to swap sides between ranks (QA phase 2, F1).
            for seed in (5, 24, 38):
                cases.append(("random groups {}".format(seed), grouped_sample(seed)))
        return cases

    def check_layout(self, name: str) -> None:
        layout = CL.get(name)
        self.assertIsNotNone(layout, name)
        for label, request in self.conformance_requests(layout):
            with self.subTest(layout=name, case=label):
                result = CL.run(name, request)
                self.assertEqual(set(result.positions), {n.id for n in request.nodes})
                # 1. deterministic, and shuffling the input changes nothing
                self.assertEqual(result, CL.run(name, request))
                shuffled = list(request.nodes)
                random.Random(3).shuffle(shuffled)
                edges = list(request.edges)
                random.Random(4).shuffle(edges)
                again = CL.run(name, LayoutRequest(nodes=tuple(shuffled), edges=tuple(edges), groups=request.groups,
                                                   direction=request.direction, gap=request.gap, rank_gap=request.rank_gap,
                                                   options=request.options, incremental=request.incremental))
                self.assertEqual(again.positions, result.positions)
                # 3. no overlap: gap apart
                boxes = boxes_of(request, result.positions)
                ids = sorted(boxes)
                for i, a in enumerate(ids):
                    for b in ids[i + 1:]:
                        self.assertFalse(close(boxes[a], boxes[b], request.gap), (a, b, boxes[a], boxes[b]))
                # 4. fixed point
                seeded = tuple(LNode(n.id, n.w, n.h, n.order, group=n.group, seed=result.positions[n.id]) for n in request.nodes)
                fixed = CL.run(name, LayoutRequest(nodes=seeded, edges=request.edges, groups=request.groups, direction=request.direction,
                                                   gap=request.gap, rank_gap=request.rank_gap, options=request.options, incremental=True))
                self.assertEqual(fixed.positions, result.positions)
                # 5. groups hug their members plus pad and hold no other node
                members = {n.id: n.group for n in request.nodes}
                parents = {g.id: g.parent for g in request.groups}
                for g in request.groups:
                    if g.id not in result.groups:
                        continue
                    box = result.groups[g.id]
                    inside = [n for n, gid in members.items() if _within(gid, g.id, parents)]
                    self.assertTrue(inside)
                    for n in boxes:
                        if n in inside:
                            self.assertTrue(box[0] <= boxes[n][0] and box[1] <= boxes[n][1] and boxes[n][2] <= box[2] and boxes[n][3] <= box[3])
                        else:
                            self.assertFalse(close(box, boxes[n], 0), (g.id, n))
                # 7. bounded
                self.assertLessEqual(result.bbox[2] - result.bbox[0], CL.MAX_SIZE)
        # 2. pins hold exactly (layouts that hold pins)
        if layout.pins:
            nodes, edges = sample_graph(10, 5, 11)
            pinned = tuple(LNode(n.id, n.w, n.h, n.order, pin=(333.33, -120.5) if n.order == 3 else None, pin_by="human" if n.order == 3 else None)
                           for n in nodes)
            request = LayoutRequest(nodes=pinned, edges=tuple(edges))
            result = CL.run(name, request)
            self.assertEqual(result.positions["n3"], (333.33, -120.5))
            boxes = boxes_of(request, result.positions)
            for n in boxes:
                if n != "n3":
                    self.assertFalse(close(boxes[n], boxes["n3"], request.gap), (name, n))
        # 6. robust: cycles, and 200 nodes with 400 edges
        nodes, edges = sample_graph(200, 201, 5, sized=False)
        big = CL.run(name, LayoutRequest(nodes=tuple(nodes), edges=tuple(edges)))
        self.assertEqual(len(big.positions), 200)


def _within(gid, target, parents) -> bool:
    while gid is not None:
        if gid == target:
            return True
        gid = parents.get(gid)
    return False
