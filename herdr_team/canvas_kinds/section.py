"""Sections (canvas v2 phase 2, 4.2 and 4.3): a titled zone that lays out what is put in it.

A section is stored as a frame (``{"type": "frame", "block": "section"}``, phase
2 D2), so every frame mechanism holds: its children move with it, a drop on
the page lands in it, ``frame_edge`` and ``look``'s tree see it. Its
``layout`` decides where its children go:

* ``row``, ``column`` or ``grid``: a stack, in the section's ``order``, ``gap``
  apart (``canvas_layouts``); moving a child reorders it, ``align: stretch``
  gives every child the largest cross size among them;
* ``free`` (the default): children stay where they are put, and ``grid: "CxR"``
  gives the section a local cell grid that ``in`` resolves ``at`` and ``points``
  in (``c0r0`` its content corner, ``c{C}r{R}`` the far one).

Either way the section hugs its children, never below its minimum (the op's
``w``/``h``, else 320x200), with a 60-unit title band (design 6.2).
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional

from herdr_team import canvas_display as D
from herdr_team import canvas_text, canvas_theme
from herdr_team.canvas_kinds import Kind, OpSpec, Tool
from herdr_team.canvas_kinds import _zone
from herdr_team.canvas_kinds._common import Element, bounds, contains, quote
from herdr_team.canvas_kinds.sdk import Block

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 130

LAYOUTS = ("free", "row", "column", "grid")
ALIGNS = ("start", "center", "end", "stretch")
PADS = ("s", "m", "l")
GAPS = ("s", "m", "l")
MIN = (320, 200)
#: A grid section with more children than this reads as a pile: ``section_crowded``.
CROWDED = 60
MAX_COLS = 24
MAX_GRID = 40
#: A local grid cell is at least this big, and ``C`` x ``R`` cells of this size are the default inner size.
CELL_MIN = 40
CELL_DEFAULT = 80
_GRID_RE = re.compile(r"^\s*([1-9][0-9]?)\s*[xX]\s*([1-9][0-9]?)\s*\Z")
SETTINGS = ("layout", "cols", "gap", "padding", "align", "grid")


def grid_of(value: Any) -> Optional[tuple]:
    match = _GRID_RE.match(value) if isinstance(value, str) else None
    return (int(match.group(1)), int(match.group(2))) if match else None


def normalize(ctx: Any, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Defaults and checks: ``cols`` only for a grid, ``grid`` only for a free section."""
    spec["title"] = ctx.text(spec, "title", limit="label", one_line=True)
    layout = ctx.choice(spec, "layout", LAYOUTS, "free")
    spec["layout"] = layout
    gap = spec.get("gap", "s")
    if not (isinstance(gap, str) and gap in GAPS) and not (isinstance(gap, (int, float)) and not isinstance(gap, bool) and 0 <= gap <= 400):
        raise ctx.invalid("gap", "gap is s, m, l or a number of units (0 to 400)")
    spec["gap"] = gap
    spec["padding"] = ctx.choice(spec, "padding", PADS, "m")
    spec["align"] = ctx.choice(spec, "align", ALIGNS, "start")
    if spec.get("cols") is not None:
        if layout != "grid":
            raise ctx.invalid("cols", "cols goes with layout grid")
        spec["cols"] = int(ctx.number(spec, "cols", 1, MAX_COLS))
    if spec.get("grid") is not None:
        if layout != "free":
            raise ctx.invalid("grid", "grid (a local cell grid) goes with layout free")
        found = grid_of(spec["grid"])
        if found is None or max(found) > MAX_GRID:
            raise ctx.invalid("grid", 'grid is "CxR" (columns x rows, each 1 to {})'.format(MAX_GRID))
        spec["grid"] = "{}x{}".format(found[0], found[1])
    return spec


def _settings(spec: Dict[str, Any]) -> Dict[str, Any]:
    return {key: spec[key] for key in SETTINGS if spec.get(key) is not None}


