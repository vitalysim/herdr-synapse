"""Arrows (canvas v2): a line or connector between two elements or points, with an optional label in a pill.

The route is straight through its points or, with ``curve``, a smooth curve
through them (``canvas_geometry.curve_pieces``). Heads are computed here, so a
renderer never does geometry: a chevron (``arrow``), a ``triangle`` or a
``dot`` at either end. The label sits in a pill at the spot the canvas chose
for it (``label_at``, QA R-3), drawn in the ``labels`` layer after every mark.

Since phase 2 an arrow's ``style.route`` picks its router (``canvas_routers``):
``straight`` (today's route, byte for byte), ``curved``, or ``orthogonal``,
which goes around what is in its way and is drawn with rounded elbows
(``canvas_geometry.rounded_path``). ``reroute`` is the hook the canvas calls
after an arrow's ends moved or its route style changed.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from herdr_team import canvas_display as D
from herdr_team import canvas_geometry as G
from herdr_team import canvas_routers as R
from herdr_team import canvas_text
from herdr_team import canvas_theme
from herdr_team.canvas_kinds import Kind, OpSpec, Tool, outline
from herdr_team.canvas_kinds._common import HEADS, MAX_SIZE, Element, bounds, end_name, quote, r2, round_int, scaled_points, shifted_points, style_of

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 30

#: The most points an arrow takes (its bends).
MAX_ARROW_POINTS = 50
#: A route's click tolerance in screen pixels.
HIT_TOLERANCE_PX = 6
#: The editor of an unlabelled arrow's label: this wide, centred on the midpoint.
EMPTY_EDIT_W = 120


#: The route styles (``style.route``), each a router in ``canvas_routers``.
ROUTES = ("straight", "orthogonal", "curved")


def layout_token(name: str, default: float) -> float:
    """A number from the design tokens' ``layout`` group (clearance, elbow radius, port spacing)."""
    found = (canvas_theme.tokens().get("layout") or {}).get(name)
    return float(found) if isinstance(found, (int, float)) and not isinstance(found, bool) else default


def route_of(el: Element) -> str:
    """The router an arrow draws with: its ``style.route``, else ``curved`` for a curve, else ``straight``."""
    found = style_of(el).get("route")
    if isinstance(found, str) and R.get(found) is not None:
        return R.get(found).name  # type: ignore[union-attr]
    return "curved" if el.get("curve") else "straight"


def path_data(el: Element) -> Optional[str]:
    """The shaft as absolute path data: ``M`` and ``L`` through the points, ``M`` and ``Q`` pieces for a curve, or
    ``L`` pieces with a ``Q`` at each elbow for an orthogonal route."""
    points = D.points_of(el.get("points"))
    if len(points) < 2:
        return None
    if route_of(el) == "orthogonal":
        return " ".join(cmd + " ".join(D.fmt(v) for v in args)
                        for cmd, args in G.rounded_path(points, layout_token("elbow_radius", 8.0)))
    parts = ["M{} {}".format(D.fmt(points[0][0]), D.fmt(points[0][1]))]
    if el.get("curve") or route_of(el) == "curved":
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
        # An arrow label is body-level text (phase 2, 6.1): its pill and lines draw from the titles band up, and a
        # skeleton bar stands in for them between the overview and titles bands (QA phase 2, F10).
        items.append({"k": "rect", "layer": "labels", "x": px, "y": py, "w": pw, "h": ph, "r": _pill_radius(), "fill": "base.surface",
                      "stroke": "base.grid", "sw": 1, "lod": list(D.LOD_BODY)})
        top = py + (ph - canvas_text.line_height(size) * len(lines)) / 2.0
        text = D.text_prim(lines, px + pw / 2.0, top, size, style, D.text_paint(el, paints), "middle",
                           (px + pad_x, py + pad_y, pw - 2 * pad_x, ph - 2 * pad_y))
        text["layer"] = "labels"
        for prim in D.body(text):
            prim["layer"] = "labels"
            items.append(prim)
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
    """The ``arrow`` op: from and to (an element or a point) or its points, a one-line label, heads and an optional curve.
    With ``route: orthogonal`` it goes around what is between its ends, and its label slides along the route instead
    of pushing an end away."""
    style = ctx.style(op, "arrow")
    label = ctx.text(op, "label", limit="label", one_line=True)
    head = ctx.choice(op, "head", HEADS, "arrow")
    tail = ctx.choice(op, "tail", HEADS, "none")
    curve = ctx.boolean(op, "curve", False) or style.get("route") in ("curved", "curve")
    orthogonal = style.get("route") in ("orthogonal", "elbow") and not curve
    points, start_id, end_id = ctx.route(op, label="" if orthogonal else label, style=style, curve=curve)
    extra: Dict[str, Any] = {}
    if orthogonal and hasattr(ctx, "obstacles"):
        probe = {"type": "arrow", "points": points, "text": label, "style": style, "curve": False, "from": start_id, "to": end_id}
        start_el = ctx.el(start_id) if start_id else None
        end_el = ctx.el(end_id) if end_id else None
        ends = [bounds(e) for e in (start_el, end_el) if e is not None]
        xs = [p[0] for p in points] + [b[0] for b in ends] + [b[2] for b in ends]
        ys = [p[1] for p in points] + [b[1] for b in ends] + [b[3] for b in ends]
        reach = (min(xs) - REACH, min(ys) - REACH, max(xs) + REACH, max(ys) + REACH)
        env = {"obstacles": ctx.obstacles(reach, [i for i in (start_id, end_id) if i]), "others": []}
        fields, blocked = route_fields(dict(probe, points=[points[0], points[-1]]), start_el, end_el, env)
        if blocked:
            ctx.warn("route_blocked", "no clear orthogonal route between the ends; the arrow is straight", [])
        points = fields.get("points", points)
        if "label_at" in fields:
            extra["label_at"] = fields["label_at"]
        if fields.get("bends"):
            extra["bends"] = fields["bends"]
    geo = ctx.geometry(points)
    frame = ctx.enclosing_frame((geo["x"], geo["y"], geo["x"] + geo["w"], geo["y"] + geo["h"]))
    ctx.create("arrow", geo["x"], geo["y"], geo["w"], geo["h"], op=op, text=label, style=style, frame=frame,
               **{"from": start_id, "to": end_id, "points": points, "head": head, "tail": tail, "curve": curve}, **extra)


