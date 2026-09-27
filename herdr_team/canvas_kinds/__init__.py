"""The canvas component registry (canvas v2): every kind, its ops and its drawing, in one module each.

Every kind of element the canvas knows (``box``, ``text``, ``arrow`` today;
``card``, ``kanban``, ``chart``, ``scene3d`` later) is one ``Kind`` record,
defined in one module of this package and listed once in ``_MODULES``. The
record carries the hooks the rest of the canvas asks a kind for: which op
fields it takes, how its text is fitted (``canvas_text`` fit policies), how
big it is before layout, what it reads back as in ``look``, which checks
apply to it, how an older stored element is upgraded, what it draws as in the
display list (``emit``), how a click hits it, what a double-click edits, and
how it moves and resizes. A hook left ``None`` means "the generic behaviour",
so a kind grows hook by hook without touching the others.

Since canvas v2 phase 1 (``API_VERSION = 2``) a module also exports ``OPS``:
the ``OpSpec`` of each op it adds (``shape``, ``arrow`` ...), so the op table,
the accepted fields, the MCP op table and the reference docs are all derived
from this registry (``.local/prd/canvas-v2-phase1.md`` 2). A module may export
``OPS`` without ``KINDS`` (``graph`` creates other kinds). Adding a kind is one
module plus its name in ``_MODULES``, nothing else.

The v1 (Excalidraw) page keeps its own list of builders under
``web/src/canvas/kinds/``; ``Kind.page`` says a kind has one there, and a test
holds ``web/dist/kinds.json`` equal to ``names(page_only=True)``. The v2 page
draws every kind from the display list and needs nothing per kind, except a
slot renderer for content the browser draws itself (``Kind.slot``).

Pure: no I/O, no Herdr calls, and no import of ``canvas`` (that module
imports this one). Kind modules may import ``canvas_text``, ``canvas_theme``,
``canvas_geometry`` and ``canvas_display``, never ``canvas``; an op handler
reaches the canvas only through the ``OpContext`` it is given
(``canvas_kinds.sdk``).
"""
from __future__ import annotations

import importlib
import sys
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

#: The version of the ``Kind`` contract itself; bumped when a hook's signature changes (2: ops, drawing, editing).
API_VERSION = 2

#: The outlines layout knows how to test exactly (arrow label placement, making room). Anything
#: else is its bounding box.
OUTLINES = ("rect", "ellipse", "diamond")

#: How a kind takes part in layout: a ``leaf`` is sized from its own content; a ``container``
#: sizes around its children (a frame, a section); a ``composite`` lays out its own addressable
#: parts (a graph, a kanban); a ``connector`` is routed between others (an arrow); an ``overlay``
#: takes no room (a comment pin).
ROLES = ("leaf", "container", "composite", "connector", "overlay")

#: How a tone colours a kind (``canvas_theme.resolve``): a shape is fill and outline, a note sticky paper, a
#: frame a tinted zone, an arrow a line with a muted label, free text and ink strokes the text colour.
TONE_GROUPS = ("shape", "note", "frame", "arrow", "text", "ink", "other")
#: The resize handles the page offers (display list ``handles``).
HANDLES = ("box", "width", "ends", "none")
#: The display-list layers, in drawing order (``canvas_display.LAYERS``).
LAYERS = ("zones", "marks", "labels", "overlays")

#: The kind modules, in registration order. Adding a kind: one module here plus this one line.
_MODULES: Tuple[str, ...] = ("shape", "text", "arrow", "frame", "pen", "path", "svg", "diagram", "chart", "viz", "image", "comment")

Element = Dict[str, Any]
Size = Tuple[float, float]
Box = Tuple[float, float, float, float]


@dataclass(frozen=True)
class OpSpec:
    """One op a kind module adds to the canvas (``canvas.OPS`` is these, by ``order``, then the core ops)."""

    #: The op's name, e.g. ``card``.
    name: str
    #: The fields it takes beyond ``op``, ``intent`` and ``if_version``.
    fields: Tuple[str, ...]
    #: ``(ctx, op) -> None``: validates the op and creates what it draws through the SDK (``sdk.OpContext``).
    create: Callable[[Any, Dict[str, Any]], None]
    #: Also accepts the canvas style fields (``tone``, ``color``, ``size`` ...).
    style: bool = False
    #: Also accepts the placement fields (``at``, ``right_of`` ... ``gap``).
    place: bool = False
    #: Its position in ``canvas.OPS``: the ops that existed before phase 1 keep their order (10, 20, ...).
    order: int = 100
    #: One line for ``docs/reference.md`` and help.
    doc: str = ""
    #: Its fragment of the MCP op table, e.g. ``card {title, body, tone}``.
    mcp: str = ""


