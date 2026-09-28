"""``cylinder``: ``radius`` (0.5) and ``height`` (1), upright; ``segments`` (24) is how smooth the page draws it."""
from __future__ import annotations

from typing import Any, Dict, List

from herdr_team.canvas_scene3d import Face, Primitive, _shapes

ORDER = 30


def normalize(ctx: Any, obj: Dict[str, Any], field: str) -> Dict[str, Any]:
    out = {"radius": _shapes.scalar(ctx, obj, field, "radius", 0.5), "height": _shapes.scalar(ctx, obj, field, "height", 1.0)}
    segments = obj.get("segments")
    if segments is not None and (not isinstance(segments, int) or isinstance(segments, bool) or not 8 <= segments <= 96):
        raise ctx.invalid(field + ".segments", "{}.segments is a whole number from 8 to 96".format(field))
    out["segments"] = segments if segments is not None else 24
    return out


def extent(params: Dict[str, Any]) -> tuple:
    r = float(params["radius"])
    return (2 * r, float(params["height"]), 2 * r)


def faces(params: Dict[str, Any], detail: int) -> List[Face]:
    return _shapes.cylinder_faces(float(params["radius"]), float(params["height"]), detail)


def silhouette(params: Dict[str, Any], proj: Any) -> List[Dict[str, Any]]:
    return _shapes.cylinder_outline(proj, float(params["radius"]), float(params["height"]))


def describe(params: Dict[str, Any]) -> str:
    return "cylinder r{} h{}".format(_shapes.g(params["radius"]), _shapes.g(params["height"]))


PRIMITIVES = (
    Primitive(name="cylinder", params=("radius", "height", "segments"), normalize=normalize, extent=extent, faces=faces,
              silhouette=silhouette, describe=describe, order=30, doc="an upright cylinder: radius (0.5), height (1)",
              example='{"op": "scene3d", "id": "db3d", "title": "A database", "objects": [{"id": "db", "shape": "cylinder", "radius": 0.6, '
                      '"height": 1.4, "tone": "success", "label": "Postgres"}]}'),
)
