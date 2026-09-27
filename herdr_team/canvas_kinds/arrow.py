"""Arrows (canvas v2): a line or connector between two elements or points, with an optional label in a pill.

The route is straight through its points or, with ``curve``, a smooth curve
through them (``canvas_geometry.curve_pieces``). Heads are computed here, so a
renderer never does geometry: a chevron (``arrow``), a ``triangle`` or a
``dot`` at either end. The label sits in a pill at the spot the canvas chose
for it (``label_at``, QA R-3), drawn in the ``labels`` layer after every mark.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from herdr_team import canvas_display as D
from herdr_team import canvas_geometry as G
from herdr_team import canvas_text
from herdr_team import canvas_theme
from herdr_team.canvas_kinds import Kind, OpSpec
from herdr_team.canvas_kinds._common import HEADS, MAX_SIZE, Element, end_name, quote, r2, round_int, scaled_points, shifted_points, style_of

#: The most points an arrow takes (its bends).
MAX_ARROW_POINTS = 50
#: A route's click tolerance in screen pixels.
HIT_TOLERANCE_PX = 6
#: The editor of an unlabelled arrow's label: this wide, centred on the midpoint.
EMPTY_EDIT_W = 120


def path_data(el: Element) -> Optional[str]:
    """The shaft as absolute path data: ``M`` and ``L`` through the points, or ``M`` and ``Q`` pieces for a curve."""
    points = D.points_of(el.get("points"))
    if len(points) < 2:
        return None
    parts = ["M{} {}".format(D.fmt(points[0][0]), D.fmt(points[0][1]))]
    if el.get("curve"):
        for _start, (cx, cy), (ex, ey) in G.curve_pieces([(p[0], p[1]) for p in points]):
            parts.append("Q{} {} {} {}".format(D.fmt(cx), D.fmt(cy), D.fmt(ex), D.fmt(ey)))
    else:
        parts += ["L{} {}".format(D.fmt(x), D.fmt(y)) for x, y in points[1:]]
    return " ".join(parts)


def heads(el: Element, width: float) -> List[Dict[str, Any]]:
    points = D.points_of(el.get("points"))
    out: List[Dict[str, Any]] = []
    for at, kind, tip, back in (("end", el.get("head") or "arrow", points[-1], points[-2]), ("start", el.get("tail") or "none", points[0], points[1])):
        found = G.head_points(str(kind), (tip[0], tip[1]), (back[0], back[1]), width)
        if found is not None:
            out.append(dict(found, at=at))
    return out


def _pill_radius() -> float:
    found = (canvas_theme.tokens().get("radius") or {}).get("arrow_label")
    return float(found) if isinstance(found, (int, float)) else 6.0


def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The shaft with its heads in the stroke's paint, then the label pill and its lines in the ``labels`` layer."""
    d = path_data(el)
    if d is None:
        return []
    style = style_of(el)
    paints = D.paints(el)
    stroke = paints["stroke"] or D.INK
    fields = D.stroke_fields(el, stroke)
    items = [D.with_opacity(dict({"k": "arrow", "d": d}, **fields, heads=heads(el, fields["sw"])), el)]
    pill = G.arrow_label_pill(el)
    if pill is not None:
        (px, py, pw, ph), size, lines = pill
        pad_x, pad_y = G.ARROW_LABEL_PAD
        items.append({"k": "rect", "layer": "labels", "x": px, "y": py, "w": pw, "h": ph, "r": _pill_radius(), "fill": "base.surface",
                      "stroke": "base.grid", "sw": 1})
        top = py + (ph - canvas_text.line_height(size) * len(lines)) / 2.0
        text = D.text_prim(lines, px + pw / 2.0, top, size, style, D.text_paint(el, paints), "middle",
                           (px + pad_x, py + pad_y, pw - 2 * pad_x, ph - 2 * pad_y))
        text["layer"] = "labels"
        items.append(text)
    return items


