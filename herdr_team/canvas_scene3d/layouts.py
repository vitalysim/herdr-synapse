"""Group layouts: where a group's children go in the group's own frame (canvas v2 phase 4, 3.3). A registry.

A layout gets the children's sizes in spec order and returns each child's
position (footprint centre x and z, base y) in the group's frame; the solver
then centres the result on x and z with its base at y = 0, so the group is
placed as one box like any other object.

- ``row``: along +x, ``gap`` apart, centred on z;
- ``stack``: along +y, each on the previous one, ``gap`` apart;
- ``grid``: ``cols`` columns along +x (default the square root of the count, rounded up), rows along +z;
- ``ring``: evenly on a circle of ``radius`` (default: wide enough that neighbours keep ``gap``), from −90°;
- ``free``: the children place themselves by relations to their siblings (the solver does it).
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

Vec = Tuple[float, float, float]


@dataclass(frozen=True)
class Layout:
    name: str
    #: ``(sizes, group, gap) -> [position]``; None for ``free`` (the solver places the children by relations).
    place: Optional[Callable[[Sequence[Vec], Mapping[str, Any], float], List[Vec]]]
    #: The group fields it reads beyond ``gap``.
    params: Tuple[str, ...] = ()
    doc: str = ""
    order: int = 100


def _row(sizes: Sequence[Vec], group: Mapping[str, Any], gap: float) -> List[Vec]:
    out, cursor = [], 0.0
    for w, _h, _d in sizes:
        out.append((cursor + w / 2.0, 0.0, 0.0))
        cursor += w + gap
    return out


def _stack(sizes: Sequence[Vec], group: Mapping[str, Any], gap: float) -> List[Vec]:
    out, cursor = [], 0.0
    for _w, h, _d in sizes:
        out.append((0.0, cursor, 0.0))
        cursor += h + gap
    return out


def columns(group: Mapping[str, Any], count: int) -> int:
    cols = group.get("cols")
    if isinstance(cols, int) and not isinstance(cols, bool) and cols > 0:
        return cols
    return max(1, int(math.ceil(math.sqrt(max(1, count)))))


def _grid(sizes: Sequence[Vec], group: Mapping[str, Any], gap: float) -> List[Vec]:
    cols = columns(group, len(sizes))
    rows = int(math.ceil(len(sizes) / float(cols))) if sizes else 0
    widths = [max((sizes[i][0] for i in range(c, len(sizes), cols)), default=0.0) for c in range(cols)]
    depths = [max((sizes[i][2] for i in range(r * cols, min(len(sizes), (r + 1) * cols))), default=0.0) for r in range(rows)]
    xs, cursor = [], 0.0
    for width in widths:
        xs.append(cursor + width / 2.0)
        cursor += width + gap
    zs, cursor = [], 0.0
    for depth in depths:
        zs.append(cursor + depth / 2.0)
        cursor += depth + gap
    return [(xs[i % cols], 0.0, zs[i // cols]) for i in range(len(sizes))]


def ring_radius(sizes: Sequence[Vec], group: Mapping[str, Any], gap: float) -> float:
    given = group.get("radius")
    if isinstance(given, (int, float)) and not isinstance(given, bool) and given > 0:
        return float(given)
    if len(sizes) <= 1:
        return 0.0
    # Neighbours on the circle keep ``gap`` between the circles around their footprints.
    widest = max(math.hypot(w, d) for w, _h, d in sizes)
    return (widest + gap) / (2.0 * math.sin(math.pi / len(sizes)))


def _ring(sizes: Sequence[Vec], group: Mapping[str, Any], gap: float) -> List[Vec]:
    radius = ring_radius(sizes, group, gap)
    out = []
    for index in range(len(sizes)):
        angle = math.radians(-90.0 + 360.0 * index / max(1, len(sizes)))
        out.append((radius * math.cos(angle), 0.0, radius * math.sin(angle)))
    return out


LAYOUTS: Tuple[Layout, ...] = (
    Layout("row", _row, doc="along +x, gap apart", order=10),
    Layout("stack", _stack, doc="along +y, each on the previous one", order=20),
    Layout("grid", _grid, params=("cols",), doc="cols columns along +x, rows along +z", order=30),
    Layout("ring", _ring, params=("radius",), doc="evenly on a circle (radius), from −90°", order=40),
    Layout("free", None, doc="children place themselves by relations to their siblings", order=50),
)

_REGISTRY: Dict[str, Layout] = {}


def register_layout(layout: Layout) -> Layout:
    if layout.name in _REGISTRY and _REGISTRY[layout.name] is not layout:
        raise ValueError("layout {} is registered twice".format(layout.name))
    _REGISTRY[layout.name] = layout
    return layout


def _unregister(name: str) -> None:
    _REGISTRY.pop(name, None)


for _layout in LAYOUTS:
    register_layout(_layout)


def get(name: Any) -> Optional[Layout]:
    return _REGISTRY.get(name) if isinstance(name, str) else None


def layouts() -> List[Layout]:
    return sorted(_REGISTRY.values(), key=lambda layout: (layout.order, layout.name))


def names() -> List[str]:
    return [layout.name for layout in layouts()]
