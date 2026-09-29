"""Headings (canvas v2 phase 2, 4.2): a board's or an area's title, in three levels.

Level 1 is the display size (48/700, up to 800 wide) and follows the title
rule: zoomed far out it stays readable, never under 12 screen pixels. Levels 2
(36/700, 640) and 3 (28/600, 560) are body-level text: between the overview and
titles bands they are skeleton bars. A heading hugs its text, with no padding;
``w`` is the width it wraps at.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Tuple

from herdr_team import canvas_display as D
from herdr_team import canvas_text, canvas_theme
from herdr_team.canvas_kinds import Kind, OpSpec
from herdr_team.canvas_kinds import _text as TX
from herdr_team.canvas_kinds._common import Element, quote, style_of

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 134

LEVELS = {1: (48.0, 700, 800.0), 2: (36.0, 700, 640.0), 3: (28.0, 600, 560.0)}
ALIGNS = ("start", "center")


def level_of(el: Element) -> int:
    level = el.get("level")
    return level if isinstance(level, int) and not isinstance(level, bool) and level in LEVELS else 2


def measure(el: Element, minimum: Tuple[float, float]) -> canvas_text.FitResult:
    """The text's lines at its level's size, wrapped at ``w`` (its ``wrap``) or at the level's widest."""
    size, weight, widest = LEVELS[level_of(el)]
    wrap = el.get("wrap_w")
    room = float(wrap) if isinstance(wrap, (int, float)) and not isinstance(wrap, bool) and wrap > 0 else widest
    text = str(el.get("text") or "")
    laid = TX.lay(text, room, size, weight, bullets=False)
    lh = canvas_text.line_height(size)
    w = max(float(minimum[0]), laid.width if not wrap else room, 1.0)
    h = max(float(minimum[1]), laid.height or lh)
    return TX.fit_result(w, h, laid.lines(), (0.0, 0.0, w, h), size, weight)


def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    x0, y0, x1, y1 = D.box_of(el)
    level = level_of(el)
    size, weight, _widest = LEVELS[level]
    style = style_of(el)
    tone = style.get("tone") if style.get("tone") in canvas_theme.TONES else "neutral"
    fill = D.INK if tone == "neutral" else "tone.{}.text".format(tone)
    laid = TX.lay(str(el.get("text") or ""), max(1.0, x1 - x0), size, weight, bullets=False)
    anchor = "middle" if el.get("align") == "center" else "start"
    lines = laid.lines()
    if not lines:
        return []
    lh = canvas_text.line_height(size)
    x = (x0 + x1) / 2.0 if anchor == "middle" else x0
    prim = D.text_prim(lines, x, y0, size, {}, fill, anchor, (x0, y0, x1 - x0, lh * len(lines)), weight)
    if level != 1:
        return D.body(prim)
    switch = D.TITLE_MIN_PX / size
    prim["lod"] = [switch, None]
    held = D.text_prim(lines, x, y0, size, {}, fill, anchor, None, weight)
    held.update(lod=[None, switch], zoom={"min_px": D.TITLE_MIN_PX, "grow": "up", "bottom": y0 + lh * len(lines)},
                base_ratio=canvas_text.baseline(1, "normal", weight))
    return [prim, held]


def text_edit(el: Element) -> Dict[str, Any]:
    x0, y0, x1, y1 = D.box_of(el)
    size, weight, _widest = LEVELS[level_of(el)]
    style = style_of(el)
    tone = style.get("tone") if style.get("tone") in canvas_theme.TONES else "neutral"
    return TX.edit("text", str(el.get("text") or ""), [x0, y0, x1 - x0, y1 - y0], size, weight, D.INK if tone == "neutral" else "tone.{}.text".format(tone),
                   wrap="auto", align="center" if el.get("align") == "center" else "start")


def resize(el: Element, w: Any, h: Any, ctx: Any) -> Dict[str, Any]:
    """Resizing a heading sets the width it wraps at; its height follows its lines."""
    if w is None:
        raise ctx.invalid("h", "a heading's height follows its lines; give w to set the width it wraps at")
    width = int(ctx.number({"w": w}, "w", 40, 4000))
    result = measure(dict(el, wrap_w=width), (1.0, 1.0))
    return {"wrap_w": width, "w": int(math.ceil(result.w - 1e-9)), "h": int(math.ceil(result.h - 1e-9)),
            "fit": canvas_text.fit_record(result, (1, 1))}


def readback(el: Element, full: bool) -> str:
    """``E-3 heading h1 "Q4 launch"``"""
    return "{} heading h{} {} [{},{} {}x{}]".format(el.get("id"), level_of(el), quote(el.get("text"), 0 if full else 80), el.get("x"), el.get("y"),
                                                    el.get("w"), el.get("h"))


def create(ctx: Any, op: Dict[str, Any]) -> None:
    """The ``heading`` op: a title at level 1, 2 or 3 (default 2), wrapped at ``w`` when given."""
    text = ctx.text(op, "text", limit="label", one_line=True, required=True)
    raw = op.get("level", 2)
    if raw not in (1, 2, 3) or isinstance(raw, bool):
        raise ctx.invalid("level", "level is 1, 2 or 3")
    align = ctx.choice(op, "align", ALIGNS, "start")
    style = ctx.style({"tone": op.get("tone") or "neutral"}, "text")
    fields: Dict[str, Any] = {"level": raw}
    if align != "start":
        fields["align"] = align
    if op.get("w") is not None:
        fields["wrap_w"] = int(ctx.number(op, "w", 40, 4000))
    fitted = ctx.fit(dict(fields, type="heading", text=text, style=style), (1.0, 1.0))
    w, h = fitted.pop("w"), fitted.pop("h")
    x, y, frame = ctx.place(op, w, h)
    ctx.create("heading", x, y, w, h, op=op, text=text, style=style, frame=frame, **fields, **fitted)


OPS = (
    OpSpec(name="heading", family="block", fields=("text", "level", "tone", "align", "w", "id", "client_id"), create=create, place=True, order=39,
           doc="a title at level 1 (display), 2 or 3", mcp="heading {text, level 1|2|3, tone, align start|center, w}"),
)

KINDS = (
    Kind(name="heading", role="leaf", ops=("heading",), fields=("text",), fit="hug", solid=True, labelled=True, cell=True, tone_group="text",
         handles="width", edit_limit="label", edit_required=True, measure=measure, readback=readback, emit=emit, text_edit=text_edit, resize=resize,
         doc="a board's or an area's title, at level 1, 2 or 3"),
)
