"""Kinds whose content the browser draws (chart, mermaid, viz): a display-list ``slot``.

The page draws the content live (``web/src/v2/render/slots/<slot>.jsx``) and
posts a still of it; the agent's picture draws that still when it exists, and
the placeholder card (the slot's ``fallback``) until then.
"""
from __future__ import annotations

from typing import Any, Callable, Dict, List

from herdr_team import canvas_display as D
from herdr_team.canvas_kinds._common import Element


def still_name(el: Element) -> str:
    """The still the page posts for an element at its version: ``E-5-v40.png``."""
    return "{}-v{}.png".format(el.get("id"), int(D.num(el.get("updated_seq"), 0.0)))


def slot_emit(subtitle: Callable[[Element], str]) -> Callable[[Element, Dict[str, Any]], List[Dict[str, Any]]]:
    """``emit`` of a browser-drawn kind: one slot holding the element's still, with the card as its fallback."""
    def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
        x0, y0, x1, y1 = D.box_of(el)
        name = still_name(el)
        stills = env.get("stills")
        return [{"k": "slot", "slot": str(el.get("type")), "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0,
                 "ref": {"id": str(el.get("id") or ""), "v": int(D.num(el.get("updated_seq"), 0.0))},
                 "still": name if stills is None or name in stills else None, "fallback": D.card(el, subtitle(el))}]
    return emit
