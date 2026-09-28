"""SVG blocks (canvas v2): an agent's SVG, sanitised when it was drawn, stored as an asset and drawn as an image."""
from __future__ import annotations

import re
from typing import Any, Dict, List

from herdr_team import canvas_display as D
from herdr_team import canvas_render
from herdr_team.canvas_kinds import Kind, OpSpec
from herdr_team.canvas_kinds._common import MAX_SIZE, Element

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 70

_ASSET = re.compile(r"^[0-9a-f]{32}\.(png|jpg|svg)\Z")


def asset_emit(subtitle: str) -> Any:
    """``emit`` of a kind drawn from a stored asset: the image, with the placeholder card as its fallback."""
    def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
        fallback = D.card(el, subtitle)
        name = el.get("asset")
        if not isinstance(name, str) or not _ASSET.match(name):
            return fallback
        x0, y0, x1, y1 = D.box_of(el)
        return [{"k": "image", "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "src": {"asset": name}, "fallback": fallback}]
    return emit


def create(ctx: Any, op: Dict[str, Any]) -> None:
    """The ``svg`` op: an agent's SVG, rebuilt from an allow-list (anything unsafe refuses the whole block), stored as an asset."""
    markup = op.get("svg")
    if not isinstance(markup, str) or not markup.strip():
        raise ctx.invalid("svg", "svg needs svg: the markup")
    clean = canvas_render.sanitize_svg(markup)
    ctx.guard_code(markup, "svg")
    natural = canvas_render.svg_size(clean) or canvas_render.DEFAULT_SVG_SIZE
    w, h = ctx.size(op, min(natural[0], MAX_SIZE), min(natural[1], MAX_SIZE))
    title = ctx.text(op, "title", limit="label", one_line=True)
    sketchy = ctx.boolean(op, "sketchy", False)
    x, y, frame = ctx.place(op, w, h)
    ctx.create("svg", x, y, w, h, op=op, text=title, style=ctx.default_style("svg"), frame=frame, store={"asset": (clean.encode("utf-8"), "svg")},
               sketchy=sketchy)


OPS = (
    OpSpec(name="svg", fields=("svg", "w", "h", "sketchy", "title", "id", "client_id"), create=create, place=True, order=60,
           doc="a sanitised SVG block, stored and drawn as an image", mcp="svg {svg}"),
)

KINDS = (
    Kind(name="svg", noun=("svg block", "svg blocks"), role="leaf", ops=("svg",), page=True, solid=True, cell=True, connectable=True, emit=asset_emit("svg block (not stored)"),
         text_edit=lambda el: None, doc="a sanitised SVG block"),
)
