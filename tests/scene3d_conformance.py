"""What every scene3d primitive promises (canvas v2 phase 4, 8.2), checked the same way for a built-in and a new one.

``check_primitive(case, prim)`` runs the whole contract on one registered
``Primitive``:

- ``normalize`` fills its defaults, is deterministic, and refuses a junk value
  of each of its params with a refusal naming the field (``objects[3].radius``);
- ``extent`` is positive and finite;
- ``faces`` are closed (their area vectors sum to zero) and face outward;
- ``silhouette`` (when it has one) lies inside the projected box of its extent,
  in every still view;
- ``describe`` never raises, on its defaults or on its example's object;
- its ``example`` is one valid ``scene3d`` op naming it.
"""
from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Sequence

from herdr_team import canvas_scene3d as S
from herdr_team.canvas_scene3d import _project, _spec
from herdr_team.canvas_scene3d import _vec as V
from herdr_team.errors import HerdrTeamError

FIELD = "objects[3]"
JUNK = {"not": "a value"}
VIEWS = ("iso", "front", "top")
#: What a primitive needs to be an object at all (a text3d its text, an arrow its ends, a model its file).
REQUIRED = {"text3d": {"text": "hello"}, "arrow3d": {"from": [0, 0, 0], "to": [1, 0, 0]}, "gltf": {"src": "models/arm-v3.glb"}}


def base_object(prim: S.Primitive) -> Dict[str, Any]:
    return dict({"id": "o", "shape": prim.name}, **REQUIRED.get(prim.name, {}))


def example_object(prim: S.Primitive) -> Dict[str, Any]:
    """The first object of the primitive's example op that has its shape."""
    op = json.loads(prim.example)
    return next(o for o in op["objects"] if o.get("shape") == prim.name)


def area_vector(points: Sequence[Sequence[float]]) -> V.Vec:
    total = (0.0, 0.0, 0.0)
    for i in range(1, len(points) - 1):
        total = V.add(total, V.mul(V.cross(V.sub(points[i], points[0]), V.sub(points[i + 1], points[0])), 0.5))
    return total


def check_normalize(case: Any, prim: S.Primitive) -> Dict[str, Any]:
    raw = base_object(prim)
    first = prim.normalize(_spec.PURE, dict(raw), FIELD)
    case.assertEqual(first, prim.normalize(_spec.PURE, dict(raw), FIELD), "{}: normalize is deterministic".format(prim.name))
    for param in prim.params:
        bad = dict(raw, **{param: JUNK})
        with case.assertRaises(HerdrTeamError, msg="{}.{} = junk".format(prim.name, param)) as caught:
            _spec.object_item(_spec.PURE, bad, FIELD)
        field = str((caught.exception.details or {}).get("field") or "")
        case.assertTrue(field.startswith(FIELD), "{}.{}: the refusal names {} ({})".format(prim.name, param, FIELD, field))
        case.assertIn(param, field + " " + str(caught.exception), "{}.{}: the refusal names the param".format(prim.name, param))
    return _spec.resolved(raw)


def check_extent(case: Any, prim: S.Primitive, params: Dict[str, Any]) -> V.Vec:
    ext = prim.extent(params)
    case.assertEqual(len(ext), 3, prim.name)
    for value in ext:
        case.assertTrue(math.isfinite(float(value)) and float(value) > 0, "{}: extent {} is positive and finite".format(prim.name, ext))
    case.assertEqual(tuple(ext), tuple(prim.extent(params)), "{}: extent is deterministic".format(prim.name))
    return tuple(float(v) for v in ext)  # type: ignore[return-value]


