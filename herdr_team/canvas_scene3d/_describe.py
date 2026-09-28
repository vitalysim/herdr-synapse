"""A scene read back as text (canvas v2 phase 4, 3.9): what an agent that cannot read images gets.

``summary(el)`` ends the element's ``look`` line (counts, camera, bounds);
``gist(el, full, stills)`` gives the lines ``look`` prints under it: one
line per top-level object with its shape, size, label and the relations that
placed it (a group lists what it holds), the links, the solver's notes and any
conflict. ``--full`` adds every object's box, tone, finish, glTF facts and its
nearest neighbours ("2.1 m right of lb"). ``facts(el)`` is the same for
``look --json``.
"""
from __future__ import annotations

import math
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence, Tuple

from herdr_team import canvas_scene3d as S
from herdr_team.canvas_scene3d import _project, _shapes, _spec, _vec as V

#: Object lines the default view prints before ``… +N objects``.
MAX_LINES = 12
#: The most gist lines ``look`` prints by default (``Kind.gist_lines``): every object line, ``… +N objects``, then the
#: links, notes, conflicts and stills lines.
GIST_LINES = MAX_LINES + 5
VIEWS = ("iso", "front", "top")


def _one(value: float) -> str:
    text = "{:.1f}".format(float(value))
    return "0.0" if text in ("-0.0",) else text.replace("-", "−")


def _quote(text: Any) -> str:
    return '"' + str(text).replace('"', '\\"') + '"'


def describe_object(obj: Mapping[str, Any], scene: Mapping[str, Any]) -> str:
    prim = S.get(obj.get("shape")) or S.get("box")
    if prim is None:
        return str(obj.get("shape"))
    if prim.container:
        kids = [o for o in scene["objects"].values() if o.get("in") == obj.get("id")]
        # Each member with its label, and its tone when it stands out from its siblings' (a degraded node among
        # healthy ones), so the default view shows what the group holds, not only ids.
        tones: Dict[str, int] = {}
        for kid in kids:
            tones[str(kid.get("tone") or "neutral")] = tones.get(str(kid.get("tone") or "neutral"), 0) + 1
        usual = sorted(tones.items(), key=lambda kv: (-kv[1], kv[0]))[0][0] if tones else "neutral"

        def member(kid: Mapping[str, Any]) -> str:
            text = str(kid.get("id"))
            if kid.get("label"):
                text += " " + _quote(kid["label"])
            tone = str(kid.get("tone") or "neutral")
            if len(kids) > 1 and tone != usual:
                text += " " + tone
            return text

        names = ", ".join(member(o) for o in kids[:6]) + (", …" if len(kids) > 6 else "")
        return "group {} of {} ({})".format(obj.get("layout") or "row", len(kids), names)
    try:
        return prim.describe(obj)
    except Exception:  # noqa: BLE001 - readback never raises on a malformed stored object
        return str(obj.get("shape"))


def _placement(ident: str, entry: Mapping[str, Any]) -> str:
    rel = str(entry.get("rel") or "")
    if rel:
        return rel
    return "(ground)" if abs(float(entry["aabb"][1])) < 1e-6 else "(free)"


def object_line(ident: str, scene: Mapping[str, Any], full: bool, units: str) -> str:
    obj = scene["objects"].get(ident) or {"id": ident, "shape": "box"}
    entry = scene["solved"]["objects"].get(ident)
    parts = [ident, describe_object(obj, scene)]
    if obj.get("label"):
        parts.append(_quote(obj["label"]))
    if entry is not None:
        parts.append(_placement(ident, entry))
    if obj.get("overlap"):
        parts.append("(overlap intended)")
    line = " ".join(parts)
    if full and entry is not None:
        box = entry["aabb"]
        extra = ["box ({}, {}, {})–({}, {}, {}) {}".format(*[_one(v) for v in box], units)]
        extra.append("tone {}".format(obj.get("tone") or "neutral"))
        if obj.get("finish"):
            extra.append(obj["finish"])
        if obj.get("opacity") is not None:
            extra.append("opacity {:g}".format(float(obj["opacity"])))
        model = scene["solved"]["models"].get(ident)
        if isinstance(model, dict):
            extra.append(model_text(model))
        near = neighbours(ident, scene, units)
        if near:
            extra.append(near)
        if obj.get("note"):
            extra.append("note " + _quote(obj["note"]))
        line += " · " + " · ".join(extra)
    return line


def model_text(model: Mapping[str, Any]) -> str:
    facts = model.get("facts") if isinstance(model.get("facts"), dict) else {}
    native = model.get("native") or []
    text = "{} triangles, {} mesh{}, {} material{}".format(model.get("tris", 0), facts.get("meshes", 0), "" if facts.get("meshes") == 1 else "es",
                                                         facts.get("materials", 0), "" if facts.get("materials") == 1 else "s")
    if native:
        text += ", native {} scaled {:g}".format("×".join(_shapes.g(v) for v in native), round(float(model.get("scale") or 1), 4))
    nodes = facts.get("nodes") or []
    if nodes:
        text += ", nodes " + ", ".join(str(n) for n in nodes[:8])
    if facts.get("packed"):
        text += ", packed from .gltf" if str(model.get("src", "")).endswith(".gltf") else ", packed"
    return text


def _side(me: Sequence[float], other: Sequence[float]) -> Optional[Tuple[float, str]]:
    """The separation between two boxes along the axis they are most apart on, and its words."""
    options = []
    for axis, (low, high) in enumerate((("left of", "right of"), ("below", "above"), ("behind", "in front of"))):
        if me[axis] >= other[axis + 3] - 1e-9:
            options.append((me[axis] - other[axis + 3], high))
        elif me[axis + 3] <= other[axis] + 1e-9:
            options.append((other[axis] - me[axis + 3], low))
    if not options:
        return None
    return max(options, key=lambda item: item[0])


