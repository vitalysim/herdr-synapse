"""``box``: a cuboid of ``size [w, h, d]`` (default 1 × 1 × 1). Also what a glTF model and an unknown shape draw as."""
from __future__ import annotations

from typing import Any, Dict, List

from herdr_team.canvas_scene3d import Face, Primitive, _shapes

ORDER = 10


def normalize(ctx: Any, obj: Dict[str, Any], field: str) -> Dict[str, Any]:
    return {"size": _shapes.size3(ctx, obj, field, "size", (1.0, 1.0, 1.0))}


def extent(params: Dict[str, Any]) -> tuple:
    w, h, d = params["size"]
    return (float(w), float(h), float(d))


def faces(params: Dict[str, Any], detail: int) -> List[Face]:
    return _shapes.box_faces(*extent(params))


def describe(params: Dict[str, Any]) -> str:
    return "box {}".format("×".join(_shapes.g(v) for v in params["size"]))


PRIMITIVES = (
    Primitive(name="box", params=("size",), normalize=normalize, extent=extent, faces=faces, describe=describe, order=10,
              doc="a cuboid: size [w, h, d] (1, 1, 1)",
              example='{"op": "scene3d", "id": "crate", "title": "A crate", "objects": [{"id": "c", "shape": "box", "size": [1.2, 0.6, 0.8], '
                      '"tone": "info", "label": "crate"}]}'),
)
