"""Server-side projections of a solved scene (canvas v2 phase 4, 3.8): the agent's picture without a browser.

``project(el, view, box)`` draws the scene an element holds, seen from one of
the still views (``iso``, ``front``, ``top``), as display-list primitives inside
``box`` (the slot's ``[x, y, w, h]`` in world units):

1. **Camera.** Orthographic, from ``tokens.scene3d.cameras[view]`` (``az``,
   ``el``), framing the solved bounds with a 6 % margin (and room for labels),
   uniform scale, y flipped for SVG. The camera setting's ``zoom`` scales that
   fit and ``target`` (an object or a point) centres it.
2. **Geometry.** Boxes, planes and glTF proxies are their faces; spheres,
   cylinders and cones their closed-form outlines (``Primitive.silhouette``);
   groups are their children; ``text3d`` is text at its projected centre;
   links and ``arrow3d`` are ``arrow`` primitives drawn over the solids.
3. **Order.** Back faces are culled by their normal. Objects are drawn far to
   near: an object whose box lies wholly on the far side of another along an
   axis the camera looks along is drawn first (the painter's order of
   separated boxes); where boxes do not separate, by the depth of their
   centres; ties by spec order. Within an object, faces go far to near by their
   centroid's depth, ties by face index. (Sorting every face by its centroid
   alone would draw a floor over what stands on it.)
4. **Shading.** A face's normal picks ``mat.<tone>.top``, ``left`` or ``right``
   (up, facing left or right on the picture); edges are ``mat.<tone>.edge`` at
   ``sw_px`` 1. ``glass`` and ``opacity`` draw at ``op``. glTF proxies have
   dashed edges.
5. **Labels.** 12-unit text on a pill above each labelled object's projected
   top; a label that meets another moves up 8 units at a time (4 times), then
   under its object, then beside it on a leader line (right, then left). A spot
   that covers no other object (what holds the labelled one up aside), leader
   included, wins over one that does. A label that meets another label
   everywhere is dropped from the picture (``labels: all`` keeps it) and
   reported for ``scene3d_labels``.
6. **Ground.** A grid in ``base.grid`` under everything, over the bounds'
   footprint.
7. **Budget.** At most ``MAX_PRIMS`` primitives: past it, the smallest objects
   draw as one outline each.

The geometry is one ``group`` (clipped to ``box`` when a zoomed or re-centred
camera reaches past it); the labels follow it, unclipped (they are placed
inside the box). The result is
cached by the element's content, view and box (``CACHE_SIZE`` entries), since
emit runs for every display list.
"""
from __future__ import annotations

import hashlib
import json
import math
from collections import OrderedDict
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team import canvas_display as D
from herdr_team import canvas_text, canvas_theme
from herdr_team import canvas_scene3d as S
from herdr_team.canvas_scene3d import _solver, _spec, _tokens, _vec as V

VIEWS = ("iso", "front", "top")
MARGIN = 0.06
MAX_PRIMS = 3000
CACHE_SIZE = 64
#: Labels are world-size text like every other label on the canvas, so the collision test at the element's size holds at
#: every zoom: ``scene3d.label.size_px`` (12) units.
LABEL_SIZE = _tokens.label_px()
LABEL_WEIGHT = 500
LABEL_PAD = 4.0
LABEL_STEP = 8.0
LABEL_TRIES = 4
LEADER = 16.0
HEAD_LEN = 8.0
HEAD_W = 6.0

P2 = Tuple[float, float]


