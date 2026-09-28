"""What an op handler may ask the canvas for (canvas v2 phase 1, 2.3): the ``OpContext`` protocol.

A kind module's ``OpSpec.create(ctx, op)`` validates its op and draws through
this interface only; ``canvas._KindCtx`` implements it over the batch being
applied. Kind modules never import ``canvas``, so a new kind is one module and
the canvas core never learns its name.

Every ``raise ctx.invalid(...)`` (or ``ctx.error``, ``ctx.too_big``) refuses
the op with the canvas's own error codes. Methods may be added; their meaning
never changes.

Since canvas v2 phase 2 (``.local/prd/canvas-v2-phase2.md`` 1.3) a kind may be
a *block*: an element an agent describes by structure (items, relations, tone)
and never by pixels. Its ``Kind.block`` is a ``Block``: the ``Collection`` of
items it holds, the settings it keeps, and how it is built (``build``, through
a ``BlockContext``) and read back (``spec``). ``canvas_blocks`` runs the one
pipeline every block op, patch, upsert and part edit goes through.

Since canvas v2 phases 3 and 4 (``.local/prd/canvas-v2-phase3-4.md`` 1.2) a
block may read files: ``Block.load`` runs before the canvas lock is taken, reads
the team's artifacts through a ``FetchIO`` (never anything else), computes what
it can (a chart's table and compile, a 3D scene's solve), and ``build`` finds the
result as ``bctx.loaded``. What it keeps goes into the content-addressed asset
store through ``bctx.store_asset``, written only when the op applies.

Pure: protocols and dataclasses, nothing else.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

try:  # ``typing.Protocol`` is 3.8+; the plugin supports 3.9, so this is only for type checkers on odd builds
    from typing import Protocol
except ImportError:  # pragma: no cover
    Protocol = object  # type: ignore[assignment,misc]

Element = Dict[str, Any]
Op = Mapping[str, Any]


@dataclass(frozen=True)
class Fetched:
    """A file ``Block.load`` read from the team's artifacts."""

    #: Its path relative to ``artifacts/`` (``models/robot.glb``), normalised.
    rel: str
    data: bytes
    #: The hex sha256 of ``data``.
    sha256: str
    #: Its suffix in lower case (``.csv``).
    suffix: str


class FetchIO(Protocol):
    """What ``Block.load`` may read: the team's artifacts, never anything else (``canvas`` implements it).

    Every refusal is a ``HerdrTeamError``: ``artifacts_unset`` (the team has no project folder), ``path_refused`` (not a
    relative path to a regular file under ``artifacts/``, symlinks resolved, or a suffix not in ``suffixes``) and
    ``too_big`` (with ``limit`` and ``max``) past ``max_bytes``. ``field`` names the op field in the refusal."""

    def read_artifact(self, rel: Any, suffixes: Sequence[str], max_bytes: int, *, field: str = "data") -> Fetched: ...
    #: A file next to an earlier one (a ``.gltf``'s buffers and textures): relative to ``fetched``'s folder, no ``..``,
    #: no scheme, no absolute path, inside the same artifacts tree.
    def sibling(self, fetched: Fetched, rel: str, suffixes: Sequence[str], max_bytes: int) -> Fetched: ...


