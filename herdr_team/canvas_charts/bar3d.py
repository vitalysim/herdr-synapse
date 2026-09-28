"""3D bars (canvas v2 phase 4, 3.11): a measure over a grid of two categories, one bar per cell, through echarts-gl.

``x`` and ``y`` are the two categories (nominal, ordinal, or a small set of
times or numbers such as hours), ``z`` the measure (summed when rows repeat,
or ``aggregate``), coloured by value on the ``chart.seq`` ramp. A nominal
``x`` goes by total (``sort``, as a bar's axis); the depth axis ``y`` takes no
``sort``, so a nominal one of weekday or month names keeps the calendar's order
(Mon…Sun, Jan…Dec) and any other goes by total. At most 40 × 40
cells: the rest of a long category folds away (the last 40 periods of a time
axis, else the 40 largest), reported as ``chart_capped``.

The page draws it with echarts-gl (``grid3D``, orthographic, the iso angles);
the agent's picture is the same box drawn in Python (``canvas_scene3d._plot3d``).
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
from herdr_team.canvas_charts import _series as SE
from herdr_team.canvas_charts._frame import Frame
from herdr_team.canvas_scene3d import _plot3d as P

ORDER = 200
CAP = 40
#: The value axis's height against the floor's longer side.
HEIGHT = 0.6
ANY = ("nominal", "ordinal", "temporal", "quantitative")


def normalize(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
    return spec


def _cap(cats: List[Any], order: str, totals: Mapping[Any, float], field: str, capped: List[str]) -> List[Any]:
    if len(cats) <= CAP:
        return cats
    if order == "time":
        capped.append("kept the last {} of {} {} values".format(CAP, len(cats), field))
        return cats[-CAP:]
    ranked = sorted(range(len(cats)), key=lambda i: (-abs(totals.get(cats[i]) or 0.0), i))
    kept = set(ranked[:CAP])
    capped.append("kept the {} largest of {} {} values".format(CAP, len(cats), field))
    return [c for i, c in enumerate(cats) if i in kept]


def _categories(spec: Mapping[str, Any], table: _data.Table, field: str, totals: Mapping[Any, float]) -> Tuple[List[Any], str]:
    """``_series.categories``, with ties between equal totals broken by name (the same order for any row order)."""
    cats, order = SE.categories(spec, table, field, totals, False)
    if order == "sort" and spec.get("sort") in (None, "y", "-y"):
        sign = 1 if spec.get("sort") == "y" else -1
        cats = sorted(cats, key=lambda v: (sign * (totals.get(v) or 0.0), v is None, str(v)))
    return cats, order


#: Weekday and month names (English, full or three letters): a depth axis made only of them keeps the calendar's order.
_CALENDARS = (("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday"),
              ("january", "february", "march", "april", "may", "june", "july", "august", "september", "october", "november",
               "december"))


def _calendar(cats: Sequence[Any]) -> Optional[List[Any]]:
    """``cats`` in calendar order when every one names a weekday, or every one a month (QA phase34 L7: a 3D bar over
    weekdays ranked its depth axis Mon, Fri, Thu, Wed, Tue by total); None otherwise."""
    for names in _CALENDARS:
        index: Dict[str, int] = {}
        for i, name in enumerate(names):
            index[name] = i
            index[name[:3]] = i
        keys = []
        for value in cats:
            key = index.get(str(value).strip().lower().rstrip(".")) if isinstance(value, str) else None
            if key is None:
                break
            keys.append(key)
        else:
            if cats and len(set(keys)) == len(keys):
                return [c for _k, c in sorted(zip(keys, cats), key=lambda pair: pair[0])]
    return None


def model(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
    x, y, z = spec["x"], spec["y"], spec.get("z")
    how = SE.how_of(spec, table, [x, y]) if z else "count"
    cells = _data.aggregate(table, list(range(table.rows)), [x, y], z, how if how != "none" else "last")
    totals_x: Dict[Any, float] = {}
    totals_y: Dict[Any, float] = {}
    for (kx, ky), value in cells.items():
        totals_x[kx] = totals_x.get(kx, 0.0) + (value or 0.0)
        totals_y[ky] = totals_y.get(ky, 0.0) + (value or 0.0)
    capped: List[str] = []
    xs, x_order = _categories(spec, table, x, totals_x)
    ys, y_order = _categories(dict(spec, sort=None), table, y, totals_y)
    if y_order == "sort":
        ys = _calendar(ys) or ys
    xs = _cap(xs, x_order, totals_x, x, capped)
    ys = _cap(ys, y_order, totals_y, y, capped)
    xi = {k: i for i, k in enumerate(xs)}
    yi = {k: i for i, k in enumerate(ys)}
    out_cells = []
    for (kx, ky), value in cells.items():
        if kx in xi and ky in yi and value is not None:
            out_cells.append([xi[kx], yi[ky], _data.compact_number(value)])
    out_cells.sort(key=lambda c: (c[1], c[0]))
    x_col, y_col = table.by_name[x], table.by_name[y]
    return {"xs": [SE._cat_label(k, x_col, spec, "x") for k in xs], "ys": [SE._cat_label(k, y_col, spec, "y") for k in ys],
            "cells": out_cells, "how": how, "capped": capped,
            "_warnings": [{"code": "chart_capped", "message": note} for note in capped]}


def extent(model: Mapping[str, Any]) -> Tuple[float, float]:
    return S.value_range([c[2] for c in model.get("cells") or []], True)


def size(model: Mapping[str, Any]) -> Tuple[float, float, float]:
    nx, ny = max(1, len(model.get("xs") or [])), max(1, len(model.get("ys") or []))
    w, d = 1.0, min(2.5, max(0.4, ny / float(nx)))
    return w, HEIGHT * max(w, d), d


def _fmt(spec: Mapping[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    fmt = (spec.get("format") or {}).get("z")
    unit = (spec.get("units") or {}).get("z") or (spec.get("_units") or {}).get("z")
    return fmt, unit


def ticks(spec: Mapping[str, Any], model: Mapping[str, Any]) -> Tuple[float, float, float, List[float]]:
    lo, hi = extent(model)
    return S.nice_ticks(lo, hi, 5)


def frame(spec: Dict[str, Any], model: Dict[str, Any], box: Tuple[float, float]) -> Frame:
    lo, hi, step, _ticks = ticks(spec, model)
    return FR.none(box, {"z": {"min": lo, "max": hi, "step": step}})


def z_title(spec: Mapping[str, Any], model: Mapping[str, Any]) -> str:
    return SE.measure_label(spec, spec.get("z"), str(model.get("how") or "none"))


def draw(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, slot: Tuple[float, float, float, float]) -> List[Dict[str, Any]]:
    w, h, d = size(model)
    plot = P.Plot3D(slot, (w, h, d))
    xs, ys = model.get("xs") or [], model.get("ys") or []
    nx, ny = max(1, len(xs)), max(1, len(ys))
    gmin, gmax, step, tick_values = ticks(spec, model)
    span = (gmax - gmin) or 1.0

    def height(value: float) -> float:
        return (value - gmin) / span * h

    plot.floor_and_walls([w * i / nx for i in range(nx + 1)], [d * j / ny for j in range(ny + 1)], [height(t) for t in tick_values])
    lo, hi = extent(model)
    base = height(0.0)
    cw, cd = w / nx, d / ny
    for i, j, value in model.get("cells") or []:
        if value is None:
            continue
        t = 0.0 if hi == lo else (float(value) - lo) / (hi - lo)
        k = P.seq_index(t)
        top = height(float(value))
        x0, z0 = cw * (i + 0.15), cd * (j + 0.15)
        box = (x0, min(base, top), z0, x0 + cw * 0.7, max(base, top) if abs(top - base) > 1e-6 else base + 1e-4, z0 + cd * 0.7)
        plot.bar(box, P.seq_ref(k), P.seq_ref(k + 1), P.seq_ref(k + 2), key=abs(float(value)))
    fmt, unit = _fmt(spec)
    plot.value_labels([(height(t), F.fmt_number(t, fmt or ("compact" if max(abs(gmin), abs(gmax)) >= 1e4 else None), unit))
                       for t in tick_values], z_title(spec, model))
    plot.edge_labels([(cw * (i + 0.5), str(label)) for i, label in enumerate(xs)], "x", spec["x"])
    plot.edge_labels([(cd * (j + 0.5), str(label)) for j, label in enumerate(ys)], "z", spec["y"])
    return plot.items()


def doc(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    return {"datasets": [], "refs": {"cells": [list(c) for c in model.get("cells") or []]}}


def option(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, gist: Sequence[str]) -> Dict[str, Any]:
    lo, hi = extent(model)
    gmin, gmax, step, _t = ticks(spec, model)
    fmt, unit = _fmt(spec)
    out = O.base(gist, [P.seq_ref(4)])
    out.update(tooltip=O.tooltip("item"), grid3D=P.grid3d(size(model)), visualMap=P.visual_map(lo, hi, 2),
               xAxis3D=P.axis3d("category", spec["x"], model.get("xs")), yAxis3D=P.axis3d("category", spec["y"], model.get("ys")),
               zAxis3D=P.axis3d("value", z_title(spec, model), lo=gmin, hi=gmax, step=step, fmt_id=F.fmt_id(fmt or "auto", unit)),
               series=[{"type": "bar3D", "name": z_title(spec, model), "data": {"$doc": "cells"}, "shading": spec.get("shading") or "lambert",
                        "bevelSize": 0, "itemStyle": {"opacity": 1}, "emphasis": {"label": {"show": False}}}])
    return out


def stats(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    xs, ys = model.get("xs") or [], model.get("ys") or []
    cells = [c for c in model.get("cells") or [] if c[2] is not None]
    out: Dict[str, Any] = {"n": len(cells), "nx": len(xs), "ny": len(ys)}
    if not cells:
        return out
    found = G.extremes([((xs[c[0]], ys[c[1]]), float(c[2])) for c in cells])
    if found:
        out["max"] = [found["max"][0][0], found["max"][0][1], found["max"][1]]
        out["min"] = [found["min"][0][0], found["min"][0][1], found["min"][1]]
    for axis, index, names in (("x", 0, xs), ("y", 1, ys)):
        sums: Dict[int, List[float]] = {}
        for c in cells:
            sums.setdefault(c[index], []).append(float(c[2]))
        means = [(names[k], math.fsum(v) / len(v)) for k, v in sorted(sums.items())]
        best = G.top_k(means, 1)
        if best:
            out["best_" + axis] = [best[0][0], best[0][1]]
    out["total"] = math.fsum(float(c[2]) for c in cells)
    return out


def gist(spec: Dict[str, Any], model: Dict[str, Any], stats: Mapping[str, Any]) -> List[str]:
    xs, ys = model.get("xs") or [], model.get("ys") or []
    lines = [G.join(["x={} ({})".format(spec["x"], len(xs)), "y={} ({})".format(spec["y"], len(ys)), "z={}".format(z_title(spec, model)),
                     "{} bars".format(stats.get("n", 0)), str(spec.get("_source") or "")])]
    parts = []
    for key in ("max", "min"):
        found = stats.get(key)
        if found:
            parts.append("{} {} × {} {}".format(key, found[0], found[1], G.num(found[2], spec, "z")))
    if parts:
        lines.append(G.join(parts))
    means = []
    if stats.get("best_x"):
        means.append("{} {} (mean {})".format(spec["x"], stats["best_x"][0], G.num(stats["best_x"][1], spec, "z")))
    if stats.get("best_y"):
        means.append("{} {} (mean {})".format(spec["y"], stats["best_y"][0], G.num(stats["best_y"][1], spec, "z")))
    if means:
        lines.append("highest: " + " · ".join(means))
    for note in model.get("capped") or []:
        lines.append("capped: " + note)
    return lines


def min_box(spec: Dict[str, Any], model: Dict[str, Any]) -> Tuple[float, float]:
    count = max(len(model.get("xs") or []), len(model.get("ys") or []))
    side = max(320.0, 160.0 + count * 8.0)
    return side, side * 0.75


CHARTS = (
    ChartType(name="bar3d", channels=(Channel("x", required=True, types=ANY, doc="the categories along the floor's width"),
                                      Channel("y", required=True, types=ANY, doc="the categories along the floor's depth"),
                                      Channel("z", required=True, types=("quantitative",), aggregate=True,
                                              doc="the measure, the bars' height (summed when rows repeat)")),
              options=("shading",), frame_kind="gl", normalize=normalize, model=model, stats=stats, doc=doc, frame=frame, option=option,
              draw=draw, gist=gist, vegalite=None, echarts=("Bar3DChart", "Grid3DComponent", "VisualMapComponent"), gl=True,
              min_box=min_box, default_box=(560, 420), caps="40 × 40 cells (the last 40 periods, else the 40 largest per axis)",
              doc_line="a measure over two categories as a grid of 3D bars (echarts-gl); a heatmap reads exact values better",
              example='{"op": "chart", "id": "load3d", "intent": "load by hour and weekday", "title": "Requests by hour × weekday", '
                      '"type": "bar3d", "rows": [{"hour": "09", "day": "Mon", "requests": 120}, {"hour": "09", "day": "Tue", "requests": 150}, '
                      '{"hour": "12", "day": "Mon", "requests": 310}, {"hour": "12", "day": "Tue", "requests": 280}], '
                      '"x": "hour", "y": "day", "z": "requests"}',
              order=ORDER),
)
