"""3D scenes (canvas v2 phase 4, ``.local/prd/canvas-v2-phase3-4.md`` 3): objects placed by relations, one element.

A ``scene3d`` is an inline block (``Block.parts == "inline"``): its root holds
its ``objects`` and ``links`` and its settings (``units``, ``camera``,
``lights``, ``ground``, ``labels``), and ``patch add/update/remove`` changes
them. Relations are solved in Python (``canvas_scene3d._solver``) when the op
applies, outside the canvas lock when the canvas offers ``Block.load``, and
the element stores the compact result (``solved``) that the page and the
projections both read. glTF models are read from ``artifacts/``, checked and
packed into one GLB asset at op time (``canvas_scene3d._gltf``).

The page draws the scene live on its one shared WebGL renderer
(``web/src/v2/scene3d``) and posts three stills (``iso``, ``front``, ``top``).
Without a page, the slot's fallback is a projection drawn here
(``canvas_scene3d._project``), labels included, and ``look`` reads the scene
back as text (``canvas_scene3d._describe``).

Pure, like every kind module: the files it reads come through the ``FetchIO``
``Block.load`` is handed, never ``open``.
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team import canvas_display as D
from herdr_team import canvas_scene3d as S3
from herdr_team import canvas_text
from herdr_team.canvas_kinds import Kind, OpSpec
from herdr_team.canvas_kinds import _slot
from herdr_team.canvas_kinds import _text as TX
from herdr_team.canvas_kinds._common import Element, quote
from herdr_team.canvas_kinds.sdk import Block, Collection
from herdr_team.canvas_scene3d import _describe, _gltf, _project, _solver, _spec, _vec, relations
from herdr_team.errors import HerdrTeamError

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``): after chart (90), before viz (100).
ORDER = 95

KIND_VERSION = 1
DEFAULT_SIZE = (640.0, 420.0)
MIN_SIZE = (240.0, 180.0)
MAX_SIDE = 4000.0
PAD = 12.0
BAND = 40.0
TITLE_SIZE, TITLE_WEIGHT = 16.0, 600
VIEWS = ("iso", "front", "top")
#: A scene with more triangles than this in its models makes the page draw some as boxes (``scene3d_heavy``).
HEAVY_TRIANGLES = 600_000
MAX_INTERSECT_PROBLEMS = 5
SEVERITY = {"scene3d_intersect": 0, "scene3d_labels": 1, "scene3d_relation": 1, "scene3d_floating": 3, "scene3d_heavy": 3}


# --------------------------------------------------------------------------
# items and normalize


def object_item(ctx: Any, raw: Any, field: str) -> Dict[str, Any]:
    return _spec.object_item(ctx, raw, field)


def link_item(ctx: Any, raw: Any, field: str) -> Dict[str, Any]:
    return _spec.link_item(ctx, raw, field)


def normalize(ctx: Any, spec: Dict[str, Any]) -> Dict[str, Any]:
    """Settings with their defaults, parallel link ids, and everything objects and links name checked (3.1)."""
    if spec.get("title") is not None:
        spec["title"] = _spec.text(ctx, spec["title"], "title", 200)
    else:
        spec["title"] = ""
    spec.update(_spec.settings(ctx, spec))
    objects = spec.get("objects") or []
    if not objects:
        raise ctx.invalid("objects", "a scene3d needs objects: [{\"id\": \"a\", \"shape\": \"box\"}, {\"id\": \"b\", \"shape\": \"sphere\", "
                                     "\"right_of\": \"a\"}]")
    spec["objects"] = [_spec.tidy(o) for o in objects]
    links = spec.get("links") or []
    _spec.number_links(links)
    spec["links"] = links
    _spec.check_refs(ctx, spec["objects"], links, spec.get("camera"))
    return spec


# --------------------------------------------------------------------------
# loading (outside the canvas lock) and building


def _op_objects(op: Mapping[str, Any]) -> List[Tuple[str, Any]]:
    """``(field, raw object)`` of every object whose file an op reads: a ``scene3d`` op's (an upsert re-reads them all),
    a patch's added objects and the updated ones that name their file again, and with ``relayout: "full"`` every object
    of the scene (its models re-read). A patch's update is merged over the object as it stood (``_root``), so its shape
    is known."""
    if op.get("op") == "scene3d":
        return [("objects[{}]".format(i), o) for i, o in enumerate(op.get("objects") or []) if isinstance(op.get("objects"), list)]
    root = op.get("_root") if isinstance(op.get("_root"), Mapping) else {}
    stood = [o for o in root.get("objects") or [] if isinstance(o, dict) and isinstance(o.get("id"), str)]
    current = {o["id"]: o for o in stood}
    out: List[Tuple[str, Any]] = []
    if op.get("relayout") == "full":
        out += [("objects[{}]".format(i), o) for i, o in enumerate(stood)]
    added = op.get("add") if isinstance(op.get("add"), dict) else {}
    if isinstance(added.get("objects"), list):
        out += [("add.objects[{}]".format(i), o) for i, o in enumerate(added["objects"])]
    updated = op.get("update") if isinstance(op.get("update"), dict) else {}
    for i, item in enumerate(updated.get("objects") or [] if isinstance(updated.get("objects"), list) else []):
        if not isinstance(item, dict):
            continue
        merged = dict(current.get(item.get("id"), {}), **item) if isinstance(item.get("id"), str) else dict(item)
        prim = S3.get(merged.get("shape", "box"))
        if prim is not None and prim.loader is not None and (prim.loader.field in item or op.get("relayout") == "full"):
            out.append(("update.objects[{}]".format(i), merged))
    return out


def _scene_key(objects: Sequence[Mapping[str, Any]], links: Sequence[Mapping[str, Any]], units: str, models: Mapping[str, Any]) -> str:
    raw = json.dumps([objects, links, units, {k: (v or {}).get("sha256") for k, v in sorted(models.items())}], sort_keys=True, default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def load(op: Mapping[str, Any], io: Any) -> Dict[str, Any]:
    """``Block.load``: read and check every glTF model the op names, and solve a whole scene op, before the lock (D9, D10).
    A refusal raised here refuses the op (``bctx.loaded`` re-raises it)."""
    models: Dict[str, S3.Model] = {}
    for field, raw in _op_objects(op):
        if not isinstance(raw, dict):
            continue
        prim = S3.get(raw.get("shape", "box"))
        if prim is None or prim.loader is None:
            continue
        src = raw.get(prim.loader.field)
        if not isinstance(src, str) or src in models:
            continue
        models[src] = prim.loader.read(io, raw, field)
    out: Dict[str, Any] = {"models": models, "solved": None, "key": None}
    if op.get("op") == "scene3d":
        try:
            spec = {key: op.get(key) for key in ("title",) + _spec.SETTINGS}
            spec["objects"] = [_spec.object_item(_spec.PURE, o, "objects[{}]".format(i)) for i, o in enumerate(op.get("objects") or [])]
            spec["links"] = [_spec.link_item(_spec.PURE, l, "links[{}]".format(i)) for i, l in enumerate(op.get("links") or [])]
            spec = normalize(_spec.PURE, spec)
            facts = _model_facts(spec["objects"], models, None)
            out["solved"] = _solver.solve(spec["objects"], spec["links"], spec["units"], facts)
            out["key"] = _scene_key(spec["objects"], spec["links"], spec["units"], facts)
        except HerdrTeamError:
            # The op itself reports what is wrong, under the lock, with the canvas's own refusal.
            out["solved"] = None
    return out


def _model_facts(objects: Sequence[Mapping[str, Any]], loaded: Mapping[str, Any], old: Optional[Mapping[str, Any]],
                 stored: Optional[Mapping[str, str]] = None) -> Dict[str, Dict[str, Any]]:
    """Per glTF object: its model's native size, the scale and offset that fit it, triangles and facts (the solver's
    ``models``). A model this op did not read keeps what the element stored for the same file."""
    from herdr_team.canvas_scene3d import gltf as gltf_prim

    from herdr_team.canvas_scene3d import _gltf

    def capped(index: int, native: Sequence[float], scale: float) -> None:
        # A model's placed size obeys the primitives' limits (QA phase34 L5): a node scale of 1e30 or a zero matrix
        # would read back as a 31-digit or a 0×0×0 size (and draw as something else).
        field = "objects[{}].src".format(index)
        if max(float(v) for v in native) < S3.MIN_SIZE:
            raise _gltf._refuse(field, "{}: the model has no size ({}); check its node transforms".format(
                field, "×".join("{:g}".format(float(v)) for v in native)))
        placed = [float(v) * scale for v in native]
        if max(placed) > S3.MAX_SIZE:
            raise _gltf._refuse(field, "{}: the model is {} units across as placed; the limit is {:g} (give height or size, "
                                       "or export it in metres)".format(field, "{:.3g}".format(max(placed)), S3.MAX_SIZE))

    out: Dict[str, Dict[str, Any]] = {}
    old_models = (old or {}).get("models") if isinstance((old or {}).get("models"), dict) else {}
    for index, obj in enumerate(objects):
        prim = S3.get(obj.get("shape"))
        if prim is None or prim.loader is None:
            continue
        src = obj.get(prim.loader.field)
        model = loaded.get(src) if isinstance(src, str) else None
        if model is not None:
            b = model.bounds
            native = [b[3] - b[0], b[4] - b[1], b[5] - b[2]]
            params = _spec.resolved(obj)
            scale = gltf_prim.scale(params, native)
            capped(index, native, scale)
            facts = {"src": model.src, "sha256": model.sha256, "native": _vec.snapped(native), "scale": _vec.snap(scale),
                     "offset": _vec.snapped([-(b[0] + b[3]) / 2.0 * scale, -b[1] * scale, -(b[2] + b[5]) / 2.0 * scale]),
                     "tris": int(model.triangles), "facts": dict(model.facts), "bytes": len(model.data)}
            if stored is not None and src in stored:
                facts["asset"] = stored[src]
            out[str(obj["id"])] = facts
            continue
        previous = old_models.get(str(obj["id"])) if isinstance(old_models, dict) else None
        if isinstance(previous, dict) and previous.get("src") == src:
            params = _spec.resolved(obj)
            native = previous.get("native") or [1, 1, 1]
            scale = gltf_prim.scale(params, native)
            capped(index, native, scale)
            kept = dict(previous)
            b_offset = previous.get("offset") or [0, 0, 0]
            old_scale = float(previous.get("scale") or 1) or 1.0
            kept["scale"] = _vec.snap(scale)
            kept["offset"] = _vec.snapped([float(v) / old_scale * scale for v in b_offset])
            out[str(obj["id"])] = kept
            continue
        out[str(obj["id"])] = {"src": src, "missing": True}
    return out


def _loaded(bctx: Any) -> Optional[Dict[str, Any]]:
    """``bctx.loaded`` when the canvas runs ``Block.load`` (it re-raises the load's refusal), else None."""
    try:
        found = bctx.loaded
    except AttributeError:
        return None
    return found if isinstance(found, dict) else None


def build(bctx: Any, spec: Dict[str, Any]) -> None:
    ctx = bctx.ctx
    objects = [{k: v for k, v in o.items() if not k.startswith("_")} for o in spec["objects"]]
    links = [{k: v for k, v in l.items() if not k.startswith("_")} for l in spec.get("links") or []]
    settings = {key: spec[key] for key in _spec.SETTINGS}
    loaded = _loaded(bctx) or {}
    fresh: Dict[str, S3.Model] = dict(loaded.get("models") or {})
    old = _solver.expand(bctx.root.get("solved")) if bctx.root is not None else None
    stored: Dict[str, str] = {}
    for src, model in sorted(fresh.items()):
        if not any(o.get("src") == src for o in objects):
            continue
        store = getattr(bctx, "store_asset", None)
        if store is None:
            raise ctx.invalid("objects", "glTF models need the canvas's asset store for 3D (Block.load); this build has none")
        stored[src] = store(model.data, "glb")
    facts = _model_facts(objects, fresh, old, stored)
    missing = [(i, o) for i, o in enumerate(objects) if facts.get(str(o["id"]), {}).get("missing")]
    if missing:
        index, obj = missing[0]
        raise ctx.invalid("objects[{}].src".format(index), "{} could not be read: this canvas has no file loader for 3D models (Block.load)"
                          .format(obj.get("src")))
    total = sum(int(f.get("tris") or 0) for f in facts.values())
    if total > _gltf.MAX_SCENE_TRIANGLES:
        raise ctx.too_big("objects", "MAX_SCENE_TRIANGLES", _gltf.MAX_SCENE_TRIANGLES, "the scene's models hold {:,} triangles; a scene holds "
                          "{:,} or fewer".format(total, _gltf.MAX_SCENE_TRIANGLES))
    for info in facts.values():
        info.pop("bytes", None)
    key = _scene_key(objects, links, settings["units"], facts)
    if loaded.get("key") == key and isinstance(loaded.get("solved"), dict):
        solved = loaded["solved"]
        for ident, info in facts.items():
            solved["models"][ident] = info
            if ident in solved["objects"]:
                solved["objects"][ident]["asset"] = info.get("asset")
    else:
        solved = _solver.solve(objects, links, settings["units"], facts)
    compact = _solver.compact(solved)
    root = bctx.root
    op = bctx.op
    w0, h0 = (float(root.get("w") or DEFAULT_SIZE[0]), float(root.get("h") or DEFAULT_SIZE[1])) if root is not None else DEFAULT_SIZE
    w = _size(ctx, op.get("w"), "w", w0 if root is not None else DEFAULT_SIZE[0])
    h = _size(ctx, op.get("h"), "h", h0 if root is not None else DEFAULT_SIZE[1])
    fields = {"objects": objects, "links": links, "solved": compact}
    if len(json.dumps(dict(fields, settings=settings, text=spec.get("title") or ""), separators=(",", ":"), ensure_ascii=False)
           .encode("utf-8")) > S3.MAX_ELEMENT_BYTES:
        raise ctx.too_big("objects", "MAX_SCENE3D_BYTES", S3.MAX_ELEMENT_BYTES, "the scene is over {} KB; split the scene, or use a glTF model"
                          .format(S3.MAX_ELEMENT_BYTES // 1024))
    style = None if root is not None else ctx.default_style("scene3d")
    bctx.root_fields(text=spec.get("title") or "", style=style, settings=settings, w=w, h=h, **fields)
    for note in solved.get("notes") or []:
        bctx.warn("scene3d_note", note, [])
    for conflict in solved.get("conflicts") or []:
        bctx.warn("scene3d_conflict", "{}: {}".format(conflict.get("code"), conflict.get("message")), [])


def _size(ctx: Any, value: Any, field: str, default: float) -> float:
    if value is None:
        return default
    found = _spec.number(ctx, value, field, 1, MAX_SIDE)
    return float(max(float(found), MIN_SIZE[0] if field == "w" else MIN_SIZE[1]))


# --------------------------------------------------------------------------
# readback


def spec(root: Element, members: List[Element], full: bool) -> Dict[str, Any]:
    """The op that would build the scene: its objects as stored, links as shorthand where shorthand says it all, and
    the settings that differ from their defaults."""
    out: Dict[str, Any] = {"op": "scene3d"}
    if root.get("alias"):
        out["id"] = root["alias"]
    if root.get("text"):
        out["title"] = root["text"]
    settings = root.get("settings") if isinstance(root.get("settings"), dict) else {}
    for key in _spec.SETTINGS:
        if key in settings and settings[key] != _spec.DEFAULT_SETTINGS[key]:
            out[key] = settings[key]
    out["objects"] = [dict(o) for o in root.get("objects") or [] if isinstance(o, dict)]
    links = [l for l in root.get("links") or [] if isinstance(l, dict) and isinstance(l.get("from"), str) and isinstance(l.get("to"), str)]
    renumbered = [dict(l, _auto=True) for l in links]
    _spec.number_links(renumbered)
    # Ids left out when numbering gives the same ones back (a -> b, a -> b#2 ...), else every link names its own.
    implied = [l["id"] for l in renumbered] == [l.get("id") for l in links]
    out_links: List[Any] = []
    for link in links:
        if implied and not link.get("tone"):
            out_links.append("{} -> {}".format(link["from"], link["to"]) + (": " + link["label"] if link.get("label") else ""))
            continue
        item = {"from": link["from"], "to": link["to"]}
        if not implied:
            item["id"] = link.get("id")
        for key in ("label", "tone"):
            if link.get(key):
                item[key] = link[key]
        out_links.append(item)
    if out_links:
        out["links"] = out_links
    return out


def readback(el: Element, full: bool) -> str:
    try:
        tail = _describe.summary(el)
    except Exception:  # noqa: BLE001 - readback never raises on a malformed element
        tail = "a 3D scene"
    return "{} scene3d{}{} [{},{} {}x{}] {}".format(el.get("id"), " " + str(el["alias"]) if el.get("alias") else "",
                                                 " " + quote(el.get("text"), 0 if full else 60) if el.get("text") else "",
                                                 el.get("x"), el.get("y"), el.get("w"), el.get("h"), tail)


def gist(el: Element, full: bool = False, env: Optional[Mapping[str, Any]] = None) -> List[str]:
    """The lines ``look`` prints under the scene (3.9); ``env["stills"]``, when given, adds which views the page posted."""
    stills = env.get("stills") if isinstance(env, Mapping) else None
    try:
        return _describe.gist(el, full, stills, ref=str(el.get("alias") or el.get("id") or ""))
    except Exception:  # noqa: BLE001 - a malformed element reads back as its summary, never an error
        return [readback(el, full)]


# --------------------------------------------------------------------------
# drawing


def slot_box(el: Element) -> Tuple[float, float, float, float]:
    """``[x, y, w, h]`` of the scene inside the card: below the title band, ``PAD`` in from the sides."""
    x0, y0, x1, y1 = D.box_of(el)
    band = BAND if str(el.get("text") or "").strip() else PAD
    return (x0 + PAD, y0 + band, max(1.0, (x1 - x0) - 2 * PAD), max(1.0, (y1 - y0) - band - PAD))


def draw_view(el: Element, view: str) -> Optional[List[Dict[str, Any]]]:
    """The projection of one still view at the element's size (``look --image --view``)."""
    if view not in VIEWS:
        return None
    try:
        return _project.draw(el, view, slot_box(el))
    except Exception:  # noqa: BLE001 - a malformed element draws nothing rather than failing the picture
        return None


def primary_view(el: Element) -> str:
    settings = el.get("settings") if isinstance(el.get("settings"), dict) else {}
    return _spec.primary_view(settings.get("camera"))


def emit(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """The card, the title, and one ``scene3d`` slot whose fallback is the primary view's projection (``drawn``)."""
    x0, y0, x1, y1 = D.box_of(el)
    items: List[Dict[str, Any]] = [{"k": "rect", "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "r": D.CARD_RADIUS, "fill": "base.surface",
                                    "stroke": "tone.neutral.zone_stroke", "sw": 1, "elev": 1}]
    title = str(el.get("text") or "")
    if title.strip():
        from herdr_team import canvas_geometry

        room = max(0.0, (x1 - x0) - 2 * PAD)
        line = D.text_prim([canvas_geometry.fit_line(title, room, TITLE_SIZE, TITLE_WEIGHT)], x0 + PAD,
                           y0 + (BAND - canvas_text.line_height(TITLE_SIZE)) / 2.0, TITLE_SIZE, {}, D.INK, "start", (x0 + PAD, y0, room, BAND),
                           TITLE_WEIGHT)
        line["lod"] = list(D.LOD_LABEL)
        items.append(line)
    sx, sy, sw, sh = slot_box(el)
    stills = env.get("stills") if isinstance(env, dict) else None
    views = {view: _slot.still_of(el, view, stills) for view in VIEWS}
    primary = primary_view(el)
    try:
        fallback = _project.draw(el, primary, (sx, sy, sw, sh))
        drawn = True
    except Exception:  # noqa: BLE001 - a malformed element draws the placeholder card
        fallback = D.card(dict(el, x=sx, y=sy, w=sw, h=sh), "3D scene · drawn on the page")
        drawn = False
    items.append({"k": "slot", "slot": "scene3d", "x": sx, "y": sy, "w": sw, "h": sh,
                  "ref": {"id": str(el.get("id") or ""), "v": int(D.num(el.get("updated_seq"), 0.0))},
                  "still": views[primary], "views": views, "drawn": drawn, "gl": True, "fallback": fallback})
    return items


def hit(el: Element) -> Dict[str, Any]:
    return {"shape": "rect", "box": D.xywh(D.box_of(el))}


def text_edit(el: Element) -> Optional[Dict[str, Any]]:
    """The title in its band."""
    if not str(el.get("text") or "").strip():
        return None
    x0, y0, x1, _y1 = D.box_of(el)
    lh = canvas_text.line_height(TITLE_SIZE)
    return TX.edit("text", str(el.get("text") or ""), [x0 + PAD, y0 + (BAND - lh) / 2.0, max(1.0, (x1 - x0) - 2 * PAD), lh], TITLE_SIZE,
                   TITLE_WEIGHT, D.INK, wrap="line")


def resize(el: Element, w: Optional[float], h: Optional[float], ctx: Any) -> Dict[str, Any]:
    """A new size (never below ``MIN_SIZE``); the projection scales to it and the title is cut to it."""
    out: Dict[str, Any] = {}
    if w is not None:
        out["w"] = max(MIN_SIZE[0], min(MAX_SIDE, float(w)))
    if h is not None:
        out["h"] = max(MIN_SIZE[1], min(MAX_SIDE, float(h)))
    return out


# --------------------------------------------------------------------------
# checks (4.2)


def _ref(el: Element) -> Any:
    return el.get("alias") or el.get("id")


def _problem(el: Element, code: str, message: str, fix: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    return {"code": code, "ids": [str(el.get("id"))], "message": message, "fix": fix, "severity": SEVERITY[code]}


def _patch(el: Element, body: Mapping[str, Any]) -> Dict[str, Any]:
    fix = {"op": "patch", "id": _ref(el)}
    fix.update(body)
    return fix


def _related(scene: Mapping[str, Any], a: str, b: str) -> bool:
    """Whether two objects touch by design: one rests on or sits inside the other (or what holds it), or they share a
    group, or one holds the other."""
    objects = scene["objects"]

    def chain(ident: str) -> List[str]:
        out = [ident]
        while objects.get(out[-1], {}).get("in") is not None and len(out) < 64:
            out.append(objects[out[-1]]["in"])
        return out

    ca, cb = chain(a), chain(b)
    if set(ca[1:]) & set(cb[1:]) or a in cb or b in ca:
        return True
    for x, ys in ((ca, cb), (cb, ca)):
        for ident in x:
            obj = objects.get(ident, {})
            for rel in relations.relations():
                if rel.contact and obj.get(rel.name) in ys:
                    return True
    return False


def intersect_check(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``scene3d_intersect``: two solid objects share more than 1 % of the smaller one's volume, and nothing says they
    should (``overlap: true``, one on or inside the other, one group)."""
    scene = _project.scene_of(el)
    solved = scene["solved"]
    leaves = [(ident, solved["objects"][ident]["aabb"]) for ident in solved["order"]
              if ident in scene["objects"] and not solved["objects"][ident].get("points")
              and not (S3.get(scene["objects"][ident].get("shape")) or S3.get("box")).container]
    out: List[Dict[str, Any]] = []
    for i, (a, box_a) in enumerate(leaves):
        for b, box_b in leaves[i + 1:]:
            if scene["objects"][a].get("overlap") or scene["objects"][b].get("overlap"):
                continue
            shared = _vec.overlap_volume(box_a, box_b)
            smaller = min(_vec.volume(box_a), _vec.volume(box_b))
            if shared <= 0 or shared <= 0.01 * max(smaller, 1e-12) or _related(scene, a, b):
                continue
            share = 100.0 * shared / max(smaller, 1e-12)
            change: Dict[str, Any] = {"id": b, "above": a, "gap": "s"}
            for rel in relations.of(scene["objects"][b]):
                if rel.group == "vertical" and rel.name != "above":
                    change[rel.name] = None
            if scene["objects"][b].get("pos") is not None:
                change["pos"] = None
            out.append(_problem(el, "scene3d_intersect", "in {}, {} and {} intersect ({:.0f} % of the smaller); place {} above {} (the fix), or "
                                "set overlap: true on one if it is meant".format(_ref(el), a, b, min(share, 100.0), b, a),
                                _patch(el, {"update": {"objects": [change]}})))
            if len(out) >= MAX_INTERSECT_PROBLEMS:
                return out
    return out


#: The sizes (times the card's) ``scene3d_labels`` offers as its fix, smallest first.
FIX_FACTORS = (1.25, 1.5, 2.0, 2.5, 3.0)


def labels_check(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``scene3d_labels``: labels the primary view drops (or, with ``labels: all``, draws over each other) at this size."""
    settings = el.get("settings") if isinstance(el.get("settings"), dict) else {}
    if settings.get("labels") == "none":
        return []
    # Labels only (``_project.labels``): the same placement as the picture without drawing the geometry, since this
    # runs under the canvas lock and the fix search below may try five sizes (QA phase34 M4).
    try:
        found = _project.labels(el, primary_view(el), slot_box(el))
    except Exception:  # noqa: BLE001 - a malformed element has no labels to check
        return []
    crowded = list(found.get("dropped") or []) + list(found.get("crowded") or [])
    if not crowded:
        return []
    if settings.get("labels") == "all":
        return [_problem(el, "scene3d_labels", "{} draws {} labels over each other with labels: all ({}); let the picture drop what does not fit".format(
            _ref(el), len(crowded), ", ".join(crowded[:6])), _patch(el, {"set": {"labels": "auto"}}))]
    x0, y0, x1, y1 = D.box_of(el)
    w, h = x1 - x0, y1 - y0
    # The smallest of these sizes that drops no label. A bigger card only gives the labels more room, so the largest is
    # tried first (no fix when even it drops one: two placements in all) and the rest are bisected: at most four
    # placements under the lock instead of six (QA phase34 M4).
    sizes = []
    for factor in FIX_FACTORS:
        size = (round(w * factor), round(h * factor))
        if size[0] > MAX_SIDE or size[1] > MAX_SIDE:
            break
        sizes.append(size)

    def fits(size: Tuple[int, int]) -> bool:
        bigger = dict(el, w=size[0], h=size[1])
        try:
            return not _project.labels(bigger, primary_view(el), slot_box(bigger)).get("dropped")
        except Exception:  # noqa: BLE001 - no fix rather than a failed check
            return False

    fix = None
    if sizes and fits(sizes[-1]):
        lo, hi = 0, len(sizes) - 1  # sizes[hi] fits; find the first that does
        while lo < hi:
            mid = (lo + hi) // 2
            if fits(sizes[mid]):
                hi = mid
            else:
                lo = mid + 1
        fix = {"op": "move", "id": _ref(el), "w": sizes[hi][0], "h": sizes[hi][1]}
    return [_problem(el, "scene3d_labels", "{} has no room for {} label{} in its {} view ({}); make it bigger".format(
        _ref(el), len(crowded), "" if len(crowded) == 1 else "s", primary_view(el), ", ".join(crowded[:6])), fix)]


def relation_check(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``scene3d_relation``: what the solver could not do (an object too big for its host, an overlap it could not push off)."""
    solved = _solver.expand(el.get("solved"))
    out = []
    for conflict in solved["conflicts"]:
        fix = conflict.get("fix") if isinstance(conflict.get("fix"), dict) else None
        out.append(_problem(el, "scene3d_relation", "in {}: {}".format(_ref(el), conflict.get("message")), _patch(el, fix) if fix else None))
    return out


def floating_check(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``scene3d_floating``: an object set by ``pos`` hangs above the ground with nothing under it said."""
    settings = el.get("settings") if isinstance(el.get("settings"), dict) else {}
    if settings.get("ground") is False:
        return []
    scene = _project.scene_of(el)
    solved = scene["solved"]
    out = []
    for ident in solved["order"]:
        obj = scene["objects"].get(ident) or {}
        if obj.get("pos") is None or obj.get("in") is not None:
            continue
        box = solved["objects"][ident]["aabb"]
        if box[1] <= 1e-3:
            continue
        below = None
        for other in solved["order"]:
            other_box = solved["objects"][other]["aabb"]
            if other == ident or solved["objects"][other].get("points") or (scene["objects"].get(other) or {}).get("in") is not None:
                continue
            if other_box[4] <= box[1] + 1e-6 and other_box[0] < box[3] and box[0] < other_box[3] and other_box[2] < box[5] and box[2] < other_box[5]:
                if below is None or other_box[4] > solved["objects"][below]["aabb"][4]:
                    below = other
        change: Dict[str, Any] = {"id": ident}
        if below is not None:
            change.update(pos=None, on=below)
        else:
            change["pos"] = [obj["pos"][0], 0, obj["pos"][2]]
        out.append(_problem(el, "scene3d_floating", "in {}, {} floats {:.2f} above {}; rest it on something".format(
            _ref(el), ident, box[1] - (solved["objects"][below]["aabb"][4] if below else 0.0), below or "the ground"),
            _patch(el, {"update": {"objects": [change]}})))
    return out


def heavy_check(el: Element, env: Dict[str, Any]) -> List[Dict[str, Any]]:
    """``scene3d_heavy``: the models hold more triangles than the page draws in full (it proxies some)."""
    solved = _solver.expand(el.get("solved"))
    total = sum(int(m.get("tris") or 0) for m in solved["models"].values() if isinstance(m, dict) and _spec.is_number(m.get("tris")))
    if total <= HEAVY_TRIANGLES:
        return []
    return [_problem(el, "scene3d_heavy", "{} holds {:,} triangles in its models; the page draws some as boxes past {:,}".format(
        _ref(el), total, HEAVY_TRIANGLES), None)]


# --------------------------------------------------------------------------
# registration


def create(ctx: Any, op: Dict[str, Any]) -> None:
    ctx.block("scene3d", op)


SETTINGS = _spec.SETTINGS
#: The op's line in the MCP table, from the registries (the shapes and relations as registered at import).
MCP = ("scene3d {title, objects [{id, shape " + "|".join(S3.names()) + ", size|radius|height, tone, label, "
       + "|".join(relations.names()) + ": <id>, in: <group>, gap}], links [\"a -> b: label\"], camera "
       + "|".join(_spec.CAMERA_PRESETS) + "}")

OPS = (
    OpSpec(name="scene3d", family="3d", fields=("title", "objects", "links") + SETTINGS + ("w", "h", "id", "client_id"), create=create, place=True, order=95,
           doc="a 3D scene: objects placed by relations (on, right_of, inside ...), links, camera; glTF from artifacts/", mcp=MCP),
)

KINDS = (
    Kind(name="scene3d", version=KIND_VERSION, role="composite", ops=("scene3d",), solid=True, cell=True, connectable=True, handles="box",
         slot="scene3d", page=False, edit_limit="label", noun=("3D scene", "3D scenes"),
         block=Block(collections=(
             Collection(name="objects", item=object_item, maximum=S3.MAX_OBJECTS, doc="the things in the scene, placed by relations",
                        refs=(("in", "objects", "clear"),) + tuple((name, "objects", "clear") for name in relations.names())
                        + (("from", "objects", "drop"), ("to", "objects", "drop"))),
             Collection(name="links", item=link_item, remove_key=_spec.link_remove_key, maximum=S3.MAX_LINKS,
                        doc="\"a -> b: label\" arrows between objects", refs=(("from", "objects", "drop"), ("to", "objects", "drop")))),
             settings=SETTINGS, parts="inline", positional=False, normalize=normalize, build=build, spec=spec, load=load, fields=("title",)),
         emit=emit, hit=hit, text_edit=text_edit, resize=resize, readback=readback,
         checks=(intersect_check, labels_check, relation_check, floating_check, heavy_check),
         still_views=VIEWS, gist=gist, gist_env=True, gist_lines=_describe.GIST_LINES, gist_first=True, draw_view=draw_view, look_json=("scene3d", _describe.facts),
         doc="a 3D scene of primitives and glTF models placed by relations; the page renders it, agents read it as text"),
)
