"""Graphs (canvas v2 phase 2, 4.7.1): nodes, groups and edges, laid out by a registered layout and routed by a router.

A graph is a block (``canvas_kinds.sdk.Block``) stored as a frame with
``block: "graph"`` (phase 2, D2): its nodes are real elements of their own
kind (box, ellipse, diamond, note, card, sticky, icon), its groups are plain
frames, and its edges are arrows bound to the nodes, every one carrying
``group`` (the root) and ``part`` (its item id: the node id, the group id, or
``e:<a>-><b>`` for an edge, ``#2`` ... for parallel ones). An agent writes the
structure and never a pixel:

* ``nodes`` ``[{id, text, kind, tone, icon, in, detail}]`` (a bare string is an
  id), ``groups`` ``[{id, title, tone, parent}]``, ``edges`` as shorthand
  (``a -> b: label``, ``a --> b`` dashed, ``a <-> b``, ``a -- b``) or objects;
* ``layout`` (any layout that uses edges, ``flow``/``layered`` are ``layers``,
  plus ``grid``), ``direction``, ``same_rank``, ``order``, ``route`` and ``gap``.

``arrange`` builds a ``canvas_layouts.LayoutRequest`` from the members (pins
held, previous positions as seeds for an incremental re-layout), runs the
layout, then routes every edge with ``canvas_routers.route_many`` along the
layout's hints and ports, around every node and every group that holds
neither end. The graph reads back in ``look`` as the op that would draw it.

A graph drawn before 0.22 (a frame with ``group`` members and no ``block``)
is converted when a ``graph`` op names it: its members keep their ids and pins.
"""
from __future__ import annotations

import re
from dataclasses import replace
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team import canvas_display as D
from herdr_team import canvas_icons, canvas_layout, canvas_layouts, canvas_routers, canvas_theme
from herdr_team.canvas_kinds import Arrangement, Kind, OpSpec, get, kind_of, kinds, outline
from herdr_team.canvas_kinds import _zone, diagram
from herdr_team.canvas_kinds._common import DASHES, HEADS, Element, bounds, quote
from herdr_team.canvas_kinds.sdk import Block, Collection
from herdr_team.canvas_layouts import _budget
from herdr_team.canvas_layouts import _util as _layout_util

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 75

#: A node is never smaller than this (0.22's graph sizing); a note or a diamond keeps its own shape minimum too.
NODE_MIN = (160.0, 60.0)
SHAPE_MINIMUM = ("note", "diamond")
MAX_GROUPS = 30
MAX_DETAIL = 2000
NODE_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}\Z")
#: ``a -> b: label``: the ids are lazy so ``a-->b`` reads as ``a --> b``.
EDGE_RE = re.compile(r"^\s*(\w[\w-]{0,31}?)\s*(<->|-->|->|--)\s*(\w[\w-]{0,31})\s*(?::\s*(.+?))?\s*\Z")
EDGE_GRAMMAR = "an edge is \"a -> b\", \"a --> b\" (dashed), \"a <-> b\", \"a -- b\" (no head), each with an optional \": label\""
#: What each shorthand arrow means beyond a solid line with a head at ``b``.
ARROWS = {"->": {}, "-->": {"style": "dashed"}, "<->": {"tail": "arrow"}, "--": {"head": "none"}}
SETTINGS = ("layout", "direction", "same_rank", "order", "route", "gap")
ROUTES = ("straight", "orthogonal", "curved")
GAPS = ("s", "m", "l")
NODE_FIELDS = ("id", "text", "kind", "tone", "color", "fill", "icon", "in", "detail")
EDGE_FIELDS = ("id", "from", "to", "label", "style", "dash", "head", "tail", "tone", "thick")
GROUP_FIELDS = ("id", "title", "tone", "parent")
#: The smallest graph root (its content still hugs).
ROOT_MIN = (80, 80)


def node_kinds() -> List[str]:
    return [kind.name for kind in kinds() if kind.node]


def layouts() -> Tuple[str, ...]:
    return tuple(canvas_layout.LAYOUTS)


# --------------------------------------------------------------------------
# items


def node_item(ctx: Any, raw: Any, field: str) -> Dict[str, Any]:
    """One node: a bare string is its id (and its text)."""
    if isinstance(raw, str):
        raw = {"id": raw}
    if not isinstance(raw, dict):
        raise ctx.invalid(field, "{} must be an object with id and text".format(field))
    for key in raw:
        if key not in NODE_FIELDS:
            raise ctx.invalid("{}.{}".format(field, key), "a node takes {}".format(", ".join(NODE_FIELDS)))
    nid = raw.get("id")
    if not isinstance(nid, str) or not NODE_ID.match(nid):
        raise ctx.invalid(field + ".id", "a node id is 1 to 32 letters, digits, _ or -")
    out: Dict[str, Any] = {"id": nid, "text": ctx.text(raw, "text", limit="label", one_line=True, value=raw.get("text", nid),
                                                       label=field + ".text") or nid}
    kind = ctx.choice(raw, "kind", node_kinds(), "box", label=field + ".kind")
    if kind != "box":
        out["kind"] = kind
    if raw.get("tone") is not None:
        out["tone"] = ctx.choice(raw, "tone", canvas_theme.TONES, "neutral", label=field + ".tone")
    if raw.get("color") is not None:
        ctx.color(raw["color"])
        out["color"] = raw["color"]
    if "fill" in raw:
        if raw["fill"] is not None:
            ctx.fill(raw["fill"])
        out["fill"] = raw["fill"]
    if raw.get("icon") is not None:
        out["icon"] = _icon(ctx, raw["icon"], field + ".icon")
    if raw.get("in") is not None:
        if not isinstance(raw["in"], str) or not NODE_ID.match(raw["in"]):
            raise ctx.invalid(field + ".in", "in names one of the graph's groups")
        out["in"] = raw["in"]
    if raw.get("detail") is not None:
        detail = ctx.text(raw, "detail", limit="text", label=field + ".detail")
        if len(detail) > MAX_DETAIL:
            raise ctx.too_big(field + ".detail", "MAX_DETAIL_CHARS", MAX_DETAIL, "a detail holds up to {} characters".format(MAX_DETAIL))
        if detail:
            out["detail"] = detail
    return out


def _icon(ctx: Any, value: Any, field: str) -> str:
    found = canvas_icons.resolve(value) if isinstance(value, str) else None
    if found is None:
        raise ctx.error("icon_unknown", "{} is not an icon; try {}".format(value, ", ".join(canvas_icons.suggest(value)) or "canvas icons --search"),
                        field=field, did_you_mean=canvas_icons.suggest(value))
    return found


def group_item(ctx: Any, raw: Any, field: str) -> Dict[str, Any]:
    if isinstance(raw, str):
        raw = {"id": raw}
    if not isinstance(raw, dict):
        raise ctx.invalid(field, "{} must be an object with id and title".format(field))
    for key in raw:
        if key not in GROUP_FIELDS:
            raise ctx.invalid("{}.{}".format(field, key), "a group takes {}".format(", ".join(GROUP_FIELDS)))
    gid = raw.get("id")
    if not isinstance(gid, str) or not NODE_ID.match(gid):
        raise ctx.invalid(field + ".id", "a group id is 1 to 32 letters, digits, _ or -")
    out: Dict[str, Any] = {"id": gid}
    title = ctx.text(raw, "title", limit="label", one_line=True, label=field + ".title")
    if title:
        out["title"] = title
    if raw.get("tone") is not None:
        out["tone"] = ctx.choice(raw, "tone", canvas_theme.TONES, "neutral", label=field + ".tone")
    if raw.get("parent") is not None:
        if not isinstance(raw["parent"], str):
            raise ctx.invalid(field + ".parent", "parent names another group")
        out["parent"] = raw["parent"]
    return out


