"""Stickies (canvas v2 phase 2, 4.2): square paper with a thought on it, the ``N`` tool on the page.

A sticky keeps its paper size (``size`` s, m or l: 160, 200 or 240 square) and
shrinks its text from 20 to 14 before it grows, and then only taller. Its
paper is its tone's ``sticky`` colour (an idea by default), with no outline and
a resting shadow; the text is centred in the tone's text colour. Phase 0's
``note`` shape stays as it was; a sticky is the v2 look.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from herdr_team import canvas_display as D
from herdr_team import canvas_text, canvas_theme
from herdr_team.canvas_kinds import Kind, OpSpec, Tool
from herdr_team.canvas_kinds._common import Element, policy_of, readback, style_of, truncated_label

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 132

PAPER = {"s": 160, "m": 200, "l": 240}
PAD = 16.0
RADIUS = 4


def paper(el: Element) -> float:
    size = el.get("size")
    return float(PAPER.get(size, PAPER["m"])) if isinstance(size, str) else float(PAPER["m"])


def measure(el: Element, minimum: Tuple[float, float]) -> canvas_text.FitResult:
    """Shrink the text to 14 inside the paper, then grow taller (a word wider than the paper still widens it)."""
    side = paper(el)
    style = style_of(el)
    floor = (canvas_theme.tokens().get("type") or {}).get("shrink_floor", canvas_text.MIN_SIZE)
    min_w, min_h = max(float(minimum[0]), side), max(float(minimum[1]), side)
    # The size to start from: 20, or the size it is drawn at (``canvas_kinds.drawn`` passes ``fit.size`` in ``style``),
    # so the drawing and ``check`` wrap exactly as the fit did (QA phase 2, F3).
    start = style.get("size") if isinstance(style.get("size"), (int, float)) and style["size"] > 0 else 20.0
    request = canvas_text.FitRequest(text=str(el.get("text") or ""), font=str(style.get("font") or "normal"), size=float(start),
                                     min_w=min_w, min_h=min_h, max_w=min_w, pad_x=PAD, pad_y=PAD, min_size=float(floor), snap=20)
    return canvas_text.fit(policy_of(el, "shrink"), request)


def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    x0, y0, x1, y1 = D.box_of(el)
    style = style_of(el)
    tone = style.get("tone") if style.get("tone") in canvas_theme.TONES else "idea"
    items: List[Dict[str, Any]] = [{"k": "rect", "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "r": RADIUS, "fill": "tone.{}.sticky".format(tone),
                                    "stroke": None, "elev": 1}]
    text = D.label(el, "tone.{}.text".format(tone))
    if text is not None:
        text["lod"] = list(D.LOD_LABEL)
        items.append(text)
    return items


def hit(el: Element) -> Dict[str, Any]:
    return {"shape": "rect", "box": D.xywh(D.box_of(el))}


def create(ctx: Any, op: Dict[str, Any]) -> None:
    """The ``sticky`` op: paper in a tone (an idea by default) with a thought on it; a blank one from the page."""
    text = ctx.text(op, "text", limit="text", required=not ctx.author_is_human)
    size = ctx.choice(op, "size", tuple(PAPER), "m")
    style = ctx.style({"tone": op.get("tone") or canvas_theme.default_tone("sticky")[0], "variant": "soft"}, "sticky")
    side = float(PAPER[size])
    minimum = (ctx.number(op, "w", 1, 20000, side), ctx.number(op, "h", 1, 20000, side))
    fitted = ctx.fit({"type": "sticky", "text": text, "style": style, "size": size}, minimum)
    w, h = fitted.pop("w"), fitted.pop("h")
    x, y, frame = ctx.place(op, w, h, (float(minimum[0]), float(minimum[1])))
    ctx.create("sticky", x, y, w, h, op=op, text=text, style=style, frame=frame, size=size, **fitted)


OPS = (
    OpSpec(name="sticky", family="block", fields=("text", "tone", "size", "w", "h", "id", "client_id"), create=create, place=True, order=37,
           doc="a sticky: square paper with a thought on it (tone idea by default)", mcp="sticky {text, tone, size s|m|l}"),
)

KINDS = (
    Kind(name="sticky", role="leaf", ops=("sticky", "graph"), fields=("text",), fit="shrink", hosts=True, solid=True, labelled=True, cell=True,
         tone_group="note", connectable=True, edit_limit="text", node=True, stretch=False, measure=measure, readback=readback, checks=(truncated_label,),
         emit=emit, hit=hit, noun=("sticky", "stickies"), doc="square paper with a thought on it; shrinks its text to 14 before it grows",
         tool=Tool(key="n", title="Sticky note", glyph="\U0001f5d2", gesture="block", order=40, template='{"op": "sticky", "text": ""}')),
)
