"""Badges (canvas v2 phase 2, 4.2): a small pill holding a status word, in a tone.

One line, cut with an ellipsis at 240 (``clamp``); 20 high at size ``s`` (12/500)
or 24 at ``m`` (14/500), with an optional 14-unit icon before the word. Soft
(the default) is the tone's fill with its text colour, solid its strong colour
with ``on_solid`` text, outline a hairline in its stroke.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from herdr_team import canvas_display as D
from herdr_team import canvas_icons, canvas_text, canvas_theme
from herdr_team.canvas_kinds import Kind, OpSpec
from herdr_team.canvas_kinds import _text as TX
from herdr_team.canvas_kinds._common import Element, quote, style_of, truncated_label

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 135

SIZES = {"s": (20.0, 12.0), "m": (24.0, 14.0)}
WEIGHT = 500
PAD_X = 8.0
MAX_W = 240.0
ICON = 14.0
ICON_GAP = 4.0
VARIANTS = ("soft", "solid", "outline")


def _size(el: Element) -> Tuple[float, float]:
    size = el.get("size")
    return SIZES.get(size, SIZES["m"]) if isinstance(size, str) else SIZES["m"]


def measure(el: Element, minimum: Tuple[float, float]) -> canvas_text.FitResult:
    """One line: as wide as the word (and icon) up to 240, cut with an ellipsis past that."""
    height, size = _size(el)
    text = str(el.get("text") or "")
    icon = (ICON + ICON_GAP) if el.get("icon") else 0.0
    natural = TX.width(text, size, WEIGHT) + 2 * PAD_X + icon
    w = min(max(float(minimum[0]), natural, height), max(MAX_W, float(minimum[0])))
    room = w - 2 * PAD_X - icon
    truncated = TX.width(text, size, WEIGHT) > room + 1e-6
    line = TX.ellipsize(text, room, size, WEIGHT) if truncated else text
    h = max(float(minimum[1]), height)
    return TX.fit_result(w, h, [line] if line else [], (PAD_X + icon, (h - canvas_text.line_height(size)) / 2.0, max(0.0, room),
                                                        canvas_text.line_height(size)), size, WEIGHT, truncated=truncated, policy="clamp")


def _paints(el: Element) -> Dict[str, Any]:
    style = style_of(el)
    tone = style.get("tone") if style.get("tone") in canvas_theme.TONES else "neutral"
    variant = style.get("variant") if style.get("variant") in VARIANTS else "soft"
    return canvas_theme.resolve_ref(tone, variant, "badge")


def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    x0, y0, x1, y1 = D.box_of(el)
    h = y1 - y0
    paints = _paints(el)
    pill: Dict[str, Any] = {"k": "rect", "x": x0, "y": y0, "w": x1 - x0, "h": h, "r": h / 2.0, "fill": paints["fill"]}
    if paints.get("stroke") and paints["stroke"] != paints["fill"]:
        pill.update(stroke=paints["stroke"], sw=1)
    items: List[Dict[str, Any]] = [pill]
    result = measure(el, (x1 - x0, h))
    ix, iy, iw, ih = result.inner
    ink = paints["text"] or D.INK
    if el.get("icon"):
        icon = canvas_icons.emit(str(el["icon"]), x0 + PAD_X, y0 + (h - ICON) / 2.0, ICON, ink, D.LOD_LABEL)
        if icon is not None:
            items.append(icon)
    if result.lines:
        text = D.text_prim(list(result.lines), x0 + ix, y0 + iy, result.size, {}, ink, "start", (x0 + ix, y0 + iy, iw, ih), WEIGHT)
        text["lod"] = list(D.LOD_LABEL)
        items.append(text)
    return items


def text_edit(el: Element) -> Dict[str, Any]:
    x0, y0, x1, y1 = D.box_of(el)
    result = measure(el, (x1 - x0, y1 - y0))
    ix, iy, iw, ih = result.inner
    return TX.edit("text", str(el.get("text") or ""), [x0 + ix, y0 + iy, iw, ih], result.size, WEIGHT, _paints(el)["text"] or D.INK, wrap="line")


def hit(el: Element) -> Dict[str, Any]:
    return {"shape": "rect", "box": D.xywh(D.box_of(el))}


def readback(el: Element, full: bool) -> str:
    """``E-5 badge "P0" (danger)``"""
    style = style_of(el)
    tone = style.get("tone") if style.get("tone") in canvas_theme.TONES else "neutral"
    return "{} badge {} ({}){}".format(el.get("id"), quote(el.get("text"), 0 if full else 60), tone,
                                       " icon {}".format(el["icon"]) if el.get("icon") else "")


def create(ctx: Any, op: Dict[str, Any]) -> None:
    """The ``badge`` op: a status word in a tone (soft, solid or outline), with an optional icon."""
    text = ctx.text(op, "text", limit="label", one_line=True, required=True)
    size = ctx.choice(op, "size", tuple(SIZES), "m")
    variant = ctx.choice(op, "variant", VARIANTS, "soft")
    style = ctx.style({"tone": op.get("tone") or "neutral", "variant": variant}, "badge")
    fields: Dict[str, Any] = {"size": size}
    if op.get("icon") is not None:
        from herdr_team.canvas_kinds.card import icon_name

        found = icon_name(ctx, op["icon"], "icon")
        if found:
            fields["icon"] = found
    fitted = ctx.fit(dict(fields, type="badge", text=text, style=style), (1.0, 1.0))
    w, h = fitted.pop("w"), fitted.pop("h")
    x, y, frame = ctx.place(op, w, h)
    ctx.create("badge", x, y, w, h, op=op, text=text, style=style, frame=frame, **fields, **fitted)


OPS = (
    OpSpec(name="badge", family="block", fields=("text", "tone", "variant", "icon", "size", "id", "client_id"), create=create, place=True, order=41,
           doc="a small pill holding a status word, in a tone", mcp="badge {text, tone, variant soft|solid|outline, icon, size s|m}"),
)

KINDS = (
    Kind(name="badge", role="leaf", ops=("badge",), fields=("text",), fit="clamp", solid=True, labelled=True, cell=True, tone_group="badge",
         connectable=True, edit_limit="label", edit_required=True, measure=measure, readback=readback, checks=(truncated_label,), emit=emit,
         hit=hit, text_edit=text_edit, doc="a small pill holding a status word"),
)
