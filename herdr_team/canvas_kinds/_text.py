"""Text layout the block kinds share (canvas v2 phase 2): wrapped titles, bodies with bullets, clamps, captions.

A body is plain text (phase 2, D10): a line that starts with ``- `` or ``* `` is
a bullet with a hanging indent, anything else a paragraph; inline markdown is
shown as typed. Every line is measured with the bundled fonts' metrics
(``canvas_text``), so what a kind stores and draws fits its room (the fit
invariant). ``emit_lines`` turns a laid-out block into display-list text,
body-level text with its skeleton (``canvas_display.body``).

Pure: no I/O.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Sequence, Tuple

from herdr_team import canvas_display as D
from herdr_team import canvas_text

ELLIPSIS = "…"
BULLETS = ("- ", "* ")
BULLET = "•"


@dataclass(frozen=True)
class Para:
    """One paragraph laid out: its lines, the indent they start at, and whether it opens with a bullet."""

    lines: Tuple[str, ...]
    indent: float
    bullet: bool


@dataclass(frozen=True)
class Laid:
    """A block of text laid out at a width: its paragraphs, size, weight, widest line and height."""

    paras: Tuple[Para, ...]
    size: float
    weight: int
    font: str
    width: float
    height: float
    truncated: bool

    @property
    def count(self) -> int:
        return sum(len(p.lines) for p in self.paras)

    def lines(self) -> List[str]:
        return [line for para in self.paras for line in para.lines]


def width(text: str, size: float, weight: int = 400, font: str = "normal") -> float:
    return canvas_text.measure(text, font, size, weight).width


def ellipsize(line: str, room: float, size: float, weight: int, font: str = "normal") -> str:
    """``line`` cut to fit ``room`` with a trailing ellipsis (the line itself when it fits with one)."""
    text = line.rstrip()
    while text and width(text + ELLIPSIS, size, weight, font) > room + 1e-6:
        text = text[:-1].rstrip()
    return text + ELLIPSIS if width(text + ELLIPSIS, size, weight, font) <= room + 1e-6 else ""


def lay(text: str, room: float, size: float, weight: int = 400, font: str = "normal", max_lines: int = 0,
        bullets: bool = True, indent: float = 16.0) -> Laid:
    """``text`` wrapped at ``room`` (bullets hang at ``indent``), at most ``max_lines`` lines when given (the last kept one
    then ends with an ellipsis). A word wider than the room breaks: nothing is ever wider than ``room``."""
    room = max(1.0, float(room))
    rows: List[Tuple[int, str]] = []
    heads: List[Tuple[float, bool]] = []
    for raw in str(text or "").split("\n"):
        bullet = bullets and raw.startswith(BULLETS)
        body = raw[2:] if bullet else raw
        left = indent if bullet else 0.0
        heads.append((left, bullet))
        lines = canvas_text.wrap(body, max(1.0, room - left), font, size, weight) if body.strip() else [""]
        rows += [(len(heads) - 1, line) for line in lines]
    while rows and rows[-1][1] == "":
        rows.pop()
    truncated = bool(max_lines) and len(rows) > max_lines
    if truncated:
        rows = rows[:max_lines]
        index, last = rows[-1]
        rows[-1] = (index, ellipsize(last, room - heads[index][0], size, weight, font))
    paras: List[Para] = []
    for index, (left, bullet) in enumerate(heads):
        lines = tuple(line for i, line in rows if i == index)
        if lines:
            paras.append(Para(lines, left, bullet))
    widest = 0.0
    for para in paras:
        for line in para.lines:
            if line:
                widest = max(widest, para.indent + width(line, size, weight, font))
    lh = canvas_text.line_height(size)
    return Laid(tuple(paras), float(size), int(weight), font, min(widest, room), len(rows) * lh, truncated)


def natural(text: str, size: float, weight: int = 400, font: str = "normal", bullets: bool = True, indent: float = 16.0) -> float:
    """The widest line of ``text`` unwrapped (bullets with their indent)."""
    widest = 0.0
    for raw in str(text or "").split("\n"):
        bullet = bullets and raw.startswith(BULLETS)
        body = raw[2:] if bullet else raw
        widest = max(widest, (indent if bullet else 0.0) + width(body, size, weight, font))
    return widest


def emit_lines(laid: Laid, x: float, top: float, room: float, fill: Any, lod: Optional[str] = "body", anchor: str = "start") -> List[Dict[str, Any]]:
    """A laid-out block as text primitives from ``(x, top)``: one per paragraph (a bullet adds its dot). ``lod``: ``body``
    draws it at the full band with a skeleton below, ``label`` down to the overview band, None always."""
    out: List[Dict[str, Any]] = []
    lh = canvas_text.line_height(laid.size)
    y = top
    style = {"font": laid.font}
    for para in laid.paras:
        if not any(para.lines):
            y += lh * len(para.lines)
            continue
        if anchor == "middle":
            prim = D.text_prim(list(para.lines), x + room / 2.0, y, laid.size, style, fill, "middle", (x, y, room, lh * len(para.lines)), laid.weight)
        else:
            prim = D.text_prim(list(para.lines), x + para.indent, y, laid.size, style, fill, "start",
                               (x + para.indent, y, max(0.0, room - para.indent), lh * len(para.lines)), laid.weight)
        prims = [prim]
        if para.bullet:
            dot = D.text_prim([BULLET], x + 2, y, laid.size, style, fill, "start", (x, y, para.indent, lh), laid.weight)
            prims.append(dot)
        for item in prims:
            if lod == "body":
                out += D.body(item) if item is prim else [dict(item, lod=list(D.LOD_BODY))]
            else:
                if lod == "label":
                    item["lod"] = list(D.LOD_LABEL)
                out.append(item)
        y += lh * len(para.lines)
    return out


def edit(field: str, value: str, box: Sequence[float], size: float, weight: int, fill: Any, part: Optional[str] = None,
         wrap: str = "box", align: str = "start") -> Dict[str, Any]:
    """An edit object (display list 1.2) for one text field, over ``box`` ``[x, y, w, h]``."""
    out = {"field": field, "value": value, "box": [box[0], box[1], box[2], box[3]], "font": "sans", "weight": int(weight), "size": float(size),
           "lh": canvas_text.line_height(size), "align": align, "wrap": wrap, "fill": fill}
    if part is not None:
        out["part"] = part
    return out


def fit_result(w: float, h: float, lines: Sequence[str], inner: Sequence[float], size: float, weight: int, truncated: bool = False,
               policy: str = "hug", grew: bool = False) -> canvas_text.FitResult:
    """A ``FitResult`` for a kind laid out by hand: its title's lines and room (``fits`` checks them)."""
    estimated = any(canvas_text.measure(line, "normal", size, weight).estimated for line in lines)
    return canvas_text.FitResult(float(w), float(h), float(size), tuple(lines), (inner[0], inner[1], inner[2], inner[3]), policy,
                                 grew=grew, truncated=truncated, estimated=estimated, font="normal", weight=int(weight))


def snap(value: float, minimum: float, step: float = 20.0) -> float:
    """``value`` rounded up to the grid when it grew past ``minimum`` (a minimum is kept exactly)."""
    import math

    value = float(math.ceil(value - 1e-9))
    if value > minimum:
        value = max(minimum, math.ceil(value / step - 1e-9) * step)
    return max(float(minimum), value)