#: How far around its ends an arrow looks for obstacles (phase 2, 3.4).
REACH = 200


def _end(el: Optional[Element], point: Any, side: Optional[str] = None) -> R.End:
    if el is None:
        x, y = float(point[0]), float(point[1])
        return R.End(box=(x, y, x, y))
    return R.End(box=bounds(el), outline=outline(el), id=str(el.get("id")), side=side)


def label_size(el: Element) -> Optional[Any]:
    """The label pill's ``(w, h)`` as drawn (None without a label)."""
    found = G.arrow_label_text(el) if str(el.get("text") or "") else None
    if found is None:
        return None
    return found[0]


def reroute(el: Element, start_el: Optional[Element], end_el: Optional[Element], env: Dict[str, Any]) -> Dict[str, Any]:
    """An arrow's fields after its ends moved or its route style changed: ``points`` and its box, and for an
    orthogonal route with a label its ``label_at`` (``route_fields`` also says when no clear orthogonal route was found).

    ``env``: ``obstacles`` ``[(id, (x0, y0, x1, y1), outline)]`` never holding an end or its container, ``others``
    (routes already placed), and for a graph's edges ``via`` (a layout's hints) and ``sides`` (its ports)."""
    fields, _blocked = route_fields(el, start_el, end_el, env)
    return fields