class View:
    """An orthographic camera fitted to a box: ``p`` maps a world point to the picture, ``depth`` how far it is."""

    def __init__(self, az: float, el: float) -> None:
        a, e = math.radians(az), math.radians(el)
        self.eye: V.Vec = (math.cos(e) * math.sin(a), math.sin(e), math.cos(e) * math.cos(a))
        self.right: V.Vec = (math.cos(a), 0.0, -math.sin(a))
        self.up: V.Vec = V.cross(self.eye, self.right)
        self.scale = 1.0
        self.ox = 0.0
        self.oy = 0.0

    def raw(self, p: Sequence[float]) -> P2:
        return (V.dot(p, self.right), V.dot(p, self.up))

    def fit(self, points: Sequence[Sequence[float]], box: Sequence[float], zoom: float = 1.0, centre: Optional[Sequence[float]] = None,
            top_room: float = 0.0) -> None:
        raws = [self.raw(p) for p in points] or [(0.0, 0.0)]
        x0, x1 = min(r[0] for r in raws), max(r[0] for r in raws)
        y0, y1 = min(r[1] for r in raws), max(r[1] for r in raws)
        bx, by, bw, bh = box
        avail_w = max(1.0, bw * (1 - 2 * MARGIN))
        avail_h = max(1.0, bh * (1 - 2 * MARGIN) - top_room)
        span_w, span_h = x1 - x0, y1 - y0
        candidates = [avail_w / span_w if span_w > 1e-9 else None, avail_h / span_h if span_h > 1e-9 else None]
        found = [c for c in candidates if c is not None]
        self.scale = (min(found) if found else 1.0) * zoom
        if centre is not None:
            cx, cy = self.raw(centre)
        else:
            cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        self.ox = bx + bw / 2.0 - cx * self.scale
        self.oy = by + top_room + (bh - top_room) / 2.0 + cy * self.scale

    def p(self, point: Sequence[float]) -> P2:
        rx, ry = self.raw(point)
        return (self.ox + rx * self.scale, self.oy - ry * self.scale)

    def depth(self, point: Sequence[float]) -> float:
        return -V.dot(point, self.eye)


class ObjectProj:
    """``proj`` for one object: its local frame (centred on x and z, base at y = 0) to the picture."""

    def __init__(self, view: View, pos: Sequence[float], rot: Sequence[float], local_h: float, ext_h: float) -> None:
        self.view = view
        self.m = V.rotation(rot)
        self.pivot_local = (0.0, local_h / 2.0, 0.0)
        self.pivot_world = (float(pos[0]), float(pos[1]) + ext_h / 2.0, float(pos[2]))

    def world(self, local: Sequence[float]) -> V.Vec:
        return V.add(V.apply(self.m, V.sub(local, self.pivot_local)), self.pivot_world)

    def p(self, local: Sequence[float]) -> P2:
        return self.view.p(self.world(local))

    def facing(self, direction: Sequence[float]) -> float:
        return V.dot(V.apply(self.m, direction), self.view.eye)


# --------------------------------------------------------------------------
# the scene an element holds


def scene_of(el: Mapping[str, Any]) -> Dict[str, Any]:
    """``{"objects": {id: resolved object}, "order", "solved", "links", "settings"}`` from a stored element (junk-tolerant)."""
    solved = _solver.expand(el.get("solved"))
    raw = [o for o in el.get("objects") or [] if isinstance(o, dict) and isinstance(o.get("id"), str)]
    objects: Dict[str, Dict[str, Any]] = {}
    for obj in raw:
        try:
            found = _spec.resolved(obj)
        except Exception:  # noqa: BLE001 - a malformed stored object draws as a box, never an error
            found = dict(obj, shape="box")
        model = solved["models"].get(obj["id"])
        if isinstance(model, dict):
            found["model"] = model
        objects[obj["id"]] = found
    settings = el.get("settings") if isinstance(el.get("settings"), dict) else {}
    return {"objects": objects, "solved": solved, "settings": dict(_spec.DEFAULT_SETTINGS, **settings)}


def _shade(n: Sequence[float], view: View) -> str:
    if abs(n[1]) >= max(abs(n[0]), abs(n[2])) - 1e-9:
        return "top" if n[1] > 0 else "right"
    return "right" if V.dot(n, view.right) > 1e-6 else "left"


def _ground(scene: Mapping[str, Any], view: View) -> List[Dict[str, Any]]:
    bounds = scene["solved"]["bounds"]
    x0, z0, x1, z1 = bounds[0], bounds[2], bounds[3], bounds[5]
    span = max(x1 - x0, z1 - z0)
    if span <= 1e-6:
        return []
    raw = span / 10.0
    power = 10 ** math.floor(math.log10(raw))
    step = next(k * power for k in (1, 2, 5, 10) if k * power >= raw)
    y = min(0.0, bounds[1])
    lines: List[Tuple[V.Vec, V.Vec]] = []
    xs = [x0] + [k * step for k in range(int(math.ceil(x0 / step)), int(math.floor(x1 / step)) + 1) if x0 < k * step < x1] + [x1]
    zs = [z0] + [k * step for k in range(int(math.ceil(z0 / step)), int(math.floor(z1 / step)) + 1) if z0 < k * step < z1] + [z1]
    lines += [((x, y, z0), (x, y, z1)) for x in xs]
    lines += [((x0, y, z), (x1, y, z)) for z in zs]
    out: List[Dict[str, Any]] = []
    seen = set()
    for a, b in lines:
        pa, pb = view.p(a), view.p(b)
        if math.hypot(pb[0] - pa[0], pb[1] - pa[1]) < 0.5:
            continue
        ends = sorted([pa, pb])
        key = (round(ends[0][0], 1), round(ends[0][1], 1), round(ends[1][0], 1), round(ends[1][1], 1))
        if key in seen:
            continue
        seen.add(key)
        out.append({"k": "line", "points": [[pa[0], pa[1]], [pb[0], pb[1]]], "stroke": "base.grid", "sw_px": 1})
    return out


