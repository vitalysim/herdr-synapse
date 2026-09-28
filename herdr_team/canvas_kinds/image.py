"""Images (canvas v2): a stored PNG, JPEG or SVG, contained in its box."""
from __future__ import annotations

from typing import Any, Dict

from herdr_team.canvas_kinds import Kind, OpSpec
from herdr_team.canvas_kinds._common import MAX_SIZE, Element, bounds_text
from herdr_team.canvas_kinds.svg import asset_emit

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 110

#: A new image is at most this wide (its pixel size, shrunk) unless the op sets w/h.
DEFAULT_MAX_W = 480.0


def create(ctx: Any, op: Dict[str, Any]) -> None:
    """The ``image`` op: a PNG or JPEG (checked by its bytes) from a path the author may read, or the page's upload."""
    data, mime, px_w, px_h = ctx.image(op)
    shrink = min(1.0, DEFAULT_MAX_W / px_w)
    w, h = ctx.size(op, max(1.0, min(MAX_SIZE, px_w * shrink)), max(1.0, min(MAX_SIZE, px_h * shrink)))
    x, y, frame = ctx.place(op, w, h)
    ctx.create("image", x, y, w, h, op=op, style=ctx.default_style("image"), frame=frame, store={"asset": (data, "image")}, mime=mime,
               px_w=px_w, px_h=px_h)


def readback(el: Element, full: bool) -> str:
    return "{} image {}x{}px {}".format(el.get("id"), el.get("px_w"), el.get("px_h"), bounds_text(el))


OPS = (
    OpSpec(name="image", fields=("path", "asset", "w", "h", "id", "client_id"), create=create, place=True, order=110,
           doc="a PNG or JPEG from a file under artifacts/, whiteboard/renders/ or your working directory", mcp="image {path}"),
)

KINDS = (
    Kind(name="image", role="leaf", ops=("image",), page=True, solid=True, cell=True, connectable=True,
         emit=asset_emit("image (not stored)"), readback=readback, doc="a stored PNG, JPEG or SVG image"),
)
