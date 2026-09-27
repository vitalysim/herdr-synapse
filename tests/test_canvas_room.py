"""Regression tests for the Phase 0 verdict's R-1 and R-3 (``.local/qa/phase0/verdict.md``).

R-1: a shape that grows to fit its text never ends up covering a neighbour it did not cover at the
size it was asked for. Marks placed on a hosting shape stay on it (it grows to hold them), a later
sibling makes way on its host, growth pushes what it now covers, and a point on a mark the engine
moved is a point on that mark, in the same batch or a later one.

R-3: every arrow label sits in a pill, on its route, clear of every mark and every other label;
a new arrow moves its later end to make room when the line is too short; the picture draws labels
above every mark, and the page gets the spot and the lines (``label_at``, ``fit``).
"""
from __future__ import annotations

import json
import unittest
import xml.etree.ElementTree as ET

from support import PLUGIN_ROOT
from test_canvas import OPERATOR, REVIEWER, CanvasRig

from herdr_team import canvas as C
from herdr_team import canvas_check as K
from herdr_team import canvas_kinds as KINDS
from herdr_team import canvas_labels as LB
from herdr_team import canvas_render as R

SVG = "http://www.w3.org/2000/svg"
HOUSE = json.loads((PLUGIN_ROOT / "tests" / "fixtures" / "canvas_scenes" / "house.json").read_text(encoding="utf-8"))["ops"]
LONG = "a long label that makes this small box grow much wider"


def overlaps(elements):
    return [p for p in K.problems(elements) if p["code"] in ("overlap", "text_on_label", "frame_edge")]


def pill(el):
    (x, y, w, h), _size, _lines = R.arrow_label_pill(el)
    return x, y, x + w, y + h


class R1TheHouse(CanvasRig):
    def by_alias(self):
        return {e.get("alias"): e for e in self.scene()["elements"] if e.get("alias")}

    def test_the_golden_house_has_no_unintended_overlap(self):
        result = self.apply(HOUSE)
        self.assertEqual(result["refused"], [])
        self.assertEqual(overlaps(self.scene()["elements"]), [])
        els = self.by_alias()
        walls, window, door, roof = (C.bounds(els[k]) for k in ("walls", "window", "door", "roof"))
        self.assertTrue(K._contains(walls, window) and K._contains(walls, door), "the window and the door stay on the walls")
        self.assertFalse(K._intersects(window, door, K.TOUCH), "siblings on the walls do not cover each other")
        self.assertFalse(K._intersects(roof, walls, K.TOUCH), "the roof grew; the walls made way below it")
        self.assertGreaterEqual(walls[1], roof[3], "below it, not beside it")
        home = C.bounds(els["home"])
        for key in ("roof", "walls", "window", "door", "sun", "tree", "caption"):
            self.assertTrue(K._contains(home, C.bounds(els[key])), key)

    def test_one_op_at_a_time_draws_the_same_house(self):
        self.apply(HOUSE)
        batch = {e["id"]: C.bounds(e) for e in self.scene()["elements"]}
        again = R1TheHouse("test_the_golden_house_has_no_unintended_overlap")
        again.setUp()
        try:
            for op in HOUSE:  # an agent over MCP draws one op per call
                again.ok(op)
            single = {e["id"]: C.bounds(e) for e in again.scene()["elements"]}
            self.assertEqual(single, batch)
        finally:
            again.doCleanups()


