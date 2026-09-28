"""``sphere``: a ball of ``radius`` (0.5) standing on its base; it projects to a circle."""
from __future__ import annotations

from typing import Any, Dict, List

from herdr_team.canvas_scene3d import Face, Primitive, _shapes

ORDER = 20


def normalize(ctx: Any, obj: Dict[str, Any], field: str) -> Dict[str, Any]:
    return {"radius": _shapes.scalar(ctx, obj, field, "radius", 0.5)}


def extent(params: Dict[str, Any]) -> tuple:
    r = float(params["radius"])
    return (2 * r, 2 * r, 2 * r)


def faces(params: Dict[str, Any], detail: int) -> List[Face]:
    return _shapes.sphere_faces(float(params["radius"]), detail)


def silhouette(params: Dict[str, Any], proj: Any) -> List[Dict[str, Any]]:
    return _shapes.sphere_outline(proj, float(params["radius"]))


def describe(params: Dict[str, Any]) -> str:
    return "sphere r{}".format(_shapes.g(params["radius"]))


PRIMITIVES = (
    Primitive(name="sphere", params=("radius",), normalize=normalize, extent=extent, faces=faces, silhouette=silhouette, describe=describe,
              order=20, doc="a ball: radius (0.5)",
              example='{"op": "scene3d", "id": "ball", "title": "A ball", "objects": [{"id": "b", "shape": "sphere", "radius": 0.4, '
                      '"tone": "warning", "label": "cache"}]}'),
)
