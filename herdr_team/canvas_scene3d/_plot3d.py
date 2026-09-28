"""3D data charts drawn for the agent's picture (canvas v2 phase 4, 3.11, interface I-7): a plot box seen in iso.

The GL chart types (``canvas_charts.bar3d``, ``scatter3d``, ``surface``)
render on the page through echarts-gl; without a page an agent still gets a
real picture, drawn here with the scene projections' camera (``_project.View``,
the ``iso`` preset, the same angles as the option's ``grid3D.viewControl``):

- a plot box ``W × H × D`` in world units: ``x`` runs along +x, the chart's ``y``
  along +z (toward the viewer) and the value (``z``) up +y, as echarts-gl's
  ``grid3D`` puts them;
- the floor grid and the two far walls with their value gridlines, drawn first;
- the marks, far to near: ``bar`` (a box, three visible faces shaded from the
  ``chart.seq`` ramp), ``point`` (a small square) and ``quad`` (a surface cell);
- the labels last: category or value ticks along the two front floor edges, the
  value axis up the left edge, and each axis's title.

Everything is token paints (``chart.*``), so one drawing serves both themes,
and at most ``MAX_PRIMS`` primitives (a chart drawing's cap): past it the
lowest marks are left out and ``notes`` says how many.

Pure, stdlib only.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from herdr_team import canvas_display as D
from herdr_team import canvas_text
from herdr_team.canvas_scene3d import _tokens
from herdr_team.canvas_scene3d import _vec as V
from herdr_team.canvas_scene3d._project import View

MAX_PRIMS = 1500
FONT = 12
LOD_TEXT = list(D.LOD_BODY)
LOD_LINE = list(D.LOD_LABEL)
INK, MUTED, AXIS, GRID, PAPER = "chart.ink", "chart.muted", "chart.axis", "chart.gridline", "chart.paper"
#: Room kept around the plot box for tick labels and titles, in picture units.
LABEL_ROOM = 64.0
SEQ_STEPS = 9

P2 = Tuple[float, float]


def seq_index(t: float) -> int:
    """The ``chart.seq`` step of a mark's top at ``t`` in 0..1 (1 ... 4: its sides take the next two, darker, steps; the
    ramp's last two are near black and stay out)."""
    t = min(1.0, max(0.0, t if math.isfinite(t) else 0.0))
    return 1 + int(math.floor(t * 3 + 0.5))


def seq_ref(index: int) -> str:
    return "chart.seq.{}".format(max(0, min(SEQ_STEPS - 1, int(index))))


def _n(value: float) -> float:
    return float(round(float(value), 2))


def fit_label(text: str, room: float, size: float = FONT) -> str:
    """``text`` cut with an ellipsis to ``room`` picture units."""
    from herdr_team import canvas_geometry

    return canvas_geometry.fit_line(str(text), max(8.0, room), size, 400)