class R1Hosts(CanvasRig):
    def test_a_host_grows_to_hold_a_mark_that_grew_on_it(self):
        panel = self.ok({"op": "shape", "at": [0, 0], "w": 200, "h": 100, "intent": "a panel"})["ids"][0]
        child = self.ok({"op": "shape", "text": LONG, "at": [20, 20], "w": 40, "h": 40, "intent": "on the panel"})["ids"][0]
        panel, child = self.el(panel), self.el(child)
        self.assertTrue(K._contains(C.bounds(panel), C.bounds(child)))
        self.assertEqual(C.bounds(panel)[2], C.bounds(child)[2] + C.HOST_PAD, "it keeps its padding on the side it grew")
        self.assertEqual(panel["fit"]["min"], [200, 100], "what was asked for stays the minimum")
        self.assertEqual(overlaps(self.scene()["elements"]), [])

    def test_a_later_sibling_makes_way_on_the_same_host(self):
        self.ok({"op": "shape", "at": [0, 0], "w": 300, "h": 200, "intent": "walls"})
        first = self.ok({"op": "shape", "text": "kitchen window", "at": [15, 15], "w": 50, "h": 40, "intent": "t"})["ids"][0]
        result = self.apply([{"op": "shape", "text": "front door with a brass knocker", "at": [80, 60], "w": 40, "h": 60, "intent": "t"}])
        second = result["applied"][0]["ids"][0]
        self.assertIn("moved_to_fit", [w["code"] for w in result["warnings"]])
        walls = next(e for e in self.scene()["elements"] if not e.get("text"))
        self.assertFalse(K._intersects(C.bounds(self.el(first)), C.bounds(self.el(second)), K.TOUCH))
        self.assertTrue(K._contains(C.bounds(walls), C.bounds(self.el(second))), "it stays on the walls")
        self.assertEqual(overlaps(self.scene()["elements"]), [])

    def test_a_mark_placed_on_a_moved_mark_follows_it_in_a_later_batch(self):
        self.ok({"op": "shape", "kind": "diamond", "text": "red tiled roof with a brick chimney", "at": [180, 150], "w": 200, "h": 80,
                 "intent": "roof"})
        walls = self.ok({"op": "shape", "at": [180, 240], "w": 200, "h": 140, "intent": "walls"})["ids"][0]
        moved = self.el(walls)
        self.assertGreater(moved["y"], 240, "the roof grew into the walls, which made way")
        self.assertEqual(moved["nudged"], [0, moved["y"] - 240], "and remembers how far")
        window = self.el(self.ok({"op": "shape", "text": "win", "at": [200, 260], "intent": "t"})["ids"][0])
        self.assertEqual((window["x"], window["y"]), (200, 260 + moved["y"] - 240), "a point on where the walls were is on the walls")
        self.assertTrue(K._contains(C.bounds(self.el(walls)), C.bounds(window)))

    def test_a_deliberate_move_is_where_its_author_wants_it(self):
        self.ok({"op": "shape", "kind": "diamond", "text": "red tiled roof with a brick chimney", "at": [180, 150], "w": 200, "h": 80,
                 "intent": "roof"})
        walls = self.ok({"op": "shape", "at": [180, 240], "w": 200, "h": 140, "intent": "walls"})["ids"][0]
        self.ok({"op": "move", "id": walls, "to": [600, 600], "intent": "t"})
        self.assertNotIn("nudged", {k for k, v in self.el(walls).items() if v is not None})
        note = self.el(self.ok({"op": "shape", "text": "n", "at": [200, 320], "intent": "t"})["ids"][0])
        self.assertEqual((note["x"], note["y"]), (200, 320), "nothing to follow any more")