def hit(el: Element) -> Dict[str, Any]:
    out: Dict[str, Any] = {"shape": "line", "points": [[x, y] for x, y in G.arrow_route(el)], "tol_px": HIT_TOLERANCE_PX}
    pill = G.arrow_label_pill(el)
    if pill is not None:
        out["pill"] = list(pill[0])
    return out


def text_edit(el: Element) -> Optional[Dict[str, Any]]:
    """The label, in its pill's inner box, or (unlabelled) in a box on the arrow's midpoint."""
    style = style_of(el)
    size = D.num(style.get("size"), 20.0) * G.ARROW_LABEL_SCALE
    lh = canvas_text.line_height(size)
    pill = G.arrow_label_pill(el)
    if pill is not None:
        (px, py, pw, ph), size, _lines = pill
        lh = canvas_text.line_height(size)
        box = [px + G.ARROW_LABEL_PAD[0], py + G.ARROW_LABEL_PAD[1], pw - 2 * G.ARROW_LABEL_PAD[0], ph - 2 * G.ARROW_LABEL_PAD[1]]
    else:
        route = G.arrow_route(el)
        mx, my = G.arrow_midpoint(route) if route else (D.num(el.get("x"), 0.0), D.num(el.get("y"), 0.0))
        box = [mx - EMPTY_EDIT_W / 2.0, my - lh / 2.0, EMPTY_EDIT_W, lh]
    return {"field": "text", "value": str(el.get("text") or ""), "box": box, "font": D.font_of(style), "weight": canvas_text.DEFAULT_WEIGHT,
            "size": size, "lh": lh, "align": "center", "wrap": "box", "fill": D.text_paint(el)}


def create(ctx: Any, op: Dict[str, Any]) -> None:
    """The ``arrow`` op: from and to (an element or a point) or its points, a one-line label, heads and an optional curve."""
    style = ctx.style(op, "arrow")
    label = ctx.text(op, "label", limit="label", one_line=True)
    head = ctx.choice(op, "head", HEADS, "arrow")
    tail = ctx.choice(op, "tail", HEADS, "none")
    curve = ctx.boolean(op, "curve", False)
    points, start_id, end_id = ctx.route(op, label=label, style=style, curve=curve)
    geo = ctx.geometry(points)
    frame = ctx.enclosing_frame((geo["x"], geo["y"], geo["x"] + geo["w"], geo["y"] + geo["h"]))
    ctx.create("arrow", geo["x"], geo["y"], geo["w"], geo["h"], op=op, text=label, style=style, frame=frame,
               **{"from": start_id, "to": end_id, "points": points, "head": head, "tail": tail, "curve": curve})


def readback(el: Element, full: bool) -> str:
    text = str(el.get("text") or "")
    return "{} arrow {} → {}{}".format(el.get("id"), end_name(el, "from", 0), end_name(el, "to", -1), " " + quote(text, 0 if full else 80) if text else "")


def translate(el: Element, dx: float, dy: float) -> Dict[str, Any]:
    """Its points move with it, and its label's spot."""
    fields: Dict[str, Any] = {}
    if isinstance(el.get("points"), list):
        fields["points"] = shifted_points(el, dx, dy)
    if isinstance(el.get("label_at"), list) and len(el["label_at"]) == 2:
        fields["label_at"] = [r2(el["label_at"][0] + dx), r2(el["label_at"][1] + dy)]
    return fields


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
    OpSpec(name="arrow", fields=("from", "to", "points", "label", "head", "tail", "curve", "id", "client_id"), create=create, style=True,
           order=20, doc="a line or connector between two elements or points, with an optional label",
           mcp="arrow {from, to (element or point), label}"),
)

KINDS = (
    Kind(name="arrow", role="connector", ops=("arrow", "graph", "mermaid"), page=True, tone_group="arrow", handles="ends",
         emit=emit, hit=hit, text_edit=text_edit, bounds=G.drawn_bounds, readback=readback, translate=translate, resize=resize,
         doc="a line or connector between two elements or points"),
)
