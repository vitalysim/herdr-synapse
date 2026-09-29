"""Readability: the metric, the keep decision, the repair, and the three checks that can see them.

Canvas v2 layout clarity (`.local/prd/canvas-v2-layout-clarity.md`). The owner
rejected a drawing their own agents made and `canvas check` called it clean, so
this module holds the two halves of that apart:

* ``canvas_readability`` is the definition - what "unclear" is, as numbers -
  and it is tested here on shapes whose answers can be worked out by hand;
* the graph kind's ``route_edges`` keep decision, the ``relayout`` repair and
  the ``crossings_high``/``routes_tangled``/``labels_adrift`` checks are tested
  against drawings built through the real canvas, because that is where the
  owner met them.

``tests/layout_conformance.py`` holds the corpus gates over the layout and the
router; this module holds what only the whole pipeline can answer.
"""
from __future__ import annotations

import copy
import unittest

import layout_conformance as conformance
import test_canvas
from test_kind_graph import GraphRig

from herdr_team import canvas_check as K
from herdr_team import canvas_readability as RD
from herdr_team.canvas_kinds import arrow as A
from herdr_team.canvas_kinds import graph as G

#: The owner's own board, as the agent wrote it: a nine-node flow with two entry points and ten labelled edges.
FLOW = {"op": "graph", "id": "shortener", "title": "How a link shortener works", "layout": "flow",
        "direction": "right", "route": "orthogonal", "at": [0, 0], "intent": "the board the owner rejected",
        "nodes": [{"id": "paste", "text": "Paste long URL"}, {"id": "api", "text": "Shortener API"},
                  {"id": "store", "text": "Store URL mapping"}, {"id": "short", "text": "Return short link"},
                  {"id": "open", "text": "Open short link"}, {"id": "redirect", "text": "Redirect to original URL"},
                  {"id": "cache", "text": "Fast lookup cache"}, {"id": "counter", "text": "Click counter"}],
        "edges": ["paste -> api: create", "api -> store: save", "store -> short: short link",
                  "open -> api: lookup", "api -> redirect: original address", "store -> cache: populate",
                  "open -> cache: cached", "cache -> api: hit", "open -> counter: track", "counter -> api: logged"]}
#: The same board as it was really built: six nodes and five edges first, then the rest. This is the shape that made
#: the picture the owner rejected - the first batch's routes survived into the second.
FLOW_FIRST = dict(FLOW, nodes=FLOW["nodes"][:6], edges=FLOW["edges"][:5])