class R1Growth(CanvasRig):
    def test_an_edit_that_grows_a_shape_pushes_the_neighbour_it_now_covers(self):
        first = self.ok({"op": "shape", "text": "a", "at": [0, 0], "intent": "t"})["ids"][0]
        second = self.ok({"op": "shape", "text": "b", "at": [200, 0], "intent": "t"})["ids"][0]
        result = self.apply([{"op": "edit", "id": first, "text": LONG + " " + LONG, "intent": "t"}])
        self.assertEqual(result["refused"], [])
        self.assertIn(second, result["applied"][0]["ids"], "the push is part of the edit's event")
        a, b = C.bounds(self.el(first)), C.bounds(self.el(second))
        self.assertFalse(K._intersects(a, b, K.TOUCH))
        self.assertEqual(self.el(second)["x"] % C.GRID, 0, "pushed by whole grid steps")
        self.assertIn("moved_to_fit", [w["code"] for w in result["warnings"]])

    def test_an_overlap_the_author_asked_for_is_not_pushed(self):
        first = self.ok({"op": "shape", "text": "a", "at": [0, 0], "w": 200, "h": 80, "intent": "t"})["ids"][0]
        second = self.ok({"op": "shape", "text": "b", "at": [150, 40], "intent": "t"})["ids"][0]
        before = C.bounds(self.el(second))
        self.ok({"op": "edit", "id": first, "text": LONG, "intent": "t"})
        self.assertEqual(C.bounds(self.el(second)), before)

    def test_a_neighbour_that_is_not_the_authors_stays_put(self):
        first = self.ok({"op": "shape", "text": "a", "at": [0, 0], "intent": "t"})["ids"][0]
        theirs = self.ok({"op": "shape", "text": "b", "at": [200, 0], "intent": "t"}, REVIEWER)["ids"][0]
        before = C.bounds(self.el(theirs))
        self.ok({"op": "edit", "id": first, "text": LONG + " " + LONG, "intent": "t"})
        self.assertEqual(C.bounds(self.el(theirs)), before)
        self.assertIn("overlap", {p["code"] for p in K.problems(self.scene()["elements"])}, "check reports what was left")

    def test_a_locked_neighbour_stays_put(self):
        first = self.ok({"op": "shape", "text": "a", "at": [0, 0], "intent": "t"})["ids"][0]
        second = self.ok({"op": "shape", "text": "b", "at": [200, 0], "intent": "t"})["ids"][0]
        self.ok({"op": "lock", "region": [360, -100, 1400, 300], "label": "hands off"}, OPERATOR)
        before = C.bounds(self.el(second))
        self.ok({"op": "edit", "id": first, "text": LONG + " " + LONG, "intent": "t"})
        self.assertEqual(C.bounds(self.el(second)), before)


class R3Placement(unittest.TestCase):
    """The pure placement rules (``canvas_labels``)."""

    def test_a_clear_route_takes_the_label_on_its_middle(self):
        spot, ring = LB.place([(0, 0), (400, 0)], (100, 24), [], [])
        self.assertEqual((spot, ring), ((200.0, 0.0), 0))

    def test_a_mark_on_the_middle_moves_the_label_along_the_line(self):
        block = ((150.0, -20.0, 250.0, 20.0), "rect")
        spot, ring = LB.place([(0, 0), (600, 0)], (100, 24), [block], [])
        self.assertEqual(ring, 0)
        self.assertEqual(spot[1], 0)
        self.assertFalse(LB.hits(block, LB.pill_box(spot, (100, 24))))

    def test_a_line_too_short_puts_the_label_beside_it(self):
        ends = [((0.0, -40.0, 160.0, 40.0), "rect"), ((200.0, -40.0, 360.0, 40.0), "rect")]
        self.assertTrue(LB.blocked_on_route([(164, 0), (196, 0)], (100, 24), ends, []))
        spot, ring = LB.place([(164, 0), (196, 0)], (100, 24), ends, [])
        self.assertGreater(ring, 0)
        self.assertFalse(any(LB.hits(end, LB.pill_box(spot, (100, 24))) for end in ends))

    def test_a_label_keeps_its_spot_while_it_is_on_the_route_and_clear(self):
        spot, ring = LB.place([(0, 0), (400, 0)], (100, 24), [], [], current=(90.0, 0.0))
        self.assertEqual((spot, ring), ((90.0, 0.0), 0))
        spot, _ring = LB.place([(0, 0), (400, 0)], (100, 24), [], [], current=(90.0, 50.0))
        self.assertEqual(spot, (200.0, 0.0), "off the route: placed again")

    def test_other_labels_are_kept_clear_of(self):
        taken = LB.pill_box((200, 0), (100, 24))
        spot, _ring = LB.place([(0, 0), (400, 0)], (100, 24), [], [taken])
        self.assertFalse(LB.hits((taken, "rect"), LB.pill_box(spot, (100, 24))))

    def test_room_at_the_end_is_what_the_run_lacks(self):
        ends = [((0.0, -40.0, 160.0, 40.0), "rect")]  # the far end; the near end (the mover) is left out
        need = LB.room_at_end([(164, 0), (196, 0)], (100, 24), ends, [], at_start=False)
        # The nearest clear centre is 50 + CLEARANCE past the far end's edge (160); from there the moved end
        # must leave the label's reach, CLEARANCE and a STEP of slack for a round end.
        self.assertAlmostEqual(need, (50 + LB.CLEARANCE + LB.STEP) + (160 + 50 + LB.CLEARANCE) - 196, delta=2.0)

    def test_outlines_are_exact(self):
        box = (0.0, 0.0, 100.0, 100.0)
        corner = (0.0, 0.0, 10.0, 10.0)
        self.assertTrue(KINDS.outline_meets(box, "rect", corner))
        self.assertFalse(KINDS.outline_meets(box, "ellipse", corner), "the ellipse misses its box's corner")
        self.assertFalse(KINDS.outline_meets(box, "diamond", (0.0, 0.0, 20.0, 20.0)))
        self.assertTrue(KINDS.outline_holds(box, "diamond", (40.0, 40.0, 60.0, 60.0)))
        self.assertFalse(KINDS.outline_holds(box, "diamond", (10.0, 10.0, 30.0, 30.0)))
        self.assertEqual(KINDS.outline({"type": "ellipse"}), "ellipse")
        self.assertEqual(KINDS.outline({"type": "some-future-kind"}), "rect")
        self.assertTrue(KINDS.hosts({"type": "box"}))
        self.assertFalse(KINDS.hosts({"type": "frame"}), "a frame owns its children instead")
        with self.assertRaises(ValueError):
            KINDS.register(KINDS.Kind(name="blob", outline="blob"))


