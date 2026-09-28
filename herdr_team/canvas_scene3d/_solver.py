"""The scene3d relation solver (canvas v2 phase 4, 3.3): objects and relations in, positions out. Pure, deterministic.

``solve(objects, links, units, models)`` places every object:

1. **Groups first, children first.** A group's children are sized (a nested
   group is solved before its parent), then laid out in the group's own frame
   by its layout (``layouts``), or by their relations to their siblings when
   the layout is ``free``. The result is centred on x and z with its base at
   y = 0, so the group is placed as one box.
2. **Dependency order.** Each object depends on what its relations name (an
   object inside a group stands for that group) and an ``arrow3d`` on its ends.
   The order is a stable topological one, ties by spec order; a cycle is
   refused ``relation_cycle`` naming it (``api → db → api``).
3. **Placement.** ``pos`` wins; otherwise each relation sets its axes and the
   first relation that names a reference fills the rest (centred on x and z,
   base-aligned on y, or as ``align`` says; ``at`` offsets a vertical relation).
   An object with no relation flows in a row along +x from the last such object,
   ``m`` apart, on the ground.
4. **Collisions.** Its box is tested against everything placed before it,
   except what it rests on (``on``), is inside (``inside``), and anything
   marked ``overlap: true``. On a hit it is pushed along its relation's
   direction, away from the reference, by the overlap plus the gap, and the
   push is a note (``cache overlapped api by 0.20 m; moved up 0.30``). After
   3 pushes it stays, with the conflict ``unresolved_overlap``.
5. **Links** become segments between the points where the line between two
   boxes' centres leaves each box.
6. Every number is snapped to 1e-4 (``_vec.snap``), so 3.9 and 3.14 agree.

The result (``Solved``) is a plain dict; ``compact`` and ``expand`` convert it
to and from the element's stored form (``docs/scene3d.md``).
"""
from __future__ import annotations

import heapq
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team import canvas_scene3d as S
from herdr_team.canvas_scene3d import _spec, _vec as V, layouts, relations
from herdr_team.canvas_scene3d.relations import Placing, Relation

#: Pushes an object gets before its collision is left as a conflict.
MAX_PUSHES = 3
#: A group's default spacing between its children.
GROUP_GAP = 0.2
#: What a push reads as, by axis and sign.
_WORDS = {(0, 1): "right", (0, -1): "left", (1, 1): "up", (1, -1): "down", (2, 1): "forward", (2, -1): "back"}
SOLVED_VERSION = 1

Box = V.Box


