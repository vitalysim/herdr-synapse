"""The layered layout (canvas v2 phase 2, 2.3): cycles, ranks, same_rank, order, groups, labels, directions, pins, and the
gates on the fixture corpus: fewer crossings than Phase 1 (G7), stability (G8) and speed (G9)."""
from __future__ import annotations

import json
import time
import unittest
from pathlib import Path

from layout_conformance import boxes_of, close, crossings, grouped_sample, request_of, sample_graph

from herdr_team import canvas_layouts as CL
from herdr_team.canvas_layouts import LEdge, LGroup, LNode, LayoutRequest
from herdr_team.canvas_layouts import _order, _rank

FIXTURES = sorted((Path(__file__).resolve().parent / "fixtures" / "layouts").glob("*.json"))


def nodes(*ids, w=160.0, h=60.0):
    return tuple(LNode(n, w, h, i) for i, n in enumerate(ids))


def edges(*pairs):
    return tuple(LEdge("e{}".format(i), a, b) for i, (a, b) in enumerate(pairs))


def run(request):
    return CL.run("layers", request)


def centre_y(result, request, n):
    node = next(x for x in request.nodes if x.id == n)
    return result.positions[n][1] + node.h / 2.0


class Ranking(unittest.TestCase):
    def test_cycles_break_where_the_input_turns_back(self):
        request = LayoutRequest(nodes=nodes("start", "work", "check", "retry"),
                                edges=edges(("start", "work"), ("work", "check"), ("check", "retry"), ("retry", "start")), incremental=False)
        result = run(request)
        ys = [result.positions[n][1] for n in ("start", "work", "check", "retry")]
        self.assertEqual(ys, sorted(ys), "the retry edge is the reversed one")
        self.assertEqual(result.ports["e3"], ("n", "s"), "a reversed edge leaves up and enters from below")
        self.assertEqual(_rank.dfs_fas(["a", "b"], [("a", "b", 1.0), ("b", "a", 1.0)]), {1})

    def test_network_simplex_is_never_longer_than_longest_path(self):
        for seed in range(1, 8):
            ns, es = sample_graph(25, 20, seed)
            ids = [n.id for n in ns]
            flipped = _rank.acyclic(ids, [(e.a, e.b, 1.0) for e in es])
            oriented = [((e.b, e.a) if i in flipped else (e.a, e.b)) for i, e in enumerate(es) if e.a != e.b]
            simple = [(a, b, 1, 1.0) for a, b in oriented]
            tight = _rank.rank(ids, simple)
            loose = _rank.rank(ids, simple, simplex=False)
            for a, b, _m, _w in simple:
                self.assertGreaterEqual(tight[b] - tight[a], 1)
            total = lambda r: sum(r[b] - r[a] for a, b, _m, _w in simple)  # noqa: E731
            self.assertLessEqual(total(tight), total(loose), seed)

    def test_same_rank_shares_a_rank_and_an_edge_inside_is_flat(self):
        request = LayoutRequest(nodes=nodes("a", "b", "c", "d"), edges=edges(("a", "b"), ("b", "c"), ("a", "d"), ("c", "d")),
                                same_rank=(("b", "c"),), incremental=False)
        result = run(request)
        self.assertEqual(centre_y(result, request, "b"), centre_y(result, request, "c"))
        self.assertIn("same_rank_flat b c", result.notes)
        self.assertIn(result.ports["e1"][0], ("e", "w"))
        self.assertLess(centre_y(result, request, "b"), centre_y(result, request, "d"))

    def test_order_lists_hold_inside_a_rank(self):
        request = LayoutRequest(nodes=nodes("r", "x", "y", "z"), edges=edges(("r", "x"), ("r", "y"), ("r", "z")),
                                order=(("z", "x", "y"),), incremental=False)
        result = run(request)
        xs = [result.positions[n][0] for n in ("z", "x", "y")]
        self.assertEqual(xs, sorted(xs))

    def test_a_label_gets_room_on_the_edges_middle_rank(self):
        request = LayoutRequest(nodes=nodes("a", "b", "c"),
                                edges=(LEdge("l", "a", "b", label=(120.0, 30.0)), LEdge("p", "a", "c"), LEdge("q", "c", "b")), incremental=False)
        result = run(request)
        lx, ly = result.labels["l"]
        label = (lx - 60, ly - 15, lx + 60, ly + 15)
        for box in boxes_of(request, result.positions).values():
            self.assertFalse(close(label, box, 0))
        self.assertGreater(ly, result.positions["a"][1] + 60)
        self.assertLess(ly, result.positions["b"][1])


