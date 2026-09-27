"""Free ``text`` (canvas v2 phase 0): sized from its lines, no padding.

A text either sizes itself (as wide as its longest line, wrapping at
``TEXT_MAX_W``) or wraps at a width the op set (``wrap: true``; its ``w`` is
that width, widened only for a single word that cannot break). Its height
always follows its lines.
"""
from __future__ import annotations

from typing import Tuple

from herdr_team import canvas_text
from herdr_team.canvas_kinds import Kind
from herdr_team.canvas_kinds._common import Element, number, readback, style_of

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


KINDS = (
    Kind(name="text", role="leaf", ops=("shape",), fields=("text", "w", "tone", "color", "font", "size"), fit="hug",
         measure=measure, readback=readback, doc="free text; as wide as its longest line (up to 600), or wrapped at w"),
)