class _Scene:
    """One solve: the resolved objects, their extents and the positions found so far."""

    def __init__(self, objects: Sequence[Mapping[str, Any]], units: str, models: Mapping[str, Mapping[str, Any]]) -> None:
        self.units = units if units in _spec.UNITS else "m"
        self.order = [str(o["id"]) for o in objects]
        self.index = {ident: i for i, ident in enumerate(self.order)}
        self.obj: Dict[str, Dict[str, Any]] = {}
        for raw in objects:
            obj = _spec.resolved(raw)
            model = models.get(str(raw["id"]))
            if model is not None:
                obj["model"] = dict(model)
            self.obj[str(raw["id"])] = obj
        self.prim = {ident: S.get(o.get("shape")) or S.get("box") for ident, o in self.obj.items()}
        self.children: Dict[Optional[str], List[str]] = {}
        for ident in self.order:
            self.children.setdefault(self.obj[ident].get("in"), []).append(ident)
        self.ext: Dict[str, V.Vec] = {}
        self.rot: Dict[str, List[float]] = {}
        #: Position in the frame of the object's scope (its group's, or the world's for top-level objects).
        self.local: Dict[str, V.Vec] = {}
        self.rel: Dict[str, str] = {}
        #: ``around``: the ring radius and angle each object got; ``arrow3d``: its two ends.
        self.ring: Dict[str, float] = {}
        self.angle: Dict[str, float] = {}
        self.points: Dict[str, List[V.Vec]] = {}
        self.notes: List[str] = []
        self.conflicts: List[Dict[str, Any]] = []
        self.world: Dict[str, V.Vec] = {}

    # -- structure -------------------------------------------------------------------------------------
    def container(self, ident: str) -> bool:
        prim = self.prim[ident]
        return bool(prim is not None and prim.container)

    def is_arrow(self, ident: str) -> bool:
        return self.obj[ident].get("shape") == "arrow3d"

    def ancestor_in(self, ident: str, scope: Optional[str]) -> Optional[str]:
        """The object that stands for ``ident`` among ``scope``'s members (itself, or the group holding it there)."""
        seen = 0
        while ident is not None and self.obj[ident].get("in") != scope:
            ident = self.obj[ident].get("in")
            seen += 1
            if seen > len(self.order):
                return None
        return ident

    def refs(self, ident: str) -> List[str]:
        obj = self.obj[ident]
        found = [str(obj[rel.name]) for rel in relations.of(obj)]
        if self.is_arrow(ident):
            found += [str(obj[end]) for end in ("from", "to") if isinstance(obj.get(end), str)]
        return [ref for ref in found if ref in self.obj]

    # -- boxes -----------------------------------------------------------------------------------------
    def leaves(self, ident: str, pos: V.Vec) -> List[Tuple[str, Box]]:
        """Every leaf box of ``ident`` placed at ``pos`` in its scope's frame."""
        if self.container(ident):
            out: List[Tuple[str, Box]] = []
            for child in self.children.get(ident, []):
                out += self.leaves(child, V.add(pos, self.local[child]))
            return out
        if self.is_arrow(ident):
            return []
        return [(ident, V.box_at(pos, self.ext[ident]))]

    def box_in(self, ident: str, scope: Optional[str]) -> Box:
        """``ident``'s box in ``scope``'s frame (it is placed, and ``scope`` holds it at some depth)."""
        pos = self.local[ident]
        holder = self.obj[ident].get("in")
        while holder != scope and holder is not None:
            pos = V.add(pos, self.local[holder])
            holder = self.obj[holder].get("in")
        return V.box_at(pos, self.ext[ident])


def _gap(value: Any, own: V.Vec, ref: Box) -> float:
    return relations.gap_units(value, own, ref)


def _cycle(nodes: Sequence[str], deps: Mapping[str, Sequence[str]]) -> List[str]:
    """One cycle among ``nodes`` (each has a dependency left among them), as ``[a, b, ..., a]``."""
    left = set(nodes)
    start = nodes[0]
    path, seen = [start], {start: 0}
    current = start
    while True:
        nxt = next((d for d in deps[current] if d in left), None)
        if nxt is None:
            return path
        if nxt in seen:
            return path[seen[nxt]:] + [nxt]
        seen[nxt] = len(path)
        path.append(nxt)
        current = nxt


def _order(scene: _Scene, members: Sequence[str], scope: Optional[str]) -> List[str]:
    """``members`` in dependency order, ties by spec order; a cycle is refused ``relation_cycle``."""
    member_set = set(members)
    deps: Dict[str, List[str]] = {}
    for ident in members:
        found: List[str] = []
        for ref in scene.refs(ident):
            anc = scene.ancestor_in(ref, scope)
            if anc == ident:
                raise _spec.PURE.error("relation_cycle", "{} is placed against {}, which it holds; name something outside it".format(ident, ref),
                                       field="objects[{}]".format(scene.index[ident]), cycle=[ident, ref])
            if anc in member_set and anc not in found:
                found.append(anc)
        deps[ident] = found
    waiting = {ident: len(deps[ident]) for ident in members}
    users: Dict[str, List[str]] = {ident: [] for ident in members}
    for ident, found in deps.items():
        for dep in found:
            users[dep].append(ident)
    heap = [(scene.index[ident], ident) for ident in members if waiting[ident] == 0]
    heapq.heapify(heap)
    out: List[str] = []
    while heap:
        _i, ident = heapq.heappop(heap)
        out.append(ident)
        for user in users[ident]:
            waiting[user] -= 1
            if waiting[user] == 0:
                heapq.heappush(heap, (scene.index[user], user))
    if len(out) < len(members):
        left = [ident for ident in members if ident not in set(out)]
        cycle = _cycle(left, deps)
        raise _spec.PURE.error("relation_cycle", "the relations go round in a circle: {}; break one of them".format(" → ".join(cycle)),
                               field="objects[{}]".format(scene.index[cycle[0]]), cycle=cycle)
    return out


# --------------------------------------------------------------------------
# placing one object