class Metric(unittest.TestCase):
    """``canvas_readability`` on shapes whose answers can be checked by hand."""

    def test_a_route_that_goes_the_long_way_round_says_so(self):
        # Two boxes 200 apart, joined by a route that runs 100 the wrong way first: 400 drawn for a 200 span.
        q = RD.edge_quality([(0.0, 0.0), (-100.0, 0.0), (-100.0, 0.0), (300.0, 0.0)], (-20.0, -20.0, 0.0, 20.0),
                            (300.0, -20.0, 320.0, 20.0))
        self.assertAlmostEqual(q["mdetour"], 500.0 / 300.0, places=6)
        self.assertEqual(q["reversals"], 1.0)
        self.assertEqual(q["bends"], 0.0)

    def test_a_straight_route_is_the_yardstick(self):
        q = RD.edge_quality([(0.0, 0.0), (300.0, 0.0)], (-20.0, -20.0, 0.0, 20.0), (300.0, -20.0, 320.0, 20.0))
        self.assertEqual(q["mdetour"], 1.0)
        self.assertEqual((q["reversals"], q["bends"]), (0.0, 0.0))

    def test_a_label_nearer_a_third_box_is_misattributed(self):
        a, b, third = (0.0, 0.0, 40.0, 40.0), (600.0, 0.0, 640.0, 40.0), (300.0, 60.0, 340.0, 100.0)
        own = RD.edge_quality([(40.0, 20.0), (600.0, 20.0)], a, b, (60.0, 20.0), [third])
        adrift = RD.edge_quality([(40.0, 20.0), (600.0, 20.0)], a, b, (320.0, 20.0), [third])
        self.assertEqual(own["label_misattributed"], 0.0)
        self.assertEqual(adrift["label_misattributed"], 1.0)

    def test_two_wires_off_one_node_count_as_a_crossing_where_a_reader_sees_it(self):
        # Both edges leave ``hub``; they cross 200 units out, which the eye reads as a crossing and the ordering
        # pass does not score at all. Nine of the eleven crossings on the rejected board were of this kind.
        nodes = {"hub": (0.0, 0.0, 40.0, 200.0), "up": (400.0, 0.0, 440.0, 40.0), "down": (400.0, 160.0, 440.0, 200.0)}
        edges = [("hub", "down", [(40.0, 20.0), (240.0, 20.0), (240.0, 180.0), (400.0, 180.0)], None, None, "to down"),
                 ("hub", "up", [(40.0, 180.0), (160.0, 180.0), (160.0, 100.0), (400.0, 100.0)], None, None, "to up")]
        found = RD.count_crossings(RD.Drawn(nodes, edges))
        self.assertEqual(found["crossings"], 0.0, "no pair has four distinct ends")
        self.assertEqual(found["crossings_seen"], 1.0)

    def test_the_shape_gate_and_the_aspect_are_one_fact(self):
        for aspect in (1.0, 1.78, 4.0, 10.0):
            self.assertAlmostEqual(RD.measure(_box_pair(aspect))["screen_use"],
                                   round(min(1.0, 1.78 / aspect) * min(1.0, aspect / 1.78), 3), places=2)

    def test_a_strip_full_of_crossing_wire_is_not_empty(self):
        nodes = {"a": (0.0, 0.0, 40.0, 40.0), "b": (0.0, 400.0, 40.0, 440.0)}
        bare = RD.measure(RD.Drawn(nodes, [("a", "b", [(20.0, 40.0), (20.0, 400.0)], None, None, "e")], direction="down"))
        self.assertEqual(bare["empty_band"], 0.0, "the wire crosses the strip along the flow's cross axis")


def _box_pair(aspect: float) -> RD.Drawn:
    height = 100.0
    return RD.Drawn({"a": (0.0, 0.0, aspect * height, height)}, [])


class KeepDecision(unittest.TestCase):
    """``route_good``: what makes a stored route worth keeping, and what only makes it legal."""

    A = (0.0, 0.0, 100.0, 60.0)
    B = (400.0, 0.0, 500.0, 60.0)

    def test_a_short_straight_route_is_kept(self):
        self.assertTrue(RD.route_good([(100.0, 30.0), (400.0, 30.0)], self.A, self.B))

    def test_a_route_twice_as_long_as_it_needs_is_not(self):
        long_way = [(100.0, 30.0), (100.0, -300.0), (400.0, -300.0), (400.0, 30.0)]
        self.assertGreater(RD.edge_quality(long_way, self.A, self.B)["mdetour"], RD.KEEP_MDETOUR)
        self.assertFalse(RD.route_good(long_way, self.A, self.B))

    def test_a_route_that_doubles_back_twice_is_not(self):
        back = [(100.0, 30.0), (250.0, 30.0), (250.0, 100.0), (150.0, 100.0), (150.0, 160.0), (400.0, 160.0), (400.0, 30.0)]
        self.assertGreater(RD.edge_quality(back, self.A, self.B)["reversals"], RD.KEEP_REVERSALS)
        self.assertFalse(RD.route_good(back, self.A, self.B))

    def test_a_route_whose_label_belongs_to_a_third_node_is_not(self):
        third = (240.0, 100.0, 300.0, 160.0)
        straight = [(100.0, 30.0), (400.0, 30.0)]
        self.assertTrue(RD.route_good(straight, self.A, self.B, (130.0, 30.0), [third]))
        self.assertFalse(RD.route_good(straight, self.A, self.B, (270.0, 30.0), [third]))

    def test_legality_is_the_same_test_the_arrow_kind_applies(self):
        """``route_legal`` is ``arrow._still_good`` over boxes: the harness has no elements to hand it."""
        start = {"id": "a", "x": 0, "y": 0, "w": 100, "h": 60}
        end = {"id": "b", "x": 400, "y": 0, "w": 100, "h": 60}
        blocker = ("c", (200.0, 0.0, 260.0, 60.0), "rect")
        cases = ([(100.0, 30.0), (400.0, 30.0)],           # straight, through the blocker
                 [(100.0, 30.0), (150.0, 30.0), (150.0, -80.0), (420.0, -80.0), (420.0, 30.0)],  # round it
                 [(100.0, 30.0), (400.0, 90.0)],           # not axis-aligned
                 [(0.0, 300.0), (400.0, 300.0)])           # touches neither end
        for points in cases:
            with self.subTest(points=points):
                self.assertEqual(RD.route_legal(points, (0.0, 0.0, 100.0, 60.0), (400.0, 0.0, 500.0, 60.0), [blocker[1]]),
                                 A._still_good([list(p) for p in points], start, end, [blocker]))


