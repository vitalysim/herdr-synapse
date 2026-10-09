"""The shapes the layout clarity round left thin: a retry loop, an editorial loop, and the banded form.

Each was a picture the owner would call unclear with ``canvas check`` silent about it: a retry loop came out 8.9:1
and a fifth of a view, a hand-off between two teams 31.8:1, and the owner's own board drawn as two bands kept 27 % of
its frame as an empty corridor between them. The tests below hold the repairs: the fold tolerates one branch
(``_fold.spine_of_ranks``) and folds a banded line whose columns stay inside one band, a wire between two bands runs
inside one of them (``layers._band_of_wire``), and ``graph_thin`` and the new ``bands_apart`` speak for boards drawn
before any of that.

Two flows that do not meet are *not* folded, however uneven. Folding each part into lanes was built and the owner
judged the picture not clearer (2026-10-09): the second flow sat under the first one's column and read as its
continuation. ``test_two_flows_that_do_not_meet_stay_two_rows`` holds that decision.
"""
from __future__ import annotations

import copy
import unittest

from support import TempState  # noqa: F401  (TempState keeps support's harness fake installed)
from test_canvas import WORKER
from test_kind_graph import GraphRig

from herdr_team import canvas as C
from herdr_team import canvas_layouts as CL
from herdr_team import canvas_readability as RD
from herdr_team.canvas_kinds import graph as G
from herdr_team.canvas_layouts import LEdge, LGroup, LNode, LayoutRequest, _fold, layers

import layout_conformance as LC


def _op(gid, title, nodes, edges, **extra):
    out = {"op": "graph", "id": gid, "title": title, "layout": "flow", "direction": "right", "route": "orthogonal",
           "at": [0, 0], "intent": "a shape probe",
           "nodes": [dict({"id": n, "text": t}, **(rest[0] if rest else {})) for n, t, *rest in nodes], "edges": edges}
    out.update(extra)
    return out


#: The verifier's retry loop: a line of six with its "done" step beside the check that ends it.
RETRY = _op("retry_loop", "A job that retries",
            [("submit", "Submit job"), ("queue", "Queue"), ("run", "Run worker"), ("check", "Check result"),
             ("retry", "Back off and retry"), ("done", "Mark done"), ("fail", "Give up")],
            ["submit -> queue: enqueue", "queue -> run: pick up", "run -> check: finish", "check -> done: ok",
             "check -> retry: error", "retry -> queue: requeue", "retry -> fail: no tries left"])
#: An editorial pipeline with a review loop: the branch hangs from the middle of the line, not its end.
EDITORIAL = _op("editorial", "How an article gets published",
                [("pitch", "Pitch accepted"), ("draft", "Write draft"), ("edit", "Editor review"),
                 ("fact", "Fact check"), ("copyedit", "Copy edit"), ("layout", "Page layout"), ("publish", "Publish"),
                 ("revise", "Revise draft")],
                ["pitch -> draft: assigned", "draft -> edit: submitted", "edit -> fact: approved",
                 "edit -> revise: changes", "revise -> edit: resubmitted", "fact -> copyedit: checked",
                 "copyedit -> layout: clean", "layout -> publish: signed off"])
#: Two flows that do not meet, one of them long enough to fold on its own: drawn as two rows all the same.
UNEVEN = _op("two_campaigns", "Launch and churn",
             [("brief", "Write the brief"), ("copy", "Draft copy"), ("design", "Design assets"),
              ("review", "Legal review"), ("schedule", "Schedule posts"), ("launch", "Launch day"),
              ("churn", "Customer churns"), ("winback", "Win-back email"), ("offer", "Discount offer")],
             ["brief -> copy: approved", "copy -> design: final copy", "design -> review: mockups",
              "review -> schedule: cleared", "schedule -> launch: queued", "churn -> winback: after 7 days",
              "winback -> offer: no reply"])
#: Two four-step journeys: left as two rows, on purpose.
SPLIT = _op("two_flows", "Two journeys that do not meet",
            [("signup", "Sign up"), ("verify", "Verify email"), ("welcome", "Welcome tour"), ("active", "Active user"),
             ("cancel", "Cancel plan"), ("survey", "Exit survey"), ("purge", "Purge data"), ("gone", "Account closed")],
            ["signup -> verify: email", "verify -> welcome: confirmed", "welcome -> active: finished",
             "cancel -> survey: asked", "survey -> purge: after 30 days", "purge -> gone: done"])
