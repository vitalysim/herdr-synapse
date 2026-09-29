"""Pen strokes (canvas v2): a freehand stroke, smooth (a filled outline, a port of perfect-freehand) or straight."""
from __future__ import annotations

from typing import Any, Dict, List

from herdr_team import canvas_display as D
from herdr_team import canvas_geometry as G
from herdr_team.canvas_kinds import Kind, OpSpec, Tool
from herdr_team.canvas_kinds._common import MAX_SIZE, Element, bounds_text, color_name, round_int, scaled_points, shifted_points, style_of

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 50

HIT_TOLERANCE_PX = 6
#: The most points one stroke takes.
MAX_PEN_POINTS = 500


def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A closed stroke's fill first, then the stroke: its smooth outline filled in the stroke's paint, or its straight line."""
    style = style_of(el)
    paints = D.paints(el)
    stroke = paints["stroke"] or D.INK
    width = D.num(style.get("width"), 2.0) or 2.0
    raw = el.get("points") if isinstance(el.get("points"), list) else []
    flat = D.points_of(raw)
    items: List[Dict[str, Any]] = []
    if el.get("closed") and paints["fill"] and len(flat) >= 3:
        items.append(D.with_opacity({"k": "poly", "points": flat, "closed": True, "fill": paints["fill"], "stroke": None}, el))
    if el.get("smooth", True):
        outline = G.freehand_outline(list(raw) + ([raw[0]] if el.get("closed") and raw else []), width, smooth=True)
        if outline:
            items.append(D.with_opacity({"k": "poly", "points": [[x, y] for x, y in outline], "closed": True, "fill": stroke, "stroke": None}, el))
        return items
    if len(flat) >= 2:
        line: Dict[str, Any] = {"k": "poly", "points": flat, "closed": True, "fill": None} if el.get("closed") else {"k": "line", "points": flat}
        line.update(D.stroke_fields(el, stroke))
        items.append(D.with_opacity(line, el))
    return items


def hit(el: Element) -> Dict[str, Any]:
    return {"shape": "line", "points": D.points_of(el.get("points")), "tol_px": HIT_TOLERANCE_PX}


def create(ctx: Any, op: Dict[str, Any]) -> None:
    """The ``pen`` op: a stroke through points (cells, "x,y" or [x, y, pressure]), smooth or straight, closed and filled or open."""
    points = ctx.points(op, "points", MAX_PEN_POINTS, pressure=True, limit_name="MAX_PEN_POINTS")
    closed = ctx.boolean(op, "closed", False)
    mode = ctx.choice(op, "style", ("smooth", "straight"), "smooth")
    style = ctx.style(op, "pen")
    if style.get("fill") and not closed:
        raise ctx.invalid("fill", "a pen stroke is filled only when closed: true")
    geo = ctx.geometry(points)
    frame = ctx.enclosing_frame((geo["x"], geo["y"], geo["x"] + geo["w"], geo["y"] + geo["h"]))
    ctx.create("pen", geo["x"], geo["y"], geo["w"], geo["h"], op=op, style=style, frame=frame, points=points, closed=closed,
               smooth=mode == "smooth")


def readback(el: Element, full: bool) -> str:
    points = el.get("points") if isinstance(el.get("points"), list) else []
    return "{} pen {} pts{} {} {}".format(el.get("id"), len(points), " closed" if el.get("closed") else "", color_name(style_of(el).get("stroke")),
                                          bounds_text(el))


def translate(el: Element, dx: float, dy: float) -> Dict[str, Any]:
    return {"points": shifted_points(el, dx, dy)} if isinstance(el.get("points"), list) else {}


def resize(el: Element, w: Any, h: Any, ctx: Any) -> Dict[str, Any]:
    """Its points scale from its box's top-left corner."""
    old_w, old_h = max(1.0, float(el.get("w") or 1)), max(1.0, float(el.get("h") or 1))
    new_w = ctx.number({"w": w}, "w", 1, MAX_SIZE) if w is not None else old_w
    new_h = ctx.number({"h": h}, "h", 1, MAX_SIZE) if h is not None else old_h
    fields: Dict[str, Any] = {"w": max(1, round_int(new_w)), "h": max(1, round_int(new_h))}
    if isinstance(el.get("points"), list):
        fields["points"] = scaled_points(el, new_w, new_h)
    return fields


OPS = (
    # ``in`` (phase 2): a stroke drawn in a free section, its points in the section's local grid.
    OpSpec(name="pen", family="primitive", fields=("points", "closed", "style", "width", "color", "fill", "opacity", "dash", "id", "client_id", "in"), create=create,
           order=40, doc="a freehand stroke through points, smooth or straight",
           mcp='pen {points [cells, "x,y" or [x,y]], closed, style smooth|straight, color, in (a free section: its local grid)}'),
)

KINDS = (
    Kind(name="pen", role="leaf", ops=("pen",), page=True, noun=("pen stroke", "pen strokes"), tone_group="ink", edit_field=None,
         emit=emit, hit=hit, readback=readback, translate=translate, resize=resize, doc="a freehand stroke",
         tool=Tool(key="p", title="Pen", glyph="\u270e", gesture="pen", order=70, one_shot=False)),
)
