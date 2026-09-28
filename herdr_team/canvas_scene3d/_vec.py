"""Vector and box arithmetic for scene3d (canvas v2 phase 4): tuples, no classes, deterministic rounding.

Axes follow three.js: y is up, x is right and z is toward the viewer. An
axis-aligned box (``Box``) is ``(x0, y0, z0, x1, y1, z1)``. Rotations are in
degrees and compose as three.js's Euler order ``XYZ`` (the matrix is
``Rx · Ry · Rz``, so a vector turns about z first), which is what the page's
``Object3D.rotation`` does with the same three numbers.

Pure Python, stdlib only.
"""
from __future__ import annotations

import math
from typing import Iterable, List, Sequence, Tuple

Vec = Tuple[float, float, float]
Box = Tuple[float, float, float, float, float, float]
Mat = Tuple[Vec, Vec, Vec]

#: Every solved number is snapped to this step, so 3.9 and 3.14 write the same scene (``snap``).
SNAP = 1e-4
EPS = 1e-9
IDENTITY: Mat = ((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), (0.0, 0.0, 1.0))


def snap(value: float) -> float:
    """``value`` rounded to ``SNAP`` half away from zero, never ``-0``."""
    if value != value or value in (float("inf"), float("-inf")):
        return 0.0
    n = math.floor(abs(value) / SNAP + 0.5)
    out = n * SNAP
    out = float("{:.4f}".format(out))
    return -out if value < 0 and out else out


def snapped(values: Iterable[float]) -> List[float]:
    return [snap(v) for v in values]


def add(a: Sequence[float], b: Sequence[float]) -> Vec:
    return (a[0] + b[0], a[1] + b[1], a[2] + b[2])


def sub(a: Sequence[float], b: Sequence[float]) -> Vec:
    return (a[0] - b[0], a[1] - b[1], a[2] - b[2])


def mul(a: Sequence[float], k: float) -> Vec:
    return (a[0] * k, a[1] * k, a[2] * k)


def dot(a: Sequence[float], b: Sequence[float]) -> float:
    return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]


def cross(a: Sequence[float], b: Sequence[float]) -> Vec:
    return (a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0])


def length(a: Sequence[float]) -> float:
    return math.sqrt(dot(a, a))


def unit(a: Sequence[float]) -> Vec:
    n = length(a)
    return (0.0, 0.0, 0.0) if n < EPS else (a[0] / n, a[1] / n, a[2] / n)


# --------------------------------------------------------------------------
# rotation


def rotation(degrees: Sequence[float]) -> Mat:
    """The rotation matrix of Euler angles ``[rx, ry, rz]`` in degrees, order XYZ (``Rx · Ry · Rz``)."""
    rx, ry, rz = (math.radians(float(v)) for v in degrees)
    cx, sx, cy, sy, cz, sz = math.cos(rx), math.sin(rx), math.cos(ry), math.sin(ry), math.cos(rz), math.sin(rz)
    # three.js Matrix4.makeRotationFromEuler, order 'XYZ'.
    return ((cy * cz, -cy * sz, sy),
            (cx * sz + sx * sy * cz, cx * cz - sx * sy * sz, -sx * cy),
            (sx * sz - cx * sy * cz, sx * cz + cx * sy * sz, cx * cy))


def apply(m: Mat, v: Sequence[float]) -> Vec:
    return (m[0][0] * v[0] + m[0][1] * v[1] + m[0][2] * v[2],
            m[1][0] * v[0] + m[1][1] * v[1] + m[1][2] * v[2],
            m[2][0] * v[0] + m[2][1] * v[1] + m[2][2] * v[2])


def is_identity(degrees: Sequence[float]) -> bool:
    return all(abs(float(v)) < EPS for v in degrees)


def rotated_extent(ext: Sequence[float], degrees: Sequence[float]) -> Vec:
    """The size of the axis-aligned box around a ``w × h × d`` box turned by ``degrees`` (conservative for round shapes)."""
    if is_identity(degrees):
        return (float(ext[0]), float(ext[1]), float(ext[2]))
    m = rotation(degrees)
    return tuple(sum(abs(m[row][col]) * float(ext[col]) for col in range(3)) for row in range(3))  # type: ignore[return-value]


# --------------------------------------------------------------------------
# boxes


def box_at(pos: Sequence[float], ext: Sequence[float]) -> Box:
    """The box of an object whose footprint centre is ``(pos.x, pos.z)``, base ``pos.y`` and size ``ext``."""
    x, y, z = pos
    w, h, d = ext
    return (x - w / 2.0, y, z - d / 2.0, x + w / 2.0, y + h, z + d / 2.0)


def box_size(box: Sequence[float]) -> Vec:
    return (box[3] - box[0], box[4] - box[1], box[5] - box[2])


def box_center(box: Sequence[float]) -> Vec:
    return ((box[0] + box[3]) / 2.0, (box[1] + box[4]) / 2.0, (box[2] + box[5]) / 2.0)


def union(boxes: Iterable[Sequence[float]]) -> Box:
    found = list(boxes)
    if not found:
        return (0.0, 0.0, 0.0, 0.0, 0.0, 0.0)
    return (min(b[0] for b in found), min(b[1] for b in found), min(b[2] for b in found),
            max(b[3] for b in found), max(b[4] for b in found), max(b[5] for b in found))


def overlap(a: Sequence[float], b: Sequence[float]) -> Vec:
    """How far two boxes reach into each other along each axis (0 or less on an axis where they are apart)."""
    return (min(a[3], b[3]) - max(a[0], b[0]), min(a[4], b[4]) - max(a[1], b[1]), min(a[5], b[5]) - max(a[2], b[2]))


def volume(box: Sequence[float]) -> float:
    w, h, d = box_size(box)
    return max(0.0, w) * max(0.0, h) * max(0.0, d)


def overlap_volume(a: Sequence[float], b: Sequence[float]) -> float:
    ox, oy, oz = overlap(a, b)
    return ox * oy * oz if ox > 0 and oy > 0 and oz > 0 else 0.0


def intersects(a: Sequence[float], b: Sequence[float], tol: float = 1e-6) -> bool:
    """Whether the boxes share volume (touching faces do not count)."""
    ox, oy, oz = overlap(a, b)
    return ox > tol and oy > tol and oz > tol


def corners(box: Sequence[float]) -> List[Vec]:
    x0, y0, z0, x1, y1, z1 = box
    return [(x, y, z) for x in (x0, x1) for y in (y0, y1) for z in (z0, z1)]


def exit_point(box: Sequence[float], inside: Sequence[float], toward: Sequence[float]) -> Vec:
    """Where the segment from ``inside`` (a point in ``box``) to ``toward`` leaves ``box`` (``toward`` when it never does)."""
    d = sub(toward, inside)
    t_exit = 1.0
    for axis in range(3):
        if abs(d[axis]) < EPS:
            continue
        lo, hi = box[axis], box[axis + 3]
        t = ((hi if d[axis] > 0 else lo) - inside[axis]) / d[axis]
        if 0.0 <= t < t_exit:
            t_exit = t
    return add(inside, mul(d, t_exit))


def footprint_radius(ext: Sequence[float]) -> float:
    """Half the diagonal of a footprint: the radius of the circle around it."""
    return math.hypot(float(ext[0]), float(ext[2])) / 2.0
