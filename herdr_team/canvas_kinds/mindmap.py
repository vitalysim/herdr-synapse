"""Mind maps (canvas v2 phase 2, 4.7.2): a root topic, its branches and their topics, laid out as a tidy tree.

A mind map is a block stored as a frame with ``block: "mindmap"`` (phase 2,
D2) that draws nothing of its own (a hit rim only): its title is the root
topic. Topics are ``box`` members (part ``root`` for the root, ``t<n>`` for
the others) and the branch lines are arrow members without heads, curved by
default. An agent writes the tree, never a position:

* ``tree``: nested object keys and list strings
  (``{"Audience": ["Devs", "Managers"], "Channels": {"Blog": ["SEO"]}}``), or
* ``topics``: ``[{id, text, under, tone, icon}]``, ``under`` a topic id or its
  exact text when unique (the root by default).

The root is solid ``accent``; each first-level branch takes the next tone of
``BRANCH_TONES`` (``branch_tones: auto``) and soft paper, deeper topics an
outline in their branch's tone. ``arrange`` runs the ``tree`` layout rightward
(``side: both`` splits the branches left and right by leaf count), and
``patch`` adds, removes (with the subtree) or updates topics.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team import canvas_icons, canvas_layouts, canvas_routers, canvas_theme
from herdr_team.canvas_kinds import Arrangement, Kind, OpSpec, outline
from herdr_team.canvas_kinds import _zone, graph
from herdr_team.canvas_kinds._common import Element, bounds, quote
from herdr_team.canvas_kinds.sdk import Block, Collection

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 76

MAX_TOPICS = 200
MAX_DEPTH = 8
BRANCH_TONES = ("info", "success", "warning", "accent", "idea", "decision")
SETTINGS = ("side", "branch_tones", "route")
SIDES = ("right", "both")
ROOT = "root"
TOPIC_ID = re.compile(r"^[A-Za-z0-9_-]{1,32}\Z")
DANGLING = re.compile(r"^t[0-9]+\Z")
TOPIC_FIELDS = ("id", "text", "under", "tone", "icon")
#: The root topic and the others: minimum sizes and label sizes.
ROOT_MIN = (180.0, 64.0)
TOPIC_MIN = (120.0, 44.0)
ROOT_SIZE = 28
#: The invisible root frame keeps this much room around the topics.
PAD = 20


def topic_item(ctx: Any, raw: Any, field: str) -> Dict[str, Any]:
    """One topic: a bare string is its text (under the root)."""
    if isinstance(raw, str):
        raw = {"text": raw}
    if not isinstance(raw, dict):
        raise ctx.invalid(field, "{} must be a topic text or {{\"text\", \"under\"}}".format(field))
    for key in raw:
        if key not in TOPIC_FIELDS:
            raise ctx.invalid("{}.{}".format(field, key), "a topic takes {}".format(", ".join(TOPIC_FIELDS)))
    out: Dict[str, Any] = {}
    if raw.get("id") is not None:
        if not isinstance(raw["id"], str) or not TOPIC_ID.match(raw["id"]) or raw["id"] == ROOT:
            raise ctx.invalid(field + ".id", "a topic id is 1 to 32 letters, digits, _ or - (not root)")
        out["id"] = raw["id"]
    out["text"] = ctx.text(raw, "text", limit="label", one_line=True, required=True, label=field + ".text")
    if raw.get("under") is not None:
        if not isinstance(raw["under"], str) or not raw["under"].strip():
            raise ctx.invalid(field + ".under", "under names a topic by id or text")
        out["under"] = raw["under"]
    if raw.get("tone") is not None:
        out["tone"] = ctx.choice(raw, "tone", canvas_theme.TONES, "neutral", label=field + ".tone")
    if raw.get("icon") is not None:
        found = canvas_icons.resolve(raw["icon"]) if isinstance(raw["icon"], str) else None
        if found is None:
            raise ctx.error("icon_unknown", "{} is not an icon; try {}".format(raw["icon"], ", ".join(canvas_icons.suggest(raw["icon"])) or
                                                                               "canvas icons --search"),
                            field=field + ".icon", did_you_mean=canvas_icons.suggest(raw["icon"]))
        out["icon"] = found
    return out


def _from_tree(ctx: Any, tree: Any) -> List[Dict[str, Any]]:
    """A nested tree as topics in depth-first order, ids ``t1``, ``t2`` ... in that order."""
    out: List[Dict[str, Any]] = []

    def add(text: Any, under: Optional[str], field: str) -> str:
        topic = topic_item(ctx, {"text": text}, field)
        topic["id"] = "t{}".format(len(out) + 1)
        if under is not None:
            topic["under"] = under
        out.append(topic)
        if len(out) > MAX_TOPICS:
            raise ctx.too_big("tree", "MAX_TOPICS", MAX_TOPICS, "the tree has over {} topics".format(MAX_TOPICS))
        return topic["id"]

    def walk(value: Any, under: Optional[str], field: str, depth: int) -> None:
        if depth > MAX_DEPTH:
            raise ctx.invalid(field, "a mind map is at most {} levels deep".format(MAX_DEPTH))
        if value is None:
            return
        if isinstance(value, str):
            add(value, under, field)
        elif isinstance(value, list):
            for index, item in enumerate(value):
                if isinstance(item, (dict, str)):
                    walk(item, under, "{}[{}]".format(field, index), depth)
                else:
                    raise ctx.invalid("{}[{}]".format(field, index), "a tree list holds topic texts or objects")
        elif isinstance(value, dict):
            for key, children in value.items():
                tid = add(key, under, "{}.{}".format(field, key))
                walk(children, tid, "{}.{}".format(field, key), depth + 1)
        else:
            raise ctx.invalid(field, "a tree is nested objects and lists of topic texts")

    walk(tree, None, "tree", 1)
    return out


def normalize(ctx: Any, spec: Dict[str, Any]) -> Dict[str, Any]:
    title = ctx.text(spec, "title", limit="label", one_line=True)
    if title:
        spec["title"] = title
    else:
        spec.pop("title", None)
    if spec.get("root") is not None:
        root = ctx.text(spec, "root", limit="label", one_line=True)
        if root and root != spec.get("title"):
            spec["root"] = root
        else:
            spec.pop("root", None)
    if spec.get("tree") is not None and spec.get("topics") is not None:
        raise ctx.invalid("tree", "a mind map takes tree or topics, not both")
    if spec.get("tree") is not None:
        spec["topics"] = _from_tree(ctx, spec.pop("tree"))
    topics = [dict(t) for t in spec.get("topics") or []]
    for t in topics:
        t.pop("_gen", None)
    if len(topics) > MAX_TOPICS:
        raise ctx.too_big("topics", "MAX_TOPICS", MAX_TOPICS, "{} topics; the limit is {}".format(len(topics), MAX_TOPICS))
    taken = {t["id"] for t in topics if t.get("id")}
    counter = 0
    for t in topics:
        if not t.get("id"):
            counter += 1
            while "t{}".format(counter) in taken:
                counter += 1
            t["id"] = "t{}".format(counter)
            taken.add(t["id"])
    ids = [t["id"] for t in topics]
    for index, tid in enumerate(ids):
        if tid in ids[:index]:
            raise ctx.invalid("topics[{}].id".format(index), "topic id {} appears twice".format(tid))
    root_text = spec.get("root") or spec.get("title") or ""
    by_text: Dict[str, List[str]] = {}
    for t in topics:
        by_text.setdefault(t["text"], []).append(t["id"])
    dropped = set()
    for index, t in enumerate(topics):
        under = t.get("under")
        if under is None or under == ROOT:
            t.pop("under", None)
        elif under in ids:
            pass
        elif len(by_text.get(under, [])) == 1:
            t["under"] = by_text[under][0]
        elif under == root_text:
            t.pop("under", None)
        elif DANGLING.match(under):
            dropped.add(t["id"])  # its parent was removed: the subtree goes with it
        elif by_text.get(under):
            raise ctx.invalid("topics[{}].under".format(index), "{} names {} topics; use an id".format(under, len(by_text[under])))
        else:
            raise ctx.invalid("topics[{}].under".format(index), "{} is not a topic id or text".format(under))
    parent = {t["id"]: t.get("under") for t in topics}
    for tid in ids:
        seen, cursor, depth = {tid}, parent.get(tid), 1
        while cursor is not None:
            if cursor in seen:
                raise ctx.invalid("topics", "topics {} and {} are under each other".format(tid, cursor))
            if cursor in dropped:
                dropped.add(tid)
                break
            seen.add(cursor)
            depth += 1
            cursor = parent.get(cursor)
        if depth > MAX_DEPTH:
            raise ctx.invalid("topics", "a mind map is at most {} levels deep".format(MAX_DEPTH))
    spec["topics"] = [t for t in topics if t["id"] not in dropped]
    if spec.get("side") is not None:
        spec["side"] = ctx.choice(spec, "side", SIDES, "both")
    if spec.get("branch_tones") is not None:
        spec["branch_tones"] = ctx.choice(spec, "branch_tones", ("auto", "none"), "auto")
    if spec.get("route") is not None:
        found = canvas_routers.get(spec["route"]) if isinstance(spec["route"], str) else None
        if found is None:
            raise ctx.invalid("route", "route is curved, orthogonal or straight")
        spec["route"] = found.name
    return spec


# --------------------------------------------------------------------------
# build


def branches(topics: Sequence[Mapping[str, Any]], auto: bool) -> Dict[str, Tuple[int, Optional[str]]]:
    """Every topic's level (1 under the root) and the tone of its branch."""
    parent = {t["id"]: t.get("under") for t in topics}
    own = {t["id"]: t.get("tone") for t in topics}
    first = [t["id"] for t in topics if t.get("under") is None]
    palette = {tid: (own[tid] or (BRANCH_TONES[i % len(BRANCH_TONES)] if auto else None)) for i, tid in enumerate(first)}
    out: Dict[str, Tuple[int, Optional[str]]] = {}
    for t in topics:
        chain = [t["id"]]
        while parent.get(chain[-1]) is not None:
            chain.append(parent[chain[-1]])  # type: ignore[arg-type]
        branch = palette.get(chain[-1])
        out[t["id"]] = (len(chain), own[t["id"]] or branch)
    return out


