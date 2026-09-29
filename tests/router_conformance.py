"""The contract every registered router keeps (canvas v2 phase 2, 3): a mixin a test case runs over a router.

``RouterConformance.check_router(name)`` routes a set of requests (ends of every
outline, points, waypoints, obstacles) and holds the router to: deterministic,
the first point on ``a``'s outline side and the last on ``b``'s (within the end
gap), coordinates rounded to two decimals, and a self-loop that leaves its node.
``maze`` builds seeded obstacle fields for the orthogonal router's own checks.

``check_route_quality(name)`` is the second contract (canvas v2 layout clarity,
3.4): what one route costs a reader, and what a *batch* of routes costs.

* per route, on the requests where a short route is the right answer: detour,
  turn-backs and bends inside ``canvas_readability``'s bounds;
* on the maze requests, where a long route genuinely is correct, a
  non-regression bound only - the numbers the fixed code measures, recorded
  here as a guard and not as a quality claim;
* per batch, every label pill clear of the obstacles, of the other routes'
  lines and of the other pills, and on its own line;
* per batch, a fixed point: routing it again with the first result fed back
  returns the same polylines.

``batches()`` adds the two shapes the batch rules are about: a ``fan`` of seven
edges off one small hub, and a ``corridor`` of five parallel edges through one
gap. Both failed the label rule before the source-anchored placement landed:
their pills stacked into one column in a neighbour's corridor.
"""
from __future__ import annotations

import random
from dataclasses import replace
from typing import Dict, List, Sequence, Tuple

from herdr_team import canvas_readability as RD
from herdr_team import canvas_routers as R
from herdr_team.canvas_routers import End, RouteRequest

Box = Tuple[float, float, float, float]

#: What one route may cost a reader where a short route is the right answer (the non-maze requests).
MAX_MDETOUR = 1.6
MAX_REVERSALS = 2
MAX_BENDS = 8
#: What this build measures on the adversarial obstacle fields, where a long route can be the right answer: a
#: regression guard, not a quality claim. The seeded field of ``maze(3)`` happens to leave a clean corridor and the
#: router takes it (detour 1.00, no turn-backs, two bends), so these are that plus a little room. A field that
#: really boxed a route in would need its own numbers, and they would go here with the seed that produced them.
MAZE_MDETOUR = 1.30
MAZE_REVERSALS = 1
MAZE_BENDS = 4
#: How near a pill may come to another route's line or another pill.
PILL_GAP = 2.0
#: How many routes of a batch may take a different line once they can see every other route, by batch. A recorded
#: number, not a target: ``route_many`` is one ordered pass, so the first route drawn cannot see the last.
FIXED_POINT_SLACK = {"fan": 2}


def near_outline(point, end: End, slack: float = R.END_GAP + 1.0) -> bool:
    """Whether ``point`` sits on ``end``'s outline (a point end: on the point), give or take ``slack``."""
    if end.is_point:
        return abs(point[0] - end.box[0]) < 0.01 and abs(point[1] - end.box[1]) < 0.01
    x0, y0, x1, y1 = end.box
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    a, b = (x1 - x0) / 2.0 + 1.5 * slack, (y1 - y0) / 2.0 + 1.5 * slack
    dx, dy = abs(point[0] - cx), abs(point[1] - cy)
    if end.outline == "ellipse":
        inner = ((dx / max(a - 2 * slack, 0.5)) ** 2 + (dy / max(b - 2 * slack, 0.5)) ** 2) >= 0.8
        return (dx / a) ** 2 + (dy / b) ** 2 <= 1.0 + 1e-9 and inner
    if end.outline == "diamond":
        return dx / a + dy / b <= 1.0 + 1e-9 and dx / max(a - 2 * slack, 0.5) + dy / max(b - 2 * slack, 0.5) >= 0.8
    return dx <= a + 1e-9 and dy <= b + 1e-9 and (dx >= a - 2 * slack - 1e-9 or dy >= b - 2 * slack - 1e-9)


def maze(seed: int, count: int = 12) -> List[Tuple[str, Box, str]]:
    """Seeded boxes scattered between (200, 0) and (960, 860), clear of the ends at (0, 380) and (1040, 380)."""
    rng = random.Random(seed)
    out = []
    while len(out) < count:
        w, h = rng.uniform(40, 160), rng.uniform(40, 160)
        x, y = rng.uniform(200, 960 - w), rng.uniform(0, 700)
        box = (round(x), round(y), round(x + w), round(y + h))
        if any(box[0] < o[1][2] + 50 and o[1][0] < box[2] + 50 and box[1] < o[1][3] + 50 and o[1][1] < box[3] + 50 for o in out):
            continue
        out.append(("o{}".format(len(out)), box, "rect"))
    return out


