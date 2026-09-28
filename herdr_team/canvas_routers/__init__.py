"""The edge router registry (canvas v2 phase 2, ``.local/prd/canvas-v2-phase2.md`` 3): one module per router.

A router draws one connector between two ends: it takes the ends' boxes and
outlines, optional waypoints (a layout's bend hints, or an arrow's stored
middle points) and the obstacles around, and returns the points of the route,
where its label goes, and how to draw it (straight pieces, a curve, or rounded
elbows). The arrow kind picks a router by the arrow's ``style.route``
(``Kind.reroute``); a graph routes all its edges with one (``route_many``).

Every public module of this package that exports ``ROUTERS`` is registered,
in the order its ``ORDER`` says (the layout registry's discovery), so a new
router is one module and nothing else. ``_grid`` and ``_astar`` are the
orthogonal router's helpers.

Pure Python, stdlib only, 3.9. Nothing here imports ``canvas`` or ``canvas_kinds``.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from herdr_team.canvas_layouts._discovery import discover

API_VERSION = 1
#: The outlines a router clips an end to (``canvas_kinds.OUTLINES``).
OUTLINES = ("rect", "ellipse", "diamond")
SIDES = ("n", "e", "s", "w")

Point = Tuple[float, float]
Box = Tuple[float, float, float, float]  # (x0, y0, x1, y1)


class RouteError(ValueError):
    """A malformed route request; names the field."""

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(message)
        self.field = field_name


@dataclass(frozen=True)
class End:
    """One end of a route: the bound element's box (or a point as ``(x, y, x, y)``) and its outline."""

    box: Box
    outline: str = "rect"
    id: Optional[str] = None
    #: The side it prefers (``n``, ``e``, ``s``, ``w``: a layout's port); None lets the router pick.
    side: Optional[str] = None

    @property
    def is_point(self) -> bool:
        return self.box[0] == self.box[2] and self.box[1] == self.box[3]

    @property
    def center(self) -> Point:
        return (self.box[0] + self.box[2]) / 2.0, (self.box[1] + self.box[3]) / 2.0


@dataclass(frozen=True)
class RouteRequest:
    id: str
    a: End
    b: End
    #: Waypoints in order: a layout's hints, or a stored arrow's middle points.
    via: Tuple[Point, ...] = ()
    #: The label pill's ``(w, h)``, when the connector has one.
    label: Optional[Tuple[float, float]] = None
    #: ``(id, box, outline)`` of what the route keeps clear of; never the ends or a container of an end.
    obstacles: Tuple[Tuple[str, Box, str], ...] = ()
    #: Routes already placed in this batch: the orthogonal router avoids running along them.
    others: Tuple[Tuple[Point, ...], ...] = ()
    clearance: float = 20
    #: The corner radius of a rounded elbow.
    radius: float = 8
    #: This edge's index among the edges leaving the same side of the same node, and how many there are.
    slot: Tuple[int, int] = (0, 1)
    #: The slot at the ``b`` end (a graph's router fills it; the default is alone on its side).
    slot_b: Tuple[int, int] = (0, 1)
    #: The least room between two ports on one side.
    port_spacing: float = 12


@dataclass(frozen=True)
class Route:
    #: From ``a``'s outline to ``b``'s; an orthogonal route has axis-aligned segments only.
    points: List[Point]
    #: The label pill's centre; None lets the arrow's own label placement decide.
    label_at: Optional[Point] = None
    #: Draw through curve pieces (``canvas_geometry.curve_pieces``).
    curve: bool = False
    #: Draw bends rounded with this radius.
    corner: float = 0
    #: No clear route within budget: this is the straight fallback.
    blocked: bool = False


@dataclass(frozen=True)
class Router:
    name: str
    route: Callable[[RouteRequest], Route]
    aliases: Tuple[str, ...] = ()
    doc: str = ""


_REGISTRY: Dict[str, Router] = {}
_ALIASES: Dict[str, str] = {}
_OWNER: Dict[str, str] = {}
_LOADED = False