@dataclass(frozen=True)
class Kind:
    """One element kind and its hooks. ``name`` is the element ``type`` in ``scene.json``."""

    name: str
    #: The shape of this kind's stored element; an element stores ``kv`` only when it is above 1.
    version: int = 1
    role: str = "leaf"
    #: The ops that create this kind (``shape`` creates ``box``, ``ellipse`` ...).
    ops: Tuple[str, ...] = ()
    #: Op fields this kind accepts beyond the common ones (``op``, ``intent``, ``if_version``).
    fields: Tuple[str, ...] = ()
    #: The default fit policy (a name in ``canvas_text.FIT_POLICIES``); None for kinds without text.
    fit: Optional[str] = None
    #: Whether the v1 (Excalidraw) page has a builder for it (phase 1 D8); a kind without one is a
    #: placeholder there and draws fully on the v2 page, which needs nothing per kind.
    page: bool = False
    #: The drawn outline inside the element's box (one of ``OUTLINES``): what a label must keep clear of.
    outline: str = "rect"
    #: Whether other marks may be placed on it (a door on the walls): it then grows to hold them, and moves with
    #: them, the way a frame does with its children, without owning them (QA R-1).
    hosts: bool = False
    #: ``"shape"`` for a kind the ``shape`` op creates with its ``kind`` field (box, ellipse, ...).
    subkind_of: Optional[str] = None
    #: ``("pen stroke", "pen strokes")``: what change summaries call it (defaults to the name and name + "s").
    noun: Tuple[str, str] = ("", "")
    #: Takes room: overlap, arrow_through and making room consider it.
    solid: bool = False
    #: A shape with a fitted label inside (a text on it is ``text_on_label``).
    labelled: bool = False
    #: ``look`` prints its cell.
    cell: bool = False
    #: How a tone colours it (one of ``TONE_GROUPS``).
    tone_group: str = "other"
    #: The display-list layer it draws in (one of ``LAYERS``; an item may override it).
    layer: str = "marks"
    #: Browser-drawn content: the page's slot renderer key (``web/src/v2/render/slots/<slot>.jsx``).
    slot: Optional[str] = None
    #: The resize handles the page offers (one of ``HANDLES``).
    handles: str = "box"
    #: Arrows may bind to it.
    connectable: bool = False
    #: The field ``edit`` changes; None: not editable.
    edit_field: Optional[str] = "text"
    #: The most characters that field holds (``text`` for up to 2000, ``label`` for one line of 200, ``comment``).
    edit_limit: str = "label"
    #: ``edit`` refuses an empty text (a free text, a live visual's title, a comment).
    edit_required: bool = False
    #: ``(w, h) -> (x, y, w, h)``: the box text may use inside a ``w`` x ``h`` element (a diamond's is smaller).
    inset: Optional[Callable[[float, float], Tuple[float, float, float, float]]] = None
    #: ``(element, minimum (w, h)) -> fit result``: the element's size from its content, before layout.
    measure: Optional[Callable[[Element, Size], Any]] = None
    #: ``(element, full) -> str``: the element's compact form in ``look`` (without "by who — intent").
    readback: Optional[Callable[[Element, bool], str]] = None
    #: Each ``(element, env) -> [problem]``, run by ``canvas_check`` next to the global checks.
    checks: Tuple[Callable[[Element, Dict[str, Any]], List[Dict[str, Any]]], ...] = ()
    #: ``(element, stored version) -> element``: an older stored element in this version's shape.
    upgrade: Optional[Callable[[Element, int], Element]] = None
    #: ``(element, env) -> [primitive]``: what it draws (``canvas_display``); None draws the placeholder card.
    emit: Optional[Callable[[Element, Dict[str, Any]], List[Dict[str, Any]]]] = None
    #: ``element -> hit`` (display list 1.2); None: the rectangle of its box.
    hit: Optional[Callable[[Element], Dict[str, Any]]] = None
    #: ``element -> edit or None`` (display list 1.2); None: from its measured inner box.
    text_edit: Optional[Callable[[Element], Optional[Dict[str, Any]]]] = None
    #: ``element -> (x0, y0, x1, y1)``: everything it draws; None: its box.
    bounds: Optional[Callable[[Element], Box]] = None
    #: ``(element, dx, dy) -> fields``: the fields a move by ``(dx, dy)`` changes; None: ``x`` and ``y``.
    translate: Optional[Callable[[Element, float, float], Dict[str, Any]]] = None
    #: ``(element, w, h, ctx) -> fields``: the fields a resize to ``w`` x ``h`` changes (either may be None); None: its
    #: size, or for a kind with ``measure`` the size its label needs over that new minimum.
    resize: Optional[Callable[[Element, Optional[float], Optional[float], Any], Dict[str, Any]]] = None
    #: ``(ctx, op, text, style) -> (fields, asked)``: the ``w``, ``h`` and ``fit`` a new element takes from the op that
    #: creates it, and the size the op asked for; None: fitted from the op's ``w``/``h`` over the kind's minimum size.
    initial: Optional[Callable[[Any, Dict[str, Any], str, Dict[str, Any]], Tuple[Dict[str, Any], Tuple[float, float]]]] = None
    #: ``(element, reader, full) -> str``: the whole ``look`` line, for a kind whose line is not ``readback`` + "by who".
    line: Optional[Callable[[Element, Optional[str], bool], str]] = None
    #: One line for generated docs and MCP schema descriptions.
    doc: str = ""

    def nouns(self) -> Tuple[str, str]:
        """``(singular, plural)`` for change summaries."""
        if self.noun[0]:
            return self.noun[0], self.noun[1] or self.noun[0] + "s"
        return self.name, self.name + "s"