def _aligned(axis: int, ref: Box, ext: V.Vec, align: str) -> float:
    lo, hi = ref[axis], ref[axis + 3]
    size = ext[axis]
    if axis == 1:
        return lo if align == "start" else (hi - size if align == "end" else (lo + hi) / 2.0 - size / 2.0)
    return lo + size / 2.0 if align == "start" else (hi - size / 2.0 if align == "end" else (lo + hi) / 2.0)


def _relation_text(scene: _Scene, ident: str, used: Sequence[Tuple[Relation, str, float]]) -> str:
    obj = scene.obj[ident]
    parts = []
    for rel, ref, gap in used:
        text = "{} {}".format(rel.name, ref)
        if rel.name == "around":
            text += " (ring {:.2f}, {:g}°)".format(scene.ring.get(ident, 0.0), round(scene.angle.get(ident, 0.0), 1))
        elif rel.gap != 0 or gap:
            text += " (gap {:.2f})".format(gap)
        if rel.takes_at and obj.get("at") is not None:
            text += ", at ({}, {})".format(_num(obj["at"][0]), _num(obj["at"][1]))
        parts.append(text)
    return ", ".join(parts)


def _num(value: Any) -> str:
    return "{:.1f}".format(float(value)).replace("-", "−") if float(value) != 0 else "0.0"


def _place(scene: _Scene, ident: str, scope: Optional[str], siblings: Mapping[Tuple[str, str], List[str]],
           flow: List[Optional[Box]]) -> Tuple[V.Vec, Any, float, List[str]]:
    """``(position, push direction, push gap, contact ids)`` of one object in its scope, before collisions."""
    obj = scene.obj[ident]
    ext = scene.ext[ident]
    if obj.get("pos") is not None:
        pos = tuple(float(v) for v in obj["pos"])
        scene.rel[ident] = "pos ({})".format(", ".join(_num(v) for v in pos))
        return pos, None, 0.0, []  # type: ignore[return-value]
    rels = relations.of(obj)
    if not rels:
        last = flow[0]
        if last is None:
            pos = (0.0, 0.0, 0.0)
        else:
            gap = _gap("m", ext, last)
            pos = (last[3] + gap + ext[0] / 2.0, 0.0, 0.0)
        scene.rel[ident] = ""
        return pos, (1.0, 0.0, 0.0), _gap("m", ext, last or (0, 0, 0, 0, 0, 0)), []
    values: Dict[str, float] = {}
    refs: Dict[str, Tuple[Relation, Box]] = {}
    used: List[Tuple[Relation, str, float]] = []
    contacts: List[str] = []
    for rel in rels:
        ref_id = str(obj[rel.name])
        ref_box = scene.box_in(ref_id, scope)
        gap = _gap(obj.get("gap") if obj.get("gap") is not None and rel.gap != 0 else rel.gap, ext, ref_box)
        group = siblings.get((rel.name, ref_id), [ident])
        placing = Placing(ext=ext, ref=ref_box, gap=gap, obj=obj, siblings=(group.index(ident), len(group)))
        values.update(rel.place(placing))
        if rel.name == "around":
            scene.ring[ident] = relations.ring_radius(placing)
            scene.angle[ident] = relations.ring_angle(placing)
        if rel.fits is not None:
            problem = rel.fits(placing)
            if problem:
                scene.conflicts.append(_does_not_fit(scene, ident, ref_id, problem))
        refs[rel.group] = (rel, ref_box)
        used.append((rel, ref_id, gap))
        if rel.contact:
            contacts.append(ref_id)
    # The push follows the relation that places it sideways, else the vertical one; one it merely rests on (on,
    # inside) pushes it along +x, a small gap apart, so it stays on its support.
    primary = next((u for u in used if u[0].group in ("x", "z", "xz")), None) or used[0]
    push, push_gap = primary[0].push, primary[2]
    if primary[0].contact:
        push_gap = _gap("s", ext, refs[primary[0].group][1])
    align = obj.get("align")
    order = {"x": ("vertical", "z", "xz"), "y": ("x", "z", "xz"), "z": ("vertical", "x", "xz")}
    for axis_name, axis in (("x", 0), ("y", 1), ("z", 2)):
        if axis_name in values:
            continue
        anchor = next((refs[group] for group in order[axis_name] if group in refs), None)
        if anchor is None:
            values[axis_name] = 0.0
            continue
        rel, ref_box = anchor
        if axis_name in ("x", "z") and rel.takes_at and obj.get("at") is not None:
            offset = float(obj["at"][0 if axis_name == "x" else 1])
            values[axis_name] = (ref_box[axis] + ref_box[axis + 3]) / 2.0 + offset
            continue
        values[axis_name] = _aligned(axis, ref_box, ext, align or ("start" if axis == 1 else "center"))
    scene.rel[ident] = _relation_text(scene, ident, used)
    return (values["x"], values["y"], values["z"]), push, push_gap, contacts


