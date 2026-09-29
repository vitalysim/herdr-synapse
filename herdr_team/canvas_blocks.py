"""Blocks (canvas v2 phase 2): one pipeline for every element an agent describes by structure, never by pixels.

Contract: ``.local/prd/canvas-v2-phase2.md`` sections 1 and 5. A block kind
(``canvas_kinds.Kind.block``, a ``canvas_kinds.sdk.Block``) says what items it
holds and how it is built and read back; this module runs it:

* ``run`` is the block op (``kanban``, ``table``, ``section`` ...): parse each
  item with its ``Collection``, ``Block.normalize``, then ``Block.build``
  through a ``BlockCtx``, then arrange. An op whose ``id`` names the author's
  block of the same kind reconciles it (an upsert, 1.8).
* ``op_patch``, ``op_place``, ``op_pin`` and ``op_unpin`` are the new core ops
  (5.1); ``canvas.py`` only registers them.
* ``settle`` runs after every op: every container whose members were added,
  removed, resized or moved is arranged again, innermost first (1.5), and the
  containers around it hug and make room (1.6).
* ``block_lines`` and ``look_block`` read a block back as its spec (5.3).

Positions follow three rules. A member of a *positional* block (a timeline, a
graph) that is moved is pinned there (D5); a member of a *stack* (a section
with ``row``/``column``/``grid``, a kanban and its columns) that is moved is
reordered by where its centre lands (``drop_index``); a pin is never moved by
a layout, by growth push-out, or by an agent when a person set it (D6).

Imports ``canvas`` (and the other way round only lazily, through
``canvas._blocks()``): this module is canvas machinery, kept apart so the core
stays readable.
"""
from __future__ import annotations

import difflib
import json
import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

from herdr_team import canvas as C
from herdr_team import canvas_check as _check
from herdr_team import canvas_kinds as _kinds
from herdr_team import canvas_theme as _theme
from herdr_team.canvas_kinds import _zone
from herdr_team.canvas_kinds.sdk import Block, Collection

Element = Dict[str, Any]
Box = Tuple[float, float, float, float]

#: Stack layouts a container may have (anything else is ``free``: members stay where they are and it hugs them).
STACKS = ("row", "column", "grid")
#: How many rounds ``settle`` arranges at most after one op (a container that grew makes its parent arrange again).
MAX_SETTLE_ROUNDS = 24
#: A block spec in ``look`` is cut into lines of at most this many characters, and at most this many lines per block.
LOOK_SPEC_LINES = 12
#: Problems a batch's ``check`` lists (each with its fix).
MAX_CHECK_PROBLEMS = 10
PIN_BY = ("human", "agent")
RELAYOUTS = ("incremental", "full")
ALIGNS = ("start", "center", "end")
#: How close (in units) ``look --full`` names a top-level element's neighbours.
NEIGHBOUR_REACH = 200


# --------------------------------------------------------------------------
# reading elements


def kind_of(el: Optional[Mapping[str, Any]]) -> Optional[_kinds.Kind]:
    return _kinds.kind_of(el) if isinstance(el, Mapping) else None


def settings_of(el: Mapping[str, Any]) -> Dict[str, Any]:
    return el.get("settings") if isinstance(el.get("settings"), dict) else {}


def stack_of(el: Optional[Mapping[str, Any]]) -> Optional[str]:
    """The stack layout of a container (``row``, ``column``, ``grid``), or None."""
    if not isinstance(el, Mapping) or el.get("type") != "frame" or not el.get("block"):
        return None
    layout = settings_of(el).get("layout")
    return layout if layout in STACKS else None


def arranged(el: Optional[Mapping[str, Any]]) -> bool:
    """A container whose members the core arranges (its kind has ``arrange``)."""
    kind = kind_of(el)
    return kind is not None and kind.arrange is not None


def positional(el: Optional[Mapping[str, Any]]) -> bool:
    """A block whose members keep positions of their own (moving one pins it), not a stack order."""
    kind = kind_of(el)
    return bool(kind is not None and kind.block is not None and kind.block.positional and stack_of(el) is None
                and kind.block.parts == "members" and kind.name != "section")


def pin_of(el: Mapping[str, Any]) -> Optional[str]:
    pin = el.get("pin")
    return pin.get("by") if isinstance(pin, dict) and pin.get("by") in PIN_BY else None


def joined_only(el: Optional[Mapping[str, Any]]) -> bool:
    """A container that takes members only on purpose (``in``, ``place in``, a drop): a stack, or a positional block."""
    return stack_of(el) is not None or positional(el)


def box_of(el: Mapping[str, Any]) -> Box:
    return C.bounds(el)  # type: ignore[arg-type]


def _union(boxes: Iterable[Sequence[float]]) -> Optional[Box]:
    found = list(boxes)
    if not found:
        return None
    return min(b[0] for b in found), min(b[1] for b in found), max(b[2] for b in found), max(b[3] for b in found)


def _drawn(el: Mapping[str, Any]) -> Box:
    kind = kind_of(el)
    if kind is not None and kind.bounds is not None:
        try:
            found = kind.bounds(dict(el))
            return float(found[0]), float(found[1]), float(found[2]), float(found[3])
        except (TypeError, ValueError, KeyError, IndexError):
            pass
    return box_of(el)


def _is_root(el: Optional[Mapping[str, Any]]) -> bool:
    kind = kind_of(el)
    return kind is not None and kind.block is not None


def root_of(ctx: Any, el: Mapping[str, Any]) -> Optional[Element]:
    """The block an element is a member of (its ``group``, when that is a block root)."""
    group = el.get("group")
    if not isinstance(group, str):
        return None
    root = ctx.el(group)
    return root if _is_root(root) else None


def _xywh(box: Sequence[float]) -> List[float]:
    return [box[0], box[1], box[2] - box[0], box[3] - box[1]]


def children_of(ctx: Any, root_id: str) -> List[Element]:
    return [el for el in ctx.live() if el.get("frame") == root_id and el["id"] != root_id and el.get("type") != "comment"]


def members_of(ctx: Any, root_id: str) -> List[Element]:
    """A block's members: its direct children and every element that names it as ``group``."""
    return [el for el in ctx.live() if el["id"] != root_id and (el.get("group") == root_id or el.get("frame") == root_id)
            and el.get("type") != "comment"]


def by_part(members: Iterable[Element], root_id: str) -> Dict[str, Element]:
    return {str(el["part"]): el for el in members if el.get("group") == root_id and isinstance(el.get("part"), str)}


def _flow(el: Mapping[str, Any]) -> bool:
    """Whether an element takes a place in a stack (connectors and overlays do not)."""
    kind = kind_of(el)
    return kind is None or kind.role not in ("connector", "overlay")


def _axis_key(layout: str, el: Mapping[str, Any]) -> Tuple[float, float]:
    x0, y0, x1, y1 = box_of(el)
    cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
    return (cx, cy) if layout == "row" else (cy, cx)


def stack_order(root: Mapping[str, Any], children: Sequence[Element]) -> List[str]:
    """The ids of a stack's children in layout order: ``root["order"]``, then any missing, by position (1.1)."""
    layout = stack_of(root) or "column"
    ids = {el["id"] for el in children if _flow(el)}
    order = [str(i) for i in root.get("order") or [] if i in ids]
    seen = set(order)
    rest = sorted((el for el in children if el["id"] in ids and el["id"] not in seen),
                  key=lambda el: (_axis_key(layout, el), C._id_number(el["id"])))
    return order + [el["id"] for el in rest]


def drop_index(layout: str, centre: Sequence[float], siblings: Sequence[Sequence[float]]) -> int:
    """Where a member dropped with its centre at ``centre`` goes among ``siblings`` (their ``[x, y, w, h]`` boxes, in
    stack order, the dropped one left out): the number of siblings before it (phase 2, 1.5; the page draws its drop line
    by the same rule, ``tests/fixtures/display/stack-drop-vectors.json``).

    ``row``: a sibling is before when its centre is left of the drop point; ``column``: above it; ``grid`` (row-major): its
    bottom is above the point, or it spans the point's height and its centre is left of it."""
    cx, cy = float(centre[0]), float(centre[1])
    count = 0
    for box in siblings:
        x, y, w, h = (float(v) for v in box)
        scx, scy = x + w / 2.0, y + h / 2.0
        if layout == "row":
            before = scx < cx
        elif layout == "column":
            before = scy < cy
        else:
            before = y + h <= cy or (y <= cy < y + h and scx < cx)
        count += 1 if before else 0
    return count


# --------------------------------------------------------------------------
# errors and small readers


def _nearest(key: str, keys: Sequence[str]) -> str:
    found = difflib.get_close_matches(str(key), [str(k) for k in keys], n=3, cutoff=0.3)
    return ", ".join(found) if found else ", ".join(str(k) for k in list(keys)[:5])


def part_unknown(field: str, key: Any, keys: Sequence[str]) -> Exception:
    return C._error("part_unknown", "{} names no item: {!r}; the nearest are {}".format(field, key, _nearest(str(key), keys) or "none"),
                    field=field, ref=key, nearest=difflib.get_close_matches(str(key), [str(k) for k in keys], n=3, cutoff=0.3))


def _ctx_of(kctx: Any) -> Any:
    return getattr(kctx, "_ctx", kctx)


def _refs(value: Any, field: str) -> List[Any]:
    if value is None:
        return []
    if isinstance(value, (str, int)):
        return [value]
    if not isinstance(value, list) or not value:
        raise C._invalid(field, "{} is an id or a non-empty list of ids".format(field))
    return list(value)


# --------------------------------------------------------------------------
# parse and normalize (the same path for create, upsert, patch and readback)


def parse(kctx: Any, kind: _kinds.Kind, op: Mapping[str, Any]) -> Dict[str, Any]:
    """An op as a spec: its alias, its fields and settings as given, each collection's items through ``Collection.item``."""
    block: Block = kind.block
    spec: Dict[str, Any] = {"op": kind.name}
    if isinstance(op.get("id"), str):
        spec["id"] = op["id"]
    for name in tuple(block.fields) + tuple(block.settings):
        if op.get(name) is not None:
            spec[name] = op[name]
    for coll in block.collections:
        raw = op.get(coll.name)
        if raw is None:
            continue
        if not isinstance(raw, list):
            raise C._invalid(coll.name, "{} is a list".format(coll.name))
        if len(raw) > coll.maximum:
            raise C._too_big(coll.name, "MAX_" + coll.name.upper(), coll.maximum,
                             "{} {}; the limit is {}".format(len(raw), coll.name, coll.maximum))
        spec[coll.name] = [coll.item(kctx, item, "{}[{}]".format(coll.name, index)) for index, item in enumerate(raw)]
    return spec


