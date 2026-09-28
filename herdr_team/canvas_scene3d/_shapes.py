"""Geometry the primitive modules share: box faces, tessellated round solids and closed-form outlines.

Local frame: centred on x and z, base at y = 0. Faces are wound counter-clockwise
seen from outside. Outlines (``silhouette``) work through ``proj``, which maps a
local point to the picture (``proj.p``) and says how much a local direction faces
the camera (``proj.facing``): the projection is orthographic, so a circle is an
ellipse and every outline is exact (elliptic arcs as cubic Béziers of at most
90° each, tangent lines between them).
"""
from __future__ import annotations

import math
from typing import Any, Callable, Dict, List, Sequence, Tuple

from herdr_team.canvas_scene3d import Face
from herdr_team.canvas_scene3d import _vec as V

P2 = Tuple[float, float]


def fmt(value: float) -> str:
    """A path number: 2 decimals, trailing zeros dropped (the display list's ``fmt``)."""
    from herdr_team import canvas_display as D

    return D.fmt(value)


# --------------------------------------------------------------------------
# faces


def _quad(points: Sequence[V.Vec], normal: V.Vec) -> Face:
    pts = tuple(points)
    got = V.cross(V.sub(pts[1], pts[0]), V.sub(pts[2], pts[0]))
    if V.dot(got, normal) < 0:
        pts = tuple(reversed(pts))
    return Face(points=pts, normal=normal)


def box_faces(w: float, h: float, d: float, y0: float = 0.0) -> List[Face]:
    """The six faces of a ``w × h × d`` box centred on x and z, from ``y0`` up."""
    x0, x1, z0, z1, y1 = -w / 2.0, w / 2.0, -d / 2.0, d / 2.0, y0 + h
    return [
        _quad([(x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)], (0.0, 1.0, 0.0)),
        _quad([(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)], (0.0, -1.0, 0.0)),
        _quad([(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)], (1.0, 0.0, 0.0)),
        _quad([(x0, y0, z0), (x0, y1, z0), (x0, y1, z1), (x0, y0, z1)], (-1.0, 0.0, 0.0)),
        _quad([(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)], (0.0, 0.0, 1.0)),
        _quad([(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0)], (0.0, 0.0, -1.0)),
    ]


def _ring(radius: float, y: float, n: int) -> List[V.Vec]:
    return [(radius * math.cos(2 * math.pi * i / n), y, -radius * math.sin(2 * math.pi * i / n)) for i in range(n)]


def _poly(points: Sequence[V.Vec]) -> Face:
    """A planar polygon, its normal from its winding."""
    n = (0.0, 0.0, 0.0)
    for i, a in enumerate(points):
        b = points[(i + 1) % len(points)]
        n = V.add(n, ((a[1] - b[1]) * (a[2] + b[2]), (a[2] - b[2]) * (a[0] + b[0]), (a[0] - b[0]) * (a[1] + b[1])))
    return Face(points=tuple(points), normal=V.unit(n))


def cylinder_faces(radius: float, height: float, n: int, top_radius: float = -1.0) -> List[Face]:
    """A cylinder (or a cone when ``top_radius`` is 0) of ``n`` sides, closed at both ends."""
    n = max(3, int(n))
    top_radius = radius if top_radius < 0 else top_radius
    bottom = _ring(radius, 0.0, n)
    if top_radius <= 0:
        apex = (0.0, height, 0.0)
        faces = [_poly([bottom[i], bottom[(i + 1) % n], apex]) for i in range(n)]
    else:
        top = _ring(top_radius, height, n)
        faces = [_poly([bottom[i], bottom[(i + 1) % n], top[(i + 1) % n], top[i]]) for i in range(n)]
        faces.append(_poly(top))
    faces.append(_poly(list(reversed(bottom))))
    return faces