def build(bctx: Any, spec: Dict[str, Any]) -> None:
    ctx = bctx.ctx
    settings = {key: spec[key] for key in SETTINGS if spec.get(key) is not None}
    settings.update(band=0, padding=PAD)
    fields: Dict[str, Any] = {}
    if bctx.root is None:
        fields["fit"] = {"policy": "hug", "min": [40, 40]}
    root = bctx.root_fields(text=spec.get("title") or "", settings=settings, **fields)
    rid = root["id"]
    before = bctx.members()
    root_text = spec.get("root") or spec.get("title") or "Mind map"
    root_style = ctx.style({"tone": "accent", "variant": "solid", "size": ROOT_SIZE}, "box")
    old = before.get(ROOT)
    bctx.member(ROOT, "box", text=root_text, style=root_style if old is None or (old.get("style") or {}).get("tone") != "accent" else None,
                frame=rid, minimum=ROOT_MIN, item={"id": ROOT})
    topics = spec.get("topics") or []
    auto = settings.get("branch_tones", "auto") != "none"
    levels = branches(topics, auto)
    route = settings.get("route") or "curved"
    for t in topics:
        level, tone = levels[t["id"]]
        variant = "soft" if level == 1 else "outline"
        style = ctx.style({"tone": tone or "neutral", "variant": variant}, "box")
        old = before.get(t["id"])
        keep = old is not None and (old.get("style") or {}).get("tone") == (tone or "neutral") and (old.get("style") or {}).get("variant") == variant
        bctx.member(t["id"], "box", text=t["text"], style=None if keep else style, frame=rid, minimum=TOPIC_MIN, icon=t.get("icon"), item=dict(t))
    for t in topics:
        under = t.get("under") or ROOT
        _level, tone = levels[t["id"]]
        part = "e:{}->{}".format(under, t["id"])
        old = before.get(part)
        style = dict(ctx.style({"tone": tone} if tone else {}, "arrow"), fill=None, route=route)
        if old is not None and (old.get("style") or {}).get("route") == route and (old.get("style") or {}).get("tone") == style.get("tone"):
            style = None
        bctx.edge(part, under, t["id"], style=style, head="none", frame=rid)
    parts = [ROOT] + [t["id"] for t in topics] + ["e:{}->{}".format(t.get("under") or ROOT, t["id"]) for t in topics]
    bctx.keep_only(parts)


