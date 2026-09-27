"""Labelled shapes: ``box``, ``note``, ``ellipse`` and ``diamond`` (canvas v2 phase 0).

Each is sized from its label before it is placed: a box hugs its text (it
grows past its minimum, wrapping at ``HUG_MAX_W``), a note keeps its paper
size and shrinks the text first, and an ellipse or a diamond grows as a whole
until its inscribed box holds the label. Minimum sizes and padding come from
the design tokens (``canvas_theme``); a ``w``/``h`` the op gives is the
minimum instead.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Tuple

from herdr_team import canvas_text, canvas_theme
from herdr_team.canvas_kinds import Kind
from herdr_team.canvas_kinds._common import Element, number, policy_of, readback, style_of, truncated_label

#: The widest a shape grows before its label wraps; a single word wider than this still widens it.
HUG_MAX_W = {"box": 320, "note": 280, "ellipse": 320, "diamond": 320}
#: The same for a node of a ``graph`` or Mermaid flowchart (an element with a ``group``).
GRAPH_NODE_MAX_W = 240
#: Grown sides round up to the placement grid (design spec 4.1).
SNAP = 20
DEFAULT_POLICY = {"box": "hug", "note": "shrink", "ellipse": "scale_shape", "diamond": "scale_shape"}
SQRT_HALF = math.sqrt(0.5)


def ellipse_inset(w: float, h: float) -> Tuple[float, float, float, float]:
    """The rectangle inscribed in the ellipse, ``w/√2`` x ``h/√2``, centred."""
    iw, ih = w * SQRT_HALF, h * SQRT_HALF
    return (w - iw) / 2.0, (h - ih) / 2.0, iw, ih


def diamond_inset(w: float, h: float) -> Tuple[float, float, float, float]:
    """The rectangle inscribed in the diamond, ``w/2`` x ``h/2``, centred."""
    return w / 4.0, h / 4.0, w / 2.0, h / 2.0


INSETS = {"ellipse": ellipse_inset, "diamond": diamond_inset}


def request(el: Element, minimum: Tuple[float, float], size: Any = None) -> canvas_text.FitRequest:
    """The fit request for a shape element at ``minimum`` (``size`` overrides the style's)."""
    kind = str(el.get("type"))
    style = style_of(el)
    pad_x, pad_y = canvas_theme.padding(kind)
    floor = (canvas_theme.tokens().get("type") or {}).get("shrink_floor", canvas_text.MIN_SIZE)
    fit = el.get("fit") if isinstance(el.get("fit"), dict) else {}
    # clamp keeps ``max_lines``; a clamped element that stored none keeps the lines it shows
    kept = fit.get("lines") if isinstance(fit.get("lines"), list) and fit.get("truncated") else None
    return canvas_text.FitRequest(
        text=str(el.get("text") or ""), font=str(style.get("font") or "normal"), size=number(size or style.get("size"), 20),
        min_w=max(1.0, float(minimum[0])), min_h=max(1.0, float(minimum[1])),
        max_w=GRAPH_NODE_MAX_W if el.get("group") else HUG_MAX_W.get(kind, 320), pad_x=pad_x, pad_y=pad_y,
        inset=INSETS.get(kind), max_lines=int(number(fit.get("max_lines"), len(kept) if kept else 3)), min_size=float(floor), snap=SNAP)


#: A node of a graph or flowchart: a note grows with its label rather than shrink it, so its text reads at
#: the size of its neighbours' (a note shrunk to 14 beside boxes at 20 was hard to read, QA F-12).
GRAPH_POLICY = {"note": "hug"}


def measure(el: Element, minimum: Tuple[float, float]) -> canvas_text.FitResult:
    """The element's size and lines from its label, never smaller than ``minimum``."""
    kind = str(el.get("type"))
    default = GRAPH_POLICY.get(kind) if el.get("group") else None
    return canvas_text.fit(policy_of(el, default or DEFAULT_POLICY.get(kind, "hug")), request(el, minimum))


def _kind(name: str, doc: str) -> Kind:
    return Kind(name=name, role="leaf", ops=("shape", "graph", "mermaid"),
                fields=("text", "w", "h", "tone", "variant", "color", "fill", "font", "size"),
                fit=DEFAULT_POLICY[name], inset=INSETS.get(name), measure=measure, readback=readback,
                checks=(truncated_label,), doc=doc)


KINDS = (
    _kind("box", "a rectangle that grows to fit its label (w/h are its minimum)"),
    _kind("ellipse", "an ellipse; grows as a whole until its inscribed box holds the label"),
    _kind("diamond", "a decision diamond; grows as a whole until its inscribed box holds the label"),
    _kind("note", "a sticky note; keeps its size and shrinks the text (to 14) before it grows"),
)


def defaults() -> Dict[str, Any]:
    """The minimum size of each shape kind, for docs and ``canvas.SHAPE_SIZES``."""
    return {kind.name: canvas_theme.size_min(kind.name) for kind in KINDS}