def assign_ids(kind: _kinds.Kind, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Items without an id get ``prefix`` + n in order, skipping ids given explicitly (marked ``_gen``: an upsert matches
    them by text). Deterministic, so an op and its readback name the same items."""
    for coll in kind.block.collections:
        items = spec.get(coll.name)
        if not isinstance(items, list) or not coll.prefix:
            continue
        taken = {str(item["id"]) for item in items if item.get("id") and not item.get("_gen")}
        counter = 0
        for item in items:
            if item.get("id") and not item.get("_gen"):
                continue
            counter += 1
            while "{}{}".format(coll.prefix, counter) in taken:
                counter += 1
            item["id"] = "{}{}".format(coll.prefix, counter)
            item["_gen"] = True
            taken.add(item["id"])
    return spec


def generated_ids(prefix: str, ids: Sequence[str]) -> List[bool]:
    """For readback: which of ``ids`` (in order) may be left out, because ``assign_ids`` gives exactly that id back to an
    item that names none. A fixed point: every id left out is given back, the others stay explicit."""
    bare = [True] * len(ids)
    while True:
        taken = {ident for ident, left_out in zip(ids, bare) if not left_out}
        counter = 0
        changed = False
        for index, ident in enumerate(ids):
            if not bare[index]:
                continue
            counter += 1
            while "{}{}".format(prefix, counter) in taken:
                counter += 1
            if "{}{}".format(prefix, counter) != ident:
                bare[index] = False
                changed = True
                break
        if not changed:
            return bare


def normalized(kctx: Any, kind: _kinds.Kind, op: Mapping[str, Any]) -> Dict[str, Any]:
    """``parse``, ``Block.normalize``, then ids: the one spec a create, an upsert, a patch and a readback compare as."""
    spec = kind.block.normalize(kctx, parse(kctx, kind, op))
    spec = assign_ids(kind, spec)
    for coll in kind.block.collections:
        keys = [coll.key(item) for item in spec.get(coll.name) or []]
        dup = next((k for i, k in enumerate(keys) if k in keys[:i]), None)
        if dup is not None:
            raise C._invalid("{}[{}].id".format(coll.name, keys.index(dup, keys.index(dup) + 1)), "{} id {} appears twice".format(coll.name, dup))
    return spec


def comparable(spec: Mapping[str, Any]) -> Dict[str, Any]:
    """A spec without the placement and bookkeeping an op may carry (for the round-trip test)."""
    return {key: value for key, value in spec.items() if key not in ("intent", "if_version") and key not in C.PLACE_FIELDS}


# --------------------------------------------------------------------------
# the build context


class BlockCtx:
    """``canvas_kinds.sdk.BlockContext`` over one batch: builds or reconciles a block's root and members."""

    def __init__(self, ctx: Any, kind: _kinds.Kind, op: Mapping[str, Any], root: Optional[Element], prepared: Any = None,
                 loaded: Any = None) -> None:
        self._c = ctx
        self.ctx = C._KindCtx(ctx)
        self.op = op
        self.kind = kind.name
        self._kind = kind
        self.root = root
        self.prepared = prepared
        self._loaded = loaded
        self.alias: Optional[str] = root.get("alias") if root is not None else None
        self._before = by_part(members_of(ctx, root["id"]), root["id"]) if root is not None else {}
        self._seq: Dict[str, int] = dict(root.get("seq") or {}) if root is not None else {}
        self.kept: Set[str] = set()
        self.created: List[str] = []
        #: The members this build made, by part: an edge finds its new ends here instead of scanning the canvas.
        self._made: Dict[str, Element] = {}
        self.provisional: Optional[Tuple[float, float]] = None

    # loading (phases 3 and 4, 1.2) ----------------------------------------
    @property
    def loaded(self) -> Any:
        """``Block.load``'s result for this op, or None; a refusal it raised is raised here."""
        found = self._loaded
        if isinstance(found, LoadFailure):
            raise found.error
        return found

    def store_asset(self, data: bytes, kind: str) -> str:
        return C._stage_asset(self._c, data, kind)

    def fetch(self, op: Mapping[str, Any]) -> Any:
        load_hook = self._kind.block.load
        if load_hook is None:
            return None
        ctx = self._c
        return load_hook(op, C._FetchIO(ctx.layout, ctx.team, ctx.doc))

    # the root ------------------------------------------------------------
    def root_fields(self, *, x: Optional[float] = None, y: Optional[float] = None, w: Optional[float] = None, h: Optional[float] = None,
                    text: Optional[str] = None, style: Optional[Dict[str, Any]] = None, settings: Optional[Dict[str, Any]] = None,
                    replace_settings: bool = False, **fields: Any) -> Element:
        ctx = self._c
        if self.root is None:
            kind = self._kind
            width = float(w if w is not None else 320)
            height = float(h if h is not None else 200)
            px, py = (x, y) if x is not None and y is not None else (0.0, 0.0)
            stored = {"block": kind.name} if kind.stored_as is not None else {}
            el = ctx.element(kind.stored_as or kind.name, px, py, width, height, text=text or "", style=style or C._default_style(kind.name),
                             alias=self.alias, client_id=C._client_id(dict(self.op)), frame=fields.pop("frame", None), **stored)
            el["settings"] = dict(settings or {})
            el["seq"] = dict(self._seq)
            el.update(fields)
            ctx.put(el)
            ctx.alias = self.alias
            self.root = el
            return el
        update: Dict[str, Any] = dict(fields)
        for key, value in (("x", x), ("y", y), ("w", w), ("h", h)):
            if value is not None:
                update[key] = C._round(value)
        if text is not None:
            update["text"] = text
        if style is not None:
            update["style"] = style
        if settings is not None:
            update["settings"] = dict(settings) if replace_settings else dict(settings_of(self.root), **settings)
        update["seq"] = dict(self._seq)
        self.root = ctx.update(ctx.el(self.root["id"]) or self.root, **update)
        return self.root

    # members --------------------------------------------------------------
    def members(self) -> Dict[str, Element]:
        return dict(self._before)

    def _alias_for(self, part: str) -> Optional[str]:
        if not self.alias or not C._ALIAS_RE.match(part) or part.startswith("e:"):
            return None
        name = "{}.{}".format(self.alias, part)
        if not C._ALIAS_RE.match(name):
            raise C._invalid("id", "{} is too long to name its parts ({}.<part> must stay within 64 characters)".format(self.alias, self.alias))
        return C._alias(self._c, {}, value=name)

    def member(self, part: str, kind: str, *, text: str = "", style: Optional[Dict[str, Any]] = None, frame: Optional[str] = None,
               minimum: Optional[Tuple[float, float]] = None, **fields: Any) -> Element:
        ctx = self._c
        assert self.root is not None, "root_fields first"
        registered = _kinds.get(kind)
        stored_type = registered.stored_as if registered is not None and registered.stored_as else kind
        extra: Dict[str, Any] = {"block": kind} if registered is not None and registered.stored_as else {}
        old = self._before.get(part)
        if old is not None:
            old = ctx.el(old["id"]) or old
        self.kept.add(part)
        container = frame or self.root["id"]
        if old is not None and old.get("type") == stored_type and (not extra or old.get("block") == kind):
            if not C._may_edit(ctx.author, old):
                changed = old.get("text") != text or any(old.get(k) != v for k, v in fields.items())
                if changed:
                    raise C._error("element_not_yours", "{} ({} of {}) is {}'s; ask them to change it".format(
                        old["id"], part, self.alias or self.root["id"], C._who(old.get("author"), None)), id=old["id"], author=old.get("author"))
                return old
            new = dict(old, text=text, style=style or old.get("style"), **fields)
            if extra:
                new.update(extra)
            new["frame"] = container
            if registered is not None and registered.measure is not None:
                base = minimum or _min_of(old, registered)
                fitted = C._fitted(new, base)
                if fitted:
                    new.update(fitted)
            if all(old.get(k) == v for k, v in new.items() if k not in ("updated_seq", "updated_at")) and set(new) == set(old):
                return old
            if pin_of(old) == "human" and not ctx.author.is_human and (new.get("w"), new.get("h")) != (old.get("w"), old.get("h")):
                raise C._error("pin_held", "{} ({} of {}) was placed by the operator; changing it would resize it. Ask them".format(
                    old["id"], part, self.alias or self.root["id"]), id=old["id"])
            return ctx.update(old, **{k: v for k, v in new.items() if old.get(k) != v or k not in old})
        if old is not None:
            self.drop(part)  # the part changed its kind: a new element replaces it
            self.kept.add(part)
        holder = ctx.el(container) or self.root
        x0, y0 = (_zone.content_box(holder) if holder.get("type") == "frame" else box_of(holder))[:2]
        el = ctx.element(stored_type, x0, y0, 1, 1, text=text, style=style or C._default_style(kind), alias=self._alias_for(part),
                         frame=container, group=self.root["id"], **extra)
        el["part"] = part
        el.update(fields)
        if registered is not None and registered.measure is not None:
            fitted = C._fitted(el, minimum or _min_of(el, registered))
            el.update(fitted)
        elif minimum is not None:
            el["w"], el["h"] = C._round(minimum[0]), C._round(minimum[1])
            el["fit"] = {"policy": "hug", "min": [C._round(minimum[0]), C._round(minimum[1])]}
        ctx.put(el)
        self.created.append(el["id"])
        self._made[part] = el
        return el

    def edge(self, part: str, a: str, b: str, *, label: str = "", style: Optional[Dict[str, Any]] = None, head: str = "arrow",
             tail: str = "none", frame: Optional[str] = None) -> Element:
        ctx = self._c
        start, end = self._before.get(a) or self._current(a), self._before.get(b) or self._current(b)
        if start is None or end is None:
            raise C._invalid("edges", "edge {} names a part that is not in the block".format(part))
        start, end = ctx.el(start["id"]) or start, ctx.el(end["id"]) or end
        points = C._route(("element", start), ("element", end))
        geo = C._geometry(points)
        fields = {"from": start["id"], "to": end["id"], "points": points, "head": head, "tail": tail, "curve": False}
        old = self._before.get(part)
        self.kept.add(part)
        if old is not None and old.get("type") == "arrow":
            old = ctx.el(old["id"]) or old
            new = dict(fields, text=label, style=style or old.get("style"), **geo)
            if all(old.get(k) == v for k, v in new.items()):
                return old
            return ctx.update(old, **new)
        el = ctx.element("arrow", geo["x"], geo["y"], geo["w"], geo["h"], text=label, style=style or C._default_style("arrow"),
                         frame=frame or self.root["id"], group=self.root["id"], **fields)  # type: ignore[index]
        el["part"] = part
        ctx.put(el)
        return el

    def _current(self, part: str) -> Optional[Element]:
        if self.root is None:
            return None
        made = self._made.get(part)
        if made is not None:
            found = self._c.el(made["id"])
            if found is not None and found.get("group") == self.root["id"] and found.get("part") == part:
                return found
        return by_part(members_of(self._c, self.root["id"]), self.root["id"]).get(part)

    def drop(self, part: str) -> None:
        ctx = self._c
        el = self._before.get(part) or self._current(part)
        if el is None:
            return
        el = ctx.el(el["id"])
        if el is None:
            return
        if not C._may_edit(ctx.author, el):
            raise C._error("element_not_yours", "{} ({}) is {}'s; ask them to remove it".format(el["id"], part, C._who(el.get("author"), None)),
                           id=el["id"], author=el.get("author"))
        if pin_of(el) == "human" and not ctx.author.is_human:
            raise C._error("pin_held", "{} ({}) was placed by the operator; ask them before removing it".format(el["id"], part), id=el["id"])
        doomed = [el["id"]] + [d for d in C._descendants(ctx, [el["id"]]) if (ctx.el(d) or {}).get("group") == (self.root or {}).get("id")]
        for other in list(ctx.live()):
            if other.get("type") == "arrow" and other.get("group") == (self.root or {}).get("id") and \
                    (other.get("from") in doomed or other.get("to") in doomed) and other["id"] not in doomed:
                doomed.append(other["id"])
        for eid in doomed:
            ctx.drop(eid)
        C._unbind(ctx, ctx.live(), set(doomed))

    def keep_only(self, parts: Iterable[str]) -> List[str]:
        keep = set(parts)
        dropped = [part for part in self._before if part not in keep]
        for part in dropped:
            self.drop(part)
        return dropped

    def new_id(self, prefix: str) -> str:
        self._seq[prefix] = int(self._seq.get(prefix) or 0) + 1
        return "{}{}".format(prefix, self._seq[prefix])

    def seen_id(self, ident: str, prefix: str) -> None:
        """Keep ``seq`` past an id this build used (a generated one, or ``c7`` given explicitly)."""
        if prefix and ident.startswith(prefix) and ident[len(prefix):].isdigit():
            self._seq[prefix] = max(int(self._seq.get(prefix) or 0), int(ident[len(prefix):]))

    def used(self, prefix: str, number: int) -> bool:
        return number <= int(self._seq.get(prefix) or 0)

    def set_order(self, container: Element, ids: Sequence[str]) -> None:
        ctx = self._c
        current = ctx.el(container["id"]) or container
        if list(current.get("order") or []) != list(ids):
            ctx.update(current, order=list(ids))

    def warn(self, code: str, message: str, parts: Sequence[str]) -> None:
        ids = [self._before[p]["id"] for p in parts if p in self._before] or ([self.root["id"]] if self.root else [])
        self._c.warn(code, message, ids)


def _min_of(el: Mapping[str, Any], kind: _kinds.Kind) -> Tuple[float, float]:
    fit = el.get("fit") if isinstance(el.get("fit"), dict) else {}
    found = fit.get("min")
    if isinstance(found, list) and len(found) == 2 and all(C._is_number(v) for v in found):
        return float(found[0]), float(found[1])
    w, h = _theme.size_min(kind.name, (160, 80))
    return float(w), float(h)


# --------------------------------------------------------------------------
# the pipeline


def _existing(ctx: Any, alias: Any) -> Optional[Element]:
    if not isinstance(alias, str):
        return None
    owners = ctx.state.aliases.get(alias) or {}
    eid = owners.get(ctx.author.name)
    el = ctx.el(eid) if eid else None
    if el is None:
        el = next((e for e in ctx.pending.values() if e is not None and e.get("alias") == alias and e.get("author") == ctx.author.name), None)
    return el


def _check_version(ctx: Any, root: Element, op: Mapping[str, Any]) -> None:
    if op.get("if_version") is None:
        return
    expected = int(C._num(op["if_version"], "if_version", 0, 10 ** 12))
    if int(root.get("updated_seq") or 0) != expected:
        raise C._error("canvas_stale", "{} changed since v{} (it is at v{}); look again".format(root["id"], expected, root.get("updated_seq")),
                       id=root["id"], current=root.get("updated_seq"))


def _may_change(ctx: Any, root: Element) -> None:
    if not C._may_edit(ctx.author, root):
        raise C._error("element_not_yours", "{} is {}'s; members change their own elements, the manager any agent's, the operator anything".format(
            root["id"], C._who(root.get("author"), None)), id=root["id"], author=root.get("author"))


def run(ctx: Any, kind_name: str, op: Mapping[str, Any]) -> Element:
    """A block kind's op (1.5): create the block, or reconcile the author's block of the same kind the op's ``id`` names."""
    kind = _kinds.get(kind_name)
    if kind is None or kind.block is None:
        raise C._invalid("op", "{} is not a block kind".format(kind_name))
    kctx = C._KindCtx(ctx)
    existing = _existing(ctx, op.get("id"))
    upsert = existing is not None and kind_of(existing) is kind
    if existing is not None and not upsert:
        C._alias(ctx, dict(op))  # a different kind under that alias: alias_taken, as today
    if upsert:
        assert existing is not None
        _may_change(ctx, existing)
        _check_version(ctx, existing, op)
    spec = normalized(kctx, kind, op)
    # ``relayout`` is a field of a kind's own op where that kind declares it (today: ``graph``), and it means there
    # what it already means on ``patch``: ``full`` drops every seed and every stored route and draws the block again.
    # Re-issuing the whole drawing was the one repair an agent would reach for, and it was byte for byte a no-op: the
    # seeds held every box and legality held every stale route, so the agent on the rejected board re-issued its
    # graph twice and nothing happened.
    mode = C._choice(op.get("relayout"), "relayout", RELAYOUTS, "incremental")
    return build(ctx, kind, spec, op, existing if upsert else None, upsert=upsert, mode=mode)


def build(ctx: Any, kind: _kinds.Kind, spec: Dict[str, Any], op: Mapping[str, Any], root: Optional[Element], upsert: bool = False,
          mode: str = "incremental") -> Element:
    """Build (or reconcile) a block from a normalized spec, arrange it, and record the result entry."""
    bctx = BlockCtx(ctx, kind, op, root, (getattr(ctx, "prepared", None) or {}).get(ctx.index),
                    (getattr(ctx, "loaded", None) or {}).get(ctx.index))
    if root is None:
        bctx.alias = C._alias(ctx, dict(op)) if op.get("id") is not None else None
    before_box = box_of(root) if root is not None else None
    _reconcile_ids(bctx, kind, spec)
    kind.block.build(bctx, spec)
    new_root = bctx.root
    if new_root is None:
        raise C._invalid("op", "{} built nothing".format(kind.name))
    if root is None:
        _place_new(ctx, kind, new_root, op)
    else:
        mark(ctx, new_root["id"], "full" if mode == "full" else "structure")
    settle(ctx, top=new_root["id"])
    final = ctx.el(new_root["id"]) or new_root
    if root is None:
        _after_new(ctx, final, op)
    elif before_box is not None and box_of(final) != before_box:
        _grew(ctx, final, before_box)
    final = ctx.el(new_root["id"]) or final
    note(ctx, final, upsert=upsert)
    return final


def prepare(ops: Sequence[Any]) -> Dict[int, Any]:
    """Each block op's ``Block.prepare`` (a big layout), computed before ``canvas.lock`` is taken, by op index. Only
    well-formed ops are prepared; the op itself reports what is wrong with any other."""
    out: Dict[int, Any] = {}
    for index, op in enumerate(ops):
        name = op.get("op") if isinstance(op, dict) else None
        spec = _kinds.op(name)
        kind = _kinds.get(name) if spec is not None else None
        if kind is None or kind.block is None or kind.block.prepare is None:
            continue
        try:
            out[index] = kind.block.prepare(dict(op))
        except (C.HerdrTeamError,) + C._OP_FAULTS:
            continue
    return out


class LoadFailure:
    """A refusal ``Block.load`` raised before the lock: ``bctx.loaded`` raises it again, so the op is refused with it."""

    def __init__(self, error: Exception) -> None:
        self.error = error


def load(ops: Sequence[Any], io: Any, find: Optional[Any] = None) -> Dict[int, Any]:
    """Each op's ``Block.load`` (files read and computed), before ``canvas.lock`` is taken (phases 3 and 4, 1.2), by op
    index: a block kind's own op, and a ``patch`` of one of its blocks. ``find(ref)`` names the author's element a ref
    means, read without the lock (None when there is none): an upsert's and a patch's load see the block as it stood
    (``_root``, and ``_spec`` for a patch). A refusal is kept as a ``LoadFailure`` for the op to report; a fault in an op
    that is not well formed is left for the op itself to report."""
    out: Dict[int, Any] = {}
    for index, op in enumerate(ops):
        if not isinstance(op, dict):
            continue
        name = op.get("op")
        target: Optional[Element] = None
        kind: Optional[_kinds.Kind] = None
        if name == "patch" and find is not None and op.get("id") is not None:
            target = _found(find, op.get("id"))
            kind = kind_of(target)
            if kind is None or kind.block is None or kind.block.load is None:
                continue
        else:
            spec = _kinds.op(name)
            kind = _kinds.get(name) if spec is not None else None
            if kind is None or kind.block is None or kind.block.load is None:
                continue
            if find is not None and op.get("id") is not None:
                found = _found(find, op.get("id"))
                target = found if kind_of(found) is kind else None
        view: Dict[str, Any] = dict(op)
        if target is not None:
            view["_root"] = target
            if name == "patch":
                try:
                    view["_spec"] = kind.block.spec(target, [], True)
                except (TypeError, ValueError, KeyError, IndexError, AttributeError):
                    continue
        try:
            out[index] = kind.block.load(view, io)
        except C.HerdrTeamError as err:
            out[index] = LoadFailure(err)
        except C._OP_FAULTS:
            continue
    return out


def _found(find: Any, ref: Any) -> Optional[Element]:
    try:
        found = find(ref)
    except (C.HerdrTeamError,) + C._OP_FAULTS:
        return None
    return found if isinstance(found, dict) else None


# --------------------------------------------------------------------------
# placing a new block


def _member_kind(el: Mapping[str, Any]) -> str:
    kind = kind_of(el)
    return kind.name if kind is not None else str(el.get("type"))


def _reconcile_ids(bctx: BlockCtx, kind: _kinds.Kind, spec: Dict[str, Any]) -> None:
    """Upsert matching (1.8): an item with an explicit id is that member; an item whose id was generated matches, by its
    text, a member of its own collection that no id matched, else it is new and takes an id never used in this block.
    ``seq`` moves past every id the block now uses."""
    before = bctx.members()
    inline = kind.block.parts == "inline"
    for coll in kind.block.collections:
        items = spec.get(coll.name)
        if not isinstance(items, list):
            continue
        if inline:
            # An inline block's generated keys are its items' names (a table's c1, r2): kept as they are.
            for item in items:
                item.pop("_gen", None)
                bctx.seen_id(str(item["id"]), coll.prefix)
            continue
        explicit = {str(item["id"]) for item in items if item.get("id") and not item.get("_gen")}
        mine = {part: el for part, el in before.items() if not coll.member or _member_kind(el) == coll.member}
        unmatched = {part: el for part, el in mine.items() if part not in explicit}
        for item in items:
            if not item.get("_gen"):
                continue
            ident = str(item["id"])
            text = str(item.get(coll.label) or "") if coll.label else None
            same = unmatched.get(ident)
            if same is not None and (text is None or str(same.get("text") or "") == text):
                unmatched.pop(ident, None)
                continue
            found = next((part for part, el in unmatched.items() if text is not None and str(el.get("text") or "") == text), None) \
                if bctx.root is not None else None
            if found is not None:
                item["id"] = found
                unmatched.pop(found, None)
                continue
            number = ident[len(coll.prefix):] if coll.prefix and ident.startswith(coll.prefix) else ""
            if bctx.root is not None and (ident in before or (number.isdigit() and bctx.used(coll.prefix, int(number)))):
                while True:
                    candidate = bctx.new_id(coll.prefix)
                    if candidate not in before and candidate not in explicit:
                        break
                item["id"] = candidate
        for item in items:
            item.pop("_gen", None)
            bctx.seen_id(str(item["id"]), coll.prefix)


def _placement(op: Mapping[str, Any]) -> Optional[str]:
    return next((key for key in C.PLACE_KEYS if op.get(key) is not None), None)


def _place_new(ctx: Any, kind: _kinds.Kind, root: Element, op: Mapping[str, Any]) -> None:
    """Where a new block starts: placed from its op at its starting size; its members are built at its content origin, and
    ``settle`` then arranges and hugs it (``_after_new`` corrects for its final size)."""
    w, h = float(root.get("w") or 320), float(root.get("h") or 200)
    placement = {key: op[key] for key in C.PLACE_FIELDS if op.get(key) is not None}
    ids = {root["id"]} | set(C._descendants(ctx, [root["id"]])) | {el["id"] for el in ctx.live() if el.get("group") == root["id"]}
    hidden = {eid: ctx.pending.get(eid) for eid in ids}
    for eid in ids:
        ctx.pending[eid] = None  # the block is not there yet: it never frames itself nor blocks its own spot
    try:
        x, y, frame = C._place(ctx, placement, w, h)
    finally:
        for eid, value in hidden.items():
            ctx.pending[eid] = value
    dx, dy = x - float(root.get("x") or 0), y - float(root.get("y") or 0)
    _shift(ctx, root["id"], dx, dy)
    moved = ctx.el(root["id"]) or root
    if moved.get("frame") != frame:
        moved = ctx.update(moved, frame=frame)
    ctx.block_placed = {"id": root["id"], "key": _placement(op), "asked": (w, h)}
    mark(ctx, root["id"], "full")


def _shift(ctx: Any, root_id: str, dx: float, dy: float) -> None:
    """Move a block (or any element) and everything inside it, as one."""
    if not (dx or dy):
        return
    for eid in [root_id] + C._descendants(ctx, [root_id]):
        el = ctx.el(eid)
        if el is None:
            continue
        ctx.update(el, **C._translated(el, dx, dy, unbind=False))


def _after_new(ctx: Any, root: Element, op: Mapping[str, Any]) -> None:
    """A new block at its final size: anchored by its placement (``left_of``/``above`` keep the side that faces the
    reference; with no placement it goes in a free spot of the author's home), locks checked, claims warned, and it makes
    way like any new mark that grew (R-1)."""
    placed = getattr(ctx, "block_placed", None) or {}
    key = placed.get("key")
    x0, y0, x1, y1 = box_of(root)
    w, h = x1 - x0, y1 - y0
    aw, ah = placed.get("asked") or (w, h)
    if key == "left_of":
        _shift(ctx, root["id"], -(w - aw), 0)
    elif key == "above":
        _shift(ctx, root["id"], 0, -(h - ah))
    elif key is None and root.get("frame") is None and (w, h) != (aw, ah):
        home = ctx.home()
        if home is not None:
            ids = {root["id"]} | set(C._descendants(ctx, [root["id"]]))
            spot = _free_slot_skipping(ctx, (home[0] + C.FRAME_PAD, home[1] + C.FRAME_PAD, home[2] - C.FRAME_PAD, home[3] - C.FRAME_PAD),
                                       w, h, ids)
            _shift(ctx, root["id"], spot[0] - x0, spot[1] - y0)
    root = ctx.el(root["id"]) or root
    everything = [root["id"]] + C._descendants(ctx, [root["id"]])
    C._check_locks(ctx, [box_of(ctx.el(eid) or root) for eid in everything])
    C._warn_claims(ctx, [root["id"]], box_of(root))
    if root.get("frame") is None and key not in ("in", "inside"):
        _make_way(ctx, root)
    elif isinstance(root.get("frame"), str) and not arranged(ctx.el(root["frame"])):
        C._grow_parents(ctx, root)


def _free_slot_skipping(ctx: Any, area: Sequence[float], w: float, h: float, skip: Set[str]) -> Tuple[float, float]:
    """``canvas._free_slot`` as if the elements in ``skip`` (the new block itself) were not there yet."""
    saved = {eid: ctx.pending.get(eid) for eid in skip}
    for eid in skip:
        ctx.pending[eid] = None
    try:
        return C._free_slot(ctx, area, w, h, reserve=[C._portrait_corner(ctx, ctx.home() or area)])
    finally:
        for eid, value in saved.items():
            ctx.pending[eid] = value


def _obstacle_boxes(ctx: Any, skip: Set[str]) -> List[Tuple[str, Box]]:
    """What a new block keeps clear of: solid marks and top-level frames and blocks (not itself, nor what it holds)."""
    out = []
    for el in ctx.live():
        if el["id"] in skip:
            continue
        if el.get("type") in _check.solid_kinds() or (el.get("type") == "frame" and el.get("frame") is None and el.get("role") != C.PORTRAIT_ROLE):
            out.append((str(el["id"]), box_of(el)))
    return out


def _make_way(ctx: Any, root: Element) -> None:
    """A new top-level block that covers a neighbour only because it grew past the size it was placed at moves to the
    nearest free spot beside it (R-1: the new mark makes way, not the old one)."""
    ids = {root["id"]} | set(C._descendants(ctx, [root["id"]]))
    box = box_of(root)
    placed = getattr(ctx, "block_placed", None) or {}
    aw, ah = placed.get("asked") or (box[2] - box[0], box[3] - box[1])
    asked = (box[0], box[1], box[0] + aw, box[1] + ah)
    boxes = _obstacle_boxes(ctx, ids)
    # A block's size is the canvas's doing, never the author's: a new one never means to cover what is already there.
    hit = next(((eid, b) for eid, b in boxes if _check._intersects(box, b, _check.TOUCH)), None)
    del asked
    if hit is None:
        return
    other = ctx.el(hit[0])
    spot = _check.free_spot(root, other, boxes, None, near=(box[0], box[1]), clearance=C.GRID)
    if spot is None:
        spot = _slide(box, boxes)
    moved = (float(spot[0]), float(spot[1]), float(spot[0]) + box[2] - box[0], float(spot[1]) + box[3] - box[1])
    try:
        C._check_locks(ctx, [moved])
    except C.HerdrTeamError:
        return
    _shift(ctx, root["id"], spot[0] - box[0], spot[1] - box[1])
    ctx.warn("moved_to_fit", "{} would cover {} at its full size; it went to {} instead".format(root["id"], hit[0], C.cell_name(spot[0], spot[1])),
             [root["id"], hit[0]])


def _slide(box: Box, boxes: Sequence[Tuple[str, Box]]) -> Tuple[int, int]:
    """Where a block too big for a nearby free spot goes: straight down from where it was put, past whatever it would
    cover, until nothing is in the way (always found: each step passes an obstacle)."""
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    y = y0
    for _step in range(len(boxes) + 1):
        room = (x0 - C.GRID, y - C.GRID, x0 + w + C.GRID, y + h + C.GRID)
        hits = [b for _eid, b in boxes if _check._intersects(room, b)]
        if not hits:
            break
        y = max(b[3] for b in hits) + 2 * C.GRID
    return C._round(x0), int(math.ceil(y / C.GRID) * C.GRID)


def _grew(ctx: Any, root: Element, old: Box) -> None:
    """An existing block changed size: its frame grows, or (top level) it pushes what it now covers (pins honoured)."""
    if isinstance(root.get("frame"), str):
        if not arranged(ctx.el(root["frame"])):
            C._grow_parents(ctx, root)
        return
    ctx.push_warn = True
    try:
        C._push_from(ctx, root, old)
    finally:
        ctx.push_warn = False


# --------------------------------------------------------------------------
# settle: arrange what an op changed (1.5)


def mark(ctx: Any, container_id: Optional[str], reason: str = "structure") -> None:
    """Arrange ``container_id`` again after this op (``full`` ignores seeds; ``hug`` only hugs; else incremental)."""
    if not isinstance(container_id, str):
        return
    dirty = ctx.dirty
    rank = {"hug": 0, "structure": 1, "full": 2}
    if rank.get(reason, 1) >= rank.get(dirty.get(container_id, "hug"), 0) or container_id not in dirty:
        dirty[container_id] = reason


def _detect(ctx: Any) -> None:
    """Mark the containers whose members this op added, removed, reframed, resized or moved (the arrangement's own
    moves are not seen again: they run after this)."""
    if ctx.detected:
        return
    ctx.detected = True
    for eid, new in list(ctx.pending.items()):
        old = ctx.state.elements.get(eid)
        if old is None and new is None:
            continue
        _detect_link(ctx, old, new)
        frames = {el.get("frame") for el in (old, new) if el is not None} | {el.get("group") for el in (old, new) if el is not None}
        for fid in frames:
            if not isinstance(fid, str) or fid == eid:
                continue
            container = ctx.el(fid)
            if not arranged(container):
                continue
            if old is None or new is None or old.get("frame") != new.get("frame") or old.get("group") != new.get("group"):
                mark(ctx, fid, "structure")
            elif (kind_of(new) or kind_of(old)) is not None and (kind_of(new) or kind_of(old)).role == "connector":  # type: ignore[union-attr]
                continue  # a re-routed arrow changes no member's place (QA phase 2, F15)
            elif (old.get("w"), old.get("h")) != (new.get("w"), new.get("h")):
                mark(ctx, fid, "structure")
            elif (old.get("x"), old.get("y")) != (new.get("x"), new.get("y")) and fid == new.get("frame"):
                mark(ctx, fid, "hug" if positional(container) or stack_of(container) is None else "structure")


def _detect_link(ctx: Any, old: Optional[Element], new: Optional[Element]) -> None:
    """A labelled arrow drawn, relabelled, re-bound or removed between two members of one row or column: the stack is
    arranged again, so the gap between them holds the label (``_zone.stack_arrange``)."""
    arrows = [el for el in (old, new) if el is not None and el.get("type") == "arrow"]
    if not arrows:
        return
    if old is not None and new is not None and all(old.get(k) == new.get(k) for k in ("text", "from", "to")):
        return
    for el in arrows:
        start, end = ctx.el(el.get("from")) if isinstance(el.get("from"), str) else None, ctx.el(el.get("to")) if isinstance(el.get("to"), str) else None
        if start is None or end is None or start.get("frame") != end.get("frame") or not isinstance(start.get("frame"), str):
            continue
        container = ctx.el(start["frame"])
        if arranged(container) and stack_of(container) in ("row", "column"):
            mark(ctx, container["id"], "structure")  # type: ignore[index]


def _depth(ctx: Any, eid: str) -> int:
    depth, cursor, seen = 0, ctx.el(eid), set()
    while cursor is not None and isinstance(cursor.get("frame"), str) and cursor["frame"] not in seen:
        seen.add(cursor["frame"])
        depth += 1
        cursor = ctx.el(cursor["frame"])
    return depth


def settle(ctx: Any, top: Optional[str] = None) -> None:
    """Arrange every marked container, innermost first; a container that changed size marks its own container, grows its
    plain frame, or (top level, not the block this op built) pushes its neighbours."""
    _joins(ctx)
    _detect(ctx)
    rounds = 0
    while ctx.dirty and rounds < MAX_SETTLE_ROUNDS * 4:
        rounds += 1
        cid = max(ctx.dirty, key=lambda i: (_depth(ctx, i), -C._id_number(i)))
        reason = ctx.dirty.pop(cid)
        root = ctx.el(cid)
        if root is None or not arranged(root):
            continue
        if not C._may_edit(ctx.author, root) and cid not in ctx.created:
            # Someone else's container keeps its shape: what changed in it waits for its author (D6; QA phase 2, F7).
            ctx.warn("not_yours", "{} is {}'s, so it was not re-arranged around this change".format(
                root.get("alias") or cid, C._who(root.get("author"), None)), [cid])
            continue
        old = box_of(root)
        arrange(ctx, root, reason)
        root = ctx.el(cid) or root
        if box_of(root) == old:
            continue
        parent = ctx.el(root["frame"]) if isinstance(root.get("frame"), str) else None
        if parent is not None and arranged(parent):
            mark(ctx, parent["id"], "structure")
        elif parent is not None:
            C._grow_parents(ctx, root)
        elif cid != top and cid not in ctx.created:
            _grew(ctx, root, old)
    _bump_roots(ctx)


def _joins(ctx: Any) -> None:
    """What this op created with ``in`` joins its container's layout (at ``index`` when it gave one)."""
    found = getattr(ctx, "join", None)
    if not found:
        return
    ctx.join = None
    cid, index = found
    container = ctx.el(cid)
    if container is None:
        return
    first = next((ctx.el(eid) for eid in ctx.created if (ctx.el(eid) or {}).get("frame") == cid), None)
    if first is not None:
        join(ctx, first, container, index)


def _bump_roots(ctx: Any) -> None:
    """Every member change bumps its block's version (the root's ``updated_seq``, D4)."""
    for eid, el in list(ctx.pending.items()):
        for source in (el, ctx.state.elements.get(eid)):
            if source is None or not isinstance(source.get("group"), str) or not isinstance(source.get("part"), str):
                continue
            root = ctx.el(source["group"])
            if _is_root(root) and int(root.get("updated_seq") or 0) != ctx.seq:  # type: ignore[union-attr]
                ctx.update(root)


def _held(ctx: Any, el: Element, positional_block: bool) -> bool:
    """A member the arrangement must leave where it is: not the author's to move, pinned by a person (for an agent), or
    pinned at all inside a positional block."""
    if not C._may_edit(ctx.author, el):
        return True
    by = pin_of(el)
    if by == "human" and not ctx.author.is_human:
        return True
    return positional_block and by is not None


def _stretch(ctx: Any, root: Element, order: Sequence[str]) -> None:
    """``align: stretch``: every member takes the largest natural cross size among them as its minimum and is refitted."""
    layout = stack_of(root)
    horizontal = layout == "row"
    natural: Dict[str, Tuple[float, float]] = {}
    for eid in order:
        el = ctx.el(eid)
        kind = kind_of(el)
        if el is None or kind is None or kind.measure is None or _held(ctx, el, False) or not kind.stretch:
            if el is not None:
                natural[eid] = (float(el.get("w") or 1), float(el.get("h") or 1))
            continue
        fitted = C._fitted(el, _min_of(el, kind))
        natural[eid] = (float(fitted.get("w") or el.get("w") or 1), float(fitted.get("h") or el.get("h") or 1))
    if not natural:
        return
    cross = max((size[1] if horizontal else size[0]) for size in natural.values())
    for eid in order:
        el = ctx.el(eid)
        kind = kind_of(el)
        if el is None or kind is None or kind.measure is None or _held(ctx, el, False) or not kind.stretch:
            continue
        base = _min_of(el, kind)
        minimum = (base[0], cross) if horizontal else (cross, base[1])
        fitted = C._fitted(el, minimum)
        if fitted:
            fitted["fit"]["min"] = [C._round(base[0]), C._round(base[1])]
            if any(el.get(k) != v for k, v in fitted.items()):
                ctx.update(el, **fitted)


def arrange(ctx: Any, root: Element, reason: str) -> None:
    """Run the root's ``arrange`` and apply it (1.5): member boxes (a resized member is refitted over its new size), routes,
    inner frames, then the root's own box (it hugs its members unless the arrangement says where it goes)."""
    kind = kind_of(root)
    if kind is None or kind.arrange is None:
        return
    rid = root["id"]
    layout = stack_of(root)
    pos = positional(root)
    children = children_of(ctx, rid)
    order = stack_order(root, children) if layout else [el["id"] for el in children]
    if layout and settings_of(root).get("align") == "stretch":
        _stretch(ctx, root, order)
        children = children_of(ctx, rid)  # their stretched sizes
    if layout and list(root.get("order") or []) != order:
        root = ctx.update(ctx.el(rid) or root, order=order)
    members = members_of(ctx, rid)
    env = {"members": members, "by_part": by_part(members, rid), "order": order, "tokens": _theme.tokens(), "prepared": None,
           "links": _links(ctx, order) if layout in ("row", "column") else [],
           "incremental": reason != "full", "reason": reason, "content": _zone.content_box(root) if root.get("type") == "frame" else box_of(root),
           "children": children, "obstacles": [], "held": [el["id"] for el in members if _held(ctx, el, pos)]}
    result = kind.arrange(ctx.el(rid) or root, members, env)
    moved: List[str] = []
    for mid, box in sorted(result.boxes.items(), key=lambda item: C._id_number(item[0])):
        el = ctx.el(mid)
        if el is None or _held(ctx, el, pos):
            continue
        x, y, w, h = (float(v) for v in box)
        fields: Dict[str, Any] = {}
        if (C._round(w), C._round(h)) != (el.get("w"), el.get("h")):
            member_kind = kind_of(el)
            if member_kind is not None and member_kind.measure is not None:
                refit = C._fitted(el, (w, h))
                if refit:
                    refit["fit"]["min"] = list((el.get("fit") or {}).get("min") or refit["fit"]["min"])
                    fields.update(refit)
            elif el.get("type") != "frame":
                fields.update(w=max(1, C._round(w)), h=max(1, C._round(h)))
        dx, dy = C._round(x) - float(el.get("x") or 0), C._round(y) - float(el.get("y") or 0)
        if fields:
            el = ctx.update(el, **fields)
        if dx or dy:
            _shift(ctx, mid, dx, dy)
            moved.append(mid)
    for aid, route in result.routes.items():
        arrow = ctx.el(aid)
        if arrow is None or not isinstance(route.get("points"), list):
            continue
        points = [[C._r2(p[0]), C._r2(p[1])] for p in route["points"]]
        fields = {"points": points}
        fields.update(C._geometry(points))
        if isinstance(route.get("label_at"), (list, tuple)) and len(route["label_at"]) == 2:
            fields["label_at"] = [C._r2(route["label_at"][0]), C._r2(route["label_at"][1])]
        if any(arrow.get(k) != v for k, v in fields.items()):
            ctx.update(arrow, **fields)
    for fid, box in result.frames.items():
        frame = ctx.el(fid)
        if frame is None:
            continue
        x, y, w, h = (C._round(v) for v in box)
        if (frame.get("x"), frame.get("y"), frame.get("w"), frame.get("h")) != (x, y, w, h):
            ctx.update(frame, x=x, y=y, w=w, h=h)
    root = ctx.el(rid) or root
    if result.root is not None:
        x, y, w, h = (C._round(v) for v in result.root)
        target = {"x": x, "y": y, "w": max(1, w), "h": max(1, h)}
    else:
        target = hug(ctx, root, keep_corner=layout is not None)
    counted = {}
    if root.get("type") == "frame":
        count = len([el for el in children_of(ctx, rid) if _flow(el)])
        if root.get("count") != count:
            counted["count"] = count
    extra = {k: v for k, v in dict(result.fields).items() if root.get(k) != v}
    if max(float(target["w"]), float(target["h"])) > C.MAX_BLOCK_SIZE and reason != "hug":
        # A layout that comes out bigger than a block may be (QA phase 2, F12 and R4: the limit is higher than one
        # element's, so a 200-node chain drawn to the right lays out).
        raise C._too_big("id", "MAX_BLOCK_SIZE", C.MAX_BLOCK_SIZE, "{} would be {} x {} units laid out; the limit is {}: split it into smaller "
                         "blocks, or try another layout or direction".format(root.get("alias") or rid, target["w"], target["h"], C.MAX_BLOCK_SIZE))
    if any(root.get(k) != v for k, v in target.items()) or counted or extra:
        ctx.update(root, **target, **counted, **extra)
    if moved:
        # A block that routes its own edges routed them (or kept them as still good): only arrows from outside follow.
        own = [el["id"] for el in members if el.get("type") == "arrow" and el.get("group") == rid and result.routes]
        C._reroute_bound(ctx, moved, skip=[m for m in moved if (ctx.el(m) or {}).get("type") == "arrow"] + own)
    for note_text in result.notes:
        ctx.warn("layout_note", "{}: {}".format(rid, note_text), [rid])
    ctx.block_stats.setdefault(rid, {}).update(dict(result.stats))
    ctx.block_moved.setdefault(rid, []).extend(m for m in moved if m not in ctx.block_moved.get(rid, []))


def _links(ctx: Any, order: Sequence[str]) -> List[Dict[str, Any]]:
    """The labelled arrows between two members of a stack: ``{"a", "b", "label": (w, h)}``, so the stack leaves their
    labels room between neighbours (QA phase 2, F5)."""
    inside = set(order)
    out: List[Dict[str, Any]] = []
    for el in ctx.live():
        if el.get("type") != "arrow" or not str(el.get("text") or "").strip() or el.get("from") not in inside or el.get("to") not in inside:
            continue
        size = C._label_size(el)
        if size is not None:
            out.append({"a": el["from"], "b": el["to"], "label": (float(size[0]), float(size[1]))})
    return out


def _snap_up(value: float, minimum: float) -> int:
    value = float(math.ceil(value - 1e-9))
    if value > minimum:
        value = max(minimum, math.ceil(value / C.GRID - 1e-9) * C.GRID)
    return int(max(minimum, value))


def hug(ctx: Any, root: Element, keep_corner: bool = True) -> Dict[str, Any]:
    """The root's box around its members plus its padding and band, never below its ``fit.min`` (1.6). A stack keeps its
    top-left corner; a positional block keeps it too unless a member lies left of or above its content, then it extends."""
    kind = kind_of(root)
    fit = root.get("fit") if isinstance(root.get("fit"), dict) else {}
    minimum = fit.get("min") if isinstance(fit.get("min"), list) and len(fit["min"]) == 2 else None
    min_w, min_h = (float(minimum[0]), float(minimum[1])) if minimum else _theme.size_min(kind.name if kind else "section", (320, 200))
    x0, y0, _x1, _y1 = box_of(root)
    if root.get("type") == "frame":
        pad, band = _zone.padding(root), _zone.band(root)
    else:
        pad, band = 0.0, 0.0
    boxes = [_drawn(el) for el in members_of(ctx, root["id"]) if el.get("frame") == root["id"] or el.get("group") == root["id"]]
    found = _union(boxes)
    if found is None:
        return {"x": C._round(x0), "y": C._round(y0), "w": C._round(min_w), "h": C._round(min_h)}
    ux0, uy0, ux1, uy1 = found
    nx0, ny0 = x0, y0
    if not keep_corner:
        nx0 = min(x0, ux0 - pad)
        ny0 = min(y0, uy0 - band - pad)
    w = _snap_up(max(min_w, ux1 + pad - nx0), min_w)
    h = _snap_up(max(min_h, uy1 + pad - ny0), min_h)
    return {"x": C._round(nx0), "y": C._round(ny0), "w": w, "h": h}


# --------------------------------------------------------------------------
# joining a container (``in``) and leaving one


def join(ctx: Any, el: Element, container: Element, index: Optional[int] = None) -> None:
    """``el`` joins ``container``'s layout (the ``in`` field, ``place in``, a drop): a stack inserts it at ``index`` (else
    by where its centre lies, or last when it is new); a positional block adopts it (``Block.adopt``) or refuses; a free
    section keeps it where it is. The container is arranged after the op."""
    cid = container["id"]
    kind = kind_of(container)
    if not C._may_edit(ctx.author, container):
        # Joining reshapes the container (it re-stacks, grows or adopts): only its author, the manager for an agent's, or
        # the operator may (D6; QA phase 2, F7).
        raise C._error("element_not_yours", "{} is {}'s; adding to it would reshape it. Ask them, or place it beside ({} ...)".format(
            container.get("alias") or cid, C._who(container.get("author"), None), "right_of" if stack_of(container) != "column" else "below"),
            id=cid, author=container.get("author"))
    if not positional(container) and el.get("type") != "frame":
        # A stack block made of its own containers (a kanban's columns) takes an element into one of them, never among
        # them (QA phase 2, F8): a new element into the first, one dropped on the block into the nearest.
        inner = [ctx.el(i) for i in container.get("order") or []]
        inner = [c for c in inner if c is not None and c.get("type") == "frame" and c.get("group") == cid and arranged(c)]
        if inner:
            target = inner[0]
            if el["id"] not in ctx.created:
                x0, y0, x1, y1 = box_of(ctx.el(el["id"]) or el)
                cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0

                def far(c: Element) -> float:
                    bx0, by0, bx1, by1 = box_of(c)
                    return max(bx0 - cx, 0.0, cx - bx1) + max(by0 - cy, 0.0, cy - by1)

                target = min(inner, key=lambda c: (far(c), inner.index(c)))
            ctx.warn("joined_inner", "{} holds its items in {}; {} went into {}".format(
                container.get("alias") or cid, "its " + (kind_of(target).nouns()[1] if kind_of(target) else "containers"), el["id"],
                target.get("alias") or target["id"]), [el["id"]])
            current = ctx.el(el["id"]) or el
            if current.get("frame") != target["id"]:
                current = ctx.update(current, frame=target["id"])
            join(ctx, current, target, index)
            return
    if positional(container) and el.get("group") == cid:
        mark(ctx, cid, "hug")
        return
    if positional(container):
        adopt = kind.block.adopt if kind is not None and kind.block is not None else None
        part = adopt(container, el) if adopt is not None else None
        if part is not None:
            part = _claim_part(ctx, container, part)
        if part is None:
            raise C._error("block_member", "{} does not take a {} as a member; add items with patch ({})".format(
                container.get("alias") or cid, kind_of(el).name if kind_of(el) else el.get("type"), kind.name if kind else "block"),
                id=cid)
        fields = {"frame": cid, "group": cid, "part": part, "pin": {"by": "human" if ctx.author.is_human else "agent", "who": ctx.author.name}}
        converted = getattr(adopt, "convert", None)
        if callable(converted):
            fields.update(converted(ctx.el(cid) or container, ctx.el(el["id"]) or el) or {})
        ctx.update(ctx.el(el["id"]) or el, **fields)
        mark(ctx, cid, "hug")
        return
    current = ctx.el(el["id"]) or el
    if current.get("frame") != cid:
        current = ctx.update(current, frame=cid)
    layout = stack_of(container)
    if layout is not None:
        siblings = [ctx.el(i) for i in stack_order(container, children_of(ctx, cid)) if i != el["id"]]
        siblings = [s for s in siblings if s is not None]
        if index is None:
            if el["id"] in ctx.created:
                position = len(siblings)
            else:
                x0, y0, x1, y1 = box_of(current)
                position = drop_index(layout, ((x0 + x1) / 2.0, (y0 + y1) / 2.0), [_xywh(box_of(s)) for s in siblings])
        else:
            position = max(0, min(len(siblings), int(index)))
        order = [s["id"] for s in siblings]
        order.insert(position, el["id"])
        root = ctx.el(cid) or container
        if list(root.get("order") or []) != order:
            ctx.update(root, order=order)
    _adopt_member(ctx, current, container)
    mark(ctx, cid, "structure")


def _adopt_member(ctx: Any, el: Element, container: Element) -> None:
    """A stack container that is a member of a block (a kanban column) makes what joins it a member of that block too,
    when the block adopts it (a card, or a sticky turned into a card)."""
    block_root = root_of(ctx, container) if isinstance(container.get("group"), str) else None
    if block_root is None or el.get("group") == block_root["id"]:
        return
    kind = kind_of(block_root)
    adopt = kind.block.adopt if kind is not None and kind.block is not None else None
    part = adopt(block_root, el) if adopt is not None else None
    if part is None:
        return
    part = _claim_part(ctx, block_root, part)
    fields: Dict[str, Any] = {"group": block_root["id"], "part": part}
    converted = getattr(adopt, "convert", None)
    if callable(converted):
        fields.update(converted(block_root, el) or {})
    if block_root.get("alias") and C._ALIAS_RE.match("{}.{}".format(block_root["alias"], part)) and not el.get("alias"):
        try:
            fields["alias"] = C._alias(ctx, {}, value="{}.{}".format(block_root["alias"], part))
        except C.HerdrTeamError:
            pass
    ctx.update(ctx.el(el["id"]) or el, **fields)
    mark(ctx, block_root["id"], "structure")


def rehome(ctx: Any, doomed: Sequence[str]) -> None:
    """A block's inner container deleted without its children (a kanban column): the members it holds join the first
    remaining container of the block, as a patch removing it would put them (``clear``); with none left they leave the
    block. So the block never holds a member that no container of it holds (1.7)."""
    gone = set(doomed)
    for eid in doomed:
        el = ctx.el(eid)
        if el is None or el.get("type") != "frame" or not isinstance(el.get("group"), str) or not isinstance(el.get("part"), str):
            continue
        root = ctx.el(el["group"])
        if root is None:
            continue
        orphans = [c for c in children_of(ctx, eid) if c.get("group") == root["id"] and c["id"] not in gone]
        if not orphans:
            continue
        if stack_of(root) is None:
            # A positional block's inner frame (a graph group): what it held stays in the block, one frame out.
            for child in orphans:
                ctx.update(ctx.el(child["id"]) or child, frame=el.get("frame"))
            mark(ctx, root["id"], "structure")
            continue
        home = next((ctx.el(i) for i in root.get("order") or [] if i not in gone and (ctx.el(i) or {}).get("type") == "frame"
                     and arranged(ctx.el(i))), None)
        for child in orphans:
            if home is not None:
                ctx.update(ctx.el(child["id"]) or child, frame=home["id"])
                join(ctx, ctx.el(child["id"]) or child, home, len(children_of(ctx, home["id"])))
            else:
                ctx.update(ctx.el(child["id"]) or child, frame=root.get("frame"))
                leave(ctx, ctx.el(child["id"]) or child, root)


def _claim_part(ctx: Any, root: Element, part: str) -> str:
    """The part an adopted element takes: ``part``, or the next free ``<prefix><n>`` when a member holds it already; the
    root's ``seq`` moves past it, so the next adoption, patch or build never hands the same id out again."""
    root = ctx.el(root["id"]) or root
    taken = set(by_part(members_of(ctx, root["id"]), root["id"]))
    prefix = part.rstrip("0123456789")
    number = part[len(prefix):]
    if not prefix or not number:
        return part
    seq = dict(root.get("seq") or {})
    value = int(number)
    while "{}{}".format(prefix, value) in taken:
        value = max(value, int(seq.get(prefix) or 0)) + 1
    if value > int(seq.get(prefix) or 0):
        seq[prefix] = value
        ctx.update(root, seq=seq)
    return "{}{}".format(prefix, value)


def leave(ctx: Any, el: Element, old_root: Element) -> None:
    """A member dropped outside its block becomes a loose element (its item is removed; the block re-lays out). What it
    holds leaves with it (a column's cards), and the block's arrows bound to any of them become loose arrows between
    the same two elements, so the block never names an item it no longer has (1.7)."""
    rid = old_root["id"]
    going = [el["id"]] + [d for d in C._descendants(ctx, [el["id"]]) if (ctx.el(d) or {}).get("group") == rid]
    # A part's alias (``g.C``) names the block's item: it goes with the item, so the block can take the name again
    # (a later ``patch add`` of the same id; QA phase 2, R5). The element keeps its id.
    prefix = "{}.".format(old_root["alias"]) if old_root.get("alias") else None
    for eid in going:
        current = ctx.el(eid)
        if current is not None and (current.get("group") == rid or current.get("part") is not None):
            fields: Dict[str, Any] = {"group": None, "part": None}
            if prefix is not None and str(current.get("alias") or "").startswith(prefix):
                fields["alias"] = None
            ctx.update(current, **fields)
    gone = set(going)
    for other in list(ctx.live()):
        if other.get("type") == "arrow" and other.get("group") == rid and (other.get("from") in gone or other.get("to") in gone):
            ctx.update(other, group=None, part=None, frame=None)
    mark(ctx, rid, "structure")


# --------------------------------------------------------------------------
# after a move (1.5): pin, reorder, adopt or leave


def after_move(ctx: Any, explicit: Sequence[str], before: Mapping[str, Element]) -> None:
    """The move op's block rules (1.5): a moved member of a positional block is pinned where it went; a member moved in a
    stack is reordered by where its centre landed (or joins the stack it was dropped in); a member dropped outside its
    block leaves it; a free section hugs what moved in it."""
    by = "human" if ctx.author.is_human else "agent"
    for eid in explicit:
        el = ctx.el(eid)
        old = before.get(eid)
        if el is None or old is None:
            continue
        moved = (el.get("x"), el.get("y")) != (old.get("x"), old.get("y"))
        new_frame, old_frame = el.get("frame"), old.get("frame")
        if moved and pin_of(old) and old.get("pin") != {"by": by, "who": ctx.author.name}:
            # A pinned element moved on purpose stays pinned where it went, now by its mover (1.1).
            el = ctx.update(el, pin={"by": by, "who": ctx.author.name})
        old_root = root_of(ctx, old)
        if old_root is not None and new_frame != old_frame:
            inside = {old_root["id"]} | set(C._descendants(ctx, [old_root["id"]]))
            if new_frame not in inside:
                leave(ctx, el, old_root)
                el = ctx.el(eid) or el
        container = ctx.el(new_frame) if isinstance(new_frame, str) else None
        if isinstance(old_frame, str) and old_frame != new_frame and arranged(ctx.el(old_frame)):
            mark(ctx, old_frame, "structure")
        if container is not None and arranged(container):
            if positional(container) and el.get("group") == container["id"]:
                if moved or new_frame != old_frame:
                    ctx.update(el, pin={"by": by, "who": ctx.author.name})
                    mark(ctx, container["id"], "hug")
            elif new_frame != old_frame or stack_of(container) is not None:
                order_before = list((ctx.el(container["id"]) or container).get("order") or [])
                join(ctx, el, container, None)
                order_after = list((ctx.el(container["id"]) or container).get("order") or [])
                if new_frame == old_frame and moved and order_before == order_after and not ctx.author.is_human:
                    # An agent's move inside a stack only reorders; one that changes no order changes nothing (F20).
                    ctx.warn("stack_order", "{} is in {} ({}), which places its members in order: the move changed no order, "
                             "so it went back; use place with in and index, or move it out".format(
                                 eid, container.get("alias") or container["id"], stack_of(container)), [eid])
            elif moved:
                mark(ctx, container["id"], "hug")
            continue
        root = root_of(ctx, el)
        if root is not None and positional(root) and moved:
            # A member of a positional block moved inside one of its inner frames (a graph group): pinned too.
            ctx.update(ctx.el(eid) or el, pin={"by": by, "who": ctx.author.name})
            mark(ctx, root["id"], "hug")


def _reroute_member(ctx: Any, el: Element) -> None:
    C._reroute_bound(ctx, [el["id"]])


# --------------------------------------------------------------------------
# the ops: patch, place, pin, unpin (5.1)


def _root_for(ctx: Any, op: Mapping[str, Any], field: str = "id") -> Element:
    if op.get(field) is None:
        raise C._invalid(field, "{} needs {}: the block (its alias or id)".format(op.get("op"), field))
    el = ctx.lookup(op[field], field)
    if isinstance(el.get("part"), str) and isinstance(el.get("group"), str) and not _is_root(el):
        root = ctx.el(el["group"])
        raise C._invalid(field, "patch the block ({}), not its part".format((root or {}).get("alias") or el["group"]))
    kind = kind_of(el)
    if kind is None or kind.block is None:
        if el.get("type") == "frame" and any(o.get("group") == el["id"] for o in ctx.live()):
            raise C._error("block_legacy", "{} is a graph drawn before 0.22; redraw it with the graph op under the same id".format(el["id"]), id=el["id"])
        raise C._invalid(field, "{} is a {}, not a block".format(el["id"], el.get("type")))
    return el


def current_spec(root: Element, members: List[Element]) -> Dict[str, Any]:
    kind = kind_of(root)
    assert kind is not None and kind.block is not None
    ordered = _ordered_members(root, members)
    return kind.block.spec(root, ordered, True)


def _ordered_members(root: Element, members: List[Element]) -> List[Element]:
    """Members in part order: stack containers by their ``order``, the rest by id."""
    rank: Dict[str, int] = {}
    for container in [root] + [m for m in members if m.get("type") == "frame"]:
        for index, ident in enumerate(container.get("order") or []):
            rank.setdefault(str(ident), index)
    return sorted(members, key=lambda el: (rank.get(el["id"], 10 ** 6), C._id_number(el["id"])))


def op_patch(ctx: Any, op: Dict[str, Any]) -> None:
    """``patch``: add, update, remove or re-set items inside a block, then re-lay it out (1.8)."""
    root = _root_for(ctx, op)
    _may_change(ctx, root)
    _check_version(ctx, root, op)
    kind = kind_of(root)
    assert kind is not None and kind.block is not None
    if not any(op.get(key) is not None for key in ("add", "update", "remove", "set", "relayout")):
        raise C._invalid("add", "patch needs add, update, remove, set or relayout")
    mode = C._choice(op.get("relayout"), "relayout", RELAYOUTS, "incremental")
    kctx = C._KindCtx(ctx)
    spec = normalized(kctx, kind, current_spec(root, members_of(ctx, root["id"])))
    spec = apply_patch(kctx, kind, spec, op, root)
    spec = kind.block.normalize(kctx, spec)
    build(ctx, kind, spec, {"id": root.get("alias"), "op": kind.name}, ctx.el(root["id"]) or root, mode=mode)
    ctx.alias = root.get("alias")


def _collection(kind: _kinds.Kind, name: Any, field: str) -> Collection:
    found = next((c for c in kind.block.collections if c.name == name), None)
    if found is None:
        raise C._invalid(field, "{} holds {}; not {!r}".format(kind.name, ", ".join(c.name for c in kind.block.collections) or "no items", name))
    return found


def _section(value: Any, field: str) -> Dict[str, Any]:
    if not isinstance(value, dict):
        raise C._invalid(field, "{} is {{<collection>: [items]}}".format(field))
    return value


def apply_patch(kctx: Any, kind: _kinds.Kind, spec: Dict[str, Any], op: Mapping[str, Any], root: Element) -> Dict[str, Any]:
    """The spec after a patch's ``remove``, ``update``, ``add`` and ``set`` (in that order); ``part_unknown`` names the
    nearest keys when a key names no item."""
    block: Block = kind.block
    spec = json.loads(json.dumps(spec))
    ctx = _ctx_of(kctx)
    seq = dict(root.get("seq") or {})
    removed: Dict[str, List[Tuple[int, str]]] = {}
    for name, keys in _section(op.get("remove") or {}, "remove").items():
        coll = _collection(kind, name, "remove." + str(name))
        items = spec.get(coll.name) or []
        original = [coll.key(item) for item in items]
        present = list(original)
        for index, raw in enumerate(_refs(keys, "remove.{}".format(name))):
            key = coll.remove_key(raw) if coll.remove_key is not None else str(raw)
            if key not in present:
                raise part_unknown("remove.{}[{}]".format(name, index), raw, present)
            items = [item for item in items if coll.key(item) != key]
            present = [coll.key(item) for item in items]
            removed.setdefault(coll.name, []).append((original.index(key) + 1, key))
        spec[coll.name] = items
    for line in cascade(kind, spec, removed):
        ctx.warn("removed_with", line, [root["id"]])
    for name, changes in _section(op.get("update") or {}, "update").items():
        coll = _collection(kind, name, "update." + str(name))
        items = spec.get(coll.name) or []
        present = [coll.key(item) for item in items]
        if not isinstance(changes, list):
            raise C._invalid("update." + str(name), "update.{} is a list of {{id, <fields>}}".format(name))
        for index, change in enumerate(changes):
            field = "update.{}[{}]".format(name, index)
            if not isinstance(change, dict) or change.get("id") is None:
                raise C._invalid(field, "{} is {{\"id\": ..., <fields>}}".format(field))
            key = str(change["id"])
            if key not in present:
                raise part_unknown(field + ".id", key, present)
            position = present.index(key)
            merged = _merge(items[position], change)
            item = coll.item(kctx, merged, field)
            item["id"] = key
            items[position] = item
        spec[coll.name] = items
    for name, adds in _section(op.get("add") or {}, "add").items():
        coll = _collection(kind, name, "add." + str(name))
        if not isinstance(adds, list):
            raise C._invalid("add." + str(name), "add.{} is a list of items".format(name))
        items = list(spec.get(coll.name) or [])
        if len(items) + len(adds) > coll.maximum:
            raise C._too_big("add." + str(name), "MAX_" + coll.name.upper(), coll.maximum,
                             "{} would hold {} {}; the limit is {}".format(kind.name, len(items) + len(adds), coll.name, coll.maximum))
        for index, raw in enumerate(adds):
            field = "add.{}[{}]".format(name, index)
            where = raw.get("index") if isinstance(raw, dict) else None
            if isinstance(raw, dict) and "index" in raw:
                raw = {k: v for k, v in raw.items() if k != "index"}
            item = coll.item(kctx, raw, field)
            if not item.get("id") or item.get("_gen"):
                if not coll.prefix:
                    raise C._invalid(field + ".id", "a {} item needs an id".format(name))
                taken = {coll.key(i) for i in items}
                while True:
                    seq[coll.prefix] = int(seq.get(coll.prefix) or 0) + 1
                    candidate = "{}{}".format(coll.prefix, seq[coll.prefix])
                    if candidate not in taken:
                        break
                item["id"] = candidate
                item.pop("_gen", None)
            elif coll.key(item) in {coll.key(i) for i in items}:
                raise C._invalid(field + ".id", "{} already holds {}".format(name, coll.key(item)))
            position = len(items) if where is None else int(C._num(where, field + ".index", 0, len(items)))
            items.insert(position, item)
        spec[coll.name] = items
    for key, value in _section(op.get("set") or {}, "set").items():
        if key not in block.settings and key not in block.fields:
            raise C._invalid("set." + str(key), "{} sets {}".format(kind.name, ", ".join(tuple(block.fields) + tuple(block.settings))))
        if value is None:
            spec.pop(key, None)
        else:
            spec[key] = value
    ctx.patch_seq = seq
    return spec


def cascade(kind: _kinds.Kind, spec: Dict[str, Any], removed: Mapping[str, Sequence[Tuple[int, str]]]) -> List[str]:
    """What names removed items follows them, by each collection's ``refs`` (1.7), until nothing more goes: ``removed``
    maps a collection to its removed ``(1-based position, key)``. Returns a line per collection that lost items."""
    lines: List[str] = []
    pending = {name: list(found) for name, found in removed.items() if found}
    rounds = 0
    while pending and rounds < 8:
        rounds += 1
        current, pending = pending, {}
        for coll in kind.block.collections:
            items = spec.get(coll.name)
            if not isinstance(items, list) or not coll.refs or not any(target in current for _f, target, _h in coll.refs):
                continue
            kept: List[Dict[str, Any]] = []
            gone: List[Tuple[int, str]] = []
            for position, item in enumerate(items, 1):
                drop = False
                for field, target, how in coll.refs:
                    if target not in current or item.get(field) is None:
                        continue
                    keys = {key for _p, key in current[target]}
                    value = item[field]
                    if how == "drop":
                        if isinstance(value, list):
                            left = [v for v in value if v not in keys]
                            drop = drop or not left
                            item[field] = left
                        elif value in keys:
                            drop = True
                    elif how == "clear" and value in keys:
                        item.pop(field)
                    elif how == "key" and isinstance(value, dict):
                        item[field] = {k: v for k, v in value.items() if k not in keys}
                    elif how in ("start", "end") and isinstance(value, int):
                        spots = [p for p, _key in current[target]]
                        item[field] = value - sum(1 for p in spots if (p < value if how == "start" else p <= value))
                starts = [f for f, t, h in coll.refs if h == "start" and isinstance(item.get(f), int)]
                ends = [f for f, t, h in coll.refs if h == "end" and isinstance(item.get(f), int)]
                if starts and ends and item[starts[0]] > item[ends[0]]:
                    drop = True
                if drop:
                    gone.append((position, coll.key(item)))
                else:
                    kept.append(item)
            if gone:
                spec[coll.name] = kept
                pending[coll.name] = gone
                lines.append("{} {} went with what {} named".format(coll.name, ", ".join(key for _p, key in gone),
                                                                    "it" if len(gone) == 1 else "they"))
    return lines


def _merge(item: Dict[str, Any], change: Mapping[str, Any]) -> Dict[str, Any]:
    out = dict(item)
    out.pop("_gen", None)
    for key, value in change.items():
        if key == "id":
            continue
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = dict(out[key], **value)  # a table row's partial cells
        elif value is None:
            out.pop(key, None)
        else:
            out[key] = value
    return out


def _targets(ctx: Any, op: Mapping[str, Any]) -> List[Element]:
    if op.get("id") is not None and op.get("ids") is not None:
        raise C._invalid("ids", "use id or ids, not both")
    refs = _refs(op.get("ids") if op.get("ids") is not None else op.get("id"), "ids" if op.get("ids") is not None else "id")
    if not refs:
        raise C._invalid("id", "{} needs id (or ids)".format(op.get("op")))
    out: List[Element] = []
    for ref in refs:
        el = ctx.lookup(ref, "ids" if op.get("ids") is not None else "id")
        if all(o["id"] != el["id"] for o in out):
            out.append(el)
    return out


def _check_pins(ctx: Any, elements: Sequence[Element]) -> None:
    """An agent never moves what a person pinned, nor a container that carries one (D6)."""
    if ctx.author.is_human:
        return
    for el in elements:
        if pin_of(el) == "human":
            raise C._error("pin_held", "{} was placed by the operator; ask them before moving it".format(el["id"]), id=el["id"])
    inside = C._descendants(ctx, [el["id"] for el in elements])
    held = next((ctx.el(eid) for eid in inside if pin_of(ctx.el(eid) or {}) == "human"), None)
    if held is not None:
        raise C._error("pin_held", "{} holds {}, which the operator placed; ask them before moving it".format(elements[0]["id"], held["id"]),
                       id=held["id"])


def op_place(ctx: Any, op: Dict[str, Any]) -> None:
    """``place``: several elements move as one group, keeping their offsets, beside a reference, at a point, or into a
    container's layout at an index (5.1)."""
    targets = _targets(ctx, op)
    for el in targets:
        if not C._may_edit(ctx.author, el):
            raise C._error("element_not_yours", "{} is {}'s; members change their own elements, the manager any agent's, the operator anything".format(
                el["id"], C._who(el.get("author"), None)), id=el["id"], author=el.get("author"))
    _check_pins(ctx, targets)
    if op.get("if_version") is not None:
        expected = int(C._num(op["if_version"], "if_version", 0, 10 ** 12))
        for el in targets:
            if int(el.get("updated_seq") or 0) != expected:
                raise C._error("canvas_stale", "{} changed since v{} (it is at v{}); look again".format(el["id"], expected, el.get("updated_seq")),
                               id=el["id"], current=el.get("updated_seq"))
    modes = [key for key in ("right_of", "left_of", "below", "above", "in", "at") if op.get(key) is not None]
    if len(modes) != 1:
        raise C._invalid(modes[1] if modes else "right_of", "place takes exactly one of right_of, left_of, below, above, in, at")
    mode = modes[0]
    if op.get("index") is not None and mode != "in":
        raise C._invalid("index", "index goes with in")
    align = C._choice(op.get("align"), "align", ALIGNS, "start")
    ids = [t["id"] for t in targets]
    moving = ids + [d for d in C._descendants(ctx, ids) if d not in ids]
    group = _union(box_of(ctx.el(i) or {}) for i in ids)
    assert group is not None
    gx0, gy0, gx1, gy1 = group
    gw, gh = gx1 - gx0, gy1 - gy0
    before = {eid: dict(ctx.el(eid) or {}) for eid in ids}
    container: Optional[Element] = None
    if mode == "in":
        container = ctx.lookup(op["in"], "in")
        if container["id"] in moving:
            raise C._invalid("in", "an element cannot go inside itself or its own children")
        if container.get("type") != "frame":
            raise C._invalid("in", "{} is not a container".format(container["id"]))
        x, y = _entry_point(ctx, container, gw, gh)
    elif mode == "at":
        x, y = C._point(op["at"], "at", ctx.lookup)[:2]
    else:
        ref = ctx.lookup(op[mode], mode)
        if ref["id"] in moving:
            raise C._invalid(mode, "an element cannot be placed beside itself")
        rx0, ry0, rx1, ry1 = box_of(ref)
        gap = C._gap(op.get("gap"))
        if mode in ("right_of", "left_of"):
            x = rx1 + gap if mode == "right_of" else rx0 - gap - gw
            y = ry0 if align == "start" else (ry0 + ry1 - gh) / 2.0 if align == "center" else ry1 - gh
        else:
            y = ry1 + gap if mode == "below" else ry0 - gap - gh
            x = rx0 if align == "start" else (rx0 + rx1 - gw) / 2.0 if align == "center" else rx1 - gw
    dx, dy = C._round(x) - gx0, C._round(y) - gy0
    boxes: List[Box] = []
    for eid in moving:
        el = ctx.el(eid)
        if el is None:
            continue
        if eid not in ids and not C._may_edit(ctx.author, el):
            raise C._error("element_not_yours", "moving {} would move {} ({}'s)".format(ids[0], eid, C._who(el.get("author"), None)),
                           id=eid, author=el.get("author"))
        boxes.append(box_of(el))
        fields = C._translated(el, dx, dy, unbind=eid in ids)
        if eid in ids and el.get("nudged"):
            fields["nudged"] = None
        boxes.append(box_of(ctx.update(el, **fields)))
    C._check_locks(ctx, boxes)
    if mode == "in" and container is not None:
        index = None if op.get("index") is None else int(C._num(op["index"], "index", 0, 10 ** 6))
        for offset, eid in enumerate(ids):
            el = ctx.el(eid)
            if el is None:
                continue
            old_root = root_of(ctx, before[eid])
            if old_root is not None and container["id"] not in ({old_root["id"]} | set(C._descendants(ctx, [old_root["id"]]))):
                leave(ctx, el, old_root)
            if isinstance(before[eid].get("frame"), str) and before[eid]["frame"] != container["id"]:
                mark(ctx, before[eid]["frame"], "structure")
            join(ctx, ctx.el(eid) or el, ctx.el(container["id"]) or container, None if index is None else index + offset)
    else:
        after_move(ctx, ids, before)
    C._reroute_bound(ctx, moving, skip=[m for m in moving if (ctx.el(m) or {}).get("type") == "arrow"])
    for eid in ids:
        el = ctx.el(eid)
        if el is not None:
            C._warn_claims(ctx, [eid], box_of(el))
    ctx.block_info = {"placed": ids}


def _entry_point(ctx: Any, container: Element, w: float, h: float) -> Tuple[float, float]:
    """Where an element put ``in`` a container starts: a stack re-stacks it anyway; a free section finds it a free spot."""
    if stack_of(container) is not None or positional(container):
        x0, y0, _x1, _y1 = _zone.content_box(container)
        return x0, y0
    area = _zone.content_box(container) if container.get("block") else C._content_area(container)
    return C._free_slot(ctx, area, w, h, exclude=container["id"], widens=True)


def op_pin(ctx: Any, op: Dict[str, Any]) -> None:
    """``pin``: hold each element where it is (by the author's kind); an agent never turns a person's pin into its own."""
    targets = _targets(ctx, op)
    by = "human" if ctx.author.is_human else "agent"
    pinned = []
    for el in targets:
        if not C._may_edit(ctx.author, el) and not ctx.author.is_human:
            raise C._error("element_not_yours", "{} is {}'s".format(el["id"], C._who(el.get("author"), None)), id=el["id"], author=el.get("author"))
        if pin_of(el) == "human" and by == "agent":
            ctx.warn("pin_held", "{} is already pinned by the operator; it stays their pin".format(el["id"]), [el["id"]])
            continue
        if el.get("pin") != {"by": by, "who": ctx.author.name}:
            ctx.update(el, pin={"by": by, "who": ctx.author.name})
        pinned.append(el["id"])
    ctx.block_info = {"pinned": pinned}


def op_unpin(ctx: Any, op: Dict[str, Any]) -> None:
    """``unpin``: let go of the pins; an agent cannot lift a person's pin. The enclosing positional block re-lays out."""
    targets = _targets(ctx, op)
    mode = C._choice(op.get("relayout"), "relayout", RELAYOUTS, "incremental")
    for el in targets:
        if not C._may_edit(ctx.author, el) and not ctx.author.is_human:
            raise C._error("element_not_yours", "{} is {}'s".format(el["id"], C._who(el.get("author"), None)), id=el["id"], author=el.get("author"))
        if pin_of(el) == "human" and not ctx.author.is_human:
            raise C._error("pin_held", "the operator placed {}; ask them".format(el["id"]), id=el["id"])
    unpinned = []
    for el in targets:
        if el.get("pin") is None:
            continue
        ctx.update(el, pin=None)
        unpinned.append(el["id"])
        root = root_of(ctx, el)
        if root is not None:
            mark(ctx, root["id"], "full" if mode == "full" else "structure")
        elif isinstance(el.get("frame"), str) and arranged(ctx.el(el["frame"])):
            mark(ctx, el["frame"], "structure")
    ctx.block_info = {"unpinned": unpinned}


def edit_part(ctx: Any, el: Element, part: Any, text: Any) -> None:
    """``edit {id, part, text}``: an inline part through ``Block.part_edit`` (a patch), or one of a member's own text fields
    (a card's ``title`` or ``body``), refitted."""
    kind = kind_of(el)
    parts = kind.parts(dict(el)) if kind is not None and kind.parts is not None else []
    names = [str(p.get("part")) for p in parts]
    if not isinstance(part, str) or part not in names:
        raise part_unknown("part", part, names)
    if kind is not None and kind.block is not None and kind.block.part_edit is not None:
        body = kind.block.part_edit(el, part, "" if text is None else text)
        op_patch(ctx, dict(body, op="patch", id=el["id"]))
        return
    edit = next((p.get("edit") for p in parts if p.get("part") == part), None) or {}
    field = edit.get("field")
    if not isinstance(field, str):
        raise C._invalid("part", "{} of {} cannot be edited".format(part, el["id"]))
    value = C._text(text, "text", C.MAX_TEXT_CHARS, "MAX_TEXT_CHARS", one_line=edit.get("wrap") == "line")
    new = dict(el, **{field: value})
    fields: Dict[str, Any] = {field: value}
    if kind is not None and kind.measure is not None:
        fields.update(C._fitted(new, _min_of(el, kind)))
    if pin_of(el) == "human" and not ctx.author.is_human and (fields.get("w", el.get("w")), fields.get("h", el.get("h"))) != (el.get("w"), el.get("h")):
        raise C._error("pin_held", "{} was placed by the operator; changing it would resize it. Ask them".format(el["id"]), id=el["id"])
    C._check_locks(ctx, [box_of(el)])
    edited = ctx.update(el, **fields)
    if box_of(edited) != box_of(el):
        C._reroute_bound(ctx, [el["id"]])
        if not (isinstance(edited.get("frame"), str) and arranged(ctx.el(edited["frame"]))):
            C._grow_parents(ctx, edited)
            C._after_growth(ctx, ctx.el(el["id"]) or edited, box_of(el))


# --------------------------------------------------------------------------
# refit of a root (1.8)


def refit_root(ctx: Any, root: Element) -> None:
    """``refit`` of a block root rebuilds it from its own spec (a table's widths, a sequence's spacing)."""
    kind = kind_of(root)
    if kind is None or kind.block is None:
        return
    kctx = C._KindCtx(ctx)
    spec = normalized(kctx, kind, current_spec(root, members_of(ctx, root["id"])))
    for member in members_of(ctx, root["id"]):
        member_kind = kind_of(member)
        if member_kind is not None and member_kind.measure is not None and C._may_edit(ctx.author, member):
            fitted = C._fitted(member, _min_of(member, member_kind))
            if fitted and any(member.get(k) != v for k, v in fitted.items()):
                ctx.update(member, **fitted)
    build(ctx, kind, spec, {"id": root.get("alias"), "op": kind.name}, ctx.el(root["id"]) or root, mode="incremental")


# --------------------------------------------------------------------------
# results (5.2)


def note(ctx: Any, root: Element, upsert: bool = False) -> None:
    """The op's ``block`` result: the root, its kind, box and version, what moved, the pins, and any layout notes."""
    rid = root["id"]
    kind = kind_of(root)
    members = members_of(ctx, rid)
    settings = settings_of(root)
    info: Dict[str, Any] = {"id": rid, "kind": kind.name if kind else root.get("type"),
                            "box": [root.get("x"), root.get("y"), root.get("w"), root.get("h")], "version": ctx.seq,
                            # What this op placed for the first time did not move (QA phase 2, F19).
                            "moved": [m for m in ctx.block_moved.get(rid, []) if m not in ctx.created],
                            "pins": {m["id"]: pin_of(m) for m in members if pin_of(m)},
                            "upsert": bool(upsert), "notes": [w["message"] for w in ctx.warnings if w.get("code") == "layout_note"]}
    parts = {m["id"]: m["part"] for m in members if pin_of(m) and isinstance(m.get("part"), str)}
    if parts:
        info["pin_parts"] = parts
    for key in ("layout", "direction"):
        if settings.get(key) is not None:
            info[key] = settings[key]
    stats = ctx.block_stats.get(rid) or {}
    if "crossings" in stats:
        info["crossings"] = int(stats["crossings"])
    ctx.block_info = info


def refresh_note(ctx: Any) -> None:
    """After the op settled: a ``pin`` or ``unpin`` entry describes its block as a block op's does, and every block entry
    carries the block's final box, moves and pins (5.2; QA phase 2, F19)."""
    info = getattr(ctx, "block_info", None)
    if not isinstance(info, dict):
        return
    rid = info.get("id")
    if rid is None:
        touched = list(info.get("pinned") or []) + list(info.get("unpinned") or [])
        root = next((r for r in (root_of(ctx, ctx.el(i) or {}) for i in touched) if r is not None), None)
        if root is None:
            return
        extra = dict(info)
        note(ctx, root)
        ctx.block_info.update(extra)
        return
    root = ctx.el(rid)
    if root is None:
        return
    upsert = bool(info.get("upsert"))
    notes = info.get("notes")
    note(ctx, root, upsert=upsert)
    if notes:
        ctx.block_info["notes"] = notes


def batch_geometry(state: Any, touched: Sequence[str], limit: int) -> Tuple[List[Dict[str, Any]], int]:
    """The final boxes of what a batch created, moved or resized (after every arrangement), at most ``limit``."""
    out = []
    for eid in touched:
        el = state.elements.get(eid)
        if el is None or el.get("type") in ("comment",):
            continue
        entry: Dict[str, Any] = {"id": eid, "x": el.get("x"), "y": el.get("y"), "w": el.get("w"), "h": el.get("h")}
        fit = el.get("fit") if isinstance(el.get("fit"), dict) else None
        if fit and fit.get("policy"):
            entry["fit"] = {"policy": fit.get("policy"), "size": fit.get("size"), "lines": len(fit.get("lines") or [])}
            if fit.get("truncated"):
                entry["fit"]["truncated"] = True
        out.append(entry)
    return out[:limit], max(0, len(out) - limit)


def batch_check(state: Any, touched: Sequence[str], reader: Optional[str]) -> Optional[Dict[str, Any]]:
    """``canvas_check`` over the touched elements' bounds plus ``AROUND_MARGIN``: only problems that involve a touched
    element, at most ``MAX_CHECK_PROBLEMS`` (each with its fix), and a count per code."""
    boxes = [box_of(state.elements[eid]) for eid in touched if eid in state.elements]
    found = _union(boxes)
    if found is None:
        return None
    margin = C.AROUND_MARGIN
    region = [C._round(found[0] - margin), C._round(found[1] - margin), C._round(found[2] + margin), C._round(found[3] + margin)]
    near = [el for el in state.elements.values() if C._intersects(box_of(el), region)]
    touched_set = set(touched)
    problems = [p for p in _check.problems(near, reader, region) if p["code"] != "stray" and touched_set.intersection(p["ids"])]
    counts: Dict[str, int] = {code: 0 for code in ("overlap", "arrow_through", "label_truncated")}
    for problem in problems:
        counts[problem["code"]] = counts.get(problem["code"], 0) + 1
    return {"region": region, "counts": counts, "problems": problems[:MAX_CHECK_PROBLEMS]}


# --------------------------------------------------------------------------
# reading a block back (5.3)


def spec_of(ctx_or_state: Any, root: Element, full: bool = False) -> Dict[str, Any]:
    """The block as the op that would build it (members in part order)."""
    kind = kind_of(root)
    if kind is None or kind.block is None:
        return {}
    elements = _elements(ctx_or_state)
    members = [el for el in elements if el["id"] != root["id"] and (el.get("group") == root["id"] or el.get("frame") == root["id"])]
    return kind.block.spec(root, _ordered_members(root, members), full)


def _elements(source: Any) -> List[Element]:
    if hasattr(source, "live"):
        return list(source.live())
    if hasattr(source, "elements") and isinstance(source.elements, dict):
        return list(source.elements.values())
    return [el for el in source if isinstance(el, dict)]


def compact(spec: Mapping[str, Any]) -> str:
    return json.dumps(spec, ensure_ascii=False, separators=(",", ":"))


def spec_lines(spec: Mapping[str, Any], width: int, limit: Optional[int]) -> Tuple[List[str], int]:
    """Compact JSON cut at item boundaries into lines of at most ``width``; ``(lines, items left out)``."""
    text = compact(spec)
    pieces: List[str] = []
    depth, start, in_str, escape = 0, 0, False, False
    for index, ch in enumerate(text):
        if in_str:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        elif ch == "," and depth <= 3:
            pieces.append(text[start:index + 1])
            start = index + 1
    pieces.append(text[start:])
    lines: List[str] = []
    current = ""
    for piece in pieces:
        if current and len(current) + len(piece) > width:
            lines.append(current)
            current = piece
        else:
            current += piece
    if current:
        lines.append(current)
    if limit is not None and len(lines) > limit:
        return lines[:limit], len(lines) - limit
    return lines, 0


def counts_text(root: Element, spec: Mapping[str, Any]) -> str:
    kind = kind_of(root)
    parts = []
    for coll in (kind.block.collections if kind and kind.block else ()):
        items = spec.get(coll.name)
        if isinstance(items, list):
            count = len(items)
            if coll.name == "columns" and any(isinstance(c, dict) and isinstance(c.get("cards"), list) for c in items):
                parts.append("columns {}, cards {}".format(count, sum(len(c.get("cards") or []) for c in items if isinstance(c, dict))))
                continue
            parts.append("{} {}".format(coll.name, count))
    return ", ".join(parts)


def header(root: Element, spec: Mapping[str, Any]) -> str:
    """``E-40 kanban work "Launch work" v57 [c40r20:c86r42 912x436] columns 3, cards 4``."""
    kind = kind_of(root)
    x0, y0, x1, y1 = box_of(root)
    title = str(root.get("text") or "")
    tail = counts_text(root, spec)
    return "{} {}{}{} v{} [{}:{} {}x{}]{}".format(
        root.get("id"), kind.name if kind else root.get("type"), " " + str(root["alias"]) if root.get("alias") else "",
        " " + C._q(title, 80) if title else "", root.get("updated_seq"), C.cell_name(x0, y0), C.cell_name(x1, y1), root.get("w"), root.get("h"),
        " " + tail if tail else "")


def block_lines(state: Any, root: Element, reader: Optional[str], full: bool, limit: Optional[int] = LOOK_SPEC_LINES,
                stills: Optional[Iterable[str]] = None) -> List[str]:
    """A block as ``look`` prints it: its header (with author and intent), its spec in compact JSON lines, its pins, and in
    ``--full`` the ids of its parts. A kind that reads back as its gist (``Kind.gist_first``: a chart, a 3D scene) prints
    its readback line and gist lines instead of the spec (``look --block`` prints the spec)."""
    kind = kind_of(root)
    if kind is not None and kind.gist_first:
        who = " by {}".format(C._who(root.get("author"), reader))
        head = (kind.readback(dict(root), full) if kind.readback is not None else header(root, spec_of(state, root, full))) + who
        if full and root.get("intent") and root.get("intent") != C.HUMAN_INTENT:
            head += " — {}".format(root["intent"])
        if limit == 0:
            return [head]
        return [head] + ["  " + line for line in _kinds.gist_of(root, full, stills=stills)]
    spec = spec_of(state, root, full)
    head = header(root, spec) + " by {}".format(C._who(root.get("author"), reader))
    if full and root.get("intent") and root.get("intent") != C.HUMAN_INTENT:
        head += " — {}".format(root["intent"])
    lines, rest = spec_lines(spec, C.LOOK_LINE_MAX, limit)
    out = [head] + ["  " + line for line in lines]
    if rest:
        out.append("  … +{} items: {} canvas look --block {}".format(rest, C.CLI, root.get("alias") or root["id"]))
    members = [el for el in _elements(state) if el.get("group") == root["id"] and isinstance(el.get("part"), str)]
    pins = ["{}({})".format(el["part"], pin_of(el)) for el in sorted(members, key=lambda e: C._id_number(e["id"])) if pin_of(el)]
    if pins:
        out.append("  pins: " + ", ".join(pins))
    stats = root.get("stats") if isinstance(root.get("stats"), dict) else {}
    if "crossings" in stats:
        out.append("  crossings {}".format(stats["crossings"]))
    if full and members:
        out.append("  ids: " + " ".join(_id_entry(el) for el in sorted(members, key=lambda e: C._id_number(e["id"]))))
    return out


def _id_entry(el: Element) -> str:
    """``part=id``, and for a generated part (a mind map's ``t3``) the text it stands for, so a patch can name the right
    one (QA phase 2, F18): ``t3=E-5 "Audience"``."""
    part, text = str(el["part"]), str(el.get("text") or "").strip()
    entry = "{}={}".format(part, el["id"])
    if text and part.rstrip("0123456789") != part and len(part.rstrip("0123456789")) == 1 and el.get("type") != "arrow":
        entry += " " + C._q(text, 40)
    return entry


def pretty(state: Any, root: Element) -> str:
    """``look --block``: the whole spec, one item per line."""
    spec = spec_of(state, root, True)
    lines = [header(root, spec), "{"]
    keys = list(spec)
    for index, key in enumerate(keys):
        value = spec[key]
        comma = "," if index < len(keys) - 1 else ""
        if isinstance(value, list) and value:
            lines.append("  {}: [".format(json.dumps(key)))
            for n, item in enumerate(value):
                lines.append("    {}{}".format(compact(item) if not isinstance(item, str) else json.dumps(item, ensure_ascii=False),
                                               "," if n < len(value) - 1 else ""))
            lines.append("  ]" + comma)
        else:
            lines.append("  {}: {}{}".format(json.dumps(key), json.dumps(value, ensure_ascii=False), comma))
    lines.append("}")
    return "\n".join(lines)


def relation(state: Any, el: Element, by_id: Mapping[str, Element]) -> str:
    """`` in arch#1/2 (row)`` for a member of a stack, `` part c2 of work`` for a block member."""
    out = ""
    frame = by_id.get(el.get("frame")) if isinstance(el.get("frame"), str) else None
    if frame is not None and frame.get("block"):
        layout = stack_of(frame)
        name = frame.get("alias") or frame["id"]
        if layout is not None:
            order = stack_order(frame, [o for o in by_id.values() if o.get("frame") == frame["id"]])
            if el["id"] in order:
                out += " in {}#{}/{} ({})".format(name, order.index(el["id"]) + 1, len(order), layout)
        else:
            out += " in {} ({})".format(name, "free" if kind_of(frame) and kind_of(frame).name == "section" else kind_of(frame).name if kind_of(frame) else "block")
    root = by_id.get(el.get("group")) if isinstance(el.get("group"), str) else None
    if root is not None and isinstance(el.get("part"), str):
        out += " part {} of {}".format(el["part"], root.get("alias") or root["id"])
    if pin_of(el):
        out += " pinned({})".format(pin_of(el))
    return out


def neighbours(el: Element, elements: Sequence[Element]) -> str:
    """`` · right of E-91, above E-97``: a top-level element's nearest solid neighbour on each side within reach."""
    x0, y0, x1, y1 = box_of(el)
    best: Dict[str, Tuple[float, str]] = {}
    for other in elements:
        if other["id"] == el["id"] or other.get("frame") is not None or other.get("type") not in _check.solid_kinds() and not (
                other.get("type") == "frame" and other.get("frame") is None):
            continue
        ox0, oy0, ox1, oy1 = box_of(other)
        overlap_y = min(y1, oy1) - max(y0, oy0) > 0
        overlap_x = min(x1, ox1) - max(x0, ox0) > 0
        for side, gap, ok in (("right of", x0 - ox1, overlap_y), ("left of", ox0 - x1, overlap_y), ("below", y0 - oy1, overlap_x),
                              ("above", oy0 - y1, overlap_x)):
            if ok and 0 <= gap <= NEIGHBOUR_REACH and (side not in best or gap < best[side][0]):
                best[side] = (gap, str(other.get("alias") or other["id"]))
    if not best:
        return ""
    return " · " + ", ".join("{} {}".format(side, best[side][1]) for side in ("right of", "left of", "below", "above") if side in best)


def member_ids(elements: Iterable[Element]) -> Set[str]:
    """Elements folded into their block's spec in ``look`` (every member of a block root that is present)."""
    elements = list(elements)
    roots = {el["id"] for el in elements if _is_root(el) and kind_of(el).name != "section" and kind_of(el).block.parts in ("members", "inline")}
    return {el["id"] for el in elements if el.get("group") in roots and isinstance(el.get("part"), str)}
