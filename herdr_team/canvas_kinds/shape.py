"""Labelled shapes: ``box``, ``note``, ``ellipse`` and ``diamond`` (canvas v2 phase 0).

Each is sized from its label before it is placed: a box hugs its text (it
grows past its minimum, wrapping at ``HUG_MAX_W``), a note keeps its paper
size and shrinks the text first, and an ellipse or a diamond grows as a whole
until its inscribed box holds the label. Minimum sizes and padding come from
the design tokens (``canvas_theme``); a ``w``/``h`` the op gives is the
minimum instead.
"""
from __future__ import annotations

import math
from dataclasses import replace
from typing import Any, Dict, List, Tuple

from herdr_team import canvas_display as D
from herdr_team import canvas_icons, canvas_text, canvas_theme
from herdr_team.canvas_kinds import Kind, OpSpec, Tool, get, subkinds
from herdr_team.canvas_kinds._common import Element, number, policy_of, readback, style_of, truncated_label

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 10

#: The widest a shape grows before its label wraps; a single word wider than this still widens it.
HUG_MAX_W = {"box": 320, "note": 280, "ellipse": 320, "diamond": 320}
#: The same for a node of a ``graph`` or Mermaid flowchart (an element with a ``group``).
GRAPH_NODE_MAX_W = 240
#: Grown sides round up to the placement grid (design spec 4.1).
SNAP = 20
DEFAULT_POLICY = {"box": "hug", "note": "shrink", "ellipse": "scale_shape", "diamond": "scale_shape"}
SQRT_HALF = math.sqrt(0.5)


def ellipse_inset(w: float, h: float) -> Tuple[float, float, float, float]:
    """The rectangle inscribed in the ellipse, ``w/√2`` x ``h/√2``, centred."""
    iw, ih = w * SQRT_HALF, h * SQRT_HALF
    return (w - iw) / 2.0, (h - ih) / 2.0, iw, ih


def diamond_inset(w: float, h: float) -> Tuple[float, float, float, float]:
    """The rectangle inscribed in the diamond, ``w/2`` x ``h/2``, centred."""
    return w / 4.0, h / 4.0, w / 2.0, h / 2.0


INSETS = {"ellipse": ellipse_inset, "diamond": diamond_inset}
#: The outline each shape draws (``canvas_kinds.OUTLINES``); a box and a note are their rectangle.
OUTLINE = {"ellipse": "ellipse", "diamond": "diamond"}


def request(el: Element, minimum: Tuple[float, float], size: Any = None) -> canvas_text.FitRequest:
    """The fit request for a shape element at ``minimum`` (``size`` overrides the style's)."""
    kind = str(el.get("type"))
    style = style_of(el)
    pad_x, pad_y = canvas_theme.padding(kind)
    floor = (canvas_theme.tokens().get("type") or {}).get("shrink_floor", canvas_text.MIN_SIZE)
    fit = el.get("fit") if isinstance(el.get("fit"), dict) else {}
    # clamp keeps ``max_lines``; a clamped element that stored none keeps the lines it shows
    kept = fit.get("lines") if isinstance(fit.get("lines"), list) and fit.get("truncated") else None
    return canvas_text.FitRequest(
        text=str(el.get("text") or ""), font=str(style.get("font") or "normal"), size=number(size or style.get("size"), 20),
        min_w=max(1.0, float(minimum[0])), min_h=max(1.0, float(minimum[1])),
        max_w=GRAPH_NODE_MAX_W if el.get("group") else HUG_MAX_W.get(kind, 320), pad_x=pad_x, pad_y=pad_y,
        inset=INSETS.get(kind), max_lines=int(number(fit.get("max_lines"), len(kept) if kept else 3)), min_size=float(floor), snap=SNAP)


#: A node of a graph or flowchart: a note grows with its label rather than shrink it, so its text reads at
#: the size of its neighbours' (a note shrunk to 14 beside boxes at 20 was hard to read, QA F-12).
GRAPH_POLICY = {"note": "hug"}