#: A hand-off between two teams: a banded line, one band after the other along the flow.
HANDOFF = _op("handoff", "Sales to onboarding hand-off",
              [("lead", "New lead", {"in": "sales"}), ("demo", "Book a demo", {"in": "sales"}),
               ("deal", "Close the deal", {"in": "sales"}), ("kick", "Kick-off call", {"in": "onb"}),
               ("setup", "Account setup", {"in": "onb"}), ("train", "Team training", {"in": "onb"})],
              ["lead -> demo: qualified", "demo -> deal: interested", "deal -> kick: signed",
               "kick -> setup: goals agreed", "setup -> train: ready"],
              groups=[{"id": "sales", "title": "Sales"}, {"id": "onb", "title": "Onboarding"}])
#: The owner's board as two declared bands.
BANDS = _op("shortener_bands", "How a link shortener works",
            [("paste", "Paste long URL", {"in": "make"}), ("api", "Shortener API", {"in": "make"}),
             ("store", "Store URL mapping", {"in": "make"}), ("short", "Return short link", {"in": "make"}),
             ("open", "Open short link", {"in": "visit"}), ("redirect", "Redirect to original URL", {"in": "visit"}),
             ("cache", "Fast lookup cache", {"in": "visit"}), ("counter", "Click counter", {"in": "visit"})],
            ["paste -> api: create", "api -> store: save", "store -> short: short link", "open -> api: lookup",
             "api -> redirect: original address", "store -> cache: populate", "open -> cache: cached",
             "cache -> api: hit", "open -> counter: track", "counter -> api: logged"],
            groups=[{"id": "make", "title": "Create a link"}, {"id": "visit", "title": "Open a link"}])


def _measure(rig, alias):
    return RD.measure(RD.from_block(rig.root(alias), rig.members(alias)))


def _boxes(rig, alias):
    """Every node box's corner by part; band frames and arrows are left out, because they follow their nodes."""
    return {e["part"]: (e["x"], e["y"]) for e in rig.members(alias)
            if e.get("part") and e.get("type") not in ("arrow", "frame")}


def _centres(rig, alias):
    return {e["part"]: (e["x"] + e["w"] / 2.0, e["y"] + e["h"] / 2.0) for e in rig.members(alias)
            if e.get("part") and e.get("type") not in ("arrow", "frame")}


def _notes(result):
    return [w["message"] for w in result.get("warnings") or []]


class SpineFold(unittest.TestCase):
    """The fold reads a line with one step beside it as a line, and nothing more than that."""

    def test_one_branch_is_tolerated_and_two_are_not(self):
        rank = {"a": 0, "b": 1, "c": 2, "d": 3, "x": 3, "e": 4, "f": 5}
        edges = [("a", "b"), ("b", "c"), ("c", "d"), ("c", "x"), ("d", "e"), ("e", "f")]
        order = {n: i for i, n in enumerate(rank)}
        self.assertEqual(_fold.spine_of_ranks(rank, edges, order), (["a", "b", "c", "d", "e", "f"], ["x"]))
        two = dict(rank, y=1)
        self.assertIsNone(_fold.spine_of_ranks(two, edges + [("a", "y")], dict(order, y=9)))

    def test_the_spine_takes_the_node_that_carries_the_line_on(self):
        # Rank 3 holds "done" (declared first) and "retry"; only "retry" leads on to rank 4.
        rank = {"s": 0, "q": 1, "r": 2, "done": 3, "retry": 3, "fail": 4, "c": 5}
        edges = [("s", "q"), ("q", "r"), ("r", "done"), ("r", "retry"), ("retry", "fail"), ("fail", "c")]
        found = _fold.spine_of_ranks(rank, edges, {n: i for i, n in enumerate(rank)})
        self.assertEqual(found, (["s", "q", "r", "retry", "fail", "c"], ["done"]))

    def test_a_branch_tied_to_two_columns_is_not_folded(self):
        """The flowchart golden's retry banner, fed from two decisions and looping back: structure, not a branch."""
        path = ["p{}".format(i) for i in range(8)]
        edges = [(a, b) for a, b in zip(path, path[1:])] + [("p2", "x"), ("p4", "x"), ("x", "p0")]
        for lanes, _fold_found in _fold.candidates(path, edges, None, ["x"]):
            self.assertIsNotNone(_fold._home(path, lanes, edges, "x"), lanes)
        self.assertEqual(_fold.candidates(path, edges, None, ["x"]), [])

    def test_a_branch_at_the_end_of_its_column_joins_it(self):
        path = ["p", "d", "e", "f", "c", "l", "u"]
        edges = [(a, b) for a, b in zip(path, path[1:])] + [("e", "r"), ("r", "e")]
        beside = _fold._beside(path, 3, edges, ["r"])
        self.assertEqual(beside, ((("p", "d", "e", "r"), ("f", "c", "l")), (("p", "d", "e", "r"), ("l", "c", "f"))))
        self.assertIsNone(_fold._beside(path, 4, edges, ["r"]), "e sits in the middle of its column at four lanes")

    def test_a_drawn_branch_on_a_column_line_is_read_back_into_that_column(self):
        path = ["p", "d", "e", "f", "c", "l"]
        seeds = {"p": (0.0, 0.0), "d": (100.0, 0.0), "e": (200.0, 0.0), "r": (300.0, 0.0),
                 "f": (200.0, 200.0), "c": (100.0, 200.0), "l": (0.0, 200.0)}
        heights = {n: 60.0 for n in seeds}
        sets, ordered = _fold.from_seeds(path, seeds, heights, branches=["r"])
        self.assertEqual(sets, (("p", "d", "e", "r"), ("f", "c", "l")))
        self.assertEqual(ordered, (("p", "d", "e", "r"), ("l", "c", "f")))


