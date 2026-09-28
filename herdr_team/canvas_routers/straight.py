"""``straight``: straight pieces through the waypoints, each bound end clipped to its outline.

This is the route every arrow had before phase 2 (``canvas._route``), exactly:
an arrow without ``style.route`` keeps its geometry byte for byte (T-R1).
"""
from __future__ import annotations

from herdr_team.canvas_routers import Route, RouteRequest, Router, straight_points

#: Its place in the registration order.
ORDER = 10


def route(request: RouteRequest) -> Route:
    return Route(points=straight_points(request))


ROUTERS = (Router(name="straight", route=route, doc="straight pieces through the waypoints, ends clipped to their outlines"),)
