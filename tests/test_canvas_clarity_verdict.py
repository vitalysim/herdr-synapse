"""The QA verdict on canvas v2 layout clarity, closed: the repair a check prints actually works, the owner's own
board can be repaired, a path of ranks is folded, and a pill never floats away from its own line.

Every test here is a finding from ``.local/qa/layout/qa-report.md``, named by its number, and every one of them
failed on the tree QA measured.
"""
from __future__ import annotations

import copy
import json
import unittest
from pathlib import Path

from support import TempState  # noqa: F401  (TempState keeps support's harness fake installed)
from collab_support import LEAD, MEMBER, MEMBERS, PEER
from test_canvas import OPERATOR, WORKER
from test_kind_graph import GraphRig

from herdr_team import canvas as C
from herdr_team import canvas_labels as LB
from herdr_team import canvas_layouts as CL
from herdr_team import canvas_readability as RD
from herdr_team.canvas_layouts import LEdge, LNode, LayoutRequest, _fold

#: The owner's own graph, as their agent drew it: nine nodes, two entry points, ten labelled edges.
FLOW = {
    "op": "graph", "id": "shortener", "title": "How a link shortener works", "layout": "flow",
    "direction": "right", "route": "orthogonal", "intent": "the owner's board",
    "nodes": [{"id": "paste", "text": "Paste long URL"}, {"id": "api", "text": "Shortener API"},
              {"id": "store", "text": "Store URL mapping"}, {"id": "short", "text": "Return short link"},
              {"id": "open", "text": "Open short link"}, {"id": "redirect", "text": "Redirect to original URL"},
              {"id": "cache", "text": "Fast lookup cache"}, {"id": "counter", "text": "Click counter"}],
    "edges": ["paste -> api: create", "api -> store: save", "store -> short: short link",
              "open -> api: lookup", "api -> redirect: original address", "store -> cache: populate",
              "open -> cache: cached", "cache -> api: hit", "open -> counter: track", "counter -> api: logged"],
}
#: The first half of it, which is how the board really came to be: six nodes, then ten edges over nine.
FLOW_FIRST = dict(FLOW, nodes=FLOW["nodes"][:6], edges=FLOW["edges"][:5])
#: A nine-step pipeline: one node per rank, which is the shape no view can fit until it is folded.
CHAIN = {
    "op": "graph", "id": "pipeline", "title": "The build pipeline", "layout": "flow", "direction": "right",
    "route": "orthogonal", "intent": "a long chain",
    "nodes": [{"id": n, "text": t} for n, t in
              (("src", "Source push"), ("lint", "Lint"), ("unit", "Unit tests"), ("build", "Build image"),
               ("scan", "Scan image"), ("stage", "Deploy to staging"), ("e2e", "End to end tests"),
               ("prod", "Deploy to production"), ("watch", "Watch metrics"))],
    "edges": ["src -> lint: on push", "lint -> unit: clean", "unit -> build: green", "build -> scan: image",
              "scan -> stage: clear", "stage -> e2e: ready", "e2e -> prod: green", "prod -> watch: live",
              "watch -> src: regression"],
}


def measured(rig, alias):
    root = rig.root(alias)
    return RD.measure(RD.from_block(root, rig.members(alias)))