class Plot3D:
    """One plot box in a slot, seen in iso. Add marks, then ``items()``."""

    def __init__(self, slot: Sequence[float], size: Sequence[float], view: str = "iso") -> None:
        self.slot = tuple(float(v) for v in slot)
        self.w, self.h, self.d = (max(1e-6, float(v)) for v in size)
        cam = _tokens.camera(view)
        self.view = View(float(cam.get("az", 45)), float(cam.get("el", 35.264)))
        x, y, w, h = self.slot
        inner = (x + LABEL_ROOM, y + 8.0, max(1.0, w - 2 * LABEL_ROOM), max(1.0, h - 8.0 - LABEL_ROOM * 0.75))
        self.view.fit(V.corners((0.0, 0.0, 0.0, self.w, self.h, self.d)), inner)
        self.marks: List[Tuple[float, int, List[Dict[str, Any]], float]] = []
        self.back: List[Dict[str, Any]] = []
        self.front: List[Dict[str, Any]] = []
        #: The text boxes placed so far (``x0, y0, x1, y1``): a label that would meet one is left out.
        self.taken: List[Tuple[float, float, float, float]] = []
        self.notes: List[str] = []

    # -- geometry ------------------------------------------------------------------------------------
    def p(self, point: Sequence[float]) -> P2:
        found = self.view.p(point)
        return (_n(found[0]), _n(found[1]))

    def depth(self, point: Sequence[float]) -> float:
        return self.view.depth(point)

    # -- the frame -----------------------------------------------------------------------------------
    def floor_and_walls(self, xs: Sequence[float], zs: Sequence[float], ys: Sequence[float]) -> None:
        """The floor (lines at ``xs`` along z and ``zs`` along x) and the far walls (lines at the value ticks ``ys``)."""
        w, h, d = self.w, self.h, self.d
        line = self.back.append
        outline = [(0.0, 0.0, 0.0), (w, 0.0, 0.0), (w, 0.0, d), (0.0, 0.0, d)]
        line({"k": "poly", "points": [list(self.p(q)) for q in outline], "closed": True, "fill": PAPER, "stroke": AXIS, "sw_px": 1})
        for x in xs:
            line({"k": "line", "points": [list(self.p((x, 0.0, 0.0))), list(self.p((x, 0.0, d)))], "stroke": GRID, "sw_px": 1,
                  "lod": LOD_LINE})
        for z in zs:
            line({"k": "line", "points": [list(self.p((0.0, 0.0, z))), list(self.p((w, 0.0, z)))], "stroke": GRID, "sw_px": 1,
                  "lod": LOD_LINE})
        # The far walls: x = 0 (spanning z) and z = 0 (spanning x), seen from +x +z.
        for wall in ([(0.0, 0.0, 0.0), (0.0, h, 0.0), (0.0, h, d), (0.0, 0.0, d)], [(0.0, 0.0, 0.0), (w, 0.0, 0.0), (w, h, 0.0), (0.0, h, 0.0)]):
            line({"k": "poly", "points": [list(self.p(q)) for q in wall], "closed": True, "fill": None, "stroke": AXIS, "sw_px": 1})
        for y in ys:
            if y <= 1e-9 or y > h + 1e-9:
                continue
            line({"k": "line", "points": [list(self.p((0.0, y, d))), list(self.p((0.0, y, 0.0))), list(self.p((w, y, 0.0)))], "stroke": GRID,
                  "sw_px": 1, "lod": LOD_LINE})

    def _text(self, text: str, at: P2, anchor: str, fill: str = MUTED, size: float = FONT, weight: int = 400) -> bool:
        """One line of text vertically centred on ``at``; left out (False) when it would meet a label already placed."""
        if not text:
            return False
        lh = canvas_text.line_height(size)
        prim = D.text_prim([text], at[0], at[1] - lh / 2.0, size, {}, fill, anchor, None, weight)
        box = prim["box"]
        rect = (box[0] - 1.0, box[1], box[0] + box[2] + 1.0, box[1] + box[3])
        if any(rect[0] < o[2] and o[0] < rect[2] and rect[1] < o[3] and o[1] < rect[3] for o in self.taken):
            return False
        self.taken.append(rect)
        prim["lod"] = LOD_TEXT
        self.front.append(prim)
        return True

    def edge_labels(self, labels: Sequence[Tuple[float, str]], axis: str, title: str = "") -> None:
        """Tick labels along a front floor edge: ``axis`` ``x`` (the edge z = D, labels to its lower left) or ``z`` (the edge
        x = W, to its lower right), each at its position along the axis; crowded ones are thinned (every k-th), and one that
        still meets a label placed before it (the value axis's, at the corner) is left out."""
        if not labels:
            return
        anchor = "end" if axis == "x" else "start"
        pad = 0.06 * max(self.w, self.d)
        points = self._edge_points([pos for pos, _t in labels], axis)
        lh = canvas_text.line_height(FONT)
        step = self.edge_step([pos for pos, _t in labels], axis)
        room = LABEL_ROOM + 0.5 * max(0.0, self.slot[2] - 2 * LABEL_ROOM) / 2.0
        for index in range(0, len(labels), step):
            text = fit_label(labels[index][1], min(room, 110.0))
            self._text(text, points[index], anchor)
        if step > 1:
            self.notes.append("{} labels: every {} shown".format(title or axis, _ordinal(step)))
        if title:
            end = self.p((self.w / 2.0, 0.0, self.d + pad * 4)) if axis == "x" else self.p((self.w + pad * 4, 0.0, self.d / 2.0))
            end = (end[0] - (18.0 if axis == "x" else -18.0), end[1] + lh)
            self._text(fit_label(title, 140.0), end, anchor, INK, FONT, 600)

    def _edge_points(self, positions: Sequence[float], axis: str) -> List[P2]:
        pad = 0.06 * max(self.w, self.d)
        return [self.p((pos, 0.0, self.d + pad)) if axis == "x" else self.p((self.w + pad, 0.0, pos)) for pos in positions]

    def edge_step(self, positions: Sequence[float], axis: str) -> int:
        """Every how many tick labels along a floor edge are shown (1: all): the thinning ``edge_labels`` does, which the
        page's echarts-gl option takes as its category axes' ``axisLabel.interval`` (QA phase34 low: crowded labels met
        at the floor's near corner)."""
        points = self._edge_points(positions, axis)
        lh = canvas_text.line_height(FONT)
        spacing = min((abs(points[i + 1][1] - points[i][1]) for i in range(len(points) - 1)), default=lh * 2)
        return max(1, int(math.ceil((lh + 2.0) / max(spacing, 1e-6))))

    def value_labels(self, ticks: Sequence[Tuple[float, str]], title: str = "") -> None:
        """Value ticks up the left vertical edge (x = 0, z = D), with the axis title above it."""
        for y, text in ticks:
            at = self.p((0.0, y, self.d))
            self._text(text, (at[0] - 6.0, at[1]), "end")
            tick_end = (at[0] - 3.0, at[1])
            self.front.append({"k": "line", "points": [list(at), [_n(tick_end[0]), _n(tick_end[1])]], "stroke": AXIS, "sw_px": 1,
                               "lod": LOD_LINE})
        if title:
            top = self.p((0.0, self.h, self.d))
            self._text(fit_label(title, 140.0), (top[0], top[1] - canvas_text.line_height(FONT)), "middle", INK, FONT, 600)

    def legend(self, items: Sequence[Tuple[str, str]]) -> None:
        """Swatches with their names in the slot's top-right corner."""
        x, y, w, _h = self.slot
        lh = canvas_text.line_height(FONT)
        widest = max((canvas_text.measure(fit_label(name, 120.0), "normal", FONT, 400).width for name, _paint in items), default=0.0)
        left = x + w - widest - 18.0
        for index, (name, paint) in enumerate(items[:12]):
            top = y + 4.0 + index * (lh + 2.0)
            self.front.append({"k": "rect", "x": _n(left), "y": _n(top + (lh - 10.0) / 2.0), "w": 10, "h": 10, "r": 2, "fill": paint})
            self._text(fit_label(name, 120.0), (left + 14.0, top + lh / 2.0), "start", INK)

    # -- marks ---------------------------------------------------------------------------------------
    def bar(self, box: Sequence[float], top: str, left: str, right: str, key: float = 0.0, stroke: str = PAPER) -> None:
        """A box mark: its faces that face the camera, shaded top, left and right."""
        prims = []
        centre = V.box_center(box)
        faces = [((0.0, 1.0, 0.0), top), ((0.0, -1.0, 0.0), top), ((1.0, 0.0, 0.0), right), ((-1.0, 0.0, 0.0), left),
                 ((0.0, 0.0, 1.0), left), ((0.0, 0.0, -1.0), right)]
        x0, y0, z0, x1, y1, z1 = box
        quads = {(0.0, 1.0, 0.0): [(x0, y1, z0), (x1, y1, z0), (x1, y1, z1), (x0, y1, z1)],
                 (0.0, -1.0, 0.0): [(x0, y0, z0), (x1, y0, z0), (x1, y0, z1), (x0, y0, z1)],
                 (1.0, 0.0, 0.0): [(x1, y0, z0), (x1, y1, z0), (x1, y1, z1), (x1, y0, z1)],
                 (-1.0, 0.0, 0.0): [(x0, y0, z0), (x0, y1, z0), (x0, y1, z1), (x0, y0, z1)],
                 (0.0, 0.0, 1.0): [(x0, y0, z1), (x1, y0, z1), (x1, y1, z1), (x0, y1, z1)],
                 (0.0, 0.0, -1.0): [(x0, y0, z0), (x1, y0, z0), (x1, y1, z0), (x0, y1, z0)]}
        for normal, paint in faces:
            if V.dot(normal, self.view.eye) <= 1e-6:
                continue
            prims.append({"k": "poly", "points": [list(self.p(q)) for q in quads[normal]], "closed": True, "fill": paint, "stroke": stroke,
                          "sw_px": 0.75})
        # Far to near by the footprint's centre on the floor (a grid of bars seen from +x +z sorts exactly so).
        self.marks.append((self.depth((centre[0], 0.0, centre[2])), len(self.marks), prims, key))

    def point(self, at: Sequence[float], px: float, paint: str, key: float = 0.0, stem: bool = False) -> None:
        """A point mark: a square ``px`` wide at ``at`` (with a faint line down to the floor when ``stem``)."""
        cx, cy = self.p(at)
        prims: List[Dict[str, Any]] = []
        if stem and at[1] > 1e-9:
            prims.append({"k": "line", "points": [[cx, cy], list(self.p((at[0], 0.0, at[2])))], "stroke": GRID, "sw_px": 1, "lod": LOD_LINE})
        half = px / 2.0
        prims.append({"k": "rect", "x": _n(cx - half), "y": _n(cy - half), "w": _n(px), "h": _n(px), "r": _n(min(2.0, half)), "fill": paint,
                      "stroke": PAPER, "sw_px": 0.5})
        self.marks.append((self.depth(at), len(self.marks), prims, key))

    def quad(self, corners: Sequence[Sequence[float]], paint: str, stroke: Optional[str], key: float = 0.0) -> None:
        """A surface cell: a filled quadrilateral (its edge in ``stroke``, or its own paint for no wireframe)."""
        centre = V.mul(V.add(V.add(corners[0], corners[1]), V.add(corners[2], corners[3])), 0.25)
        prim = {"k": "poly", "points": [list(self.p(q)) for q in corners], "closed": True, "fill": paint, "stroke": stroke or paint,
                "sw_px": 0.5}
        self.marks.append((self.depth(centre), len(self.marks), [prim], key))

    # -- output --------------------------------------------------------------------------------------
    def items(self) -> List[Dict[str, Any]]:
        """Everything, back to front, within ``MAX_PRIMS``: past it the marks with the lowest ``key`` are left out."""
        room = MAX_PRIMS - len(self.back) - len(self.front)
        marks = list(self.marks)
        total = sum(len(m[2]) for m in marks)
        if total > room:
            dropped = 0
            for mark in sorted(marks, key=lambda m: (m[3], m[1])):
                if total <= room:
                    break
                total -= len(mark[2])
                marks.remove(mark)
                dropped += 1
            self.notes.append("{} lowest marks not drawn (the picture holds {} primitives)".format(dropped, MAX_PRIMS))
        marks.sort(key=lambda m: (-m[0], m[1]))
        out = list(self.back)
        for mark in marks:
            out += mark[2]
        return out + self.front