class OpContext(Protocol):
    """The batch an op applies in, as a kind module sees it."""

    #: Who applies the batch (a member name, or ``human``), whether that is the operator, and the op's intent.
    author_name: str
    author_is_human: bool
    intent: str
    now: float
    team_name: str

    # -- refusals ---------------------------------------------------------------------------------------
    def invalid(self, field: str, message: str, **details: Any) -> Exception: ...
    def error(self, code: str, message: str, **details: Any) -> Exception: ...
    def too_big(self, field: str, limit: str, maximum: int, message: str) -> Exception: ...

    # -- reading fields -----------------------------------------------------------------------------------
    #: ``limit``: ``text`` (2,000), ``label`` (200, one line), ``comment`` (1,000), ``symbol`` (80). ``value`` reads
    #: something other than ``op[field]`` (a graph node's text), ``label`` names it in errors.
    def text(self, op: Op, field: str, *, limit: str = "text", one_line: bool = False, required: bool = False,
             value: Any = ..., label: Optional[str] = None) -> str: ...
    def choice(self, op: Op, field: str, choices: Sequence[str], default: str, *, value: Any = ..., label: Optional[str] = None) -> str: ...
    def number(self, op: Op, field: str, low: float, high: float, default: Optional[float] = None) -> Optional[float]: ...
    def boolean(self, op: Op, field: str, default: bool) -> bool: ...
    def points(self, op: Op, field: str, maximum: int, *, pressure: bool = False, minimum: int = 2, limit_name: str = "") -> List[List[float]]: ...
    def point(self, value: Any, field: str) -> List[float]: ...
    def is_point(self, value: Any) -> bool: ...
    def color(self, value: Any) -> str: ...
    def fill(self, value: Any) -> Optional[str]: ...
    def guard_code(self, text: str, field: str) -> None: ...

    # -- style and size -----------------------------------------------------------------------------------
    def style(self, op: Op, kind: str, base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]: ...
    def default_style(self, kind: str) -> Dict[str, Any]: ...
    def size(self, op: Op, default_w: float, default_h: float) -> Tuple[float, float]: ...
    #: ``{"w", "h", "fit"}`` of an element sized from its label by its kind (``{}`` for a kind without ``measure``).
    def fit(self, el: Element, minimum: Tuple[float, float]) -> Dict[str, Any]: ...
    #: A free text's ``w``/``h``/``wrap``/``fit`` for its text, style and wrap width.
    def text_fields(self, el: Element, text: str, style: Dict[str, Any], wrap_w: Optional[float] = None) -> Dict[str, Any]: ...
    #: What a labelled element may not shrink below: an op's ``w``/``h``, else its ``fit.min``, else its size.
    def minimum(self, el: Element, w: Any = None, h: Any = None) -> Tuple[float, float]: ...
    def shape_size(self, kind: str) -> Tuple[float, float]: ...

    # -- the canvas ---------------------------------------------------------------------------------------
    def lookup(self, ref: Any, field: str) -> Element: ...
    def live(self) -> List[Element]: ...
    #: An element as this op would leave it (None when it was deleted).
    def el(self, eid: str) -> Optional[Element]: ...
    #: Whether the author may change ``el`` (its author, the manager for an agent's, the operator for anything).
    def may_edit(self, el: Element) -> bool: ...
    #: Record a change to an element (its ``updated_seq`` follows).
    def update(self, el: Element, **fields: Any) -> Element: ...
    #: A region (``"c1r2:c3r4"``, ``"x0,y0,x1,y1"``, a list, or an element's bounds) as ``[x0, y0, x1, y1]``.
    def region(self, value: Any, field: str) -> List[int]: ...
    #: ``(x, y, frame)`` for a new element of ``w`` x ``h`` from the op's placement (``at``, ``right_of`` ...).
    def place(self, op: Op, w: float, h: float, asked: Optional[Tuple[float, float]] = None) -> Tuple[float, float, Optional[str]]: ...
    def enclosing_frame(self, box: Sequence[float]) -> Optional[str]: ...
    def geometry(self, points: Sequence[Sequence[float]]) -> Dict[str, int]: ...
    #: A new element, recorded: alias and ``client_id`` come from ``op``; ``store`` maps fields to ``(bytes, kind)`` assets
    #: stored once the element is clear of locks. Then, by the kind: a sized kind makes way for its neighbours and sits
    #: on its host; a container or overlay is recorded as it is; anything else gets the claim and legibility warnings.
    def create(self, kind: str, x: float, y: float, w: float, h: float, *, op: Op, text: str = "", style: Optional[Dict[str, Any]] = None,
               frame: Optional[str] = None, store: Optional[Dict[str, Tuple[bytes, str]]] = None, **fields: Any) -> Element: ...
    #: An arrow's route from ``op``'s ``points`` or ``from``/``to``: ``(points, from id, to id)``; a labelled arrow
    #: between two elements makes room for its label.
    def route(self, op: Op, *, label: str, style: Dict[str, Any], curve: bool) -> Tuple[List[List[float]], Optional[str], Optional[str]]: ...
    #: An ``image`` op's picture: ``(bytes, mime, px_w, px_h)`` from ``path`` or ``asset``.
    def image(self, op: Op) -> Tuple[bytes, str, int, int]: ...
    #: A file under the team's artifacts, as the relative path an element stores.
    def artifact(self, rel: Any) -> str: ...
    def require_viz(self) -> None: ...
    #: A graph or Mermaid flowchart as a frame of native shapes and bound arrows (layout in Python).
    def expand_graph(self, op: Op, title: str, nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]], algorithm: str,
                     direction: str, subgraphs: Sequence[Dict[str, Any]] = ()) -> None: ...
    def graph_nodes(self, raw: List[Any]) -> List[Dict[str, Any]]: ...
    def mentions(self, explicit: Any, text: str) -> List[str]: ...
    def warn(self, code: str, message: str, ids: Sequence[str]) -> None: ...
    #: The comment an op names (``reply_to``), or an ``element_unknown`` refusal.
    def comment(self, ref: Any, field: str) -> Element: ...
    #: A comment that mentions members: posted to them once the batch is applied.
    def mention(self, comment: Element) -> None: ...

    # -- blocks (phase 2) ---------------------------------------------------------------------------------
    #: Run the block pipeline (``canvas_blocks``, 1.5) for a kind's own op: create the block, or reconcile the one the
    #: op's ``id`` names (an upsert). Returns the root.
    def block(self, kind: str, spec: Mapping[str, Any]) -> Element: ...
    #: ``(id, (x0, y0, x1, y1), outline)`` of every solid element meeting ``box``, leaving out ``exclude`` and the
    #: containers of those (an arrow's ends are never its own obstacles).
    def obstacles(self, box: Sequence[float], exclude: Sequence[str]) -> List[Tuple[str, Tuple[float, float, float, float], str]]: ...


