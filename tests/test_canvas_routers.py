"""The router registry (canvas v2 phase 2, 3): discovery, the contract, the orthogonal router's own promises, a dropped-in
router, and T-R1: ``straight`` is today's route, byte for byte, on every arrow of every golden scene."""
from __future__ import annotations

import json
import random
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

from router_conformance import RouterConformance, maze, near_outline

from herdr_team import canvas as C
from herdr_team import canvas_geometry as G
from herdr_team import canvas_routers as R
from herdr_team.canvas_kinds import arrow
from herdr_team.canvas_routers import End, RouteRequest

FIXTURES = Path(__file__).resolve().parent / "fixtures"


def crosses(points, box) -> bool:
    """Whether an axis-aligned route runs through the inside of ``box``."""
    x0, y0, x1, y1 = box
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        if ax == bx and x0 < ax < x1 and min(ay, by) < y1 and y0 < max(ay, by):
            return True
        if ay == by and y0 < ay < y1 and min(ax, bx) < x1 and x0 < max(ax, bx):
            return True
    return False


class Registry(RouterConformance, unittest.TestCase):
    def test_every_router_module_is_found_and_keeps_the_contract(self):
        self.assertEqual(R.modules(), ["straight", "curved", "orthogonal"])
        self.assertEqual(R.names(), ["straight", "curved", "curve", "orthogonal", "elbow"])
        self.assertIs(R.get("elbow"), R.get("orthogonal"))
        for router in R.routers():
            self.check_router(router.name)
        with self.assertRaises(R.RouteError):
            R.route("wiggly", RouteRequest(id="x", a=End(box=(0, 0, 1, 1)), b=End(box=(5, 5, 6, 6))))
        with self.assertRaises(R.RouteError):
            R.route("straight", RouteRequest(id="x", a=End(box=(0, 0, 1, 1), outline="star"), b=End(box=(5, 5, 6, 6))))

    def test_curved_is_the_straight_points_drawn_as_a_curve(self):
        request = RouteRequest(id="c", a=End(box=(0, 0, 100, 50)), b=End(box=(300, 200, 400, 250)), via=((200.0, 20.0),))
        self.assertEqual(R.route("curved", request).points, R.route("straight", request).points)
        self.assertTrue(R.route("curved", request).curve)


class Orthogonal(unittest.TestCase):
    A = End(box=(0.0, 350.0, 160.0, 410.0), id="a")
    B = End(box=(1040.0, 350.0, 1200.0, 410.0), id="b")

    def test_generated_mazes_are_solved_without_a_piece_through_a_grown_obstacle(self):
        for seed in range(1, 21):
            obstacles = maze(seed)
            with self.subTest(seed=seed):
                found = R.route("orthogonal", RouteRequest(id="m", a=self.A, b=self.B, obstacles=tuple(obstacles)))
                self.assertFalse(found.blocked)
                points = found.points
                self.assertTrue(all(a[0] == b[0] or a[1] == b[1] for a, b in zip(points, points[1:])), "axis-aligned only")
                for _id, box, _o in obstacles:
                    grown = (box[0] - 19.99, box[1] - 19.99, box[2] + 19.99, box[3] + 19.99)
                    self.assertFalse(crosses(points, grown), (seed, box, points))
                self.assertTrue(near_outline(points[0], self.A) and near_outline(points[-1], self.B))

    def test_ports_spread_on_a_shared_side_and_a_lone_pair_meets_straight(self):
        hub = End(box=(0.0, 0.0, 300.0, 60.0), id="hub", side="s")
        leaves = [End(box=(x, 300.0, x + 100.0, 360.0), id="l{}".format(i), side="n") for i, x in enumerate((0.0, 200.0, 400.0))]
        routes = R.route_many("orthogonal", [RouteRequest(id="r{}".format(i), a=hub, b=leaf) for i, leaf in enumerate(leaves)])
        starts = sorted(route.points[0][0] for route in routes.values())
        self.assertTrue(all(b - a >= 12 for a, b in zip(starts, starts[1:])), starts)
        pair = R.route("orthogonal", RouteRequest(id="p", a=End(box=(0.0, 0.0, 200.0, 60.0), id="x"), b=End(box=(100.0, 300.0, 260.0, 360.0), id="y")))
        self.assertEqual(len(pair.points), 2, "facing ends that overlap meet in one straight piece")

    def test_clearance_stubs_and_rounded_elbows(self):
        found = R.route("orthogonal", RouteRequest(id="s", a=self.A, b=End(box=(600.0, 700.0, 760.0, 760.0), id="c"),
                                                   obstacles=(("w", (300.0, 300.0, 400.0, 900.0), "rect"),)))
        (x0, y0), (x1, y1) = found.points[0], found.points[1]
        self.assertGreaterEqual(abs(x1 - x0) + abs(y1 - y0), 10.0, "the first piece is at least clearance / 2")
        self.assertEqual(found.corner, 8)
        pieces = G.rounded_path([list(p) for p in found.points], 8)
        self.assertEqual([cmd for cmd, _args in pieces].count("Q"), len(found.points) - 2)

    def test_the_label_goes_on_the_longest_inner_piece_clear_of_obstacles(self):
        found = R.route("orthogonal", RouteRequest(id="l", a=self.A, b=End(box=(600.0, 700.0, 760.0, 760.0), id="c"), label=(60.0, 24.0),
                                                   obstacles=(("w", (300.0, 300.0, 400.0, 900.0), "rect"),)))
        lx, ly = found.label_at
        self.assertFalse(300 < lx + 30 and lx - 30 < 400 and 300 < ly + 12 and ly - 12 < 900)
        pieces = list(zip(found.points, found.points[1:]))
        self.assertTrue(any(min(a[0], b[0]) - 0.01 <= lx <= max(a[0], b[0]) + 0.01 and min(a[1], b[1]) - 0.01 <= ly <= max(a[1], b[1]) + 0.01
                            for a, b in pieces))

    def test_no_route_within_budget_falls_back_to_straight(self):
        walls = tuple(("w{}".format(i), box, "rect") for i, box in enumerate(
            [(-400.0, 250.0, 1600.0, 300.0), (-400.0, 460.0, 1600.0, 510.0), (-400.0, 250.0, -350.0, 510.0), (1550.0, 250.0, 1600.0, 510.0),
             (500.0, 250.0, 550.0, 510.0)]))
        found = R.route("orthogonal", RouteRequest(id="b", a=self.A, b=self.B, obstacles=walls))
        self.assertTrue(found.blocked)
        self.assertEqual(found.points, R.route("straight", RouteRequest(id="b", a=self.A, b=self.B)).points)

    def test_a_self_loop_is_five_points_out_of_e_and_into_n(self):
        found = R.route("orthogonal", RouteRequest(id="loop", a=self.A, b=self.A))
        self.assertEqual(found.points, [(164.0, 380.0), (200.0, 380.0), (200.0, 330.0), (80.0, 330.0), (80.0, 346.0)])

    def test_routes_avoid_running_along_each_other(self):
        a, b = End(box=(0.0, 0.0, 100.0, 60.0), id="a"), End(box=(600.0, 400.0, 700.0, 460.0), id="b")
        first = R.route("orthogonal", RouteRequest(id="1", a=a, b=b))
        second = R.route("orthogonal", RouteRequest(id="2", a=End(box=(0.0, 100.0, 100.0, 160.0), id="c"), b=b, others=(tuple(first.points),)))
        shared = [p for p in second.points[1:-1] if p in first.points[1:-1]]
        self.assertEqual(shared, [])