class PrintedRepairs(GraphRig):
    """F1 and F2: a readability check may only name a repair that has been applied and watched work."""

    def test_the_repair_every_readability_check_prints_is_a_valid_op(self):
        """``graph {id, relayout:"full"}`` was refused ``op_invalid: graph needs nodes``, so the only advice
        ``canvas check`` could give about an unreadable drawing could not be carried out."""
        self.ok(copy.deepcopy(FLOW_FIRST))
        self.ok(copy.deepcopy(FLOW))
        result = self.apply([{"op": "graph", "id": "shortener", "relayout": "full", "intent": "draw it again"}])
        self.assertEqual(result["refused"], [])
        self.assertEqual(len(result["applied"]), 1)

    def test_an_op_that_names_a_block_and_no_items_redraws_it_and_removes_nothing(self):
        self.ok(copy.deepcopy(FLOW))
        before = {el["id"] for el in self.members("shortener")}
        self.ok({"op": "graph", "id": "shortener", "relayout": "full", "intent": "again"})
        self.assertEqual({el["id"] for el in self.members("shortener")}, before)
        self.assertEqual(self.root("shortener")["text"], "How a link shortener works")

    def test_the_title_an_op_gives_lands_on_the_stored_items(self):
        self.ok(copy.deepcopy(FLOW))
        self.ok({"op": "graph", "id": "shortener", "title": "How it really works", "intent": "retitle"})
        self.assertEqual(self.root("shortener")["text"], "How it really works")
        self.assertEqual(len(self.members("shortener")), 18, "nothing added, nothing removed")

    def test_a_readback_names_the_parts_the_block_has_and_not_the_ones_it_remembers(self):
        """The owner's board reached this state: a box re-adopted into the graph got a fresh part (``n326``) and kept
        the item it was built from (``{"id": "paste"}``), so every op built from the readback removed it and added a
        second one under the old name."""
        self.ok(copy.deepcopy(FLOW))
        box = self.part("shortener", "paste")
        self.apply([{"op": "move", "ids": [box["id"]], "by": [0, -400], "frame": None, "intent": "out"}], OPERATOR)
        root = self.root("shortener")
        self.apply([{"op": "move", "ids": [box["id"]], "to": [float(root["x"]) + 40, float(root["y"]) + 40],
                     "frame": root["id"], "intent": "back in"}], OPERATOR)
        again = self.el(box["id"])
        self.assertTrue(again.get("part"), "it was adopted as a node again")
        from herdr_team import canvas_blocks as B

        spec = B.current_spec(self.root("shortener"), self.members("shortener"))
        ids = [item if isinstance(item, str) else item["id"] for item in spec["nodes"]]
        self.assertIn(again["part"], ids)
        self.assertEqual(len(ids), len(set(ids)), ids)

    def test_a_check_that_fires_names_a_repair_that_clears_it(self):
        """The whole rule, end to end, on the board that made it: build the drawing the way the owner's board was
        built, then apply whatever ``canvas check`` prints and look again."""
        self.ok(copy.deepcopy(FLOW_FIRST))
        self.ok(copy.deepcopy(FLOW))
        self.apply([{"op": "claim", "region": "c0r0:c40r20", "intent": "mine"}], OPERATOR)
        for _round in range(3):
            problems = [p for p in C.check(self.layout, self.team, WORKER.name)["problems"]
                        if p["code"] in ("crossings_high", "routes_tangled", "labels_adrift", "graph_thin")]
            if not problems:
                break
            for problem in problems:
                self.assertIsNotNone(problem.get("fix"), problem["code"])
                result = self.apply([dict(problem["fix"])])
                self.assertEqual(result["refused"], [], (problem["code"], problem["message"]))
        problems = [p["code"] for p in C.check(self.layout, self.team, WORKER.name)["problems"]
                    if p["code"] in ("crossings_high", "routes_tangled", "labels_adrift", "graph_thin")]
        self.assertEqual(problems, [], "a repair that leaves the check firing is worse than no check")

    def test_a_relayout_reaches_the_pipelines_own_fixed_point(self):
        """F11: applying the repair twice must change nothing, or the check can never be satisfied."""
        self.ok(copy.deepcopy(FLOW_FIRST))
        self.ok(copy.deepcopy(FLOW))
        self.ok({"op": "graph", "id": "shortener", "relayout": "full", "intent": "again"})
        first = json.dumps(sorted(((el.get("part"), el.get("x"), el.get("y"), el.get("points"))
                                   for el in self.members("shortener")), key=lambda row: str(row[0])))
        self.ok({"op": "graph", "id": "shortener", "relayout": "full", "intent": "again"})
        again = json.dumps(sorted(((el.get("part"), el.get("x"), el.get("y"), el.get("points"))
                                   for el in self.members("shortener")), key=lambda row: str(row[0])))
        self.assertEqual(first, again)

    def test_a_check_stays_quiet_when_a_relayout_would_change_nothing(self):
        """Two nodes with ten edges between them: eight crossings a reader sees, and no layout of two boxes can do
        better. All three checks fired on it and the repair they printed reproduced the board to the digit."""
        self.ok({"op": "graph", "id": "parallel", "layout": "flow", "direction": "right", "intent": "ten wires",
                 "nodes": ["a", "b"], "edges": ["a -> b: w{}".format(i) for i in range(10)]})
        codes = [p["code"] for p in C.check(self.layout, self.team, WORKER.name)["problems"]
                 if p["code"] in ("crossings_high", "routes_tangled", "labels_adrift", "graph_thin")]
        self.assertEqual(codes, [])


