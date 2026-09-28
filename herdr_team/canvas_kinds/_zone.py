"""The v2 section look every container block shares (canvas v2 phase 2, 4.8): a tinted zone, a 60-unit title band.

A section, a kanban and its columns, a timeline and a graph root are stored as
frames (phase 2, D2) and drawn by these helpers: a ``rect r=16`` in the tone's
``zone`` with a hairline ``zone_stroke``, and the title (``section_title``,
20/600) at ``(pad, 16)`` in a 60-unit band, by the title rule
(``canvas_display.title_pair``: in the band close up, above the container
zoomed out, for a top-level container). A container without a title has no
band: its padding is all the room above its content. Plain frames keep their
Phase 1 look (``frame.py``).

Pure: reads the element and the design tokens.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from herdr_team import canvas_display as D
from herdr_team import canvas_text, canvas_theme
from herdr_team.canvas_kinds._common import Element, style_of

_SECTION = canvas_theme.section("section", {}) or {}
#: The title band, where the title sits in it, and the title's type (design 6.2).
BAND = int(_SECTION.get("band", 60))
TITLE_AT_Y = int(_SECTION.get("title_at_y", 16))
TITLE_SIZE = 20
TITLE_WEIGHT = 600
#: The zone's corner radius and hairline.
RADIUS = 16
OUTLINE_PX = 1
#: What an empty section says, muted, while it is big enough on screen to read.
EMPTY_HINT = str(_SECTION.get("empty_hint", "Empty section"))
MAX_TITLE_CHARS = 120

Box = Tuple[float, float, float, float]


def settings_of(el: Element) -> Dict[str, Any]:
    return el.get("settings") if isinstance(el.get("settings"), dict) else {}


def title_of(el: Element) -> str:
    return str(el.get("text") or "")[:MAX_TITLE_CHARS]


def band(el: Element) -> float:
    """The title band: ``BAND`` with a title, else 0 (the padding still applies)."""
    if settings_of(el).get("band") == 0:
        return 0.0
    return float(BAND) if title_of(el).strip() else 0.0


def padding(el: Element) -> float:
    """The room inside the zone around its content: the ``padding`` setting (``s`` 20, ``m`` 32, ``l`` 48; default m)."""
    raw = settings_of(el).get("padding")
    if isinstance(raw, (int, float)) and not isinstance(raw, bool):
        return float(raw)
    return canvas_theme.pad(raw if isinstance(raw, str) else "m")


def content_box(el: Element) -> Box:
    """``(x0, y0, x1, y1)`` inside the padding and the band: where the members go."""
    x0, y0, x1, y1 = D.box_of(el)
    pad = padding(el)
    top = y0 + band(el) + pad
    return x0 + pad, top, max(x0 + pad, x1 - pad), max(top, y1 - pad)


def _paints(el: Element) -> Dict[str, Optional[str]]:
    style = style_of(el)
    tone = style.get("tone") if style.get("tone") in canvas_theme.TONES else "neutral"
    variant = style.get("variant") if style.get("variant") in canvas_theme.VARIANTS else "soft"
    return canvas_theme.resolve_ref(tone, variant, "frame")


def emit(el: Element, env: Dict[str, Any], *, visible: bool = True, hint: bool = False) -> List[Dict[str, Any]]:
    """The zone and its title pair; ``visible=False`` draws nothing (a hit rim only, a mind map's root); ``hint`` adds the
    muted "Empty section" line in the content box."""
    if not visible:
        return []
    x0, y0, x1, y1 = D.box_of(el)
    paints = _paints(el)
    items: List[Dict[str, Any]] = [{"k": "rect", "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "r": RADIUS, "fill": paints["fill"],
                                    "stroke": paints["stroke"] or D.MUTED, "sw_px": OUTLINE_PX}]
    title = title_of(el)
    pad = padding(el)
    if title.strip():
        room = max(0.0, (x1 - x0) - 2 * pad)
        items += D.title_pair(el, title, x0 + pad, y0 + TITLE_AT_Y, float(TITLE_SIZE), TITLE_WEIGHT, paints["text"] or D.INK,
                              not isinstance(el.get("frame"), str), room)
    if hint:
        cx0, cy0, cx1, cy1 = content_box(el)
        size = 16.0
        lh = canvas_text.line_height(size)
        line = D.text_prim([EMPTY_HINT], (x0 + x1) / 2.0, max(cy0, (cy0 + cy1 - lh) / 2.0), size, {}, D.MUTED, "middle",
                           ((x0 + x1) / 2.0 - (cx1 - cx0) / 2.0, max(cy0, (cy0 + cy1 - lh) / 2.0), cx1 - cx0, lh), 400)
        line["lod"] = list(D.LOD_BODY)
        items.append(line)
    return items


def title_edit(el: Element) -> Optional[Dict[str, Any]]:
    """The band title's editor, on one line (``wrap: line``)."""
    x0, y0, x1, _y1 = D.box_of(el)
    pad = padding(el)
    size = float(TITLE_SIZE)
    lh = canvas_text.line_height(size)
    return {"field": "text", "value": title_of(el), "font": "sans", "weight": TITLE_WEIGHT, "size": size,
            "box": [x0 + pad, y0 + TITLE_AT_Y, max(0.0, (x1 - x0) - 2 * pad), lh], "lh": lh, "align": "start", "wrap": "line",
            "fill": _paints(el)["text"] or D.INK}


def hit(el: Element) -> Dict[str, Any]:
    """A frame hit: the band (or, with no title, the padding) selects it; inside, a click reaches the members."""
    return {"shape": "frame", "box": D.xywh(D.box_of(el)), "band": band(el) or padding(el)}


#: The stack layouts a container may use; anything else is a free container (its members stay where they are).
STACKS = ("row", "column", "grid")


#: Room a label needs between two neighbours beyond its own length: the arrow's end gaps, the label's clearance from both
#: ends and a step of slack (``canvas_labels``).
LABEL_ROOM = 24.0


def _label_room(layout: Any, order: List[str], links: List[Dict[str, Any]], gap: float) -> Dict[str, float]:
    """How far each member of a row or column moves along it so that a labelled arrow between neighbours has room
    for its label on the line (QA phase 2, F5): the extra gap accumulates down the stack."""
    if layout not in ("row", "column") or not links:
        return {}
    index = {eid: i for i, eid in enumerate(order)}
    need: Dict[int, float] = {}
    for link in links:
        a, b = index.get(link.get("a")), index.get(link.get("b"))
        if a is None or b is None or abs(a - b) != 1:
            continue
        w, h = link.get("label") or (0.0, 0.0)
        along = float(w if layout == "row" else h)
        need[min(a, b)] = max(need.get(min(a, b), 0.0), along + LABEL_ROOM)
    out: Dict[str, float] = {}
    total = 0.0
    for i, eid in enumerate(order):
        out[eid] = total
        total += max(0.0, need.get(i, 0.0) - gap)
    return out if total else {}


def stack_arrange(root: Element, members: List[Element], env: Dict[str, Any]) -> Any:
    """A stack container's arrangement (phase 2, 4.3): its direct children, in ``env["order"]``, through the ``row``,
    ``column`` or ``grid`` layout (``canvas_layouts``), from its content corner; a free container keeps them. ``stretch``
    is the core's (it refits the members before this runs), so it stacks as ``start`` here."""
    from herdr_team import canvas_layouts
    from herdr_team.canvas_kinds import Arrangement

    settings = settings_of(root)
    layout = settings.get("layout")
    if layout not in STACKS:
        return Arrangement(boxes={})
    by_id = {el["id"]: el for el in env.get("children") or members}
    order = [eid for eid in env.get("order") or [] if eid in by_id]
    if not order:
        return Arrangement(boxes={})
    nodes = []
    for index, eid in enumerate(order):
        x0, y0, x1, y1 = D.box_of(by_id[eid])
        nodes.append(canvas_layouts.LNode(id=eid, w=x1 - x0, h=y1 - y0, order=index))
    align = settings.get("align")
    options: Dict[str, Any] = {}
    if layout == "grid":
        cols = settings.get("cols")
        if isinstance(cols, int) and not isinstance(cols, bool) and cols > 0:
            options["cols"] = cols
        options["align"] = "start"
    else:
        options["align"] = align if align in ("start", "center", "end") else "start"
    gap = canvas_theme.gap(settings.get("gap"), 20)
    result = canvas_layouts.run(layout, canvas_layouts.LayoutRequest(nodes=tuple(nodes), gap=gap, options=options))
    cx0, cy0, _cx1, _cy1 = content_box(root)
    shift = _label_room(layout, order, env.get("links") or [], gap)
    boxes = {}
    for node in nodes:
        px, py = result.positions[node.id]
        extra = shift.get(node.id, 0.0)
        px, py = (px + extra, py) if layout == "row" else (px, py + extra)
        boxes[node.id] = (cx0 + px, cy0 + py, node.w, node.h)
    return Arrangement(boxes=boxes, notes=tuple(n for n in result.notes if not n.startswith("pin_ignored")))
