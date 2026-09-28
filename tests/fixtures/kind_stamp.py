"""A test-only kind: ``stamp``, a small pill holding a status word (canvas v2 phase 1, 2.1; called ``badge`` until the real
``badge`` kind arrived in phase 2).

``tests/test_canvas_one_module.py`` loads this module with ``canvas_kinds._load_extra`` and proves that one module is
all a new kind needs: its op, fields, size, readback, check, drawing and hit test, with no other file changed.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from herdr_team import canvas_display as D
from herdr_team import canvas_text
from herdr_team.canvas_kinds import Kind, OpSpec
from herdr_team.canvas_kinds._common import Element, number, quote, style_of

PAD = (12.0, 4.0)
MINIMUM = (48.0, 28.0)


def measure(el: Element, minimum: Tuple[float, float]) -> canvas_text.FitResult:
    style = style_of(el)
    request = canvas_text.FitRequest(text=str(el.get("text") or ""), size=number(style.get("size"), 16), min_w=max(1.0, minimum[0]),
                                     min_h=max(1.0, minimum[1]), max_w=240, pad_x=PAD[0], pad_y=PAD[1])
    return canvas_text.fit("hug", request)


def readback(el: Element, full: bool) -> str:
    return "{} stamp {} [{},{} {}x{}]".format(el.get("id"), quote(el.get("text")), el.get("x"), el.get("y"), el.get("w"), el.get("h"))


def todo_left(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    if str(el.get("text") or "").strip().upper() != "TODO":
        return []
    return [{"code": "stamp_todo", "ids": [str(el.get("id"))], "message": "{} still says TODO".format(el.get("id")), "fix": None}]


def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    x0, y0, x1, y1 = D.box_of(el)
    paints = D.paints(el)
    pill = {"k": "rect", "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "r": (y1 - y0) / 2.0, "fill": paints["fill"]}
    pill.update(D.stroke_fields(el, paints["stroke"] or D.INK, 1.0))
    items = [pill]
    text = D.label(el, D.text_paint(el, paints))
    if text is not None:
        items.append(text)
    return items


def hit(el: Element) -> Dict[str, Any]:
    return {"shape": "rect", "box": D.xywh(D.box_of(el))}


def create(ctx: Any, op: Dict[str, Any]) -> None:
    text = ctx.text(op, "text", limit="label", one_line=True, required=True)
    style = ctx.style(op, "stamp")
    style["size"] = 16
    fields = ctx.fit({"type": "stamp", "text": text, "style": style}, MINIMUM)
    w, h = fields.pop("w"), fields.pop("h")
    x, y, frame = ctx.place(op, w, h)
    ctx.create("stamp", x, y, w, h, op=op, text=text, style=style, frame=frame, **fields)


OPS = (
    OpSpec(name="stamp", fields=("text", "tone", "id", "client_id"), create=create, place=True, order=900, doc="a small pill holding a status word",
           mcp="stamp {text, tone}"),
)

KINDS = (
    Kind(name="stamp", role="leaf", ops=("stamp",), fit="hug", solid=True, labelled=True, cell=True, tone_group="shape", connectable=True,
         measure=measure, readback=readback, checks=(todo_left,), emit=emit, hit=hit, doc="a small pill holding a status word"),
)