def sphere_faces(radius: float, n: int) -> List[Face]:
    """A UV sphere of ``n`` segments and ``n // 2`` rings, standing on y = 0."""
    n = max(4, int(n))
    rings = max(2, n // 2)
    cy = radius

    def point(ring: int, seg: int) -> V.Vec:
        phi = math.pi * ring / rings
        theta = 2 * math.pi * seg / n
        return (radius * math.sin(phi) * math.cos(theta), cy + radius * math.cos(phi), -radius * math.sin(phi) * math.sin(theta))

    faces: List[Face] = []
    top, bottom = (0.0, cy + radius, 0.0), (0.0, cy - radius, 0.0)
    for seg in range(n):
        nxt = (seg + 1) % n
        faces.append(_poly([top, point(1, seg), point(1, nxt)]))
        for ring in range(1, rings - 1):
            faces.append(_poly([point(ring, seg), point(ring + 1, seg), point(ring + 1, nxt), point(ring, nxt)]))
        faces.append(_poly([point(rings - 1, seg), bottom, point(rings - 1, nxt)]))
    return faces


# --------------------------------------------------------------------------
# outlines


def _cross2(a: P2, b: P2) -> float:
    return a[0] * b[1] - a[1] * b[0]


def _ellipse(center: P2, u: P2, v: P2, t: float) -> P2:
    return (center[0] + u[0] * math.cos(t) + v[0] * math.sin(t), center[1] + u[1] * math.cos(t) + v[1] * math.sin(t))


def _tangent(u: P2, v: P2, t: float) -> P2:
    return (-u[0] * math.sin(t) + v[0] * math.cos(t), -u[1] * math.sin(t) + v[1] * math.cos(t))


def arc(center: P2, u: P2, v: P2, t0: float, t1: float, first: bool) -> Tuple[List[str], List[P2]]:
    """The elliptic arc ``center + u cos t + v sin t`` from ``t0`` to ``t1`` as cubic Béziers of at most 90° each (``M``
    first when ``first``, else it continues the path), and points sampled on it."""
    span = t1 - t0
    count = max(1, int(math.ceil(abs(span) / (math.pi / 2.0) - 1e-9)))
    step = span / count
    k = 4.0 / 3.0 * math.tan(step / 4.0)
    parts: List[str] = []
    samples: List[P2] = []
    start = _ellipse(center, u, v, t0)
    if first:
        parts.append("M {} {}".format(fmt(start[0]), fmt(start[1])))
    for i in range(count):
        a, b = t0 + i * step, t0 + (i + 1) * step
        p0, p3 = _ellipse(center, u, v, a), _ellipse(center, u, v, b)
        d0, d3 = _tangent(u, v, a), _tangent(u, v, b)
        p1 = (p0[0] + k * d0[0], p0[1] + k * d0[1])
        p2 = (p3[0] - k * d3[0], p3[1] - k * d3[1])
        parts.append("C {} {} {} {} {} {}".format(fmt(p1[0]), fmt(p1[1]), fmt(p2[0]), fmt(p2[1]), fmt(p3[0]), fmt(p3[1])))
        for j in range(4):
            samples.append(_ellipse(center, u, v, a + (b - a) * j / 4.0))
    samples.append(_ellipse(center, u, v, t1))
    return parts, samples


def ellipse_outline(center: P2, u: P2, v: P2) -> Dict[str, Any]:
    parts, samples = arc(center, u, v, 0.0, 2 * math.pi, True)
    return {"d": " ".join(parts + ["Z"]), "points": [list(p) for p in samples]}


def _circle_axes(proj: Any, center: V.Vec, radius: float) -> Tuple[P2, P2, P2]:
    """The projected centre and the two axes of a horizontal circle (local x and z) of ``radius`` at ``center``."""
    c = proj.p(center)
    a = proj.p((center[0] + radius, center[1], center[2]))
    b = proj.p((center[0], center[1], center[2] + radius))
    return c, (a[0] - c[0], a[1] - c[1]), (b[0] - c[0], b[1] - c[1])


def sphere_outline(proj: Any, radius: float) -> List[Dict[str, Any]]:
    """A sphere projects orthographically to a circle: its radius is ``r × scale``, read from the three axes' images."""
    center = (0.0, radius, 0.0)
    c = proj.p(center)
    total = 0.0
    for axis in ((radius, 0.0, 0.0), (0.0, radius, 0.0), (0.0, 0.0, radius)):
        q = proj.p(V.add(center, axis))
        total += (q[0] - c[0]) ** 2 + (q[1] - c[1]) ** 2
    r2 = math.sqrt(total / 2.0)
    found = ellipse_outline(c, (r2, 0.0), (0.0, r2))
    found["part"] = "body"
    return [found]


def cylinder_outline(proj: Any, radius: float, height: float) -> List[Dict[str, Any]]:
    """The convex hull of the two end ellipses (two half ellipses joined by their parallel tangents), and the top cap
    when it faces the camera."""
    c0, u, v = _circle_axes(proj, (0.0, 0.0, 0.0), radius)
    c1 = proj.p((0.0, height, 0.0))
    dvec = (c1[0] - c0[0], c1[1] - c0[1])
    out: List[Dict[str, Any]] = []
    if math.hypot(*dvec) < 1e-6:
        # Seen along its axis: one ellipse (a circle from the top).
        body = ellipse_outline(c1 if proj.facing((0.0, 1.0, 0.0)) >= 0 else c0, u, v)
    else:
        t0 = math.atan2(_cross2(v, dvec), _cross2(u, dvec))
        mid = _ellipse((0.0, 0.0), u, v, t0 + math.pi / 2.0)
        sign = 1.0 if mid[0] * dvec[0] + mid[1] * dvec[1] < 0 else -1.0
        # Around the bottom ellipse on the side away from the top, then around the top ellipse on the far side.
        a0, a1 = t0, t0 + sign * math.pi
        parts, samples = arc(c0, u, v, a0, a1, True)
        end_top = _ellipse(c1, u, v, a1)
        parts.append("L {} {}".format(fmt(end_top[0]), fmt(end_top[1])))
        more, more_samples = arc(c1, u, v, a1, a1 + sign * math.pi, False)
        parts += more + ["Z"]
        body = {"d": " ".join(parts), "points": [list(p) for p in samples + more_samples]}
    body["part"] = "body"
    out.append(body)
    if proj.facing((0.0, 1.0, 0.0)) > 1e-6 and abs(_cross2(u, v)) > 1e-6:
        cap = ellipse_outline(c1, u, v)
        cap["part"] = "cap"
        out.append(cap)
    return out


def _inside(center: P2, u: P2, v: P2, point: P2, scale: float) -> bool:
    """Whether ``point`` lies inside the ellipse ``center + u cos t + v sin t``."""
    det = _cross2(u, v)
    if abs(det) < 1e-12:
        return False
    px, py = point[0] - center[0], point[1] - center[1]
    a = (px * v[1] - py * v[0]) / det
    b = (u[0] * py - u[1] * px) / det
    return a * a + b * b <= scale + 1e-9


def cone_outline(proj: Any, radius: float, height: float) -> List[Dict[str, Any]]:
    """The base ellipse and the two tangents from the apex (the ellipse alone when the apex projects inside it)."""
    c0, u, v = _circle_axes(proj, (0.0, 0.0, 0.0), radius)
    apex = proj.p((0.0, height, 0.0))
    det = _cross2(u, v)
    if abs(det) < 1e-9:
        # The base is edge-on: a triangle.
        ends = [(c0[0] + u[0], c0[1] + u[1]), (c0[0] - u[0], c0[1] - u[1])] if math.hypot(*u) >= math.hypot(*v) else \
            [(c0[0] + v[0], c0[1] + v[1]), (c0[0] - v[0], c0[1] - v[1])]
        pts = [ends[0], apex, ends[1]]
        d = "M {} {} ".format(fmt(pts[0][0]), fmt(pts[0][1])) + " ".join("L {} {}".format(fmt(p[0]), fmt(p[1])) for p in pts[1:]) + " Z"
        return [{"part": "body", "d": d, "points": [list(p) for p in pts]}]
    px, py = apex[0] - c0[0], apex[1] - c0[1]
    qa = (px * v[1] - py * v[0]) / det
    qb = (u[0] * py - u[1] * px) / det
    q = math.hypot(qa, qb)
    if q <= 1.0 + 1e-9:
        body = ellipse_outline(c0, u, v)
        body["part"] = "body"
        return [body]
    phi = math.atan2(qb, qa)
    alpha = math.acos(1.0 / q)
    t_in, t_out = phi + alpha, phi - alpha
    start = _ellipse(c0, u, v, t_in)
    parts = ["M {} {}".format(fmt(start[0]), fmt(start[1])), "L {} {}".format(fmt(apex[0]), fmt(apex[1]))]
    more, samples = arc(c0, u, v, t_out, t_in - 2 * math.pi, False)
    end = _ellipse(c0, u, v, t_out)
    parts.append("L {} {}".format(fmt(end[0]), fmt(end[1])))
    parts += more + ["Z"]
    return [{"part": "body", "d": " ".join(parts), "points": [list(p) for p in [start, apex] + samples]}]


# --------------------------------------------------------------------------
# normalizing params


def size3(ctx: Any, obj: Dict[str, Any], field: str, key: str, default: Sequence[float]) -> List[float]:
    from herdr_team import canvas_scene3d as S
    from herdr_team.canvas_scene3d import _spec

    value = obj.get(key)
    if value is None:
        return list(default)
    if _spec.is_number(value):
        value = [value] * len(default)
    if not isinstance(value, list) or len(value) != len(default) or not all(_spec.is_number(v) and S.MIN_SIZE <= float(v) <= S.MAX_SIZE
                                                                           for v in value):
        raise ctx.invalid("{}.{}".format(field, key), "{}.{} is [{}] in units, each from {:g} to {:g}".format(
            field, key, ", ".join(("w", "h", "d")[:len(default)] if len(default) == 3 else ("w", "d")), S.MIN_SIZE, S.MAX_SIZE))
    return list(value)


def scalar(ctx: Any, obj: Dict[str, Any], field: str, key: str, default: float, low: float = 0.001, high: float = 10_000.0) -> float:
    from herdr_team.canvas_scene3d import _spec

    value = obj.get(key)
    if value is None:
        return default
    return _spec.number(ctx, value, "{}.{}".format(field, key), low, high)


def g(value: Any) -> str:
    """A size as readback prints it: ``1.2``, ``8``, ``0.35``."""
    number = float(value)
    text = "{:.2f}".format(number).rstrip("0").rstrip(".")
    return text if text not in ("-0", "") else "0"


Proj = Any
Silhouette = Callable[[Dict[str, Any], Proj], List[Dict[str, Any]]]
