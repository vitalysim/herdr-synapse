"""Regressions for the canvas v2 phase 2 verification verdict (``.local/qa/phase2/verdict.md``, R1 to R7): each class
names its issue."""
from __future__ import annotations

import random
import re
import shutil
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest import mock

from registry_conformance import assert_contains_in_order, assert_discovery_order
from support import PLUGIN_ROOT
from test_canvas import OPERATOR, CanvasRig

from herdr_team import canvas as C
from herdr_team import canvas_check as K
from herdr_team import canvas_layouts as CL
from herdr_team import canvas_routers as R
from herdr_team.canvas_kinds import graph as G
from herdr_team.canvas_layouts import LEdge, LNode, LayoutRequest, _budget
from herdr_team.canvas_routers import End, RouteRequest

FIXTURES = Path(__file__).resolve().parent / "fixtures" / "layouts"


def random_graph(n, m, seed, chain=False):
    """``n`` nodes ``N0``... and ``m`` edges: a random tree (or a chain-like spine) plus random extra edges."""
    rnd = random.Random(seed)
    pairs = set()
    for i in range(1, n):
        pairs.add((max(0, i - 1 - rnd.randrange(3)) if chain else rnd.randrange(i), i))
    while len(pairs) < m:
        a, b = rnd.randrange(n), rnd.randrange(n)
        if a != b:
            pairs.add((min(a, b), max(a, b)))
    return ["N{}".format(i) for i in range(n)], ["N{} -> N{}".format(a, b) for a, b in sorted(pairs)]


def enters(points, box, inset=1.0):
    """Whether the axis-aligned polyline runs through the inside of ``box`` (shrunk by ``inset``)."""
    x0, y0, x1, y1 = box[0] + inset, box[1] + inset, box[2] - inset, box[3] - inset
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        if ax == bx and x0 < ax < x1 and min(ay, by) < y1 and y0 < max(ay, by):
            return True
        if ay == by and y0 < ay < y1 and min(ax, bx) < x1 and x0 < max(ax, bx):
            return True
    return False


class Base(CanvasRig):
    def elements(self):
        return self.scene()["elements"]

    def by_alias(self, alias):
        return next(e for e in self.elements() if e.get("alias") == alias)

    def codes(self):
        return [p["code"] for p in C.check(self.layout, self.team, "alpha-worker")["problems"]]

    def stack(self, layout, gap, names="abcd", at=(0, 0)):
        section = {"op": "section", "id": "s", "title": "Flow", "layout": layout, "at": list(at), "intent": "t"}
        if gap is not None:
            section["gap"] = gap
        self.ok(section, OPERATOR)
        for name in names:
            self.ok({"op": "card", "in": "s", "id": name, "title": "Card " + name, "intent": "t"}, OPERATOR)


# --------------------------------------------------------------------------
# R1


