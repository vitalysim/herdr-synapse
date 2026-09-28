"""Relations: how an object is placed against another (canvas v2 phase 4, 3.3). A registry, one function each.

An object's position is the centre of its box's footprint (x, z) and its base
height (y). A relation places it on the axes it ``sets`` against its reference
``R`` (the named object's box, in the frame both share); the solver fills the
other axes from the reference too (centred on x and z, base-aligned on y, or
as ``align`` says). At most one relation may set an axis.

| Relation | Sets | Placement | Default gap |
|---|---|---|---|
| ``on`` | y | base = R.top (contact); ``at: [dx, dz]`` offsets it from R's centre | 0 |
| ``above`` / ``below`` | y | base = R.top + gap / top = R.base − gap | m |
| ``left_of`` / ``right_of`` | x | max x = R.min x − gap / min x = R.max x + gap | m |
| ``in_front_of`` / ``behind`` | z | min z = R.max z + gap / max z = R.min z − gap | m |
| ``inside`` | y | centred in R, base on R's inner floor (R.base + ``WALL``); it must fit (``does_not_fit``) | 0 |
| ``around`` | x, z | on a ring (``ring``, default R's footprint radius + gap + its own) at ``angle`` (default: evenly among the objects around the same R, in spec order, from −90°) | m |

Gap tokens ``s``, ``m`` and ``l`` are 10, 25 and 50 % of the larger of the two
footprints' largest side; a number is in the scene's units.

``register_relation`` adds one (a test module or an extension); a relation is
data plus one pure ``place`` function.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

Vec = Tuple[float, float, float]
Box = Tuple[float, float, float, float, float, float]

#: The wall an object ``inside`` another keeps from its host's sides and floor.
WALL = 0.02
#: The axis groups: one relation per group.
GROUPS = ("vertical", "x", "z", "xz")


@dataclass(frozen=True)
class Placing:
    """What a relation places from: the object's size, its reference's box and the gap in units."""

    ext: Vec
    ref: Box
    gap: float
    obj: Mapping[str, Any]
    #: ``(index, count)`` among the objects placed by this relation against the same reference, in spec order.
    siblings: Tuple[int, int] = (0, 1)


@dataclass(frozen=True)
class Relation:
    name: str
    #: Its axis group (``GROUPS``).
    group: str
    #: The axes it sets (``x``, ``y``, ``z``).
    sets: Tuple[str, ...]
    #: ``Placing -> {axis: value}`` for the axes it sets (x and z: the footprint centre; y: the base).
    place: Callable[[Placing], Dict[str, float]]
    #: The default gap: a token (``s``, ``m``, ``l``) or a number.
    gap: Any = "m"
    #: It touches its reference by design (``on``, ``inside``): the collision test lets it.
    contact: bool = False
    #: It takes ``at: [dx, dz]`` (an offset from its reference's centre).
    takes_at: bool = False
    #: The way the solver pushes the object off a collision: a unit axis, or ``radial`` (away from R's centre).
    push: Any = (1.0, 0.0, 0.0)
    #: ``Placing -> message`` when the object cannot be placed so (``inside`` a host too small), else None.
    fits: Optional[Callable[[Placing], Optional[str]]] = None
    #: ``left_of`` reads as ``left of`` in text.
    words: str = ""
    doc: str = ""
    order: int = 100


def _cx(box: Box) -> float:
    return (box[0] + box[3]) / 2.0


def _cz(box: Box) -> float:
    return (box[2] + box[5]) / 2.0


def _on(p: Placing) -> Dict[str, float]:
    return {"y": p.ref[4]}


def _above(p: Placing) -> Dict[str, float]:
    return {"y": p.ref[4] + p.gap}


def _below(p: Placing) -> Dict[str, float]:
    return {"y": p.ref[1] - p.gap - p.ext[1]}


def _left_of(p: Placing) -> Dict[str, float]:
    return {"x": p.ref[0] - p.gap - p.ext[0] / 2.0}


def _right_of(p: Placing) -> Dict[str, float]:
    return {"x": p.ref[3] + p.gap + p.ext[0] / 2.0}


def _in_front_of(p: Placing) -> Dict[str, float]:
    return {"z": p.ref[5] + p.gap + p.ext[2] / 2.0}


def _behind(p: Placing) -> Dict[str, float]:
    return {"z": p.ref[2] - p.gap - p.ext[2] / 2.0}


def _inside(p: Placing) -> Dict[str, float]:
    return {"y": p.ref[1] + WALL}


def _inside_fits(p: Placing) -> Optional[str]:
    room = (p.ref[3] - p.ref[0] - 2 * WALL, p.ref[4] - p.ref[1] - WALL, p.ref[5] - p.ref[2] - 2 * WALL)
    if all(p.ext[i] <= room[i] + 1e-9 for i in range(3)):
        return None
    return "{:.2f}×{:.2f}×{:.2f} does not fit in {:.2f}×{:.2f}×{:.2f}".format(p.ext[0], p.ext[1], p.ext[2], max(0.0, room[0]),
                                                                           max(0.0, room[1]), max(0.0, room[2]))


def ring_radius(p: Placing) -> float:
    """The ring an object ``around`` R sits on: its ``ring``, else R's footprint radius + gap + its own."""
    given = p.obj.get("ring")
    if isinstance(given, (int, float)) and not isinstance(given, bool):
        return float(given)
    ref_r = math.hypot(p.ref[3] - p.ref[0], p.ref[5] - p.ref[2]) / 2.0
    own = math.hypot(p.ext[0], p.ext[2]) / 2.0
    return ref_r + p.gap + own


