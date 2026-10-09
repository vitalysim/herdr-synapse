"""Canvas geometry every renderer shares (canvas v2 phase 1): routes, label pills, bounds, the view box, path data.

Moved unchanged out of ``canvas_render`` so the display list
(``canvas_display``) and both SVG writers compute one geometry, and
``canvas_render`` re-exports every old name. Pure: no I/O, and no import of
``canvas`` (which imports this module through ``canvas_render``).

* Arrows: ``arrow_route`` (a curve sampled into a polyline), ``curve_pieces``,
  ``arrow_midpoint``, ``head_points`` (the head geometry the display list
  carries, so renderers never compute it) and the label pill
  (``arrow_label_text``, ``arrow_label_center``, ``arrow_label_pill``).
* Frames: ``frame_title`` and ``frame_title_box`` follow the one zoom rule of
  phase 1 (``.local/prd/canvas-v2-phase1.md`` 1.6): at a scale of at least
  ``FRAME_TITLE_LOD`` screen pixels per unit the title sits in the frame's
  band at 16, below it the full title stands above the frame at no less than
  12 screen pixels.
* ``drawn_bounds``, ``view_box`` (with its settling loop) and ``pixel_size``.
* Pen strokes: ``freehand_outline`` (a port of perfect-freehand).
* Path data: ``parse_path``, ``absolute_path``, ``normalize_path``,
  ``sanitize_path_data`` and ``path_mlcqz`` (absolute ``M L C Q Z`` only, arcs
  flattened: the path grammar of the display list).
"""
from __future__ import annotations

import math
import re
from typing import Any, Dict, List, Optional, Sequence, Tuple

from herdr_team import canvas_text as _ctext
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError

Box = Tuple[float, float, float, float]
Point = Tuple[float, float]

DEFAULT_MAX_PX = 1024
#: Room around everything a whole-board picture draws.
PADDING = 40
#: Excalidraw's freedraw: an outline this many times the stroke width across at full pressure.
FREEHAND_SIZE = 4.25
FREEHAND_THINNING = 0.6
FREEHAND_STREAMLINE = 0.5
CAP_STEPS = 8


def _path_invalid(message: str) -> HerdrTeamError:
    return HerdrTeamError("op_invalid", "path data: {}".format(message), EXIT_REFUSED, {"field": "d"})


def bounds(el: Dict[str, Any]) -> Box:
    """``(x0, y0, x1, y1)`` of an element's box (at least one unit wide and tall)."""
    x, y = float(el.get("x") or 0), float(el.get("y") or 0)
    return x, y, x + max(1.0, float(el.get("w") or 1)), y + max(1.0, float(el.get("h") or 1))


_bounds = bounds


