"""``cone``: ``radius`` (0.5) at the base and ``height`` (1), point up."""
from __future__ import annotations

from typing import Any, Dict, List

from herdr_team.canvas_scene3d import Face, Primitive, _shapes

ORDER = 40


def normalize(ctx: Any, obj: Dict[str, Any], field: str) -> Dict[str, Any]:
    return {"radius": _shapes.scalar(ctx, obj, field, "radius", 0.5), "height": _shapes.scalar(ctx, obj, field, "height", 1.0)}


def extent(params: Dict[str, Any]) -> tuple:
    r = float(params["radius"])
    return (2 * r, float(params["height"]), 2 * r)


def faces(params: Dict[str, Any], detail: int) -> List[Face]:
    return _shapes.cylinder_faces(float(params["radius"]), float(params["height"]), detail, top_radius=0.0)


def silhouette(params: Dict[str, Any], proj: Any) -> List[Dict[str, Any]]:
    return _shapes.cone_outline(proj, float(params["radius"]), float(params["height"]))


def describe(params: Dict[str, Any]) -> str:
    return "cone r{} h{}".format(_shapes.g(params["radius"]), _shapes.g(params["height"]))


PRIMITIVES = (
    Primitive(name="cone", params=("radius", "height"), normalize=normalize, extent=extent, faces=faces, silhouette=silhouette,
              describe=describe, order=40, doc="a cone, point up: radius (0.5), height (1)",
              example='{"op": "scene3d", "id": "alert3d", "title": "An alert", "objects": [{"id": "a", "shape": "cone", "radius": 0.35, '
                      '"height": 0.7, "tone": "danger", "label": "fraud"}]}'),
)