_REGISTRY: Dict[str, Kind] = {}
_OPS: Dict[str, OpSpec] = {}
#: Which module registered each kind and op (``_unload`` takes a test module's out again).
_OWNER: Dict[Tuple[str, str], str] = {}
_LOADED = False


def register(kind: Kind) -> Kind:
    """Add ``kind``; a second kind with the same name, or an unknown role, is a programming error."""
    for field, value, allowed in (("role", kind.role, ROLES), ("outline", kind.outline, OUTLINES),
                                  ("tone_group", kind.tone_group, TONE_GROUPS), ("handles", kind.handles, HANDLES),
                                  ("layer", kind.layer, LAYERS)):
        if value not in allowed:
            raise ValueError("kind {}: {} {!r} is not one of {}".format(kind.name, field, value, ", ".join(allowed)))
    if kind.name in _REGISTRY and _REGISTRY[kind.name] is not kind:
        raise ValueError("kind {} is registered twice".format(kind.name))
    _REGISTRY[kind.name] = kind
    return kind


def register_op(spec: OpSpec, reserved: Sequence[str] = ()) -> OpSpec:
    """Add an op; a second op with the same name (or a core op's), repeated fields, or no ``create`` are programming errors."""
    if not spec.name or spec.name in reserved:
        raise ValueError("op {!r} is reserved or empty".format(spec.name))
    if spec.name in _OPS and _OPS[spec.name] is not spec:
        raise ValueError("op {} is registered twice".format(spec.name))
    if len(set(spec.fields)) != len(spec.fields):
        raise ValueError("op {}: a field is listed twice".format(spec.name))
    if not callable(spec.create):
        raise ValueError("op {}: create is not callable".format(spec.name))
    _OPS[spec.name] = spec
    return spec


def _register_module(module: Any, owner: str) -> None:
    for kind in getattr(module, "KINDS", ()):
        register(kind)
        _OWNER[("kind", kind.name)] = owner
    for spec in getattr(module, "OPS", ()):
        register_op(spec)
        _OWNER[("op", spec.name)] = owner