def route_fields(el: Element, start_el: Optional[Element], end_el: Optional[Element], env: Dict[str, Any]) -> Any:
    """``reroute``'s fields, and whether the orthogonal router found no clear route (the caller warns ``route_blocked``).

    An orthogonal route that still leaves one end and reaches the other, and meets none of ``env``'s obstacles, is kept
    as it is: a graph's routes (spread ports, bends through the layout's hints) survive a move of something else."""
    points = D.points_of(el.get("points"))
    if len(points) < 2:
        return {}, False
    name = route_of(el)
    if name == "orthogonal" and not env.get("via") and _still_good(points, start_el, end_el, env.get("obstacles") or ()):
        return {}, False
    sides = env.get("sides") or (None, None)
    start = _end(start_el, points[0], sides[0])
    end = _end(end_el, points[-1], sides[1])
    if name == "orthogonal" or el.get("bends") == "router":
        # The router's own bends are not waypoints: a route style change starts again from the ends.
        via = tuple((float(p[0]), float(p[1])) for p in env.get("via") or ())
    else:
        via = tuple((float(p[0]), float(p[1])) for p in (env.get("via") if env.get("via") is not None else points[1:-1]))
    request = R.RouteRequest(id=str(el.get("id") or "arrow"), a=start, b=end, via=via,
                             label=label_size(el) if name == "orthogonal" else None,
                             obstacles=tuple((str(i), tuple(float(v) for v in box), str(o)) for i, box, o in env.get("obstacles") or ()),
                             others=tuple(tuple((float(x), float(y)) for x, y in route) for route in env.get("others") or ()),
                             clearance=layout_token("clearance", 20.0), radius=layout_token("elbow_radius", 8.0),
                             slot=tuple(env.get("slot") or (0, 1)), slot_b=tuple(env.get("slot_b") or (0, 1)),  # type: ignore[arg-type]
                             port_spacing=layout_token("port_spacing", 12.0))
    found = R.route(name, request)
    new_points = [[r2(x), r2(y)] for x, y in found.points]
    xs = [p[0] for p in new_points]
    ys = [p[1] for p in new_points]
    fields: Dict[str, Any] = {"points": new_points, "x": round_int(min(xs)), "y": round_int(min(ys)),
                              "w": max(1, round_int(max(xs) - min(xs))), "h": max(1, round_int(max(ys) - min(ys)))}
    if found.label_at is not None:
        fields["label_at"] = [r2(found.label_at[0]), r2(found.label_at[1])]
    if name == "orthogonal" and len(new_points) > 2:
        fields["bends"] = "router"
    elif el.get("bends") is not None:
        fields["bends"] = None
    return fields, found.blocked


# --------------------------------------------------------------------------
# checks (QA phase 2, F14): what the layout check did not see


#: A label whose pill's centre lies further than this from its route, beyond half its own height, reads as someone
#: else's (the placement's fourth ring beside the line).
LABEL_ASTRAY = 36.0


def _segments_cross(p1: Any, p2: Any, p3: Any, p4: Any) -> bool:
    def orient(a: Any, b: Any, c: Any) -> int:
        value = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(value) < 1e-9 else (1 if value > 0 else -1)

    return orient(p1, p2, p3) * orient(p1, p2, p4) < 0 and orient(p3, p4, p1) * orient(p3, p4, p2) < 0


def _crosses_itself(pieces: List[Any]) -> bool:
    """Whether two pieces of a route that are not neighbours cross. Only pieces whose boxes meet are tested, so a long
    route (a big graph's edge through many hints) costs little more than its length (QA phase 2, R2)."""
    boxes = [(min(a[0], b[0]), min(a[1], b[1]), max(a[0], b[0]), max(a[1], b[1])) for a, b in pieces]
    for i, a in enumerate(pieces):
        ax0, ay0, ax1, ay1 = boxes[i]
        for j in range(i + 2, len(pieces)):
            bx0, by0, bx1, by1 = boxes[j]
            if bx0 <= ax1 and ax0 <= bx1 and by0 <= ay1 and ay0 <= by1 and _segments_cross(a[0], a[1], pieces[j][0], pieces[j][1]):
                return True
    return False


def _enters(a: Any, b: Any, box: Any, inset: float = 2.0) -> bool:
    """Whether the piece ``a``-``b`` runs through the inside of ``box`` (shrunk by ``inset``)."""
    x0, y0, x1, y1 = box[0] + inset, box[1] + inset, box[2] - inset, box[3] - inset
    if x0 >= x1 or y0 >= y1:
        return False
    lo, hi = 0.0, 1.0
    dx, dy = b[0] - a[0], b[1] - a[1]
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if p == 0:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            lo = max(lo, t)
        else:
            hi = min(hi, t)
        if lo > hi:
            return False
    return hi - lo > 1e-9


def _solid_core(el: Element) -> Any:
    """The largest axis-aligned box inside an element's outline: its box for a rectangle, the inscribed one for an
    ellipse or a diamond (a route may pass the corners of their boxes)."""
    x0, y0, x1, y1 = bounds(el)
    factor = {"ellipse": 0.7071, "diamond": 0.5}.get(outline(el), 1.0)
    cx, cy, hw, hh = (x0 + x1) / 2.0, (y0 + y1) / 2.0, (x1 - x0) / 2.0 * factor, (y1 - y0) / 2.0 * factor
    return cx - hw, cy - hh, cx + hw, cy + hh