def check_faces(case: Any, prim: S.Primitive, params: Dict[str, Any], ext: V.Vec) -> None:
    """Closed (the area vectors sum to 0), each face wound to its normal, and outward as a whole: the enclosed volume by
    the divergence theorem is positive and no larger than the extent's box (a torus is not convex, so no per-face test
    against the centre)."""
    faces = prim.faces(params, S.DETAIL)
    if not faces:
        return
    total = (0.0, 0.0, 0.0)
    volume = 0.0
    for index, face in enumerate(faces):
        case.assertGreaterEqual(len(face.points), 3, "{} face {}".format(prim.name, index))
        area = area_vector(face.points)
        total = V.add(total, area)
        case.assertGreater(V.dot(area, face.normal), -1e-9, "{} face {}: wound counter-clockwise from outside".format(prim.name, index))
        middle = tuple(sum(p[k] for p in face.points) / len(face.points) for k in range(3))
        volume += V.dot(middle, area) / 3.0
        for point in face.points:
            for axis, (lo, hi) in enumerate(((-ext[0] / 2, ext[0] / 2), (0.0, ext[1]), (-ext[2] / 2, ext[2] / 2))):
                case.assertTrue(lo - 1e-6 <= point[axis] <= hi + 1e-6, "{} face {}: inside its extent".format(prim.name, index))
    case.assertLess(V.length(total), 1e-6 * max(1.0, max(ext) ** 2), "{}: its faces close (area vectors sum to 0)".format(prim.name))
    case.assertGreater(volume, 0.0, "{}: its faces face out (positive enclosed volume)".format(prim.name))
    case.assertLessEqual(volume, ext[0] * ext[1] * ext[2] * (1 + 1e-6), "{}: the volume fits its extent".format(prim.name))


def check_silhouette(case: Any, prim: S.Primitive, params: Dict[str, Any], ext: V.Vec) -> None:
    if prim.silhouette is None:
        return
    box = (-ext[0] / 2.0, 0.0, -ext[2] / 2.0, ext[0] / 2.0, ext[1], ext[2] / 2.0)
    for name in VIEWS:
        cam = _project._tokens.camera(name)
        view = _project.View(float(cam["az"]), float(cam["el"]))
        view.fit(V.corners(box), (0.0, 0.0, 400.0, 300.0))
        proj = _project.ObjectProj(view, (0.0, 0.0, 0.0), (0.0, 0.0, 0.0), ext[1], ext[1])
        corners = [proj.p(c) for c in V.corners(box)]
        x0, x1 = min(c[0] for c in corners), max(c[0] for c in corners)
        y0, y1 = min(c[1] for c in corners), max(c[1] for c in corners)
        outlines = prim.silhouette(params, proj)
        case.assertTrue(outlines, "{} {}: an outline".format(prim.name, name))
        for outline in outlines:
            case.assertIn(outline.get("part"), ("body", "cap"), prim.name)
            case.assertTrue(str(outline.get("d", "")).startswith("M "), prim.name)
            for x, y in outline.get("points") or []:
                case.assertTrue(x0 - 0.05 <= x <= x1 + 0.05 and y0 - 0.05 <= y <= y1 + 0.05,
                                "{} {}: outline point ({}, {}) inside the projected box {}".format(prim.name, name, x, y, (x0, y0, x1, y1)))


def check_describe(case: Any, prim: S.Primitive, params: Dict[str, Any]) -> None:
    text = prim.describe(params)
    case.assertIsInstance(text, str)
    case.assertTrue(text.startswith(prim.name), "{}: describe starts with its name ({})".format(prim.name, text))
    example = _spec.resolved(example_object(prim))
    case.assertTrue(prim.describe(example), prim.name)


def check_example(case: Any, prim: S.Primitive) -> None:
    op = json.loads(prim.example)
    case.assertEqual(op.get("op"), "scene3d", prim.name)
    case.assertTrue(any(o.get("shape") == prim.name for o in op.get("objects") or []), "{}: its example uses it".format(prim.name))
    for index, raw in enumerate(op["objects"]):
        _spec.object_item(_spec.PURE, raw, "objects[{}]".format(index))


def check_primitive(case: Any, prim: S.Primitive) -> None:
    case.assertTrue(prim.doc, "{}: a doc line".format(prim.name))
    params = check_normalize(case, prim)
    ext = check_extent(case, prim, params)
    check_faces(case, prim, params, ext)
    check_silhouette(case, prim, params, ext)
    check_describe(case, prim, params)
    check_example(case, prim)


def examples() -> List[Dict[str, Any]]:
    """Every registered primitive's example op."""
    return [json.loads(prim.example) for prim in S.primitives() if prim.example]
