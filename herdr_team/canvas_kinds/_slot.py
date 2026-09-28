"""Kinds whose content the browser draws (chart, mermaid, viz, scene3d): a display-list ``slot``.

The page draws the content live (``web/src/v2/render/slots/<slot>.jsx``) and
posts a still of it; the agent's picture draws that still when it exists, and
the slot's ``fallback`` until then: the placeholder card (with the first gist
lines of a kind that has a ``gist``), or the kind's own faithful drawing
(``drawn: true``: a chart's Python drawing, a scene's projection).

Since canvas v2 phases 3 and 4 (slot contract v2, ``.local/prd/canvas-v2-phase3-4.md``
1.3 and 1.4) a slot may also carry the stills of several views (``views``), a
document asset the page fetches (``ref.doc``), and whether its live content
needs a WebGL context (``gl``). The writers' still rule is in
``canvas_svg.slot_node``: a slot draws its still when one exists and either the
theme is light or ``drawn`` is not true (stills are light-themed, D17).
"""
from __future__ import annotations

import re
from typing import Any, Callable, Dict, List, Optional, Sequence

from herdr_team import canvas_display as D
from herdr_team import canvas_kinds as _kinds
from herdr_team.canvas_kinds._common import Element

#: Gist lines the placeholder card of a slot without a drawing lists.
CARD_GIST_LINES = 6


def still_name(el: Element, view: str = "") -> str:
    """The still the page posts for an element at its version: ``E-5-v40.png``, or ``E-5-v40-iso.png`` for a view."""
    return "{}-v{}{}.png".format(el.get("id"), int(D.num(el.get("updated_seq"), 0.0)), "-" + view if view else "")


#: A still's name (``E-5-v40.png``, ``E-5-v40-iso.png``): an element without an id or a version has none.
_STILL_RE = re.compile(r"^E-[1-9][0-9]*-v[0-9]+(-[a-z]{1,12})?\.png\Z")


def _resolved(name: str, stills: Any) -> Optional[str]:
    """``name`` when that still exists, or when the caller does not know which exist (``stills`` None: the agent's
    picture, which embeds what it finds and falls back otherwise); None for an element that can have no still."""
    if not _STILL_RE.match(name):
        return None
    return name if stills is None or name in stills else None


def still_of(el: Element, view: str, stills: Any) -> Optional[str]:
    """The still a slot names for ``view`` (``still_name``), or None when the page posted none (``stills``, the names
    that exist; None when unknown) or the element can have none (no id or version)."""
    return _resolved(still_name(el, view), stills)


def gist_card(el: Element, subtitle: str, lines: Sequence[str], title: Optional[str] = None) -> List[Dict[str, Any]]:
    """The placeholder card with the element's gist lines in it (its title, then the lines, muted, then what it is), so an
    agent's picture of a slot the page never drew still reads. ``title`` None is the element's text."""
    if not lines:
        return D.card(el, subtitle, title)
    from herdr_team import canvas_geometry, canvas_text

    x0, y0, x1, y1 = D.box_of(el)
    w, h = x1 - x0, y1 - y0
    items: List[Dict[str, Any]] = [{"k": "rect", "x": x0, "y": y0, "w": w, "h": h, "r": D.CARD_RADIUS, "fill": "base.surface",
                                    "stroke": "tone.neutral.stroke", "sw_px": 1.5, "dash": [8, 6]}]
    room = max(0.0, w - 24)
    small = 11.0
    bottom = y1 - 8 - canvas_text.line_height(small)
    top = y0 + 10
    title = str(el.get("text") or "") if title is None else title
    if title.strip() and top + canvas_text.line_height(14) <= bottom:
        lh = canvas_text.line_height(14)
        prim = D.text_prim([canvas_geometry.fit_line(title, room, 14, 600)], x0 + 12, top, 14, {}, D.INK, "start", (x0 + 12, top, room, lh), 600)
        prim["lod"] = list(D.LOD_LABEL)
        items.append(prim)
        top += lh + 4
    size = 12.0
    lh = canvas_text.line_height(size)
    for line in list(lines)[:CARD_GIST_LINES]:
        if top + lh > bottom:
            break
        text = canvas_geometry.fit_line(str(line), room, size, 400)
        prim = D.text_prim([text], x0 + 12, top, size, {}, D.MUTED, "start", (x0 + 12, top, room, lh), 400)
        prim["lod"] = list(D.LOD_BODY)
        items.append(prim)
        top += lh
    if bottom >= y0 + 4:
        sub = D.text_prim([canvas_geometry.fit_line(subtitle, room, small, 400)], x0 + 12, bottom, small, {}, D.MUTED, "start",
                          (x0 + 12, bottom, room, canvas_text.line_height(small)), 400)
        sub["lod"] = list(D.LOD_BODY)
        items.append(sub)
    return items


def slot_emit(subtitle: Callable[[Element], str], *, fallback: Optional[Callable[[Element, Dict[str, Any]], Optional[List[Dict[str, Any]]]]] = None,
              views: Optional[Sequence[str]] = None, doc: Optional[Callable[[Element], Optional[str]]] = None,
              gl: Optional[Callable[[Element], bool]] = None,
              box: Optional[Callable[[Element], Sequence[float]]] = None) -> Callable[[Element, Dict[str, Any]], List[Dict[str, Any]]]:
    """``emit`` of a browser-drawn kind: one slot holding the element's still, with a fallback.

    ``fallback(el, env)`` is the kind's faithful drawing (the slot says ``drawn: true``); None, or a hook returning None,
    keeps the placeholder card, which lists the kind's first gist lines. ``views`` are the kind's ``still_views`` when
    it has several (``views`` maps each to its still or null; ``still`` stays the first one's). ``doc(el)`` names the
    asset the page fetches (``ref.doc``); ``gl(el)`` says its live content needs WebGL. ``box(el)`` is the slot's
    ``[x, y, w, h]`` when it is not the whole element (a chart's plot below its title); the slot is then the only item
    (a kind that draws a card around it adds that itself)."""
    def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
        x0, y0, x1, y1 = D.box_of(el)
        sx, sy, sw, sh = (x0, y0, x1 - x0, y1 - y0) if box is None else tuple(float(v) for v in box(el))
        stills = env.get("stills")
        names = list(views) if views else [""]
        primary = still_name(el, names[0])
        ref: Dict[str, Any] = {"id": str(el.get("id") or ""), "v": int(D.num(el.get("updated_seq"), 0.0))}
        if doc is not None:
            found = doc(el)
            if isinstance(found, str) and found:
                ref["doc"] = found
        drawing = fallback(el, env) if fallback is not None else None
        item: Dict[str, Any] = {"k": "slot", "slot": str(_slot_key(el)), "x": sx, "y": sy, "w": sw, "h": sh, "ref": ref,
                                "still": _resolved(primary, stills)}
        if views:
            item["views"] = {view: _resolved(still_name(el, view), stills) for view in names}
        if drawing is not None:
            item["fallback"] = drawing
            item["drawn"] = True
        else:
            # A slot inside a card the kind draws (its title above it) is a card of the slot's own size, without the title.
            card_el = el if box is None else dict(el, x=sx, y=sy, w=sw, h=sh)
            item["fallback"] = gist_card(card_el, subtitle(el), _kinds.gist_of(el), None if box is None else "")
        if gl is not None and gl(el):
            item["gl"] = True
        return [item]
    return emit


def _slot_key(el: Element) -> str:
    kind = _kinds.kind_of(el)
    return kind.slot if kind is not None and kind.slot else str(el.get("type"))