def parse_edge(text: str) -> Optional[Dict[str, Any]]:
    """``a -> b: label`` as an edge item (without its id), or None when it does not parse."""
    match = EDGE_RE.match(text)
    if match is None:
        return None
    a, arrow, b, label = match.groups()
    out: Dict[str, Any] = {"from": a, "to": b}
    if label and label.strip():
        out["label"] = label.strip()
    out.update(ARROWS[arrow])
    return out


def edge_key(a: str, b: str) -> str:
    return "e:{}->{}".format(a, b)


def edge_item(ctx: Any, raw: Any, field: str) -> Dict[str, Any]:
    """One edge: shorthand, ``[from, to, label]`` (0.21), or an object; its id is ``e:<from>-><to>`` unless it names one."""
    auto = True
    if isinstance(raw, str):
        parsed = parse_edge(raw)
        if parsed is None:
            raise ctx.invalid(field, "{} does not parse: {}".format(raw[:80], EDGE_GRAMMAR))
        raw = parsed
    elif isinstance(raw, (list, tuple)) and len(raw) in (2, 3):
        raw = {"from": raw[0], "to": raw[1], "label": raw[2] if len(raw) == 3 else None}
    if not isinstance(raw, dict):
        raise ctx.invalid(field, "{} must be \"a -> b\" or {{\"from\", \"to\", \"label\"}}".format(field))
    for key in raw:
        if key not in EDGE_FIELDS:
            raise ctx.invalid("{}.{}".format(field, key), "an edge takes {}".format(", ".join(EDGE_FIELDS)))
    for end in ("from", "to"):
        value = raw.get(end)
        if not isinstance(value, str) or not NODE_ID.match(value):
            raise ctx.invalid("{}.{}".format(field, end), "{} is not one of the graph's node ids".format(
                value if isinstance(value, str) else "{}.{}".format(field, end)))
    out: Dict[str, Any] = {"id": edge_key(raw["from"], raw["to"]), "from": raw["from"], "to": raw["to"]}
    if isinstance(raw.get("id"), str) and raw["id"]:
        if not raw["id"].startswith(out["id"]):
            raise ctx.invalid(field + ".id", "an edge id is {} (or {}#2 ... for another edge between them)".format(out["id"], out["id"]))
        out["id"] = raw["id"]
        auto = False
    label = ctx.text(raw, "label", limit="label", one_line=True, label=field + ".label")
    if label:
        out["label"] = label
    dash = raw.get("dash")
    if raw.get("style") is not None:
        style = ctx.choice(raw, "style", DASHES, "solid", label=field + ".style")
    elif isinstance(dash, bool) or dash is None:
        style = "dashed" if dash else "solid"
    else:
        style = ctx.choice(raw, "dash", DASHES, "solid", label=field + ".dash")
    if style != "solid":
        out["style"] = style
    head = ctx.choice(raw, "head", HEADS, "arrow", label=field + ".head")
    if head != "arrow":
        out["head"] = head
    tail = ctx.choice(raw, "tail", HEADS, "none", label=field + ".tail")
    if tail != "none":
        out["tail"] = tail
    if raw.get("tone") is not None:
        out["tone"] = ctx.choice(raw, "tone", canvas_theme.TONES, "neutral", label=field + ".tone")
    if raw.get("thick"):
        out["thick"] = True
    if auto:
        out["_auto"] = True
    return out


def edge_remove_key(entry: Any) -> str:
    """A ``remove`` entry: ``"api -> ok"`` or ``e:api->ok``."""
    if isinstance(entry, str) and not entry.startswith("e:"):
        parsed = parse_edge(entry)
        if parsed is not None:
            return edge_key(parsed["from"], parsed["to"])
    return str(entry)


def shorthand(item: Mapping[str, Any]) -> Optional[str]:
    """An edge item as shorthand, or None when it carries more than shorthand says."""
    if set(item) - {"id", "from", "to", "label", "style", "head", "tail"}:
        return None
    style, head, tail = item.get("style", "solid"), item.get("head", "arrow"), item.get("tail", "none")
    for arrow, meaning in ARROWS.items():
        if (style, head, tail) == (meaning.get("style", "solid"), meaning.get("head", "arrow"), meaning.get("tail", "none")):
            text = "{} {} {}".format(item["from"], arrow, item["to"])
            return text + (": " + item["label"] if item.get("label") else "")
    return None


# --------------------------------------------------------------------------
# normalize


def _pairs(ctx: Any, spec: Dict[str, Any], name: str, known: Sequence[str]) -> Optional[List[List[str]]]:
    value = spec.get(name)
    if value is None:
        return None
    if not isinstance(value, list) or not all(isinstance(entry, list) and len(entry) >= 2 for entry in value):
        raise ctx.invalid(name, "{} is a list of lists of node ids ([[\"a\", \"b\"]])".format(name))
    out: List[List[str]] = []
    for index, entry in enumerate(value):
        kept = [str(m) for m in entry if m in known]
        gone = [str(m) for m in entry if m not in known]
        if gone:
            # A node a patch removed leaves its sets; a name that never was a node is the agent's to hear about.
            _warn(ctx, "{}_pruned".format(name), "{}[{}] names {}, not a node of the graph; left out".format(name, index, ", ".join(gone)))
        if len(kept) >= 2:
            out.append(kept)
    return out


def _warn(ctx: Any, code: str, message: str) -> None:
    try:
        ctx.warn(code, message, [])
    except (AttributeError, TypeError):
        pass  # normalizing without a canvas (a round trip in a test)


