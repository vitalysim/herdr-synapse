"""Icons as elements (canvas v2 phase 2, 4.2): a Lucide icon with an optional label under it.

The icon is a fixed square (``size`` s, m, l or xl: 24, 32, 48 or 64) drawn as
paths (``canvas_icons``) in its tone's text colour, or ink. A label goes below
it (12/500, muted, at most 2 lines and 160 wide); the element is as wide as the
wider of the two. An unknown name is refused with the nearest names
(``canvas icons --search <word>`` lists them). An icon may be a graph node.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from herdr_team import canvas_display as D
from herdr_team import canvas_icons, canvas_text, canvas_theme
from herdr_team.canvas_kinds import Kind, OpSpec
from herdr_team.canvas_kinds import _text as TX
from herdr_team.canvas_kinds._common import Element, quote, style_of

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 136

SIZES = {"s": 24.0, "m": 32.0, "l": 48.0, "xl": 64.0}
LABEL_SIZE, LABEL_WEIGHT = 12.0, 500
LABEL_MAX = 160.0
LABEL_LINES = 2
GAP = 6.0


def side(el: Element) -> float:
    size = el.get("size")
    return SIZES.get(size, SIZES["m"]) if isinstance(size, str) else SIZES["m"]


def measure(el: Element, minimum: Tuple[float, float]) -> canvas_text.FitResult:
    """The square, and the label under it (clamped to 2 lines at 160); as wide as the wider of the two."""
    square = side(el)
    label = str(el.get("text") or "")
    laid = TX.lay(label, LABEL_MAX, LABEL_SIZE, LABEL_WEIGHT, max_lines=LABEL_LINES, bullets=False) if label else None
    w = max(float(minimum[0]), square, laid.width if laid else 0.0)
    h = max(float(minimum[1]), square + ((GAP + laid.height) if laid and laid.count else 0.0))
    inner = ((w - (laid.width if laid else 0.0)) / 2.0, square + GAP, laid.width if laid else 0.0, laid.height if laid else 0.0)
    return TX.fit_result(w, h, laid.lines() if laid else [], inner, LABEL_SIZE, LABEL_WEIGHT, truncated=bool(laid and laid.truncated), policy="clamp")


def _ink(el: Element) -> str:
    style = style_of(el)
    tone = style.get("tone") if style.get("tone") in canvas_theme.TONES else "neutral"
    return D.INK if tone == "neutral" else "tone.{}.text".format(tone)


def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    x0, y0, x1, y1 = D.box_of(el)
    square = side(el)
    items: List[Dict[str, Any]] = []
    drawn = canvas_icons.emit(str(el.get("icon") or ""), x0 + (x1 - x0 - square) / 2.0, y0, square, _ink(el))
    if drawn is not None:
        items.append(drawn)
    label = str(el.get("text") or "")
    if label:
        laid = TX.lay(label, min(LABEL_MAX, x1 - x0), LABEL_SIZE, LABEL_WEIGHT, max_lines=LABEL_LINES, bullets=False)
        items += TX.emit_lines(laid, x0, y0 + square + GAP, x1 - x0, D.MUTED, lod="label", anchor="middle")
    return items


def hit(el: Element) -> Dict[str, Any]:
    return {"shape": "rect", "box": D.xywh(D.box_of(el))}


def text_edit(el: Element) -> Dict[str, Any]:
    x0, y0, x1, y1 = D.box_of(el)
    square = side(el)
    return TX.edit("text", str(el.get("text") or ""), [x0, y0 + square + GAP, x1 - x0, max(canvas_text.line_height(LABEL_SIZE), y1 - y0 - square - GAP)],
                   LABEL_SIZE, LABEL_WEIGHT, D.MUTED, align="center")


def readback(el: Element, full: bool) -> str:
    """``E-11 icon database "Orders DB"``"""
    label = str(el.get("text") or "")
    return "{} icon {}{}".format(el.get("id"), el.get("icon") or "?", " " + quote(label, 0 if full else 60) if label else "")


def create(ctx: Any, op: Dict[str, Any]) -> None:
    """The ``icon`` op: a Lucide icon by name, in a size and tone, with an optional label under it."""
    from herdr_team.canvas_kinds.card import icon_name

    if op.get("name") is None:
        raise ctx.invalid("name", "icon needs name (canvas icons --search <word>)")
    name = icon_name(ctx, op["name"], "name")
    size = ctx.choice(op, "size", tuple(SIZES), "m")
    label = ctx.text(op, "label", limit="label", one_line=True)
    style = ctx.style({"tone": op.get("tone") or "neutral"}, "text")
    fitted = ctx.fit({"type": "icon", "text": label, "style": style, "size": size, "icon": name}, (1.0, 1.0))
    w, h = fitted.pop("w"), fitted.pop("h")
    x, y, frame = ctx.place(op, w, h)
    ctx.create("icon", x, y, w, h, op=op, text=label, style=style, frame=frame, icon=name, size=size, **fitted)


OPS = (
    OpSpec(name="icon", fields=("name", "size", "tone", "label", "id", "client_id"), create=create, place=True, order=42,
           doc="a Lucide icon by name (canvas icons --search), with an optional label", mcp="icon {name, size s|m|l|xl, tone, label}"),
)

KINDS = (
    Kind(name="icon", role="leaf", ops=("icon", "graph"), fields=("label",), fit="clamp", solid=True, labelled=True, cell=True, tone_group="text",
         connectable=True, edit_limit="label", node=True, measure=measure, readback=readback, emit=emit, hit=hit, text_edit=text_edit,
         doc="a Lucide icon with an optional label under it"),
)