def _does_not_fit(scene: _Scene, ident: str, host: str, problem: str) -> Dict[str, Any]:
    host_obj = scene.obj[host]
    fix: Optional[Dict[str, Any]] = None
    ext = scene.ext[ident]
    if host_obj.get("shape") == "box":
        size = [float(v) for v in host_obj.get("size") or [1, 1, 1]]
        need = [max(size[0], ext[0] + 2 * relations.WALL + 0.05), max(size[1], ext[1] + relations.WALL + 0.05),
                max(size[2], ext[2] + 2 * relations.WALL + 0.05)]
        fix = {"update": {"objects": [{"id": host, "size": [round(v, 2) for v in need]}]}}
    else:
        fix = {"update": {"objects": [{"id": ident, "inside": None, "on": host}]}}
    return {"code": "does_not_fit", "ids": [ident, host], "message": "{} is inside {}, but {}".format(ident, host, problem), "fix": fix}


def _hits(scene: _Scene, boxes: Sequence[Tuple[str, Box]], placed: Sequence[Tuple[str, Box]], skip: set) -> Optional[Tuple[str, Box, Box]]:
    for own_id, own in boxes:
        for other_id, other in placed:
            if other_id in skip or own_id == other_id:
                continue
            if V.intersects(own, other, 1e-6):
                return other_id, other, own
    return None


def _descendants(scene: _Scene, ident: str) -> List[str]:
    out = []
    for child in scene.children.get(ident, []):
        out.append(child)
        out += _descendants(scene, child)
    return out


def _settle(scene: _Scene, ident: str, pos: V.Vec, push: Any, gap: float, contacts: Sequence[str], placed: Sequence[Tuple[str, Box]],
            scope: Optional[str]) -> V.Vec:
    """Push ``ident`` off what it hits (at most ``MAX_PUSHES`` times), noting each push."""
    obj = scene.obj[ident]
    if push is None or obj.get("overlap"):
        return pos
    skip = set(contacts)
    for contact in contacts:
        skip.update(_descendants(scene, contact))
    skip.update(ident_ for ident_ in scene.order if scene.obj[ident_].get("overlap"))
    skip.update(_descendants(scene, ident))
    for attempt in range(MAX_PUSHES + 1):
        found = _hits(scene, scene.leaves(ident, pos), placed, skip)
        if found is None:
            return pos
        other_id, other, own = found
        if attempt == MAX_PUSHES:
            scene.conflicts.append({"code": "unresolved_overlap", "ids": [ident, other_id],
                                    "message": "{} still overlaps {} after {} pushes".format(ident, other_id, MAX_PUSHES),
                                    "fix": {"update": {"objects": [_above_fix(scene, ident, other_id)]}}})
            return pos
        if push == "radial":
            ref = scene.obj[ident].get("around")
            centre = V.box_center(scene.box_in(str(ref), scope)) if ref in scene.obj else (0.0, 0.0, 0.0)
            direction = V.unit((pos[0] - centre[0], 0.0, pos[2] - centre[2]))
            if V.length(direction) < 0.5:
                direction = (1.0, 0.0, 0.0)
            depth = min(V.overlap(own, other)[0], V.overlap(own, other)[2])
            step = V.mul(direction, depth + gap)
            word = "out"
        else:
            axis = next(i for i in range(3) if abs(push[i]) > 0.5)
            sign = 1.0 if push[axis] > 0 else -1.0
            depth = (other[axis + 3] - own[axis]) if sign > 0 else (own[axis + 3] - other[axis])
            step = tuple(sign * (depth + gap) if i == axis else 0.0 for i in range(3))
            word = _WORDS[(axis, int(sign))]
        pos = V.add(pos, step)
        scene.notes.append("{} overlapped {} by {:.2f} {}; moved {} {:.2f}".format(ident, other_id, max(0.0, depth), scene.units, word,
                                                                                   V.length(step)))
    return pos


