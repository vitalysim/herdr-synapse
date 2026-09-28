"""The layout engine registry (canvas v2 phase 2, ``.local/prd/canvas-v2-phase2.md`` 2): one module per layout.

A layout places boxes: it takes sized nodes (``LNode``), the edges between them
(``LEdge``) and optional nested groups (``LGroup``), and returns the top-left
corner of every node in the request's own frame, plus what a router needs to
draw the edges (bend hints, preferred sides, label spots). Kinds build the
request from their members and apply the result; nothing here knows about
elements, the canvas or its kinds.

Every public module of this package that exports ``LAYOUTS`` is registered,
in the order its ``ORDER`` says (``_discovery``), so a new layout is one module
and nothing else. Modules whose name starts with ``_`` are shared helpers
(``_rank``, ``_order``, ``_position`` for the layered layout, ``_tidy`` for trees,
``_pins`` for the pin rules every layout shares).

Every layout keeps one contract (``tests/layout_conformance.py``): it is
deterministic (the same request gives the same result on 3.9 and 3.14, and the
input order of nodes and edges does not matter, only their ``order`` values);
a pinned node sits exactly on its pin; unpinned nodes keep ``gap`` apart; a
request seeded with its own result comes back unchanged; groups hug their
members; and nothing hangs on cycles, self-loops or duplicates. ``run`` is the
only entry point callers use: it validates the request, runs the layout,
checks the pins and rounds every coordinate to two decimals.

Pure Python, stdlib only, 3.9. Nothing here imports ``canvas`` or ``canvas_kinds``.
"""
from __future__ import annotations

import math
import sys
from dataclasses import dataclass, field, replace
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team.canvas_layouts._discovery import discover

#: The version of this registry's contract; bumped when a dataclass or ``run`` changes meaning.
API_VERSION = 1
#: Which way ranks (or tree levels) run: ``down`` and ``right`` are what agents ask for; ``up`` and ``left`` come from Mermaid.
DIRECTIONS = ("down", "right", "up", "left")
#: The largest side a laid-out drawing may have (the canvas's ``MAX_SIZE``); a bigger one is the caller's ``too_big``.
MAX_SIZE = 20_000

Point = Tuple[float, float]
Box = Tuple[float, float, float, float]  # (x0, y0, x1, y1)


class LayoutError(ValueError):
    """A malformed request (an edge to an unknown node, an option the layout does not take); names the field."""

    def __init__(self, field_name: str, message: str) -> None:
        super().__init__(message)
        self.field = field_name


@dataclass(frozen=True)
class LNode:
    """A box to place: ``w`` x ``h``, with its input ``order`` (every tie breaks on it, then on ``id``)."""

    id: str
    w: float
    h: float
    order: int
    #: The innermost group the node belongs to.
    group: Optional[str] = None
    #: A fixed top-left corner, in the request's frame: never moved.
    pin: Optional[Point] = None
    #: Who set the pin (``human`` or ``agent``); layouts treat both alike, callers report them.
    pin_by: Optional[str] = None
    #: The node's previous top-left corner, honoured when the request is ``incremental``.
    seed: Optional[Point] = None
    #: Per layout: a tree reads ``{"parent": id, "side": "left" | "right"}``.
    data: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class LEdge:
    """A relation from ``a`` to ``b``; ``directed=False`` is ``a -- b`` (ranked as ``a -> b``, drawn without heads)."""

    id: str
    a: str
    b: str
    directed: bool = True
    weight: float = 1.0
    minlen: int = 1
    #: The label pill's ``(w, h)``: a layered layout keeps room for it on the edge.
    label: Optional[Tuple[float, float]] = None


@dataclass(frozen=True)
class LGroup:
    """A nested group of nodes (a graph's ``groups``); ``pad`` is left, top (the title band), right, bottom."""

    id: str
    parent: Optional[str] = None
    pad: Tuple[float, float, float, float] = (20, 40, 20, 20)