class Repair(GraphRig):
    """What an author can do about a drawing that has gone stale, and what the op tells them."""

    def test_re_issuing_the_drawing_now_repairs_it_and_then_settles(self):
        """The trap the agent on the rejected board fell into: it re-issued its whole drawing and nothing happened.

        Re-issuing used to be byte for byte a no-op - the seeds held every box and legality held every stale route -
        so the one repair an agent reaches for by itself did nothing at all. Now it cuts the stale wire, and after a
        redraw or two it stops changing: it repairs and then it settles, which is what an author needs from it."""
        self.ok(copy.deepcopy(FLOW_FIRST))
        self.ok(copy.deepcopy(FLOW))
        stale = _drawing(self, "shortener")
        self.ok(copy.deepcopy(FLOW))
        repaired = _drawing(self, "shortener")
        self.assertNotEqual(repaired, stale, "re-issuing the drawing repairs what had gone stale")
        # And it settles. Not after one redraw: cutting a stale route can put it alongside a third, so the wire
        # takes a few passes to come to rest. What matters is that it comes to rest rather than cycling.
        settled = repaired
        for _redraw in range(4):
            self.ok(copy.deepcopy(FLOW))
            now = _drawing(self, "shortener")
            if now == settled:
                break
            settled = now
        self.ok(copy.deepcopy(FLOW))
        self.assertEqual(_drawing(self, "shortener"), settled, "a redraw settles rather than cycling")

    def test_relayout_full_on_the_graph_op_recovers_the_fresh_drawing(self):
        """``graph {relayout: "full"}`` drops every seed and every stored route: the drawing one op would have made."""
        self.ok(copy.deepcopy(FLOW_FIRST))
        self.ok(copy.deepcopy(FLOW))
        self.ok(dict(copy.deepcopy(FLOW), relayout="full"))
        repaired = _drawing(self, "shortener")

        fresh = GraphRig("test_nothing")
        fresh.setUp()
        self.addCleanup(fresh.doCleanups)
        fresh.ok(copy.deepcopy(FLOW))
        self.assertEqual(repaired, _drawing(fresh, "shortener"),
                         "a full relayout draws what one op would have drawn")

    def test_a_route_that_is_legal_but_bad_is_cut_again_and_the_op_says_so(self):
        """The defect, in one test: a stored polyline that is orthogonal, touches both ends and clears every box,
        and is also twice as long as it needs to be. Legality used to be the whole test."""
        self.ok(copy.deepcopy(FLOW))
        edge = next(e for e in self.members("shortener") if e.get("part") == "e:api->store")
        points = [(float(p[0]), float(p[1])) for p in edge["points"]]
        below = float(self.root("shortener")["y"]) + float(self.root("shortener")["h"]) + 60.0
        detour = [[points[0][0], points[0][1]], [points[0][0], below], [points[-1][0], below],
                  [points[-1][0], points[-1][1]]]
        self.ok({"op": "move", "id": edge["id"], "points": detour, "intent": "the wire the old keep decision kept"},
                author=test_canvas.OPERATOR)
        kept = next(e for e in self.members("shortener") if e.get("part") == "e:api->store")
        self.assertEqual([[float(p[0]), float(p[1])] for p in kept["points"]], detour, "the detour is stored")
        world = {p: _box(e) for p, e in G.roles(self.root("shortener"), self.members("shortener"))[0].items()}
        self.assertTrue(RD.route_legal(detour, world["api"], world["store"],
                                       [box for part, box in world.items() if part not in ("api", "store")]),
                        "and it is legal: that is the point")

        result = self.apply([copy.deepcopy(FLOW)])
        self.assertEqual(result["refused"], [], result["refused"])
        notes = list((result["applied"][0].get("block") or {}).get("notes") or [])
        recut = [n for n in notes if "routes_recut" in n]
        self.assertTrue(recut, "the op has to say that it cut stale wire again: {}".format(notes))
        self.assertIn("e:api->store", recut[0])
        after = next(e for e in self.members("shortener") if e.get("part") == "e:api->store")
        self.assertNotEqual([[float(p[0]), float(p[1])] for p in after["points"]], detour)

    def test_a_route_into_a_node_the_operator_pinned_is_kept(self):
        """Freshness beats stability, except against evidence of intent: ``pin.by == "human"`` is that evidence."""
        self.ok(copy.deepcopy(FLOW))
        node = self.part("shortener", "store")
        self.ok({"op": "pin", "id": node["id"], "intent": "the operator placed this one"},
                author=test_canvas.OPERATOR)
        edge = next(e for e in self.members("shortener") if e.get("part") == "e:api->store")
        points = [(float(p[0]), float(p[1])) for p in edge["points"]]
        below = float(self.root("shortener")["y"]) + float(self.root("shortener")["h"]) + 60.0
        detour = [[points[0][0], points[0][1]], [points[0][0], below], [points[-1][0], below],
                  [points[-1][0], points[-1][1]]]
        self.ok({"op": "move", "id": edge["id"], "points": detour, "intent": "the operator drew this wire"},
                author=test_canvas.OPERATOR)
        self.ok(copy.deepcopy(FLOW))
        after = next(e for e in self.members("shortener") if e.get("part") == "e:api->store")
        self.assertEqual([[float(p[0]), float(p[1])] for p in after["points"]], detour,
                         "a route into a node the operator pinned keeps the shape they gave it")


