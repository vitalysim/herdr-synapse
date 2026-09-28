"""``plane``: a flat slab of ``size [w, d]`` (4 × 4) and ``thickness`` (0.02), lying down: a floor, a table top, a base."""
from __future__ import annotations

from typing import Any, Dict, List

from herdr_team.canvas_scene3d import Face, Primitive, _shapes

ORDER = 50


def normalize(ctx: Any, obj: Dict[str, Any], field: str) -> Dict[str, Any]:
    return {"size": _shapes.size3(ctx, obj, field, "size", (4.0, 4.0)),
            "thickness": _shapes.scalar(ctx, obj, field, "thickness", 0.02, 0.001, 100.0)}


def extent(params: Dict[str, Any]) -> tuple:
    w, d = params["size"]
    return (float(w), float(params["thickness"]), float(d))


def faces(params: Dict[str, Any], detail: int) -> List[Face]:
    return _shapes.box_faces(*extent(params))


def describe(params: Dict[str, Any]) -> str:
    return "plane {}".format("×".join(_shapes.g(v) for v in params["size"]))


PRIMITIVES = (
    Primitive(name="plane", params=("size", "thickness"), normalize=normalize, extent=extent, faces=faces, describe=describe, order=50,
              doc="a flat slab lying down: size [w, d] (4, 4), thickness (0.02); others go on it",
              example='{"op": "scene3d", "id": "floor", "title": "A floor", "objects": [{"id": "f", "shape": "plane", "size": [6, 4]}, '
                      '{"id": "b", "shape": "box", "on": "f", "label": "box"}]}'),
)
