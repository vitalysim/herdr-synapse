"""``grid``: boxes row-major in their order, in columns as wide as their widest box and rows as tall as their tallest.

``cols`` sets the column count (default the rounded-up square root of the
node count) and ``align`` places a box in its cell (``start`` or ``center``,
the default). A grid is an order: pins are not held (``Layout.pins`` False).
"""
from __future__ import annotations

import math
from typing import Dict, List

from herdr_team.canvas_layouts import Layout, LayoutError, LayoutRequest, LayoutResult, Point
from herdr_team.canvas_layouts._util import nodes_in_order

#: Its place in the registration order.
ORDER = 40
ALIGNS = ("start", "center")
MAX_COLS = 200


def grid(request: LayoutRequest) -> LayoutResult:
    nodes = nodes_in_order(request)
    cols = request.options.get("cols")
    if cols is None:
        cols = max(1, int(math.ceil(math.sqrt(len(nodes)))))
    if not isinstance(cols, int) or isinstance(cols, bool) or not 1 <= cols <= MAX_COLS:
        raise LayoutError("options.cols", "cols is a whole number from 1 to {}".format(MAX_COLS))
    align = request.options.get("align", "center")
    if align not in ALIGNS:
        raise LayoutError("options.align", "align is one of {}".format(", ".join(ALIGNS)))
    rows = int(math.ceil(len(nodes) / float(cols)))
    widths: List[float] = [0.0] * cols
    heights: List[float] = [0.0] * rows
    for index, node in enumerate(nodes):
        widths[index % cols] = max(widths[index % cols], node.w)
        heights[index // cols] = max(heights[index // cols], node.h)
    xs = [math.fsum(widths[:c]) + c * request.gap for c in range(cols)]
    ys = [math.fsum(heights[:r]) + r * request.gap for r in range(rows)]
    out: Dict[str, Point] = {}
    for index, node in enumerate(nodes):
        c, r = index % cols, index // cols
        dx, dy = ((widths[c] - node.w) / 2.0, (heights[r] - node.h) / 2.0) if align == "center" else (0.0, 0.0)
        out[node.id] = (xs[c] + dx, ys[r] + dy)
    return LayoutResult(positions=out)


LAYOUTS = (
    Layout(name="grid", run=grid, edges=False, directions=("down", "right", "up", "left"), options=("cols", "align"), pins=False,
           router="straight", doc="boxes row-major in order; columns as wide as their widest box, rows as tall as their tallest"),
)
