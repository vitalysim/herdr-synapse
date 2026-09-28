"""3D surfaces (canvas v2 phase 4, 3.11): a measure over a grid of two numbers, as a shaded sheet, through echarts-gl.

``x`` and ``y`` are quantitative and together form a grid (every x with every
y, most cells present), ``z`` is the height (the mean where rows repeat, or
``aggregate``). At most 100 × 100 grid points; a scatter of unrelated points
is refused (a surface needs its grid, else use ``scatter3d``). ``wireframe``
(default true) draws the cell edges. The gist names the peak and the trough
and the direction the surface rises in; the picture is the sheet's cells,
shaded by height on the ``chart.seq`` ramp, far to near.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team.canvas_charts import Channel, ChartType, _data
from herdr_team.canvas_charts import _format as F
from herdr_team.canvas_charts import _frame as FR
from herdr_team.canvas_charts import _gist as G
from herdr_team.canvas_charts import _option as O
from herdr_team.canvas_charts import _scale as S
from herdr_team.canvas_charts._data import invalid, refuse
from herdr_team.canvas_charts._frame import Frame
from herdr_team.canvas_scene3d import _plot3d as P

ORDER = 220
CAP = 100
#: The share of the x × y grid that must hold a value.
MIN_COVERAGE = 0.5
#: Grid lines the picture keeps along each axis (an even stride of the model's).
DRAW_SIDE = 30
SIZE = (1.0, 0.6, 1.0)


def normalize(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
    spec.setdefault("wireframe", True)
    return spec


def model(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
    x, y, z = spec["x"], spec["y"], spec["z"]
    rows = list(range(table.rows))
    repeats = _data.repeats(table, rows, [x, y])
    how = spec.get("aggregate") or ("mean" if repeats else "none")
    if how == "none" and repeats:
        raise invalid("aggregate", "{} and {} repeat; pick how to combine them: aggregate: mean, median, min, max, sum or count".format(x, y))
    cells = _data.aggregate(table, rows, [x, y], z, how if how != "none" else "last")
    xs = sorted({k[0] for k in cells if k[0] is not None})
    ys = sorted({k[1] for k in cells if k[1] is not None})
    for name, values in ((x, xs), (y, ys)):
        if len(values) > CAP:
            raise refuse("chart_refused", "a surface takes at most {} × {} grid points; {} has {} distinct values: round or bin it "
                         "(or use scatter3d for loose points)".format(CAP, CAP, name, len(values)), field=name, limit="CAP", max=CAP)
        if len(values) < 2:
            raise invalid(name, "a surface needs at least 2 distinct {} values; {} has {}".format(name, name, len(values)))
    xi = {v: i for i, v in enumerate(xs)}
    yi = {v: j for j, v in enumerate(ys)}
    grid: List[List[Optional[float]]] = [[None] * len(xs) for _ in ys]
    filled = 0
    for (kx, ky), value in cells.items():
        if kx is None or ky is None or value is None:
            continue
        grid[yi[ky]][xi[kx]] = _data.compact_number(value)
        filled += 1
    coverage = filled / float(len(xs) * len(ys))
    if coverage < MIN_COVERAGE:
        raise refuse("chart_refused", "a surface needs z on a grid of {} × {}: only {} of {} cells ({:.0f} %) have a value; use scatter3d "
                     "for loose points".format(x, y, filled, len(xs) * len(ys), coverage * 100), field="z")
    return {"xs": [_data.compact_number(v) for v in xs], "ys": [_data.compact_number(v) for v in ys], "grid": grid, "how": how,
            "filled": filled, "capped": []}


def _values(model: Mapping[str, Any]) -> List[float]:
    return [float(v) for row in model.get("grid") or [] for v in row if v is not None]


def ranges(model: Mapping[str, Any]) -> List[Tuple[float, float, float, List[float]]]:
    xs, ys = model.get("xs") or [0, 1], model.get("ys") or [0, 1]
    zs = _values(model)
    return [S.nice_ticks(min(xs), max(xs), 5), S.nice_ticks(min(ys), max(ys), 5),
            S.nice_ticks(*S.value_range(zs, False), count=5)]


def frame(spec: Dict[str, Any], model: Dict[str, Any], box: Tuple[float, float]) -> Frame:
    found = ranges(model)
    return FR.none(box, {a: {"min": r[0], "max": r[1], "step": r[2]} for a, r in zip(("x", "y", "z"), found)})


def _fmt(spec: Mapping[str, Any], axis: str) -> Tuple[Optional[str], Optional[str]]:
    fmt = (spec.get("format") or {}).get(axis)
    unit = (spec.get("units") or {}).get(axis) or (spec.get("_units") or {}).get(axis)
    return fmt, unit


def _tick(value: float, spec: Mapping[str, Any], axis: str, big: bool) -> str:
    fmt, unit = _fmt(spec, axis)
    return F.fmt_number(value, fmt or ("compact" if big else None), unit)


def z_title(spec: Mapping[str, Any], model: Mapping[str, Any]) -> str:
    how = str(model.get("how") or "none")
    return spec["z"] if how in ("none", "last") else "{}({})".format(how, spec["z"])


def draw(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, slot: Tuple[float, float, float, float]) -> List[Dict[str, Any]]:
    w, h, d = SIZE
    plot = P.Plot3D(slot, SIZE)
    (x0, x1, _xs, xt), (y0, y1, _ys, yt), (z0, z1, _zs, zt) = ranges(model)

    def at(value: float, lo: float, hi: float, length: float) -> float:
        return (value - lo) / ((hi - lo) or 1.0) * length

    plot.floor_and_walls([at(t, x0, x1, w) for t in xt], [at(t, y0, y1, d) for t in yt], [at(t, z0, z1, h) for t in zt])
    xs, ys, grid = model.get("xs") or [], model.get("ys") or [], model.get("grid") or []
    cols = _data.stride(len(xs), DRAW_SIDE)
    rows = _data.stride(len(ys), DRAW_SIDE)
    if len(cols) < len(xs) or len(rows) < len(ys):
        plot.notes.append("drawn at {} × {} of {} × {} grid points".format(len(cols), len(rows), len(xs), len(ys)))
    values = _values(model)
    lo, hi = (min(values), max(values)) if values else (0.0, 1.0)
    stroke = P.PAPER if spec.get("wireframe", True) else None
    for rj in range(len(rows) - 1):
        for ci in range(len(cols) - 1):
            corners = [(cols[ci], rows[rj]), (cols[ci + 1], rows[rj]), (cols[ci + 1], rows[rj + 1]), (cols[ci], rows[rj + 1])]
            zs = [grid[j][i] for i, j in corners]
            if any(v is None for v in zs):
                continue
            mean = math.fsum(float(v) for v in zs) / 4.0
            t = 0.0 if hi == lo else (mean - lo) / (hi - lo)
            points = [(at(float(xs[i]), x0, x1, w), at(float(v), z0, z1, h), at(float(ys[j]), y0, y1, d)) for (i, j), v in zip(corners, zs)]
            paint = P.seq_ref(1 + int(math.floor(t * 5 + 0.5)))
            plot.quad(points, paint, stroke, key=mean)
    big = [max(abs(r[0]), abs(r[1])) >= 1e4 for r in ((x0, x1), (y0, y1), (z0, z1))]
    plot.value_labels([(at(t, z0, z1, h), _tick(t, spec, "z", big[2])) for t in zt], z_title(spec, model))
    plot.edge_labels([(at(t, x0, x1, w), _tick(t, spec, "x", big[0])) for t in xt], "x", spec["x"])
    plot.edge_labels([(at(t, y0, y1, d), _tick(t, spec, "y", big[1])) for t in yt], "z", spec["y"])
    return plot.items()


def doc(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    xs, ys, grid = model.get("xs") or [], model.get("ys") or [], model.get("grid") or []
    return {"datasets": [], "refs": {"grid": [[xs[i], ys[j], grid[j][i]] for i in range(len(xs)) for j in range(len(ys))]}}


def option(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, gist: Sequence[str]) -> Dict[str, Any]:
    (x0, x1, xs, _xt), (y0, y1, ys, _yt), (z0, z1, zs, _zt) = ranges(model)
    values = _values(model)
    lo, hi = (min(values), max(values)) if values else (0.0, 1.0)
    out = O.base(gist, [P.seq_ref(4)])
    axes = {}
    for key, name, a, b, step in (("xAxis3D", "x", x0, x1, xs), ("yAxis3D", "y", y0, y1, ys), ("zAxis3D", "z", z0, z1, zs)):
        fmt, unit = _fmt(spec, name)
        axes[key] = P.axis3d("value", z_title(spec, model) if name == "z" else spec[name], lo=a, hi=b, step=step,
                             fmt_id=F.fmt_id(fmt or "auto", unit))
    out.update(tooltip=O.tooltip("item"), grid3D=P.grid3d(SIZE), visualMap=P.visual_map(lo, hi, 2), **axes)
    out["series"] = [{"type": "surface", "name": z_title(spec, model), "data": {"$doc": "grid"}, "shading": "color",
                      "wireframe": {"show": bool(spec.get("wireframe", True)), "lineStyle": {"color": "chart.paper", "width": 1}}}]
    return out


def stats(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    xs, ys, grid = model.get("xs") or [], model.get("ys") or [], model.get("grid") or []
    cells = [(float(xs[i]), float(ys[j]), float(v)) for j, row in enumerate(grid) for i, v in enumerate(row) if v is not None]
    out: Dict[str, Any] = {"n": len(cells), "nx": len(xs), "ny": len(ys)}
    if not cells:
        return out
    hi = max(range(len(cells)), key=lambda k: (cells[k][2], -k))
    lo = min(range(len(cells)), key=lambda k: (cells[k][2], k))
    out["peak"], out["trough"] = list(cells[hi]), list(cells[lo])
    # The least-squares plane z = a x + b y + c: which way the surface rises, over the grid's span.
    n = len(cells)
    mx = math.fsum(c[0] for c in cells) / n
    my = math.fsum(c[1] for c in cells) / n
    mz = math.fsum(c[2] for c in cells) / n
    sxx = math.fsum((c[0] - mx) ** 2 for c in cells)
    syy = math.fsum((c[1] - my) ** 2 for c in cells)
    sxy = math.fsum((c[0] - mx) * (c[1] - my) for c in cells)
    sxz = math.fsum((c[0] - mx) * (c[2] - mz) for c in cells)
    syz = math.fsum((c[1] - my) * (c[2] - mz) for c in cells)
    det = sxx * syy - sxy * sxy
    if det > 1e-12:
        a = (sxz * syy - syz * sxy) / det
        b = (syz * sxx - sxz * sxy) / det
        out["rise"] = [a * (max(xs) - min(xs)), b * (max(ys) - min(ys))]
    out["range"] = cells[hi][2] - cells[lo][2]
    return out


def gist(spec: Dict[str, Any], model: Dict[str, Any], stats: Mapping[str, Any]) -> List[str]:
    lines = [G.join(["x={} ({})".format(spec["x"], stats.get("nx", 0)), "y={} ({})".format(spec["y"], stats.get("ny", 0)),
                     "z={}".format(z_title(spec, model)), "{} grid points".format(stats.get("n", 0)), str(spec.get("_source") or "")])]
    if stats.get("peak") and not stats.get("range"):
        lines.append("flat: {} is {} everywhere".format(spec["z"], G.num(stats["peak"][2], spec, "z")))
        return lines
    parts = []
    for key in ("peak", "trough"):
        found = stats.get(key)
        if found:
            parts.append("{} {} at {}={}, {}={}".format(key, G.num(found[2], spec, "z"), spec["x"], G.num(found[0], spec, "x"), spec["y"],
                                                         G.num(found[1], spec, "y")))
    if parts:
        lines.append(G.join(parts))
    rise = stats.get("rise")
    if rise and stats.get("range"):
        words = []
        for axis, amount in zip(("x", "y"), rise):
            if abs(amount) < 0.1 * float(stats["range"]):
                continue
            words.append("{} as {} grows ({})".format("rises" if amount > 0 else "falls", spec[axis], G.num(amount, spec, "z")))
        lines.append("gradient: {} {}".format(spec["z"], " and ".join(words)) if words else "gradient: no overall slope (peaks and dips)")
    return lines


def min_box(spec: Dict[str, Any], model: Dict[str, Any]) -> Tuple[float, float]:
    return 360.0, 280.0


CHARTS = (
    ChartType(name="surface", channels=(Channel("x", required=True, types=("quantitative",), doc="a number along the floor's width (a grid)"),
                                        Channel("y", required=True, types=("quantitative",), doc="a number along the floor's depth (a grid)"),
                                        Channel("z", required=True, types=("quantitative",), aggregate=True, doc="the height")),
              options=("wireframe",), frame_kind="gl", normalize=normalize, model=model, stats=stats, doc=doc, frame=frame, option=option,
              draw=draw, gist=gist, vegalite=None, echarts=("SurfaceChart", "Grid3DComponent", "VisualMapComponent"), gl=True,
              min_box=min_box, default_box=(560, 420), caps="100 × 100 grid points; half the grid must hold values",
              doc_line="a measure over a grid of two numbers as a 3D sheet (echarts-gl); a heatmap reads exact values better",
              example='{"op": "chart", "id": "loss", "intent": "the loss landscape", "title": "Loss by learning rate and batch", '
                      '"type": "surface", "rows": [{"lr": 1, "batch": 16, "loss": 0.9}, {"lr": 2, "batch": 16, "loss": 0.7}, '
                      '{"lr": 1, "batch": 32, "loss": 0.8}, {"lr": 2, "batch": 32, "loss": 0.5}], "x": "lr", "y": "batch", "z": "loss"}',
              order=ORDER),
)