class StraightIsTodaysRoute(unittest.TestCase):
    """T-R1: every bound arrow of every golden scene re-routes through ``straight`` exactly as ``canvas._route`` does."""

    def test_every_golden_arrow(self):
        count = 0
        for path in sorted((FIXTURES / "display" / "scenes").glob("*.json")):
            scene = json.loads(path.read_text())
            by_id = {el["id"]: el for el in scene.get("elements") or []}
            for el in by_id.values():
                if el.get("type") != "arrow" or len(el.get("points") or []) < 2 or (el.get("style") or {}).get("route"):
                    continue
                start, end = by_id.get(el.get("from")), by_id.get(el.get("to"))
                points = el["points"]
                expected = C._route(("element", start) if start else ("point", points[0]), ("element", end) if end else ("point", points[-1]),
                                    points[1:-1])
                fields = arrow.reroute(dict(el), start, end, {})
                with self.subTest(scene=path.stem, arrow=el["id"]):
                    self.assertEqual(json.dumps(fields["points"]), json.dumps(expected))
                    self.assertEqual({k: fields[k] for k in ("x", "y", "w", "h")}, C._geometry(expected))
                count += 1
        self.assertGreater(count, 5)

    def test_random_ends_and_waypoints(self):
        rng = random.Random(7)
        for index in range(300):
            start = {"id": "s", "type": rng.choice(["box", "ellipse", "diamond"]), "x": rng.randint(-500, 500), "y": rng.randint(-500, 500),
                     "w": rng.randint(0, 300), "h": rng.randint(0, 200)}
            end = {"id": "e", "type": rng.choice(["box", "ellipse", "note"]), "x": rng.randint(-500, 500), "y": rng.randint(-500, 500),
                   "w": rng.randint(1, 300), "h": rng.randint(1, 200)}
            middle = [[round(rng.uniform(-600, 600), 3), round(rng.uniform(-600, 600), 3)] for _ in range(rng.randint(0, 2))]
            el = {"id": "E-1", "type": "arrow", "from": "s", "to": "e", "points": [[0, 0]] + middle + [[1, 1]]}
            with self.subTest(case=index):
                self.assertEqual(arrow.reroute(el, start, end, {})["points"], C._route(("element", start), ("element", end), middle))


class DroppedInRouter(unittest.TestCase):
    """A new router is one file in the package: copied as ``zigzag.py``, the registry finds it and an arrow can use it (G10)."""

    NAME = "herdr_team.canvas_routers.zigzag"

    def setUp(self):
        folder = Path(tempfile.mkdtemp(prefix="router-drop-"))
        self.addCleanup(shutil.rmtree, folder, True)
        shutil.copy(FIXTURES / "layouts" / "router_zigzag.py", folder / "zigzag.py")
        self.folder = str(folder)
        R.__path__.append(self.folder)
        self.addCleanup(self._restore)
        R._reload()

    def _restore(self):
        if self.folder in R.__path__:
            R.__path__.remove(self.folder)
        sys.modules.pop(self.NAME, None)
        R._reload()

    def test_the_file_is_a_router_with_no_list_edited(self):
        self.assertEqual(R.modules()[-1], "zigzag")
        el = {"id": "E-1", "type": "arrow", "style": {"route": "zigzag"}, "points": [[0, 0], [1, 1]]}
        fields = arrow.reroute(el, {"id": "a", "type": "box", "x": 0, "y": 0, "w": 100, "h": 50}, {"id": "b", "type": "box", "x": 300, "y": 200, "w": 100, "h": 50}, {})
        self.assertEqual(fields["points"], [[50, 25], [350, 25], [350, 225]])

    def test_taking_it_out_restores_the_names(self):
        self._restore()
        self.assertEqual(R.names(), ["straight", "curved", "curve", "orthogonal", "elbow"])


if __name__ == "__main__":
    unittest.main()