class R1SkipArrowsInTightStacks(Base):
    """An orthogonal arrow that skips cards in a row or column goes around every card between, at every gap, and
    ``check`` never offers a fix that changes nothing."""

    GAPS = (None, "s", "m", "l", 0, 4, 10, 16, 30)

    def test_the_route_goes_around_every_card_between_at_every_gap(self):
        for layout in ("row", "column"):
            for gap in self.GAPS:
                for label in ("", "calls it"):
                    with self.subTest(layout=layout, gap=gap, label=label):
                        self.setUp()
                        self.stack(layout, gap)
                        for a, b in (("a", "c"), ("a", "d"), ("b", "d")):
                            arrow = self.ok({"op": "arrow", "from": a, "to": b, "label": label, "route": "orthogonal", "intent": "t"},
                                            OPERATOR)["ids"][0]
                            points = [tuple(p) for p in self.el(arrow)["points"]]
                            for card in "abcd":
                                if card in (a, b):
                                    continue
                                el = self.by_alias(card)
                                self.assertFalse(enters(points, (el["x"], el["y"], el["x"] + el["w"], el["y"] + el["h"])),
                                                 (a, b, card, points))
                        self.assertNotIn("arrow_through", self.codes())

    def test_the_reported_repro_routes_around_card_b(self):
        # The verdict's repro: a row with no gap, three cards, a -> c orthogonal.
        self.stack("row", None, "abc")
        arrow = self.ok({"op": "arrow", "from": "a", "to": "c", "route": "orthogonal", "intent": "t"}, OPERATOR)["ids"][0]
        b = self.by_alias("b")
        points = self.el(arrow)["points"]
        self.assertGreater(len(points), 2, "not straight through card b")
        self.assertFalse(enters([tuple(p) for p in points], (b["x"], b["y"], b["x"] + b["w"], b["y"] + b["h"]), 0.0), points)

    def test_the_fix_check_offers_clears_what_it_reports(self):
        for layout in ("row", "column"):
            for gap in self.GAPS:
                with self.subTest(layout=layout, gap=gap):
                    self.setUp()
                    self.stack(layout, gap)
                    arrow = self.ok({"op": "arrow", "from": "a", "to": "d", "intent": "t"}, OPERATOR)["ids"][0]
                    # The arrow is the operator's: an agent is told so, with no op it could only turn into a proposal
                    # (layout findings, decision 1a), and the operator is printed the fix, which works.
                    told = [p for p in C.check(self.layout, self.team, "alpha-worker")["problems"] if p["code"] == "arrow_through"]
                    self.assertTrue(told, "a straight arrow across b and c")
                    self.assertTrue(all(p["fix"] is None and p["fix_by"] == ["the operator"] for p in told), told)
                    found = [p for p in C.check(self.layout, self.team, C.HUMAN)["problems"] if p["code"] == "arrow_through"]
                    self.assertTrue(found, "a straight arrow across b and c")
                    fixes = [p for p in found if p.get("fix")]
                    self.assertEqual(len(fixes), 1, "the same arrow is rerouted once, even when it cuts two cards")
                    repair = fixes[0]
                    for problem in found:
                        if not problem.get("fix"):
                            self.assertEqual(problem.get("fix_with"), {"code": repair["code"], "ids": repair["ids"]})
                    result = self.apply([repair["fix"]], OPERATOR)
                    self.assertEqual((result["refused"], result["proposed"]), ([], []))
                    after = [p for p in C.check(self.layout, self.team, C.HUMAN)["problems"]
                             if p["code"] == "arrow_through" and arrow in p["ids"]]
                    self.assertEqual(after, [], "following the fix clears it")

    def test_an_orthogonal_arrow_through_a_mark_gets_no_fix_that_repeats_its_route(self):
        box = {"id": "E-1", "type": "box", "x": 0, "y": 0, "w": 100, "h": 60}
        wall = {"id": "E-2", "type": "box", "x": 200, "y": -200, "w": 100, "h": 460}
        end = {"id": "E-3", "type": "box", "x": 400, "y": 0, "w": 100, "h": 60}
        arrow = {"id": "E-4", "type": "arrow", "from": "E-1", "to": "E-3", "points": [[104, 30], [396, 30]], "x": 104, "y": 30, "w": 292,
                 "h": 1}
        for route, fixed in (("orthogonal", False), ("elbow", False), ("curved", True), (None, True)):
            with self.subTest(route=route):
                el = dict(arrow, style={"route": route} if route else {})
                found = [p for p in K.problems([box, wall, end, el]) if p["code"] == "arrow_through"]
                self.assertEqual(len(found), 1)
                self.assertEqual(found[0]["fix"] is not None, fixed, found[0])
                if not fixed:
                    self.assertIn("no clear way around", found[0]["message"])

    def test_a_crowded_end_keeps_its_neighbour_an_obstacle(self):
        # Router level: ends 20 apart from the card between them; the stubs shorten instead of dropping the card.
        a = End(box=(0.0, 0.0, 240.0, 80.0), id="a")
        c = End(box=(520.0, 0.0, 760.0, 80.0), id="c")
        b = (260.0, 0.0, 500.0, 80.0)
        found = R.route("orthogonal", RouteRequest(id="e", a=a, b=c, obstacles=(("b", b, "rect"),)))
        self.assertFalse(found.blocked)
        self.assertFalse(enters(found.points, b, 0.0), found.points)


# --------------------------------------------------------------------------
# R2


