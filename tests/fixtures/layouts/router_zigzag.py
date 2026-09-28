"""A test-only router (canvas v2 phase 2, G10): from ``a`` to ``b`` in one horizontal and one vertical piece.

Copied into the ``canvas_routers`` package's search path as ``zigzag.py`` by
``tests/test_canvas_routers.py``: the registry finds it with no other edit, and
an arrow can name it as its ``route`` style.
"""
from __future__ import annotations

from herdr_team.canvas_routers import Route, RouteRequest, Router, anchor

ORDER = 900


def route(request: RouteRequest) -> Route:
    (ax, ay), (bx, by) = anchor(request.a), anchor(request.b)
    return Route(points=[(ax, ay), (bx, ay), (bx, by)])


ROUTERS = (Router(name="zigzag", route=route, doc="one horizontal and one vertical piece (a test router)"),)
