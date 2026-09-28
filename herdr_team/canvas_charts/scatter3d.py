"""3D scatter (canvas v2 phase 4, 3.11): points by three measures, coloured by a category and sized by a fourth.

``x``, ``y`` and ``z`` are quantitative (``z`` is up, as echarts-gl's
``grid3D`` has it; ``y`` runs along the floor's depth); ``color`` splits the
points into at most 12 groups and ``size`` scales them. At most 5,000 points
(an even stride over the rows, reported as ``chart_capped``). The gist reads
the ranges, the correlation of each pair of axes and the groups; the picture
draws each point as a small square, far to near.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team.canvas_charts import Channel, ChartType, _data
from herdr_team.canvas_charts import _format as F
from herdr_team.canvas_charts import _frame as FR
from herdr_team.canvas_charts import _gist as G
from herdr_team.canvas_charts import _option as O
from herdr_team.canvas_charts import _scale as S
from herdr_team.canvas_charts._frame import Frame
from herdr_team.canvas_scene3d import _plot3d as P

ORDER = 210
CAP = 5000
CAP_GROUPS = 12
#: Points the picture draws (a stride over the model's); each is one square.
DRAW_CAP = 1200
#: Under this many points each gets a faint line down to the floor (depth reads better).
STEMS = 300
OTHER = "Other"
AXES = ("x", "y", "z")


def normalize(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
    return spec


def model(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
    names = [spec[a] for a in AXES]
    cols = [table.by_name[n].values for n in names]
    size_col = table.by_name[spec["size"]].values if spec.get("size") else None
    color_col = table.by_name[spec["color"]].values if spec.get("color") else None
    rows = [i for i in range(table.rows) if all(c[i] is not None for c in cols)]
    capped: List[str] = []
    groups: List[str] = []
    if color_col is not None:
        counts: Dict[str, int] = {}
        order: List[str] = []
        for i in rows:
            key = _label(color_col[i])
            if key not in counts:
                order.append(key)
                counts[key] = 0
            counts[key] += 1
        groups = sorted(order, key=lambda k: (-counts[k], k))
        if len(groups) > CAP_GROUPS:
            capped.append("{} of {} {} values folded into {}".format(len(groups) - CAP_GROUPS + 1, len(groups), spec["color"], OTHER))
            groups = groups[:CAP_GROUPS - 1] + [OTHER]
    index = {g: n for n, g in enumerate(groups)}
    # The same points in any row order: sorted by their values, then sampled.
    rows = sorted(rows, key=lambda i: (tuple(float(c[i]) for c in cols), _label(color_col[i]) if color_col is not None else "",
                                       float(size_col[i]) if size_col is not None and size_col[i] is not None else 0.0))
    kept = rows
    if len(rows) > CAP:
        kept = [rows[i] for i in _data.stride(len(rows), CAP)]
        capped.append("sampled {} of {} points (an even stride)".format(len(kept), len(rows)))
    points = []
    for i in kept:
        point = [_data.compact_number(float(c[i])) for c in cols]
        if color_col is not None:
            point.append(index.get(_label(color_col[i]), len(groups) - 1))
        if size_col is not None:
            point.append(_data.compact_number(float(size_col[i])) if size_col[i] is not None else None)
        points.append(point)
    return {"points": points, "groups": groups, "n": len(rows), "capped": capped,
            "_warnings": [{"code": "chart_capped", "message": note} for note in capped]}


def _label(value: Any) -> str:
    return "(null)" if value is None else str(value)


def _column(model: Mapping[str, Any], k: int) -> List[float]:
    return [float(p[k]) for p in model.get("points") or [] if p[k] is not None]


def ranges(model: Mapping[str, Any]) -> List[Tuple[float, float, float, List[float]]]:
    """Each axis's nice ``(min, max, step, ticks)``."""
    out = []
    for k in range(3):
        lo, hi = S.value_range(_column(model, k), False)
        out.append(S.nice_ticks(lo, hi, 5))
    return out


def frame(spec: Dict[str, Any], model: Dict[str, Any], box: Tuple[float, float]) -> Frame:
    found = ranges(model)
    return FR.none(box, {a: {"min": r[0], "max": r[1], "step": r[2]} for a, r in zip(AXES, found)})


def _fmt(spec: Mapping[str, Any], axis: str) -> Tuple[Optional[str], Optional[str]]:
    fmt = (spec.get("format") or {}).get(axis)
    unit = (spec.get("units") or {}).get(axis) or (spec.get("_units") or {}).get(axis)
    return fmt, unit


def _tick_text(value: float, spec: Mapping[str, Any], axis: str, big: bool) -> str:
    fmt, unit = _fmt(spec, axis)
    return F.fmt_number(value, fmt or ("compact" if big else None), unit)


