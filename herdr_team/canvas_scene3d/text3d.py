"""``text3d``: a line of text standing in the scene (``text``, 80 characters or fewer; ``height`` 0.3 units; ``billboard``
true turns it to face the camera on the page). The picture draws it as text at its projected anchor."""
from __future__ import annotations

from typing import Any, Dict, List

from herdr_team import canvas_text
from herdr_team.canvas_scene3d import Face, Primitive, _shapes, _spec

ORDER = 60
DEPTH = 0.02


def normalize(ctx: Any, obj: Dict[str, Any], field: str) -> Dict[str, Any]:
    raw = obj.get("text")
    if raw is None:
        raise ctx.invalid(field + ".text", "a text3d needs text (80 characters or fewer)")
    text = _spec.text(ctx, raw, field + ".text", _spec.MAX_LABEL)
    if not text:
        raise ctx.invalid(field + ".text", "a text3d needs text (80 characters or fewer)")
    billboard = obj.get("billboard", True)
    if not isinstance(billboard, bool):
        raise ctx.invalid(field + ".billboard", "billboard is true (it faces the camera) or false")
    return {"text": text, "height": _shapes.scalar(ctx, obj, field, "height", 0.3, 0.001, 1000.0), "billboard": billboard}


def extent(params: Dict[str, Any]) -> tuple:
    height = float(params["height"])
    width = canvas_text.measure(str(params["text"]), "normal", 100.0, 500).width / 100.0 * height
    return (max(width, height * 0.5), height, DEPTH)


def faces(params: Dict[str, Any], detail: int) -> List[Face]:
    return []


def describe(params: Dict[str, Any]) -> str:
    return "text3d {} h{}".format(_spec_quote(params["text"]), _shapes.g(params["height"]))


def _spec_quote(text: str) -> str:
    return '"' + str(text).replace('"', '\\"') + '"'


PRIMITIVES = (
    Primitive(name="text3d", params=("text", "height", "billboard"), normalize=normalize, extent=extent, faces=faces, describe=describe,
              order=60, doc="a line of text in the scene: text, height (0.3), billboard (true)",
              example='{"op": "scene3d", "id": "sign", "title": "A sign", "objects": [{"id": "p", "shape": "plane", "size": [3, 2]}, '
                      '{"id": "t", "shape": "text3d", "text": "loading dock", "height": 0.25, "above": "p", "gap": 0.2}]}'),
)
