"""Route styles on arrows (canvas v2 phase 2, 3.3 and 3.4): ``route`` at creation, ``restyle route``, re-routing when an
end moves, rounded elbows in the drawing, and the ``arrow_through`` fix that leaves no arrow through a mark (T-R6)."""
from __future__ import annotations

import unittest

import test_canvas

from herdr_team import canvas as C
from herdr_team import canvas_check as K
from herdr_team import canvas_display as D
from herdr_team.canvas_kinds import arrow

WALL = [{"op": "shape", "id": "a", "text": "Client", "at": [0, 0], "intent": "t"},
        {"op": "shape", "id": "wall", "text": "Firewall", "at": [260, 0], "w": 160, "h": 200, "intent": "t"},
        {"op": "shape", "id": "b", "text": "Server", "at": [520, 0], "intent": "t"}]


def orthogonal(points) -> bool:
    return all(p[0] == q[0] or p[1] == q[1] for p, q in zip(points, points[1:]))


class Routes(test_canvas.CanvasRig):
    def through(self):
        return [p for p in K.problems(self.scene()["elements"]) if p["code"] == "arrow_through"]

    def test_an_orthogonal_arrow_goes_around_what_is_between_its_ends(self):
        self.apply(WALL)
        eid = self.ok({"op": "arrow", "from": "a", "to": "b", "route": "orthogonal", "label": "TLS", "intent": "t"})["ids"][0]
        el = self.el(eid)
        self.assertEqual(el["style"]["route"], "orthogonal")
        self.assertTrue(orthogonal(el["points"]))
        self.assertGreater(len(el["points"]), 2)
        self.assertEqual(self.through(), [])
        self.assertIsNotNone(el.get("label_at"))
        entry = next(e for e in D.display_list(self.scene())["entries"] if e["id"] == eid)
        shaft = next(item for item in entry["items"] if item["k"] == "arrow")
        self.assertIn("Q", shaft["d"], "rounded elbows")

    def test_the_arrow_through_fix_routes_around_and_straight_stays_byte_for_byte(self):
        self.apply(WALL)
        eid = self.ok({"op": "arrow", "from": "a", "to": "b", "intent": "t"})["ids"][0]
        straight = self.el(eid)["points"]
        self.assertEqual(len(straight), 2)
        [problem] = self.through()
        self.assertEqual(problem["fix"]["route"], "orthogonal")
        self.ok(problem["fix"])
        self.assertEqual(self.through(), [])
        self.assertTrue(orthogonal(self.el(eid)["points"]))
        self.ok({"op": "restyle", "ids": [eid], "route": "straight", "intent": "back"})
        self.assertEqual(self.el(eid)["points"], straight)

    def test_moving_an_end_reroutes_and_a_curve_draws_curved(self):
        self.apply(WALL)
        eid = self.ok({"op": "arrow", "from": "a", "to": "b", "route": "orthogonal", "intent": "t"})["ids"][0]
        self.ok({"op": "move", "id": "b", "by": [0, 400], "intent": "t"})
        el = self.el(eid)
        self.assertTrue(orthogonal(el["points"]))
        self.assertTrue(arrow._touches(el["points"][-1], self.el(C.parse_point("b", self.scene(), "alpha-worker") and
                                                                      next(e["id"] for e in self.scene()["elements"] if e.get("alias") == "b"))))
        self.assertEqual(self.through(), [])
        curved = self.ok({"op": "arrow", "from": "a", "to": "wall", "route": "curved", "intent": "t"})["ids"][0]
        entry = next(e for e in D.display_list(self.scene())["entries"] if e["id"] == curved)
        self.assertIn("Q", next(item for item in entry["items"] if item["k"] == "arrow")["d"])

    def test_a_route_that_is_still_good_is_kept(self):
        a = {"id": "a", "type": "box", "x": 0, "y": 0, "w": 100, "h": 50}
        b = {"id": "b", "type": "box", "x": 300, "y": 200, "w": 100, "h": 50}
        el = {"id": "E-3", "type": "arrow", "style": {"route": "orthogonal"}, "points": [[104, 25], [200, 25], [200, 225], [296, 225]]}
        self.assertEqual(arrow.reroute(el, a, b, {}), {})
        blocked = {"obstacles": [("w", (150.0, 0.0, 250.0, 100.0), "rect")]}
        self.assertNotEqual(arrow.reroute(el, a, b, blocked), {})


if __name__ == "__main__":
    unittest.main()