class OrderAndGroups(unittest.TestCase):
    def test_groups_are_contiguous_and_hold_only_their_nodes(self):
        ns, es = sample_graph(30, 25, 9)
        grouped = tuple(LNode(n.id, n.w, n.h, n.order, group="g{}".format(n.order % 3) if n.order % 4 else None) for n in ns)
        request = LayoutRequest(nodes=grouped, edges=tuple(es), groups=(LGroup("g0"), LGroup("g1"), LGroup("g2", parent="g1")),
                                incremental=False)
        result = run(request)
        boxes = boxes_of(request, result.positions)
        parents = {"g0": None, "g1": None, "g2": "g1"}
        for g, box in result.groups.items():
            for node in grouped:
                chain, cursor = set(), node.group
                while cursor is not None:
                    chain.add(cursor)
                    cursor = parents[cursor]
                if g in chain:
                    self.assertTrue(box[0] <= boxes[node.id][0] and boxes[node.id][2] <= box[2])
                else:
                    self.assertFalse(close(box, boxes[node.id], 0), (g, node.id))
        self.assertTrue(all(close(result.groups["g1"], result.groups["g2"], 0) for _ in [0]))

    def test_random_grouped_graphs_lay_out_with_groups_apart(self):
        # QA phase 2, F1: groups that swapped sides between ranks made the separation constraints cyclic (KeyError).
        for seed in range(120):
            for direction in ("down", "right"):
                request = grouped_sample(seed, direction)
                with self.subTest(seed=seed, direction=direction):
                    result = run(request)
                    boxes = boxes_of(request, result.positions)
                    for g, box in result.groups.items():
                        for node in request.nodes:
                            if node.group != g:
                                self.assertFalse(close(box, boxes[node.id], 0), (g, node.id))
                        for h, other in result.groups.items():
                            if g < h:
                                self.assertFalse(close(box, other, 0), (g, h))

    def test_sibling_groups_take_one_order_on_every_rank(self):
        model = _order.Model([["a1", "b1"], ["b2", "a2"]], {}, {}, {"a1": "A", "a2": "A", "b1": "B", "b2": "B"},
                             {"A": None, "B": None}, {}, {"a1": 0, "b1": 1, "b2": 2, "a2": 3})
        fixed = _order.reconcile(model, [["a1", "b1"], ["b2", "a2"]])
        self.assertEqual(fixed[0].index("a1") < fixed[0].index("b1"), fixed[1].index("a2") < fixed[1].index("b2"))
        consistent = [["a1", "b1"], ["a2", "b2"]]
        self.assertIs(_order.reconcile(model, consistent), consistent)

    def test_the_accumulator_tree_counts_crossings(self):
        layers = [["a", "b"], ["c", "d"]]
        down = {"a": [("d", 1.0)], "b": [("c", 1.0)]}
        self.assertEqual(_order.cross_count(layers, down), 1.0)
        self.assertEqual(_order.cross_count([["a", "b"], ["d", "c"]], down), 0.0)


class Directions(unittest.TestCase):
    def test_right_up_and_left_turn_the_down_drawing(self):
        request = LayoutRequest(nodes=nodes("a", "b", "c"), edges=edges(("a", "b"), ("b", "c")), incremental=False)
        down = run(request).positions
        right = run(LayoutRequest(nodes=request.nodes, edges=request.edges, direction="right", incremental=False)).positions
        up = run(LayoutRequest(nodes=request.nodes, edges=request.edges, direction="up", incremental=False)).positions
        left = run(LayoutRequest(nodes=request.nodes, edges=request.edges, direction="left", incremental=False)).positions
        self.assertLess(down["a"][1], down["c"][1])
        self.assertGreater(up["a"][1], up["c"][1])
        self.assertLess(right["a"][0], right["c"][0])
        self.assertGreater(left["a"][0], left["c"][0])
        self.assertEqual(run(LayoutRequest(nodes=request.nodes, edges=request.edges, direction="right", incremental=False)).ports["e0"], ("e", "w"))