def _ordinal(k: int) -> str:
    return {2: "2nd", 3: "3rd"}.get(k, "{}th".format(k))


# --------------------------------------------------------------------------
# the echarts-gl option pieces the three GL chart types share (theme-neutral, 2.6)


def box_size(size: Sequence[float]) -> Dict[str, float]:
    """``grid3D``'s box in echarts-gl units (the longest side 100), in the drawing's proportions."""
    w, h, d = (max(1e-6, float(v)) for v in size)
    k = 100.0 / max(w, h, d)
    return {"boxWidth": _n(w * k), "boxHeight": _n(h * k), "boxDepth": _n(d * k)}


def grid3d(size: Sequence[float], view: str = "iso") -> Dict[str, Any]:
    """``grid3D`` seen as the drawing sees it: orthographic, at the view's elevation (``alpha``) and azimuth (``beta``)."""
    cam = _tokens.camera(view)
    text = {"color": MUTED, "fontFamily": "$sans", "fontSize": FONT}
    out: Dict[str, Any] = {"viewControl": {"projection": "orthographic", "alpha": float(cam.get("el", 35.264)), "beta": float(cam.get("az", 45)),
                                           "autoRotate": False, "animation": False},
                           "axisLine": {"lineStyle": {"color": AXIS}}, "axisTick": {"lineStyle": {"color": AXIS}},
                           "splitLine": {"lineStyle": {"color": GRID}}, "axisPointer": {"show": False},
                           "axisLabel": {"textStyle": text}, "axisTitle": {"textStyle": dict(text, color=INK)},
                           "environment": "none", "light": {"main": {"intensity": 1.1, "shadow": False}, "ambient": {"intensity": 0.45}}}
    out.update(box_size(size))
    return out