def route_checks(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``route_loop``: a route that crosses itself or runs back through one of its own ends; ``label_astray``: a label
    placed so far beside its route that it no longer reads as the arrow's."""
    points = D.points_of(el.get("points"))
    if len(points) < 2:
        return []
    out: List[Dict[str, Any]] = []
    name = el.get("alias") or el.get("id")
    by_id = env.get("by_id") or {}
    pieces = list(zip(points, points[1:]))
    loop = _crosses_itself(pieces)
    if not loop and not el.get("curve"):
        for end, inner in ((by_id.get(el.get("from")), pieces[1:]), (by_id.get(el.get("to")), pieces[:-1])):
            if end is not None and any(_enters(a, b, _solid_core(end)) for a, b in inner):
                loop = True
    if loop:
        out.append({"code": "route_loop", "ids": [str(el.get("id"))],
                    "message": "{} loops: its route crosses itself or runs back through one of its ends; move the ends apart, "
                               "or restyle its route".format(name),
                    "fix": {"op": "restyle", "id": el.get("alias") or el.get("id"), "route": "straight"}})
    size = label_size(el)
    at = el.get("label_at")
    if size is not None and isinstance(at, (list, tuple)) and len(at) == 2:
        route = G.arrow_route(el)
        if len(route) >= 2:
            px, py = float(at[0]), float(at[1])
            best = min(_point_piece(px, py, a, b) for a, b in zip(route, route[1:]))
            if best > float(size[1]) / 2.0 + LABEL_ASTRAY:
                out.append({"code": "label_astray", "ids": [str(el.get("id"))],
                            "message": "{}'s label sits {} units from its line, where it reads as something else's; give its "
                                       "ends room (a wider gap) or a shorter label".format(name, int(best)), "fix": None})
    return out


def _point_piece(px: float, py: float, a: Any, b: Any) -> float:
    ax, ay, bx, by = float(a[0]), float(a[1]), float(b[0]), float(b[1])
    dx, dy = bx - ax, by - ay
    length = dx * dx + dy * dy
    t = 0.0 if length == 0 else max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / length))
    return ((px - ax - t * dx) ** 2 + (py - ay - t * dy) ** 2) ** 0.5


#: How far from its end's outline an orthogonal route's first or last point may sit and still count as touching it.
TOUCH = R.END_GAP + 1.0


def _touches(point: Any, el: Optional[Element]) -> bool:
    if el is None:
        return True
    x0, y0, x1, y1 = bounds(el)
    px, py = float(point[0]), float(point[1])
    shape = outline(el)
    if shape != "rect":
        # On a curved or slanted outline: its own distance from the centre is about 1 there.
        from herdr_team.canvas_kinds import _norm

        grown = _norm(shape, (x0 - TOUCH, y0 - TOUCH, x1 + TOUCH, y1 + TOUCH), px, py)
        return grown <= 1.0 + 1e-6 and _norm(shape, (x0, y0, x1, y1), px, py) >= 0.9
    inside_x, inside_y = x0 - TOUCH <= px <= x1 + TOUCH, y0 - TOUCH <= py <= y1 + TOUCH
    return inside_x and inside_y and not (x0 + 1 < px < x1 - 1 and y0 + 1 < py < y1 - 1)


def _still_good(points: List[List[float]], start_el: Optional[Element], end_el: Optional[Element], obstacles: Any) -> bool:
    """Whether a stored route is orthogonal, touches both ends, and runs through no obstacle."""
    if start_el is None and end_el is None:
        return False
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        if abs(ax - bx) > 1e-6 and abs(ay - by) > 1e-6:
            return False
    if not (_touches(points[0], start_el) and _touches(points[-1], end_el)):
        return False
    for _eid, box, _outline in obstacles:
        x0, y0, x1, y1 = (float(v) for v in box)
        for (ax, ay), (bx, by) in zip(points, points[1:]):
            if min(ax, bx) < x1 and x0 < max(ax, bx) and min(ay, by) < y1 and y0 < max(ay, by):
                return False
            if (ax == bx and x0 < ax < x1 and min(ay, by) < y1 and y0 < max(ay, by)) or \
                    (ay == by and y0 < ay < y1 and min(ax, bx) < x1 and x0 < max(ax, bx)):
                return False
    return True


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
         reroute=reroute, checks=(route_checks,), doc="a line or connector between two elements or points", tool=Tool(key="a", title="Arrow", glyph="\u2192", gesture="connect", order=60)),
)