def normalize(ctx: Any, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Defaults and checks across items: ids unique, groups nest, every edge and ``in`` names what exists, the limits."""
    title = ctx.text(spec, "title", limit="label", one_line=True)
    if title:
        spec["title"] = title
    else:
        spec.pop("title", None)
    nodes = spec.get("nodes")
    if not isinstance(nodes, list) or not nodes:
        raise ctx.invalid("nodes", "graph needs nodes: [{\"id\": \"a\", \"text\": \"...\"}, ...]")
    if len(nodes) > diagram.MAX_GRAPH_NODES:
        raise ctx.too_big("nodes", "MAX_GRAPH_NODES", diagram.MAX_GRAPH_NODES, "{} nodes; the limit is {}".format(len(nodes), diagram.MAX_GRAPH_NODES))
    edges = spec.get("edges") or []
    if len(edges) > diagram.MAX_GRAPH_EDGES:
        raise ctx.too_big("edges", "MAX_GRAPH_EDGES", diagram.MAX_GRAPH_EDGES, "{} edges; the limit is {}".format(len(edges), diagram.MAX_GRAPH_EDGES))
    groups = spec.get("groups") or []
    if len(groups) > MAX_GROUPS:
        raise ctx.too_big("groups", "MAX_GRAPH_GROUPS", MAX_GROUPS, "{} groups; the limit is {}".format(len(groups), MAX_GROUPS))
    seen: Dict[str, str] = {}
    for index, node in enumerate(nodes):
        if node["id"] in seen:
            raise ctx.invalid("nodes[{}].id".format(index), "node id {} appears twice".format(node["id"]))
        seen[node["id"]] = "node"
    parents: Dict[str, Optional[str]] = {}
    for index, group in enumerate(groups):
        if group["id"] in seen:
            raise ctx.invalid("groups[{}].id".format(index), "id {} is taken (group and node ids are one set)".format(group["id"]))
        seen[group["id"]] = "group"
        parents[group["id"]] = group.get("parent")
    for index, group in enumerate(groups):
        parent = group.get("parent")
        if parent is not None and parents.get(parent, "") == "" and parent not in parents:
            raise ctx.invalid("groups[{}].parent".format(index), "{} is not one of the graph's groups".format(parent))
        cursor, chain = parent, {group["id"]}
        while cursor is not None:
            if cursor in chain:
                raise ctx.invalid("groups[{}].parent".format(index), "groups {} nest in a circle".format(group["id"]))
            chain.add(cursor)
            cursor = parents.get(cursor)
    for index, node in enumerate(nodes):
        if node.get("in") is not None and node["in"] not in parents:
            raise ctx.invalid("nodes[{}].in".format(index), "{} is not one of the graph's groups".format(node["in"]))
    used: Dict[str, int] = {}
    for index, edge in enumerate(edges):
        for end in ("from", "to"):
            if seen.get(edge[end]) != "node":
                raise ctx.invalid("edges[{}].{}".format(index, end), "{} is not one of the graph's node ids".format(edge[end]))
    for edge in edges:
        if not edge.get("_auto"):
            used[edge["id"]] = 1
    for edge in edges:
        if edge.pop("_auto", False):
            base, count = edge_key(edge["from"], edge["to"]), 1
            candidate = base
            while candidate in used:
                count += 1
                candidate = "{}#{}".format(base, count)
            edge["id"] = candidate
            used[candidate] = 1
    if spec.get("edges") is not None:
        spec["edges"] = edges
    if spec.get("layout") is not None:
        spec["layout"] = ctx.choice(spec, "layout", layouts(), "layers")
    if spec.get("direction") is not None:
        spec["direction"] = ctx.choice(spec, "direction", canvas_layouts.DIRECTIONS, "down")
    if spec.get("route") is not None:
        found = canvas_routers.get(spec["route"]) if isinstance(spec["route"], str) else None
        if found is None:
            raise ctx.invalid("route", "route is one of {}".format(" | ".join(ROUTES)))
        spec["route"] = found.name
    if spec.get("gap") is not None:
        gap = spec["gap"]
        if not (isinstance(gap, str) and gap in GAPS) and not (isinstance(gap, (int, float)) and not isinstance(gap, bool) and 0 <= gap <= 400):
            raise ctx.invalid("gap", "gap is s, m, l or a number of units (0 to 400)")
    for name in ("same_rank", "order"):
        found_pairs = _pairs(ctx, spec, name, [n["id"] for n in nodes])
        if found_pairs:
            spec[name] = found_pairs
        else:
            spec.pop(name, None)
    return spec


# --------------------------------------------------------------------------
# build


def _depth(groups: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    parents = {g["id"]: g.get("parent") for g in groups}
    out: Dict[str, int] = {}
    for gid in parents:
        depth, cursor, seen = 0, parents[gid], {gid}
        while cursor is not None and cursor not in seen:
            seen.add(cursor)
            depth += 1
            cursor = parents.get(cursor)
        out[gid] = depth
    return out


def _style_op(op: Mapping[str, Any]) -> Dict[str, Any]:
    """The op's style fields (they colour every node of a new graph)."""
    return {key: op[key] for key in ("tone", "variant", "color", "fill", "font", "size", "width", "dash", "opacity", "rough")
            if op.get(key) is not None}


def node_style(ctx: Any, op: Mapping[str, Any], node: Mapping[str, Any]) -> Dict[str, Any]:
    """A node's style: its kind's default, then the op's style fields, then the node's own tone, colour and fill."""
    kind = node.get("kind") or "box"
    style = ctx.style(_style_op(op), kind)
    own = {key: node[key] for key in ("tone", "color") if node.get(key) is not None}
    if "fill" in node:
        own["fill"] = node["fill"]
    return ctx.style(own, kind, style) if own else style


def edge_style(ctx: Any, op: Mapping[str, Any], edge: Mapping[str, Any], route: str) -> Dict[str, Any]:
    base = _style_op(op)
    base.pop("fill", None)
    base.pop("dash", None)
    if edge.get("tone") is not None:
        base["tone"] = edge["tone"]
    style = dict(ctx.style(base, "arrow"), fill=None, dash=edge.get("style") or "solid", route=route)
    if edge.get("thick"):
        style["width"] = 4
    return style


def node_minimum(kind: str) -> Tuple[float, float]:
    if kind in SHAPE_MINIMUM:
        w, h = canvas_theme.size_min(kind, NODE_MIN)
        return max(NODE_MIN[0], float(w)), max(NODE_MIN[1], float(h))
    return NODE_MIN


def router_of(settings: Mapping[str, Any]) -> str:
    if settings.get("route"):
        return str(settings["route"])
    layout = canvas_layouts.get(settings.get("layout") or "layers")
    return layout.router if layout is not None else "orthogonal"


def build(bctx: Any, spec: Dict[str, Any]) -> None:
    """Root, then groups by depth, then nodes, then edges (as 0.21 made them), each reconciled by its part."""
    ctx = bctx.ctx
    op = bctx.op
    settings = {key: spec[key] for key in SETTINGS if spec.get(key) is not None}
    before = bctx.members()
    fields: Dict[str, Any] = {}
    if bctx.root is None:
        fields["fit"] = {"policy": "hug", "min": list(ROOT_MIN)}
    root = bctx.root_fields(text=spec.get("title") or "", settings=settings, **fields)
    if bctx.root is not None and set((root.get("settings") or {})) - set(settings):
        root = ctx.update(root, settings=dict(settings))
    rid = root["id"]
    groups = spec.get("groups") or []
    depth = _depth(groups)
    frame_of: Dict[str, str] = {}
    for group in sorted(groups, key=lambda g: (depth[g["id"]], groups.index(g))):
        old = before.get(group["id"])
        style = None
        if old is None or (old.get("item") or {}).get("tone") != group.get("tone"):
            style = ctx.style({"tone": group["tone"]}, "frame") if group.get("tone") else ctx.default_style("frame")
        parent = frame_of.get(group.get("parent") or "", rid)
        el = bctx.member(group["id"], "frame", text=group.get("title") or group["id"], style=style, frame=parent, minimum=(40.0, 40.0),
                         item=dict(group))
        frame_of[group["id"]] = el["id"]
    for node in spec["nodes"]:
        kind = node.get("kind") or "box"
        old = before.get(node["id"])
        own = {key: node.get(key) for key in ("kind", "tone", "color", "fill")}
        old_own = {key: (old.get("item") or {}).get(key) for key in ("kind", "tone", "color", "fill")} if old is not None else None
        style = node_style(ctx, op, node) if old is None or own != old_own else None
        extra: Dict[str, Any] = {"icon": node.get("icon"), "detail": node.get("detail"), "item": dict(node)}
        if kind == "card":
            extra["body"] = node.get("detail")
        bctx.member(node["id"], kind, text=node["text"], style=style, frame=frame_of.get(node.get("in") or "", rid),
                    minimum=node_minimum(kind), **extra)
    route = router_of(settings)
    for edge in spec.get("edges") or []:
        old = before.get(edge["id"])
        style = None
        old_item = (old.get("item") or {}) if old is not None else None
        if old is None or any(old_item.get(k) != edge.get(k) for k in ("style", "tone", "thick")) or \
                (old.get("style") or {}).get("route") != route:
            style = edge_style(ctx, op, edge, route)
        el = bctx.edge(edge["id"], edge["from"], edge["to"], label=edge.get("label") or "", style=style, head=edge.get("head") or "arrow",
                       tail=edge.get("tail") or "none", frame=rid)
        keep: Dict[str, Any] = {}
        if el.get("item") != edge:
            keep["item"] = dict(edge)
        if old is not None and old.get("type") == "arrow" and (old.get("points") or []) != (el.get("points") or []):
            # The build draws edges straight; the arrangement keeps a route that is still good, so give it back.
            keep.update({key: old[key] for key in ("points", "x", "y", "w", "h", "label_at") if key in old})
        if keep:
            ctx.update(ctx.el(el["id"]) or el, **keep)
    bctx.keep_only([g["id"] for g in groups] + [n["id"] for n in spec["nodes"]] + [e["id"] for e in spec.get("edges") or []])


# --------------------------------------------------------------------------
# arrange


def _gap(settings: Mapping[str, Any], default: float) -> float:
    value = settings.get("gap")
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return canvas_theme.gap(value, default) if value is not None else default


def _layout_tokens() -> Dict[str, Any]:
    found = canvas_theme.tokens().get("layout")
    return found if isinstance(found, dict) else {}


def roles(root: Element, members: Sequence[Element]) -> Tuple[Dict[str, Element], Dict[str, Element], Dict[str, Element], List[Element]]:
    """``(nodes, groups, edges, loose)``: by part, and the children that are no item (a drop not adopted)."""
    rid = root["id"]
    nodes: Dict[str, Element] = {}
    groups: Dict[str, Element] = {}
    edges: Dict[str, Element] = {}
    loose: List[Element] = []
    for el in members:
        part = el.get("part") if el.get("group") == rid else None
        if not isinstance(part, str):
            if el.get("type") not in ("arrow", "comment"):
                loose.append(el)
            continue
        if el.get("type") == "arrow":
            edges[part] = el
        elif el.get("type") == "frame" and not el.get("block"):
            groups[part] = el
        else:
            nodes[part] = el
    return nodes, groups, edges, loose


def _box(el: Element) -> Tuple[float, float, float, float]:
    return bounds(el)


def arrange(root: Element, members: List[Element], env: Dict[str, Any]) -> Arrangement:
    """Lay the nodes out and route the edges (1.4). A member that only moved (``hug``) keeps everything where it is."""
    settings = _zone.settings_of(root)
    nodes, groups, edges, loose = roles(root, members)
    if not nodes:
        return Arrangement(boxes={})
    tokens = _layout_tokens()
    if env.get("reason") == "hug":
        # A member moved by hand stays where it went (D5): only its groups follow it (QA phase 2, F15).
        return Arrangement(boxes={}, frames=_hugged_groups(nodes, groups, tokens))
    ox, oy = (env.get("content") or _zone.content_box(root))[:2]
    held = set(env.get("held") or ())
    incremental = bool(env.get("incremental", True))
    # The batch arranging now is the latest one to touch the block: a member it created is new (it has no seed yet).
    fresh_seq = max([int(root.get("updated_seq") or 0)] + [int(el.get("updated_seq") or 0) for el in members])
    part_of = {el["id"]: part for part, el in list(nodes.items()) + list(groups.items())}
    group_parent = {part: part_of.get(str(el.get("frame"))) for part, el in groups.items()}
    order_of = {part: index for index, part in enumerate(sorted(nodes, key=lambda p: _num_id(nodes[p]["id"])))}
    layout = canvas_layouts.get(settings.get("layout") or "layers") or canvas_layouts.get("layers")
    assert layout is not None
    lnodes = []
    for part, el in nodes.items():
        x0, y0, x1, y1 = _box(el)
        pinned = el.get("pin") is not None or el["id"] in held
        fresh = int(el.get("created_seq") or 0) == fresh_seq and not pinned
        lnodes.append(canvas_layouts.LNode(
            id=part, w=x1 - x0, h=y1 - y0, order=order_of[part], group=part_of.get(str(el.get("frame"))) if layout.groups else None,
            pin=(x0 - ox, y0 - oy) if pinned else None, pin_by=(el.get("pin") or {}).get("by") if pinned else None,
            seed=(x0 - ox, y0 - oy) if incremental and not fresh and not pinned else None))
    group_pad = tokens.get("group_pad") if isinstance(tokens.get("group_pad"), list) and len(tokens["group_pad"]) == 4 else [20, 40, 20, 20]
    lgroups = [canvas_layouts.LGroup(id=part, parent=group_parent.get(part), pad=tuple(float(v) for v in group_pad))  # type: ignore[arg-type]
               for part in sorted(groups, key=lambda p: _num_id(groups[p]["id"]))]
    ledges = []
    for part, el in sorted(edges.items(), key=lambda item: _num_id(item[1]["id"])):
        a, b = part_of.get(str(el.get("from"))), part_of.get(str(el.get("to")))
        if a is None or b is None or a not in nodes or b not in nodes:
            continue
        label = _label_size(el)
        ledges.append(canvas_layouts.LEdge(id=part, a=a, b=b, directed=(el.get("head") or "arrow") != "none" or (el.get("tail") or "none") != "none",
                                           label=label))
    direction = settings.get("direction") or "down"
    if direction not in layout.directions:
        direction = layout.directions[0]
    request = canvas_layouts.LayoutRequest(
        nodes=tuple(lnodes), edges=tuple(ledges) if layout.edges else (), groups=tuple(lgroups) if layout.groups else (),
        direction=direction, gap=_gap(settings, float(tokens.get("gap", 40))), rank_gap=float(tokens.get("rank_gap", 80)),
        same_rank=tuple(tuple(s) for s in settings.get("same_rank") or ()) if layout.name == "layers" else (),
        order=tuple(tuple(s) for s in settings.get("order") or ()) if layout.name == "layers" else (),
        incremental=incremental and any(n.seed is not None for n in lnodes))
    result = _run(layout.name, request)
    boxes: Dict[str, Tuple[float, float, float, float]] = {}
    world: Dict[str, Tuple[float, float, float, float]] = {}
    for node in lnodes:
        px, py = result.positions[node.id]
        el = nodes[node.id]
        boxes[el["id"]] = (ox + px, oy + py, node.w, node.h)
        world[node.id] = (ox + px, oy + py, ox + px + node.w, oy + py + node.h)
    frames: Dict[str, Tuple[float, float, float, float]] = {}
    group_world: Dict[str, Tuple[float, float, float, float]] = {}
    if layout.groups:
        for part, (x0, y0, x1, y1) in result.groups.items():
            frames[groups[part]["id"]] = (ox + x0, oy + y0, x1 - x0, y1 - y0)
            group_world[part] = (ox + x0, oy + y0, ox + x1, oy + y1)
    else:
        # A layout without groups still draws them around their members.
        member_boxes = {part: world[part] for part in world}
        found = _layout_util.group_boxes(lgroups, {n.id: n.group for n in lnodes}, member_boxes)
        for part, (x0, y0, x1, y1) in found.items():
            frames[groups[part]["id"]] = (x0, y0, x1 - x0, y1 - y0)
            group_world[part] = (x0, y0, x1, y1)
    routes, blocked = route_edges(root, nodes, groups, edges, loose, world, group_world, result, settings, (ox, oy))
    notes = tuple(n for n in result.notes if not n.startswith("pin_ignored"))
    if ROUTE_BUDGET_NOTE in blocked:
        notes += ("route_budget: routing ran out of its budget; the edges it did not reach are drawn straight (check names any that "
                  "cross a node)",)
    notes += tuple("route_blocked {}: no clear route, drawn straight".format(part) for part in blocked if part != ROUTE_BUDGET_NOTE)
    stats = dict(result.stats)
    return Arrangement(boxes=boxes, routes=routes, frames=frames, notes=notes, stats=stats)


def _hugged_groups(nodes: Mapping[str, Element], groups: Mapping[str, Element], tokens: Mapping[str, Any]) -> Dict[str, Tuple[float, float, float, float]]:
    """Every group frame around its members where they stand, plus the group pad (``x, y, w, h``)."""
    if not groups:
        return {}
    part_of = {el["id"]: part for part, el in list(nodes.items()) + list(groups.items())}
    pad = tokens.get("group_pad") if isinstance(tokens.get("group_pad"), list) and len(tokens["group_pad"]) == 4 else [20, 40, 20, 20]
    lgroups = [canvas_layouts.LGroup(id=part, parent=part_of.get(str(el.get("frame"))) if str(el.get("frame")) in part_of else None,
                                     pad=tuple(float(v) for v in pad)) for part, el in groups.items()]  # type: ignore[arg-type]
    found = _layout_util.group_boxes(lgroups, {part: part_of.get(str(el.get("frame"))) for part, el in nodes.items()},
                                     {part: _box(el) for part, el in nodes.items()})
    return {groups[part]["id"]: (x0, y0, x1 - x0, y1 - y0) for part, (x0, y0, x1, y1) in found.items()}


def _run(name: str, request: canvas_layouts.LayoutRequest) -> canvas_layouts.LayoutResult:
    key = _cache_key(name, request)
    found = _PREPARED.get(key)
    if found is not None:
        return found
    return canvas_layouts.run(name, request)


#: Layouts computed outside the canvas lock (``prepare``), by request; ``arrange`` takes one when its request matches.
_PREPARED: Dict[Any, canvas_layouts.LayoutResult] = {}
_PREPARED_MAX = 16


def _cache_key(name: str, request: canvas_layouts.LayoutRequest) -> Any:
    return name, repr(request)


def _num_id(eid: Any) -> int:
    try:
        return int(str(eid).split("-")[-1])
    except ValueError:
        return 0


def _label_size(el: Element) -> Optional[Tuple[float, float]]:
    from herdr_team.canvas_kinds import arrow

    found = arrow.label_size(el)
    return (float(found[0]), float(found[1])) if found is not None else None


def route_edges(root: Element, nodes: Mapping[str, Element], groups: Mapping[str, Element], edges: Mapping[str, Element],
                loose: Sequence[Element], world: Mapping[str, Tuple[float, float, float, float]],
                group_world: Mapping[str, Tuple[float, float, float, float]], result: canvas_layouts.LayoutResult,
                settings: Mapping[str, Any], origin: Tuple[float, float]) -> Tuple[Dict[str, Dict[str, Any]], List[str]]:
    """Every edge routed along the layout's hints and ports, around every node and every group holding neither end; and
    the edges that found no clear route (drawn straight)."""
    from herdr_team.canvas_kinds import arrow

    name = router_of(settings)
    ox, oy = origin
    tokens = _layout_tokens()
    group_of = {part: _chain(str(el.get("frame")), groups) for part, el in nodes.items()}
    part_by_id = {n["id"]: p for p, n in nodes.items()}
    shapes = {p: outline(n) for p, n in nodes.items()}
    node_obstacles = [(p, (nodes[p]["id"], world[p], shapes[p])) for p in world]
    node_ids = {nodes[p]["id"] for p in world}
    loose_obstacles = [(el2["id"], bounds(el2), outline(el2)) for el2 in loose]
    requests = []
    ids = {}
    for part, el in sorted(edges.items(), key=lambda item: _num_id(item[1]["id"])):
        a, b = part_by_id.get(str(el.get("from"))), part_by_id.get(str(el.get("to")))
        if a is None or b is None:
            continue
        route_name = arrow.route_of(el) if (el.get("style") or {}).get("route") else name
        ports = result.ports.get(part) if route_name == "orthogonal" else None
        obstacles = [entry for p, entry in node_obstacles if p != a and p != b]
        inside = set(group_of.get(a, ())) | set(group_of.get(b, ()))
        obstacles += [(groups[g]["id"], box, "rect") for g, box in group_world.items() if g not in inside]
        obstacles += loose_obstacles
        if route_name == "orthogonal" and a != b:
            # A route that still leaves one end, reaches the other and runs through nothing stays as it is.
            points = el.get("points") or []
            if len(points) >= 2:
                xs, ys = [p[0] for p in points], [p[1] for p in points]
                near = [o for o in obstacles if o[1][0] < max(xs) and min(xs) < o[1][2] and o[1][1] < max(ys) and min(ys) < o[1][3]]
                placed_a = dict(nodes[a], x=world[a][0], y=world[a][1], w=world[a][2] - world[a][0], h=world[a][3] - world[a][1])
                placed_b = dict(nodes[b], x=world[b][0], y=world[b][1], w=world[b][2] - world[b][0], h=world[b][3] - world[b][1])
                if arrow._still_good([list(p[:2]) for p in points], placed_a, placed_b, near):
                    continue
        via = tuple((ox + x, oy + y) for x, y in result.hints.get(part, ()))
        if a == b and route_name != "orthogonal":
            x0, y0, x1, y1 = world[a]
            cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
            via = ((x1 + 40, cy), (x1 + 40, y0 - 20), (cx, y0 - 20))
        request = canvas_routers.RouteRequest(
            id=part, a=canvas_routers.End(box=world[a], outline=outline(nodes[a]), id=nodes[a]["id"], side=ports[0] if ports else None),
            b=canvas_routers.End(box=world[b], outline=outline(nodes[b]), id=nodes[b]["id"], side=ports[1] if ports else None),
            via=via, label=_label_size(el) if route_name == "orthogonal" else None, obstacles=tuple(obstacles),
            clearance=float(tokens.get("clearance", 20)), radius=float(tokens.get("elbow_radius", 8)),
            port_spacing=float(tokens.get("port_spacing", 12)))
        requests.append((route_name, request))
        ids[part] = el["id"]
    out: Dict[str, Dict[str, Any]] = {}
    blocked: List[str] = []
    kept = tuple(tuple((float(p[0]), float(p[1])) for p in el.get("points") or []) for part, el in edges.items() if el["id"] not in ids.values())
    requests = [(route_name, replace(request, others=kept)) for route_name, request in requests]
    by_router: Dict[str, List[canvas_routers.RouteRequest]] = {}
    for route_name, request in requests:
        by_router.setdefault(route_name, []).append(request)
    found_all: Dict[str, Any] = {}
    # Every route and detour of the graph shares one budget, so a big graph never holds the canvas lock for long
    # (QA phase 2, R2): past it, what is left is drawn straight and noted, the same way on every machine.
    budget = _budget.Budget(work=ROUTE_WORK + ROUTE_WORK_PER_EDGE * len(requests), seconds=ROUTE_SECONDS)
    with _budget.running(budget):
        for route_name, batch in by_router.items():
            found_all.update(canvas_routers.route_many(route_name, batch))
        # A graph-wide straight or curved route that is drawn through a node is routed around it instead, that edge
        # alone (a force, grid or radial layout draws some edges across nodes, and a tree its cross links; QA phase 2,
        # F9 and R3). An edge that names its own route keeps it. A curved edge keeps its curve: its detour's corners
        # are rounded into it (``curve_friendly``).
        detour = []
        for route_name, request in requests:
            found = found_all.get(request.id)
            if found is None or route_name not in DETOURED or request.a.id == request.b.id or \
                    (edges[request.id].get("style") or {}).get("route") not in (None, name):
                continue
            drawn = drawn_points(found.points, route_name == "curved")
            dx0, dy0 = min(p[0] for p in drawn), min(p[1] for p in drawn)
            dx1, dy1 = max(p[0] for p in drawn), max(p[1] for p in drawn)
            if any(_through(drawn, box) for _id, box, _outline in request.obstacles
                   if box[0] < dx1 and dx0 < box[2] and box[1] < dy1 and dy0 < box[3] and _id in node_ids):
                detour.append(replace(request, label=_label_size(edges[request.id]), via=(), quick=True))
        if detour:
            curved = {request.id for route_name, request in requests if route_name == "curved"}
            for part, found in canvas_routers.route_many("orthogonal", detour).items():
                if found.blocked:
                    continue
                if part in curved:
                    found = replace(found, points=curve_friendly(found.points, float(tokens.get("elbow_radius", 8))), label_at=None)
                found_all[part] = found
    if budget.exhausted is not None:
        blocked.append(ROUTE_BUDGET_NOTE)
    for part, found in found_all.items():
        if found.blocked:
            blocked.append(part)
        entry: Dict[str, Any] = {"points": [[x, y] for x, y in found.points]}
        if found.label_at is not None:
            entry["label_at"] = [found.label_at[0], found.label_at[1]]
        out[ids[part]] = entry
    return out, blocked


#: The routers whose graph-wide routes detour around a node they would be drawn through (``route_edges``).
DETOURED = ("straight", "curved")
#: One graph's routing budget (QA phase 2, R2), in the router's work units (an A* state, a leg's set-up; about 10 us
#: each on the dev Mac), plus per edge, and a wall-clock ceiling. 50 nodes and 80 edges on any layout, and 200 and 400
#: on the default layered one, fit inside it; bigger work (200 nodes on a tree, a grid or rings, whose edges detour)
#: takes what it can and draws the rest straight, noted, instead of holding the canvas lock for seconds.
ROUTE_WORK = 6_000
ROUTE_WORK_PER_EDGE = 100
ROUTE_SECONDS = 0.45
#: The entry ``route_edges`` adds to its blocked list when the budget ran out (``arrange`` notes it once).
ROUTE_BUDGET_NOTE = "*budget"


def drawn_points(points: Sequence[Sequence[float]], curved: bool, per_piece: int = 8) -> List[Tuple[float, float]]:
    """The line a route is drawn along: its points, or for a curved one its curve pieces sampled."""
    pts = [(float(p[0]), float(p[1])) for p in points]
    if not curved or len(pts) < 2:
        return pts
    from herdr_team import canvas_geometry

    out = [pts[0]]
    for (ax, ay), (cx, cy), (bx, by) in canvas_geometry.curve_pieces(pts):
        for step in range(1, per_piece + 1):
            t = step / float(per_piece)
            out.append(((1 - t) ** 2 * ax + 2 * (1 - t) * t * cx + t * t * bx, (1 - t) ** 2 * ay + 2 * (1 - t) * t * cy + t * t * by))
    return out


def curve_friendly(points: Sequence[Sequence[float]], radius: float) -> List[Tuple[float, float]]:
    """An orthogonal route's points for an arrow drawn as a curve: every corner gets a point ``radius`` before and after
    it, so the curve (``canvas_geometry.curve_pieces``) runs straight along the pieces and turns in a small arc at each
    corner instead of cutting across it (a curved graph's detour; QA phase 2, R3)."""
    pts = [(float(p[0]), float(p[1])) for p in points]
    if len(pts) < 3:
        return pts
    out = [pts[0]]
    for index in range(1, len(pts) - 1):
        (ax, ay), (bx, by), (cx, cy) = pts[index - 1], pts[index], pts[index + 1]
        before, after = abs(bx - ax) + abs(by - ay), abs(cx - bx) + abs(cy - by)
        r = min(radius, before / 2.0, after / 2.0)
        if r <= 0:
            out.append((bx, by))
            continue
        out.append((round(bx - (bx - ax) / before * r, 2), round(by - (by - ay) / before * r, 2)))
        out.append((bx, by))
        out.append((round(bx + (cx - bx) / after * r, 2), round(by + (cy - by) / after * r, 2)))
    out.append(pts[-1])
    return out


def _through(points: Sequence[Sequence[float]], box: Sequence[float], inset: float = 2.0) -> bool:
    """Whether the polyline ``points`` enters the inside of ``box`` (shrunk by ``inset``): Liang-Barsky per piece."""
    x0, y0, x1, y1 = box[0] + inset, box[1] + inset, box[2] - inset, box[3] - inset
    if x0 >= x1 or y0 >= y1:
        return False
    for (ax, ay), (bx, by) in zip(points, points[1:]):
        dx, dy = bx - ax, by - ay
        lo, hi = 0.0, 1.0
        inside = True
        for p, q in ((-dx, ax - x0), (dx, x1 - ax), (-dy, ay - y0), (dy, y1 - ay)):
            if p == 0:
                if q < 0:
                    inside = False
                    break
                continue
            t = q / p
            if p < 0:
                lo = max(lo, t)
            else:
                hi = min(hi, t)
            if lo > hi:
                inside = False
                break
        if inside and hi - lo > 1e-9:
            return True
    return False


def _chain(frame_id: str, groups: Mapping[str, Element]) -> List[str]:
    by_id = {el["id"]: part for part, el in groups.items()}
    out: List[str] = []
    cursor = frame_id
    while cursor in by_id and by_id[cursor] not in out:
        out.append(by_id[cursor])
        cursor = str(groups[by_id[cursor]].get("frame"))
    return out


# --------------------------------------------------------------------------
# prepare: a new graph's layout before the canvas lock (1.3)


class _Loose:
    """Enough of an ``OpContext`` to read a raw op outside the canvas; anything odd gives up (the op then reports it)."""

    author_name = ""

    def invalid(self, field: str, message: str, **details: Any) -> Exception:
        return ValueError(message)

    error = too_big = lambda self, *a, **k: ValueError("refused")  # noqa: E731

    def text(self, op: Mapping[str, Any], field: str, *, limit: str = "text", one_line: bool = False, required: bool = False,
             value: Any = ..., label: Optional[str] = None) -> str:
        found = op.get(field) if value is ... else value
        if found is None:
            if required:
                raise ValueError(field)
            return ""
        if not isinstance(found, str) or (one_line and "\n" in found) or len(found) > 2000:
            raise ValueError(field)
        return found.strip()

    def choice(self, op: Mapping[str, Any], field: str, choices: Sequence[str], default: str, *, value: Any = ...,
               label: Optional[str] = None) -> str:
        found = op.get(field) if value is ... else value
        if found is None:
            return default
        if found not in choices:
            raise ValueError(field)
        return str(found)

    def color(self, value: Any) -> str:
        raise ValueError("colours are the canvas's to read")

    fill = color

    def warn(self, code: str, message: str, ids: Sequence[str]) -> None:
        raise ValueError(code)


def _probe_size(kind: str, text: str, icon: Optional[str]) -> Optional[Tuple[float, float]]:
    """A new node's size, as ``BlockContext.member`` will fit it (the default style)."""
    import math

    from herdr_team.canvas_kinds import get

    found = get(kind)
    if found is None or found.measure is None:
        return None
    probe = {"type": kind, "text": text, "style": {"font": "normal", "size": 20}, "group": "graph", "fit": {}}
    if icon:
        probe["icon"] = icon
    result = found.measure(probe, node_minimum(kind))
    return float(max(1, int(math.ceil(result.w - 1e-9)))), float(max(1, int(math.ceil(result.h - 1e-9))))


def prepare(op: Dict[str, Any]) -> Any:
    """A new graph's layout, computed before ``canvas.lock`` is taken; ``arrange`` takes it when its request is the same.
    Only a plain op is prepared (no style fields, shape nodes, no id that could name a graph already drawn)."""
    if any(op.get(key) is not None for key in ("tone", "variant", "color", "fill", "font", "size", "width", "dash", "opacity", "rough")):
        return None
    loose = _Loose()
    try:
        spec: Dict[str, Any] = {"op": "graph"}
        for key in ("title",) + SETTINGS:
            if op.get(key) is not None:
                spec[key] = op[key]
        for name, item in (("nodes", node_item), ("groups", group_item), ("edges", edge_item)):
            raw = op.get(name)
            if raw is not None:
                if not isinstance(raw, list):
                    return None
                spec[name] = [item(loose, entry, "{}[{}]".format(name, i)) for i, entry in enumerate(raw)]
        spec = normalize(loose, spec)
    except (ValueError, TypeError, KeyError, AttributeError):
        return None
    nodes = spec["nodes"]
    if any((n.get("kind") or "box") not in ("box", "ellipse", "diamond", "note") for n in nodes):
        return None
    layout = canvas_layouts.get(spec.get("layout") or "layers")
    if layout is None:
        return None
    tokens = _layout_tokens()
    groups = spec.get("groups") or []
    depth = _depth(groups)
    group_order = sorted(groups, key=lambda g: (depth[g["id"]], groups.index(g)))
    lnodes = []
    for index, n in enumerate(nodes):
        size = _probe_size(n.get("kind") or "box", n["text"], n.get("icon"))
        if size is None:
            return None
        lnodes.append(canvas_layouts.LNode(id=n["id"], w=size[0], h=size[1], order=index, group=n.get("in") if layout.groups else None))
    group_pad = tokens.get("group_pad") if isinstance(tokens.get("group_pad"), list) and len(tokens["group_pad"]) == 4 else [20, 40, 20, 20]
    lgroups = [canvas_layouts.LGroup(id=g["id"], parent=g.get("parent"), pad=tuple(float(v) for v in group_pad))  # type: ignore[arg-type]
               for g in group_order]
    ledges = []
    for e in spec.get("edges") or []:
        label = None
        if e.get("label"):
            from herdr_team import canvas_geometry

            found = canvas_geometry.arrow_label_text({"text": e["label"], "points": [[0, 0], [1, 0]], "style": {"font": "normal", "size": 20}})
            label = (float(found[0][0]), float(found[0][1])) if found is not None else None
        ledges.append(canvas_layouts.LEdge(id=e["id"], a=e["from"], b=e["to"],
                                           directed=(e.get("head") or "arrow") != "none" or (e.get("tail") or "none") != "none", label=label))
    direction = spec.get("direction") or "down"
    if direction not in layout.directions:
        direction = layout.directions[0]
    request = canvas_layouts.LayoutRequest(
        nodes=tuple(lnodes), edges=tuple(ledges) if layout.edges else (), groups=tuple(lgroups) if layout.groups else (),
        direction=direction, gap=_gap(spec, float(tokens.get("gap", 40))), rank_gap=float(tokens.get("rank_gap", 80)),
        same_rank=tuple(tuple(x) for x in spec.get("same_rank") or ()) if layout.name == "layers" else (),
        order=tuple(tuple(x) for x in spec.get("order") or ()) if layout.name == "layers" else (), incremental=False)
    result = canvas_layouts.run(layout.name, request, seconds=canvas_layouts.PREPARE_SECONDS)
    if len(_PREPARED) >= _PREPARED_MAX:
        _PREPARED.pop(next(iter(_PREPARED)))
    _PREPARED[_cache_key(layout.name, request)] = result
    return {"layout": layout.name}


# --------------------------------------------------------------------------
# readback


def spec(root: Element, members: List[Element], full: bool) -> Dict[str, Any]:
    """The graph as the op that would draw it: groups, nodes (a bare id when that is all), edges in shorthand when plain."""
    out: Dict[str, Any] = {"op": "graph"}
    if root.get("alias"):
        out["id"] = root["alias"]
    if root.get("text"):
        out["title"] = root["text"]
    settings = _zone.settings_of(root)
    for key in SETTINGS:
        if settings.get(key) is not None:
            out[key] = settings[key]
    nodes, groups, edges, _loose = roles(root, members)
    part_of = {el["id"]: part for part, el in list(nodes.items()) + list(groups.items())}
    group_items = []
    for part, el in groups.items():
        item = dict(el.get("item") or {"id": part})
        title = str(el.get("text") or "")
        if title and title != part:
            item["title"] = title
        else:
            item.pop("title", None)
        parent = part_of.get(str(el.get("frame")))
        if parent is not None:
            item["parent"] = parent
        else:
            item.pop("parent", None)
        group_items.append(item)
    if group_items:
        out["groups"] = group_items
    node_items: List[Any] = []
    for part, el in nodes.items():
        item = dict(el.get("item") or _derived_node(part, el))
        item["text"] = str(el.get("text") or "") or part
        group = part_of.get(str(el.get("frame")))
        if group is not None and group in groups:
            item["in"] = group
        else:
            item.pop("in", None)
        node_items.append(part if item == {"id": part, "text": part} else item)
    out["nodes"] = node_items
    edge_items: List[Any] = []
    used: Dict[str, int] = {}
    for part, el in edges.items():
        item = dict(el.get("item") or {"id": part, "from": part_of.get(str(el.get("from")), "?"), "to": part_of.get(str(el.get("to")), "?")})
        label = str(el.get("text") or "")
        if label:
            item["label"] = label
        else:
            item.pop("label", None)
        base, count = edge_key(item["from"], item["to"]), 1
        auto = base
        while auto in used:
            count += 1
            auto = "{}#{}".format(base, count)
        used[item["id"]] = 1
        short = shorthand(item) if item["id"] == auto else None
        if short is not None:
            edge_items.append(short)
        else:
            if item["id"] == auto:
                item.pop("id")
            edge_items.append(item)
    if edge_items:
        out["edges"] = edge_items
    return out


def _derived_node(part: str, el: Element) -> Dict[str, Any]:
    """A member with no stored item (a shape dropped in and adopted): what it is now."""
    item: Dict[str, Any] = {"id": part}
    kind = kind_of(el)
    if kind is not None and kind.name != "box" and kind.node:
        item["kind"] = kind.name
    return item


def readback(el: Element, full: bool) -> str:
    """``E-40 graph checkout "Checkout flow" flow/right [c40r2 912x436]``."""
    settings = _zone.settings_of(el)
    title = str(el.get("text") or "")
    return "{} graph{}{} {}/{} [{},{} {}x{}]".format(el.get("id"), " " + str(el["alias"]) if el.get("alias") else "",
                                                    " " + quote(title, 0 if full else 80) if title else "",
                                                    settings.get("layout") or "layers", settings.get("direction") or "down",
                                                    el.get("x"), el.get("y"), el.get("w"), el.get("h"))


def adopt(root: Element, el: Element) -> Optional[str]:
    """A node kind dropped into the graph becomes a node ``n<k>`` (``k`` its element number; the core pins it where it
    landed)."""
    kind = kind_of(el)
    if kind is None or not kind.node:
        return None
    return "n{}".format(_num_id(el.get("id")))


# --------------------------------------------------------------------------
# checks


def pin_overlap(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Two pinned members of this graph that overlap: only a person can say which should move."""
    by_id = env.get("by_id") or {}
    pinned = [m for m in by_id.values() if m.get("group") == el.get("id") and m.get("pin") and m.get("type") != "arrow" and m.get("part")]
    out = []
    for i, a in enumerate(pinned):
        for b in pinned[i + 1:]:
            ba, bb = bounds(a), bounds(b)
            if ba[0] < bb[2] and bb[0] < ba[2] and ba[1] < bb[3] and bb[1] < ba[3]:
                out.append({"code": "pin_overlap", "ids": [a["id"], b["id"]],
                            "message": "{} and {} are both pinned and overlap in {}; unpin one so the layout can place it".format(
                                a["id"], b["id"], el.get("alias") or el.get("id")),
                            "fix": {"op": "unpin", "id": b["id"], "intent": "let the layout place {}".format(b["id"])}})
    return out


#: A graph is reported when its edges cross more than this, or a quarter of its edge count (``crossings_high``).
CROSSINGS_FLOOR = 3
#: Past this many nodes the check does not try a full relayout to compare (it would cost more than it says).
CROSSINGS_MAX_NODES = 80
#: A fresh relayout's crossing count by request: a check run again over an unchanged graph does not lay it out again.
_FRESH_CROSSINGS: Dict[Any, float] = {}


def _pieces(points: Sequence[Sequence[float]]) -> List[Tuple[Tuple[float, float], Tuple[float, float]]]:
    return [((float(a[0]), float(a[1])), (float(b[0]), float(b[1]))) for a, b in zip(points, points[1:])]


def _cross(p1: Tuple[float, float], p2: Tuple[float, float], p3: Tuple[float, float], p4: Tuple[float, float]) -> bool:
    def orient(a: Tuple[float, float], b: Tuple[float, float], c: Tuple[float, float]) -> int:
        value = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(value) < 1e-9 else (1 if value > 0 else -1)

    return orient(p1, p2, p3) * orient(p1, p2, p4) < 0 and orient(p3, p4, p1) * orient(p3, p4, p2) < 0


def edge_crossings(routes: Sequence[Tuple[Tuple[str, str], Sequence[Sequence[float]]]]) -> int:
    """How many pairs of edges with four distinct ends cross (each drawn as its route)."""
    count = 0
    pieces = [_pieces(points) for _ends, points in routes]
    # Each route's box, and each piece's: pairs whose boxes do not meet cannot cross (most pairs, on a laid-out graph).
    boxes = [(min(min(a[0], b[0]) for a, b in ps), min(min(a[1], b[1]) for a, b in ps), max(max(a[0], b[0]) for a, b in ps),
              max(max(a[1], b[1]) for a, b in ps)) if ps else None for ps in pieces]
    for i in range(len(routes)):
        box_i, (a1, b1) = boxes[i], routes[i][0]
        if box_i is None:
            continue
        for j in range(i + 1, len(routes)):
            box_j = boxes[j]
            if box_j is None or box_j[0] > box_i[2] or box_i[0] > box_j[2] or box_j[1] > box_i[3] or box_i[1] > box_j[3]:
                continue
            (a2, b2) = routes[j][0]
            if a1 in (a2, b2) or b1 in (a2, b2):
                continue
            if any(_cross(s[0], s[1], t[0], t[1]) for s in pieces[i] for t in pieces[j]
                   if not (max(s[0][0], s[1][0]) < min(t[0][0], t[1][0]) or max(t[0][0], t[1][0]) < min(s[0][0], s[1][0]) or
                           max(s[0][1], s[1][1]) < min(t[0][1], t[1][1]) or max(t[0][1], t[1][1]) < min(s[0][1], s[1][1]))):
                count += 1
    return count


def crossings_high(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """A graph whose edges cross a lot, when a full relayout would cross less: the fix is that relayout."""
    by_id = env.get("by_id") or {}
    members = [m for m in by_id.values() if m.get("group") == el.get("id")]
    nodes, groups, edges, _loose = roles(el, members)
    if not edges or len(nodes) > CROSSINGS_MAX_NODES:
        return []
    routes = [((str(e.get("from")), str(e.get("to"))), e.get("points") or []) for e in edges.values() if len(e.get("points") or []) >= 2]
    found = edge_crossings(routes)
    if found <= max(CROSSINGS_FLOOR, len(edges) // 4):
        return []
    settings = _zone.settings_of(el)
    layout = canvas_layouts.get(settings.get("layout") or "layers")
    if layout is None or not layout.edges or not layout.crossings:
        return []
    part_of = {m["id"]: part for part, m in nodes.items()}
    request = canvas_layouts.LayoutRequest(
        nodes=tuple(canvas_layouts.LNode(id=part, w=bounds(m)[2] - bounds(m)[0], h=bounds(m)[3] - bounds(m)[1], order=i)
                    for i, (part, m) in enumerate(sorted(nodes.items(), key=lambda item: _num_id(item[1]["id"])))),
        edges=tuple(canvas_layouts.LEdge(id=part, a=part_of[str(e.get("from"))], b=part_of[str(e.get("to"))])
                    for part, e in edges.items() if str(e.get("from")) in part_of and str(e.get("to")) in part_of),
        direction=(settings.get("direction") or "down") if (settings.get("direction") or "down") in layout.directions else layout.directions[0],
        incremental=False)
    key = _cache_key(layout.name, request)
    fresh_crossings = _FRESH_CROSSINGS.get(key)
    if fresh_crossings is None:
        fresh_crossings = float(canvas_layouts.run(layout.name, request).stats.get("crossings", found))
        if len(_FRESH_CROSSINGS) >= _PREPARED_MAX:
            _FRESH_CROSSINGS.pop(next(iter(_FRESH_CROSSINGS)))
        _FRESH_CROSSINGS[key] = fresh_crossings
    if fresh_crossings >= found:
        return []
    alias = el.get("alias") or el.get("id")
    return [{"code": "crossings_high", "ids": [str(el.get("id"))],
             "message": "{} has {} edge crossings; a full relayout draws about {}".format(alias, found, int(fresh_crossings)),
             "fix": {"op": "patch", "id": alias, "relayout": "full", "intent": "lay {} out again from scratch".format(alias)}}]


# --------------------------------------------------------------------------
# the op


def _legacy(ctx: Any, alias: Any) -> Optional[Element]:
    """A graph drawn before 0.22 under this alias: the author's frame with ``group`` members and no ``block``."""
    if not isinstance(alias, str):
        return None
    for el in ctx.live():
        if el.get("alias") == alias and el.get("type") == "frame" and not el.get("block") and el.get("author") == ctx.author_name:
            if any(m.get("group") == el["id"] for m in ctx.live()):
                return el
    return None


def convert_legacy(ctx: Any, root: Element) -> None:
    """Make a 0.21 graph a graph block: nodes and subgraph frames take their alias suffix as part, edges ``e:a->b``."""
    rid = root["id"]
    alias = str(root.get("alias") or "")
    parts: Dict[str, str] = {}
    members = [m for m in ctx.live() if m.get("group") == rid]
    for m in members:
        if m.get("type") == "arrow":
            continue
        suffix = str(m.get("alias") or "")
        part = suffix[len(alias) + 1:] if suffix.startswith(alias + ".") else m["id"].replace("-", "_")
        parts[m["id"]] = part
        item: Dict[str, Any] = {"id": part}
        if m.get("type") == "frame":
            if m.get("text") and m["text"] != part:
                item["title"] = m["text"]
        else:
            item["text"] = str(m.get("text") or part)
            if m.get("type") != "box":
                item["kind"] = m["type"]
        ctx.update(m, part=part, item=item)
    used: Dict[str, int] = {}
    for m in members:
        if m.get("type") != "arrow" or m.get("from") not in parts or m.get("to") not in parts:
            continue
        base, count = edge_key(parts[m["from"]], parts[m["to"]]), 1
        key = base
        while key in used:
            count += 1
            key = "{}#{}".format(base, count)
        used[key] = 1
        ctx.update(m, part=key)
    ctx.update(root, block="graph", settings={}, fit={"policy": "hug", "min": list(ROOT_MIN)})


def create(ctx: Any, op: Dict[str, Any]) -> None:
    """The ``graph`` op: a block of nodes, groups and edges; an ``id`` it already names is that graph, re-drawn (an upsert,
    or the conversion of a graph drawn before 0.22)."""
    nodes = op.get("nodes")
    if isinstance(nodes, list) and len(nodes) > diagram.MAX_GRAPH_NODES:
        raise ctx.too_big("nodes", "MAX_GRAPH_NODES", diagram.MAX_GRAPH_NODES, "{} nodes; the limit is {}".format(len(nodes), diagram.MAX_GRAPH_NODES))
    legacy = _legacy(ctx, op.get("id"))
    if legacy is not None and ctx.may_edit(legacy):
        convert_legacy(ctx, legacy)
    ctx.block("graph", op)


OPS = (
    OpSpec(name="graph", family="diagram", fields=("nodes", "edges", "layout", "direction", "title", "id", "groups", "same_rank", "order", "route", "client_id"),
           create=create, style=True, place=True, order=70,
           doc="nodes, groups and edges laid out by a registered layout and routed around each other (a block you patch)",
           mcp="graph {nodes [{id,text,kind,tone,icon,in,detail}], edges [\"a -> b: label\", \"a --> b\"], groups [{id,title,tone,parent}], "
               "layout layers|flow|tree|radial|force|grid, direction down|right|up|left, same_rank, order, route orthogonal|straight|curved}"),
)

KINDS = (
    Kind(name="graph", stored_as="frame", role="composite", ops=("graph", "mermaid"), cell=True, tone_group="frame", layer="zones",
         block=Block(collections=(Collection(name="nodes", item=node_item, maximum=10_000, label="text", doc="the boxes",
                                             refs=(("in", "groups", "clear"),)),
                                  Collection(name="groups", item=group_item, maximum=10_000, doc="nested frames around nodes",
                                             refs=(("parent", "groups", "clear"),)),
                                  Collection(name="edges", item=edge_item, remove_key=edge_remove_key, maximum=10_000,
                                             doc="\"a -> b: label\" relations", refs=(("from", "nodes", "drop"), ("to", "nodes", "drop")))),
                     settings=SETTINGS, fields=("title",), parts="members", positional=True, normalize=normalize, build=build, spec=spec,
                     adopt=adopt, prepare=prepare, max_members=2000),
         arrange=arrange, emit=_zone.emit, hit=_zone.hit, text_edit=_zone.title_edit, readback=readback, checks=(pin_overlap, crossings_high),
         noun=("graph", "graphs"), doc="a graph: nodes, groups and edges, laid out and routed (patch it to change it)"),
)
