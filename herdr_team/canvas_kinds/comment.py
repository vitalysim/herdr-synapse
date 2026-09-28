"""Comments (canvas v2): a numbered pin on a point or an element, the same size on screen at any zoom."""
from __future__ import annotations

from typing import Any, Dict, List

from herdr_team import canvas_display as D
from herdr_team import canvas_geometry as G
from herdr_team import canvas_text
from herdr_team.canvas_kinds import Kind, OpSpec
from herdr_team.canvas_kinds._common import Element, bounds, cell_name, quote, r2, who

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 120

#: The pin's radius and number size in screen pixels.
PIN_PX = 10
NUMBER_PX = 11


def point(el: Element) -> List[float]:
    raw = el.get("point") if isinstance(el.get("point"), list) and len(el["point"]) >= 2 else [el.get("x"), el.get("y")]
    return [D.num(raw[0], 0.0), D.num(raw[1], 0.0)]


def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A pin in the author's chip colour (grey once resolved) with its number, anchored in screen pixels."""
    x, y = point(el)
    chip = D.chip(el.get("author"), env)
    pin: Dict[str, Any] = {"k": "ellipse", "cx": 0, "cy": 0, "rx": PIN_PX, "ry": PIN_PX, "fill": chip["bg"], "stroke": "#ffffff", "sw": 1.5}
    if el.get("resolved"):
        pin.update(fill="tone.neutral.solid", op=0.45)
    number = str(el.get("id") or "C-?").split("-", 1)[-1]
    text = D.text_prim([number], 0, 4 - canvas_text.baseline(NUMBER_PX, "normal", 400), NUMBER_PX, {}, chip["fg"], "middle", None, 400)
    return [{"k": "group", "screen": [x, y], "items": [pin, text]}]


def hit(el: Element) -> Dict[str, Any]:
    x, y = point(el)
    return {"shape": "pin", "x": x, "y": y, "r_px": PIN_PX}


def create(ctx: Any, op: Dict[str, Any]) -> None:
    """The ``comment`` op: a pin at a point, on an element (its top-right corner), or a reply beside another comment."""
    at = op.get("at")
    if at is None:
        raise ctx.invalid("at", "comment needs at: an element, a comment to reply to, or a point")
    text = ctx.text(op, "text", limit="comment", required=True)
    reply_to = ctx.comment(op["reply_to"], "reply_to")["id"] if op.get("reply_to") is not None else None
    if ctx.is_point(at):
        on, where = None, ctx.point(at, "at")[:2]
    else:
        target = ctx.lookup(at, "at")
        if target.get("type") == "comment":
            if reply_to is not None and reply_to != target["id"]:
                raise ctx.invalid("reply_to", "at names comment {} but reply_to names {}".format(target["id"], reply_to))
            reply_to = target["id"]
            on = target.get("on")
            base = target.get("point") if isinstance(target.get("point"), list) else [target.get("x"), target.get("y")]
            where = [float(base[0]) + 16, float(base[1]) + 16]
        else:
            on = target["id"]
            x0, y0, x1, _y1 = bounds(target)
            where = [x1, y0]
    mentions = ctx.mentions(op.get("mentions"), text)
    el = ctx.create("comment", where[0], where[1], 1, 1, op=op, text=text, style=ctx.default_style("comment"), on=on,
                    point=[r2(where[0]), r2(where[1])], mentions=mentions, reply_to=reply_to, resolved=False, resolved_by=None)
    if mentions:
        ctx.mention(el)


def line(el: Element, reader: Any, full: bool) -> str:
    """``C-4 comment on E-3 by alpha → @beta: "why?" (open)``: who sits in the middle of a comment's line."""
    raw = el.get("point") if isinstance(el.get("point"), list) else [el.get("x") or 0, el.get("y") or 0]
    where = "on {}".format(el["on"]) if el.get("on") else "at {}".format(cell_name(raw[0], raw[1]))
    if el.get("reply_to"):
        where = "reply to {} {}".format(el["reply_to"], where)
    mentions = " → " + ", ".join("@" + str(m) for m in el.get("mentions") or []) if el.get("mentions") else ""
    status = "(resolved by {})".format(who(el.get("resolved_by"), reader)) if el.get("resolved") else "(open)"
    return "{} comment {} by {}{}: {} {}".format(el.get("id"), where, who(el.get("author"), reader), mentions,
                                              quote(el.get("text") or "", 0 if full else 60), status)


def translate(el: Element, dx: float, dy: float) -> Dict[str, Any]:
    return {"point": [r2(el["point"][0] + dx), r2(el["point"][1] + dy)]} if isinstance(el.get("point"), list) else {}


def resize(el: Element, w: Any, h: Any, ctx: Any) -> Dict[str, Any]:
    raise ctx.invalid("w", "a comment is a pin; it has no size")


OPS = (
    OpSpec(name="comment", fields=("at", "text", "mentions", "reply_to", "client_id"), create=create, order=120,
           doc="a comment pin on a point or an element; @mentions wake the members it names", mcp="comment {at, text, mentions}"),
)

KINDS = (
    Kind(name="comment", role="overlay", ops=("comment",), page=True, layer="overlays", handles="none", edit_limit="comment",
         edit_required=True, text_edit=lambda el: None, emit=emit, hit=hit, bounds=G.drawn_bounds, line=line, translate=translate, resize=resize,
         doc="a comment pin on a point or an element"),
)