#: An icon before the label (phase 2, 4.2): drawn at ``ICON`` units, and the label's room shrinks by ``ICON_ROOM`` on the left.
ICON = 20.0
ICON_ROOM = 28.0


def measure(el: Element, minimum: Tuple[float, float]) -> canvas_text.FitResult:
    """The element's size and lines from its label, never smaller than ``minimum``; an ``icon`` takes ``ICON_ROOM`` on the
    label's left."""
    kind = str(el.get("type"))
    default = GRAPH_POLICY.get(kind) if el.get("group") else None
    wanted = request(el, minimum)
    if not el.get("icon"):
        return canvas_text.fit(policy_of(el, default or DEFAULT_POLICY.get(kind, "hug")), wanted)
    # Half the icon's room on each side widens the box by all of it; then the label's room moves right by the other half.
    wanted = replace(wanted, pad_x=wanted.pad_x + ICON_ROOM / 2.0)
    result = canvas_text.fit(policy_of(el, default or DEFAULT_POLICY.get(kind, "hug")), wanted)
    ix, iy, iw, ih = result.inner
    return replace(result, inner=(ix + ICON_ROOM / 2.0, iy, iw, ih))


def radius(name: str) -> float:
    """A shape's corner radius (design tokens ``radius``: 8 for a box, 4 for a note)."""
    found = (canvas_theme.tokens().get("radius") or {}).get(name)
    return float(found) if isinstance(found, (int, float)) else (4.0 if name == "note" else 8.0)