def _behind(a: Mapping[str, Any], b: Mapping[str, Any], eye: V.Vec) -> Optional[bool]:
    """Whether box ``a`` must be drawn before box ``b`` (True), after it (False), or either (None)."""
    best: Optional[Tuple[float, bool]] = None
    for axis in range(3):
        weight = abs(eye[axis])
        if weight < 1e-6:
            continue
        if a["box"][axis + 3] <= b["box"][axis] + 1e-9:
            first = eye[axis] > 0  # a is on the low side: it is farther when the eye looks from the high side
        elif b["box"][axis + 3] <= a["box"][axis] + 1e-9:
            first = eye[axis] < 0
        else:
            continue
        if best is None or weight > best[0]:
            best = (weight, first)
    if best is not None:
        return best[1]
    if any(a["box"][axis + 3] <= b["box"][axis] + 1e-9 or b["box"][axis + 3] <= a["box"][axis] + 1e-9 for axis in range(3)):
        return None
    return a["depth"] > b["depth"] if abs(a["depth"] - b["depth"]) > 1e-9 else None


def _rects_meet(a: Sequence[float], b: Sequence[float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def draw_order(items: List[Dict[str, Any]], view: View) -> List[Dict[str, Any]]:
    """Far to near (3.8 point 3): separated boxes by which side of each other they are on, the rest by centre depth."""
    n = len(items)
    after: List[List[int]] = [[] for _ in range(n)]
    waiting = [0] * n
    for i in range(n):
        for j in range(i + 1, n):
            if not _rects_meet(items[i]["rect"], items[j]["rect"]):
                continue
            found = _behind(items[i], items[j], view.eye)
            if found is None:
                continue
            first, second = (i, j) if found else (j, i)
            after[first].append(second)
            waiting[second] += 1
    import heapq

    heap = [(-items[i]["depth"], items[i]["index"], i) for i in range(n) if waiting[i] == 0]
    heapq.heapify(heap)
    done = [False] * n
    out: List[Dict[str, Any]] = []
    while len(out) < n:
        if not heap:
            # A cycle of mutually overlapping boxes: the farthest one left goes first.
            i = max((k for k in range(n) if not done[k]), key=lambda k: (items[k]["depth"], -items[k]["index"]))
            waiting[i] = 0
            heap.append((-items[i]["depth"], items[i]["index"], i))
        _d, _index, i = heapq.heappop(heap)
        if done[i]:
            continue
        done[i] = True
        out.append(items[i])
        for k in after[i]:
            waiting[k] -= 1
            if waiting[k] == 0 and not done[k]:
                heapq.heappush(heap, (-items[k]["depth"], items[k]["index"], k))
    return out


def _paints(obj: Mapping[str, Any]) -> Tuple[str, Optional[float]]:
    tone = obj.get("tone") if isinstance(obj.get("tone"), str) else "neutral"
    opacity = obj.get("opacity") if _spec.is_number(obj.get("opacity")) else None
    if obj.get("finish") == "glass":
        glass = _tokens.finish("glass").get("opacity", 0.35)
        opacity = min(float(opacity), float(glass)) if opacity is not None else float(glass)
    return tone, opacity


def _solid(obj: Mapping[str, Any], entry: Mapping[str, Any], view: View, index: int) -> Optional[Dict[str, Any]]:
    """What one leaf object draws, with the box and depth ``draw_order`` sorts it by."""
    prim = S.get(obj.get("shape")) or S.get("box")
    if prim is None or prim.container or obj.get("shape") == "arrow3d":
        return None
    box = tuple(entry["aabb"])
    corners = [view.p(c) for c in V.corners(box)]
    rect = (min(c[0] for c in corners), min(c[1] for c in corners), max(c[0] for c in corners), max(c[1] for c in corners))
    local = prim.extent(obj)
    proj = ObjectProj(view, entry["pos"], entry["rot"], float(local[1]), float(entry["ext"][1]))
    tone, opacity = _paints(obj)
    edge = _tokens.mat_ref(tone, "edge")
    prims: List[Dict[str, Any]] = []
    if obj.get("shape") == "text3d":
        centre = view.p(V.box_center(box))
        size = max(_tokens.label_min_px(), float(obj.get("height") or 0.3) * view.scale)
        text = str(obj.get("text") or "")
        prims.append(D.text_prim([text], centre[0], centre[1] - canvas_text.line_height(size) / 2.0, size, {}, D.INK, "middle", None, 500))
    elif prim.silhouette is not None:
        for outline in prim.silhouette(obj, proj):
            face = "top" if outline.get("part") == "cap" else "left"
            item = {"k": "path", "d": outline["d"], "fill": _tokens.mat_ref(tone, face), "stroke": edge, "sw_px": 1}
            prims.append(item)
    else:
        faces = []
        for f_index, face in enumerate(prim.faces(obj, S.DETAIL)):
            normal = V.apply(proj.m, face.normal)
            if V.dot(normal, view.eye) <= 1e-6:
                continue
            world = [proj.world(p) for p in face.points]
            centroid = V.mul(V.add(V.add(world[0], world[1]), V.add(world[2], world[-1])), 0.25)
            faces.append((-view.depth(centroid), f_index, [view.p(p) for p in world], _shade(normal, view)))
        faces.sort(key=lambda f: (f[0], f[1]))
        for _depth, _i, points, shade in faces:
            item = {"k": "poly", "points": [[p[0], p[1]] for p in points], "closed": True, "fill": _tokens.mat_ref(tone, shade),
                    "stroke": edge, "sw_px": 1}
            if obj.get("shape") == "gltf":
                item["dash"] = [4, 3]
            prims.append(item)
    if opacity is not None and opacity < 1:
        for item in prims:
            item["op"] = round(float(opacity), 2)
    area = (rect[2] - rect[0]) * (rect[3] - rect[1])
    return {"id": obj["id"], "index": index, "box": box, "rect": rect, "depth": view.depth(V.box_center(box)), "prims": prims, "area": area,
            "edge": edge}


def _arrow(points: Sequence[P2], stroke: str) -> Optional[Dict[str, Any]]:
    (ax, ay), (bx, by) = points
    length = math.hypot(bx - ax, by - ay)
    if length < 2.0:
        return None
    ux, uy = (bx - ax) / length, (by - ay) / length
    head = min(HEAD_LEN, length * 0.5)
    base = (bx - ux * head, by - uy * head)
    half = HEAD_W / 2.0 * head / HEAD_LEN
    tri = [[bx, by], [base[0] - uy * half, base[1] + ux * half], [base[0] + uy * half, base[1] - ux * half]]
    return {"k": "arrow", "d": "M {} {} L {} {}".format(D.fmt(ax), D.fmt(ay), D.fmt(base[0]), D.fmt(base[1])), "stroke": stroke,
            "sw_px": 1.5, "heads": [{"at": "end", "shape": "triangle", "points": tri}]}


def _label_rect(text: str, cx: float, bottom: float) -> Tuple[float, float, float, float]:
    width = canvas_text.measure(text, "normal", LABEL_SIZE, LABEL_WEIGHT).width + 2 * LABEL_PAD
    height = canvas_text.line_height(LABEL_SIZE)
    return (cx - width / 2.0, bottom - height, cx + width / 2.0, bottom)


def _label_prims(text: str, rect: Sequence[float], leader: Optional[Tuple[P2, P2]]) -> List[Dict[str, Any]]:
    x0, y0, x1, y1 = rect
    out: List[Dict[str, Any]] = []
    if leader is not None:
        (ax, ay), (bx, by) = leader
        out.append({"k": "line", "points": [[ax, ay], [bx, by]], "stroke": D.MUTED, "sw_px": 1, "lod": list(D.LOD_LABEL)})
    out.append({"k": "rect", "x": x0, "y": y0, "w": x1 - x0, "h": y1 - y0, "r": 4, "fill": "base.surface", "op": 0.85,
                "lod": list(D.LOD_LABEL)})
    prim = D.text_prim([text], (x0 + x1) / 2.0, y0, LABEL_SIZE, {}, D.INK, "middle", (x0, y0, x1 - x0, y1 - y0), LABEL_WEIGHT)
    prim["lod"] = list(D.LOD_LABEL)
    out.append(prim)
    return out


def _place_label(text: str, anchor: P2, placed: List[Sequence[float]], box: Sequence[float], keep: bool,
                 avoid: Sequence[Sequence[float]] = (), outline: Optional[Sequence[float]] = None
                 ) -> Tuple[Optional[Tuple[float, float, float, float]], Optional[Tuple[P2, P2]], bool]:
    """Where a label goes (3.8 point 5): above its object, moved up in steps (with a line back once it is well clear),
    then under it, then beside it on a leader, right then left (``outline``: the object's ``x0, y0, x1, y1`` on the
    picture). A spot clear of ``avoid`` (every other object but what holds this one up), leader line included, is
    preferred; only other labels and the box's edge rule a spot out. None when every spot meets another label
    (``keep``: the first spot anyway, and the last value says it is crowded)."""
    bx0, by0, bx1, by1 = box[0], box[1], box[0] + box[2], box[1] + box[3]

    def inside(rect: Sequence[float]) -> bool:
        return rect[0] >= bx0 - 0.01 and rect[1] >= by0 - 0.01 and rect[2] <= bx1 + 0.01 and rect[3] <= by1 + 0.01

    def clear(rect: Sequence[float], leader: Optional[Tuple[P2, P2]]) -> bool:
        if any(_rects_meet(rect, other) for other in avoid):
            return False
        return leader is None or not any(_segment_meets(leader[0], leader[1], other) for other in avoid)

    spots = _label_spots(text, anchor, box, outline)
    # Only what lies near the spots can meet them: one pass narrows both lists (the scene's labels grow quadratically).
    reach = (min(r[0] for r, _l in spots) - 1, min(r[1] for r, _l in spots) - 1, max(r[2] for r, _l in spots) + 1,
             max(r[3] for r, _l in spots) + 1)
    placed = [other for other in placed if _rects_meet(reach, other)]
    avoid = [other for other in avoid if _rects_meet(reach, other)]
    for strict in ((True, False) if avoid else (False,)):
        for rect, leader in spots:
            if inside(rect) and not any(_rects_meet(rect, other) for other in placed) and not (strict and not clear(rect, leader)):
                return rect, leader, False
    first = spots[0][0]
    return (first if keep else None), None, keep


def _segment_meets(a: P2, b: P2, rect: Sequence[float], inset: float = 2.0) -> bool:
    """Whether the segment ``a``–``b`` passes through ``rect`` shrunk by ``inset`` (Liang–Barsky); a leader may start at
    its own object's edge, so touching another's border does not count."""
    x0, y0, x1, y1 = rect[0] + inset, rect[1] + inset, rect[2] - inset, rect[3] - inset
    if x0 >= x1 or y0 >= y1:
        return False
    dx, dy = b[0] - a[0], b[1] - a[1]
    t0, t1 = 0.0, 1.0
    for p, q in ((-dx, a[0] - x0), (dx, x1 - a[0]), (-dy, a[1] - y0), (dy, y1 - a[1])):
        if abs(p) < 1e-12:
            if q < 0:
                return False
            continue
        t = q / p
        if p < 0:
            t0 = max(t0, t)
        else:
            t1 = min(t1, t)
        if t0 > t1:
            return False
    return True


def _label_spots(text: str, anchor: P2, box: Sequence[float], outline: Optional[Sequence[float]]
                 ) -> List[Tuple[Tuple[float, float, float, float], Optional[Tuple[P2, P2]]]]:
    """The spots a label tries, in order, each with its leader line (or None)."""
    bx0, bx1 = box[0], box[0] + box[2]
    first = _label_rect(text, anchor[0], anchor[1] - LABEL_PAD)
    # Kept inside the box sideways before anything else: a label near an edge slides in.
    shift = max(0.0, bx0 - first[0]) - max(0.0, first[2] - bx1)
    first = (first[0] + shift, first[1], first[2] + shift, first[3])
    height, width = first[3] - first[1], first[2] - first[0]
    out: List[Tuple[Tuple[float, float, float, float], Optional[Tuple[P2, P2]]]] = []
    for step in range(LABEL_TRIES + 1):
        rect = (first[0], first[1] - step * LABEL_STEP, first[2], first[3] - step * LABEL_STEP)
        out.append((rect, (anchor, ((rect[0] + rect[2]) / 2.0, rect[3])) if step >= 2 else None))
    ox0, oy0, ox1, oy1 = outline if outline is not None else (anchor[0], anchor[1], anchor[0], anchor[1])
    for step in range(3):
        top = oy1 + LABEL_PAD + step * LABEL_STEP
        out.append(((first[0], top, first[2], top + height), None))
    mid = (oy0 + oy1) / 2.0
    for dy in (0.0, -LABEL_STEP, LABEL_STEP):
        cy = mid + dy
        right = (ox1 + LEADER, cy - height / 2.0, ox1 + LEADER + width, cy + height / 2.0)
        out.append((right, ((ox1 - min(LEADER, (ox1 - ox0) / 2.0), cy), (right[0], cy))))
    for dy in (0.0, -LABEL_STEP, LABEL_STEP):
        cy = mid + dy
        left = (ox0 - LEADER - width, cy - height / 2.0, ox0 - LEADER, cy + height / 2.0)
        out.append((left, ((ox0 + min(LEADER, (ox1 - ox0) / 2.0), cy), (left[2], cy))))
    return out


def _supports(scene: Mapping[str, Any], ident: str) -> set:
    """``ident`` and what holds it up: the groups it is in, what it rests on or sits inside (``on``, ``inside``), and theirs.
    A label may cover these; it keeps clear of every other object when it can."""
    from herdr_team.canvas_scene3d import relations

    out: set = set()
    todo = [ident]
    while todo and len(out) < 256:
        current = todo.pop()
        if current in out:
            continue
        out.add(current)
        obj = scene["objects"].get(current) or {}
        if isinstance(obj.get("in"), str):
            todo.append(obj["in"])
        for rel in relations.relations():
            if rel.contact and isinstance(obj.get(rel.name), str):
                todo.append(obj[rel.name])
    return out


def _cache_key(el: Mapping[str, Any], view: str, box: Sequence[float]) -> str:
    raw = json.dumps([el.get("objects"), el.get("links"), el.get("solved"), el.get("settings"), view, [round(float(v), 3) for v in box]],
                     sort_keys=True, default=str)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()


#: Projections by content, view and box, as JSON text: a hit is one parse, and no caller can change what is cached.
_CACHE: "OrderedDict[str, str]" = OrderedDict()


def project(el: Mapping[str, Any], view: str, box: Sequence[float]) -> Dict[str, Any]:
    """``{"items": [the geometry's group, then the labels], "dropped": [label ids], "crowded": [ids drawn over another
    with labels: all], "count": primitives, "scale": picture units per scene unit, "labels": labels drawn}``."""
    view = view if view in VIEWS else "iso"
    key = _cache_key(el, view, box)
    found = _CACHE.get(key)
    if found is None:
        found = json.dumps(_project(el, view, box), separators=(",", ":"))
        _CACHE[key] = found
        while len(_CACHE) > CACHE_SIZE:
            _CACHE.popitem(last=False)
    else:
        _CACHE.move_to_end(key)
    return json.loads(found)


def _clear_cache() -> None:
    _CACHE.clear()


def _fitted(el: Mapping[str, Any], view_name: str, box: Sequence[float]) -> Tuple[Dict[str, Any], "View", str, float, Optional[Sequence[float]]]:
    """The scene, its camera fitted to ``box``, the label mode, the zoom and the re-centred target (if any)."""
    scene = scene_of(el)
    solved = scene["solved"]
    settings = scene["settings"]
    cam = _tokens.camera(view_name)
    view = View(float(cam.get("az", 45)), float(cam.get("el", 35.264)))
    camera = settings.get("camera")
    zoom = float(camera.get("zoom", 1.0)) if isinstance(camera, dict) and _spec.is_number(camera.get("zoom")) else 1.0
    target = camera.get("target") if isinstance(camera, dict) else None
    centre: Optional[Sequence[float]] = None
    if isinstance(target, str) and target in solved["objects"]:
        centre = V.box_center(solved["objects"][target]["aabb"])
    elif isinstance(target, list) and len(target) == 3 and all(_spec.is_number(v) for v in target):
        centre = [float(v) for v in target]
    labels_mode = settings.get("labels") if settings.get("labels") in _spec.LABEL_MODES else "auto"
    points: List[Sequence[float]] = []
    for ident in solved["order"]:
        entry = solved["objects"][ident]
        if solved["objects"][ident].get("points"):
            points += entry["points"]
        elif ident in scene["objects"] and not (S.get(scene["objects"][ident].get("shape")) or S.get("box")).container:
            points += V.corners(entry["aabb"])
    for link in solved["links"]:
        points += link["points"]
    if settings.get("ground", True) and solved["order"]:
        b = solved["bounds"]
        points += [(b[0], min(0.0, b[1]), b[2]), (b[3], min(0.0, b[1]), b[5])]
    has_labels = labels_mode != "none" and any(o.get("label") for o in scene["objects"].values()) or labels_mode == "all"
    view.fit(points, box, zoom, centre, top_room=canvas_text.line_height(LABEL_SIZE) + 2 * LABEL_PAD if has_labels else 0.0)
    return scene, view, labels_mode, zoom, centre


def _solid_rect(obj: Mapping[str, Any], entry: Mapping[str, Any], view: "View") -> Optional[Tuple[float, float, float, float]]:
    """The picture rectangle ``_solid`` gives a leaf object (its AABB's projected corners), without drawing it; None for
    what draws no solid (a group, an arrow)."""
    prim = S.get(obj.get("shape")) or S.get("box")
    if prim is None or prim.container or obj.get("shape") == "arrow3d":
        return None
    corners = [view.p(c) for c in V.corners(tuple(entry["aabb"]))]
    return (min(c[0] for c in corners), min(c[1] for c in corners), max(c[0] for c in corners), max(c[1] for c in corners))


def _place_labels(scene: Mapping[str, Any], view: "View", box: Sequence[float], labels_mode: str,
                  rects: Sequence[Tuple[str, Sequence[float]]]) -> Tuple[List[Dict[str, Any]], List[str], List[str], int]:
    """Every object and link label placed (3.8 point 5) against the solids' ``(id, rect)``: ``(label primitives, dropped
    ids, crowded ids, labels drawn)``. The picture and the ``scene3d_labels`` check both come through here."""
    solved = scene["solved"]
    dropped: List[str] = []
    crowded: List[str] = []
    placed: List[Sequence[float]] = []
    label_items: List[Dict[str, Any]] = []
    if labels_mode == "none":
        return label_items, dropped, crowded, 0
    held: Dict[str, set] = {}
    for other in solved["order"]:
        holder = (scene["objects"].get(other) or {}).get("in")
        seen = 0
        while isinstance(holder, str) and seen < 64:
            held.setdefault(holder, set()).add(other)
            holder = (scene["objects"].get(holder) or {}).get("in")
            seen += 1
    for ident in solved["order"]:
        obj = scene["objects"].get(ident) or {}
        text = obj.get("label") or (ident if labels_mode == "all" else None)
        if not text or ident not in solved["objects"]:
            continue
        b = solved["objects"][ident]["aabb"]
        top = view.p(((b[0] + b[3]) / 2.0, b[4], (b[2] + b[5]) / 2.0))
        corners = [view.p(c) for c in V.corners(b)]
        anchor = (top[0], min(r[1] for r in corners))
        own = _supports(scene, ident) | held.get(ident, set())
        avoid = [rect for rid, rect in rects if rid not in own]
        outline = (min(r[0] for r in corners), min(r[1] for r in corners), max(r[0] for r in corners), max(r[1] for r in corners))
        rect, leader, crowd = _place_label(str(text), anchor, placed, box, labels_mode == "all", avoid, outline)
        if crowd:
            crowded.append(ident)
        if rect is None:
            dropped.append(ident)
            continue
        placed.append(rect)
        label_items += _label_prims(str(text), rect, leader)
    for link in solved["links"]:
        if not link.get("label"):
            continue
        a, b = (view.p(p) for p in link["points"])
        mid = ((a[0] + b[0]) / 2.0, (a[1] + b[1]) / 2.0 + canvas_text.line_height(LABEL_SIZE) / 2.0)
        rect, leader, crowd = _place_label(str(link["label"]), mid, placed, box, labels_mode == "all")
        if crowd:
            crowded.append(link["key"])
        if rect is None:
            dropped.append(link["key"])
            continue
        placed.append(rect)
        label_items += _label_prims(str(link["label"]), rect, leader)
    return label_items, dropped, crowded, len(placed)


def labels(el: Mapping[str, Any], view: str, box: Sequence[float]) -> Dict[str, Any]:
    """``{"dropped": [ids], "crowded": [ids]}`` exactly as ``project`` finds them, without drawing the geometry: the
    labels only need each solid's rectangle, so the ``scene3d_labels`` check (and its search for a size that fits, under
    the canvas lock) costs a fraction of a projection (QA phase34 M4). Cached like ``project``."""
    view = view if view in VIEWS else "iso"
    key = _cache_key(el, view, box) + ":labels"
    found = _CACHE.get(key)
    if found is None:
        full = _CACHE.get(_cache_key(el, view, box))
        if full is not None:
            got = json.loads(full)
            result = {"dropped": got["dropped"], "crowded": got["crowded"]}
        else:
            scene, fitted, labels_mode, _zoom, _centre = _fitted(el, view, box)
            solved = scene["solved"]
            rects = []
            for ident in solved["order"]:
                obj = scene["objects"].get(ident)
                if obj is None:
                    continue
                rect = _solid_rect(obj, solved["objects"][ident], fitted)
                if rect is not None:
                    rects.append((ident, rect))
            _items, dropped, crowded, _count = _place_labels(scene, fitted, box, labels_mode, rects)
            result = {"dropped": dropped, "crowded": crowded}
        found = json.dumps(result, separators=(",", ":"))
        _CACHE[key] = found
        while len(_CACHE) > CACHE_SIZE:
            _CACHE.popitem(last=False)
    else:
        _CACHE.move_to_end(key)
    return json.loads(found)


def _project(el: Mapping[str, Any], view_name: str, box: Sequence[float]) -> Dict[str, Any]:
    scene, view, labels_mode, zoom, centre = _fitted(el, view_name, box)
    solved = scene["solved"]
    settings = scene["settings"]
    items: List[Dict[str, Any]] = []
    if settings.get("ground", True) and solved["order"]:
        items += _ground(scene, view)
    solids = []
    for index, ident in enumerate(solved["order"]):
        obj = scene["objects"].get(ident)
        entry = solved["objects"][ident]
        if obj is None:
            continue
        found = _solid(obj, entry, view, index)
        if found is not None:
            solids.append(found)
    ordered = draw_order(solids, view)
    geometry = sum(len(s["prims"]) for s in ordered)
    budget = MAX_PRIMS - len(items) - 4 * (len(solved["links"]) + len(scene["objects"]))
    if geometry > budget:
        for solid in sorted(ordered, key=lambda s: (s["area"], s["index"])):
            if geometry <= budget:
                break
            r = solid["rect"]
            geometry -= len(solid["prims"]) - 1
            solid["prims"] = [{"k": "rect", "x": r[0], "y": r[1], "w": r[2] - r[0], "h": r[3] - r[1], "fill": None, "stroke": solid["edge"],
                               "sw_px": 1}]
    for solid in ordered:
        items += solid["prims"]
    for link in solved["links"]:
        tone = link.get("tone") if link.get("tone") in canvas_theme.TONES else None
        stroke = "tone.{}.stroke".format(tone) if tone and tone != "neutral" else "base.line"
        arrow = _arrow([view.p(p) for p in link["points"]], stroke)
        if arrow is not None:
            items.append(arrow)
    for ident in solved["order"]:
        entry = solved["objects"][ident]
        if entry.get("points"):
            obj = scene["objects"].get(ident) or {}
            tone = obj.get("tone") if obj.get("tone") in canvas_theme.TONES else None
            arrow = _arrow([view.p(p) for p in entry["points"]], "tone.{}.stroke".format(tone) if tone and tone != "neutral" else "base.line")
            if arrow is not None:
                items.append(arrow)
    label_items, dropped, crowded, drawn = _place_labels(scene, view, box, labels_mode, [(s["id"], s["rect"]) for s in ordered])
    # The fit keeps the geometry inside the box; a zoomed or re-centred camera may reach past it, and then the geometry is
    # clipped to it. The labels are placed inside the box and stay out of the clip, so a picture never cuts a word.
    group: Dict[str, Any] = {"k": "group", "items": items}
    if zoom > 1.0 + 1e-9 or centre is not None:
        group["clip"] = [box[0], box[1], box[2], box[3]]
    return {"items": [group] + label_items, "dropped": dropped, "crowded": crowded, "count": len(items) + len(label_items),
            "scale": view.scale, "labels": drawn}


def draw(el: Mapping[str, Any], view: str, box: Sequence[float]) -> List[Dict[str, Any]]:
    """The primitives only."""
    return project(el, view, box)["items"]
