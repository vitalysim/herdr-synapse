"""Frames (canvas v2): a titled area that owns its children, drawn under everything else.

The title is the only zoom rule of phase 1 (``.local/prd/canvas-v2-phase1.md``
1.6): close up it sits in the frame's 40-unit band at 16, cut to the band's
width; zoomed out (below ``canvas_geometry.FRAME_TITLE_LOD`` screen pixels per
unit) the whole title stands above the frame, never under 12 screen pixels.
The display list carries both alternatives with their ``lod``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from herdr_team import canvas_display as D
from herdr_team import canvas_geometry as G
from herdr_team import canvas_text
from herdr_team.canvas_kinds import Kind, OpSpec, Tool
from herdr_team.canvas_kinds._common import Element, bounds, contains, style_of, who

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 40

#: Room a frame keeps around its children, and its title band on top (``canvas.FRAME_PAD``, ``FRAME_TOP``).
FRAME_PAD, FRAME_TOP = 20, G.FRAME_BAND
#: A frame's outline in screen pixels (a hairline at any zoom).
OUTLINE_PX = 1.5
#: The band a click selects a frame by; inside it a click reaches the children.
BAND = G.FRAME_BAND


def _title_fill(el: Element) -> str:
    return D.paints(el)["text"] or D.MUTED


def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The tinted zone, then its title twice: in the band close up, above the frame zoomed out."""
    style = style_of(el)
    paints = D.paints(el)
    x0, y0, x1, y1 = D.box_of(el)
    items: List[Dict[str, Any]] = [{"k": "rect", "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "r": 16 if style.get("tone") else 6,
                                    "fill": paints["fill"], "stroke": paints["stroke"] or D.MUTED, "sw_px": OUTLINE_PX}]
    title = str(el.get("text") or "")[:G.FRAME_TITLE_MAX_CHARS]
    if not title:
        return items
    size, weight = float(G.FRAME_TITLE_SIZE), G.FRAME_TITLE_WEIGHT
    fill = paints["text"] or D.MUTED
    lh = canvas_text.line_height(size)
    room = max(0.0, (x1 - x0) - 2 * G.FRAME_TITLE_AT[0])
    band = D.text_prim([G.fit_line(title, room, size, weight)], x0 + G.FRAME_TITLE_AT[0], y0 + G.FRAME_TITLE_AT[1], size, {}, fill, "start",
                       (x0 + G.FRAME_TITLE_AT[0], y0 + G.FRAME_TITLE_AT[1], room, lh), weight)
    band["lod"] = [G.FRAME_TITLE_LOD, None]
    above = D.text_prim([title], x0, y0 - lh, size, {}, fill, "start", None, weight)
    above.update(lod=[None, G.FRAME_TITLE_LOD], zoom={"min_px": G.FRAME_TITLE_MIN_PX, "grow": "up", "bottom": y0},
                 base_ratio=canvas_text.baseline(1, "normal", weight))
    return items + [band, above]


def hit(el: Element) -> Dict[str, Any]:
    return {"shape": "frame", "box": D.xywh(D.box_of(el)), "band": BAND}


def text_edit(el: Element) -> Optional[Dict[str, Any]]:
    """The title, on one line in the band."""
    x0, y0, x1, _y1 = D.box_of(el)
    size = float(G.FRAME_TITLE_SIZE)
    return {"field": "text", "value": str(el.get("text") or ""), "font": "sans", "weight": G.FRAME_TITLE_WEIGHT, "size": size,
            "box": [x0 + G.FRAME_TITLE_AT[0], y0 + G.FRAME_TITLE_AT[1], max(0.0, (x1 - x0) - 2 * G.FRAME_TITLE_AT[0]), BAND - 2 * G.FRAME_TITLE_AT[1]],
            "lh": canvas_text.line_height(size), "align": "start", "wrap": "line", "fill": _title_fill(el)}


PLACE_KEYS = ("at", "right_of", "left_of", "below", "above", "inside")


def create(ctx: Any, op: Dict[str, Any]) -> None:
    """The ``frame`` op: a titled area around children, over a region (taking what lies inside), or placed with a size."""
    title = ctx.text(op, "title", limit="label", one_line=True)
    style = ctx.style(op, "frame")
    children_raw, region_raw = op.get("children"), op.get("region")
    if children_raw is not None and region_raw is not None:
        raise ctx.invalid("region", "a frame takes children or a region, not both")
    placed = [key for key in PLACE_KEYS + ("w", "h") if op.get(key) is not None]
    children: List[Dict[str, Any]] = []
    if children_raw is not None:
        if placed:
            raise ctx.invalid(placed[0], "a frame around children takes its bounds from them")
        if not isinstance(children_raw, list) or not children_raw:
            raise ctx.invalid("children", "children must be a non-empty list of elements")
        for ref in children_raw:
            child = ctx.lookup(ref, "children")
            if child.get("type") == "comment":
                raise ctx.invalid("children", "comments stay pinned where they are; frame the element they point at")
            if all(c["id"] != child["id"] for c in children):
                children.append(child)
        for child in children:
            if not ctx.may_edit(child):
                raise ctx.error("element_not_yours", "{} is {}'s; a frame can only take elements you may edit".format(child["id"], who(child.get("author"), None)),
                                id=child["id"], author=child.get("author"))
        boxes = [bounds(c) for c in children]
        x0, y0 = min(b[0] for b in boxes) - FRAME_PAD, min(b[1] for b in boxes) - FRAME_TOP
        x1, y1 = max(b[2] for b in boxes) + FRAME_PAD, max(b[3] for b in boxes) + FRAME_PAD
        parent = ctx.enclosing_frame((x0, y0, x1, y1))
    elif region_raw is not None:
        if placed:
            raise ctx.invalid(placed[0], "a frame over a region takes its bounds from the region")
        x0, y0, x1, y1 = ctx.region(region_raw, "region")
        parent = ctx.enclosing_frame((x0, y0, x1, y1))
        inside = [el for el in ctx.live() if el.get("type") != "comment" and el["id"] != parent and contains((x0, y0, x1, y1), bounds(el))]
        inside_ids = {el["id"] for el in inside}
        # Only the top level moves in: what sits in a frame that is itself inside keeps that frame.
        children = [el for el in inside if el.get("frame") not in inside_ids and ctx.may_edit(el)]
    else:
        w, h = ctx.size(op, 400, 300)
        x0, y0, parent = ctx.place(op, w, h)
        x1, y1 = x0 + w, y0 + h
    frame = ctx.create("frame", x0, y0, x1 - x0, y1 - y0, op=op, text=title, style=style, frame=parent)
    for child in children:
        ctx.update(ctx.el(child["id"]) or child, frame=frame["id"])


OPS = (
    OpSpec(name="frame", family="primitive", fields=("title", "w", "h", "children", "region", "id", "client_id"), create=create, style=True, place=True, order=30,
           doc="a titled area around children, over a region, or placed with a size", mcp="frame {title, at+w+h | children | region}"),
)

KINDS = (
    Kind(name="frame", role="container", ops=("frame", "graph", "mermaid", "portrait"), page=True, cell=True, tone_group="frame", layer="zones",
         emit=emit, hit=hit, text_edit=text_edit, doc="a titled area that owns its children",
         tool=Tool(key="f", title="Frame", glyph="\u2b1a", gesture="frame", order=80)),
)