# --------------------------------------------------------------------------
# arrange


def arrange(root: Element, members: List[Element], env: Dict[str, Any]) -> Arrangement:
    if env.get("reason") == "hug":
        return Arrangement(boxes={})
    rid = root["id"]
    topics = {str(el["part"]): el for el in members if el.get("group") == rid and isinstance(el.get("part"), str) and el.get("type") != "arrow"}
    lines = {str(el["part"]): el for el in members if el.get("group") == rid and el.get("type") == "arrow" and isinstance(el.get("part"), str)}
    if ROOT not in topics:
        return Arrangement(boxes={})
    settings = _zone.settings_of(root)
    tokens = graph._layout_tokens()
    ox, oy = (env.get("content") or _zone.content_box(root))[:2]
    held = set(env.get("held") or ())
    parent: Dict[str, Optional[str]] = {ROOT: None}
    for part, line in lines.items():
        a, _arrow, b = part[2:].partition("->")
        if b in topics and a in topics:
            parent[b] = a
    order = sorted(topics, key=lambda p: (p != ROOT, graph._num_id(topics[p]["id"])))
    nodes = []
    for index, part in enumerate(order):
        el = topics[part]
        x0, y0, x1, y1 = bounds(el)
        pinned = el.get("pin") is not None or el["id"] in held
        nodes.append(canvas_layouts.LNode(id=part, w=x1 - x0, h=y1 - y0, order=index, pin=(x0 - ox, y0 - oy) if pinned else None,
                                          data={"parent": parent.get(part)}))
    side = settings.get("side") or "both"
    request = canvas_layouts.LayoutRequest(nodes=tuple(nodes), direction="right", gap=float(tokens.get("tree_gap", 24)),
                                           rank_gap=float(tokens.get("tree_level_gap", 60)), options={"side": side})
    result = canvas_layouts.run("tree", request)
    boxes: Dict[str, Tuple[float, float, float, float]] = {}
    world: Dict[str, Tuple[float, float, float, float]] = {}
    for node in nodes:
        px, py = result.positions[node.id]
        boxes[topics[node.id]["id"]] = (ox + px, oy + py, node.w, node.h)
        world[node.id] = (ox + px, oy + py, ox + px + node.w, oy + py + node.h)
    route = settings.get("route") or "curved"
    requests = []
    ids: Dict[str, str] = {}
    for part, line in sorted(lines.items(), key=lambda item: graph._num_id(item[1]["id"])):
        a, _arrow, b = part[2:].partition("->")
        if a not in world or b not in world:
            continue
        ab, bb = world[a], world[b]
        rightward = (bb[0] + bb[2]) / 2.0 >= (ab[0] + ab[2]) / 2.0
        via: Tuple[Tuple[float, float], ...] = ()
        if route != "orthogonal":
            sx = ab[2] if rightward else ab[0]
            ex = bb[0] if rightward else bb[2]
            mx = (sx + ex) / 2.0
            ya, yb = (ab[1] + ab[3]) / 2.0, (bb[1] + bb[3]) / 2.0
            via = ((mx, ya),) if abs(ya - yb) < 1.0 else ((mx, ya), (mx, yb))
        ports = ("e", "w") if rightward else ("w", "e")
        requests.append(canvas_routers.RouteRequest(
            id=part, a=canvas_routers.End(box=ab, outline=outline(topics[a]), id=topics[a]["id"], side=ports[0] if route == "orthogonal" else None),
            b=canvas_routers.End(box=bb, outline=outline(topics[b]), id=topics[b]["id"], side=ports[1] if route == "orthogonal" else None),
            via=via, obstacles=tuple((topics[p]["id"], world[p], "rect") for p in world if p not in (a, b)) if route == "orthogonal" else (),
            clearance=float(tokens.get("clearance", 20)), radius=float(tokens.get("elbow_radius", 8)), port_spacing=float(tokens.get("port_spacing", 12))))
        ids[part] = line["id"]
    routes: Dict[str, Dict[str, Any]] = {}
    for part, found in canvas_routers.route_many(route, requests).items():
        routes[ids[part]] = {"points": [[x, y] for x, y in found.points]}
    return Arrangement(boxes=boxes, routes=routes, notes=tuple(n for n in result.notes), stats=dict(result.stats))