def register(router: Router) -> Router:
    if not callable(router.route):
        raise ValueError("router {}: route is not callable".format(router.name))
    for name in (router.name,) + tuple(router.aliases):
        taken = _REGISTRY.get(name) or _REGISTRY.get(_ALIASES.get(name, ""))
        if taken is not None and taken is not router:
            raise ValueError("router name {} is registered twice".format(name))
    _REGISTRY[router.name] = router
    for alias in router.aliases:
        _ALIASES[alias] = router.name
    return router


def _register_module(module: Any, owner: str) -> None:
    for router in getattr(module, "ROUTERS", ()):
        register(router)
        _OWNER[router.name] = owner


def modules() -> List[str]:
    return [name for _order, name, _module in discover(__name__, __path__, "ROUTERS")]


def load() -> None:
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        for _order, short, module in discover(__name__, __path__, "ROUTERS"):
            _register_module(module, "{}.{}".format(__name__, short))
    except BaseException:
        _LOADED = False
        raise


def _reload() -> None:
    global _LOADED
    _REGISTRY.clear()
    _ALIASES.clear()
    _OWNER.clear()
    _LOADED = False
    load()


def _load_extra(module_name: str) -> None:
    import importlib

    load()
    _register_module(importlib.import_module(module_name), module_name)


def _unload(module_name: str) -> None:
    for name, owner in list(_OWNER.items()):
        if owner != module_name:
            continue
        router = _REGISTRY.pop(name, None)
        del _OWNER[name]
        for alias in (router.aliases if router is not None else ()):
            _ALIASES.pop(alias, None)
    sys.modules.pop(module_name, None)


def get(name: Any) -> Optional[Router]:
    load()
    if not isinstance(name, str):
        return None
    return _REGISTRY.get(name) or _REGISTRY.get(_ALIASES.get(name, ""))


def routers() -> List[Router]:
    load()
    return list(_REGISTRY.values())


def names(aliases: bool = True) -> List[str]:
    out: List[str] = []
    for router in routers():
        out.append(router.name)
        if aliases:
            out.extend(router.aliases)
    return out


def _check(request: RouteRequest) -> None:
    for end in ("a", "b"):
        value = getattr(request, end)
        if value.outline not in OUTLINES:
            raise RouteError(end + ".outline", "outline is one of {}".format(", ".join(OUTLINES)))
        if value.side is not None and value.side not in SIDES:
            raise RouteError(end + ".side", "side is one of n, e, s, w")
        if value.box[2] < value.box[0] or value.box[3] < value.box[1]:
            raise RouteError(end + ".box", "a box is (x0, y0, x1, y1) with x0 <= x1 and y0 <= y1")


def _r2(value: float) -> float:
    rounded = round(float(value), 2)
    return float(int(rounded)) if rounded == int(rounded) else rounded


def route(name: str, request: RouteRequest) -> Route:
    """The route of ``request`` by the router ``name`` (aliases resolved), every coordinate rounded to two decimals."""
    router = get(name)
    if router is None:
        raise RouteError("route", "{!r} is not a router; the routers are {}".format(name, ", ".join(names())))
    _check(request)
    found = router.route(request)
    return replace(found, points=[(_r2(x), _r2(y)) for x, y in found.points],
                   label_at=(_r2(found.label_at[0]), _r2(found.label_at[1])) if found.label_at is not None else None)


def route_many(name: str, requests: Sequence[RouteRequest]) -> Dict[str, Route]:
    """Every request's route in input order; each sees the routes before it as ``others``. Edges that leave the same
    side of the same node get spread slots first (``with_slots``)."""
    out: Dict[str, Route] = {}
    placed: List[Tuple[Point, ...]] = []
    for request in with_slots(requests):
        found = route(name, replace(request, others=tuple(request.others) + tuple(placed)))
        out[request.id] = found
        placed.append(tuple(found.points))
    return out


# --------------------------------------------------------------------------
# shared by the routers


#: The gap between an end's outline and the route's first or last point (``canvas._clip``).
END_GAP = 4.0


