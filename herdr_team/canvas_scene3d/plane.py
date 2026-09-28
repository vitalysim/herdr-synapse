"""``plane``: a flat slab lying down: a floor, a table top, a base. ``size [w, d]`` and ``thickness`` (0.02); with no
``size`` (or ``"fit"``) it fits what stands on it, with a margin (4 × 4 when nothing does)."""
from __future__ import annotations

from typing import Any, Dict, List

from herdr_team.canvas_scene3d import Face, Primitive, _shapes

ORDER = 50
#: The size of a fitted plane nothing stands on, and the one the solver places its riders against.
DEFAULT = (4.0, 4.0)
FIT = "fit"


def normalize(ctx: Any, obj: Dict[str, Any], field: str) -> Dict[str, Any]:
    size = obj.get("size")
    return {"size": FIT if size is None or size == FIT else _shapes.size3(ctx, obj, field, "size", DEFAULT),
            "thickness": _shapes.scalar(ctx, obj, field, "thickness", 0.02, 0.001, 100.0)}


def hugs(params: Dict[str, Any]) -> bool:
    return params.get("size") in (None, FIT)


def extent(params: Dict[str, Any]) -> tuple:
    w, d = DEFAULT if hugs(params) else params["size"]
    return (float(w), float(params["thickness"]), float(d))


def faces(params: Dict[str, Any], detail: int) -> List[Face]:
    return _shapes.box_faces(*extent(params))


def describe(params: Dict[str, Any]) -> str:
    return "plane fit" if hugs(params) else "plane {}".format("×".join(_shapes.g(v) for v in params["size"]))


PRIMITIVES = (
    Primitive(name="plane", params=("size", "thickness"), normalize=normalize, extent=extent, faces=faces, describe=describe, order=50,
              hugs=hugs,
              doc="a flat slab lying down: size [w, d], or none to fit what stands on it with a margin; thickness (0.02); "
                  "others go on it",
              example='{"op": "scene3d", "id": "floor", "title": "A floor", "objects": [{"id": "f", "shape": "plane"}, '
                      '{"id": "b", "shape": "box", "on": "f", "label": "box"}]}'),
)