def _box(el):
    x, y = float(el["x"]), float(el["y"])
    return x, y, x + float(el["w"]), y + float(el["h"])


class Checks(GraphRig):
    """``canvas check`` must be able to see what the owner saw."""

    CODES = ("crossings_high", "routes_tangled", "labels_adrift")

    def test_a_tangled_drawing_is_no_longer_clean(self):
        """The board the old code produced, rebuilt by writing its stale wire back onto a clean drawing.

        Rebuilding it this way rather than replaying a stored scene is deliberate: the fix means the pipeline no
        longer *makes* that board, so the only honest way to hold the check to it is to hand it the wire."""
        self.ok(copy.deepcopy(FLOW))
        self._tangle("shortener")
        found = [p["code"] for p in K.problems(self.scene()["elements"]) if p["code"] in self.CODES]
        self.assertEqual(sorted(found), ["crossings_high", "labels_adrift", "routes_tangled"],
                         "all three see it: seven crossings a reader can see, wire more than three times as long as "
                         "the gaps it crosses, and seven labels nearer a third node than their own ends")
        for problem in K.problems(self.scene()["elements"]):
            if problem["code"] in self.CODES:
                self.assertEqual(problem["fix"], {"op": "graph", "id": "shortener", "relayout": "full",
                                                  "intent": "draw shortener again from scratch"},
                                 "every one of them names a repair that works")

    def test_a_drawing_the_pipeline_makes_is_clean(self):
        for label, batches in (("fresh", [FLOW]), ("two batches", [FLOW_FIRST, FLOW]),
                               ("repaired", [FLOW_FIRST, FLOW, dict(FLOW, relayout="full")])):
            with self.subTest(board=label):
                rig = GraphRig("test_nothing")
                rig.setUp()
                self.addCleanup(rig.doCleanups)
                for op in batches:
                    rig.ok(copy.deepcopy(op))
                found = [(p["code"], p["message"]) for p in K.problems(rig.scene()["elements"]) if p["code"] in self.CODES]
                self.assertEqual(found, [], label)

    def test_the_check_can_never_be_tighter_than_the_gate(self):
        """The check is about what an agent should be told to fix; the gate is about what the pipeline owes.

        A board inside the check's numbers may still fail a gate, and never the other way round: a check that fired
        on a drawing the pipeline is allowed to produce would be crying wolf on its own work."""
        pairs = (("mdetour_median", G.TANGLED_MDETOUR_MEDIAN), ("mdetour_max", G.TANGLED_MDETOUR_MAX),
                 ("reversals_max", G.TANGLED_REVERSALS_MAX), ("edge_on_edge_len", G.TANGLED_EDGE_ON_EDGE),
                 ("label_orphan_max", G.ADRIFT_ORPHAN))
        for metric, threshold in pairs:
            how, ceiling = conformance.ceiling_of(metric, 10)
            with self.subTest(metric=metric):
                self.assertEqual(how, "<=")
                self.assertGreaterEqual(threshold, ceiling, "{}: the check is tighter than the gate".format(metric))

    def _tangle(self, alias):
        """Give every edge of the drawing the wire the old keep decision would have left on it."""
        root = self.root(alias)
        for el in self.members(alias):
            if el.get("type") != "arrow" or not el.get("points"):
                continue
            points = [(float(p[0]), float(p[1])) for p in el["points"]]
            a, b = points[0], points[-1]
            detour = float(root["y"]) + float(root["h"]) + 40.0
            self.ok({"op": "move", "id": el["id"], "intent": "the wire the old keep decision left",
                     "points": [[a[0], a[1]], [a[0], detour], [(a[0] + b[0]) / 2.0, detour],
                                [(a[0] + b[0]) / 2.0, a[1]], [b[0], a[1]], [b[0], b[1]]]},
                    author=test_canvas.OPERATOR)