def _above_fix(scene: _Scene, ident: str, other: str) -> Dict[str, Any]:
    obj = scene.obj[ident]
    change: Dict[str, Any] = {"id": ident, "above": other, "gap": "s"}
    for rel in relations.of(obj):
        if rel.group == "vertical" and rel.name != "above":
            change[rel.name] = None
    if obj.get("pos") is not None:
        change["pos"] = None
    return change


def _solve_scope(scene: _Scene, members: Sequence[str], scope: Optional[str], placed: List[Tuple[str, Box]]) -> None:
    """Place ``members`` in ``scope``'s frame by their relations (the world, or a free group)."""
    siblings: Dict[Tuple[str, str], List[str]] = {}
    for ident in members:
        for rel in relations.of(scene.obj[ident]):
            if rel.name == "around":
                siblings.setdefault((rel.name, str(scene.obj[ident][rel.name])), []).append(ident)
    flow: List[Optional[Box]] = [None]
    arrows: List[str] = []
    for ident in _order(scene, members, scope):
        if scene.is_arrow(ident):
            arrows.append(ident)
            _place_arrow(scene, ident, scope)
            continue
        pos, push, gap, contacts = _place(scene, ident, scope, siblings, flow)
        pos = _settle(scene, ident, pos, push, gap, contacts, placed, scope)
        scene.local[ident] = pos
        if not relations.of(scene.obj[ident]) and scene.obj[ident].get("pos") is None:
            flow[0] = V.box_at(pos, scene.ext[ident])
        placed.extend(scene.leaves(ident, pos))


def _place_arrow(scene: _Scene, ident: str, scope: Optional[str]) -> None:
    obj = scene.obj[ident]
    ends = []
    for end in ("from", "to"):
        value = obj.get(end)
        ends.append(value if isinstance(value, str) and value in scene.obj else [float(v) for v in value])
    points = segment(scene, ends[0], ends[1], scope)
    radius = float(obj.get("radius") or 0.04)
    box = V.union([(p[0] - radius, p[1] - radius, p[2] - radius, p[0] + radius, p[1] + radius, p[2] + radius) for p in points])
    scene.ext[ident] = V.box_size(box)
    scene.local[ident] = ((box[0] + box[3]) / 2.0, box[1], (box[2] + box[5]) / 2.0)
    scene.rel[ident] = "from {} to {}".format(*(e if isinstance(e, str) else "({})".format(", ".join(_num(v) for v in e)) for e in ends))
    scene.points[ident] = points


def segment(scene: _Scene, a: Any, b: Any, scope: Optional[str] = None) -> List[V.Vec]:
    """The two ends of a link or arrow between objects (or points): where the line between the centres leaves each box."""
    box_a = scene.box_in(a, scope) if isinstance(a, str) else None
    box_b = scene.box_in(b, scope) if isinstance(b, str) else None
    ca = V.box_center(box_a) if box_a is not None else tuple(a)
    cb = V.box_center(box_b) if box_b is not None else tuple(b)
    if box_a is not None and box_b is not None and V.intersects(box_a, box_b, 0.0):
        return [ca, cb]  # type: ignore[list-item]
    start = V.exit_point(box_a, ca, cb) if box_a is not None else ca
    end = V.exit_point(box_b, cb, ca) if box_b is not None else cb
    return [start, end]  # type: ignore[list-item]