class OperatorPins(GraphRig):
    """F2: an operator's pin survives a relayout. It does not stop one."""

    def test_a_box_the_operator_resized_keeps_its_size_and_the_drawing_is_laid_out_again(self):
        self.ok(copy.deepcopy(FLOW))
        box = self.part("shortener", "store")
        self.apply([{"op": "move", "id": box["id"], "by": [0, 40], "intent": "here"}], OPERATOR)
        self.apply([{"op": "resize", "id": box["id"], "to": [320, 140], "intent": "bigger"}], OPERATOR)
        pinned = self.el(box["id"])
        self.assertEqual(pinned["pin"], {"by": "human", "who": "human"})
        result = self.apply([{"op": "graph", "id": "shortener", "relayout": "full", "intent": "again"}])
        self.assertEqual(result["refused"], [], "one resized box used to lock the whole drawing")
        after = self.el(box["id"])
        self.assertEqual((after["w"], after["h"]), (pinned["w"], pinned["h"]), "the operator's size held")

    def test_a_relayout_holds_a_pinned_box_where_the_operator_put_it(self):
        self.ok(copy.deepcopy(FLOW))
        box = self.part("shortener", "paste")
        root = self.root("shortener")
        self.apply([{"op": "move", "ids": [box["id"]], "to": [float(root["x"]) + 60, float(root["y"]) + 500],
                     "intent": "down here"}], OPERATOR)
        was = self.el(box["id"])
        self.ok({"op": "graph", "id": "shortener", "relayout": "full", "intent": "again"})
        now = self.el(box["id"])
        self.assertEqual((now["x"], now["y"]), (was["x"], was["y"]))

    def test_a_full_respec_that_would_drop_a_pinned_part_names_the_repair_that_would_not(self):
        self.ok(copy.deepcopy(FLOW))
        box = self.part("shortener", "paste")
        self.apply([{"op": "move", "ids": [box["id"]], "by": [0, -600], "frame": None, "intent": "out"}], OPERATOR)
        root = self.root("shortener")
        self.apply([{"op": "move", "ids": [box["id"]], "to": [float(root["x"]) + 40, float(root["y"]) + 40],
                     "frame": root["id"], "intent": "back in"}], OPERATOR)
        refused = self.refused(copy.deepcopy(FLOW))
        self.assertEqual(refused["code"], "pin_held")
        self.assertIn('"relayout": "full"', refused["message"])


