"""Callouts (canvas v2 phase 2, 4.2): a note, tip, warning or decision set apart from the board.

The callout's ``kind`` picks its tone and icon (a decision is ``decision`` and
a scale, a warning ``warning`` and a triangle); ``icon`` overrides the icon. It
hugs its title (16/600) and body (16/400) up to 400 wide, on its tone's soft
fill with a bar down its left edge.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from herdr_team import canvas_display as D
from herdr_team import canvas_icons, canvas_text
from herdr_team.canvas_kinds import Kind, OpSpec
from herdr_team.canvas_kinds import _text as TX
from herdr_team.canvas_kinds._common import Element, quote, truncated_label

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 133

KINDS_TONE = {"note": "info", "tip": "success", "important": "accent", "warning": "warning", "danger": "danger", "decision": "decision",
              "question": "idea"}
KINDS_ICON = {"note": "info", "tip": "lightbulb", "important": "star", "warning": "triangle-alert", "danger": "octagon-alert",
              "decision": "scale", "question": "circle-help"}
MAX_W = 400.0
MIN = (240.0, 64.0)
PAD_X, PAD_Y = 16.0, 14.0
BAR = 4.0
ICON = 20.0
INDENT = 28.0
SIZE = 16.0
TITLE_WEIGHT, BODY_WEIGHT = 600, 400
GAP = 4.0


def _kind(el: Element) -> str:
    found = el.get("callout")
    return found if isinstance(found, str) and found in KINDS_TONE else "note"


def layout(el: Element, w: float) -> Dict[str, Any]:
    inner = max(1.0, w - 2 * PAD_X - BAR - INDENT)
    title = TX.lay(str(el.get("text") or ""), inner, SIZE, TITLE_WEIGHT, bullets=False)
    body_text = el.get("body") if isinstance(el.get("body"), str) else ""
    body = TX.lay(body_text, inner, SIZE, BODY_WEIGHT) if body_text.strip() else None
    lh = canvas_text.line_height(SIZE)
    title_h = title.height if title.count else 0.0
    height = PAD_Y + title_h + (GAP if title_h and body else 0.0) + (body.height if body else 0.0) + PAD_Y
    return {"inner": inner, "title": title, "title_h": title_h, "body": body, "h": max(height, PAD_Y * 2 + lh), "left": PAD_X + BAR + INDENT}


def measure(el: Element, minimum: Tuple[float, float]) -> canvas_text.FitResult:
    """Hug the title and body up to 400 wide (never below ``minimum``, at least 240x64)."""
    min_w, min_h = max(float(minimum[0]), MIN[0]), max(float(minimum[1]), MIN[1])
    extra = 2 * PAD_X + BAR + INDENT
    body_text = el.get("body") if isinstance(el.get("body"), str) else ""
    natural = max(TX.natural(str(el.get("text") or ""), SIZE, TITLE_WEIGHT, bullets=False), TX.natural(body_text, SIZE, BODY_WEIGHT)) + extra
    w = TX.snap(min(max(min_w, natural), max(MAX_W, min_w)), min_w)
    laid = layout(el, w)
    h = TX.snap(max(min_h, laid["h"]), min_h)
    title = laid["title"]
    return TX.fit_result(w, h, title.lines(), (laid["left"], PAD_Y, laid["inner"], max(laid["title_h"], canvas_text.line_height(SIZE))),
                         SIZE, TITLE_WEIGHT, grew=w > min_w or h > min_h)


def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    x0, y0, x1, y1 = D.box_of(el)
    tone = KINDS_TONE[_kind(el)]
    items: List[Dict[str, Any]] = [
        {"k": "rect", "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "r": 8, "fill": "tone.{}.fill".format(tone), "stroke": None},
        {"k": "rect", "x": x0, "y": y0, "w": BAR, "h": y1 - y0, "r": BAR / 2.0, "fill": "tone.{}.solid".format(tone)},
    ]
    laid = layout(el, x1 - x0)
    ink = "tone.{}.text".format(tone)
    icon = el.get("icon") if isinstance(el.get("icon"), str) else KINDS_ICON[_kind(el)]
    lh = canvas_text.line_height(SIZE)
    drawn = canvas_icons.emit(icon, x0 + PAD_X + BAR, y0 + PAD_Y + (lh - ICON) / 2.0, ICON, ink, D.LOD_LABEL)
    if drawn is not None:
        items.append(drawn)
    items += TX.emit_lines(laid["title"], x0 + laid["left"], y0 + PAD_Y, laid["inner"], ink, lod="label")
    if laid["body"] is not None:
        items += TX.emit_lines(laid["body"], x0 + laid["left"], y0 + PAD_Y + laid["title_h"] + (GAP if laid["title_h"] else 0.0), laid["inner"],
                               ink, lod="body")
    return items


def parts(el: Element) -> List[Dict[str, Any]]:
    x0, y0, x1, _y1 = D.box_of(el)
    laid = layout(el, x1 - x0)
    ink = "tone.{}.text".format(KINDS_TONE[_kind(el)])
    lh = canvas_text.line_height(SIZE)
    title_box = [x0 + laid["left"], y0 + PAD_Y, laid["inner"], max(laid["title_h"], lh)]
    body = laid["body"]
    body_top = y0 + PAD_Y + laid["title_h"] + (GAP if laid["title_h"] else 0.0)
    body_box = [x0 + laid["left"], body_top, laid["inner"], max(body.height if body else 0.0, lh)]
    return [{"part": "title", "hit": {"shape": "rect", "box": title_box},
             "edit": TX.edit("text", str(el.get("text") or ""), title_box, SIZE, TITLE_WEIGHT, ink, part="title"), "lod": list(D.LOD_LABEL)},
            {"part": "body", "hit": {"shape": "rect", "box": body_box},
             "edit": TX.edit("body", el.get("body") if isinstance(el.get("body"), str) else "", body_box, SIZE, BODY_WEIGHT, ink, part="body"),
             "lod": list(D.LOD_BODY)}]


def text_edit(el: Element) -> Optional[Dict[str, Any]]:
    return parts(el)[0]["edit"]


def hit(el: Element) -> Dict[str, Any]:
    return {"shape": "rect", "box": D.xywh(D.box_of(el))}


def readback(el: Element, full: bool) -> str:
    """``E-9 callout decision "Open question": Keep sessions or go stateless?``"""
    title = str(el.get("text") or "")
    body = el.get("body") if isinstance(el.get("body"), str) else ""
    short = " ".join(body.split())
    if not full and len(short) > 80:
        short = short[:79] + "…"
    return "{} callout {}{}{}".format(el.get("id"), _kind(el), " " + quote(title, 0 if full else 60) if title else "",
                                      ": " + short if short else "")


def create(ctx: Any, op: Dict[str, Any]) -> None:
    """The ``callout`` op: a note, tip, important point, warning, danger, decision or question, in its kind's tone."""
    kind = ctx.choice(op, "kind", tuple(KINDS_TONE), "note")
    title = ctx.text(op, "title", limit="label", one_line=True)
    body = ctx.text(op, "body", limit="text")
    if not title and not body and not ctx.author_is_human:
        raise ctx.invalid("title", "a callout needs a title or a body")
    fields: Dict[str, Any] = {"callout": kind}
    if body:
        fields["body"] = body
    if op.get("icon") is not None:
        from herdr_team.canvas_kinds.card import icon_name

        found = icon_name(ctx, op["icon"], "icon")
        if found:
            fields["icon"] = found
    style = ctx.style({"tone": KINDS_TONE[kind], "variant": "soft"}, "callout")
    minimum = (ctx.number(op, "w", 1, 20000, MIN[0]), MIN[1])
    fitted = ctx.fit(dict(fields, type="callout", text=title, style=style), minimum)
    w, h = fitted.pop("w"), fitted.pop("h")
    x, y, frame = ctx.place(op, w, h, (float(minimum[0]), float(minimum[1])))
    ctx.create("callout", x, y, w, h, op=op, text=title, style=style, frame=frame, **fields, **fitted)


OPS = (
    OpSpec(name="callout", family="block", fields=("kind", "title", "body", "icon", "w", "id", "client_id"), create=create, place=True, order=38,
           doc="a note, tip, important point, warning, danger, decision or question, set apart in its tone",
           mcp="callout {kind note|tip|important|warning|danger|decision|question, title, body, icon}"),
)

KINDS = (
    Kind(name="callout", role="leaf", ops=("callout",), fields=("title", "body"), fit="hug", solid=True, labelled=True, cell=True,
         tone_group="callout", connectable=True, edit_limit="label", measure=measure, readback=readback, checks=(truncated_label,),
         emit=emit, hit=hit, text_edit=text_edit, parts=parts, doc="a note, tip, warning or decision set apart in its tone"),
)