def _solve_group(scene: _Scene, gid: str) -> None:
    """Size and lay out a group's children (children first), then centre them: the group is one box."""
    kids = scene.children.get(gid, [])
    for child in kids:
        if scene.container(child):
            _solve_group(scene, child)
    obj = scene.obj[gid]
    layout = layouts.get(obj.get("layout") or "row") or layouts.get("row")
    sizes = [scene.ext[c] for c in kids]
    raw_gap = obj.get("gap")
    widest = max((max(e[0], e[2]) for e in sizes), default=1.0)
    gap = GROUP_GAP if raw_gap is None else relations.gap_units(raw_gap, (widest, 0.0, widest), (0, 0, 0, widest, 0, widest))
    if layout is not None and layout.place is not None:
        for child, pos in zip(kids, layout.place(sizes, obj, gap)):
            scene.local[child] = pos
            scene.rel[child] = "in {}".format(gid)
    else:
        _solve_scope(scene, kids, gid, [])
        for child in kids:
            text = scene.rel.get(child, "")
            scene.rel[child] = "in {}".format(gid) + (", " + text if text else "")
    boxes = [box for child in kids for _id, box in scene.leaves(child, scene.local[child])]
    union = V.union(boxes) if boxes else (0.0, 0.0, 0.0, 0.001, 0.001, 0.001)
    shift = (-(union[0] + union[3]) / 2.0, -union[1], -(union[2] + union[5]) / 2.0)
    for child in kids:
        scene.local[child] = V.add(scene.local[child], shift)
    scene.ext[gid] = tuple(max(0.001, v) for v in V.box_size(union))  # type: ignore[assignment]


def _world(scene: _Scene, ident: str) -> V.Vec:
    if ident in scene.world:
        return scene.world[ident]
    holder = scene.obj[ident].get("in")
    pos = scene.local[ident] if holder is None else V.add(_world(scene, holder), scene.local[ident])
    scene.world[ident] = pos
    return pos


def solve(objects: Sequence[Mapping[str, Any]], links: Sequence[Mapping[str, Any]] = (), units: str = "m",
          models: Optional[Mapping[str, Mapping[str, Any]]] = None) -> Dict[str, Any]:
    """Solve a scene: ``{"bounds", "objects": {id: {...}}, "order", "links", "notes", "conflicts", "models"}`` (3.3)."""
    scene = _Scene(objects, units, models or {})
    for ident in scene.order:
        if scene.container(ident):
            continue
        prim = scene.prim[ident]
        scene.rot[ident] = _spec.rotation_of(scene.obj[ident])
        scene.ext[ident] = V.rotated_extent(prim.extent(scene.obj[ident]) if prim is not None else (1.0, 1.0, 1.0), scene.rot[ident])
    for ident in scene.children.get(None, []):
        if scene.container(ident):
            _solve_group(scene, ident)
    for ident in scene.order:
        scene.rot.setdefault(ident, [0.0, 0.0, 0.0])
    _solve_scope(scene, scene.children.get(None, []), None, [])
    out_objects: Dict[str, Dict[str, Any]] = {}
    leaf_boxes: List[Box] = []
    for ident in scene.order:
        pos = _world(scene, ident)
        ext = scene.ext[ident]
        box = V.box_at(pos, ext)
        entry: Dict[str, Any] = {"pos": V.snapped(pos), "rot": V.snapped(scene.rot[ident]), "ext": V.snapped(ext), "aabb": V.snapped(box),
                                 "rel": scene.rel.get(ident, ""), "parent": scene.obj[ident].get("in")}
        model = scene.obj[ident].get("model")
        if isinstance(model, dict):
            entry["asset"] = model.get("asset")
            entry["tris"] = int(model.get("tris") or 0)
        if scene.is_arrow(ident):
            entry["points"] = [V.snapped(p) for p in scene.points.get(ident, [])]
        elif not scene.container(ident):
            leaf_boxes.append(box)
        out_objects[ident] = entry
    out_links = []
    for link in links:
        a, b = str(link.get("from")), str(link.get("to"))
        if a not in scene.obj or b not in scene.obj:
            continue
        points = segment(scene, a, b, None)
        item: Dict[str, Any] = {"key": str(link.get("id") or _spec.link_key(a, b)), "from": a, "to": b, "points": [V.snapped(p) for p in points]}
        if link.get("label"):
            item["label"] = link["label"]
        if link.get("tone"):
            item["tone"] = link["tone"]
        out_links.append(item)
    extra = [tuple(p) for link in out_links for p in link["points"]]
    extra += [tuple(p) for entry in out_objects.values() for p in entry.get("points") or []]
    boxes = leaf_boxes + [(p[0], p[1], p[2], p[0], p[1], p[2]) for p in extra]
    bounds = V.snapped(V.union(boxes)) if boxes else [0.0] * 6
    models_out = {}
    for ident in scene.order:
        model = scene.obj[ident].get("model")
        if isinstance(model, dict):
            models_out[ident] = dict(model)
    return {"v": SOLVED_VERSION, "bounds": bounds, "order": list(scene.order), "objects": out_objects, "links": out_links,
            "notes": list(scene.notes), "conflicts": list(scene.conflicts), "models": models_out, "units": scene.units}