class R2Budgets(Base):
    """Every layout and every graph's routing run under a budget: a deterministic work cap with a straight fallback,
    and a wall-clock ceiling behind it."""

    def test_a_budget_caps_work_and_time_and_scopes_to_its_block(self):
        budget = _budget.Budget(work=10)
        self.assertEqual((budget.left(), budget.cap(50), budget.over()), (10, 10, False))
        budget.spend(10)
        self.assertTrue(budget.over())
        self.assertEqual(budget.exhausted, "work")
        ticks = iter([0.0, 0.5, 2.0])
        timed = _budget.Budget(seconds=1.0, clock=lambda: next(ticks))
        self.assertFalse(timed.over())
        self.assertTrue(timed.over())
        self.assertEqual(timed.exhausted, "time")
        self.assertIsNone(_budget.active().work, "outside running: unlimited")
        with _budget.running(budget):
            self.assertIs(_budget.active(), budget)
            with _budget.running(_budget.Budget(work=3)) as inner:
                self.assertIs(_budget.active(), inner)
            self.assertIs(_budget.active(), budget)
        self.assertIsNone(_budget.active().work)

    def test_a_spent_budget_draws_a_route_straight_and_blocked_at_once(self):
        a, b = End(box=(0.0, 0.0, 100.0, 60.0), id="a"), End(box=(600.0, 0.0, 700.0, 60.0), id="b")
        wall = ("w", (300.0, -60.0, 340.0, 120.0), "rect")
        request = RouteRequest(id="e", a=a, b=b, obstacles=(wall,))
        self.assertFalse(R.route("orthogonal", request).blocked)
        with _budget.running(_budget.Budget(work=0)):
            spent = R.route("orthogonal", request)
        self.assertTrue(spent.blocked)
        self.assertEqual(spent.points, R.route("straight", request).points)

    def test_a_graph_past_its_routing_budget_is_noted_and_the_same_every_time(self):
        nodes, edges = random_graph(40, 70, 3)
        results = []
        with mock.patch.object(G, "ROUTE_WORK", 0), mock.patch.object(G, "ROUTE_WORK_PER_EDGE", 20):
            for _ in range(2):
                self.setUp()
                result = self.apply([{"op": "graph", "id": "g", "layout": "grid", "nodes": nodes, "edges": edges, "at": [0, 0],
                                      "intent": "t"}], OPERATOR)
                self.assertEqual(result["refused"], [])
                self.assertTrue(any("route_budget" in w["message"] for w in result["warnings"]), result["warnings"])
                results.append(sorted((e.get("part"), tuple(map(tuple, e["points"]))) for e in self.elements() if e["type"] == "arrow"))
        self.assertEqual(results[0], results[1], "the work cap, not the clock, decides what is routed")

    def test_fifty_nodes_route_fully_inside_the_budget_on_every_layout(self):
        nodes, edges = random_graph(50, 80, 100)
        spent = []
        real = _budget.Budget.__init__

        def record(budget, *args, **kwargs):
            real(budget, *args, **kwargs)
            spent.append(budget)

        for layout in ("layers", "tree", "force", "radial", "grid"):
            with self.subTest(layout=layout):
                self.setUp()
                del spent[:]
                with mock.patch.object(_budget.Budget, "__init__", record):
                    result = self.apply([{"op": "graph", "id": "g", "layout": layout, "nodes": nodes, "edges": edges, "at": [0, 0],
                                          "intent": "t"}], OPERATOR)
                self.assertEqual(result["refused"], [])
                routing = [b for b in spent if b.work is not None]
                self.assertTrue(routing)
                self.assertTrue(all(b.exhausted is None for b in routing), [(b.work, b.spent) for b in routing])
                self.assertEqual(result["check"]["counts"]["arrow_through"], 0)

    def test_a_layout_out_of_time_returns_a_valid_drawing_and_says_so(self):
        ids, pairs = random_graph(60, 120, 5)
        nodes = tuple(LNode(id=n, w=160.0, h=60.0, order=i) for i, n in enumerate(ids))
        edges = tuple(LEdge(id="e{}".format(i), a=pair.split(" -> ")[0], b=pair.split(" -> ")[1]) for i, pair in enumerate(pairs))
        for name in ("layers", "force"):
            with self.subTest(layout=name):
                result = CL.run(name, LayoutRequest(nodes=nodes, edges=edges, incremental=False), seconds=0.0)
                self.assertTrue(any(n.startswith("layout_budget") for n in result.notes), result.notes)
                self.assertEqual(set(result.positions), {n.id for n in nodes})
                boxes = [(x, y, x + 160.0, y + 60.0) for x, y in result.positions.values()]
                for i, a in enumerate(boxes):
                    for b in boxes[i + 1:]:
                        self.assertFalse(a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3], (a, b))
        self.assertFalse(any(n.startswith("layout_budget") for n in CL.run("layers", LayoutRequest(nodes=nodes, edges=edges)).notes))

    def test_a_crossings_check_runs_no_layout_that_does_not_count_crossings(self):
        calls = []
        real = CL.run

        def probe(name, request, **options):
            calls.append(name)
            return real(name, request, **options)

        nodes, edges = random_graph(30, 60, 4)
        self.apply([{"op": "graph", "id": "g", "layout": "force", "nodes": nodes, "edges": edges, "at": [0, 0], "intent": "t"}], OPERATOR)
        with mock.patch.object(CL, "run", probe):
            C.check(self.layout, self.team, "alpha-worker")
        self.assertEqual(calls, [], "force reports no crossings: nothing to compare against")
        self.assertEqual([layout.name for layout in CL.layouts() if layout.crossings], ["layers"])

    def test_two_hundred_nodes_on_rings_no_longer_take_seconds(self):
        # The verdict measured 9 to 12 s for this create; the budget holds it near half a second on the dev Mac.
        nodes, edges = random_graph(200, 400, 0)
        started = time.perf_counter()
        result = self.apply([{"op": "graph", "id": "g", "layout": "radial", "nodes": nodes, "edges": edges, "at": [0, 0], "intent": "t"}],
                            OPERATOR)
        self.assertEqual(result["refused"], [])
        self.assertLess(time.perf_counter() - started, 6.0, "lenient bound; about 0.5 s on the dev Mac, 0.7 s on 3.9")