SIZE = (1.0, 0.7, 1.0)


def colors(model: Mapping[str, Any]) -> List[str]:
    return [S.cat(i) for i in range(max(1, len(model.get("groups") or [])))]


def draw(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, slot: Tuple[float, float, float, float]) -> List[Dict[str, Any]]:
    w, h, d = SIZE
    plot = P.Plot3D(slot, SIZE)
    (x0, x1, xs, xt), (y0, y1, ys, yt), (z0, z1, zs, zt) = ranges(model)

    def at(value: float, lo: float, hi: float, length: float) -> float:
        return (value - lo) / ((hi - lo) or 1.0) * length

    plot.floor_and_walls([at(t, x0, x1, w) for t in xt], [at(t, y0, y1, d) for t in yt], [at(t, z0, z1, h) for t in zt])
    points = model.get("points") or []
    shown = [points[i] for i in _data.stride(len(points), DRAW_CAP)]
    if len(shown) < len(points):
        plot.notes.append("{} of {} points drawn".format(len(shown), len(points)))
    paints = colors(model)
    grouped = bool(model.get("groups"))
    size_k = 3 + (1 if grouped else 0)
    sizes = [float(p[size_k]) for p in shown if spec.get("size") and len(p) > size_k and p[size_k] is not None]
    s_lo, s_hi = (min(sizes), max(sizes)) if sizes else (0.0, 0.0)
    for p in shown:
        paint = paints[int(p[3]) % len(paints)] if grouped else paints[0]
        px = 6.0
        if sizes and len(p) > size_k and p[size_k] is not None:
            px = 4.0 + 8.0 * ((float(p[size_k]) - s_lo) / ((s_hi - s_lo) or 1.0))
        where = (at(float(p[0]), x0, x1, w), at(float(p[2]), z0, z1, h), at(float(p[1]), y0, y1, d))
        plot.point(where, px, paint, key=float(p[2]), stem=len(shown) <= STEMS)
    big = [max(abs(r[0]), abs(r[1])) >= 1e4 for r in ((x0, x1), (y0, y1), (z0, z1))]
    plot.value_labels([(at(t, z0, z1, h), _tick_text(t, spec, "z", big[2])) for t in zt], spec["z"])
    plot.edge_labels([(at(t, x0, x1, w), _tick_text(t, spec, "x", big[0])) for t in xt], "x", spec["x"])
    plot.edge_labels([(at(t, y0, y1, d), _tick_text(t, spec, "y", big[1])) for t in yt], "z", spec["y"])
    if grouped:
        plot.legend(list(zip(model["groups"], paints)))
    return plot.items()