@dataclass(frozen=True)
class LayoutRequest:
    nodes: Tuple[LNode, ...]
    edges: Tuple[LEdge, ...] = ()
    groups: Tuple[LGroup, ...] = ()
    direction: str = "down"
    #: Between neighbours in a rank or a stack.
    gap: float = 40
    #: Between ranks or tree levels.
    rank_gap: float = 80
    #: Sets of nodes that share a rank.
    same_rank: Tuple[Tuple[str, ...], ...] = ()
    #: Relative order inside a rank: left to right for down/up, top to bottom for right/left.
    order: Tuple[Tuple[str, ...], ...] = ()
    #: Per layout (``Layout.options`` lists the names it takes).
    options: Mapping[str, Any] = field(default_factory=dict)
    incremental: bool = True


@dataclass(frozen=True)
class LayoutResult:
    #: The top-left corner of every node, in the request's frame (a pin exactly as given).
    positions: Dict[str, Point]
    #: Every group's box (its members plus ``pad``).
    groups: Dict[str, Box] = field(default_factory=dict)
    #: Per edge: the points a router bends through (the layered layout's dummy nodes).
    hints: Dict[str, List[Point]] = field(default_factory=dict)
    #: Per edge: the preferred sides at ``a`` and at ``b`` (``n``, ``e``, ``s``, ``w``).
    ports: Dict[str, Tuple[str, str]] = field(default_factory=dict)
    #: Per edge: the label centre the layout kept room for.
    labels: Dict[str, Point] = field(default_factory=dict)
    bbox: Box = (0, 0, 0, 0)
    notes: Tuple[str, ...] = ()
    #: ``crossings``, ``moved`` (seeded nodes that moved more than 20), ``iterations``.
    stats: Mapping[str, float] = field(default_factory=dict)


@dataclass(frozen=True)
class Layout:
    """One registered layout."""

    name: str
    run: Callable[[LayoutRequest], LayoutResult]
    aliases: Tuple[str, ...] = ()
    #: It uses edges (the stacks and the grid do not).
    edges: bool = True
    #: It honours groups.
    groups: bool = False
    directions: Tuple[str, ...] = DIRECTIONS
    #: The default router of a graph laid out with it (``canvas_routers``).
    router: str = "orthogonal"
    #: The option names it accepts; any other is a ``LayoutError``.
    options: Tuple[str, ...] = ()
    doc: str = ""
    #: It holds pins. A stack or a grid is an order: ``run`` takes pins out of its request and notes ``pin_ignored``.
    pins: bool = True


_REGISTRY: Dict[str, Layout] = {}
_ALIASES: Dict[str, str] = {}
_OWNER: Dict[str, str] = {}
_LOADED = False


def register(layout: Layout) -> Layout:
    """Add ``layout``; a second layout or alias with a taken name, or an unknown direction, is a programming error."""
    for direction in layout.directions:
        if direction not in DIRECTIONS:
            raise ValueError("layout {}: direction {!r} is not one of {}".format(layout.name, direction, ", ".join(DIRECTIONS)))
    if not callable(layout.run):
        raise ValueError("layout {}: run is not callable".format(layout.name))
    for name in (layout.name,) + tuple(layout.aliases):
        taken = _REGISTRY.get(name) or _REGISTRY.get(_ALIASES.get(name, ""))
        if taken is not None and taken is not layout:
            raise ValueError("layout name {} is registered twice".format(name))
    _REGISTRY[layout.name] = layout
    for alias in layout.aliases:
        _ALIASES[alias] = layout.name
    return layout


def _register_module(module: Any, owner: str) -> None:
    for layout in getattr(module, "LAYOUTS", ()):
        register(layout)
        _OWNER[layout.name] = owner


def modules() -> List[str]:
    """The layout modules found in this package (their short names), in registration order."""
    return [name for _order, name, _module in discover(__name__, __path__, "LAYOUTS")]


def load() -> None:
    """Import every layout module in this package once; each registers its ``LAYOUTS``."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        for _order, short, module in discover(__name__, __path__, "LAYOUTS"):
            _register_module(module, "{}.{}".format(__name__, short))
    except BaseException:
        _LOADED = False
        raise


def _reload() -> None:
    """Test hook: forget every registration and find the layout modules again (after a test adds a module file)."""
    global _LOADED
    _REGISTRY.clear()
    _ALIASES.clear()
    _OWNER.clear()
    _LOADED = False
    load()


def _load_extra(module_name: str) -> None:
    """Test hook: register one more module's ``LAYOUTS``."""
    import importlib

    load()
    _register_module(importlib.import_module(module_name), module_name)


