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
import json
import unittest

import collab_support

import layout_conformance as conformance
import test_canvas
from test_kind_graph import GraphRig

from herdr_team import canvas_blocks as B
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

    def test_re_issuing_a_stale_drawing_repairs_it_once_and_then_changes_nothing(self):
        """The trap the agent on the rejected board fell into, and the regression the fix for it brought in.

        The trap: re-issuing used to be byte for byte a no-op even over wire that had gone stale - the seeds held every
        box and legality held every stale route - so the one repair an agent reaches for by itself did nothing. The
        regression (layout findings N3): the quality test that fixed it also judged wire the pipeline had just drawn
        against these very boxes, so a re-issue *after* the repair cut it again, worse, and only then settled. Both
        are held here: stale wire is repaired by the first re-issue, and the drawing that repair leaves is final."""
        self.ok(copy.deepcopy(FLOW_FIRST))
        self.ok(copy.deepcopy(FLOW))
        _tangle(self, "shortener")
        stale = _drawing(self, "shortener")
        self.ok(copy.deepcopy(FLOW))
        repaired = _drawing(self, "shortener")
        self.assertNotEqual(repaired, stale, "re-issuing the drawing repairs what had gone stale")
        for _redraw in range(2):
            self.ok(copy.deepcopy(FLOW))
            self.assertEqual(_drawing(self, "shortener"), repaired, "the repaired drawing is final: a re-issue changes nothing")

    def test_a_board_drawn_before_the_fingerprint_is_judged_on_quality(self):
        """A board on disk from before ``arranged`` existed carries none, so nothing on it is taken as settled: the
        first incremental op judges its wire by quality, which is what repairs a board the old keep decision left."""
        self.ok(copy.deepcopy(FLOW))
        root = self.root("shortener")
        self.assertIsInstance(root.get(B.ARRANGED), int, "an arrangement that routes leaves its fingerprint")
        legacy = {k: v for k, v in root.items() if k != B.ARRANGED}
        self.assertIsNone(B._settled_for(None, legacy, [], "structure", False))
        self.assertIsNone(B._settled_for(None, root, [], "full", False), "relayout full never takes the wire as settled")

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
        problems = K.problems(self.scene()["elements"])
        repairs = [p for p in problems if p["code"] in self.CODES and p.get("fix")]
        self.assertEqual(len(repairs), 1, "all three readability findings print one actual relayout")
        for problem in problems:
            if problem["code"] in self.CODES:
                shared = problem.get("fix_with")
                repair = next((p for p in problems if shared and p["code"] == shared["code"] and p["ids"] == shared["ids"]), problem)
                self.assertEqual(repair["fix"], {"op": "graph", "id": "shortener", "relayout": "full",
                                                  "intent": "draw shortener again from scratch"},
                                 "every one of them names a repair that works")
        result = self.apply([dict(repairs[0]["fix"])])
        self.assertEqual((result["refused"], result["proposed"]), ([], []))
        self.assertEqual([p["code"] for p in K.problems(self.scene()["elements"]) if p["code"] in self.CODES], [])

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
        _tangle(self, alias)