def load() -> None:
    """Import every module in ``_MODULES`` once; each registers its ``KINDS`` and ``OPS``."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        for module_name in _MODULES:
            name = "{}.{}".format(__name__, module_name)
            _register_module(importlib.import_module(name), name)
    except BaseException:
        _LOADED = False
        raise


def _load_extra(module_name: str) -> None:
    """Test hook: register one more module's ``KINDS`` and ``OPS`` (the one-module test's ``badge``)."""
    load()
    module = importlib.import_module(module_name)
    _register_module(module, module_name)
    for hook in _CHANGED:
        hook()


def _unload(module_name: str) -> None:
    """Test hook: take the kinds and ops ``_load_extra`` registered from ``module_name`` out again."""
    for (what, name), owner in list(_OWNER.items()):
        if owner != module_name:
            continue
        (_REGISTRY if what == "kind" else _OPS).pop(name, None)
        del _OWNER[(what, name)]
    sys.modules.pop(module_name, None)
    for hook in _CHANGED:
        hook()


#: Called after ``_load_extra`` and ``_unload``: modules that derive tables from the registry drop their caches.
_CHANGED: List[Callable[[], None]] = []


def on_change(hook: Callable[[], None]) -> None:
    """Call ``hook`` whenever a test hook changes the registry (derived tables are cached)."""
    if hook not in _CHANGED:
        _CHANGED.append(hook)


def get(name: Any) -> Optional[Kind]:
    """The kind named ``name``, or None for a kind this build does not know (callers fall back to generic)."""
    load()
    return _REGISTRY.get(name) if isinstance(name, str) else None


def kinds() -> List[Kind]:
    """Every registered kind, in registration order."""
    load()
    return list(_REGISTRY.values())


def names(page_only: bool = False) -> List[str]:
    """Registered kind names; ``page_only`` keeps those the v1 page has a builder for."""
    return [kind.name for kind in kinds() if kind.page or not page_only]


def ops() -> List[OpSpec]:
    """Every op the kind modules add, by ``order`` (then registration order)."""
    load()
    specs = list(_OPS.values())
    return sorted(specs, key=lambda spec: (spec.order, specs.index(spec)))


def op(name: Any) -> Optional[OpSpec]:
    """The op named ``name`` a kind module adds, or None."""
    load()
    return _OPS.get(name) if isinstance(name, str) else None


def subkinds(parent: str) -> List[Kind]:
    """The kinds an op picks with its ``kind`` field (``shape``: box, ellipse, diamond, note, text)."""
    return [kind for kind in kinds() if kind.subkind_of == parent]


def slots() -> List[str]:
    """Every slot renderer key a kind names (the v2 page's ``SLOT_KINDS``)."""
    return [kind.slot for kind in kinds() if kind.slot]


def outline(el: Element) -> str:
    """The element's outline (``rect`` for a kind this build does not know)."""
    kind = get(el.get("type"))
    return kind.outline if kind is not None else "rect"


def _norm(outline_name: str, box: Box, x: float, y: float) -> float:
    """The outline's own distance of ``(x, y)`` from its centre: 1 on the outline, below 1 inside it."""
    cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
    a, b = max((box[2] - box[0]) / 2.0, 0.5), max((box[3] - box[1]) / 2.0, 0.5)
    dx, dy = (x - cx) / a, (y - cy) / b
    if outline_name == "ellipse":
        return dx * dx + dy * dy
    if outline_name == "diamond":
        return abs(dx) + abs(dy)
    return max(abs(dx), abs(dy))


def outline_meets(box: Box, outline_name: str, rect: Box) -> bool:
    """Whether ``rect`` reaches into the outline drawn in ``box``. Exact for every outline in ``OUTLINES``:
    each norm is separable, so the rectangle's point nearest the centre is the centre clamped into it."""
    if not (box[0] < rect[2] and rect[0] < box[2] and box[1] < rect[3] and rect[1] < box[3]):
        return False
    cx, cy = (box[0] + box[2]) / 2.0, (box[1] + box[3]) / 2.0
    return _norm(outline_name, box, min(max(cx, rect[0]), rect[2]), min(max(cy, rect[1]), rect[3])) < 1.0


def outline_holds(box: Box, outline_name: str, rect: Box) -> bool:
    """Whether ``rect`` lies wholly inside the outline drawn in ``box`` (every outline is convex: its corners do)."""
    return all(_norm(outline_name, box, x, y) <= 1.0 + 1e-9 for x in (rect[0], rect[2]) for y in (rect[1], rect[3]))


def hosts(el: Element) -> bool:
    """Whether marks placed on this element sit on it (it grows to hold them and carries them when it moves)."""
    kind = get(el.get("type"))
    return bool(kind is not None and kind.hosts)


def by_op(op_name: str) -> List[Kind]:
    """The kinds an op creates."""
    return [kind for kind in kinds() if op_name in kind.ops]


def drawn(el: Element) -> Any:
    """The element's label as it is drawn now, or None for a kind without text: at the element's current
    size and drawn text size (``fit.size``, else ``style.size``) when it fits there (the ``keep`` fit
    policy), else the size its kind would grow it to, with no further shrinking.

    ``canvas_check`` compares its size with the element's to find overflow, and
    ``canvas_display`` takes its inner box and lines to draw the label.
    """
    kind = get(el.get("type"))
    if kind is None or kind.measure is None:
        return None
    style = el.get("style") if isinstance(el.get("style"), dict) else {}
    fit = el.get("fit") if isinstance(el.get("fit"), dict) else {}
    size = fit.get("size") if isinstance(fit.get("size"), (int, float)) and fit["size"] > 0 else style.get("size")
    # A clamped label shows only its first lines by design (``label_truncated`` says so); anything else must fit.
    probe = dict(el, style=dict(style, size=size or 20), fit=dict(fit, policy="clamp" if fit.get("policy") == "clamp" else "keep"))
    w, h = max(1.0, _num(el.get("w"), 1.0)), max(1.0, _num(el.get("h"), 1.0))
    if el.get("type") == "text":
        # A text's box is its lines: wrapped at its wrap width when it wraps, else as wide as they need.
        stored = fit.get("min") if isinstance(fit.get("min"), list) and len(fit["min"]) == 2 else None
        wrap = (_num(stored[0], w) if stored else w) if el.get("wrap") else 1.0
        return kind.measure(probe, (max(1.0, wrap), 1.0))
    return kind.measure(probe, (w, h))


def _num(value: Any, default: float) -> float:
    try:
        found = float(value)
    except (TypeError, ValueError):
        return default
    return found if found == found and found not in (float("inf"), float("-inf")) else default