def _key(item: Dict[str, Any]) -> str:
    return str(item["id"])


@dataclass(frozen=True)
class Collection:
    """One kind of item a block holds (``cards``, ``rows``, ``events``)."""

    #: Its name in the op and in ``patch`` (``add: {"cards": [...]}``).
    name: str
    #: ``(ctx, raw, field) -> item``: one raw item normalized (a bare string is the short form); refuses with
    #: ``ctx.invalid(field, ...)``, where ``field`` is ``cards[3]``.
    item: Callable[[Any, Any, str], Dict[str, Any]]
    #: Generated ids: ``prefix`` + n (``c`` -> c1, c2 ...), never reused inside one block; "" = the item must carry one.
    prefix: str = ""
    #: The item's key (its ``id``).
    key: Callable[[Dict[str, Any]], str] = _key
    #: A ``remove`` entry -> the key it removes (default ``str(entry)``).
    remove_key: Optional[Callable[[Any], str]] = None
    #: The most items; more is refused ``too_big``.
    maximum: int = 200
    doc: str = ""
    #: The item field an upsert matches an item without an id by (``title``, ``text``): exact text among the members
    #: no id matched (1.8); "" = match by key only.
    label: str = ""
    #: The kind its members are (``card``), so an upsert matches an item only with a member of its own collection.
    member: str = ""
    #: What an item names in other collections, as ``(field, collection, how)``, so removing an item takes care of what
    #: names it (1.7). ``how``: ``drop`` (the item goes with what it names, as an edge with its node; a list field
    #: loses the entry, and the item goes when the list empties), ``clear`` (the field is cleared, and ``normalize``
    #: gives its default: a card whose column goes lands in the first), ``key`` (the field is a mapping keyed by
    #: those items, and loses the key), ``start`` or ``end`` (the field is a 1-based position in that collection,
    #: renumbered; an item whose start passes its end goes).
    refs: Tuple[Tuple[str, str, str], ...] = ()


def _no_normalize(ctx: Any, spec: Dict[str, Any]) -> Dict[str, Any]:
    return spec


def _no_build(bctx: Any, spec: Dict[str, Any]) -> None:
    raise NotImplementedError("a block kind needs build")


def _no_spec(root: Element, members: List[Element], full: bool) -> Dict[str, Any]:
    return {}