def build(bctx: Any, spec: Dict[str, Any]) -> None:
    """The root with its settings and minimum; ``children`` or a ``region`` join it (on create)."""
    ctx = bctx.ctx
    op = bctx.op
    settings = _settings(spec)
    style = None
    if bctx.root is None or op.get("tone") is not None or op.get("variant") is not None:
        base = bctx.root.get("style") if bctx.root is not None and isinstance(bctx.root.get("style"), dict) else None
        style = ctx.style({k: op[k] for k in ("tone", "variant") if op.get(k) is not None}, "frame", base)
    pad = canvas_theme.pad(settings.get("padding", "m"))
    band = float(_zone.BAND) if str(spec.get("title") or "").strip() else 0.0
    grid = grid_of(settings.get("grid"))
    asked_w = ctx.number(op, "w", 40, 20000) if op.get("w") is not None else None
    asked_h = ctx.number(op, "h", 40, 20000) if op.get("h") is not None else None
    fields: Dict[str, Any] = {}
    if bctx.root is None or asked_w is not None or asked_h is not None or grid is not None:
        if grid is not None:
            inner_w = asked_w if asked_w is not None else grid[0] * CELL_DEFAULT
            inner_h = asked_h if asked_h is not None else grid[1] * CELL_DEFAULT
            settings["cell"] = [max(CELL_MIN, round(inner_w / grid[0], 2)), max(CELL_MIN, round(inner_h / grid[1], 2))]
            minimum = [int(round(settings["cell"][0] * grid[0] + 2 * pad)), int(round(settings["cell"][1] * grid[1] + 2 * pad + band))]
        else:
            minimum = [int(asked_w if asked_w is not None else MIN[0]), int(asked_h if asked_h is not None else MIN[1])]
        fields["fit"] = {"policy": "hug", "min": minimum}
        if bctx.root is None:
            fields["w"], fields["h"] = minimum
    root = bctx.root_fields(text=spec.get("title") or "", style=style, settings=settings, **fields)
    if bctx.root is not None and grid is None and "cell" in (root.get("settings") or {}):
        cleaned = dict(root["settings"])
        cleaned.pop("cell", None)
        root = bctx.ctx.update(root, settings=cleaned)
    _take_children(bctx, root, op)


def _take_children(bctx: Any, root: Element, op: Dict[str, Any]) -> None:
    ctx = bctx.ctx
    children: List[Element] = []
    if op.get("children") is not None:
        raw = op["children"]
        if not isinstance(raw, list) or not raw:
            raise ctx.invalid("children", "children must be a non-empty list of elements")
        for ref in raw:
            child = ctx.lookup(ref, "children")
            if child.get("type") == "comment":
                raise ctx.invalid("children", "comments stay pinned where they are; put the element they point at in the section")
            if child["id"] == root["id"]:
                raise ctx.invalid("children", "a section cannot hold itself")
            if all(c["id"] != child["id"] for c in children):
                children.append(child)
    elif op.get("region") is not None:
        x0, y0, x1, y1 = ctx.region(op["region"], "region")
        inside = [el for el in ctx.live() if el.get("type") != "comment" and el["id"] != root["id"] and contains((x0, y0, x1, y1), bounds(el))
                  and not isinstance(el.get("group"), str)]
        ids = {el["id"] for el in inside}
        children = [el for el in inside if el.get("frame") not in ids and ctx.may_edit(el)]
    for child in children:
        if not ctx.may_edit(child):
            raise ctx.error("element_not_yours", "{} is {}'s; a section can only take elements you may edit".format(child["id"], child.get("author")),
                            id=child["id"], author=child.get("author"))
    for child in children:
        current = ctx.el(child["id"]) or child
        if current.get("frame") != root["id"]:
            ctx.update(current, frame=root["id"])
    if children and (root.get("settings") or {}).get("layout") in _zone.STACKS:
        order = [str(i) for i in root.get("order") or []] + [c["id"] for c in children]
        bctx.set_order(ctx.el(root["id"]) or root, list(dict.fromkeys(order)))


def spec(root: Element, members: List[Element], full: bool) -> Dict[str, Any]:
    out: Dict[str, Any] = {"op": "section"}
    if root.get("alias"):
        out["id"] = root["alias"]
    if root.get("text"):
        out["title"] = root["text"]
    settings = _zone.settings_of(root)
    for key in SETTINGS:
        value = settings.get(key)
        if value is None or (key, value) in (("layout", "free"), ("gap", "s"), ("padding", "m"), ("align", "start")):
            continue
        out[key] = value
    return out


def _counter(el: Element) -> Optional[tuple]:
    """A kanban column's ``· n`` or ``· n/limit`` after its title, and whether it is over its limit."""
    settings = _zone.settings_of(el)
    if not settings.get("counter"):
        return None
    count = int(el.get("count") or 0)
    limit = settings.get("limit")
    if isinstance(limit, int) and not isinstance(limit, bool) and limit > 0:
        return " · {}/{}".format(count, limit), count > limit
    return " · {}".format(count), False