def doc(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    groups = model.get("groups") or []
    if not groups:
        return {"datasets": [], "refs": {"points": [list(p) for p in model.get("points") or []]}}
    refs: Dict[str, Any] = {}
    for n, _name in enumerate(groups):
        refs["g{}".format(n)] = [[p[0], p[1], p[2]] + p[4:] for p in model.get("points") or [] if p[3] == n]
    return {"datasets": [], "refs": refs}


def option(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, gist: Sequence[str]) -> Dict[str, Any]:
    (x0, x1, xs, _xt), (y0, y1, ys, _yt), (z0, z1, zs, _zt) = ranges(model)
    out = O.base(gist, colors(model))
    axes = {}
    for key, name, lo, hi, step in (("xAxis3D", "x", x0, x1, xs), ("yAxis3D", "y", y0, y1, ys), ("zAxis3D", "z", z0, z1, zs)):
        fmt, unit = _fmt(spec, name)
        axes[key] = P.axis3d("value", spec[name], lo=lo, hi=hi, step=step, fmt_id=F.fmt_id(fmt or "auto", unit))
    out.update(tooltip=O.tooltip("item"), grid3D=P.grid3d(SIZE), **axes)
    groups = model.get("groups") or []
    symbol: Dict[str, Any] = {"symbolSize": 6}
    if spec.get("size"):
        sizes = [float(p[-1]) for p in model.get("points") or [] if p[-1] is not None]
        lo, hi = (min(sizes), max(sizes)) if sizes else (0.0, 1.0)
        out["visualMap"] = O.size_maps(out["color"], max(1, len(groups)), 3, lo, hi, (4, 12))
        symbol = {}
    if groups:
        out["series"] = [dict({"type": "scatter3D", "name": name, "data": {"$doc": "g{}".format(n)},
                               "itemStyle": {"borderColor": "chart.paper", "borderWidth": 0.5}}, **symbol) for n, name in enumerate(groups)]
        out["legend"] = {"show": True, "top": 4, "right": 4, "orient": "vertical", "textStyle": {"fontFamily": "$sans", "fontSize": FR.FONT,
                                                                                                "color": "chart.ink"},
                         "data": list(groups)}
    else:
        out["series"] = [dict({"type": "scatter3D", "name": spec["z"], "data": {"$doc": "points"},
                               "itemStyle": {"borderColor": "chart.paper", "borderWidth": 0.5}}, **symbol)]
    return out


def stats(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    """Over every point with all three values (the full table, not the sample)."""
    names = [spec[a] for a in AXES]
    cols = [table.by_name[n].values for n in names]
    rows = [i for i in range(table.rows) if all(c[i] is not None for c in cols)]
    values = [[float(c[i]) for i in rows] for c in cols]
    out: Dict[str, Any] = {"n": len(rows), "ranges": [[min(v), max(v)] if v else [0.0, 0.0] for v in values], "r": {}}
    for a, b in ((0, 1), (0, 2), (1, 2)):
        found = G.pearson(values[a], values[b])
        if found is not None:
            out["r"]["{}{}".format(AXES[a], AXES[b])] = round(found[0], 4)
    if spec.get("color"):
        color_col = table.by_name[spec["color"]].values
        counts: Dict[str, int] = {}
        for i in rows:
            key = _label(color_col[i])
            counts[key] = counts.get(key, 0) + 1
        out["groups"] = sorted(([k, v] for k, v in counts.items()), key=lambda kv: (-kv[1], kv[0]))[:6]
        out["n_groups"] = len(counts)
    return out


def _strength(r: float) -> str:
    size = abs(r)
    word = "strong" if size >= 0.7 else ("moderate" if size >= 0.4 else ("weak" if size >= 0.15 else "none"))
    return word if word == "none" else "{} {}".format(word, "+" if r > 0 else "−")


def gist(spec: Dict[str, Any], model: Dict[str, Any], stats: Mapping[str, Any]) -> List[str]:
    parts = ["x={}".format(spec["x"]), "y={}".format(spec["y"]), "z={}".format(spec["z"]), "{} points".format(stats.get("n", 0))]
    if spec.get("color"):
        parts.append("color={} ({})".format(spec["color"], stats.get("n_groups", 0)))
    if spec.get("size"):
        parts.append("size={}".format(spec["size"]))
    parts.append(str(spec.get("_source") or ""))
    lines = [G.join(parts)]
    found = stats.get("ranges") or []
    if found:
        lines.append(G.join(["{} {}…{}".format(spec[a], G.num(r[0], spec, a), G.num(r[1], spec, a)) for a, r in zip(AXES, found)]))
    corr = stats.get("r") or {}
    if corr:
        lines.append("r: " + " · ".join("{}~{} {:.2f} ({})".format(spec[k[0]], spec[k[1]], v, _strength(v)).replace("-", "−")
                                        for k, v in corr.items()))
    if stats.get("groups"):
        lines.append("groups: " + " · ".join("{} {}".format(k, v) for k, v in stats["groups"]) +
                     (" …" if stats.get("n_groups", 0) > len(stats["groups"]) else ""))
    for note in model.get("capped") or []:
        lines.append("capped: " + note)
    return lines


def min_box(spec: Dict[str, Any], model: Dict[str, Any]) -> Tuple[float, float]:
    return 360.0, 280.0


CHARTS = (
    ChartType(name="scatter3d", channels=(Channel("x", required=True, types=("quantitative",), doc="a measure along the floor's width"),
                                          Channel("y", required=True, types=("quantitative",), doc="a measure along the floor's depth"),
                                          Channel("z", required=True, types=("quantitative",), doc="a measure, up"),
                                          Channel("color", types=("nominal", "ordinal"), doc="one colour per value (12 at most)"),
                                          Channel("size", types=("quantitative",), doc="the points' size")),
              frame_kind="gl", normalize=normalize, model=model, stats=stats, doc=doc, frame=frame, option=option, draw=draw, gist=gist,
              vegalite=None, echarts=("Scatter3DChart", "Grid3DComponent"), gl=True, min_box=min_box, default_box=(560, 420),
              caps="5,000 points (an even stride), 12 colours",
              doc_line="points by three measures in 3D (echarts-gl); a 2D scatter with colour often reads better",
              example='{"op": "chart", "id": "clusters", "intent": "three features at once", "title": "Latency, load and errors", '
                      '"type": "scatter3d", "rows": [{"lat": 12, "load": 0.3, "err": 1, "zone": "eu"}, {"lat": 30, "load": 0.7, "err": 4, '
                      '"zone": "eu"}, {"lat": 18, "load": 0.5, "err": 2, "zone": "us"}, {"lat": 44, "load": 0.9, "err": 7, "zone": "us"}], '
                      '"x": "lat", "y": "load", "z": "err", "color": "zone"}',
              order=ORDER),
)