@dataclass(frozen=True)
class Block:
    """A composite described by structure (1.3): what it holds and how it is built and read back.

    Round trip (test T-B1): ``normalize(parse(spec(root, members)))`` equals the normalized op that built the block,
    so what an agent reads back is exactly what it could write."""

    collections: Tuple[Collection, ...] = ()
    #: Op fields kept in ``root["settings"]``; ``patch set`` may change them.
    settings: Tuple[str, ...] = ()
    #: ``members`` (items are elements carrying ``group`` and ``part``) or ``inline`` (the root holds and draws them).
    parts: str = "members"
    #: Members keep positions of their own (a move pins them); False: a stack order (a move reorders).
    positional: bool = True
    #: ``(ctx, spec) -> spec``: defaults and checks across items; refusals name the field.
    normalize: Callable[[Any, Dict[str, Any]], Dict[str, Any]] = _no_normalize
    #: ``(bctx, spec)``: create or reconcile the root and its members (``BlockContext``).
    build: Callable[[Any, Dict[str, Any]], None] = _no_build
    #: ``(root, members in part order, full) -> op``: the block read back as the op that would build it.
    spec: Callable[[Element, List[Element], bool], Dict[str, Any]] = _no_spec
    #: ``(root, joining element) -> item id``, or None: the element stays a loose child.
    adopt: Optional[Callable[[Element, Element], Optional[str]]] = None
    #: ``(root, part, text) -> patch body``: an inline part's edit as a patch.
    part_edit: Optional[Callable[[Element, str, str], Dict[str, Any]]] = None
    #: ``(raw op) -> data`` computed outside the canvas lock (a big layout).
    prepare: Optional[Callable[[Dict[str, Any]], Any]] = None
    #: ``(op, io) -> data`` read and computed before the canvas lock is taken (artifact files, a solve): its result is
    #: ``bctx.loaded``, and a ``HerdrTeamError`` it raises refuses the op with that error when ``build`` reads it. For a
    #: ``patch`` of one of its blocks it gets the patch op with ``_spec`` (the block's spec, read without the lock) and
    #: ``_root`` (its root element); for an upsert, its own op with ``_root``.
    load: Optional[Callable[[Mapping[str, Any], FetchIO], Any]] = None
    max_members: int = 400
    #: Fields the op takes that are neither settings nor collections nor common (``title``).
    fields: Tuple[str, ...] = ("title",)


class BlockContext(Protocol):
    """What ``Block.build`` builds through (``canvas_blocks`` implements it)."""

    ctx: OpContext
    op: Mapping[str, Any]
    kind: str
    #: The root as it stands (None while creating).
    root: Optional[Element]
    prepared: Any
    #: ``Block.load``'s result for this op (None when it had none, or could not run before the lock: the block was made
    #: earlier in the same batch); reading it re-raises the refusal ``load`` raised.
    loaded: Any

    #: Store bytes in the content-addressed asset store (``doc``: a JSON document for the page, <= 2 MB; ``glb``: a
    #: binary glTF, <= 16 MB; ``json``: a Vega-Lite spec; ``svg``, ``png``, ``html``): the asset name, written only once
    #: the op applies.
    def store_asset(self, data: bytes, kind: str) -> str: ...
    #: ``Block.load`` run now, under the lock: only for what could not be loaded before it (see ``loaded``).
    def fetch(self, op: Mapping[str, Any]) -> Any: ...

    #: Creates the root on first call (placed from the op), else updates it; ``settings`` are merged, or replace the
    #: stored ones with ``replace_settings`` (a setting the spec no longer has goes: ``patch set {"color": null}``).
    def root_fields(self, *, x: Optional[float] = None, y: Optional[float] = None, w: Optional[float] = None, h: Optional[float] = None,
                    text: Optional[str] = None, style: Optional[Dict[str, Any]] = None, settings: Optional[Dict[str, Any]] = None,
                    replace_settings: bool = False, **fields: Any) -> Element: ...
    #: ``part -> element`` as the block stood before this build.
    def members(self) -> Dict[str, Element]: ...
    #: Create (fitted by its kind, alias ``<root alias>.<part>``) or update in place (keeps id, author, pin and z).
    def member(self, part: str, kind: str, *, text: str = "", style: Optional[Dict[str, Any]] = None, frame: Optional[str] = None,
               minimum: Optional[Tuple[float, float]] = None, **fields: Any) -> Element: ...
    #: An arrow member bound to members ``a`` and ``b``.
    def edge(self, part: str, a: str, b: str, *, label: str = "", style: Optional[Dict[str, Any]] = None, head: str = "arrow",
             tail: str = "none", frame: Optional[str] = None) -> Element: ...
    def drop(self, part: str) -> None: ...
    def keep_only(self, parts: Iterable[str]) -> List[str]: ...
    def new_id(self, prefix: str) -> str: ...
    def set_order(self, container: Element, ids: Sequence[str]) -> None: ...
    def warn(self, code: str, message: str, parts: Sequence[str]) -> None: ...