class Pins(unittest.TestCase):
    def test_pins_hold_and_the_rest_clears_them(self):
        ns, es = sample_graph(20, 10, 4)
        free = run(LayoutRequest(nodes=tuple(ns), edges=tuple(es), incremental=False))
        spot = free.positions["n5"]
        pinned = tuple(LNode(n.id, n.w, n.h, n.order, pin=(spot[0] + 5.0, spot[1] + 3.0) if n.id == "n5" else None) for n in ns)
        pinned += (LNode("p", 160, 60, 99, pin=free.positions["n6"]),)
        request = LayoutRequest(nodes=pinned, edges=tuple(es), incremental=False)
        result = run(request)
        self.assertEqual(result.positions["n5"], (spot[0] + 5.0, spot[1] + 3.0))
        boxes = boxes_of(request, result.positions)
        for a in boxes:
            for b in boxes:
                if a < b and not (a in ("n5", "p") and b in ("n5", "p")):
                    self.assertFalse(close(boxes[a], boxes[b], request.gap), (a, b))

    def test_overlapping_pins_are_reported(self):
        request = LayoutRequest(nodes=(LNode("a", 100, 50, 0, pin=(0.0, 0.0)), LNode("b", 100, 50, 1, pin=(50.0, 20.0))), incremental=False)
        self.assertIn("pin_overlap a b", run(request).notes)


class FixtureGates(unittest.TestCase):
    """G7, G8 and G9 on ``tests/fixtures/layouts``: each fixture records its Phase 1 ``layered`` crossings."""

    def layout(self, fixture, **fields):
        request = request_of(fixture, **fields)
        return request, run(request)

    def test_crossings_g7(self):
        total_before = total_after = 0
        for path in FIXTURES:
            fixture = json.loads(path.read_text())
            with self.subTest(fixture=fixture["name"]):
                request, result = self.layout(fixture, incremental=False)
                sizes = {n.id: (n.w, n.h) for n in request.nodes}
                found = crossings(result.positions, sizes, [tuple(e) for e in fixture["edges"]], {int(k): v for k, v in result.hints.items()})
                self.assertLessEqual(found, fixture["phase1_crossings"])
                if fixture["kind"] in ("planar", "tree"):
                    self.assertEqual(found, 0)
                total_before += fixture["phase1_crossings"]
                total_after += found
        self.assertLessEqual(total_after, total_before * 0.7, (total_before, total_after))

    def test_one_more_node_moves_few_g8_and_the_fixed_point_is_exact(self):
        for path in FIXTURES:
            fixture = json.loads(path.read_text())
            with self.subTest(fixture=fixture["name"]):
                request, first = self.layout(fixture, incremental=False)
                seeded = tuple(LNode(n.id, n.w, n.h, n.order, seed=first.positions[n.id]) for n in request.nodes)
                again = run(LayoutRequest(nodes=seeded, edges=request.edges, incremental=True))
                self.assertEqual(again.positions, first.positions, "the fixed point")
                anchor = request.nodes[len(request.nodes) // 2].id
                grown = run(LayoutRequest(nodes=seeded + (LNode("new", 160, 60, 999),),
                                          edges=request.edges + (LEdge("new-edge", anchor, "new"),), incremental=True))
                moved = [n.id for n in request.nodes if max(abs(grown.positions[n.id][0] - first.positions[n.id][0]),
                                                           abs(grown.positions[n.id][1] - first.positions[n.id][1])) > 20]
                self.assertLessEqual(len(moved), 0.25 * len(request.nodes), moved)

    def test_speed_g9(self):
        ns, es = sample_graph(200, 201, 5, sized=True)
        started = time.perf_counter()
        run(LayoutRequest(nodes=tuple(ns), edges=tuple(es), incremental=False))
        self.assertLess(time.perf_counter() - started, 1.5)
        big_nodes, big_edges = sample_graph(500, 300, 1)
        started = time.perf_counter()
        run(LayoutRequest(nodes=tuple(big_nodes), edges=tuple(big_edges), incremental=False))
        self.assertLess(time.perf_counter() - started, 2.0, "a 500-node graph lays out in under 2 s (3.9 included)")


if __name__ == "__main__":
    unittest.main()
