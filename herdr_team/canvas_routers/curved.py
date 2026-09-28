"""``curved`` (alias ``curve``): the straight route's points drawn as a smooth curve through them.

The arrow draws ``canvas_geometry.curve_pieces`` over the points, as an arrow
with ``curve: true`` always has; two points bow gently to one side.
"""
from __future__ import annotations

from herdr_team.canvas_routers import Route, RouteRequest, Router, straight_points

#: Its place in the registration order.
ORDER = 20


def route(request: RouteRequest) -> Route:
    return Route(points=straight_points(request), curve=True)


ROUTERS = (Router(name="curved", route=route, aliases=("curve",), doc="a smooth curve through the waypoints"),)