def neighbours(ident: str, scene: Mapping[str, Any], units: str, count: int = 2) -> str:
    """``2.1 m right of lb, 0.3 m below cache``: the nearest top-level objects around it."""
    solved = scene["solved"]
    entry = solved["objects"].get(ident)
    if entry is None:
        return ""
    me = entry["aabb"]
    found = []
    for other_id in solved["order"]:
        other = solved["objects"][other_id]
        if other_id == ident or other.get("parent") != entry.get("parent") or other.get("points"):
            continue
        side = _side(me, other["aabb"])
        if side is None:
            found.append((0.0, "{} overlaps {}".format(ident, other_id)) if V.intersects(me, other["aabb"]) else (0.0, "touching " + other_id))
            continue
        found.append((side[0], "{} {} {} {}".format(_shapes.g(side[0]), units, side[1], other_id)))
    found.sort(key=lambda item: item[0])
    return ", ".join(text for _d, text in found[:count])


def link_text(link: Mapping[str, Any]) -> str:
    text = "{} → {}".format(link.get("from"), link.get("to"))
    if link.get("label"):
        text += " " + _quote(link["label"])
    return text


def summary(el: Mapping[str, Any], stills: Optional[Iterable[str]] = None) -> str:
    """``7 objects, 3 links · camera iso · bounds 8.0×2.4×5.0 m`` (the end of the scene's ``look`` line) and, when the
    stills are known, ``stills iso ✓ front ✗ top ✗``."""
    scene = _project.scene_of(el)
    solved = scene["solved"]
    units = scene["settings"].get("units") or "m"
    b = solved["bounds"]
    objects = len(scene["objects"])
    links = len(solved["links"])
    camera = _spec.camera_preset(scene["settings"].get("camera"))
    text = "{} object{}, {} link{} · camera {} · bounds {}×{}×{} {}".format(
        objects, "" if objects == 1 else "s", links, "" if links == 1 else "s", camera, _one(b[3] - b[0]), _one(b[4] - b[1]), _one(b[5] - b[2]), units)
    if stills is not None:
        names = set(stills)
        text += " · stills " + " ".join("{} {}".format(view, "✓" if still_name(el, view) in names else "✗") for view in VIEWS)
    return text


def still_name(el: Mapping[str, Any], view: str) -> str:
    """The still the page posts for a view of an element at its version: ``E-5-v40-iso.png`` (1.3)."""
    seq = el.get("updated_seq")
    number = int(seq) if isinstance(seq, (int, float)) and not isinstance(seq, bool) and math.isfinite(seq) else 0
    return "{}-v{}{}.png".format(el.get("id"), number, "-" + view if view else "")


def gist(el: Mapping[str, Any], full: bool = False, stills: Optional[Iterable[str]] = None, ref: str = "") -> List[str]:
    """The scene as text (3.9), under its readback line: one line per top-level object, then the links, the solver's
    notes and any conflict. By default at most ``MAX_LINES`` top-level objects, then ``… +N objects`` (every object's
    relation line is the scene's layout, so the default view keeps them rather than a short cap); ``full`` lists
    everything, children and boxes included. ``stills``, when known, adds which views the page posted."""
    scene = _project.scene_of(el)
    solved = scene["solved"]
    units = scene["settings"].get("units") or "m"
    tail: List[str] = []
    if solved["links"]:
        tail.append("links: " + " · ".join(link_text(link) for link in solved["links"]))
    if solved["notes"]:
        tail.append("notes: " + "; ".join(solved["notes"]))
    conflicts = ["{}: {}".format(c.get("code"), c.get("message")) for c in solved["conflicts"]]
    if conflicts:
        tail += ["conflict " + c for c in conflicts] if full else ["conflicts: " + "; ".join(conflicts)]
    if stills is not None:
        names = set(stills)
        tail.append("stills " + " ".join("{} {}".format(view, "✓" if still_name(el, view) in names else "✗") for view in VIEWS))
    top = [ident for ident in solved["order"] if (scene["objects"].get(ident) or {}).get("in") is None]
    if full:
        shown = top
    else:
        shown = top if len(top) <= MAX_LINES else top[:MAX_LINES - 1]
    lines: List[str] = []
    for ident in shown:
        lines.append(object_line(ident, scene, full, units))
        if full:
            for child in _children(ident, scene):
                lines.append("  " + object_line(child, scene, True, units))
    if len(shown) < len(top):
        lines.append("… +{} objects: canvas look --block {}".format(len(top) - len(shown), ref or el.get("alias") or el.get("id")))
    return lines + tail


def _children(ident: str, scene: Mapping[str, Any]) -> List[str]:
    out = []
    for other in scene["solved"]["order"]:
        if (scene["objects"].get(other) or {}).get("in") == ident:
            out.append(other)
            out += _children(other, scene)
    return out


def facts(el: Mapping[str, Any]) -> Dict[str, Any]:
    """``look --json``'s ``scene3d`` entry: bounds, every object's shape, box and relations, and the conflicts."""
    scene = _project.scene_of(el)
    solved = scene["solved"]
    return {"bounds": solved["bounds"],
            "objects": [{"id": ident, "shape": (scene["objects"].get(ident) or {}).get("shape"), "aabb": solved["objects"][ident]["aabb"],
                         "rel": solved["objects"][ident].get("rel") or ""} for ident in solved["order"]],
            "links": [{"key": link["key"], "from": link["from"], "to": link["to"], "label": link.get("label")} for link in solved["links"]],
            "notes": list(solved["notes"]), "conflicts": [{k: c.get(k) for k in ("code", "ids", "message")} for c in solved["conflicts"]]}
