"""``gltf``: a glTF 2.0 model from the team's ``artifacts/`` (``src``: a ``.glb``, or a ``.gltf`` with its files beside it),
scaled uniformly to ``height`` or ``size`` (default: its own size) and centred on its footprint with its base at y = 0.

The file is read, checked and packed into one GLB when the op is applied
(``_gltf``); the element stores it as an asset, so the page never reads
``artifacts/`` and a later edit of the file never changes a drawn scene. The
picture draws the model as its labelled box, dashed.
"""
from __future__ import annotations

from typing import Any, Dict, List

from herdr_team.canvas_scene3d import Face, Loader, Primitive, _gltf, _shapes

ORDER = 90


def normalize(ctx: Any, obj: Dict[str, Any], field: str) -> Dict[str, Any]:
    src = obj.get("src")
    if not isinstance(src, str) or not src.strip():
        raise ctx.invalid(field + ".src", "a gltf needs src: a .glb or .gltf file under artifacts/ (models/arm.glb)")
    if not src.lower().endswith((".glb", ".gltf")):
        raise ctx.invalid(field + ".src", "{}.src is a .glb or .gltf file under artifacts/".format(field))
    if obj.get("height") is not None and obj.get("size") is not None:
        raise ctx.invalid(field + ".size", "give height or size, not both")
    out: Dict[str, Any] = {"src": src}
    if obj.get("height") is not None:
        out["height"] = _shapes.scalar(ctx, obj, field, "height", 1.0)
    if obj.get("size") is not None:
        size = obj["size"]
        out["size"] = _shapes.scalar(ctx, obj, field, "size", 1.0) if not isinstance(size, list) else \
            _shapes.size3(ctx, obj, field, "size", (1.0, 1.0, 1.0))
    center = obj.get("center", True)
    if center is not True:
        raise ctx.invalid(field + ".center", "a model is always centred on its footprint with its base at y = 0 (center: true)")
    return out


def scale(params: Dict[str, Any], native: Any) -> float:
    """The uniform scale that fits the model's native size to ``height`` or ``size`` (1 when neither is given)."""
    w, h, d = (max(1e-9, float(v)) for v in native)
    if params.get("height") is not None:
        return float(params["height"]) / h
    size = params.get("size")
    if isinstance(size, list):
        return min(float(size[0]) / w, float(size[1]) / h, float(size[2]) / d)
    if size is not None:
        return float(size) / max(w, h, d)
    return 1.0


def native(params: Dict[str, Any]) -> Any:
    model = params.get("model")
    if isinstance(model, dict) and isinstance(model.get("native"), list) and len(model["native"]) == 3:
        return model["native"]
    return None


def extent(params: Dict[str, Any]) -> tuple:
    size = native(params)
    if size is None:
        # Not read yet (a malformed element, or a unit test with no FetchIO): a unit proxy, or the size it asked for.
        if params.get("height") is not None:
            h = float(params["height"])
            return (h, h, h)
        given = params.get("size")
        if isinstance(given, list):
            return tuple(float(v) for v in given)
        if given is not None:
            return (float(given),) * 3
        return (1.0, 1.0, 1.0)
    k = scale(params, size)
    return tuple(max(0.001, float(v) * k) for v in size)


def faces(params: Dict[str, Any], detail: int) -> List[Face]:
    return _shapes.box_faces(*extent(params))


def describe(params: Dict[str, Any]) -> str:
    return "gltf {} {}".format(_spec_quote(params.get("src")), "×".join(_shapes.g(v) for v in extent(params)))


def _spec_quote(text: Any) -> str:
    return '"' + str(text).replace('"', '\\"') + '"'


LOADER = Loader(name="gltf", field="src", suffixes=(".glb", ".gltf"), read=_gltf.read,
                doc="a .glb (16 MB) or .gltf (2 MB, files beside it) under artifacts/, packed into one GLB asset")

PRIMITIVES = (
    Primitive(name="gltf", params=("src", "height", "size", "center"), normalize=normalize, extent=extent, faces=faces, describe=describe,
              loader=LOADER, order=90,
              doc="a glTF 2.0 model from artifacts/: src, height or size (uniform), no Draco/meshopt/KTX2, 300k triangles",
              example='{"op": "scene3d", "id": "arm", "title": "Arm v3", "objects": [{"id": "bench", "shape": "box", "size": [2, 0.9, 1]}, '
                      '{"id": "m", "shape": "gltf", "src": "models/arm-v3.glb", "height": 0.8, "on": "bench", "label": "Arm v3"}]}'),
)