class RealPipeline(GraphRig):
    """The whole pipeline's own numbers on the owner's board, which the pure harness cannot see.

    ``tests/layout_conformance`` measures the layout and the router. The canvas then settles every label again
    (``canvas._settle_labels``), and its obstacle set is not the router's, so a pill the router placed can still be
    moved. These bounds are the pipeline's, not the router's, and they are what the owner actually looks at.
    """

    #: Measured on this build. Kept as a regression guard with a little room, not as a target.
    BUDGET = {"crossings_seen": 1, "mdetour_median": 1.15, "mdetour_max": 1.70, "reversals_max": 1,
              "bends_max": 4, "label_astray_max": 0.0, "label_misattributed": 2, "label_orphan_max": 0.45,
              "edge_over_node": 0, "edge_near_node": 0, "edge_on_edge_len": 125, "screen_use": 0.33}

    def test_the_owners_board_stays_inside_its_budget(self):
        for label, batches in (("fresh", [FLOW]), ("two batches", [FLOW_FIRST, FLOW])):
            rig = GraphRig("test_nothing")
            rig.setUp()
            self.addCleanup(rig.doCleanups)
            for op in batches:
                rig.ok(copy.deepcopy(op))
            root = rig.root("shortener")
            found = RD.measure(RD.from_block(root, rig.members("shortener")))
            for metric, bound in sorted(self.BUDGET.items()):
                with self.subTest(board=label, metric=metric):
                    if metric == "screen_use":
                        self.assertGreaterEqual(found[metric], bound)
                    else:
                        self.assertLessEqual(found[metric], bound)


def _drawing(rig, alias):
    """Every box and every polyline of the drawing, rounded: what two runs have to agree on to be the same drawing."""
    root = rig.root(alias)
    out = []
    for el in sorted(rig.members(alias), key=lambda e: str(e.get("part"))):
        box = (round(float(el["x"]) - float(root["x"]), 2), round(float(el["y"]) - float(root["y"]), 2),
               round(float(el["w"]), 2), round(float(el["h"]), 2))
        points = [(round(float(p[0]) - float(root["x"]), 2), round(float(p[1]) - float(root["y"]), 2))
                  for p in (el.get("points") or ())]
        out.append((el.get("part"), box, points))
    return out


if __name__ == "__main__":
    unittest.main()
