"""``row`` and ``column``: boxes in sequence, ``gap`` apart (sections and kanban columns, phase 2 4.3).

A stack is an order, not a drawing: nodes follow their ``order`` values, and
``align`` (``start``, ``center`` or ``end``) lines them up across the stack.
``stretch`` is the caller's (it refits members to one size and runs again).
Pins are not held (``Layout.pins`` False): a person reorders a stack by
dropping, not by pinning.
"""
from __future__ import annotations

from typing import Dict

from herdr_team.canvas_layouts import Layout, LayoutError, LayoutRequest, LayoutResult, Point
from herdr_team.canvas_layouts._util import nodes_in_order

#: Its place in the registration order.
ORDER = 50
ALIGNS = ("start", "center", "end")


def _stack(request: LayoutRequest, horizontal: bool) -> LayoutResult:
    align = request.options.get("align", "start")
    if align not in ALIGNS:
        raise LayoutError("options.align", "align is one of {}".format(", ".join(ALIGNS)))
    nodes = nodes_in_order(request)
    thick = max((n.h if horizontal else n.w) for n in nodes)
    out: Dict[str, Point] = {}
    cursor = 0.0
    for node in nodes:
        along, across = (node.w, node.h) if horizontal else (node.h, node.w)
        offset = 0.0 if align == "start" else (thick - across) / (2.0 if align == "center" else 1.0)
        out[node.id] = (cursor, offset) if horizontal else (offset, cursor)
        cursor += along + request.gap
    return LayoutResult(positions=out)


def row(request: LayoutRequest) -> LayoutResult:
    return _stack(request, True)


def column(request: LayoutRequest) -> LayoutResult:
    return _stack(request, False)


LAYOUTS = (
    Layout(name="row", run=row, edges=False, directions=("down",), options=("align",), pins=False, router="straight",
           doc="boxes left to right in order, gap apart, aligned start, center or end"),
    Layout(name="column", run=column, edges=False, directions=("down",), options=("align",), pins=False, router="straight",
           doc="boxes top to bottom in order, gap apart, aligned start, center or end"),
)
