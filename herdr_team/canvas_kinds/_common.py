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