# --------------------------------------------------------------------------
# readback


def spec(root: Element, members: List[Element], full: bool) -> Dict[str, Any]:
    """The mind map as its op: a nested ``tree`` when the topics read as one, else the ``topics`` list."""
    out: Dict[str, Any] = {"op": "mindmap"}
    if root.get("alias"):
        out["id"] = root["alias"]
    rid = root["id"]
    title = str(root.get("text") or "")
    if title:
        out["title"] = title
    topics_by_part = {str(el["part"]): el for el in members if el.get("group") == rid and isinstance(el.get("part"), str) and el.get("type") != "arrow"}
    root_topic = topics_by_part.get(ROOT)
    root_text = str(root_topic.get("text") or "") if root_topic is not None else ""
    if root_text and root_text != title:
        out["root"] = root_text
    settings = _zone.settings_of(root)
    for key in SETTINGS:
        if settings.get(key) is not None:
            out[key] = settings[key]
    parent: Dict[str, Optional[str]] = {}
    for el in members:
        part = el.get("part")
        if el.get("group") == rid and el.get("type") == "arrow" and isinstance(part, str) and part.startswith("e:"):
            a, _arrow, b = part[2:].partition("->")
            parent[b] = None if a == ROOT else a
    topics = []
    for part, el in topics_by_part.items():
        if part == ROOT:
            continue
        item = dict(el.get("item") or {"id": part})
        item["id"] = part
        item["text"] = str(el.get("text") or "") or part
        under = parent.get(part)
        if under is not None:
            item["under"] = under
        else:
            item.pop("under", None)
        topics.append(item)
    tree = _as_tree(topics)
    if tree is not None:
        out["tree"] = tree
    else:
        out["topics"] = topics
    return out