# --------------------------------------------------------------------------
# R3


class R3TreeCrossLinks(Base):
    """A tree's curved edges, cross links included, are never drawn through a node."""

    def test_a_tree_with_cross_links_draws_no_arrow_through_a_node(self):
        nodes = ["n{}".format(i) for i in range(10)]
        tree = ["n{} -> n{}".format((i - 1) // 2, i) for i in range(1, 10)]
        links = ["n3 -> n6", "n7 -> n2", "n8 -> n5", "n4 -> n9", "n1 -> n6"]
        self.ok({"op": "graph", "id": "g", "layout": "tree", "nodes": nodes, "edges": tree + links, "at": [0, 0], "intent": "t"}, OPERATOR)
        self.assertNotIn("arrow_through", self.codes())

    def test_fifty_nodes_and_eighty_edges_on_a_tree(self):
        for seed in (100, 101, 102):
            with self.subTest(seed=seed):
                self.setUp()
                nodes, edges = random_graph(50, 80, seed)
                result = self.apply([{"op": "graph", "id": "g", "layout": "tree", "nodes": nodes, "edges": edges, "at": [0, 0],
                                      "intent": "t"}], OPERATOR)
                self.assertEqual(result["check"]["counts"]["arrow_through"], 0)

    def test_a_detour_keeps_its_curve_and_turns_in_small_arcs(self):
        route = [(0.0, 0.0), (0.0, -100.0), (300.0, -100.0), (300.0, 0.0)]
        friendly = G.curve_friendly(route, 8.0)
        self.assertEqual((friendly[0], friendly[-1]), (route[0], route[-1]))
        for corner in route[1:-1]:
            self.assertIn(corner, friendly)
        drawn = G.drawn_points(friendly, True)
        # The drawn curve never strays more than the radius from the orthogonal route.
        for x, y in drawn:
            near = min(abs(x - 0.0) if -100.0 <= y <= 0.0 else 1e9, abs(y + 100.0) if 0.0 <= x <= 300.0 else 1e9,
                       abs(x - 300.0) if -100.0 <= y <= 0.0 else 1e9)
            self.assertLessEqual(near, 8.0, (x, y))

    def test_check_reads_a_curved_arrow_along_its_curve(self):
        a = {"id": "E-1", "type": "box", "x": 0, "y": 0, "w": 100, "h": 60}
        b = {"id": "E-2", "type": "box", "x": 600, "y": 0, "w": 100, "h": 60}
        middle = {"id": "E-3", "type": "box", "x": 300, "y": 50, "w": 100, "h": 60}
        # The points run straight above the box; the curve through them bows down into it (CURVE_BOW).
        arrow = {"id": "E-4", "type": "arrow", "from": "E-1", "to": "E-2", "points": [[104, 30], [596, 30]], "x": 104, "y": 30, "w": 492,
                 "h": 1}
        straight = [p for p in K.problems([a, b, middle, arrow]) if p["code"] == "arrow_through"]
        curved = [p for p in K.problems([a, b, middle, dict(arrow, curve=True)]) if p["code"] == "arrow_through"]
        self.assertEqual((len(straight), len(curved)), (0, 1))


# --------------------------------------------------------------------------
# R4


class R4LongLayeredChains(Base):
    """A 200-node layered chain drawn to the right lays out: a path of ranks is folded into lanes, a chain that is not
    a path closes its ranks up instead, and past the documented block limit it is refused by name.

    The three chains here are deliberately different shapes. A *pure* path is now folded
    (``canvas_layouts._fold``), which is what took the corpus's long chain from 16:1 to 2:1, so it no longer runs past
    ``MAX_SIZE`` and its ranks no longer have to close up. A chain with anything else in it - a branch, two nodes in
    one rank - is still drawn as one line, and that is the shape the rank-closing and the block limit are for.
    """

    def test_a_chain_like_graph_to_the_right_lays_out(self):
        nodes, edges = random_graph(200, 400, 0, chain=True)
        result = self.apply([{"op": "graph", "id": "g", "direction": "right", "nodes": nodes, "edges": edges, "at": [0, 0], "intent": "t"}],
                            OPERATOR)
        self.assertEqual(result["refused"], [])
        root = self.by_alias("g")
        self.assertLessEqual(max(root["w"], root["h"]), C.MAX_BLOCK_SIZE)
        self.assertTrue(any("ranks_closed_up" in w["message"] for w in result["warnings"]), result["warnings"])

    def test_a_pure_chain_of_two_hundred_is_folded_into_lanes(self):
        nodes = ["N{}".format(i) for i in range(200)]
        edges = ["N{} -> N{}".format(i, i + 1) for i in range(199)]
        result = self.apply([{"op": "graph", "id": "g", "direction": "right", "nodes": nodes, "edges": edges, "at": [0, 0], "intent": "t"}],
                            OPERATOR)
        self.assertEqual(result["refused"], [])
        root = self.by_alias("g")
        self.assertLessEqual(max(root["w"], root["h"]), C.MAX_BLOCK_SIZE)
        self.assertTrue(any("shape_folded" in w["message"] for w in result["warnings"]), result["warnings"])
        self.assertLess(max(root["w"], root["h"]) / max(min(root["w"], root["h"]), 1.0), 6.0,
                        "a two-hundred-step line fits a view once it is folded")

    def test_a_chain_with_a_branch_in_it_is_still_drawn_as_one_line(self):
        """The fold is for a path of ranks. One rank holding two nodes is not one, and nothing about it changes."""
        nodes = ["N{}".format(i) for i in range(200)]
        # 199 in a line and one hanging beside rank 5: two nodes in one rank, so the ranks are not a path.
        edges = ["N{} -> N{}".format(i, i + 1) for i in range(198)] + ["N4 -> N199", "N199 -> N6"]
        result = self.apply([{"op": "graph", "id": "g", "direction": "right", "nodes": nodes, "edges": edges, "at": [0, 0], "intent": "t"}],
                            OPERATOR)
        self.assertEqual(result["refused"], [])
        root = self.by_alias("g")
        self.assertFalse(any("shape_folded" in w["message"] for w in result["warnings"]), result["warnings"])
        self.assertGreater(root["w"], C.MAX_SIZE)
        self.assertLessEqual(root["w"], C.MAX_BLOCK_SIZE)

    def test_past_the_block_limit_it_is_refused_by_name(self):
        nodes = [{"id": "N{}".format(i), "text": "a node whose label runs long enough to widen it a lot {}".format(i)} for i in range(200)]
        edges = ["N{} -> N{}".format(i, i + 1) for i in range(198)] + ["N4 -> N199", "N199 -> N6"]
        refused = self.refused({"op": "graph", "id": "g", "direction": "right", "nodes": nodes, "edges": edges, "at": [0, 0], "intent": "t"},
                               OPERATOR)
        self.assertEqual((refused["code"], refused["details"]["limit"], refused["details"]["max"]),
                         ("canvas_limit", "MAX_BLOCK_SIZE", C.MAX_BLOCK_SIZE))
        self.assertIn("split it", refused["message"])

    def test_ranks_close_up_only_past_the_limit(self):
        # Twelve nodes with one rank holding two of them: a line, and not a path of ranks, so no fold and the usual
        # rank spacing.
        nodes = tuple(LNode(id="n{}".format(i), w=160.0, h=60.0, order=i) for i in range(12)) + \
            (LNode(id="side", w=160.0, h=60.0, order=12),)
        edges = tuple(LEdge(id="e{}".format(i), a="n{}".format(i), b="n{}".format(i + 1)) for i in range(11)) + \
            (LEdge(id="s1", a="n4", b="side"), LEdge(id="s2", a="side", b="n6"))
        result = CL.run("layers", LayoutRequest(nodes=nodes, edges=edges, direction="right"))
        self.assertFalse(any(n.startswith("ranks_closed_up") for n in result.notes))
        self.assertFalse(any(n.startswith("shape_folded") for n in result.notes))
        xs = sorted(set(round(x, 2) for x, _y in result.positions.values()))
        self.assertEqual(xs[1] - xs[0], 160.0 + 80.0, "the usual rank gap")


# --------------------------------------------------------------------------
# R5


class R5AliasesLeaveWithTheNode(Base):
    def test_a_node_dragged_out_frees_its_alias_for_the_graph(self):
        self.ok({"op": "graph", "id": "g", "intent": "t", "nodes": ["A", "B", "C"], "edges": ["A -> B", "B -> C"], "at": [0, 0]})
        c = self.by_alias("g.C")
        self.apply([{"op": "move", "ids": [c["id"]], "by": [3000, 0], "frame": None, "intent": "t"}], OPERATOR)
        loose = self.el(c["id"])
        self.assertIsNone(loose.get("group"))
        self.assertFalse(loose.get("alias"), "the part's alias left with the part")
        result = self.apply([{"op": "patch", "id": "g", "add": {"nodes": ["C"], "edges": ["B -> C"]}, "intent": "t"}])
        self.assertEqual(result["refused"], [])
        again = self.by_alias("g.C")
        self.assertNotEqual(again["id"], c["id"])
        self.assertEqual(again["group"], self.by_alias("g")["id"])

    def test_a_card_dragged_out_of_a_board_frees_its_alias_too(self):
        self.ok({"op": "kanban", "id": "k", "at": [0, 0], "intent": "t", "columns": [{"id": "a", "title": "A", "cards": ["one", "two"]}]})
        card = self.by_alias("k.c1")
        self.apply([{"op": "move", "ids": [card["id"]], "by": [4000, 0], "frame": None, "intent": "t"}], OPERATOR)
        self.assertFalse(self.el(card["id"]).get("alias"))
        self.assertEqual([e["id"] for e in self.elements() if e.get("alias") == "k.c1"], [])
        self.assertEqual(self.el(card["id"])["text"], "one", "the element itself stays, loose")


# --------------------------------------------------------------------------
# R6


class R6OpenRegistries(unittest.TestCase):
    """A real new module registers wherever its ORDER puts it, and the registry tests still hold (they check the
    built-in modules are present and in order, never that nothing else is)."""

    def test_a_layout_module_that_sorts_first_keeps_the_registry_tests_green(self):
        from herdr_team import canvas_layout as L

        folder = Path(tempfile.mkdtemp(prefix="layout-first-"))
        self.addCleanup(shutil.rmtree, folder, True)
        source = (FIXTURES / "layout_diagonal.py").read_text()
        (folder / "aardvark.py").write_text(re.sub(r"(?m)^ORDER = \d+", "ORDER = 1", source).replace('name="diagonal"', 'name="aardvark"'))
        CL.__path__.append(str(folder))

        def restore():
            if str(folder) in CL.__path__:
                CL.__path__.remove(str(folder))
            sys.modules.pop("herdr_team.canvas_layouts.aardvark", None)
            CL._reload()

        self.addCleanup(restore)
        CL._reload()
        self.assertEqual(CL.modules()[0], "aardvark")
        assert_contains_in_order(self, CL.modules(), ["layers", "tree", "radial", "force", "grid", "stack"])
        assert_discovery_order(self, CL, CL.modules())
        self.assertIn("aardvark", L.LAYOUTS)


# --------------------------------------------------------------------------
# R7


class R7Notice(unittest.TestCase):
    def test_notice_names_every_bundled_third_party_asset(self):
        notice = (PLUGIN_ROOT / "NOTICE").read_text()
        assets = PLUGIN_ROOT / "assets"
        licences = sorted(p for p in assets.rglob("*") if p.is_file() and p.name in ("LICENSE", "LICENSE.txt", "OFL.txt"))
        self.assertTrue(licences)
        for path in licences:
            folder = path.parent.relative_to(PLUGIN_ROOT).as_posix() + "/"
            with self.subTest(folder=folder):
                self.assertIn(folder, notice)
                self.assertIn(path.relative_to(PLUGIN_ROOT).as_posix(), notice)
        self.assertIn("Lucide", notice)
        self.assertIn("ISC", notice)


if __name__ == "__main__":
    unittest.main()