def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The outline (rectangle, ellipse or diamond) in its tone, then its label centred in its inner box."""
    name = str(el.get("type"))
    x0, y0, x1, y1 = D.box_of(el)
    w, h = x1 - x0, y1 - y0
    paints = D.paints(el)
    body: Dict[str, Any]
    if name == "ellipse":
        body = {"k": "ellipse", "cx": x0 + w / 2, "cy": y0 + h / 2, "rx": w / 2, "ry": h / 2}
    elif name == "diamond":
        cx, cy = x0 + w / 2, y0 + h / 2
        body = {"k": "poly", "points": [[cx, y0], [x1, cy], [cx, y1], [x0, cy]], "closed": True}
    else:
        body = {"k": "rect", "x": x0, "y": y0, "w": w, "h": h, "r": radius(name)}
    body["fill"] = paints["fill"]
    body.update(D.stroke_fields(el, paints["stroke"] or D.INK))
    items = [D.with_opacity(body, el)]
    # A note stored before 0.22 has no label colour: it was drawn in ink on its paper.
    colour = D.INK if name == "note" and paints["text"] is None else D.text_paint(el, paints)
    text = D.label(el, colour)
    if el.get("icon"):
        found = _kinds_drawn(el)
        if found is not None:
            ix, iy, _iw, ih = found.inner
            icon = canvas_icons.emit(str(el["icon"]), x0 + ix - ICON_ROOM, y0 + iy + (ih - ICON) / 2.0, ICON, colour)
            if icon is not None:
                items.append(icon)
    if text is not None:
        # A shape's label is a title-level text (phase 2, 6.1): it stays down to the overview band (QA phase 2, F10).
        text["lod"] = list(D.LOD_LABEL)
        items.append(text)
    return items


def _kinds_drawn(el: Element) -> Any:
    from herdr_team.canvas_kinds import drawn

    return drawn(el)


def hit(el: Element) -> Dict[str, Any]:
    return {"shape": OUTLINE.get(str(el.get("type")), "rect"), "box": D.xywh(D.box_of(el))}


#: The shapes an ``icon`` may sit on, before the label.
ICON_KINDS = ("box", "ellipse", "diamond", "note")
#: Each shape's button in the page's tool bar.
TOOLS = {
    "box": Tool(key="r", title="Rectangle", glyph="\u25ad", gesture="shape", order=10),
    "ellipse": Tool(key="o", title="Ellipse", glyph="\u25ef", gesture="shape", order=20),
    "diamond": Tool(key="d", title="Diamond", glyph="\u25c7", gesture="shape", order=30),
}


def _kind(name: str, doc: str) -> Kind:
    return Kind(name=name, role="leaf", ops=("shape", "graph", "mermaid"),
                fields=("text", "w", "h", "tone", "variant", "color", "fill", "font", "size", "icon"), node=True,
                fit=DEFAULT_POLICY[name], page=True, outline=OUTLINE.get(name, "rect"), hosts=True, subkind_of="shape", solid=True,
                labelled=True, cell=True, tone_group="note" if name == "note" else "shape", connectable=True, edit_limit="text",
                inset=INSETS.get(name), measure=measure, readback=readback, checks=(truncated_label,), emit=emit, hit=hit, doc=doc,
                noun=("box", "boxes") if name == "box" else ("", ""), tool=TOOLS.get(name))


def create(ctx: Any, op: Dict[str, Any]) -> None:
    """The ``shape`` op: a kind it picks with ``kind`` (a box by default), sized from its label (0.22: ``w``/``h`` are
    the minimum), then placed; the sized shape makes room for its neighbours (``OpContext.create``)."""
    name = ctx.choice(op, "kind", [kind.name for kind in subkinds("shape")], "box")
    kind = get(name)
    icon = None
    if op.get("icon") is not None:
        if name not in ICON_KINDS:
            raise ctx.invalid("icon", "icon goes on a box, ellipse, diamond or note")
        from herdr_team.canvas_kinds.card import icon_name

        icon = icon_name(ctx, op["icon"], "icon")
    # A kind whose whole content is its text (a free text) needs one; a shape may be blank.
    text = ctx.text(op, "text", limit="text", required=kind is not None and kind.measure is not None and not kind.labelled)
    style = ctx.style(op, name)
    if kind is not None and kind.initial is not None:
        fields, asked = kind.initial(ctx, op, text, style)
    else:
        probe = {"type": name, "text": text, "style": style}
        if icon:
            probe["icon"] = icon
        fields = ctx.fit(probe, ctx.size(op, *ctx.shape_size(name)))
        asked = (float(fields["fit"]["min"][0]), float(fields["fit"]["min"][1]))
    w, h = fields.pop("w"), fields.pop("h")
    if icon:
        fields["icon"] = icon
    x, y, frame = ctx.place(op, w, h, (float(asked[0]), float(asked[1])))
    ctx.create(name, x, y, w, h, op=op, text=text, style=style, frame=frame, **fields)


OPS = (
    OpSpec(name="shape", fields=("kind", "text", "w", "h", "icon", "id", "client_id"), create=create, style=True, place=True, order=10,
           doc="a box, ellipse, diamond, note or free text, sized from its label (w/h are its minimum)",
           mcp="shape {kind box|ellipse|diamond|note|text, text, at|right_of|below|inside, w, h (minimums: shapes grow to fit their "
               "label), tone neutral|info|success|warning|danger|accent|idea|decision, variant soft|solid|outline, color, fill; a text wraps at w}"),
)

KINDS = (
    _kind("box", "a rectangle that grows to fit its label (w/h are its minimum)"),
    _kind("ellipse", "an ellipse; grows as a whole until its inscribed box holds the label"),
    _kind("diamond", "a decision diamond; grows as a whole until its inscribed box holds the label"),
    _kind("note", "a sticky note; keeps its size and shrinks the text (to 14) before it grows"),
)


def defaults() -> Dict[str, Any]:
    """The minimum size of each shape kind, for docs and ``canvas.SHAPE_SIZES``."""
    return {kind.name: canvas_theme.size_min(kind.name) for kind in KINDS}