def _as_tree(topics: Sequence[Mapping[str, Any]]) -> Optional[Any]:
    """The topics as a nested tree when that says everything: ids in depth-first order, no tone or icon, sibling texts unique."""
    if any(set(t) - {"id", "text", "under"} for t in topics):
        return None
    children: Dict[Optional[str], List[Mapping[str, Any]]] = {}
    for t in topics:
        children.setdefault(t.get("under"), []).append(t)
    order: List[str] = []

    def walk(under: Optional[str]) -> Any:
        kids = children.get(under, [])
        texts = [k["text"] for k in kids]
        if len(set(texts)) != len(texts):
            raise ValueError("duplicate sibling texts")
        if all(not children.get(k["id"]) for k in kids):
            for k in kids:
                order.append(k["id"])
            return [k["text"] for k in kids]
        out: Dict[str, Any] = {}
        for k in kids:
            order.append(k["id"])
            out[k["text"]] = walk(k["id"])
        return out

    try:
        tree = walk(None)
    except ValueError:
        return None
    if order != ["t{}".format(i + 1) for i in range(len(order))] or len(order) != len(topics):
        return None
    return tree


def readback(el: Element, full: bool) -> str:
    title = str(el.get("text") or "")
    return "{} mindmap{}{} [{},{} {}x{}]".format(el.get("id"), " " + str(el["alias"]) if el.get("alias") else "",
                                                " " + quote(title, 0 if full else 80) if title else "", el.get("x"), el.get("y"), el.get("w"), el.get("h"))


def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Nothing: the root topic is the mind map's title, and the frame is a hit rim only."""
    return _zone.emit(el, env, visible=False)


def create(ctx: Any, op: Dict[str, Any]) -> None:
    ctx.block("mindmap", op)


OPS = (
    OpSpec(name="mindmap", fields=("title", "root", "tree", "topics", "side", "branch_tones", "route", "id", "client_id"), create=create, place=True,
           order=72, doc="a mind map from a nested tree of topics, laid out as a tidy tree around its root",
           mcp="mindmap {title, root, tree {\"Topic\": [\"sub\", ...]} | topics [{id, text, under, tone, icon}], side both|right, "
               "branch_tones auto|none}"),
)

KINDS = (
    Kind(name="mindmap", stored_as="frame", role="composite", ops=("mindmap",), cell=True, tone_group="frame", layer="zones",
         block=Block(collections=(Collection(name="topics", item=topic_item, prefix="t", maximum=MAX_TOPICS, label="text", doc="the topics"),),
                     settings=SETTINGS, fields=("title", "root", "tree"), parts="members", positional=True, normalize=normalize, build=build,
                     spec=spec, max_members=2 * MAX_TOPICS + 2),
         arrange=arrange, emit=emit, hit=_zone.hit, text_edit=lambda el: None, readback=readback, checks=(graph.pin_overlap,),
         noun=("mind map", "mind maps"), doc="a mind map: a root, branches and topics, laid out as a tidy tree"),
)