def requests() -> List[RouteRequest]:
    a = End(box=(0.0, 350.0, 160.0, 410.0), id="a")
    out = []
    for outline in R.OUTLINES:
        b = End(box=(1000.0, 500.0, 1200.0, 620.0), outline=outline, id="b")
        out.append(RouteRequest(id="plain-" + outline, a=a, b=b))
        out.append(RouteRequest(id="via-" + outline, a=a, b=b, via=((600.0, 200.0),)))
        out.append(RouteRequest(id="maze-" + outline, a=a, b=b, obstacles=tuple(maze(3)), label=(80.0, 24.0)))
    out.append(RouteRequest(id="points", a=End(box=(10.0, 10.0, 10.0, 10.0)), b=End(box=(400.0, 300.0, 400.0, 300.0))))
    out.append(RouteRequest(id="loop", a=a, b=a))
    return out


def batches() -> List[Tuple[str, List[RouteRequest]]]:
    """Batches a router draws in one go, each with its own label on every edge.

    ``fan``: seven edges leaving one 160x60 hub for seven targets spread down the right. ``corridor``: five parallel
    edges from five stacked sources to five stacked targets, through one gap between two walls. Between them they are
    every way a batch of routes can spoil each other - ports crowding one side, lines running alongside each other,
    and pills with nowhere to go that is not another route's corridor.
    """
    # The hub is as tall as its seven ports need (``graph.arrange`` grows one for real: ``(degree + 1) x spacing``).
    # A 60-unit side cannot hold seven wires side by side, and a batch whose ports are already in a bunch has no
    # clear spot left for any of its pills - which is a fit-policy failure, not a router one.
    hub = End(box=(0.0, 360.0, 160.0, 460.0), id="hub")
    fan = [RouteRequest(id="fan-{}".format(i), a=hub, label=(70.0, 24.0),
                        b=End(box=(700.0, 40.0 + i * 130.0, 900.0, 100.0 + i * 130.0), id="t{}".format(i)),
                        obstacles=tuple(("t{}".format(j), (700.0, 40.0 + j * 130.0, 900.0, 100.0 + j * 130.0), "rect")
                                        for j in range(7) if j != i))
           for i in range(7)]
    walls = (("upper", (300.0, -400.0, 420.0, 180.0), "rect"), ("lower", (300.0, 420.0, 420.0, 1000.0), "rect"))
    corridor = [RouteRequest(id="corridor-{}".format(i), label=(80.0, 24.0),
                             a=End(box=(0.0, i * 90.0, 160.0, 60.0 + i * 90.0), id="s{}".format(i)),
                             b=End(box=(700.0, i * 90.0, 860.0, 60.0 + i * 90.0), id="e{}".format(i)),
                             obstacles=walls + tuple(("s{}".format(j), (0.0, j * 90.0, 160.0, 60.0 + j * 90.0), "rect")
                                                     for j in range(5) if j != i)
                             + tuple(("e{}".format(j), (700.0, j * 90.0, 860.0, 60.0 + j * 90.0), "rect")
                                     for j in range(5) if j != i))
                for i in range(5)]
    return [("fan", fan), ("corridor", corridor)]


def quality_of(request: RouteRequest, found: "R.Route") -> Dict[str, float]:
    """What this one route costs a reader (``canvas_readability.edge_quality``)."""
    return RD.edge_quality(found.points, request.a.box, request.b.box, found.label_at,
                           [box for _id, box, _outline in request.obstacles])


def _meets(pill: Box, a: Sequence[float], b: Sequence[float]) -> bool:
    x0, x1 = min(a[0], b[0]) - PILL_GAP, max(a[0], b[0]) + PILL_GAP
    y0, y1 = min(a[1], b[1]) - PILL_GAP, max(a[1], b[1]) + PILL_GAP
    return pill[0] < x1 and x0 < pill[2] and pill[1] < y1 and y0 < pill[3]


