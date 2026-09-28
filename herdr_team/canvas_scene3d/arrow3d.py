"""``arrow3d``: an arrow between two objects or points (``from``, ``to``: object ids or ``[x, y, z]``; ``radius`` 0.04,
``head`` 0.15). It runs between the facing sides of the objects' boxes. Scene ``links`` are drawn the same way."""
from __future__ import annotations

from typing import Any, Dict, List

from herdr_team.canvas_scene3d import Face, Primitive, _shapes, _spec

ORDER = 70


def _end(ctx: Any, value: Any, field: str) -> Any:
    if isinstance(value, str):
        return _spec.ident(ctx, value, field)
    return _spec.vector(ctx, value, field, 3)


def normalize(ctx: Any, obj: Dict[str, Any], field: str) -> Dict[str, Any]:
    if obj.get("from") is None or obj.get("to") is None:
        raise ctx.invalid(field + (".from" if obj.get("from") is None else ".to"), "an arrow3d needs from and to (object ids or [x, y, z])")
    return {"from": _end(ctx, obj["from"], field + ".from"), "to": _end(ctx, obj["to"], field + ".to"),
            "radius": _shapes.scalar(ctx, obj, field, "radius", 0.04, 0.001, 100.0),
            "head": _shapes.scalar(ctx, obj, field, "head", 0.15, 0.001, 100.0)}


def extent(params: Dict[str, Any]) -> tuple:
    r = 2 * float(params["radius"])
    return (r, r, r)


def faces(params: Dict[str, Any], detail: int) -> List[Face]:
    return []


def _name(end: Any) -> str:
    return end if isinstance(end, str) else "({})".format(", ".join(_shapes.g(v) for v in end))


def describe(params: Dict[str, Any]) -> str:
    return "arrow3d {} → {}".format(_name(params["from"]), _name(params["to"]))


PRIMITIVES = (
    Primitive(name="arrow3d", params=("from", "to", "radius", "head"), normalize=normalize, extent=extent, faces=faces, describe=describe,
              order=70, doc="an arrow between objects or points: from, to, radius (0.04), head (0.15)",
              example='{"op": "scene3d", "id": "flow3d", "title": "A flow", "objects": [{"id": "a", "shape": "box"}, '
                      '{"id": "b", "shape": "box", "right_of": "a", "gap": 1}, {"id": "ab", "shape": "arrow3d", "from": "a", "to": "b"}]}'),
)
