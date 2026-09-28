"""A test-only scene3d primitive (canvas v2 phase 4, 8.2 and gate G8): the one-module proof.

``canvas_scene3d._load_extra("prim_torus")`` registers ``torus`` with nothing
else changed: the op takes ``shape: "torus"``, the solver places it by its
extent, ``look`` describes it, the projection draws its faces and the catalog
lists it. ``_unload("prim_torus")`` takes it out again.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List

from herdr_team.canvas_scene3d import Face, Primitive, _shapes
from herdr_team.canvas_scene3d import _vec as V

ORDER = 500


def normalize(ctx: Any, obj: Dict[str, Any], field: str) -> Dict[str, Any]:
    radius = _shapes.scalar(ctx, obj, field, "radius", 0.5)
    tube = _shapes.scalar(ctx, obj, field, "tube", 0.15)
    if tube >= radius:
        raise ctx.invalid(field + ".tube", "{}.tube is less than its radius".format(field))
    return {"radius": radius, "tube": tube}


def extent(params: Dict[str, Any]) -> tuple:
    outer = 2 * (float(params["radius"]) + float(params["tube"]))
    return (outer, 2 * float(params["tube"]), outer)


def faces(params: Dict[str, Any], detail: int) -> List[Face]:
    """A lying ring of ``detail`` segments round and 8 round the tube: quads facing out of the tube."""
    big, small = float(params["radius"]), float(params["tube"])
    n, m = max(6, int(detail)), 8

    def point(i: int, j: int) -> V.Vec:
        u, v = 2 * math.pi * i / n, 2 * math.pi * j / m
        r = big + small * math.cos(v)
        return (r * math.cos(u), small + small * math.sin(v), -r * math.sin(u))

    out = []
    for i in range(n):
        for j in range(m):
            quad = [point(i, j), point(i + 1, j), point(i + 1, j + 1), point(i, j + 1)]
            centre = V.mul(V.add(V.add(quad[0], quad[1]), V.add(quad[2], quad[3])), 0.25)
            u = 2 * math.pi * (i + 0.5) / n
            core = (big * math.cos(u), small, -big * math.sin(u))
            normal = V.unit(V.sub(centre, core))
            got = V.cross(V.sub(quad[1], quad[0]), V.sub(quad[2], quad[0]))
            if V.dot(got, normal) < 0:
                quad.reverse()
            out.append(Face(points=tuple(quad), normal=normal))
    return out


def describe(params: Dict[str, Any]) -> str:
    return "torus r{} t{}".format(_shapes.g(params["radius"]), _shapes.g(params["tube"]))


PRIMITIVES = (
    Primitive(name="torus", params=("radius", "tube"), normalize=normalize, extent=extent, faces=faces, describe=describe, order=ORDER,
              doc="a lying ring: radius (0.5), tube (0.15)",
              example='{"op": "scene3d", "id": "ring3d", "title": "A ring", "objects": [{"id": "t", "shape": "torus", "radius": 0.6, '
                      '"tube": 0.2, "tone": "accent", "label": "ring"}]}'),
)
