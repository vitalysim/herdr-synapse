"""A test-only layout (canvas v2 phase 2, G10): boxes on a diagonal, in their order, ``gap`` apart on both axes.

Copied into the ``canvas_layouts`` package's search path as ``diagonal.py`` by
``tests/test_canvas_layouts.py``: the registry finds it with no other edit, a
graph can name it, and it keeps the layout contract (pins included).
"""
from __future__ import annotations

from herdr_team.canvas_layouts import Layout, LayoutRequest, LayoutResult
from herdr_team.canvas_layouts import _util

ORDER = 900


def diagonal(request: LayoutRequest) -> LayoutResult:
    nodes = _util.nodes_in_order(request)
    x = y = 0.0
    wanted = {}
    for node in nodes:
        wanted[node.id] = (x, y)
        x += node.w + request.gap
        y += node.h + request.gap
    fixed = {n.id: _util.box_of(n.pin, (n.w, n.h)) for n in nodes if n.pin is not None}
    for n in nodes:
        if n.pin is not None:
            wanted[n.id] = (float(n.pin[0]), float(n.pin[1]))
    free = [n.id for n in nodes if n.pin is None]
    settled, _moved = _util.settle(free, wanted, {n.id: (n.w, n.h) for n in nodes}, fixed, request.gap)
    wanted.update(settled)
    return LayoutResult(positions=wanted, notes=tuple(_util.pin_overlaps(nodes)))


LAYOUTS = (Layout(name="diagonal", run=diagonal, router="straight", doc="boxes on a diagonal (a test layout)"),)