def _unload(module_name: str) -> None:
    """Test hook: take the layouts ``_load_extra`` registered from ``module_name`` out again."""
    for name, owner in list(_OWNER.items()):
        if owner != module_name:
            continue
        layout = _REGISTRY.pop(name, None)
        del _OWNER[name]
        for alias in (layout.aliases if layout is not None else ()):
            _ALIASES.pop(alias, None)
    sys.modules.pop(module_name, None)


def get(name: Any) -> Optional[Layout]:
    """The layout named ``name`` (aliases resolved), or None."""
    load()
    if not isinstance(name, str):
        return None
    return _REGISTRY.get(name) or _REGISTRY.get(_ALIASES.get(name, ""))


def layouts() -> List[Layout]:
    """Every registered layout, in registration order."""
    load()
    return list(_REGISTRY.values())


def names(aliases: bool = True) -> List[str]:
    """Every layout name, each followed by its aliases when ``aliases``."""
    out: List[str] = []
    for layout in layouts():
        out.append(layout.name)
        if aliases:
            out.extend(layout.aliases)
    return out


# --------------------------------------------------------------------------
# validation and the contract


def _finite(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)


def _point_ok(value: Any) -> bool:
    return isinstance(value, (tuple, list)) and len(value) == 2 and all(_finite(v) for v in value)


def validate(layout: Layout, request: LayoutRequest) -> None:
    """Refuse a malformed request with a ``LayoutError`` naming the field."""
    if request.direction not in layout.directions:
        raise LayoutError("direction", "layout {} takes direction {}, not {!r}".format(layout.name, " | ".join(layout.directions), request.direction))
    for name in ("gap", "rank_gap"):
        value = getattr(request, name)
        if not _finite(value) or value < 0:
            raise LayoutError(name, "{} must be a number of 0 or more".format(name))
    for key in request.options:
        if key not in layout.options:
            raise LayoutError("options." + str(key), "layout {} takes {}".format(
                layout.name, "the options " + ", ".join(layout.options) if layout.options else "no options"))
    ids = set()
    for index, node in enumerate(request.nodes):
        where = "nodes[{}]".format(index)
        if not isinstance(node.id, str) or not node.id:
            raise LayoutError(where + ".id", "a node id is a non-empty string")
        if node.id in ids:
            raise LayoutError(where + ".id", "node {} appears twice".format(node.id))
        ids.add(node.id)
        if not (_finite(node.w) and _finite(node.h)) or node.w < 0 or node.h < 0:
            raise LayoutError(where, "node {} needs a finite size of 0 or more".format(node.id))
        if not isinstance(node.order, int) or isinstance(node.order, bool):
            raise LayoutError(where + ".order", "node {}: order is an int".format(node.id))
        for name in ("pin", "seed"):
            value = getattr(node, name)
            if value is not None and not _point_ok(value):
                raise LayoutError("{}.{}".format(where, name), "node {}: {} is (x, y)".format(node.id, name))
    group_ids = set()
    for index, group in enumerate(request.groups):
        if not isinstance(group.id, str) or not group.id or group.id in group_ids or group.id in ids:
            raise LayoutError("groups[{}].id".format(index), "group ids are unique across nodes and groups")
        if len(group.pad) != 4 or not all(_finite(p) and p >= 0 for p in group.pad):
            raise LayoutError("groups[{}].pad".format(index), "pad is four numbers of 0 or more")
        group_ids.add(group.id)
    parents = {group.id: group.parent for group in request.groups}
    for index, group in enumerate(request.groups):
        if group.parent is not None and group.parent not in group_ids:
            raise LayoutError("groups[{}].parent".format(index), "group {} is not one of the groups".format(group.parent))
        seen, cursor = {group.id}, group.parent
        while cursor is not None:
            if cursor in seen:
                raise LayoutError("groups[{}].parent".format(index), "groups {} nest in a cycle".format(group.id))
            seen.add(cursor)
            cursor = parents.get(cursor)
    for index, node in enumerate(request.nodes):
        if node.group is not None and node.group not in group_ids:
            raise LayoutError("nodes[{}].group".format(index), "group {} is not one of the groups".format(node.group))
    edge_ids = set()
    for index, edge in enumerate(request.edges):
        where = "edges[{}]".format(index)
        if not isinstance(edge.id, str) or not edge.id or edge.id in edge_ids:
            raise LayoutError(where + ".id", "edge ids are unique non-empty strings")
        edge_ids.add(edge.id)
        for end in ("a", "b"):
            if getattr(edge, end) not in ids:
                raise LayoutError("{}.{}".format(where, end), "{} is not one of the nodes".format(getattr(edge, end)))
        if not isinstance(edge.minlen, int) or isinstance(edge.minlen, bool) or edge.minlen < 0:
            raise LayoutError(where + ".minlen", "minlen is an int of 0 or more")
        if not _finite(edge.weight) or edge.weight < 0:
            raise LayoutError(where + ".weight", "weight is a number of 0 or more")
        if edge.label is not None and not (_point_ok(edge.label) and edge.label[0] >= 0 and edge.label[1] >= 0):
            raise LayoutError(where + ".label", "label is the pill's (w, h)")
    for name in ("same_rank", "order"):
        for index, members in enumerate(getattr(request, name)):
            for member in members:
                if member not in ids:
                    raise LayoutError("{}[{}]".format(name, index), "{} is not one of the nodes".format(member))


