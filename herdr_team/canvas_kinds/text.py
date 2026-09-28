"""Free ``text`` (canvas v2 phase 0): sized from its lines, no padding.

A text either sizes itself (as wide as its longest line, wrapping at
``TEXT_MAX_W``) or wraps at a width the op set (``wrap: true``; its ``w`` is
that width, widened only for a single word that cannot break). Its height
always follows its lines.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from herdr_team import canvas_display as D
from herdr_team import canvas_text
from herdr_team.canvas_kinds import Kind, Tool
from herdr_team.canvas_kinds._common import MAX_SIZE, Element, number, readback, round_int, style_of

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 20

#: The widest a text sizes itself before it wraps.
TEXT_MAX_W = 600


def measure(el: Element, minimum: Tuple[float, float]) -> canvas_text.FitResult:
    """The text's size: ``minimum[0]`` is its wrap width when it wraps, else the narrowest it may be."""
    style = style_of(el)
    wrap_w = max(1.0, float(minimum[0]))
    request = canvas_text.FitRequest(
        text=str(el.get("text") or ""), font=str(style.get("font") or "normal"), size=number(style.get("size"), 20),
        min_w=wrap_w, min_h=1.0, max_w=wrap_w if el.get("wrap") else max(wrap_w, TEXT_MAX_W))
    return canvas_text.fit("hug", request)


def initial(ctx: Any, op: Dict[str, Any], text: str, style: Dict[str, Any]) -> Tuple[Dict[str, Any], Tuple[float, float]]:
    """A new text's size: its ``w`` is the width it wraps at (none: as wide as its lines); an ``h`` is ignored, its height
    is what its lines need."""
    wrap_w = ctx.size(op, 1, 1)[0] if op.get("w") is not None else None
    fields = ctx.text_fields({"type": "text"}, text, style, wrap_w)
    return fields, (float(fields["fit"]["min"][0]), float(fields["h"]))


def resize(el: Element, w: Any, h: Any, ctx: Any) -> Dict[str, Any]:
    """Resizing a text sets the width it wraps at; its height follows from its lines."""
    if w is None:
        raise ctx.invalid("h", "a text's height follows its lines; give w to set the width it wraps at")
    width = ctx.number({"w": w}, "w", 1, MAX_SIZE)
    return ctx.text_fields(el, str(el.get("text") or ""), style_of(el), float(max(1, round_int(width))))


def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Its lines, hanging from its top-left corner in its tone's text colour."""
    text = D.label(el, D.text_paint(el))
    return [text] if text is not None else []


KINDS = (
    Kind(name="text", role="leaf", ops=("shape",), fields=("text", "w", "tone", "color", "font", "size"), fit="hug", page=True,
         subkind_of="shape", solid=True, cell=True, tone_group="text", handles="width", connectable=True, edit_limit="text", edit_required=True,
         measure=measure, readback=readback, emit=emit, initial=initial, resize=resize,
         doc="free text; as wide as its longest line (up to 600), or wrapped at w",
         tool=Tool(key="t", title="Text", glyph="T", gesture="text", order=50)),
)
