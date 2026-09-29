"""Comments (canvas v2): a numbered pin on a point or an element, the same size on screen at any zoom.

A comment on an element stores where on it it sits (``anchor``, canvas v2 phase 5, 8): ``[u, v]``, fractions of the
element's box (``[1, 0]`` is the top-right corner, where a new one goes), or ``[u, v, dx, dy]`` with an offset in world
units past that point (a reply sits 16 right and 16 down of what it answers). Whenever an op changes the element's box,
the canvas moves the comment's ``point`` with it (``canvas._follow_comments``), so every reader keeps reading ``point``.
"""
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


def anchor_for(where: Any, box: Any) -> List[float]:
    """The anchor of a point on a box: ``[u, v]`` clamped to 0..1, plus the remainder as ``[dx, dy]`` when it lies outside."""
    x0, y0, x1, y1 = (float(v) for v in box)
    w, h = max(1.0, x1 - x0), max(1.0, y1 - y0)
    px, py = float(where[0]), float(where[1])
    u = min(1.0, max(0.0, (px - x0) / w))
    v = min(1.0, max(0.0, (py - y0) / h))
    dx, dy = r2(px - (x0 + u * w)), r2(py - (y0 + v * h))
    anchor = [_frac(u), _frac(v)]
    return anchor + [dx, dy] if dx or dy else anchor


def _frac(value: float) -> float:
    found = round(float(value), 4)
    return int(found) if found == int(found) else found


def anchored_point(anchor: Any, box: Any) -> List[float]:
    """Where an anchor sits on a box now."""
    x0, y0, x1, y1 = (float(v) for v in box)
    w, h = max(1.0, x1 - x0), max(1.0, y1 - y0)
    u, v = float(anchor[0]), float(anchor[1])
    dx, dy = (float(anchor[2]), float(anchor[3])) if len(anchor) == 4 else (0.0, 0.0)
    return [r2(x0 + u * w + dx), r2(y0 + v * h + dy)]


def valid_anchor(value: Any) -> bool:
    return isinstance(value, list) and len(value) in (2, 4) and all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in value) \
        and 0.0 <= float(value[0]) <= 1.0 and 0.0 <= float(value[1]) <= 1.0


def create(ctx: Any, op: Dict[str, Any]) -> None:
    """The ``comment`` op: a pin at a point, on an element (its top-right corner), or a reply beside another comment."""
    at = op.get("at")
    if at is None:
        raise ctx.invalid("at", "comment needs at: an element, a comment to reply to, or a point")
    text = ctx.text(op, "text", limit="comment", required=True)
    reply_to = ctx.comment(op["reply_to"], "reply_to")["id"] if op.get("reply_to") is not None else None
    anchor = None
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
            host = ctx.el(on) if isinstance(on, str) else None
            if host is not None:
                anchor = anchor_for(where, bounds(host))
        else:
            on = target["id"]
            x0, y0, x1, _y1 = bounds(target)
            where = [x1, y0]
            anchor = [1, 0]
    mentions = ctx.mentions(op.get("mentions"), text)
    fields: Dict[str, Any] = {"anchor": anchor} if anchor is not None else {}
    el = ctx.create("comment", where[0], where[1], 1, 1, op=op, text=text, style=ctx.default_style("comment"), on=on,
                    point=[r2(where[0]), r2(where[1])], mentions=mentions, reply_to=reply_to, resolved=False, resolved_by=None, **fields)
    if mentions:
        ctx.mention(el)


def line(el: Element, reader: Any, full: bool) -> str:
    """``C-4 comment on E-3 by alpha → @beta: "why?" (open)``: who sits in the middle of a comment's line."""
    raw = el.get("point") if isinstance(el.get("point"), list) else [el.get("x") or 0, el.get("y") or 0]
    where = "on {}".format(el["on"]) if el.get("on") else "at {}".format(cell_name(raw[0], raw[1]))
    if not el.get("on") and isinstance(el.get("was_on"), str):
        where += " (was on {}, deleted)".format(el["was_on"])
    if el.get("reply_to"):
        where = "reply to {} {}".format(el["reply_to"], where)
    mentions = " → " + ", ".join("@" + str(m) for m in el.get("mentions") or []) if el.get("mentions") else ""
    status = "(resolved by {})".format(who(el.get("resolved_by"), reader)) if el.get("resolved") else ("(answered)" if el.get("answered") else "(open)")
    return "{} comment {} by {}{}: {} {}".format(el.get("id"), where, who(el.get("author"), reader), mentions,
                                              quote(el.get("text") or "", 0 if full else 60), status)


def translate(el: Element, dx: float, dy: float) -> Dict[str, Any]:
    return {"point": [r2(el["point"][0] + dx), r2(el["point"][1] + dy)]} if isinstance(el.get("point"), list) else {}


def resize(el: Element, w: Any, h: Any, ctx: Any) -> Dict[str, Any]:
    raise ctx.invalid("w", "a comment is a pin; it has no size")


OPS = (
    OpSpec(name="comment", family="primitive", fields=("at", "text", "mentions", "reply_to", "client_id"), create=create, order=120,
           doc="a comment pin on a point or an element; @mentions wake the members it names", mcp="comment {at, text, mentions}",
           proposable=False),
)

KINDS = (
    Kind(name="comment", role="overlay", ops=("comment",), page=True, layer="overlays", handles="none", edit_limit="comment",
         edit_required=True, text_edit=lambda el: None, emit=emit, hit=hit, bounds=G.drawn_bounds, line=line, translate=translate, resize=resize,
         doc="a comment pin on a point or an element"),
)