def _intersects(a: Sequence[float], b: Sequence[float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _points(raw: Any) -> List[Point]:
    out = []
    for point in raw or []:
        try:
            out.append((float(point[0]), float(point[1])))
        except (TypeError, ValueError, IndexError):
            continue
    return out


# --------------------------------------------------------------------------
# path data

_PATH_CHARS = re.compile(r"^[MmLlHhVvCcSsQqTtAaZz0-9eE.,+\-\s]*\Z")
_NUM_RE = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_PATH_ARGS = {"M": 2, "L": 2, "H": 1, "V": 1, "C": 6, "S": 4, "Q": 4, "T": 2, "A": 7, "Z": 0}


def _skip_separators(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t\r\n,":
        pos += 1
    return pos


def parse_path(d: str) -> List[Tuple[str, List[float]]]:
    """``[(command, args)]`` of SVG path data; ``op_invalid`` on anything but commands and numbers."""
    if not isinstance(d, str) or not d.strip():
        raise _path_invalid("empty")
    if not _PATH_CHARS.match(d):
        raise _path_invalid("only path commands (MLHVCSQTAZ) and numbers are allowed")
    segments: List[Tuple[str, List[float]]] = []
    command: Optional[str] = None
    pos = 0
    size = len(d)
    while True:
        pos = _skip_separators(d, pos)
        if pos >= size:
            break
        ch = d[pos]
        if ch.isalpha():
            if ch in "eE":
                raise _path_invalid("unexpected exponent")
            command = ch
            pos += 1
            if command in "Zz":
                segments.append((command, []))
                command = None
                continue
        elif command is None:
            raise _path_invalid("numbers before a command")
        count = _PATH_ARGS[command.upper()]
        args: List[float] = []
        for index in range(count):
            pos = _skip_separators(d, pos)
            if command in "Aa" and index in (3, 4):
                if pos < size and d[pos] in "01":
                    args.append(float(d[pos]))
                    pos += 1
                    continue
                raise _path_invalid("arc flags must be 0 or 1")
            match = _NUM_RE.match(d, pos)
            if match is None:
                raise _path_invalid("{} needs {} numbers".format(command, count))
            value = float(match.group(0))
            if not math.isfinite(value):
                raise _path_invalid("a number is not finite")
            args.append(value)
            pos = match.end()
        segments.append((command, args))
        if command == "M":
            command = "L"
        elif command == "m":
            command = "l"
    if not segments or segments[0][0] not in "Mm":
        raise _path_invalid("must start with M")
    return segments


def _fmt(value: float) -> str:
    text = "{:.3f}".format(value).rstrip("0").rstrip(".")
    return "0" if text in ("-0", "") else text


def sanitize_path_data(d: str) -> str:
    """SVG path data with only path commands and numbers; ``op_invalid`` otherwise."""
    return " ".join(cmd + " ".join(_fmt(a) for a in args) for cmd, args in parse_path(d))


def _vector_angle(ux: float, uy: float, vx: float, vy: float) -> float:
    return math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)


def arc_points(x1: float, y1: float, rx: float, ry: float, rotation: float, large: float, sweep: float, x2: float, y2: float,
               steps: int = 16) -> List[Tuple[float, float]]:
    """Points along an SVG elliptical arc (endpoint parameterisation, SVG 1.1 F.6.5)."""
    if rx == 0 or ry == 0 or (x1 == x2 and y1 == y2):
        return [(x2, y2)]
    phi = math.radians(rotation)
    cos_phi, sin_phi = math.cos(phi), math.sin(phi)
    dx2, dy2 = (x1 - x2) / 2.0, (y1 - y2) / 2.0
    x1p = cos_phi * dx2 + sin_phi * dy2
    y1p = -sin_phi * dx2 + cos_phi * dy2
    rx, ry = abs(rx), abs(ry)
    lam = (x1p * x1p) / (rx * rx) + (y1p * y1p) / (ry * ry)
    if lam > 1:
        rx *= math.sqrt(lam)
        ry *= math.sqrt(lam)
    numerator = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    denominator = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    coef = math.sqrt(max(0.0, numerator / denominator)) if denominator else 0.0
    if bool(large) == bool(sweep):
        coef = -coef
    cxp = coef * rx * y1p / ry
    cyp = -coef * ry * x1p / rx
    cx = cos_phi * cxp - sin_phi * cyp + (x1 + x2) / 2.0
    cy = sin_phi * cxp + cos_phi * cyp + (y1 + y2) / 2.0
    ux, uy = (x1p - cxp) / rx, (y1p - cyp) / ry
    vx, vy = (-x1p - cxp) / rx, (-y1p - cyp) / ry
    theta = _vector_angle(1.0, 0.0, ux, uy)
    delta = _vector_angle(ux, uy, vx, vy)
    if not sweep and delta > 0:
        delta -= 2 * math.pi
    elif sweep and delta < 0:
        delta += 2 * math.pi
    out = []
    for k in range(1, steps + 1):
        t = theta + delta * k / steps
        out.append((cx + rx * math.cos(t) * cos_phi - ry * math.sin(t) * sin_phi,
                    cy + rx * math.cos(t) * sin_phi + ry * math.sin(t) * cos_phi))
    return out


def absolute_path(segments: List[Tuple[str, List[float]]]) -> Tuple[List[Tuple[str, List[float]]], List[Tuple[float, float]]]:
    """The segments with absolute coordinates, and every point that bounds the drawing (ends and control points)."""
    out: List[Tuple[str, List[float]]] = []
    points: List[Tuple[float, float]] = []
    cx = cy = sx = sy = 0.0
    last_cubic: Optional[Tuple[float, float]] = None
    last_quad: Optional[Tuple[float, float]] = None
    for command, args in segments:
        rel = command.islower()
        upper = command.upper()
        ox, oy = (cx, cy) if rel else (0.0, 0.0)
        cubic: Optional[Tuple[float, float]] = None
        quad: Optional[Tuple[float, float]] = None
        if upper == "M":
            cx, cy = args[0] + ox, args[1] + oy
            sx, sy = cx, cy
            out.append(("M", [cx, cy]))
            points.append((cx, cy))
        elif upper == "L":
            cx, cy = args[0] + ox, args[1] + oy
            out.append(("L", [cx, cy]))
            points.append((cx, cy))
        elif upper == "H":
            cx = args[0] + (cx if rel else 0.0)
            out.append(("H", [cx]))
            points.append((cx, cy))
        elif upper == "V":
            cy = args[0] + (cy if rel else 0.0)
            out.append(("V", [cy]))
            points.append((cx, cy))
        elif upper == "C":
            c1 = (args[0] + ox, args[1] + oy)
            c2 = (args[2] + ox, args[3] + oy)
            cx, cy = args[4] + ox, args[5] + oy
            out.append(("C", [c1[0], c1[1], c2[0], c2[1], cx, cy]))
            points += [c1, c2, (cx, cy)]
            cubic = c2
        elif upper == "S":
            c1 = (2 * cx - last_cubic[0], 2 * cy - last_cubic[1]) if last_cubic else (cx, cy)
            c2 = (args[0] + ox, args[1] + oy)
            cx, cy = args[2] + ox, args[3] + oy
            out.append(("S", [c2[0], c2[1], cx, cy]))
            points += [c1, c2, (cx, cy)]
            cubic = c2
        elif upper == "Q":
            c1 = (args[0] + ox, args[1] + oy)
            cx, cy = args[2] + ox, args[3] + oy
            out.append(("Q", [c1[0], c1[1], cx, cy]))
            points += [c1, (cx, cy)]
            quad = c1
        elif upper == "T":
            c1 = (2 * cx - last_quad[0], 2 * cy - last_quad[1]) if last_quad else (cx, cy)
            cx, cy = args[0] + ox, args[1] + oy
            out.append(("T", [cx, cy]))
            points += [c1, (cx, cy)]
            quad = c1
        elif upper == "A":
            x2, y2 = args[5] + ox, args[6] + oy
            points += arc_points(cx, cy, args[0], args[1], args[2], args[3], args[4], x2, y2)
            cx, cy = x2, y2
            out.append(("A", [args[0], args[1], args[2], args[3], args[4], cx, cy]))
        else:  # Z
            cx, cy = sx, sy
            out.append(("Z", []))
        last_cubic = cubic
        last_quad = quad
    return out, points


def normalize_path(d: str) -> Tuple[str, float, float]:
    """``(d, w, h)``: absolute path data moved so its bounds start at (0, 0), and its natural size."""
    segments, points = absolute_path(parse_path(d))
    min_x = min(p[0] for p in points)
    min_y = min(p[1] for p in points)
    max_x = max(p[0] for p in points)
    max_y = max(p[1] for p in points)
    parts = []
    for command, args in segments:
        if command == "H":
            moved = [args[0] - min_x]
        elif command == "V":
            moved = [args[0] - min_y]
        elif command == "A":
            moved = args[:5] + [args[5] - min_x, args[6] - min_y]
        else:
            moved = [value - (min_x if i % 2 == 0 else min_y) for i, value in enumerate(args)]
        parts.append(command + " ".join(_fmt(v) for v in moved))
    return " ".join(parts), max(1.0, max_x - min_x), max(1.0, max_y - min_y)




def path_mlcqz(d: str) -> List[Tuple[str, List[float]]]:
    """Path data as absolute ``M L C Q Z`` segments: ``H``/``V`` become lines, ``S``/``T`` their full curves
    (the reflected control point written out) and arcs are flattened into lines (``arc_points``). This is the
    display list's path grammar (phase 1, 1.3), so a renderer draws a path with no path algebra of its own."""
    out: List[Tuple[str, List[float]]] = []
    cx = cy = sx = sy = 0.0
    last_cubic: Optional[Point] = None
    last_quad: Optional[Point] = None
    for command, args in parse_path(d):
        rel = command.islower()
        upper = command.upper()
        ox, oy = (cx, cy) if rel else (0.0, 0.0)
        cubic: Optional[Point] = None
        quad: Optional[Point] = None
        if upper == "M":
            cx, cy = args[0] + ox, args[1] + oy
            sx, sy = cx, cy
            out.append(("M", [cx, cy]))
        elif upper in ("L", "H", "V"):
            if upper == "L":
                cx, cy = args[0] + ox, args[1] + oy
            elif upper == "H":
                cx = args[0] + (cx if rel else 0.0)
            else:
                cy = args[0] + (cy if rel else 0.0)
            out.append(("L", [cx, cy]))
        elif upper in ("C", "S"):
            if upper == "C":
                c1 = (args[0] + ox, args[1] + oy)
                c2 = (args[2] + ox, args[3] + oy)
                end = (args[4] + ox, args[5] + oy)
            else:
                c1 = (2 * cx - last_cubic[0], 2 * cy - last_cubic[1]) if last_cubic else (cx, cy)
                c2 = (args[0] + ox, args[1] + oy)
                end = (args[2] + ox, args[3] + oy)
            out.append(("C", [c1[0], c1[1], c2[0], c2[1], end[0], end[1]]))
            cx, cy = end
            cubic = c2
        elif upper in ("Q", "T"):
            if upper == "Q":
                c1 = (args[0] + ox, args[1] + oy)
                end = (args[2] + ox, args[3] + oy)
            else:
                c1 = (2 * cx - last_quad[0], 2 * cy - last_quad[1]) if last_quad else (cx, cy)
                end = (args[0] + ox, args[1] + oy)
            out.append(("Q", [c1[0], c1[1], end[0], end[1]]))
            cx, cy = end
            quad = c1
        elif upper == "A":
            x2, y2 = args[5] + ox, args[6] + oy
            for px, py in arc_points(cx, cy, args[0], args[1], args[2], args[3], args[4], x2, y2):
                out.append(("L", [px, py]))
            cx, cy = x2, y2
        else:  # Z
            cx, cy = sx, sy
            out.append(("Z", []))
        last_cubic = cubic
        last_quad = quad
    return out


# --------------------------------------------------------------------------
# freehand strokes


def _densify(pts: List[Tuple[float, float, Optional[float]]], step: float) -> List[Tuple[float, float, Optional[float]]]:
    """A Catmull-Rom spline through sparse points (agents draw with a few grid cells), about ``step`` units apart."""
    if len(pts) < 3:
        return pts
    out = [pts[0]]
    for index in range(len(pts) - 1):
        p0 = pts[max(0, index - 1)]
        p1, p2 = pts[index], pts[index + 1]
        p3 = pts[min(len(pts) - 1, index + 2)]
        length = math.hypot(p2[0] - p1[0], p2[1] - p1[1])
        count = min(24, max(1, int(math.ceil(length / max(step, 1.0)))))
        for k in range(1, count + 1):
            t = k / float(count)
            t2, t3 = t * t, t * t * t
            x = 0.5 * (2 * p1[0] + (-p0[0] + p2[0]) * t + (2 * p0[0] - 5 * p1[0] + 4 * p2[0] - p3[0]) * t2 + (-p0[0] + 3 * p1[0] - 3 * p2[0] + p3[0]) * t3)
            y = 0.5 * (2 * p1[1] + (-p0[1] + p2[1]) * t + (2 * p0[1] - 5 * p1[1] + 4 * p2[1] - p3[1]) * t2 + (-p0[1] + 3 * p1[1] - 3 * p2[1] + p3[1]) * t3)
            pressure = p1[2] if p1[2] is None or p2[2] is None else p1[2] + (p2[2] - p1[2]) * t
            out.append((x, y, pressure))
    return out


def freehand_outline(points: Sequence[Sequence[float]], width: float, smooth: bool = True) -> List[Tuple[float, float]]:
    """The filled outline polygon of a pen stroke (a Python port of perfect-freehand).

    Streamlined input points, a radius per point from pressure (real or
    simulated from speed, as the engine does for a mouse), offsets on both
    sides of the stroke direction, and round caps. ``smooth=False`` keeps the
    points as given and a constant width.
    """
    pts = []
    for raw in points:
        try:
            x, y = float(raw[0]), float(raw[1])
            pressure = float(raw[2]) if len(raw) > 2 and raw[2] is not None else None
        except (TypeError, ValueError, IndexError):
            continue
        if pts and pts[-1][0] == x and pts[-1][1] == y:
            continue
        pts.append((x, y, pressure))
    if not pts:
        return []
    size = max(1.0, float(width) * FREEHAND_SIZE)
    if len(pts) == 1:
        x, y, _p = pts[0]
        r = size / 2.0
        return [(round(x + r * math.cos(a), 2), round(y + r * math.sin(a), 2))
                for a in (2 * math.pi * k / 16 for k in range(16))]
    streamline = FREEHAND_STREAMLINE if smooth else 0.0
    thinning = FREEHAND_THINNING if smooth else 0.0
    if smooth:
        pts = _densify(pts, size)
    line = [pts[0]]
    for x, y, p in pts[1:]:
        px, py, _pp = line[-1]
        t = 1.0 - streamline
        nx, ny = px + (x - px) * t, py + (y - py) * t
        if (nx, ny) != (px, py):
            line.append((nx, ny, p))
    if smooth and (line[-1][0], line[-1][1]) != (pts[-1][0], pts[-1][1]):
        line.append(pts[-1])
    if len(line) < 2:
        line = [pts[0], pts[-1]]
    simulate = all(p is None for _x, _y, p in pts)
    radii = []
    previous = 0.5
    for index, (x, y, p) in enumerate(line):
        if simulate:
            dist = math.hypot(x - line[index - 1][0], y - line[index - 1][1]) if index else 0.0
            speed = min(1.0, dist / size)
            target = min(1.0, 1.0 - speed)
            pressure = min(1.0, previous + (target - previous) * (speed * 0.275))
        else:
            pressure = p if p is not None else 0.5
        pressure = max(0.0, min(1.0, pressure))
        previous = pressure
        radii.append(max(0.25, size / 2.0 * (1.0 - thinning * (1.0 - pressure))))
    left: List[Tuple[float, float]] = []
    right: List[Tuple[float, float]] = []
    normals: List[Tuple[float, float]] = []
    count = len(line)
    for index, (x, y, _p) in enumerate(line):
        ax, ay, _a = line[max(0, index - 1)]
        bx, by, _b = line[min(count - 1, index + 1)]
        vx, vy = bx - ax, by - ay
        length = math.hypot(vx, vy) or 1.0
        nx, ny = -vy / length, vx / length
        normals.append((nx, ny))
        r = radii[index]
        left.append((x + nx * r, y + ny * r))
        right.append((x - nx * r, y - ny * r))

    def cap(cx: float, cy: float, r: float, start_angle: float) -> List[Tuple[float, float]]:
        return [(cx + r * math.cos(start_angle - math.pi * k / CAP_STEPS), cy + r * math.sin(start_angle - math.pi * k / CAP_STEPS))
                for k in range(1, CAP_STEPS)]

    end_x, end_y, _e = line[-1]
    start_x, start_y, _s = line[0]
    end_cap = cap(end_x, end_y, radii[-1], math.atan2(normals[-1][1], normals[-1][0]))
    start_cap = cap(start_x, start_y, radii[0], math.atan2(normals[0][1], normals[0][0]) + math.pi)
    polygon = left + end_cap + list(reversed(right)) + start_cap
    return [(round(x, 2), round(y, 2)) for x, y in polygon]


# --------------------------------------------------------------------------
# arrows


#: A two-point curve bows to the right of its direction by this fraction of its length at the control point
#: (half that at the curve's middle); the page bends its arrow through the same middle.
CURVE_BOW = 0.2


def curve_pieces(points: List[Point]) -> List[Tuple[Point, Point, Point]]:
    """A curved arrow as quadratic pieces ``(start, control, end)``: one bowed piece for two points, else a
    smooth curve through the midpoints between the given points."""
    if len(points) == 2:
        (ax, ay), (bx, by) = points
        mx, my = (ax + bx) / 2, (ay + by) / 2
        dx, dy = bx - ax, by - ay
        return [((ax, ay), (mx - dy * CURVE_BOW, my + dx * CURVE_BOW), (bx, by))]
    pieces = []
    start = points[0]
    for index in range(1, len(points) - 1):
        (cx, cy), (nx, ny) = points[index], points[index + 1]
        end = (nx, ny) if index == len(points) - 2 else ((cx + nx) / 2, (cy + ny) / 2)
        pieces.append((start, (cx, cy), end))
        start = end
    return pieces


_curve_pieces = curve_pieces


def rounded_path(points: Sequence[Sequence[float]], r: float) -> List[Tuple[str, List[float]]]:
    """An orthogonal route as path pieces with rounded elbows (canvas v2 phase 2, 3.3): ``M``, then per bend an ``L``
    to where the curve starts and a ``Q`` through the corner, then an ``L`` to the end. A corner's radius is ``r`` or
    half of either neighbouring piece, whichever is smaller, so short jogs stay square."""
    pts = [(float(p[0]), float(p[1])) for p in points]
    if len(pts) < 2:
        return [("M", [pts[0][0], pts[0][1]])] if pts else []
    out: List[Tuple[str, List[float]]] = [("M", [pts[0][0], pts[0][1]])]
    for index in range(1, len(pts) - 1):
        (ax, ay), (bx, by), (cx, cy) = pts[index - 1], pts[index], pts[index + 1]
        before, after = math.hypot(bx - ax, by - ay), math.hypot(cx - bx, cy - by)
        radius = min(float(r), before / 2.0, after / 2.0)
        if radius <= 0 or before == 0 or after == 0:
            out.append(("L", [bx, by]))
            continue
        sx, sy = bx - (bx - ax) / before * radius, by - (by - ay) / before * radius
        ex, ey = bx + (cx - bx) / after * radius, by + (cy - by) / after * radius
        out.append(("L", [sx, sy]))
        out.append(("Q", [bx, by, ex, ey]))
    out.append(("L", [pts[-1][0], pts[-1][1]]))
    return out


def arrow_route(el: Dict[str, Any], per_piece: int = 12) -> List[Point]:
    """The line an arrow is drawn along, as a polyline: its points, or its curve sampled (what a label sits on)."""
    points = _points(el.get("points"))
    if not el.get("curve") or len(points) < 2:
        return points
    route = [points[0]]
    for (ax, ay), (cx, cy), (bx, by) in curve_pieces(points):
        for step in range(1, per_piece + 1):
            t = step / float(per_piece)
            route.append(((1 - t) ** 2 * ax + 2 * (1 - t) * t * cx + t * t * bx, (1 - t) ** 2 * ay + 2 * (1 - t) * t * cy + t * t * by))
    return route


def arrow_midpoint(points: List[Point]) -> Point:
    """Halfway along the polyline."""
    if not points:
        return 0.0, 0.0
    lengths = [math.hypot(b[0] - a[0], b[1] - a[1]) for a, b in zip(points, points[1:])]
    half = sum(lengths) / 2.0
    for (a, b), length in zip(zip(points, points[1:]), lengths):
        if half <= length and length > 0:
            t = half / length
            return a[0] + (b[0] - a[0]) * t, a[1] + (b[1] - a[1]) * t
        half -= length
    return points[-1]


def head_points(kind: str, tip: Point, back: Point, width: float) -> Optional[Dict[str, Any]]:
    """An arrow head's geometry, or None for ``none`` (or a zero-length end): a chevron (``arrow``) or a
    ``triangle`` as three points ``[left, tip, right]``, a ``dot`` as a circle at the tip."""
    dx, dy = tip[0] - back[0], tip[1] - back[1]
    length = math.hypot(dx, dy)
    if kind == "none" or length == 0:
        return None
    if kind == "dot":
        return {"shape": "dot", "cx": tip[0], "cy": tip[1], "r": 3 + width}
    ux, uy = dx / length, dy / length
    size = 10.0 + 2.0 * width
    bx, by = tip[0] - ux * size, tip[1] - uy * size
    nx, ny = -uy * size * 0.5, ux * size * 0.5
    points = [[bx + nx, by + ny], [tip[0], tip[1]], [bx - nx, by - ny]]
    return {"shape": "triangle" if kind == "triangle" else "chevron", "points": points}


#: An arrow label wraps at 11 x its font size, however long the arrow (Excalidraw widened it with the
#: arrow, so an arrow lengthened to make room for its label got a wider label that needed more room,
#: QA R-3); the canvas stores the lines (``fit.lines``) and the page draws them. It sits in a pill with
#: this padding (design spec 6.1).
ARROW_LABEL_EMS = 11
ARROW_LABEL_PAD = (8, 2)
#: An arrow label is drawn at this fraction of the arrow's text size (a style ``size`` of 20 is the
#: design's 16-unit ``label`` role); the page draws it at the same size (web/src/canvas/kinds/arrow.js).
ARROW_LABEL_SCALE = 0.8


def arrow_label_width(el: Dict[str, Any], size: float) -> float:
    """The width an arrow's label wraps at."""
    return ARROW_LABEL_EMS * float(size)


def _font_key(style: Dict[str, Any]) -> str:
    return str(style.get("font") or "normal")


def arrow_label_text(el: Dict[str, Any]) -> Optional[Tuple[Tuple[float, float], float, List[str]]]:
    """An arrow label's pill size ``(w, h)``, font size and lines, wherever it goes; None without one."""
    label = str(el.get("text") or "")
    if not label or len(_points(el.get("points"))) < 2:
        return None
    style = el.get("style") if isinstance(el.get("style"), dict) else {}
    size = float(style.get("size") or 20) * ARROW_LABEL_SCALE
    found = _label_block(label, _font_key(style), size, _ctext._CORRECTIONS.get())
    return found[0], found[1], list(found[2])


#: Arrow labels measured, by their text, font, size and the corrections in force: a label is asked for its pill by the
#: drawing, the hit test, the editor, the bounds and the id badge, and wrapping is the costly part.
_LABEL_BLOCKS: Dict[Tuple[Any, ...], Tuple[Tuple[float, float], float, Tuple[str, ...]]] = {}


def _label_block(label: str, font: str, size: float, corrections: Any) -> Tuple[Tuple[float, float], float, Tuple[str, ...]]:
    key = (label, font, size, corrections[0] if corrections is not None else "", id(_ctext.load()))
    found = _LABEL_BLOCKS.get(key)
    if found is None:
        lines = _ctext.wrap(label, ARROW_LABEL_EMS * size, font, size)
        widest = max(_ctext.measure(line, font, size).width for line in lines)
        found = ((widest + 2 * ARROW_LABEL_PAD[0], len(lines) * _ctext.line_height(size) + 2 * ARROW_LABEL_PAD[1]), size, tuple(lines))
        if len(_LABEL_BLOCKS) > 4096:
            _LABEL_BLOCKS.clear()
        _LABEL_BLOCKS[key] = found
    return found


def arrow_label_center(el: Dict[str, Any]) -> Point:
    """Where an arrow's label is centred: the spot the canvas chose for it (``label_at``, QA R-3), else the
    arrow's midpoint (an arrow stored before labels were placed)."""
    spot = el.get("label_at")
    if isinstance(spot, (list, tuple)) and len(spot) == 2 and all(isinstance(v, (int, float)) for v in spot):
        return float(spot[0]), float(spot[1])
    return arrow_midpoint(arrow_route(el))


def arrow_label_pill(el: Dict[str, Any]) -> Optional[Tuple[Tuple[float, float, float, float], float, List[str]]]:
    """An arrow label's pill ``(x, y, w, h)`` at its placed centre, its font size and lines; None without one."""
    found = arrow_label_text(el)
    if found is None:
        return None
    (box_w, box_h), size, lines = found
    mx, my = arrow_label_center(el)
    return (mx - box_w / 2, my - box_h / 2, box_w, box_h), size, lines


# --------------------------------------------------------------------------
# frames

#: A frame's title band (``canvas.FRAME_TOP``) and where its title sits in it (design spec 6.2).
FRAME_BAND = 40
FRAME_TITLE_AT = (20, 8)
FRAME_TITLE_SIZE = 16
FRAME_TITLE_WEIGHT = 600
#: The one zoom rule of phase 1 (1.6): at this many screen pixels per unit or more the title sits in the
#: band; below it the full title stands above the frame, never under ``FRAME_TITLE_MIN_PX`` on screen.
FRAME_TITLE_LOD = 0.75
FRAME_TITLE_MIN_PX = 12
FRAME_TITLE_MAX_CHARS = 120


def fit_line(text: str, width: float, size: float, weight: int) -> str:
    """``text`` on one line no wider than ``width``, ending with … when cut."""
    if _ctext.measure(text, size=size, weight=weight).width <= width:
        return text
    while text and _ctext.measure(text + "…", size=size, weight=weight).width > width:
        text = text[:-1]
    return text.rstrip() + "…" if text else ""


_fit_line = fit_line


def frame_title(el: Dict[str, Any], u: float) -> Optional[Tuple[str, float, float, float]]:
    """A frame's title as drawn at ``u`` canvas units per pixel: ``(line, x, top, size)``, or None without one.

    In the band (scale ``1/u`` at least ``FRAME_TITLE_LOD``) it is cut to the band's width at 16; above the
    frame it is the whole title at ``max(16, 12 u)``, its line ending at the frame's top edge."""
    title = str(el.get("text") or "")
    if not title:
        return None
    x0, y0, x1, _y1 = bounds(el)
    scale = 1.0 / u if u > 0 else float("inf")
    if scale >= FRAME_TITLE_LOD:
        line = fit_line(title[:FRAME_TITLE_MAX_CHARS], (x1 - x0) - 2 * FRAME_TITLE_AT[0], FRAME_TITLE_SIZE, FRAME_TITLE_WEIGHT)
        return line, x0 + FRAME_TITLE_AT[0], y0 + FRAME_TITLE_AT[1], float(FRAME_TITLE_SIZE)
    size = max(float(FRAME_TITLE_SIZE), FRAME_TITLE_MIN_PX / scale)
    return title[:FRAME_TITLE_MAX_CHARS], x0, y0 - _ctext.line_height(size), size


_frame_title = frame_title


def frame_title_box(el: Dict[str, Any], u: float) -> Optional[Box]:
    """Where a frame's title is drawn at ``u`` units per pixel, ``(x0, y0, x1, y1)``; it may stand above the frame."""
    title = frame_title(el, u)
    if title is None:
        return None
    line, x, top, size = title
    return x, top, x + _ctext.measure(line, size=size, weight=FRAME_TITLE_WEIGHT).width, top + _ctext.line_height(size)


# --------------------------------------------------------------------------
# bounds and the view box


#: A comment pin's radius at 100% zoom: what its bounds hold.
PIN_R = 10


def drawn_bounds(el: Dict[str, Any]) -> Box:
    """Everything an element draws: its box, for an arrow its curve's bow and its label's pill, which
    may lie outside its points' box (a view of a region still draws a label that reaches into it), and
    for a comment its pin at 100% zoom."""
    if el.get("type") == "comment":
        point = el.get("point") if isinstance(el.get("point"), list) and len(el["point"]) >= 2 else [el.get("x") or 0, el.get("y") or 0]
        try:
            px, py = float(point[0]), float(point[1])
        except (TypeError, ValueError):
            px, py = 0.0, 0.0
        return px - PIN_R, py - PIN_R, px + PIN_R, py + PIN_R
    box = bounds(el)
    if el.get("type") != "arrow":
        return box
    boxes = [box]
    route = arrow_route(el)
    if route:
        boxes.append((min(p[0] for p in route), min(p[1] for p in route), max(p[0] for p in route), max(p[1] for p in route)))
    pill = arrow_label_pill(el) if el.get("text") else None
    if pill is not None:
        (px, py, pw, ph), _size, _lines = pill
        boxes.append((px, py, px + pw, py + ph))
    return min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)


def pixel_size(box: Sequence[float], max_px: int = DEFAULT_MAX_PX) -> Tuple[int, int]:
    """The PNG size for a view box: the longer side scaled to ``max_px``."""
    width, height = box[2] - box[0], box[3] - box[1]
    scale = max_px / max(width, height, 1.0)
    return max(1, int(round(width * scale))), max(1, int(round(height * scale)))


def units_per_px(box: Sequence[float], max_px: int = DEFAULT_MAX_PX) -> float:
    """Canvas units per output pixel of a picture of ``box``: its width over its pixel width (both writers)."""
    return (box[2] - box[0]) / float(pixel_size(box, max_px)[0])


def normalize_region(region: Sequence[float]) -> Box:
    """A region as ``(x0, y0, x1, y1)`` with the smaller ends first, at least one unit wide and tall."""
    x0, y0, x1, y1 = (float(v) for v in region)
    return min(x0, x1), min(y0, y1), max(x0, x1, min(x0, x1) + 1), max(y0, y1, min(y0, y1) + 1)


def view_box(scene: Dict[str, Any], region: Optional[Sequence[float]] = None, max_px: int = DEFAULT_MAX_PX) -> Box:
    """The region, or everything on the canvas plus ``PADDING``, as ``(x0, y0, x1, y1)``.

    Everything is what the picture draws, not only element boxes: an arrow label's pill (wider than a
    short arrow) and a frame's title, which stands above the frame when the board is zoomed out, so
    they are never cut at the picture's edge (QA F-10). A title grows with the units per pixel, which
    grow with the box, so the box is settled over a few rounds at ``max_px``.
    """
    if region is not None:
        return normalize_region(region)
    if any(isinstance(c, dict) and isinstance(c.get("region"), list) for c in scene.get("claims") or []):
        # A claim's label hangs outside its region at a fixed screen size, from whichever corner keeps it off words at
        # that zoom, and the display list's own box holds it (``canvas_display.bbox_of``): the picture draws that box,
        # so this answers the same box rather than a second guess at where the labels went.
        from herdr_team import canvas_display as _display  # it imports this module: late on purpose

        found = _display.bbox_of(_display.entries(scene), max_px)
        return tuple(_display.r2(v) for v in found)  # type: ignore[return-value]
    elements = [e for e in scene.get("elements") or [] if isinstance(e, dict)]
    boxes: List[Sequence[float]] = [drawn_bounds(e) for e in elements]
    for item in list(scene.get("claims") or []) + list(scene.get("locks") or []):
        region_box = item.get("region") if isinstance(item, dict) else None
        if isinstance(region_box, list) and len(region_box) == 4:
            boxes.append(tuple(float(v) for v in region_box))
    return settle_box(boxes, [e for e in elements if e.get("type") == "frame" and e.get("text")], max_px)


def settle_box(boxes: Sequence[Sequence[float]], frames: Sequence[Dict[str, Any]], max_px: int = DEFAULT_MAX_PX) -> Box:
    """``boxes`` padded by ``PADDING``, grown until it holds every frame title as drawn at its own scale."""
    if not boxes:
        return -PADDING, -PADDING, 400.0 + PADDING, 300.0 + PADDING

    def padded(found: Sequence[Sequence[float]]) -> Box:
        return (min(b[0] for b in found) - PADDING, min(b[1] for b in found) - PADDING,
                max(b[2] for b in found) + PADDING, max(b[3] for b in found) + PADDING)

    box = padded(boxes)
    for _round in range(4):
        if not frames:
            break
        u = units_per_px(box, max_px)
        titles = [t for t in (frame_title_box(el, u) for el in frames) if t is not None]
        grown = padded(list(boxes) + titles)
        if grown == box:
            break
        box = grown
    return box