def axis3d(kind: str, name: str, data: Optional[Sequence[str]] = None, lo: Optional[float] = None, hi: Optional[float] = None,
           step: Optional[float] = None, fmt_id: Optional[str] = None, every: Optional[int] = None) -> Dict[str, Any]:
    """An ``xAxis3D``/``yAxis3D``/``zAxis3D``: a category axis over ``data`` (``every``: show every k-th label, as the
    drawing thins them), or a value axis with explicit bounds."""
    out: Dict[str, Any] = {"type": kind, "name": name, "nameTextStyle": {"color": INK, "fontFamily": "$sans", "fontSize": FONT}}
    if kind == "category":
        out["data"] = list(data or [])
        if every:
            out["axisLabel"] = {"interval": max(0, int(every) - 1)}
    else:
        out.update(min=lo, max=hi, interval=step)
        if fmt_id:
            out["axisLabel"] = {"formatter": {"$fmt": fmt_id}}
    return out


def visual_map(lo: float, hi: float, dimension: int) -> Dict[str, Any]:
    """The value's colour ramp (``chart.seq``), not shown: the gist and the axis say the values."""
    return {"show": False, "dimension": dimension, "min": lo, "max": hi if hi > lo else lo + 1,
            "inRange": {"color": [seq_ref(i) for i in range(1, 7)]}}
