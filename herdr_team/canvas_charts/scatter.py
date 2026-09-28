"""Scatter and bubble charts (canvas v2 phase 3, 2.4): one point per row, two measures against each other.

``x`` and ``y`` are numbers; ``color`` groups the points (up to 12 groups, the
rest "Other"), ``size`` scales them (``bubble`` is a scatter that needs it),
``label`` names them in the gist's outliers. ``trend: "linear"`` draws the
least-squares line. Up to 5,000 points are drawn on the page (an even stride
over the sorted x past that, said in the gist); the gist's correlation, slope
and ranges always use every row.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team.canvas_charts import Channel, ChartType, _data
from herdr_team.canvas_charts import _draw as DR
from herdr_team.canvas_charts import _format as F
from herdr_team.canvas_charts import _frame as FR
from herdr_team.canvas_charts import _gist as G
from herdr_team.canvas_charts import _option as O
from herdr_team.canvas_charts import _scale as S
from herdr_team.canvas_charts import _series as SE
from herdr_team.canvas_charts import _vegalite as VL
from herdr_team.canvas_charts._frame import Frame

ORDER = 50
#: Points the page draws; more are sampled with an even stride over the sorted x.
CAP_POINTS = 5000
#: Points the Python drawing (the model) keeps.
MODEL_POINTS = 400
CAP_COLORS = 12
SIZE_PX = (4.0, 22.0)
DOT_PX = 7.0


def _fmt(spec: Mapping[str, Any], channel: str) -> Tuple[Optional[str], Optional[str]]:
    fmt = (spec.get("format") or {}).get(channel)
    unit = (spec.get("units") or {}).get(channel) or (spec.get("_units") or {}).get(channel)
    return fmt, unit


def _points(spec: Mapping[str, Any], table: _data.Table) -> Dict[str, Any]:
    """Every row with both numbers, sorted by x (then y, then label: row order never matters), its group and size."""
    xs, ys = table.by_name[spec["x"]].values, table.by_name[spec["y"]].values
    colors = table.by_name[spec["color"]].values if spec.get("color") else None
    sizes = table.by_name[spec["size"]].values if spec.get("size") else None
    labels = table.by_name[spec["label"]].values if spec.get("label") else None
    rows = []
    for i in range(table.rows):
        if xs[i] is None or ys[i] is None or (sizes is not None and sizes[i] is None):
            continue
        rows.append((xs[i], ys[i], str(colors[i]) if colors is not None and colors[i] is not None else (SE.NULL if colors is not None else ""),
                     sizes[i] if sizes is not None else None, str(labels[i]) if labels is not None and labels[i] is not None else ""))
    rows.sort(key=lambda r: (r[0], r[1], r[2], r[4], r[3] if r[3] is not None else 0.0))
    groups: List[str] = []
    capped: List[str] = []
    if colors is not None:
        counts: Dict[str, int] = {}
        for r in rows:
            counts[r[2]] = counts.get(r[2], 0) + 1
        groups = sorted(counts, key=lambda g: (-counts[g], g))
        if len(groups) > CAP_COLORS:
            rest = set(groups[CAP_COLORS - 1:])
            groups = groups[:CAP_COLORS - 1] + [SE.OTHER]
            rows = [(r[0], r[1], SE.OTHER if r[2] in rest else r[2], r[3], r[4]) for r in rows]
            capped.append("{} of {} {} values folded into {}".format(len(rest), len(rest) + CAP_COLORS - 1, spec["color"], SE.OTHER))
    return {"rows": rows, "groups": groups, "capped": capped}


def model(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
    found = _points(spec, table)
    rows = found["rows"]
    capped = list(found["capped"])
    if len(rows) > CAP_POINTS:
        capped.append("drawn: {} of {} points (an even stride over x)".format(CAP_POINTS, len(rows)))
    keep = _data.stride(len(rows), MODEL_POINTS)
    groups = found["groups"]
    index = {g: i for i, g in enumerate(groups)}
    sizes = [r[3] for r in rows if r[3] is not None]
    points = [[_data.compact_number(rows[i][0]), _data.compact_number(rows[i][1]), index.get(rows[i][2], 0)]
              + ([_data.compact_number(rows[i][3])] if spec.get("size") else []) for i in keep]
    return {"points": points, "groups": groups, "n": len(rows), "capped": capped,
            "x": [_data.compact_number(min(r[0] for r in rows)), _data.compact_number(max(r[0] for r in rows))] if rows else [0, 1],
            "y": [_data.compact_number(min(r[1] for r in rows)), _data.compact_number(max(r[1] for r in rows))] if rows else [0, 1],
            "size": [_data.compact_number(min(sizes)), _data.compact_number(max(sizes))] if sizes else None,
            "_warnings": [{"code": "chart_capped", "message": n} for n in capped], "_full": found}


def _fit(spec: Mapping[str, Any], model: Mapping[str, Any]) -> Optional[List[float]]:
    return model.get("fit") if spec.get("trend") == "linear" else None


def colors(spec: Mapping[str, Any], model: Mapping[str, Any]) -> List[str]:
    groups = model.get("groups") or []
    wanted = set(spec.get("highlight") or [])
    if wanted and wanted & set(groups):
        return ["chart.highlight" if g in wanted else "chart.dim" for g in groups]
    return [S.cat(i) for i in range(max(1, len(groups)))]


def frame(spec: Dict[str, Any], model: Dict[str, Any], box: Tuple[float, float]) -> Frame:
    fx, ux = _fmt(spec, "x")
    fy, uy = _fmt(spec, "y")
    groups = model.get("groups") or []
    legend = FR.legend_items(groups, colors(spec, model)) if len(groups) > 1 else []
    lo_x, hi_x = model.get("x") or [0, 1]
    lo_y, hi_y = model.get("y") or [0, 1]
    title = F.with_unit(spec["y"], uy)
    return FR.cartesian(box, {"kind": "value", "lo": float(lo_x), "hi": float(hi_x), "zero": False, "fmt": fx, "unit": ux},
                        {"kind": "value", "lo": float(lo_y), "hi": float(hi_y), "zero": False, "fmt": fy, "unit": uy}, legend,
                        spec.get("legend") or "auto", y_title=title)


def _radius(spec: Mapping[str, Any], model: Mapping[str, Any], value: Optional[float]) -> float:
    rng = model.get("size")
    if not spec.get("size") or value is None or not rng:
        return DOT_PX / 2.0
    lo, hi = float(rng[0]), float(rng[1])
    t = 0.5 if hi <= lo else (float(value) - lo) / (hi - lo)
    area = SIZE_PX[0] ** 2 + t * (SIZE_PX[1] ** 2 - SIZE_PX[0] ** 2)
    return math.sqrt(area) / 2.0


def draw(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, slot: Tuple[float, float, float, float]) -> List[Dict[str, Any]]:
    pen = DR.Pen(slot[0], slot[1])
    DR.axes(pen, frame)
    px, py, pw, ph = frame.plot
    x_axis, y_axis = frame.axes["x"], frame.axes["y"]
    paints = colors(spec, model)
    for point in model.get("points") or []:
        x = DR.value_pos(x_axis, point[0], px, pw, False)
        y = DR.value_pos(y_axis, point[1], py, ph, True)
        r = _radius(spec, model, point[3] if len(point) > 3 else None)
        pen.rect(x - r, y - r, 2 * r, 2 * r, paints[int(point[2]) % len(paints)], "chart.paper", 0.75, r=r, op=0.8)
    fit = _fit(spec, model)
    if fit:
        slope, intercept = fit
        lo, hi = float(x_axis["min"]), float(x_axis["max"])
        ends = [(lo, slope * lo + intercept), (hi, slope * hi + intercept)]
        ylo, yhi = float(y_axis["min"]), float(y_axis["max"])
        clipped = [(x, min(yhi, max(ylo, y))) for x, y in ends]
        pen.line([[DR.value_pos(x_axis, x, px, pw, False), DR.value_pos(y_axis, y, py, ph, True)] for x, y in clipped], "chart.ink", 1.5, dash=[6, 4],
                 op=0.8)
    for note in spec.get("annotations") or []:
        if note.get("y") is not None and _data.number(note["y"]) is not None:
            DR.annotation_y(pen, DR.value_pos(y_axis, _data.number(note["y"])[0], py, ph, True), frame, note.get("text") or "")  # type: ignore[index]
        elif note.get("x") is not None and _data.number(note["x"]) is not None:
            DR.annotation_x(pen, DR.value_pos(x_axis, _data.number(note["x"])[0], px, pw, False), frame, note.get("text") or "")  # type: ignore[index]
    DR.legend(pen, frame)
    return pen.items


def doc(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    found = model.get("_full") or _points(spec, table)
    rows = found["rows"]
    keep = _data.stride(len(rows), CAP_POINTS)
    groups = model.get("groups") or [""]
    datasets = []
    for index, group in enumerate(groups):
        source = [[_data.compact_number(rows[i][0]), _data.compact_number(rows[i][1])] + ([_data.compact_number(rows[i][3])] if spec.get("size") else [])
                  + [rows[i][4]] for i in keep if not model.get("groups") or rows[i][2] == group]
        datasets.append({"id": "d{}".format(index), "dimensions": ["x", "y"] + (["size"] if spec.get("size") else []) + ["label"], "source": source})
    return {"datasets": datasets, "refs": {}}


def option(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, gist: Sequence[str]) -> Dict[str, Any]:
    out = O.base(gist, colors(spec, model))
    out.update(grid=O.grid(frame), xAxis=O.axis(frame, "x"), yAxis=O.axis(frame, "y"), legend=O.legend(frame), tooltip=O.tooltip("item"),
               dataset=O.dataset())
    groups = model.get("groups") or [spec["y"]]
    series = []
    for index, group in enumerate(groups):
        item: Dict[str, Any] = {"type": "scatter", "name": group, "datasetIndex": index, "encode": {"x": "x", "y": "y", "tooltip": ["label", "x", "y"]},
                                "symbolSize": DOT_PX, "itemStyle": {"opacity": 0.8, "borderColor": "chart.paper", "borderWidth": 0.75}}
        series.append(item)
    fit = _fit(spec, model)
    if fit and series:
        slope, intercept = fit
        lo, hi = float(frame.axes["x"]["min"]), float(frame.axes["x"]["max"])
        series[0]["markLine"] = {"silent": True, "symbol": "none", "lineStyle": {"type": "dashed", "color": "chart.ink", "opacity": 0.8},
                                 "data": [[{"coord": [_data.compact_number(lo), _data.compact_number(slope * lo + intercept)]},
                                           {"coord": [_data.compact_number(hi), _data.compact_number(slope * hi + intercept)]}]]}
    out["series"] = series
    if spec.get("size") and model.get("size"):
        lo, hi = model["size"]
        out["visualMap"] = O.size_maps(out["color"], len(series), 2, lo, hi, SIZE_PX)
    return out


def stats(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    found = model.get("_full") or _points(spec, table)
    rows = found["rows"]
    xs, ys = [r[0] for r in rows], [r[1] for r in rows]
    out: Dict[str, Any] = {"n": len(rows)}
    fitted = G.pearson(xs, ys)
    if fitted is not None:
        r, slope, intercept = fitted
        out.update(r=r, slope=slope, intercept=intercept)
        residuals = [y - (slope * x + intercept) for x, y in zip(xs, ys)]
        _lo, _hi, idx = G.outliers(residuals)
        idx.sort(key=lambda i: (-abs(residuals[i]), i))
    else:
        _lo, _hi, idx = G.outliers(ys)
        idx.sort(key=lambda i: (-abs(ys[i]), i))
    out["outliers"] = [[rows[i][4] or "", rows[i][0], rows[i][1]] for i in idx[:3]]
    if rows:
        out["x"] = [min(xs), max(xs)]
        out["y"] = [min(ys), max(ys)]
    out["groups"] = len(found.get("groups") or [])
    return out


def normalize(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
    return spec


def _with_fit(model_fn: Any) -> Any:
    def wrapped(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
        out = model_fn(spec, table)
        found = out.get("_full") or {}
        rows = found.get("rows") or []
        fitted = G.pearson([r[0] for r in rows], [r[1] for r in rows])
        if fitted is not None:
            out["fit"] = [_data.compact_number(fitted[1]), _data.compact_number(fitted[2])]
        return out
    return wrapped


def gist(spec: Dict[str, Any], model: Dict[str, Any], stats: Mapping[str, Any]) -> List[str]:
    parts = ["x={}".format(spec["x"]), "y={}".format(spec["y"]), "{} point{}".format(stats.get("n", 0), "" if stats.get("n") == 1 else "s")]
    if spec.get("color"):
        parts.append("color={} ({})".format(spec["color"], stats.get("groups", 0)))
    if spec.get("size"):
        parts.append("size={}".format(spec["size"]))
    parts.append(str(spec.get("_source") or ""))
    lines = [G.join(parts)]
    if stats.get("r") is not None:
        r = float(stats["r"])
        strength = "strong" if abs(r) >= 0.7 else "moderate" if abs(r) >= 0.4 else "weak" if abs(r) >= 0.1 else "no"
        lines.append("r = {:.2f} ({} {}) · slope {} {} per {}".format(r, strength, "positive" if r > 0 else "negative" if r < 0 else "",
                                                                  F.signed(float(stats["slope"]), None, None), spec["y"], spec["x"]).replace("  ", " "))
    if stats.get("x"):
        fx, ux = _fmt(spec, "x")
        fy, uy = _fmt(spec, "y")
        lines.append("x {}…{} · y {}…{}".format(F.fmt_number(stats["x"][0], fx, ux), F.fmt_number(stats["x"][1], fx, ux),
                                                F.fmt_number(stats["y"][0], fy, uy), F.fmt_number(stats["y"][1], fy, uy)))
    if stats.get("outliers"):
        fx, ux = _fmt(spec, "x")
        fy, uy = _fmt(spec, "y")
        lines.append("outliers: " + " · ".join("{}({}, {})".format(label + " " if label else "", F.fmt_number(x, fx, ux), F.fmt_number(y, fy, uy))
                                               for label, x, y in stats["outliers"]))
    for note in model.get("capped") or []:
        lines.append("capped: " + note)
    return lines


def vegalite(spec: Dict[str, Any], model: Dict[str, Any]) -> Dict[str, Any]:
    groups = model.get("groups") or []
    rows = [{"x": p[0], "y": p[1], **({"group": groups[int(p[2])]} if groups else {}), **({"size": p[3]} if len(p) > 3 else {})}
            for p in model.get("points") or []]
    enc: Dict[str, Any] = {"x": {"field": "x", "type": "quantitative", "title": spec["x"], "scale": {"zero": False}},
                           "y": {"field": "y", "type": "quantitative", "title": spec["y"], "scale": {"zero": False}}}
    if groups:
        enc["color"] = {"field": "group", "type": "nominal", "title": spec.get("color")}
    if spec.get("size"):
        enc["size"] = {"field": "size", "type": "quantitative", "title": spec["size"]}
    return VL.compat({"type": "point", "filled": True}, rows, enc, {"width": "container"})


def min_box(spec: Dict[str, Any], model: Dict[str, Any]) -> Tuple[float, float]:
    return 300.0, 220.0


CHARTS = (
    ChartType(name="scatter", aliases=("bubble",),
              channels=(Channel("x", required=True, types=("quantitative",), doc="a measure along the bottom"),
                        Channel("y", required=True, types=("quantitative",), doc="a measure up the side"),
                        Channel("color", types=("nominal", "ordinal"), doc="groups of points (up to 12)"),
                        Channel("size", types=("quantitative",), doc="a third measure as the point's area (bubble)"),
                        Channel("label", doc="names points in the gist (outliers)")),
              options=("trend",), frame_kind="cartesian", normalize=normalize, model=_with_fit(model), stats=stats, doc=doc, frame=frame,
              option=option, draw=draw, gist=gist, vegalite=vegalite,
              echarts=("ScatterChart", "GridComponent", "DatasetComponent", "LegendComponent", "TooltipComponent", "MarkLineComponent",
                       "VisualMapComponent", "AriaComponent"),
              min_box=min_box, default_box=(520, 380), caps="5,000 points (even stride), 12 colours", doc_line="two measures against each other, one point per row (bubble: sized)",
              example='{"op": "chart", "id": "spend", "intent": "does ad spend bring signups", "title": "Spend vs signups", "type": "scatter", '
                      '"rows": [{"campaign": "Brand", "spend": 9800, "signups": 410}, {"campaign": "Search", "spend": 4200, "signups": 260}, '
                      '{"campaign": "Social", "spend": 3100, "signups": 150}, {"campaign": "Video", "spend": 5600, "signups": 190}, '
                      '{"campaign": "Email", "spend": 800, "signups": 120}, {"campaign": "Display", "spend": 2500, "signups": 60}], '
                      '"x": "spend", "y": "signups", "label": "campaign", "trend": "linear", "units": {"x": "$"}}',
              order=50),
)