class RouterConformance:
    """Mixin for ``unittest.TestCase``."""

    def check_route_quality(self, name: str) -> None:
        """What one route and one batch of routes cost a reader (canvas v2 layout clarity, 3.4)."""
        router = R.get(name)
        self.assertIsNotNone(router, name)
        if name != "orthogonal":
            return  # detours, turn-backs and pills are the orthogonal router's own vocabulary
        for request in requests():
            if request.id in ("loop", "points"):
                continue
            with self.subTest(router=name, request=request.id):
                found = R.route(name, request)
                if found.blocked:
                    continue
                q = quality_of(request, found)
                maze = request.id.startswith("maze")
                self.assertLessEqual(q["mdetour"], MAZE_MDETOUR if maze else MAX_MDETOUR)
                self.assertLessEqual(q["reversals"], MAZE_REVERSALS if maze else MAX_REVERSALS)
                self.assertLessEqual(q["bends"], MAZE_BENDS if maze else MAX_BENDS)
        for label, batch in batches():
            with self.subTest(router=name, batch=label):
                drawn = R.route_many(name, batch)
                self.assertEqual(set(drawn), {r.id for r in batch})
                pills: List[Box] = []
                for request in batch:
                    found = drawn[request.id]
                    if found.blocked or request.label is None:
                        continue
                    if found.label_at is None:
                        continue  # no spot on this line is clear: ``canvas_labels`` places it beside the line
                    w, h = request.label
                    cx, cy = found.label_at
                    pill = (cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0)
                    # on its own line
                    self.assertLessEqual(RD.point_to_poly(found.label_at, found.points), 2.0,
                                         "{}: the pill left its own line".format(request.id))
                    # clear of the obstacles, of every other route's line, and of every other pill
                    for _id, box, _outline in request.obstacles:
                        self.assertFalse(pill[0] < box[2] and box[0] < pill[2] and pill[1] < box[3] and box[1] < pill[3],
                                         "{}: the pill lands on {}".format(request.id, _id))
                    for other in batch:
                        if other.id == request.id or drawn[other.id].blocked:
                            continue
                        line = drawn[other.id].points
                        self.assertFalse(any(_meets(pill, a, b) for a, b in zip(line, line[1:])),
                                         "{}: the pill lands on {}'s line".format(request.id, other.id))
                    for box in pills:
                        self.assertFalse(pill[0] < box[2] and box[0] < pill[2] and pill[1] < box[3] and box[1] < pill[3],
                                         "{}: two pills on top of each other".format(request.id))
                    pills.append(pill)
                # A batch is a fixed point: each route re-drawn against the finished lines of **all** the others
                # comes back the same. ``route_many`` is one ordered pass, so a route drawn first cannot see the
                # ones drawn after it; this is the rule that says how much that costs.
                #
                # ``corridor`` - five parallel edges through one gap - has to be exact, and it is: before the
                # crossing cost and the second label pass it was not. ``fan`` is allowed the routes recorded in
                # ``FIXED_POINT_SLACK``: seven edges leaving one side, where the first one drawn would take a
                # different corridor if it could see the six it does not yet know about. Closing that would take a
                # second *routing* pass, and whether that is worth its budget is an open question, not a silent gap.
                lines = {r.id: tuple(drawn[r.id].points) for r in batch}
                changed = []
                for request in R.with_slots(batch):
                    others = tuple(request.others) + tuple(v for k, v in lines.items() if k != request.id)
                    again = R.route(name, replace(request, others=others))
                    if list(again.points) != list(lines[request.id]):
                        changed.append(request.id)
                self.assertLessEqual(len(changed), FIXED_POINT_SLACK.get(label, 0),
                                     "{}: these routes move on a second pass: {}".format(label, changed))

    def check_router(self, name: str) -> None:
        router = R.get(name)
        self.assertIsNotNone(router, name)
        for request in requests():
            with self.subTest(router=name, request=request.id):
                found = R.route(name, request)
                self.assertEqual(found, R.route(name, request), "deterministic")
                self.assertGreaterEqual(len(found.points), 2)
                for x, y in found.points:
                    self.assertEqual((round(x, 2), round(y, 2)), (x, y))
                if request.id == "loop":
                    if name == "orthogonal":
                        self.assertTrue(any(p[0] > request.a.box[2] or p[1] < request.a.box[1] for p in found.points))
                    continue
                if not found.blocked:
                    self.assertTrue(near_outline(found.points[0], request.a), (found.points[0], request.a))
                    self.assertTrue(near_outline(found.points[-1], request.b), (found.points[-1], request.b))