# --------------------------------------------------------------------------
# the stored form (docs/scene3d.md)

#: The fields of one stored object row, in order.
OBJECT_ROW = ("id", "pos", "rot", "ext", "parent", "rel")


def compact(solved: Mapping[str, Any]) -> Dict[str, Any]:
    """The element's ``solved``: rows instead of dicts (``OBJECT_ROW``), the models and links kept small."""
    rows = []
    for ident in solved.get("order") or []:
        entry = solved["objects"][ident]
        row = [ident, entry["pos"], entry["rot"], entry["ext"], entry.get("parent"), entry.get("rel") or ""]
        if entry.get("points"):
            row.append({"points": entry["points"]})
        rows.append(row)
    out: Dict[str, Any] = {"v": SOLVED_VERSION, "bounds": list(solved.get("bounds") or [0] * 6), "objects": rows,
                           "links": [[l["key"], l["from"], l["to"], l["points"], l.get("label"), l.get("tone")] for l in solved.get("links") or []]}
    if solved.get("notes"):
        out["notes"] = list(solved["notes"])
    if solved.get("conflicts"):
        out["conflicts"] = [dict(c) for c in solved["conflicts"]]
    if solved.get("models"):
        out["models"] = {ident: dict(model) for ident, model in solved["models"].items()}
    return out


def expand(stored: Any) -> Dict[str, Any]:
    """The element's ``solved`` as ``solve`` returns it (junk reads as an empty scene)."""
    empty = {"v": SOLVED_VERSION, "bounds": [0.0] * 6, "order": [], "objects": {}, "links": [], "notes": [], "conflicts": [], "models": {}}
    if not isinstance(stored, dict):
        return empty
    out = dict(empty)
    bounds = stored.get("bounds")
    if isinstance(bounds, list) and len(bounds) == 6 and all(_spec.is_number(v) for v in bounds):
        out["bounds"] = [float(v) for v in bounds]
    models = stored.get("models") if isinstance(stored.get("models"), dict) else {}
    objects: Dict[str, Dict[str, Any]] = {}
    order: List[str] = []
    for row in stored.get("objects") or []:
        if not isinstance(row, list) or len(row) < 6 or not isinstance(row[0], str):
            continue
        vecs = [row[i] for i in (1, 2, 3)]
        if not all(isinstance(v, list) and len(v) == 3 and all(_spec.is_number(x) for x in v) for v in vecs):
            continue
        pos, rot, ext = ([float(x) for x in v] for v in vecs)
        entry: Dict[str, Any] = {"pos": pos, "rot": rot, "ext": ext, "aabb": list(V.box_at(pos, ext)),
                                 "parent": row[4] if isinstance(row[4], str) else None, "rel": row[5] if isinstance(row[5], str) else ""}
        if len(row) > 6 and isinstance(row[6], dict) and isinstance(row[6].get("points"), list):
            entry["points"] = [[float(x) for x in p] for p in row[6]["points"] if isinstance(p, list) and len(p) == 3]
        model = models.get(row[0])
        if isinstance(model, dict):
            entry["asset"] = model.get("asset")
            entry["tris"] = int(model.get("tris") or 0) if _spec.is_number(model.get("tris")) else 0
        objects[row[0]] = entry
        order.append(row[0])
    links = []
    for row in stored.get("links") or []:
        if isinstance(row, list) and len(row) >= 4 and isinstance(row[3], list) and len(row[3]) == 2:
            try:
                points = [[float(x) for x in p] for p in row[3]]
            except (TypeError, ValueError):
                continue
            item: Dict[str, Any] = {"key": str(row[0]), "from": str(row[1]), "to": str(row[2]), "points": points}
            if len(row) > 4 and isinstance(row[4], str):
                item["label"] = row[4]
            if len(row) > 5 and isinstance(row[5], str):
                item["tone"] = row[5]
            links.append(item)
    out.update(order=order, objects=objects, links=links, models=dict(models))
    out["notes"] = [str(n) for n in stored.get("notes") or [] if isinstance(n, str)]
    out["conflicts"] = [c for c in stored.get("conflicts") or [] if isinstance(c, dict)]
    return out
