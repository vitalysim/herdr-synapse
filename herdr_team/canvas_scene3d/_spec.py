"""Validating a scene3d op's objects, links and settings (canvas v2 phase 4, 3.1 to 3.3).

Every function takes a ``ctx`` with ``invalid(field, message)``,
``too_big(field, limit, max, message)`` and ``error(code, message)``: the
canvas's ``OpContext`` under the lock, or ``PURE`` outside it (``Block.load``
solves before the lock is taken). Both raise the same ``HerdrTeamError``s, so a
refusal reads the same wherever it is found, and names the field
(``objects[3].radius``) with a "did you mean" where one helps.

An object is stored as it was given, validated and in canonical form (no
defaults filled in), so reading a scene back gives the op that built it
(test T-B1). ``resolved(obj)`` is the same object with every default, which the
solver and the projections use.
"""
from __future__ import annotations

import difflib
import math
import re
from typing import Any, Dict, List, Mapping, Sequence, Tuple

from herdr_team import canvas_theme
from herdr_team.canvas_scene3d import _tokens
from herdr_team.errors import HerdrTeamError

ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}\Z")
#: ``a -> b``, ``a->b: label``.
LINK_RE = re.compile(r"^\s*([A-Za-z0-9_-]{1,32}?)\s*->\s*([A-Za-z0-9_-]{1,32})\s*(?::\s*(.+?))?\s*\Z")
LINK_FIELDS = ("id", "from", "to", "label", "tone")
MAX_LABEL = 80
MAX_NOTE = 200
MAX_COORD = 100_000.0
#: The finishes, cameras and lights are the token file's (``scene3d``), so a new preset is a token, not code.
FINISHES = tuple(_tokens.finishes()) or ("matte", "glossy", "metal", "glass")
ALIGNS = ("start", "center", "end")
GAP_TOKENS = {"s": 0.10, "m": 0.25, "l": 0.50}

UNITS = ("m", "cm", "mm", "units")
CAMERA_PRESETS = tuple(_tokens.cameras()) or ("iso", "front", "top", "side", "orbit")
LIGHT_PRESETS = tuple(_tokens.lights()) or ("studio", "soft", "day", "night", "flat")
LABEL_MODES = ("auto", "all", "none")
SETTINGS = ("units", "camera", "lights", "ground", "labels")
DEFAULT_SETTINGS: Dict[str, Any] = {"units": "m", "camera": "iso", "lights": "studio", "ground": True, "labels": "auto"}
SETTINGS_DOC = {"units": "a label only: m|cm|mm|units (default m)",
                "camera": "iso|front|top|side|orbit, or {preset, az, el, zoom, target}",
                "lights": "studio|soft|day|night|flat", "ground": "a ground grid under the scene (default true)",
                "labels": "auto (labels that fit) | all (every object, by label or id) | none"}
CAMERA_FIELDS = ("preset", "az", "el", "zoom", "target")

#: Fields every object may carry, whatever its shape (the relation fields are ``relations.names()``).
BASE_FIELDS = ("id", "shape", "tone", "finish", "opacity", "label", "rotate", "in", "overlap", "note", "pos", "at", "align", "gap",
               "ring", "angle")


class _Pure:
    """The refusals ``OpContext`` gives, for code that runs outside the canvas (``Block.load``)."""

    def invalid(self, field: str, message: str, **details: Any) -> HerdrTeamError:
        return HerdrTeamError("op_invalid", message, details=dict(details, field=field))

    def error(self, code: str, message: str, **details: Any) -> HerdrTeamError:
        return HerdrTeamError(code, message, details=details)

    def too_big(self, field: str, limit: str, maximum: int, message: str) -> HerdrTeamError:
        return HerdrTeamError("canvas_limit", message, details={"field": field, "limit": limit, "max": maximum})


PURE = _Pure()


def common_fields() -> Tuple[str, ...]:
    from herdr_team.canvas_scene3d import relations

    return BASE_FIELDS + tuple(relations.names())


def nearest(value: Any, choices: Sequence[str]) -> str:
    """`` (did you mean "bar"?)`` for the closest of ``choices``, case-insensitive first, else ""."""
    if not isinstance(value, str) or not choices:
        return ""
    lowered = {c.lower(): c for c in choices}
    if value.lower() in lowered:
        return ' (did you mean "{}"?)'.format(lowered[value.lower()])
    found = difflib.get_close_matches(value.lower(), list(lowered), n=1, cutoff=0.6)
    return ' (did you mean "{}"?)'.format(lowered[found[0]]) if found else ""


