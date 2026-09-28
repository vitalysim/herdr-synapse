"""Helpers the kind modules share: reading an element's style and fit, and the readback line.

Pure, like the registry: no I/O and no import of ``canvas``.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from herdr_team import canvas_text

Element = Dict[str, Any]


def style_of(el: Element) -> Dict[str, Any]:
    return el.get("style") if isinstance(el.get("style"), dict) else {}


def fit_of(el: Element) -> Dict[str, Any]:
    return el.get("fit") if isinstance(el.get("fit"), dict) else {}


def number(value: Any, default: float) -> float:
    try:
        found = float(value)
    except (TypeError, ValueError):
        return default
    return found if found > 0 else default


def policy_of(el: Element, default: str) -> str:
    """The element's stored fit policy when this build knows it, else the kind's default."""
    policy = fit_of(el).get("policy")
    return policy if isinstance(policy, str) and policy in canvas_text.FIT_POLICIES else default


def quote(text: Any, limit: int = 0) -> str:
    """A label as ``look`` quotes it: one line (`` / `` between lines), cut at ``limit`` with …."""
    value = " / ".join(str(text or "").split("\n"))
    if limit and len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return '"' + value.replace('"', '\\"') + '"'


def fit_notes(el: Element) -> List[str]:
    """What the fit did that a reader should know: shrunk text, clamped lines, estimated widths."""
    fit = fit_of(el)
    notes = []
    drawn = fit.get("size")
    asked = style_of(el).get("size")
    if isinstance(drawn, (int, float)) and isinstance(asked, (int, float)) and drawn < asked:
        notes.append("shrunk to {:g}px".format(drawn))
    if fit.get("truncated"):
        notes.append("clamped {} lines".format(len(fit.get("lines") or [])))
    if fit.get("estimated"):
        notes.append("estimated")
    return notes


def readback(el: Element, full: bool) -> str:
    """``E-7 box "Checkout API" [120,40 212x64]`` plus fit notes: the core of the element's ``look`` line."""
    text = str(el.get("text") or "")
    core = "{} {}{} [{},{} {}x{}]".format(el.get("id"), el.get("type"), " " + quote(text, 0 if full else 80) if text else "",
                                         el.get("x"), el.get("y"), el.get("w"), el.get("h"))
    notes = fit_notes(el)
    return core + (" ({})".format(", ".join(notes)) if notes else "")


def truncated_label(el: Element, env: Optional[Dict[str, Any]] = None) -> List[Dict[str, Any]]:
    """A check: a clamped label keeps its full text but shows only part of it."""
    if not fit_of(el).get("truncated"):
        return []
    return [{"code": "label_truncated", "ids": [str(el.get("id"))],
             "message": "{} {} shows {} of its lines (clamped); the full text is kept: widen it or shorten the text".format(
                 el.get("id"), el.get("type"), len(fit_of(el).get("lines") or [])), "fix": None}]


# --------------------------------------------------------------------------
# the vocabulary the canvas and its kinds share (``canvas`` imports these; kinds never import ``canvas``)

#: The placement grid and cell names (``c<col>r<row>``).
GRID = 20
#: The largest side an element may have.
MAX_SIZE = 20_000
#: The largest side a laid-out block (a graph, a mind map, a stack) may reach: a layout places the members, so a big
#: graph drawn to the right (200 nodes in a chain) is bigger than anything one op sizes by hand (QA phase 2, R4).
MAX_BLOCK_SIZE = 50_000
#: Named colours (legacy Open Color hexes); a named colour is read as its tone since 0.22.
COLORS = {"black": "#1e1e1e", "gray": "#868e96", "red": "#e03131", "pink": "#c2255c", "purple": "#9c36b5",
          "blue": "#1971c2", "teal": "#0c8599", "green": "#2f9e44", "orange": "#f08c00", "yellow": "#f59f00",
          "white": "#ffffff"}
FILLS = {"red": "#ffc9c9", "pink": "#fcc2d7", "purple": "#eebefa", "blue": "#a5d8ff", "teal": "#99e9f2",
         "green": "#b2f2bb", "yellow": "#ffec99", "orange": "#ffd8a8", "gray": "#e9ecef", "white": "#ffffff"}
DASHES = ("solid", "dashed", "dotted")
HEADS = ("arrow", "triangle", "dot", "none")


def round_int(value: float) -> int:
    """Half up to an int (the canvas's coordinates)."""
    import math

    return int(math.floor(float(value) + 0.5))


def r2(value: float) -> Any:
    """Two decimals; an integral value as an int."""
    rounded = round(float(value), 2)
    return int(rounded) if rounded == int(rounded) else rounded


def cell_name(x: Any, y: Any) -> str:
    """``c<col>r<row>`` of the grid cell holding the point ``(x, y)``."""
    import math

    return "c{}r{}".format(int(math.floor(float(x) / GRID)), int(math.floor(float(y) / GRID)))


def bounds(el: Element) -> Any:
    """``(x0, y0, x1, y1)`` of an element (at least one unit wide and tall)."""
    x, y = float(el.get("x") or 0), float(el.get("y") or 0)
    return x, y, x + max(1.0, float(el.get("w") or 1)), y + max(1.0, float(el.get("h") or 1))


def contains(outer: Any, inner: Any) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def bounds_text(el: Element) -> str:
    """``[x,y wxh]`` as ``look`` prints an element's box."""
    return "[{},{} {}x{}]".format(el.get("x"), el.get("y"), el.get("w"), el.get("h"))


def color_name(value: Any) -> str:
    """A stored stroke's legacy colour name, else the hex."""
    for name, hex_value in COLORS.items():
        if hex_value == value:
            return name
    return str(value or "")


def end_name(el: Element, key: str, index: int) -> str:
    """An arrow end as ``look`` names it: the bound element's id, else the cell of its point."""
    if el.get(key):
        return str(el[key])
    points = el.get("points") or []
    if points:
        try:
            point = points[index]
            return cell_name(point[0], point[1])
        except (TypeError, ValueError, IndexError, KeyError):
            return "?"
    return "?"


def who(name: Any, reader: Optional[str]) -> str:
    """``you`` for the reader, ``the operator`` for the human, else the member name."""
    if reader is not None and name == reader:
        return "you"
    return "the operator" if name == "human" else str(name)


def scaled_points(el: Element, new_w: float, new_h: float) -> List[List[Any]]:
    """An arrow's or a stroke's points scaled from its box's top-left corner to a new size."""
    old_w, old_h = max(1.0, float(el.get("w") or 1)), max(1.0, float(el.get("h") or 1))
    x0, y0 = float(el.get("x") or 0), float(el.get("y") or 0)
    sx, sy = new_w / old_w, new_h / old_h
    return [[r2(x0 + (p[0] - x0) * sx), r2(y0 + (p[1] - y0) * sy)] + list(p[2:]) for p in el.get("points") or []]


def shifted_points(el: Element, dx: float, dy: float) -> List[List[Any]]:
    return [[r2(p[0] + dx), r2(p[1] + dy)] + list(p[2:]) for p in el.get("points") or []]