class R3OnTheCanvas(CanvasRig):
    def test_a_short_arrow_moves_its_later_end_to_hold_its_label(self):
        a = self.ok({"op": "shape", "text": "Client", "at": [0, 0], "intent": "t"})["ids"][0]
        b = self.ok({"op": "shape", "text": "API", "right_of": a, "intent": "t"})["ids"][0]
        result = self.apply([{"op": "arrow", "from": a, "to": b, "label": "POST /orders with an idempotency key", "intent": "t"}])
        arrow = self.el(result["applied"][0]["ids"][0])
        self.assertIn(b, result["applied"][0]["ids"], "the later end moved in the same event")
        self.assertEqual(self.el(a)["x"], 0, "the earlier end stays")
        self.assertEqual(self.el(b)["y"], 0, "along the arrow, so the row stays a row")
        box = pill(arrow)
        for end in (a, b):
            self.assertFalse(LB.hits(LB.obstacle_of(self.el(end)), box, 0.0), end)
        self.assertLessEqual(LB.distance(R.arrow_route(arrow), *arrow["label_at"]), LB.ON_ROUTE, "on the line")
        self.assertEqual(arrow["fit"]["lines"], R.arrow_label_text(arrow)[2], "the page draws the picture's lines")

    def test_two_labels_on_one_pair_do_not_meet(self):
        a = self.ok({"op": "shape", "text": "API", "at": [0, 0], "intent": "t"})["ids"][0]
        b = self.ok({"op": "shape", "text": "Worker", "right_of": a, "intent": "t"})["ids"][0]
        there = self.ok({"op": "arrow", "from": a, "to": b, "label": "enqueue a signed job with retries", "intent": "t"})["ids"][0]
        back = self.ok({"op": "arrow", "from": b, "to": a, "label": "status callback", "intent": "t"})["ids"][0]
        self.assertFalse(LB.hits((pill(self.el(there)), "rect"), pill(self.el(back)), 0.0))
        for arrow in (there, back):
            for end in (a, b):
                self.assertFalse(LB.hits(LB.obstacle_of(self.el(end)), pill(self.el(arrow)), 0.0))

    def test_an_arrow_by_points_never_moves_marks_and_its_label_goes_beside_the_line(self):
        block = self.ok({"op": "shape", "text": "in the way", "at": [100, -40], "w": 200, "h": 80, "intent": "t"})["ids"][0]
        arrow = self.el(self.ok({"op": "arrow", "points": [[0, 0], [400, 0]], "label": "passes behind", "intent": "t"})["ids"][0])
        self.assertEqual(C.bounds(self.el(block)), (100.0, -40.0, 300.0, 40.0))
        self.assertFalse(LB.hits(LB.obstacle_of(self.el(block)), pill(arrow), 0.0))

    def test_a_label_follows_its_arrow_when_both_ends_move(self):
        a = self.ok({"op": "shape", "text": "A", "at": [0, 0], "intent": "t"})["ids"][0]
        b = self.ok({"op": "shape", "text": "B", "at": [600, 0], "intent": "t"})["ids"][0]
        arrow = self.ok({"op": "arrow", "from": a, "to": b, "label": "calls", "intent": "t"})["ids"][0]
        before = self.el(arrow)["label_at"]
        self.ok({"op": "move", "ids": [a, b], "by": [0, 300], "intent": "t"})
        after = self.el(arrow)
        self.assertEqual(after["label_at"], [before[0], before[1] + 300])
        self.assertLessEqual(LB.distance(R.arrow_route(after), *after["label_at"]), LB.ON_ROUTE)

    def test_a_label_on_a_curve_sits_on_the_drawn_curve(self):
        a = self.ok({"op": "shape", "text": "A", "at": [0, 0], "intent": "t"})["ids"][0]
        b = self.ok({"op": "shape", "text": "B", "at": [600, 0], "intent": "t"})["ids"][0]
        arrow = self.el(self.ok({"op": "arrow", "from": a, "to": b, "label": "bows", "curve": True, "intent": "t"})["ids"][0])
        self.assertGreater(arrow["label_at"][1], 40 + 20, "the curve bows below the straight line; the label is on the curve")

    def test_the_picture_draws_labels_above_every_mark(self):
        a = self.ok({"op": "shape", "text": "A", "at": [0, 0], "intent": "t"})["ids"][0]
        b = self.ok({"op": "shape", "text": "B", "at": [600, 0], "intent": "t"})["ids"][0]
        self.ok({"op": "arrow", "from": a, "to": b, "label": "calls", "intent": "t"})
        self.ok({"op": "shape", "text": "later", "at": [1000, 300], "intent": "t"})
        root = ET.fromstring(R.render_svg(self.scene(), marks=False))
        rects = list(root.iter("{%s}rect" % SVG))
        pill_index = next(i for i, r in enumerate(rects) if r.get("rx") == "6" and r.get("fill") == "#ffffff" and r.get("stroke-width") == "1")
        later = next(i for i, r in enumerate(rects) if float(r.get("x") or 0) == 1000)
        self.assertGreater(pill_index, later)

    def test_a_region_view_draws_a_label_that_reaches_into_it(self):
        self.ok({"op": "arrow", "points": [[0, 0], [400, 0]], "label": "label", "intent": "t"})
        arrow = next(e for e in self.scene()["elements"] if e["type"] == "arrow")
        spot = arrow["label_at"]
        self.assertTrue(K._contains(R.drawn_bounds(arrow), pill(arrow)))
        svg = R.render_svg(self.scene(), region=[spot[0] - 10, spot[1] - 5, spot[0] + 10, spot[1] + 5], marks=False)
        self.assertIn(">label<", svg)


class R4DeadCode(unittest.TestCase):
    def test_label_room_is_gone(self):
        # The kinds' inset functions (canvas_kinds.shape) are the one place for a shape's label room.
        self.assertFalse(hasattr(K, "LABEL_ROOM"))


if __name__ == "__main__":
    unittest.main()
