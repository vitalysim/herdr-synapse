"""``group``: holds objects (``in: <group>``) and lays them out as one: ``layout`` row (default), stack, grid (``cols``),
ring (``radius``) or free; ``gap`` (0.2) spaces the children. The group is then placed like any object."""
from __future__ import annotations

from typing import Any, Dict, List

from herdr_team.canvas_scene3d import Face, Primitive, _shapes, layouts

ORDER = 80
DEFAULT_GAP = 0.2


def normalize(ctx: Any, obj: Dict[str, Any], field: str) -> Dict[str, Any]:
    layout = obj.get("layout", "row")
    if layouts.get(layout) is None:
        from herdr_team.canvas_scene3d import _spec

        raise ctx.invalid(field + ".layout", "{}.layout is one of: {}{}".format(field, ", ".join(layouts.names()),
                                                                             _spec.nearest(layout, layouts.names())))
    out: Dict[str, Any] = {"layout": layout}
    cols = obj.get("cols")
    if cols is not None:
        if not isinstance(cols, int) or isinstance(cols, bool) or not 1 <= cols <= 50:
            raise ctx.invalid(field + ".cols", "{}.cols is a whole number from 1 to 50".format(field))
        out["cols"] = cols
    if obj.get("radius") is not None:
        out["radius"] = _shapes.scalar(ctx, obj, field, "radius", 1.0)
    return out


def extent(params: Dict[str, Any]) -> tuple:
    found = params.get("_extent")
    if isinstance(found, (list, tuple)) and len(found) == 3:
        return tuple(max(0.001, float(v)) for v in found)
    return (0.001, 0.001, 0.001)


def faces(params: Dict[str, Any], detail: int) -> List[Face]:
    return []


def describe(params: Dict[str, Any]) -> str:
    return "group {}".format(params.get("layout", "row"))


PRIMITIVES = (
    Primitive(name="group", params=("layout", "cols", "radius"), normalize=normalize, extent=extent, faces=faces, describe=describe,
              container=True, order=80, doc="holds objects (their in) and lays them out: layout row|stack|grid|ring|free, gap (0.2), cols, radius",
              example='{"op": "scene3d", "id": "pool", "title": "A pool", "objects": [{"id": "g", "shape": "group", "layout": "row", '
                      '"gap": 0.3}, {"id": "a", "shape": "box", "in": "g"}, {"id": "b", "shape": "box", "in": "g"}]}'),
)