def clip(end: End, toward: Point, gap: float = END_GAP) -> Point:
    """Where the line from the end's centre toward ``toward`` leaves its outline, plus a small gap (``canvas._clip``,
    exactly: the straight router must reproduce every stored arrow byte for byte)."""
    x0, y0, x1, y1 = end.box
    x1, y1 = max(x1, x0 + 1.0), max(y1, y0 + 1.0)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    dx, dy = float(toward[0]) - cx, float(toward[1]) - cy
    length = math.hypot(dx, dy)
    a, b = (x1 - x0) / 2.0, (y1 - y0) / 2.0
    if length == 0:
        return _r2(cx), _r2(cy)
    if end.outline == "ellipse":
        t = 1.0 / math.sqrt((dx / a) ** 2 + (dy / b) ** 2)
    elif end.outline == "diamond":
        t = 1.0 / (abs(dx) / a + abs(dy) / b)
    else:
        t = min(a / abs(dx) if dx else float("inf"), b / abs(dy) if dy else float("inf"))
    if t >= 1.0:
        return _r2(cx), _r2(cy)
    t = min(1.0, t + gap / length)
    return _r2(cx + dx * t), _r2(cy + dy * t)


def anchor(end: End) -> Point:
    """The point an end aims from: a point end's point, else its box's centre."""
    if end.is_point:
        return end.box[0], end.box[1]
    x0, y0, x1, y1 = end.box
    return (x0 + max(x1, x0 + 1.0)) / 2.0, (y0 + max(y1, y0 + 1.0)) / 2.0


def straight_points(request: RouteRequest) -> List[Point]:
    """The straight route: each bound end clipped to its outline toward its neighbouring point (``canvas._route``)."""
    a, b = anchor(request.a), anchor(request.b)
    middle = [(_r2(p[0]), _r2(p[1])) for p in request.via]
    first_toward = request.via[0] if request.via else b
    last_toward = request.via[-1] if request.via else a
    p0 = (_r2(a[0]), _r2(a[1])) if request.a.is_point else clip(request.a, first_toward)
    pn = (_r2(b[0]), _r2(b[1])) if request.b.is_point else clip(request.b, last_toward)
    return [p0] + middle + [pn]


def facing(box: Box, toward: Point) -> str:
    """The side of ``box`` that faces ``toward`` along the dominant axis (measured against the box's own size)."""
    cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
    dx, dy = toward[0] - cx, toward[1] - cy
    w, h = max(box[2] - box[0], 1.0), max(box[3] - box[1], 1.0)
    if abs(dx) / w >= abs(dy) / h:
        return "e" if dx >= 0 else "w"
    return "s" if dy >= 0 else "n"


def sides(request: RouteRequest) -> Tuple[Optional[str], Optional[str]]:
    """The sides a route leaves ``a`` by and enters ``b`` by (None for a point end)."""
    first_toward = request.via[0] if request.via else anchor(request.b)
    last_toward = request.via[-1] if request.via else anchor(request.a)
    side_a = None if request.a.is_point else (request.a.side or facing(request.a.box, first_toward))
    side_b = None if request.b.is_point else (request.b.side or facing(request.b.box, last_toward))
    return side_a, side_b


def with_slots(requests: Sequence[RouteRequest]) -> List[RouteRequest]:
    """The requests with ``slot`` and ``slot_b`` set: the routes on one side of one element, ordered by where their
    other end lies along that side, so they leave side by side without crossing."""
    users: Dict[Tuple[str, str], List[Tuple[float, int, str]]] = {}
    for index, request in enumerate(requests):
        if request.a.id is not None and request.a.id == request.b.id:
            continue
        side_a, side_b = sides(request)
        for end, side, other, which in ((request.a, side_a, request.via[0] if request.via else anchor(request.b), "a"),
                                        (request.b, side_b, request.via[-1] if request.via else anchor(request.a), "b")):
            if end.id is None or side is None:
                continue
            along = other[0] if side in ("n", "s") else other[1]
            users.setdefault((end.id, side), []).append((along, index, which))
    slots: Dict[Tuple[int, str], Tuple[int, int]] = {}
    for entries in users.values():
        entries.sort()
        for k, (_along, index, which) in enumerate(entries):
            slots[(index, which)] = (k, len(entries))
    return [replace(request, slot=slots.get((index, "a"), request.slot), slot_b=slots.get((index, "b"), request.slot_b))
            for index, request in enumerate(requests)]