def ring_angle(p: Placing) -> float:
    given = p.obj.get("angle")
    if isinstance(given, (int, float)) and not isinstance(given, bool):
        return float(given)
    index, count = p.siblings
    return -90.0 + 360.0 * index / max(1, count)


def _around(p: Placing) -> Dict[str, float]:
    radius, angle = ring_radius(p), math.radians(ring_angle(p))
    return {"x": _cx(p.ref) + radius * math.cos(angle), "z": _cz(p.ref) + radius * math.sin(angle)}


RELATIONS: Tuple[Relation, ...] = (
    Relation("on", "vertical", ("y",), _on, gap=0, contact=True, takes_at=True, push=(1.0, 0.0, 0.0), words="on", order=10,
             doc="rests on R's top, centred on it (at: [dx, dz] offsets it)"),
    Relation("above", "vertical", ("y",), _above, takes_at=True, push=(0.0, 1.0, 0.0), words="above", order=20,
             doc="floats above R by the gap, centred on it"),
    Relation("below", "vertical", ("y",), _below, takes_at=True, push=(0.0, -1.0, 0.0), words="below", order=30,
             doc="hangs below R by the gap, centred on it"),
    Relation("inside", "vertical", ("y",), _inside, gap=0, contact=True, takes_at=True, push=(1.0, 0.0, 0.0), fits=_inside_fits,
             words="inside", order=40, doc="centred in R, on its inner floor; must fit"),
    Relation("left_of", "x", ("x",), _left_of, push=(-1.0, 0.0, 0.0), words="left of", order=50, doc="to R's left (−x), base-aligned"),
    Relation("right_of", "x", ("x",), _right_of, push=(1.0, 0.0, 0.0), words="right of", order=60, doc="to R's right (+x), base-aligned"),
    Relation("in_front_of", "z", ("z",), _in_front_of, push=(0.0, 0.0, 1.0), words="in front of", order=70,
             doc="in front of R (+z, toward the viewer), base-aligned"),
    Relation("behind", "z", ("z",), _behind, push=(0.0, 0.0, -1.0), words="behind", order=80, doc="behind R (−z), base-aligned"),
    Relation("around", "xz", ("x", "z"), _around, push="radial", words="around", order=90,
             doc="on a ring around R (ring, angle; default evenly spaced from −90°), base-aligned"),
)

_REGISTRY: Dict[str, Relation] = {}


def register_relation(rel: Relation) -> Relation:
    """Add a relation; a second one with the same name, an unknown group or axis is a programming error."""
    if rel.group not in GROUPS:
        raise ValueError("relation {}: group {!r} is not one of {}".format(rel.name, rel.group, ", ".join(GROUPS)))
    if not rel.sets or any(axis not in ("x", "y", "z") for axis in rel.sets):
        raise ValueError("relation {}: sets is a non-empty tuple of x, y, z".format(rel.name))
    if rel.name in _REGISTRY and _REGISTRY[rel.name] is not rel:
        raise ValueError("relation {} is registered twice".format(rel.name))
    _REGISTRY[rel.name] = rel
    return rel


def _unregister(name: str) -> None:
    """Test hook: take a relation ``register_relation`` added out again."""
    _REGISTRY.pop(name, None)


for _rel in RELATIONS:
    register_relation(_rel)


def get(name: Any) -> Optional[Relation]:
    return _REGISTRY.get(name) if isinstance(name, str) else None


def relations() -> List[Relation]:
    return sorted(_REGISTRY.values(), key=lambda rel: (rel.order, rel.name))


def names() -> List[str]:
    return [rel.name for rel in relations()]


def of(obj: Mapping[str, Any]) -> List[Relation]:
    """The relations an object uses, in registry order."""
    return [rel for rel in relations() if obj.get(rel.name) is not None]


def gap_units(value: Any, own: Sequence[float], ref: Sequence[float]) -> float:
    """A gap in units: a token is a share of the larger of the two footprints' largest side."""
    from herdr_team.canvas_scene3d._spec import GAP_TOKENS

    if isinstance(value, str) and value in GAP_TOKENS:
        side = max(float(own[0]), float(own[2]), float(ref[3] - ref[0]), float(ref[5] - ref[2]))
        return GAP_TOKENS[value] * side
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return 0.0