def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The zone and its title (with a kanban column's count), and the "Empty section" hint while it holds nothing."""
    items = _zone.emit(el, env, hint=el.get("count") == 0 and not _zone.settings_of(el).get("counter"))
    counter = _counter(el)
    if counter is None:
        return items
    text, over = counter
    title = next((p for p in items if p.get("k") == "text" and p.get("zoom") is None and p.get("size") == _zone.TITLE_SIZE), None)
    if title is None:
        return items
    x0, _y0, x1, _y1 = D.box_of(el)
    used = max((line.get("w") or 0 for line in title["lines"]), default=0.0)
    room = (x1 - x0) - 2 * _zone.padding(el) - used
    if room <= 0:
        return items
    size = float(_zone.TITLE_SIZE)
    line = D.text_prim([text], title["x"] + used, title["box"][1], size, {}, "tone.danger.text" if over else D.MUTED, "start",
                       (title["x"] + used, title["box"][1], room, canvas_text.line_height(size)), _zone.TITLE_WEIGHT)
    line["lod"] = list(title.get("lod") or D.LOD_LABEL)
    return items + [line]


def readback(el: Element, full: bool) -> str:
    """``E-2 section arch "Auth redesign" row gap m [c40r2 912x436]`` (``look`` adds the children)."""
    settings = _zone.settings_of(el)
    layout = settings.get("layout") or "free"
    extra = " gap {}".format(settings["gap"]) if settings.get("gap") not in (None, "s") else ""
    if settings.get("grid"):
        extra += " grid {}".format(settings["grid"])
    title = str(el.get("text") or "")
    return "{} section{}{} {}{} [{},{} {}x{}]".format(el.get("id"), " " + str(el["alias"]) if el.get("alias") else "",
                                                     " " + quote(title, 0 if full else 80) if title else "", layout, extra,
                                                     el.get("x"), el.get("y"), el.get("w"), el.get("h"))


def crowded(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    if _zone.settings_of(el).get("layout") != "grid":
        return []
    count = sum(1 for other in (env.get("by_id") or {}).values() if other.get("frame") == el.get("id"))
    if count <= CROWDED:
        return []
    return [{"code": "section_crowded", "ids": [str(el.get("id"))],
             "message": "{} holds {} children in a grid (over {}); split it into sections".format(el.get("id"), count, CROWDED), "fix": None}]


def create(ctx: Any, op: Dict[str, Any]) -> None:
    """The ``section`` op: around children, over a region, or placed (``w``/``h`` its minimum); an ``id`` it already
    names is that section, re-set (an upsert)."""
    if op.get("children") is not None and op.get("region") is not None:
        raise ctx.invalid("region", "a section takes children or a region, not both")
    placed = [key for key in ("at", "right_of", "left_of", "below", "above", "inside", "in") if op.get(key) is not None]
    if (op.get("children") is not None or op.get("region") is not None) and not placed:
        if op.get("region") is not None:
            x0, y0, x1, y1 = ctx.region(op["region"], "region")
            op = dict(op, at=[x0, y0], w=op.get("w") or max(40, x1 - x0), h=op.get("h") or max(40, y1 - y0))
        else:
            boxes = [bounds(ctx.lookup(ref, "children")) for ref in op["children"] if isinstance(ref, str)] if isinstance(op["children"], list) else []
            if boxes:
                pad = canvas_theme.pad(op.get("padding") if isinstance(op.get("padding"), str) else "m")
                band = float(_zone.BAND) if str(op.get("title") or "").strip() else 0.0
                op = dict(op, at=[min(b[0] for b in boxes) - pad, min(b[1] for b in boxes) - pad - band])
    ctx.block("section", op)


OPS = (
    # ``gap`` is both the stack's gap and, with right_of/below..., the distance from the reference (it is a placement field).
    OpSpec(name="section", family="block", fields=("title", "layout", "cols", "padding", "align", "grid", "children", "region", "w", "h", "tone", "variant",
                                   "id", "client_id"),
           create=create, place=True, order=35,
           doc="a titled zone that lays out what is put in it: a row, column or grid, or free (with a local grid)",
           mcp="section {title, layout row|column|grid|free, gap s|m|l, padding s|m|l, align start|center|end|stretch, cols, grid \"CxR\", "
               "children | region, w, h, tone}; put things in it with in"),
)

KINDS = (
    Kind(name="section", stored_as="frame", role="container", ops=("section", "kanban"), cell=True, tone_group="frame", layer="zones",
         block=Block(settings=SETTINGS, fields=("title",), parts="members", positional=False, normalize=normalize, build=build, spec=spec),
         arrange=_zone.stack_arrange, emit=emit, hit=_zone.hit, text_edit=_zone.title_edit, readback=readback, checks=(crowded,),
         noun=("section", "sections"), doc="a titled zone that lays out its children (row, column, grid or free)",
         tool=Tool(key="s", title="Section", glyph="▣", gesture="frame", order=85,
                   template='{"op": "section", "title": "Section", "layout": "free"}')),
)