def is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def number(ctx: Any, value: Any, field: str, low: float, high: float) -> float:
    if not is_number(value) or not (low <= float(value) <= high):
        raise ctx.invalid(field, "{} is a number from {:g} to {:g}".format(field, low, high))
    return value


def positive(ctx: Any, value: Any, field: str, low: float = 0.001, high: float = 10_000.0) -> float:
    return number(ctx, value, field, low, high)


def vector(ctx: Any, value: Any, field: str, n: int, low: float = -MAX_COORD, high: float = MAX_COORD) -> List[Any]:
    if not isinstance(value, list) or len(value) != n or not all(is_number(v) and low <= float(v) <= high for v in value):
        raise ctx.invalid(field, "{} is [{}] (numbers from {:g} to {:g})".format(field, ", ".join("xyz"[:n] if n != 2 else "ab"), low, high))
    return list(value)


def text(ctx: Any, value: Any, field: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ctx.invalid(field, "{} is text".format(field))
    found = " ".join(value.split())
    if len(found) > limit:
        raise ctx.too_big(field, "MAX_LABEL", limit, "{} holds at most {} characters".format(field, limit))
    return found


def choice(ctx: Any, value: Any, field: str, choices: Sequence[str]) -> str:
    if value not in choices:
        raise ctx.invalid(field, "{} is one of: {}{}".format(field, ", ".join(choices), nearest(value, choices)))
    return value


def gap(ctx: Any, value: Any, field: str) -> Any:
    if isinstance(value, str) and value in GAP_TOKENS:
        return value
    if is_number(value) and 0 <= float(value) <= 10_000:
        return value
    raise ctx.invalid(field, "{} is s, m, l (10, 25, 50 % of the larger footprint) or a number of units".format(field))


def ident(ctx: Any, value: Any, field: str) -> str:
    if not isinstance(value, str) or not ID_RE.match(value):
        raise ctx.invalid(field, "{} is 1 to 32 letters, digits, _ or -".format(field))
    return value


# --------------------------------------------------------------------------
# objects


def object_item(ctx: Any, raw: Any, field: str) -> Dict[str, Any]:
    """One object, validated and canonical: its common fields, its relations and its primitive's own params."""
    from herdr_team import canvas_scene3d as S
    from herdr_team.canvas_scene3d import relations

    if not isinstance(raw, dict):
        raise ctx.invalid(field, "{} is an object {{id, shape, ...}}".format(field))
    shape = raw.get("shape", "box")
    prim = S.get(shape)
    if prim is None:
        raise ctx.invalid(field + ".shape", "{} is one of: {}{}".format(field + ".shape", ", ".join(S.names()), nearest(shape, S.names())))
    allowed = common_fields() + tuple(prim.params)
    for key in raw:
        if key not in allowed:
            raise ctx.invalid("{}.{}".format(field, key), "a {} takes {}{}".format(
                shape, ", ".join(tuple(prim.params) + BASE_FIELDS[2:] + tuple(relations.names())), nearest(key, allowed)))
    if raw.get("id") is None:
        raise ctx.invalid(field + ".id", "every object needs an id (objects[].id), so relations and patches can name it")
    out: Dict[str, Any] = {"id": ident(ctx, raw["id"], field + ".id"), "shape": shape}
    if raw.get("tone") is not None:
        out["tone"] = choice(ctx, raw["tone"], field + ".tone", canvas_theme.TONES)
    if raw.get("finish") is not None:
        out["finish"] = choice(ctx, raw["finish"], field + ".finish", FINISHES)
    if raw.get("opacity") is not None:
        out["opacity"] = number(ctx, raw["opacity"], field + ".opacity", 0.2, 1.0)
    if raw.get("label") is not None:
        found = text(ctx, raw["label"], field + ".label", MAX_LABEL)
        if found:
            out["label"] = found
    if raw.get("note") is not None:
        found = text(ctx, raw["note"], field + ".note", MAX_NOTE)
        if found:
            out["note"] = found
    if raw.get("rotate") is not None:
        rot = raw["rotate"]
        out["rotate"] = number(ctx, rot, field + ".rotate", -360, 360) if not isinstance(rot, list) else \
            vector(ctx, rot, field + ".rotate", 3, -360, 360)
        if prim.container or prim.name in ("arrow3d",):
            raise ctx.invalid(field + ".rotate", "a {} does not turn; rotate what it holds".format(shape))
    if raw.get("in") is not None:
        out["in"] = ident(ctx, raw["in"], field + ".in")
    if raw.get("overlap") is not None:
        if not isinstance(raw["overlap"], bool):
            raise ctx.invalid(field + ".overlap", "overlap is true (an intended intersection) or false")
        if raw["overlap"]:
            out["overlap"] = True
    placed = []
    for rel in relations.relations():
        value = raw.get(rel.name)
        if value is None:
            continue
        out[rel.name] = ident(ctx, value, "{}.{}".format(field, rel.name))
        placed.append(rel)
    groups: Dict[str, str] = {}
    for rel in placed:
        for axis in rel.sets:
            if axis in groups:
                raise ctx.invalid("{}.{}".format(field, rel.name), "{}: {} and {} both place {}; keep one".format(
                    field, groups[axis], rel.name, axis))
            groups[axis] = rel.name
    if raw.get("pos") is not None:
        if placed:
            raise ctx.invalid(field + ".pos", "{}: pos places it absolutely; drop {} or pos".format(field, ", ".join(r.name for r in placed)))
        out["pos"] = vector(ctx, raw["pos"], field + ".pos", 3)
    if raw.get("at") is not None:
        if not any(rel.takes_at for rel in placed):
            raise ctx.invalid(field + ".at", "at [dx, dz] is an offset from what it is on, above, below or inside; add one of those")
        out["at"] = vector(ctx, raw["at"], field + ".at", 2)
    if raw.get("align") is not None:
        if not placed:
            raise ctx.invalid(field + ".align", "align lines it up with what a relation names; add a relation")
        out["align"] = choice(ctx, raw["align"], field + ".align", ALIGNS)
    if raw.get("gap") is not None:
        out["gap"] = gap(ctx, raw["gap"], field + ".gap")
    for key in ("ring", "angle"):
        if raw.get(key) is None:
            continue
        if "around" not in out:
            raise ctx.invalid("{}.{}".format(field, key), "{} goes with around (the ring's radius and the angle on it)".format(key))
        out[key] = positive(ctx, raw[key], "{}.{}".format(field, key)) if key == "ring" else number(ctx, raw[key], field + ".angle", -360, 360)
    if prim.name == "arrow3d" and (placed or raw.get("in") is not None or raw.get("pos") is not None):
        raise ctx.invalid(field, "an arrow3d is placed by its from and to; drop its relations, in and pos")
    params = prim.normalize(ctx, raw, field)
    for key in prim.params:
        if key in raw and raw[key] is not None:
            out[key] = params.get(key, raw[key]) if key in params else raw[key]
    return out


def tidy(obj: Dict[str, Any]) -> Dict[str, Any]:
    """An object after what its relations named went away (``patch remove`` clears them): the fields that only mean
    something with a relation (``at``, ``align``, ``ring``, ``angle``) go too."""
    from herdr_team.canvas_scene3d import relations

    placed = relations.of(obj)
    if obj.get("at") is not None and not any(rel.takes_at for rel in placed):
        obj.pop("at")
    if obj.get("align") is not None and not placed:
        obj.pop("align")
    if obj.get("around") is None:
        obj.pop("ring", None)
        obj.pop("angle", None)
    return obj


def resolved(obj: Mapping[str, Any]) -> Dict[str, Any]:
    """An object with its primitive's defaults filled in (``PURE`` refuses nothing a stored object holds)."""
    from herdr_team import canvas_scene3d as S

    prim = S.get(obj.get("shape"))
    out = dict(obj)
    if prim is not None:
        out.update(prim.normalize(PURE, dict(obj), "objects"))
    return out


def rotation_of(obj: Mapping[str, Any]) -> List[float]:
    rot = obj.get("rotate")
    if is_number(rot):
        return [0.0, float(rot), 0.0]
    if isinstance(rot, list) and len(rot) == 3 and all(is_number(v) for v in rot):
        return [float(v) for v in rot]
    return [0.0, 0.0, 0.0]


# --------------------------------------------------------------------------
# links


def link_key(a: str, b: str) -> str:
    return "{}->{}".format(a, b)


def link_item(ctx: Any, raw: Any, field: str) -> Dict[str, Any]:
    """``"a -> b"``, ``"a -> b: label"`` or ``{from, to, label, tone}``; its id is ``a->b`` (``a->b#2`` for another)."""
    auto = True
    if isinstance(raw, str):
        match = LINK_RE.match(raw)
        if match is None:
            raise ctx.invalid(field, "{} does not parse: a link is \"a -> b\" or \"a -> b: label\"".format(raw[:80]))
        a, b, label = match.groups()
        raw = {"from": a, "to": b}
        if label:
            raw["label"] = label
    if not isinstance(raw, dict):
        raise ctx.invalid(field, "{} is \"a -> b\" or {{from, to, label, tone}}".format(field))
    for key in raw:
        if key not in LINK_FIELDS:
            raise ctx.invalid("{}.{}".format(field, key), "a link takes {}{}".format(", ".join(LINK_FIELDS), nearest(key, LINK_FIELDS)))
    out: Dict[str, Any] = {"id": "", "from": ident(ctx, raw.get("from"), field + ".from"), "to": ident(ctx, raw.get("to"), field + ".to")}
    out["id"] = link_key(out["from"], out["to"])
    if isinstance(raw.get("id"), str) and raw["id"]:
        if raw["id"] != out["id"] and not re.match(r"^" + re.escape(out["id"]) + r"#[0-9]{1,3}\Z", raw["id"]):
            raise ctx.invalid(field + ".id", "a link id is {} (or {}#2 ... for another link between them)".format(out["id"], out["id"]))
        out["id"] = raw["id"]
        auto = False
    if raw.get("label") is not None:
        found = text(ctx, raw["label"], field + ".label", MAX_LABEL)
        if found:
            out["label"] = found
    if raw.get("tone") is not None:
        out["tone"] = choice(ctx, raw["tone"], field + ".tone", canvas_theme.TONES)
    if auto:
        out["_auto"] = True
    return out


def link_remove_key(entry: Any) -> str:
    """A ``remove`` entry: ``"api -> db"`` or ``api->db``."""
    if isinstance(entry, str):
        match = LINK_RE.match(entry)
        if match is not None and not match.group(3):
            return link_key(match.group(1), match.group(2))
    return str(entry)


def number_links(links: List[Dict[str, Any]]) -> None:
    """Parallel links get ``#2``, ``#3`` ... in order, skipping ids given explicitly."""
    taken = {str(link["id"]) for link in links if not link.get("_auto")}
    for link in links:
        if not link.pop("_auto", False):
            continue
        base = link_key(link["from"], link["to"])
        candidate, count = base, 1
        while candidate in taken:
            count += 1
            candidate = "{}#{}".format(base, count)
        link["id"] = candidate
        taken.add(candidate)


# --------------------------------------------------------------------------
# settings


def camera(ctx: Any, value: Any, field: str = "camera") -> Any:
    """A preset name, or ``{preset, az, el, zoom, target}`` (what "Save view" sends)."""
    if isinstance(value, str):
        return choice(ctx, value, field, CAMERA_PRESETS)
    if not isinstance(value, dict):
        raise ctx.invalid(field, "camera is one of {} or {{preset, az, el, zoom, target}}".format(", ".join(CAMERA_PRESETS)))
    for key in value:
        if key not in CAMERA_FIELDS:
            raise ctx.invalid("{}.{}".format(field, key), "a camera takes {}{}".format(", ".join(CAMERA_FIELDS), nearest(key, CAMERA_FIELDS)))
    out: Dict[str, Any] = {"preset": choice(ctx, value.get("preset", "orbit"), field + ".preset", CAMERA_PRESETS)}
    if value.get("az") is not None:
        out["az"] = number(ctx, value["az"], field + ".az", -360, 360)
    if value.get("el") is not None:
        out["el"] = number(ctx, value["el"], field + ".el", -89, 90)
    if value.get("zoom") is not None:
        out["zoom"] = number(ctx, value["zoom"], field + ".zoom", 0.25, 8)
    if value.get("target") is not None:
        target = value["target"]
        out["target"] = vector(ctx, target, field + ".target", 3) if isinstance(target, list) else ident(ctx, target, field + ".target")
    return out


def camera_preset(value: Any) -> str:
    if isinstance(value, dict):
        value = value.get("preset")
    return value if value in CAMERA_PRESETS else "iso"


def primary_view(value: Any) -> str:
    """The still view a camera setting draws first: iso, front and top are themselves; side and orbit draw iso (3.5)."""
    preset = camera_preset(value)
    return preset if preset in ("iso", "front", "top") else "iso"


def settings(ctx: Any, spec: Mapping[str, Any]) -> Dict[str, Any]:
    """The five settings, validated, with their defaults."""
    out = dict(DEFAULT_SETTINGS)
    if spec.get("units") is not None:
        out["units"] = choice(ctx, spec["units"], "units", UNITS)
    if spec.get("camera") is not None:
        out["camera"] = camera(ctx, spec["camera"])
    if spec.get("lights") is not None:
        out["lights"] = choice(ctx, spec["lights"], "lights", LIGHT_PRESETS)
    if spec.get("ground") is not None:
        if not isinstance(spec["ground"], bool):
            raise ctx.invalid("ground", "ground is true or false")
        out["ground"] = spec["ground"]
    if spec.get("labels") is not None:
        out["labels"] = choice(ctx, spec["labels"], "labels", LABEL_MODES)
    return out


# --------------------------------------------------------------------------
# across objects


def check_refs(ctx: Any, objects: Sequence[Mapping[str, Any]], links: Sequence[Mapping[str, Any]], camera_value: Any = None) -> None:
    """What objects and links name exists, groups hold only what their layout allows, and nothing is in itself."""
    from herdr_team import canvas_scene3d as S
    from herdr_team.canvas_scene3d import layouts, relations

    ids = [str(o["id"]) for o in objects]
    by_id = {str(o["id"]): o for o in objects}
    def known(value: Any, field: str) -> None:
        if value not in by_id:
            raise ctx.invalid(field, "{} is not an object of the scene; objects: {}{}".format(
                value, ", ".join(ids[:20]) + (" …" if len(ids) > 20 else ""), nearest(value, ids)))

    for i, obj in enumerate(objects):
        field = "objects[{}]".format(i)
        if obj.get("in") is not None:
            known(obj["in"], field + ".in")
            holder = by_id[obj["in"]]
            prim = S.get(holder.get("shape"))
            if prim is None or not prim.container:
                raise ctx.invalid(field + ".in", "{} is a {}; only a group holds objects (shape: group)".format(obj["in"], holder.get("shape")))
        for rel in relations.relations():
            ref = obj.get(rel.name)
            if ref is None:
                continue
            known(ref, "{}.{}".format(field, rel.name))
            if ref == obj["id"]:
                raise ctx.invalid("{}.{}".format(field, rel.name), "{} is placed {} itself; name another object".format(obj["id"], rel.name))
        prim = S.get(obj.get("shape"))
        if prim is not None and prim.name == "arrow3d":
            for end in ("from", "to"):
                if isinstance(obj.get(end), str):
                    known(obj[end], "{}.{}".format(field, end))
    # Containment: no object is (transitively) in itself.
    for i, obj in enumerate(objects):
        seen = [str(obj["id"])]
        holder = obj.get("in")
        while holder is not None:
            if holder in seen:
                raise ctx.error("relation_cycle", "objects[{}]: {} is in itself ({})".format(i, obj["id"], " → ".join(seen + [holder])),
                                field="objects[{}].in".format(i), cycle=seen + [holder])
            seen.append(holder)
            holder = by_id.get(holder, {}).get("in")
    # A child of a laid-out group is placed by its layout; a child of a free group only relative to its siblings.
    for i, obj in enumerate(objects):
        holder_id = obj.get("in")
        if holder_id is None:
            continue
        holder = by_id[holder_id]
        layout = holder.get("layout") or "row"
        field = "objects[{}]".format(i)
        rels = [rel.name for rel in relations.relations() if obj.get(rel.name) is not None]
        if layout != "free":
            if rels or obj.get("pos") is not None:
                raise ctx.invalid("{}.{}".format(field, rels[0] if rels else "pos"), "{} is in {} (layout {}), which places it; drop {} or use "
                                  "layout free".format(obj["id"], holder_id, layout, rels[0] if rels else "pos"))
        else:
            for name in rels:
                ref = obj[name]
                if by_id[ref].get("in") != holder_id:
                    raise ctx.invalid("{}.{}".format(field, name), "{} is in the free group {}; it is placed relative to its siblings "
                                      "({}), not {}".format(obj["id"], holder_id, ", ".join(o["id"] for o in objects if o.get("in") == holder_id
                                                                                           and o["id"] != obj["id"]) or "none", ref))
    for i, obj in enumerate(objects):
        prim = S.get(obj.get("shape"))
        if prim is not None and prim.container and obj.get("layout") not in (None,) + tuple(layouts.names()):
            raise ctx.invalid("objects[{}].layout".format(i), "layout is one of: {}".format(", ".join(layouts.names())))
    for i, link in enumerate(links):
        known(link.get("from"), "links[{}].from".format(i))
        known(link.get("to"), "links[{}].to".format(i))
    if isinstance(camera_value, dict) and isinstance(camera_value.get("target"), str):
        known(camera_value["target"], "camera.target")