def _tangle(rig, alias):
    """Give every edge of the drawing the wire the old keep decision would have left on it."""
    root = rig.root(alias)
    for el in rig.members(alias):
        if el.get("type") != "arrow" or not el.get("points"):
            continue
        points = [(float(p[0]), float(p[1])) for p in el["points"]]
        a, b = points[0], points[-1]
        detour = float(root["y"]) + float(root["h"]) + 40.0
        rig.ok({"op": "move", "id": el["id"], "intent": "the wire the old keep decision left",
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


class PeerRepair(collab_support.CollabRig):
    """A peer running the printed repair on someone else's graph is told the real reason (layout findings N1).

    ``graph {id, relayout: "full"}`` names a block by its alias, and an alias belongs to its author. A peer who sent it
    was refused ``op_invalid: graph needs nodes`` - true of the op as a *create*, and the wrong reason: the graph
    exists, it is somebody else's. An agent that reads "needs nodes" supplies nodes, and that is how a second copy of
    the owner's graph came to be drawn beside the first. Refused by its real reason, ``element_not_yours``, the op
    takes the path every other change to another author's work takes: the gate runs it once more and it becomes a
    proposal for the operator, naming whose the graph is.
    """

    def _drawn(self):
        self.ok(dict(copy.deepcopy(FLOW), intent="the member's own graph"))
        return [(e["id"], e.get("x"), e.get("y"), e.get("points")) for e in self.scene()["elements"]]

    def test_a_peers_repair_of_another_authors_graph_becomes_a_proposal_naming_the_author(self):
        before = self._drawn()
        proposed = self.proposed({"op": "graph", "id": "shortener", "relayout": "full", "intent": "run the printed repair"},
                                 author=collab_support.PEER)
        self.assertIn(collab_support.MEMBER.name, proposed["message"], "it says whose graph it is")
        self.assertNotIn("needs nodes", json.dumps(proposed))
        self.assertEqual([(e["id"], e.get("x"), e.get("y"), e.get("points")) for e in self.scene()["elements"]], before,
                         "nothing is drawn, and above all no second graph")
        # The operator can carry it out by accepting it: the relayout the peer asked for, done by someone who may.
        self.ok({"op": "accept", "id": proposed["proposal"], "intent": "yes, tidy it"}, author=collab_support.LEAD)
        self.assertEqual(len([e for e in self.scene()["elements"] if e.get("alias") == "shortener"]), 1)

    def test_a_host_who_tidied_a_peers_mark_reissues_as_a_no_op(self):
        """The host right (A1) writes ``moved_by`` on a peer's mark after the arrangement that moved it, in the same op;
        a fingerprint that read it never matched the drawing it was taken of, and on the owner's board the first
        re-issue after the repair cut the wire again (crossings 2 -> 3, reversals 0 -> 2)."""
        self._drawn()
        proposal = self.apply([{"op": "patch", "id": "shortener", "add": {"nodes": [{"id": "limiter", "text": "Rate limiter"}],
                                                                        "edges": ["api -> limiter: throttle"]},
                                "intent": "one more"}], author=collab_support.PEER)
        self.ok({"op": "accept", "id": proposal["proposed"][0]["proposal"], "intent": "yes"}, author=collab_support.LEAD)
        self.ok({"op": "graph", "id": "shortener", "relayout": "full", "intent": "tidy my own graph"})
        mark = next(e for e in self.scene()["elements"] if e.get("text") == "Rate limiter")
        self.assertEqual(mark.get("moved_by"), collab_support.MEMBER.name, "the host tidied the peer's mark")
        spec = dict(B.spec_of(self.scene()["elements"], self.by_alias("shortener")), op="graph", id="shortener",
                    intent="the author re-sends its own spec")
        once = _drawing(self, "shortener")
        for times in (1, 2):
            self.ok(copy.deepcopy(spec))
            self.assertEqual(_drawing(self, "shortener"), once, "re-issued {}x".format(times))

    def root(self, alias):
        return self.by_alias(alias)

    def members(self, alias):
        rid = self.by_alias(alias)["id"]
        return [e for e in self.scene()["elements"] if e.get("group") == rid]

    def test_without_the_gate_the_refusal_names_the_author_and_the_op_that_works(self):
        from unittest import mock

        from herdr_team import canvas_collab

        before = self._drawn()
        with mock.patch.object(canvas_collab, "GATE_ON", False):
            refused = self.refused({"op": "graph", "id": "shortener", "relayout": "full", "intent": "run the repair"},
                                   author=collab_support.PEER)
        self.assertEqual(refused["code"], "element_not_yours", refused)
        self.assertNotIn("needs nodes", refused["message"])
        self.assertIn(collab_support.MEMBER.name, refused["message"], "it says whose graph it is, and who may")
        printed = json.loads(refused["message"][refused["message"].index("{"):refused["message"].rindex("}") + 1])
        self.assertEqual(printed, {"op": "graph", "id": "shortener", "relayout": "full", "intent": "draw shortener again from scratch"})
        self.assertEqual(refused["details"]["fix"], printed, "the message and the fix print the same op")
        self.assertEqual([(e["id"], e.get("x"), e.get("y"), e.get("points")) for e in self.scene()["elements"]], before)
        # Verbatim: every agent op needs an intent, so an op printed without one was refused for the very author it names.
        for who in (collab_support.MEMBER, collab_support.MANAGER, collab_support.LEAD):
            result = self.apply([copy.deepcopy(printed)], author=who)
            self.assertEqual((result["refused"], result["proposed"]), ([], []), "and it works, as printed, for {}".format(who.name))

    def test_a_peer_who_sends_the_alias_with_nodes_is_told_whose_graph_that_name_already_is(self):
        """An alias belongs to its author, so a peer's graph under the same name is the peer's own and is drawn - but
        that is how the owner's board came to hold two copies of one graph, under ``applied ... check: clean``. The
        answer names the first graph and its author."""
        self._drawn()
        result = self.apply([{"op": "graph", "id": "shortener", "intent": "add the nodes it asked for",
                              "nodes": [{"id": "api", "text": "Shortener API"}], "edges": []}], author=collab_support.PEER)
        warned = [w for w in result["warnings"] if w["code"] == "alias_theirs"]
        self.assertEqual(len(warned), 1, result["warnings"])
        self.assertIn(collab_support.MEMBER.name, warned[0]["message"])
        first = [e for e in self.scene()["elements"] if e.get("alias") == "shortener" and e.get("author") == collab_support.MEMBER.name]
        self.assertEqual(warned[0]["ids"], [first[0]["id"]], "it names the first graph")
        # Its own second op on that alias is an upsert of its own graph: nothing more to say.
        again = self.apply([{"op": "graph", "id": "shortener", "intent": "one more node",
                             "nodes": [{"id": "api", "text": "Shortener API"}, {"id": "db", "text": "Store"}], "edges": ["api -> db"]}],
                           author=collab_support.PEER)
        self.assertEqual([w for w in again["warnings"] if w["code"] == "alias_theirs"], [])

    def test_a_peers_own_graph_of_the_same_name_is_still_theirs_to_redraw(self):
        self._drawn()
        self.ok(dict(copy.deepcopy(FLOW), at=[0, 2000], intent="the peer's own graph"), author=collab_support.PEER)
        result = self.apply([{"op": "graph", "id": "shortener", "relayout": "full", "intent": "redraw mine"}],
                            author=collab_support.PEER)
        self.assertEqual((result["refused"], result["proposed"]), ([], []), result)


def _grid(gid, nodes, edges):
    return {"op": "graph", "id": gid, "title": gid, "layout": "flow", "direction": "right", "route": "orthogonal",
            "at": [0, 0], "intent": "a shape that broke re-issue", "edges": edges,
            "nodes": [{"id": n, "text": t} for n, t in nodes]}


#: The two boards outside the corpus the verifier of the clarity round named, as it measured them: forty steps with
#: shortcuts and back edges (``mdetour_max`` 1.90 -> 2.48 on a re-issue), and a dense bipartite field with two sources
#: and back edges (crossings 7 -> 10, wire on wire 2 -> 43).
BIG40 = _grid("big40", [("n%d" % i, "Step %d" % i) for i in range(40)],
              ["n%d -> n%d: s%d" % (i, i + 1, i) for i in range(39)] +
              ["n%d -> n%d: skip" % (i, i + 5) for i in range(0, 34, 6)] +
              ["n%d -> n%d: back" % (i + 8, i) for i in range(2, 31, 9)])
ADVERSARIAL = _grid("adversarial", [("s1", "Source one"), ("s2", "Source two"), ("m1", "Middle one"),
                                    ("m2", "Middle two"), ("m3", "Middle three"), ("m4", "Middle four"),
                                    ("t1", "Target one"), ("t2", "Target two"), ("t3", "Target three"),
                                    ("side", "Off to one side")],
                    ["s1 -> m1: a", "s1 -> m3: b", "s1 -> m4: c", "s2 -> m1: d", "s2 -> m2: e", "s2 -> m4: f",
                     "m1 -> t2: g", "m1 -> t3: h", "m2 -> t1: i", "m2 -> t3: j", "m3 -> t1: k", "m3 -> t2: l",
                     "m4 -> t1: m", "m4 -> t3: n", "t3 -> s1: loop back", "t1 -> m2: retry",
                     "side -> m2: aside", "m4 -> side: out"])


def corpus_ops(board):
    """A corpus board as the ``graph`` ops that would build it, pass by pass: what an agent actually sends.

    The corpus fixtures hold sizes rather than words, so each node is named by its own id; the shape - ranks, fans,
    bands, cycles, the two-batch history - is the fixture's, which is what re-issue idempotence depends on.
    """
    fixture = conformance.corpus(board)
    ops = []
    for members in fixture["passes"]:
        op = {"op": "graph", "id": board.replace("-", "_"), "title": board, "layout": "flow",
              "direction": fixture["direction"], "route": "orthogonal", "at": [0, 0], "intent": "corpus board",
              "nodes": [dict({"id": n["id"], "text": n["id"]}, **({"in": n["group"]} if n.get("group") else {}))
                        for n in members["nodes"]],
              "edges": ["{} -> {}{}".format(e["a"], e["b"], ": " + e["name"] if e.get("name") else "")
                        for e in members["edges"]]}
        if members.get("groups"):
            op["groups"] = [dict({"id": g["id"], "title": g.get("title") or g["id"]},
                                 **({"parent": g["parent"]} if g.get("parent") else {})) for g in members["groups"]]
        ops.append(op)
    return ops


class Reissue(GraphRig):
    """Re-sending an unchanged ``graph`` op changes nothing, through the whole pipeline (layout findings N3/F11).

    ``tests/layout_conformance.check_reissue`` holds the harness to it; this holds the code that ships, labels
    settled by the canvas and boxes rounded as they are stored, which the harness cannot see - and which is where a
    route drawn against a box at 632.07 met the same box stored at 632 on the next op.
    """

    def _reissued(self, label, ops):
        rig = GraphRig("test_nothing")
        rig.setUp()
        self.addCleanup(rig.doCleanups)
        for op in ops:
            rig.ok(copy.deepcopy(op))
        once = _board(rig)
        for times in (1, 2):
            rig.ok(dict(copy.deepcopy(ops[-1]), intent="the same op again"))
            self.assertEqual(_changed(once, _board(rig)), [], "{}: re-issued {}x, these elements changed".format(label, times))

    def _relaid(self, label, ops):
        """``relayout:"full"`` is a fixed point - the second one changes nothing, the third neither - and the plain
        re-issue after it changes nothing. Every stored field but bookkeeping is compared, the root's box and every
        label spot included: on the adversarial board the whole block crept up 2 units per relayout, forever, and on
        the banded board the re-issue after one grew the frame 20 units (QA round 2)."""
        rig = GraphRig("test_nothing")
        rig.setUp()
        self.addCleanup(rig.doCleanups)
        for op in ops:
            rig.ok(copy.deepcopy(op))
        alias = ops[-1]["id"]
        rig.ok({"op": "graph", "id": alias, "relayout": "full", "intent": "lay it out again"})
        once = _board(rig)
        for times in (2, 3):
            rig.ok({"op": "graph", "id": alias, "relayout": "full", "intent": "and again"})
            self.assertEqual(_changed(once, _board(rig)), [], "{}: relayout full x{}, these elements changed".format(label, times))
        rig.ok(dict(copy.deepcopy(ops[-1]), intent="the same op again"))
        self.assertEqual(_changed(once, _board(rig)), [], "{}: re-issued after relayout full, these elements changed".format(label))

    def test_relayout_full_is_a_fixed_point_on_every_corpus_board_and_the_named_ones(self):
        boards = [(board, corpus_ops(board)) for board in conformance.corpus_names()]
        boards += [("owner's flow", [FLOW]), ("owner's flow, two batches", [FLOW_FIRST, FLOW]), ("big40", [BIG40]),
                   ("adversarial", [ADVERSARIAL])]
        for label, ops in boards:
            with self.subTest(board=label):
                self._relaid(label, ops)

    def test_every_corpus_board_reissues_as_a_no_op(self):
        for board in conformance.corpus_names():
            with self.subTest(board=board):
                self._reissued(board, corpus_ops(board))

    def test_a_board_the_operator_placed_boxes_in_reissues_as_a_no_op(self):
        """The owner's own board: the operator had placed three boxes by hand and sized one. A layered layout seeded
        with a drawing holding boxes off their ranks is not its own fixed point - on that board it moved "Redirect to
        original URL" 35 units on every re-issue, so the wire was cut again every time and never settled - so a
        settled block is put back exactly as it was left, not laid out again (``canvas_blocks._as_left``)."""
        rig = GraphRig("test_nothing")
        rig.setUp()
        self.addCleanup(rig.doCleanups)
        rig.ok(copy.deepcopy(FLOW))
        for part, by in (("paste", [-200, -340]), ("store", [0, -260]), ("open", [-560, -100])):
            el = rig.part("shortener", part)
            rig.ok({"op": "move", "id": el["id"], "by": by, "intent": "the operator's place"}, author=test_canvas.OPERATOR)
        store = rig.part("shortener", "store")
        rig.ok({"op": "move", "id": store["id"], "w": 160, "h": 80, "intent": "the operator's size"}, author=test_canvas.OPERATOR)
        rig.ok({"op": "graph", "id": "shortener", "relayout": "full", "intent": "the printed repair"})
        spec = dict(rig.spec("shortener"), op="graph", id="shortener", intent="the agent re-sends its own spec")
        self._reissued_spec(rig, spec)

    def _reissued_spec(self, rig, spec):
        once = _board(rig)
        for times in (1, 2, 3):
            rig.ok(copy.deepcopy(spec))
            self.assertEqual(_changed(once, _board(rig)), [], "re-issued {}x, these elements changed".format(times))

    def test_the_boards_the_verifier_named_reissue_as_a_no_op(self):
        for label, ops in (("owner's flow", [FLOW]), ("owner's flow, two batches", [FLOW_FIRST, FLOW]),
                           ("big40", [BIG40]), ("adversarial", [ADVERSARIAL])):
            with self.subTest(board=label):
                self._reissued(label, ops)


#: What every op rewrites whether or not anything changed: the op's own bookkeeping, not the drawing.
_BOOKKEEPING = {"seq", "updated_seq", "updated_at", "created_at", "batch", "intent", "client_id"}


def _board(rig):
    """Every element and every stored field but bookkeeping: what a re-issue that changes nothing must leave alone.

    Not only boxes and polylines (``_drawing``): a re-issue that kept every box and every route still re-rolled two
    label spots on the owner's flow (misattributed 0 -> 2) and grew the banded board's frame, and a gate that read
    boxes and routes alone passed it (QA round 2)."""
    return {el["id"]: json.dumps({k: v for k, v in el.items() if k not in _BOOKKEEPING}, sort_keys=True, default=str)
            for el in rig.scene()["elements"]}


def _changed(before, after):
    return sorted(eid for eid in set(before) | set(after) if before.get(eid) != after.get(eid))


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