class HostArrangesPeerMarks(GraphRig):
    """A1, one level deeper than the reported case: a peer's contribution must not freeze the drawing it lands in."""

    def setUp(self):
        from support import whiteboard_on

        self.ts = TempState(members=MEMBERS)
        self.addCleanup(self.ts.cleanup)
        whiteboard_on(self.ts.session, self.ts.team, via="cli")
        self.layout, self.team = self.ts.layout, self.ts.team
        from unittest import mock

        from herdr_team import canvas_render as R

        patcher = mock.patch.object(R, "find_resvg", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_graphs_author_may_lay_out_a_peer_mark_inside_it(self):
        rig = self
        rig.ok(copy.deepcopy(FLOW), MEMBER)
        root = rig.root("shortener")
        proposal = rig.apply([{"op": "patch", "id": "shortener",
                               "add": {"nodes": [{"id": "limiter", "text": "Rate limiter"}],
                                       "edges": ["api -> limiter: throttle"]},
                               "intent": "one more"}], PEER)
        pid = (proposal["proposed"] or [{}])[0].get("proposal")
        self.assertTrue(pid, proposal)
        rig.apply([{"op": "accept", "id": pid, "intent": "yes"}], LEAD)
        mark = next(el for el in rig.scene()["elements"] if el.get("text") == "Rate limiter")
        was = (mark["x"], mark["y"])
        rig.apply([{"op": "patch", "id": "shortener", "add": {"nodes": [{"id": "limit", "text": "Rate limit"}],
                                                              "edges": ["api -> limit: throttle"]},
                    "intent": "extend"}], MEMBER)
        result = rig.apply([{"op": "graph", "id": "shortener", "relayout": "full", "intent": "tidy my own graph"}], MEMBER)
        self.assertEqual(result["refused"], [])
        self.assertEqual(result["proposed"], [], "tidying your own frame is not a proposal")
        now = rig.el(mark["id"])
        self.assertEqual(now.get("group"), root["id"], "it is a member of the coder's graph")
        self.assertEqual(now["text"], "Rate limiter", "the peer's words are untouched")
        self.assertEqual(now["author"], PEER.name, "and so is their authorship")
        self.assertNotEqual((now["x"], now["y"]), was, "but where it sits is the host's to decide")
        self.assertEqual(now.get("moved_by"), MEMBER.name)


class Folding(unittest.TestCase):
    """F4: a path of ranks is drawn as lanes, and a drawn fold stays folded."""

    def request(self, steps, direction="right", labelled=True):
        nodes = tuple(LNode(id="n{}".format(i), w=220.0, h=60.0, order=i) for i in range(steps))
        edges = tuple(LEdge(id="e{}".format(i), a="n{}".format(i), b="n{}".format(i + 1),
                            label=(120.0, 28.0) if labelled else None)
                      for i in range(steps - 1))
        return LayoutRequest(nodes=nodes, edges=edges, direction=direction, incremental=False)

    def shape(self, request, result):
        size = {n.id: (n.w, n.h) for n in request.nodes}
        places = [(x, y, size[k][0], size[k][1]) for k, (x, y) in result.positions.items()]
        wide = max(x + w for x, _y, w, _h in places) - min(x for x, _y, _w, _h in places)
        tall = max(y + h for _x, y, _w, h in places) - min(y for _x, y, _w, _h in places)
        return max(wide, tall) / max(min(wide, tall), 1.0)

    def test_a_nine_step_line_is_folded_into_lanes_and_fits_a_view(self):
        request = self.request(9)
        result = CL.run("layers", request)
        self.assertTrue(any(n.startswith("shape_folded") for n in result.notes), result.notes)
        self.assertLess(self.shape(request, result), 4.0, "a nine-step line is 16:1 unfolded")
        self.assertGreater(float(result.stats["screen_ink"]), 0.15)

    def test_a_short_line_is_left_alone(self):
        result = CL.run("layers", self.request(4))
        self.assertFalse(any(n.startswith("shape_folded") for n in result.notes), result.notes)

    def test_a_folded_board_stays_folded_when_a_step_is_added(self):
        """The stability contract, and the reason a fold along the flow could never ship: a fold read off the graph
        vanishes the moment some rank holds two nodes, and every box goes back into a line."""
        request = self.request(9)
        first = CL.run("layers", request)
        seeded = tuple(LNode(n.id, n.w, n.h, n.order, seed=first.positions[n.id]) for n in request.nodes)
        grown = CL.run("layers", LayoutRequest(
            nodes=seeded + (LNode("extra", 220.0, 60.0, 999),),
            edges=request.edges + (LEdge("extra-edge", "n4", "extra", label=(120.0, 28.0)),),
            direction="right", incremental=True))
        moved = [n.id for n in request.nodes
                 if max(abs(grown.positions[n.id][0] - first.positions[n.id][0]),
                        abs(grown.positions[n.id][1] - first.positions[n.id][1])) > 20]
        self.assertLessEqual(len(moved), 2, moved)

    def test_a_drawn_line_is_not_folded_under_its_author(self):
        """The other half: a board already drawn as a line stays one. Folding it would move every box."""
        request = self.request(9)
        line = CL.run("layers", LayoutRequest(nodes=request.nodes, edges=request.edges, direction="right",
                                              same_rank=(), order=(), incremental=False))
        # Lay the same graph out again seeded from a *straight* drawing of it: one node per rank, one lane.
        straight = tuple(LNode(n.id, n.w, n.h, n.order, seed=(i * 400.0, 0.0)) for i, n in enumerate(request.nodes))
        again = CL.run("layers", LayoutRequest(nodes=straight, edges=request.edges, direction="right", incremental=True))
        self.assertFalse(any(n.startswith("shape_folded") for n in again.notes), again.notes)
        self.assertTrue(any(n.startswith("shape_folded") for n in line.notes), "but a fresh one is folded")

    def test_a_fold_never_sends_a_step_down_a_column_past_other_boxes(self):
        steps = 12
        path = ["n{}".format(i) for i in range(steps)]
        skips = [("n0", "n5"), ("n6", "n11")]
        for lanes, _found in _fold.candidates(path, [(path[i], path[i + 1]) for i in range(steps - 1)] + skips):
            self.assertEqual(_fold._reaching(path, lanes, skips), 0, lanes)

    def test_the_lane_count_is_estimated_before_anything_is_laid_out(self):
        # A box 220 x 60 with 40/80 of spacing: nine of them want about four lanes, two hundred about sixteen.
        self.assertEqual(_fold.lanes_for(9, 60.0, 220.0, 40.0, 40.0, False, 16.0 / 9.0), 4)
        self.assertGreaterEqual(_fold.lanes_for(200, 60.0, 160.0, 40.0, 80.0, False, 16.0 / 9.0), 12)


class ThinGraphs(GraphRig):
    """F4's other half: ``graph_thin`` ships, and only when a relayout would really draw the board bigger."""

    def test_a_board_drawn_as_a_line_is_reported_and_the_repair_fixes_it(self):
        self.ok(copy.deepcopy(CHAIN))
        # Put it back into a line by hand, which is the state the owner's long boards are in.
        root = self.root("pipeline")
        nodes = sorted((el for el in self.members("pipeline") if el.get("type") == "box"),
                       key=lambda el: (float(el["y"]), float(el["x"])))
        for index, el in enumerate(nodes):
            self.ok({"op": "move", "id": el["id"], "to": [float(root["x"]) + 40 + index * 320,
                                                          float(root["y"]) + 60], "intent": "in a line"})
        problems = C.check(self.layout, self.team, WORKER.name)["problems"]
        found = [p for p in problems if p["code"] == "graph_thin"]
        self.assertTrue(found, [p["code"] for p in C.check(self.layout, self.team, WORKER.name)["problems"]])
        self.assertIn("too small to read", found[0]["message"])
        shared = found[0].get("fix_with")
        repair = next((p for p in problems if shared and p["code"] == shared["code"] and p["ids"] == shared["ids"]), found[0])
        self.assertIsInstance(repair["fix"], dict, "the finding carries its fix or names the same repair above")
        result = self.apply([dict(repair["fix"])])
        self.assertEqual((result["refused"], result["proposed"]), ([], []))
        self.assertEqual([p["code"] for p in C.check(self.layout, self.team, WORKER.name)["problems"]
                          if p["code"] == "graph_thin"], [])

    def test_a_board_no_relayout_can_improve_is_not_reported(self):
        self.ok({"op": "graph", "id": "two", "layout": "flow", "direction": "right", "intent": "two boxes",
                 "nodes": ["a", "b"], "edges": ["a -> b: only"]})
        self.assertEqual([p["code"] for p in C.check(self.layout, self.team, WORKER.name)["problems"]
                          if p["code"] == "graph_thin"], [])


class LabelsStayOnTheirLines(unittest.TestCase):
    """F6: ``label_astray_max`` was claimed to be zero everywhere. It was zero on the corpus."""

    def test_a_pill_never_ends_up_further_than_its_bound_from_its_own_line(self):
        ends = [((0.0, -40.0, 160.0, 40.0), "rect"), ((200.0, -40.0, 360.0, 40.0), "rect")]
        route = [(164.0, 0.0), (196.0, 0.0)]
        spot, _ring = LB.place(route, (100.0, 24.0), ends, [])
        self.assertLessEqual(LB.distance(route, spot[0], spot[1]), LB.ASTRAY_MAX)

    def test_the_gate_and_the_code_carry_the_same_bound(self):
        import layout_conformance as conformance

        how, bound = conformance.ceiling_of("label_astray_max", 10)
        self.assertEqual((how, bound), ("<=", LB.ASTRAY_MAX))


class EmptyCorridors(unittest.TestCase):
    """F3: spreading the lanes for the shape must not turn the room it adds into one empty corridor."""

    def bands(self):
        nodes, edges, groups = [], [], []
        for band in ("one", "two"):
            groups.append(CL.LGroup(id=band, parent=None, pad=(20.0, 40.0, 20.0, 20.0)))
            for step in range(4):
                nodes.append(LNode(id="{}{}".format(band, step), w=220.0, h=60.0, order=len(nodes), group=band))
                if step:
                    edges.append(LEdge(id="{}e{}".format(band, step), a="{}{}".format(band, step - 1),
                                       b="{}{}".format(band, step), label=(120.0, 28.0)))
        for step in range(4):
            edges.append(LEdge(id="cross{}".format(step), a="one{}".format(step), b="two{}".format(step),
                               label=(120.0, 28.0)))
        return LayoutRequest(nodes=tuple(nodes), edges=tuple(edges), groups=tuple(groups), direction="right",
                             incremental=False)

    def test_a_banded_drawing_is_not_spread_into_a_wider_corridor(self):
        """The guard, in the picture: the room a spread adds must go into the boxes, and on a two-band drawing every
        unit of it goes into the one corridor between the bands instead, so the spread is not taken."""
        request = self.bands()
        result = CL.run("layers", request)
        self.assertFalse(any(n.startswith("shape_balanced") for n in result.notes), result.notes)

    def test_the_guard_measures_the_widest_strip_with_no_box_in_it(self):
        from herdr_team.canvas_layouts import layers as L

        class Graph(object):
            w = {"a": 100.0, "b": 100.0}

        request = LayoutRequest(nodes=(LNode("a", 100.0, 60.0, 0), LNode("b", 100.0, 60.0, 1)))
        self.assertAlmostEqual(L._empty_band(request, Graph(), {"a": 0.0, "b": 100.0}), 0.0, places=6)
        self.assertAlmostEqual(L._empty_band(request, Graph(), {"a": 0.0, "b": 300.0}), 200.0 / 400.0, places=6)


class AutomaticClaimsFitTheirMarks(GraphRig):
    """F7: ``claim_edge`` was firing on claims the system made for itself."""

    def test_a_claim_the_system_made_is_fitted_again_when_its_marks_grow(self):
        self.ok({"op": "graph", "id": "g", "layout": "flow", "direction": "right", "intent": "a graph",
                 "nodes": ["a", "b", "c"], "edges": ["a -> b: one", "b -> c: two"]})
        self.apply([{"op": "patch", "id": "g", "add": {"nodes": ["d", "e", "f"],
                                                       "edges": ["c -> d: three", "d -> e: four", "e -> f: five"]},
                     "intent": "extend"}])
        self.apply([{"op": "patch", "id": "g", "relayout": "full", "intent": "again"}])
        problems = [p for p in C.check(self.layout, self.team, WORKER.name)["problems"] if p["code"] == "claim_edge"]
        self.assertEqual(problems, [], [p["message"] for p in problems])


class LanesDoNotLockAnAuthorOut(GraphRig):
    """F5: an author's own drawing growing while it is laid out again is not a mark in somebody else's lane."""

    def setUp(self):
        from unittest import mock

        from support import whiteboard_on

        from herdr_team import canvas_render as R

        self.ts = TempState(members=MEMBERS)
        self.addCleanup(self.ts.cleanup)
        whiteboard_on(self.ts.session, self.ts.team, via="cli")
        self.layout, self.team = self.ts.layout, self.ts.team
        patcher = mock.patch.object(R, "find_resvg", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_a_relayout_that_grows_into_a_peers_lane_still_applies(self):
        self.ok(copy.deepcopy(FLOW), MEMBER)
        self.ok(dict(copy.deepcopy(FLOW), id="peer_flow", title="The peer's own"), PEER)
        grown = self.apply([{"op": "patch", "id": "shortener",
                             "add": {"nodes": [{"id": "limit", "text": "Rate limit"},
                                               {"id": "audit", "text": "Audit log"}],
                                     "edges": ["api -> limit: throttle", "api -> audit: record"]},
                             "intent": "extend"}], MEMBER)
        self.assertEqual((grown["refused"], grown["proposed"]), ([], []))
        result = self.apply([{"op": "graph", "id": "shortener", "relayout": "full", "intent": "tidy my own"}], MEMBER)
        self.assertEqual((result["refused"], result["proposed"]), ([], []),
                         "the author of a drawing may always lay their own drawing out again")


class ClaimLabels(GraphRig):
    """F8: a claim snaps to the marks it holds, so its corner is a frame's corner - and that is where the title is."""

    def test_a_claims_label_and_border_clear_a_frames_own_title(self):
        from herdr_team import canvas_display as D

        self.ok({"op": "frame", "id": "board", "title": "Customer interview findings, week 38", "at": [0, 0],
                 "w": 900, "h": 420, "intent": "a frame"})
        self.ok({"op": "graph", "id": "flow", "layout": "flow", "direction": "right", "inside": "board",
                 "intent": "inside", "nodes": ["a", "b", "c"], "edges": ["a -> b: one", "b -> c: two"]})
        scene = self.scene()
        claim = next(c for c in scene["claims"] if c.get("auto"))
        entry = next(e for e in D.display_list(scene, stills=set())["entries"] if e["id"] == claim["id"])
        rect = next(i for i in entry["items"] if i["k"] == "rect")
        title = next(e for e in scene["elements"] if e.get("alias") == "board")
        self.assertLessEqual(rect["y"] + rect["h"] // 1, float(title["y"]) + float(title["h"]) + 1000)
        group = next(i for i in entry["items"] if i["k"] == "group")
        text = group["items"][0] if group["items"][0]["k"] == "text" else group["items"][0]["items"][0]
        self.assertLess(text["box"][1] + text["box"][3], 0.0, "the label hangs above the region, not over its title")
        self.assertLess(rect["y"], float(title["y"]), "and the dashed border is outside the frame")