class Shapes(GraphRig):
    """Each shape through the real canvas: drawn bigger, clean, its own fixed point, and stable when it grows."""

    def draw(self, op):
        result = self.apply([copy.deepcopy(op)])
        self.assertEqual(result["refused"], [])
        return result

    def grow(self, op, after, extra):
        grown = copy.deepcopy(op)
        grown["nodes"] = grown["nodes"] + [extra]
        grown["edges"] = list(grown["edges"]) + ["{} -> {}: then".format(after, extra["id"])]
        return grown

    def assert_settled(self, op, alias):
        """A re-issue changes nothing, and one more step moves at most one box the author already had."""
        before = _boxes(self, alias)
        self.draw(op)
        self.assertEqual(_boxes(self, alias), before, "re-issued, a box moved")
        mid = op["nodes"][len(op["nodes"]) // 2]
        extra = dict({"id": "extra_step", "text": "One more step"}, **({"in": mid["in"]} if mid.get("in") else {}))
        self.draw(self.grow(op, mid["id"], extra))
        after = _boxes(self, alias)
        moved = [k for k in before if max(abs(after[k][0] - before[k][0]), abs(after[k][1] - before[k][1])) > 20]
        self.assertLessEqual(len(moved), 1, moved)

    def test_a_retry_loop_is_folded_into_a_loop(self):
        notes = _notes(self.draw(RETRY))
        self.assertTrue(any("shape_folded" in n and "done beside it" in n for n in notes), notes)
        found = _measure(self, "retry_loop")
        # 8.92:1 and screen_ink 0.038 on the commit this round started from.
        self.assertLess(found["content_aspect"], 3.0)
        self.assertGreater(found["screen_ink"], 0.15)
        self.assertEqual((found["crossings_seen"], found["reversals_total"], found["label_misattributed"]), (0, 0, 0))
        self.assertEqual(self.problems(("graph_thin", "routes_tangled", "crossings_high", "labels_adrift")), [])
        self.assert_settled(RETRY, "retry_loop")

    def test_a_branch_never_lands_between_two_steps_of_a_column(self):
        """The editorial pipeline's "revise" once landed between "copy edit" and "page layout", and the step between
        those two was routed round it: the snake the fold exists to remove."""
        self.draw(EDITORIAL)
        found = _measure(self, "editorial")
        self.assertLessEqual(found["mdetour_max"], 1.05)
        self.assertLess(found["content_aspect"], 3.0, "10:1 before")
        self.assertEqual(found["crossings_seen"], 0)
        self.assert_settled(EDITORIAL, "editorial")

    def test_two_flows_that_do_not_meet_stay_two_rows(self):
        """However uneven: a six-step launch plan beside a three-step churn flow is two processes, one row each.

        The owner rejected the folded picture (the churn flow read as the launch plan's continuation), so the fold
        must not reach a drawing whose parts do not meet, and a re-issue must not fold it either."""
        notes = _notes(self.draw(UNEVEN))
        self.assertFalse(any("shape_folded" in n for n in notes), notes)
        centres = _centres(self, "two_campaigns")
        launch = {round(centres[n][1]) for n in ("brief", "copy", "design", "review", "schedule", "launch")}
        churn = {round(centres[n][1]) for n in ("churn", "winback", "offer")}
        self.assertEqual((len(launch), len(churn)), (1, 1), "each flow reads left to right on its own row")
        self.assertNotEqual(launch, churn)
        before = _boxes(self, "two_campaigns")
        self.assertFalse(any("shape_folded" in n for n in _notes(self.draw(UNEVEN))))
        self.assertEqual(_boxes(self, "two_campaigns"), before, "re-issued, a box moved")
        self.assertEqual(self.problems(("graph_thin",)), [], "no check may advertise the fold it does not draw")

    def test_two_short_journeys_stay_two_rows(self):
        notes = _notes(self.draw(SPLIT))
        self.assertFalse(any("shape_folded" in n for n in notes), notes)
        rows = {round(y) for _x, y in _boxes(self, "two_flows").values()}
        self.assertEqual(len(rows), 2, "each journey reads left to right on its own row")

    def test_a_banded_line_folds_one_column_per_band(self):
        self.draw(HANDOFF)
        found = _measure(self, "handoff")
        self.assertLess(found["content_aspect"], 3.0, "31.8:1 before")
        centres = _centres(self, "handoff")
        sales = {round(centres[n][0]) for n in ("lead", "demo", "deal")}
        onboarding = {round(centres[n][0]) for n in ("kick", "setup", "train")}
        self.assertEqual((len(sales), len(onboarding)), (1, 1), "each team's steps share one column")
        self.assertTrue(sales.isdisjoint(onboarding))
        self.assert_settled(HANDOFF, "handoff")

    def test_wire_between_bands_runs_inside_them_and_the_corridor_closes(self):
        self.draw(BANDS)
        found = _measure(self, "shortener_bands")
        # 0.36 (188 units) and 2 crossings on the commit this round started from.
        self.assertLessEqual(found["band_corridor"], 0.15)
        self.assertEqual(found["crossings_seen"], 0)
        self.assertEqual(found["edge_through_other_band"], 0)
        self.assertEqual(self.problems(("bands_apart", "routes_tangled", "crossings_high")), [])
        self.assert_settled(BANDS, "shortener_bands")

    def test_an_unbanded_step_added_to_a_banded_board_does_not_reshuffle_it(self):
        """With no corridor to stand in, the new box used to be slotted between the bands and pushed both apart:
        all ten boxes moved."""
        self.draw(BANDS)
        before = _boxes(self, "shortener_bands")
        self.draw(self.grow(BANDS, "open", {"id": "extra_step", "text": "One more step"}))
        after = _boxes(self, "shortener_bands")
        moved = [k for k in before if max(abs(after[k][0] - before[k][0]), abs(after[k][1] - before[k][1])) > 20]
        self.assertLessEqual(len(moved), 2, moved)


class ChecksSpeak(GraphRig):
    """A board drawn before these repairs is told so, by a check whose printed repair clears it."""

    def flatten(self, alias, parts):
        """Put the named boxes back into one line by hand: the state a board drawn by the previous pipeline is in."""
        root = self.root(alias)
        for index, part in enumerate(parts):
            el = self.part(alias, part)
            self.ok({"op": "move", "id": el["id"], "to": [float(root["x"]) + 40 + index * 320, float(root["y"]) + 60],
                     "intent": "in a line"})

    def codes(self):
        return sorted(p["code"] for p in C.check(self.layout, self.team, WORKER.name)["problems"])

    def repair(self, code):
        problems = C.check(self.layout, self.team, WORKER.name)["problems"]
        found = [p for p in problems if p["code"] == code]
        self.assertTrue(found, self.codes())
        shared = found[0].get("fix_with")
        repair = next((p for p in problems if shared and p["code"] == shared["code"] and p["ids"] == shared["ids"]), found[0])
        self.assertIsInstance(repair["fix"], dict, "a finding either carries its repair or names the same repair above")
        self.assertEqual(self.apply([dict(repair["fix"])])["refused"], [])
        self.assertNotIn(code, self.codes())
        return found[0]

    def test_graph_thin_speaks_for_a_retry_loop_drawn_as_a_line(self):
        self.ok(copy.deepcopy(RETRY))
        self.flatten("retry_loop", ["submit", "queue", "run", "check", "retry", "fail", "done"])
        codes = self.codes()
        self.assertIn("graph_thin", codes)
        # The folded redraw uses a fraction of the wire, and that is the fold's doing, not a tangle.
        self.assertNotIn("routes_tangled", codes)
        self.repair("graph_thin")

    def test_bands_apart_speaks_for_a_corridor_and_the_repair_closes_it(self):
        self.ok(copy.deepcopy(BANDS))
        visit = self.part("shortener_bands", "visit")
        self.ok({"op": "move", "id": visit["id"], "by": [0, 220], "intent": "the corridor of the old layout"})
        self.assertGreater(_measure(self, "shortener_bands")["band_corridor"], G.BANDS_APART)
        problem = self.repair("bands_apart")
        self.assertIn("units apart", problem["message"])
        self.assertLessEqual(_measure(self, "shortener_bands")["band_corridor"], 0.15)


class Helpers(unittest.TestCase):
    """The smaller repairs the shapes needed."""

    def test_two_labelled_steps_between_one_pair_get_room_for_both_pills(self):
        nodes = (LNode("a", 160.0, 60.0, 0), LNode("b", 160.0, 60.0, 1))
        edges = (LEdge("e1", "a", "b", label=(80.0, 24.0)), LEdge("e2", "b", "a", label=(110.0, 24.0)))
        result = CL.run("layers", LayoutRequest(nodes=nodes, edges=edges, same_rank=(("a", "b"),), direction="right"))
        gap = abs(result.positions["b"][1] - result.positions["a"][1]) - 60.0
        self.assertGreaterEqual(gap, 24.0 + 24.0 + 3 * layers.FLAT_LABEL_MARGIN - 1e-6)

    def test_a_first_step_down_a_folded_column_is_on_its_own_line(self):
        nodes = {"s": (0.0, 0.0, 160.0, 60.0), "q": (0.0, 140.0, 160.0, 200.0), "r": (300.0, 140.0, 460.0, 200.0)}
        drawn = RD.Drawn(nodes, [("s", "q", [(80.0, 60.0), (80.0, 140.0)], None, None, "enqueue"),
                                 ("q", "r", [(160.0, 170.0), (300.0, 170.0)], None, None, "run")],
                         None, "right")
        self.assertEqual(RD.measure(drawn)["entry_cross_offset_max"], 0.0)

    def test_the_check_memo_tells_two_boards_with_the_same_ids_apart(self):
        root = {"id": "E-1", "updated_seq": 3}
        one = [{"id": "E-2", "x": 0, "y": 0, "w": 10, "h": 10, "updated_seq": 3}]
        two = [{"id": "E-2", "x": 500, "y": 0, "w": 10, "h": 10, "updated_seq": 3}]
        self.assertNotEqual(G._fresh_key(root, one), G._fresh_key(root, two))

    def test_the_check_is_looser_than_the_gate(self):
        """A check that fired on a corridor the pipeline is allowed to draw would cry wolf on its own work."""
        self.assertGreater(G.BANDS_APART, LC.CEILINGS["band_corridor"][1])

    def test_the_corridor_gate_is_red_on_the_corridor_it_was_written_for(self):
        how, bound = LC.ceiling_of("band_corridor", 10)
        self.assertEqual(how, "<=")
        self.assertFalse(LC.holds(how, 188.0 / 519.0, bound), "the base commit's 0.36")


class Groups(unittest.TestCase):
    def test_a_fold_never_puts_two_bands_in_one_column(self):
        nodes = tuple(LNode("n{}".format(i), 160.0, 60.0, i, group="g{}".format(i % 2)) for i in range(8))
        edges = tuple(LEdge("e{}".format(i), "n{}".format(i), "n{}".format(i + 1)) for i in range(7))
        groups = (LGroup("g0", pad=(20.0, 40.0, 20.0, 20.0)), LGroup("g1", pad=(20.0, 40.0, 20.0, 20.0)))
        result = CL.run("layers", LayoutRequest(nodes=nodes, edges=edges, groups=groups, direction="right"))
        self.assertFalse(any(n.startswith("shape_folded") for n in result.notes), result.notes)


if __name__ == "__main__":
    unittest.main()
