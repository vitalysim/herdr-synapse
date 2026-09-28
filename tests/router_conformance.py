"""The contract every registered router keeps (canvas v2 phase 2, 3): a mixin a test case runs over a router.

``RouterConformance.check_router(name)`` routes a set of requests (ends of every
outline, points, waypoints, obstacles) and holds the router to: deterministic,
the first point on ``a``'s outline side and the last on ``b``'s (within the end
gap), coordinates rounded to two decimals, and a self-loop that leaves its node.
``maze`` builds seeded obstacle fields for the orthogonal router's own checks.
"""
from __future__ import annotations

import random
from typing import List, Tuple

from herdr_team import canvas_routers as R
from herdr_team.canvas_routers import End, RouteRequest

Box = Tuple[float, float, float, float]


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


class RouterConformance:
    """Mixin for ``unittest.TestCase``."""

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
