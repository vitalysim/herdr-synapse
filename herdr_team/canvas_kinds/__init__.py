"""The canvas component registry (canvas v2): every kind, its ops and its drawing, in one module each.

Every kind of element the canvas knows (``box``, ``text``, ``arrow`` today;
``card``, ``kanban``, ``chart``, ``scene3d`` later) is one ``Kind`` record,
defined in one module of this package, which ``load`` finds by itself. The
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
module in this package, nothing else: every public module here that exports
``KINDS`` or ``OPS`` is registered (``modules()``), in the order its ``ORDER``
says (QA phase 1, V-2). Modules whose name starts with ``_`` are shared helpers,
and a module exporting neither (``sdk``) registers nothing.

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
import pkgutil
import sys
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

#: The version of the ``Kind`` contract itself; bumped when a hook's signature changes (2: ops, drawing, editing;
#: 3: blocks, stored-as kinds, arrangements, reroutes and inline parts, canvas v2 phase 2).
API_VERSION = 3

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
TONE_GROUPS = ("shape", "note", "frame", "arrow", "text", "ink", "other", "card", "badge", "callout")
#: The resize handles the page offers (display list ``handles``).
HANDLES = ("box", "width", "ends", "none")
#: The display-list layers, in drawing order (``canvas_display.LAYERS``).
LAYERS = ("zones", "marks", "labels", "overlays")
#: How a kind's tool on the page makes one (``web/src/v2/interact/gesture.js`` ``GESTURES``): ``shape`` drags a box or
#: clicks for the default size and sends the ``shape`` op with the kind (a ``subkind_of="shape"`` kind only); ``text``
#: opens the text editor where it clicks; ``connect`` drags an arrow between two points or elements; ``pen`` draws a
#: stroke; ``frame`` drags a frame around what it covers; ``block`` clicks or drags and sends the tool's ``template`` op at
#: that point or box (canvas v2 phase 2, 6.4).
GESTURES = ("shape", "text", "connect", "pen", "frame", "block")
#: The keys the page's own tools take (``v`` select, ``h`` hand); a kind's tool may not use them.
RESERVED_TOOL_KEYS = ("v", "h")

#: Where a kind module without its own ``ORDER`` is registered: after the built-in ones, by module name. A module's
#: ``ORDER`` (an int) sets its place in the registration order, which is the order of ``names()``, the shape op's
#: kinds and change summaries; the built-in modules use 10, 20, ... 120.
DEFAULT_ORDER = 1000

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
class Tool:
    """A person's way to make a kind on the page: one button of the v2 tool bar and its key (QA phase 1, V-2).

    The page reads every kind's tool from ``assets/canvas/tokens.json`` (``tools``, written by ``canvas_theme``), so a
    new kind with a tool needs no page edit as long as it uses one of the ``GESTURES``.
    """

    #: The one-letter shortcut, lower case (``r``); unique across kinds, never one of ``RESERVED_TOOL_KEYS``.
    key: str
    #: The button's name (``Rectangle``).
    title: str
    #: A short symbol drawn on the button.
    glyph: str
    #: How the tool makes the kind (one of ``GESTURES``).
    gesture: str
    #: Its place in the tool bar, after the select and hand tools.
    order: int = 100
    #: Back to select after one use (the pen stays, as in most whiteboards).
    one_shot: bool = True
    #: The op the tool sends, as a JSON object text without placement (``{"op": "sticky", "text": ""}``); the gesture
    #: adds ``at`` (and ``w``/``h``, or ``region`` for the frame gesture). Empty: the gesture's own op.
    template: str = ""

    def template_op(self) -> Optional[Dict[str, Any]]:
        """``template`` parsed (None when the tool has none)."""
        return json.loads(self.template) if self.template else None


@dataclass(frozen=True)
class Arrangement:
    """Where a container's or composite's members go (``Kind.arrange``, canvas v2 phase 2, 1.4). Pure data.

    Boxes are world ``(x, y, w, h)``; a pinned member's box must equal its current box. The core applies the boxes
    (refitting a member whose size changed), the routes and the inner frames, then hugs the root unless ``root`` says
    where it goes."""

    boxes: Dict[str, Tuple[float, float, float, float]]
    #: Arrow member id -> ``{"points", "label_at"?}`` (from ``canvas_routers``).
    routes: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    #: Inner frames (a graph's groups) -> their world box.
    frames: Dict[str, Tuple[float, float, float, float]] = field(default_factory=dict)
    #: The root's own box; None: the core hugs it around its members (1.6).
    root: Optional[Tuple[float, float, float, float]] = None
    #: Layout notes, reported as ``layout_note`` warnings.
    notes: Tuple[str, ...] = ()
    #: ``crossings``, ``moved``, ``iterations`` ...
    stats: Mapping[str, float] = field(default_factory=dict)
    #: Fields of the root the arrangement computed from where its members went (a timeline's axis and marks).
    fields: Mapping[str, Any] = field(default_factory=dict)


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
    #: Its button in the page's tool bar; None: people do not make it from the tool bar.
    tool: Optional[Tool] = None
    #: How its elements are stored when that is another kind's ``type``: ``"frame"`` stores ``{"type": "frame",
    #: "block": <name>}`` (a section, a kanban), so every frame mechanism holds (phase 2, D2); None: ``type`` is the name.
    stored_as: Optional[str] = None
    #: A composite described by structure (``canvas_kinds.sdk.Block``): its collections, settings, build and readback.
    block: Optional[Any] = None
    #: ``(root, members, env) -> Arrangement``: where a container's or composite's members go (1.4).
    arrange: Optional[Callable[[Element, List[Element], Dict[str, Any]], Arrangement]] = None
    #: ``(el, start element, end element, env) -> fields``: a connector's route after its ends moved or its route style
    #: changed (``points``, ``x``, ``y``, ``w``, ``h``, ``label_at``?) (3.4).
    reroute: Optional[Callable[[Element, Optional[Element], Optional[Element], Dict[str, Any]], Dict[str, Any]]] = None
    #: May be a node of a graph or a topic of a mindmap (box, ellipse, diamond, note, card, sticky, icon).
    node: bool = False
    #: A stack's ``align: stretch`` may widen or heighten it (False keeps its own shape: a sticky's square paper).
    stretch: bool = True
    #: ``element -> [{"part", "hit", "edit", "lod"?}]``: its inline parts in the display entry (6.2).
    parts: Optional[Callable[[Element], List[Dict[str, Any]]]] = None

    def nouns(self) -> Tuple[str, str]:
        """``(singular, plural)`` for change summaries."""
        if self.noun[0]:
            return self.noun[0], self.noun[1] or self.noun[0] + "s"
        return self.name, self.name + "s"


#: How a block item follows a removed item it names (``sdk.Collection.refs``).
REF_HOWS = ("drop", "clear", "key", "start", "end")

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
    if kind.tool is not None:
        _check_tool(kind)
    if kind.block is not None:
        names = {coll.name for coll in kind.block.collections}
        for coll in kind.block.collections:
            for ref in getattr(coll, "refs", ()):
                if len(ref) != 3 or ref[1] not in names or ref[2] not in REF_HOWS:
                    raise ValueError("kind {}: {} refs {!r} must be (field, one of its collections, one of {})".format(
                        kind.name, coll.name, ref, ", ".join(REF_HOWS)))
    if kind.stored_as is not None:
        if kind.stored_as == kind.name or kind.stored_as not in _REGISTRY or _REGISTRY[kind.stored_as].stored_as is not None:
            raise ValueError("kind {}: stored_as {!r} must name a kind registered before it that is stored as itself".format(
                kind.name, kind.stored_as))
    _REGISTRY[kind.name] = kind
    return kind


def _check_tool(kind: Kind) -> None:
    tool = kind.tool
    assert tool is not None
    if tool.gesture not in GESTURES:
        raise ValueError("kind {}: tool gesture {!r} is not one of {}".format(kind.name, tool.gesture, ", ".join(GESTURES)))
    if tool.gesture == "shape" and kind.subkind_of != "shape":
        raise ValueError("kind {}: the shape gesture sends the shape op, so the kind must be a shape subkind".format(kind.name))
    if tool.template:
        try:
            parsed = json.loads(tool.template)
        except ValueError:
            parsed = None
        if not isinstance(parsed, dict) or not isinstance(parsed.get("op"), str):
            raise ValueError("kind {}: tool template must be a JSON object with an op".format(kind.name))
    if len(tool.key) != 1 or not ("a" <= tool.key <= "z") or tool.key in RESERVED_TOOL_KEYS:
        raise ValueError("kind {}: tool key {!r} must be one lower-case letter other than {}".format(kind.name, tool.key, ", ".join(RESERVED_TOOL_KEYS)))
    for other in _REGISTRY.values():
        if other.name != kind.name and other.tool is not None and other.tool.key == tool.key:
            raise ValueError("kind {}: tool key {!r} is already {}'s".format(kind.name, tool.key, other.name))


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


def _discover() -> List[Tuple[int, str, Any]]:
    """``(order, name, module)`` of every kind module in this package, in registration order."""
    found = []
    for info in pkgutil.iter_modules(__path__):
        if info.name.startswith("_"):
            continue
        name = "{}.{}".format(__name__, info.name)
        module = importlib.import_module(name)
        if not (hasattr(module, "KINDS") or hasattr(module, "OPS")):
            continue
        order = getattr(module, "ORDER", DEFAULT_ORDER)
        if not isinstance(order, int) or isinstance(order, bool):
            raise ValueError("kind module {}: ORDER must be an int, not {!r}".format(info.name, order))
        found.append((order, info.name, module))
    return sorted(found, key=lambda item: (item[0], item[1]))


def modules() -> List[str]:
    """The kind modules found in this package (their short names), in registration order."""
    return [name for _order, name, _module in _discover()]


def load() -> None:
    """Import every kind module in this package once (``modules()``); each registers its ``KINDS`` and ``OPS``."""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    try:
        for _order, short, module in _discover():
            _register_module(module, "{}.{}".format(__name__, short))
    except BaseException:
        _LOADED = False
        raise


def _reload() -> None:
    """Test hook: forget every registration and find the kind modules again (after a test adds a module file)."""
    global _LOADED
    _REGISTRY.clear()
    _OPS.clear()
    _OWNER.clear()
    _LOADED = False
    load()
    for hook in _CHANGED:
        hook()


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


def kind_of(el: Mapping[str, Any]) -> Optional[Kind]:
    """An element's kind: its ``block`` kind when that kind is stored as the element's ``type`` (a section is a frame
    with ``block: "section"``), else the kind its ``type`` names; None for a kind this build does not know."""
    load()
    block = el.get("block")
    if isinstance(block, str):
        found = _REGISTRY.get(block)
        if found is not None and found.stored_as is not None and found.stored_as == el.get("type"):
            return found
    kind = _REGISTRY.get(el.get("type")) if isinstance(el.get("type"), str) else None
    # A kind stored as another is never an element's own type (an element ``{"type": "kanban"}`` is no kanban).
    return kind if kind is not None and kind.stored_as is None else None


def element_types() -> List[str]:
    """The element ``type`` values this build stores: every kind stored as itself (a section is a ``frame``)."""
    return [kind.name for kind in kinds() if kind.stored_as is None]


def blocks() -> List[Kind]:
    """The kinds described by structure (``Kind.block``), in registration order."""
    return [kind for kind in kinds() if kind.block is not None]


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


def tools() -> List[Kind]:
    """The kinds with a tool bar button, in tool bar order (``Tool.order``, then registration order)."""
    found = [kind for kind in kinds() if kind.tool is not None]
    return sorted(found, key=lambda kind: (kind.tool.order if kind.tool is not None else 0, found.index(kind)))


def slots() -> List[str]:
    """Every slot renderer key a kind names (the v2 page's ``SLOT_KINDS``)."""
    return [kind.slot for kind in kinds() if kind.slot]


def outline(el: Element) -> str:
    """The element's outline (``rect`` for a kind this build does not know)."""
    kind = kind_of(el)
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
    kind = kind_of(el)
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
    kind = kind_of(el)
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