def _r2(value: float) -> float:
    rounded = round(float(value), 2)
    return float(int(rounded)) if rounded == int(rounded) else rounded


def _r2p(point: Sequence[float]) -> Point:
    return (_r2(point[0]), _r2(point[1]))


def run(name: str, request: LayoutRequest) -> LayoutResult:
    """Validate ``request``, run the layout ``name`` on it, then enforce the contract.

    Every pin is held exactly (else ``LayoutError``: that is a bug in the layout),
    every other coordinate is rounded to two decimals, and ``bbox`` covers every
    node and group. A layout that does not hold pins (``Layout.pins`` False) gets the
    request without them, and the result notes ``pin_ignored <id>`` for each.
    """
    layout = get(name)
    if layout is None:
        raise LayoutError("layout", "{!r} is not a layout; the layouts are {}".format(name, ", ".join(names())))
    validate(layout, request)
    notes: List[str] = []
    if not layout.pins and any(node.pin is not None for node in request.nodes):
        notes += ["pin_ignored {}".format(node.id) for node in sorted(request.nodes, key=lambda n: (n.order, n.id)) if node.pin is not None]
        request = replace(request, nodes=tuple(replace(node, pin=None, pin_by=None) for node in request.nodes))
    if not request.nodes:
        return LayoutResult(positions={}, notes=tuple(notes))
    result = layout.run(request)
    positions: Dict[str, Point] = {}
    for node in request.nodes:
        found = result.positions.get(node.id)
        if found is None or not _point_ok(found):
            raise LayoutError("positions", "layout {} placed no box for {}".format(layout.name, node.id))
        if node.pin is not None:
            if abs(found[0] - node.pin[0]) > 1e-6 or abs(found[1] - node.pin[1]) > 1e-6:
                raise LayoutError("positions", "layout {} moved the pinned node {}".format(layout.name, node.id))
            positions[node.id] = (float(node.pin[0]), float(node.pin[1]))
        else:
            positions[node.id] = _r2p(found)
    groups = {gid: (_r2(b[0]), _r2(b[1]), _r2(b[2]), _r2(b[3])) for gid, b in result.groups.items()}
    size = {node.id: (node.w, node.h) for node in request.nodes}
    xs0 = [p[0] for p in positions.values()] + [b[0] for b in groups.values()]
    ys0 = [p[1] for p in positions.values()] + [b[1] for b in groups.values()]
    xs1 = [positions[n][0] + size[n][0] for n in positions] + [b[2] for b in groups.values()]
    ys1 = [positions[n][1] + size[n][1] for n in positions] + [b[3] for b in groups.values()]
    bbox = (_r2(min(xs0)), _r2(min(ys0)), _r2(max(xs1)), _r2(max(ys1)))
    return LayoutResult(positions=positions, groups=groups,
                        hints={eid: [_r2p(p) for p in points] for eid, points in result.hints.items()},
                        ports=dict(result.ports), labels={eid: _r2p(p) for eid, p in result.labels.items()}, bbox=bbox,
                        notes=tuple(notes) + tuple(result.notes), stats=dict(result.stats))
