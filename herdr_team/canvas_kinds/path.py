"""Paths (canvas v2): SVG path data drawn as one shape, scaled from its natural size."""
from __future__ import annotations

from typing import Any, Dict, List

from herdr_team import canvas_display as D
from herdr_team import canvas_geometry as G
from herdr_team.canvas_kinds import Kind, OpSpec
from herdr_team.canvas_kinds._common import MAX_SIZE, Element, bounds_text, r2, round_int, style_of
from herdr_team.errors import HerdrTeamError

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 60

#: The longest path data one op takes.
MAX_PATH_CHARS = 20000


def path_data(d: Any) -> str:
    """Stored path data in the display list's grammar: absolute ``M L C Q Z``, arcs flattened (``op_invalid`` when it is not path data)."""
    return " ".join(cmd + " ".join(D.fmt(v) for v in args) for cmd, args in G.path_mlcqz(str(d or "")))


def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The path in a group that places and scales it; its stroke keeps the style's width at any scale."""
    try:
        d = path_data(el.get("d"))
    except HerdrTeamError:
        return []
    style = style_of(el)
    paints = D.paints(el)
    scale = D.num(el.get("scale"), 1.0) or 1.0
    width = (D.num(style.get("width"), 2.0) or 2.0) / scale
    body = D.with_opacity(dict({"k": "path", "d": d, "fill": paints["fill"]}, **D.stroke_fields(el, paints["stroke"] or D.INK, width)), el)
    return [{"k": "group", "t": [scale, 0, 0, scale, D.num(el.get("x"), 0.0), D.num(el.get("y"), 0.0)], "items": [body]}]


def create(ctx: Any, op: Dict[str, Any]) -> None:
    """The ``path`` op: SVG path data (commands and numbers only), moved to start at its corner and scaled."""
    d = op.get("d")
    if not isinstance(d, str) or not d.strip():
        raise ctx.invalid("d", "path needs d: SVG path data")
    if len(d) > MAX_PATH_CHARS:
        raise ctx.too_big("d", "MAX_PATH_CHARS", MAX_PATH_CHARS, "path data is {} characters; the limit is {}".format(len(d), MAX_PATH_CHARS))
    normalized, natural_w, natural_h = G.normalize_path(d)
    scale = ctx.number(op, "scale", 0.01, 100, 1.0)
    w, h = natural_w * scale, natural_h * scale
    if w > MAX_SIZE or h > MAX_SIZE:
        raise ctx.invalid("scale", "the path would be {:g} x {:g}; the limit is {} per side".format(w, h, MAX_SIZE))
    style = ctx.style(op, "path")
    x, y, frame = ctx.place(op, w, h)
    ctx.create("path", x, y, w, h, op=op, style=style, frame=frame, d=normalized, scale=r2(scale))


def readback(el: Element, full: bool) -> str:
    return "{} path {}".format(el.get("id"), bounds_text(el))


def resize(el: Element, w: Any, h: Any, ctx: Any) -> Dict[str, Any]:
    """It scales to the new width, keeping its proportions."""
    old_w, old_h = max(1.0, float(el.get("w") or 1)), max(1.0, float(el.get("h") or 1))
    new_w = ctx.number({"w": w}, "w", 1, MAX_SIZE) if w is not None else old_w
    if h is not None:
        ctx.number({"h": h}, "h", 1, MAX_SIZE)
    scale = D.num(el.get("scale"), 1.0) or 1.0
    natural_w, natural_h = old_w / scale, old_h / scale
    scale = new_w / natural_w
    return {"w": max(1, round_int(new_w)), "h": max(1, round_int(natural_h * scale)), "scale": round(scale, 4)}


OPS = (
    OpSpec(name="path", family="primitive", fields=("d", "scale", "id", "client_id"), create=create, style=True, place=True, order=50,
           doc="SVG path data drawn as one shape", mcp="path {d}"),
)

KINDS = (
    Kind(name="path", role="leaf", ops=("path",), page=True, solid=True, tone_group="ink", edit_field=None, emit=emit,
         readback=readback, resize=resize, doc="SVG path data drawn as one shape"),
)
