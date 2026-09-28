"""Heatmaps (canvas v2 phase 3, 2.4): a measure over two categorical axes, as a grid of coloured cells.

``x`` runs along the bottom and ``y`` down the side (categories, ordered
categories or dates); ``value`` colours each cell (summed when rows repeat) on
the sequential ramp, or the diverging one around zero (``palette:
"diverging"``). At most 60 x 60 cells: the categories past that fold into
"Other" (dates keep the latest). A colour ramp under the grid gives the scale.
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

ORDER = 60
CAP = 60
RAMP_H = 30.0
#: The ramp bar's height, and the gap between it and the values at its ends.
RAMP_BAR = 10.0
RAMP_GAP = 6.0
#: Cells get their value written in them when there are at most this many and the value fits.
AUTO_LABELS = 60


def _axis_values(spec: Mapping[str, Any], table: _data.Table, field: str, channel: str,
                 totals: Mapping[Any, float]) -> Tuple[List[Any], List[str], List[str]]:
    """``(values, labels, capped notes)`` of one axis: dates in time order (gaps filled), ordered categories as given,
    the others by total; past ``CAP`` the latest dates are kept, or the rest fold into "Other"."""
    ordered, order = SE.categories(spec, table, field, totals, True)
    capped: List[str] = []
    if len(ordered) > CAP:
        if order == "time":
            capped.append("kept the last {} of {} {} values".format(CAP, len(ordered), field))
            ordered = ordered[-CAP:]
        else:
            kept = set(sorted(ordered, key=lambda v: (-abs(totals.get(v) or 0.0), SE._tie(v)))[:CAP - 1])
            capped.append("{} of {} {} values folded into {}".format(len(ordered) - len(kept), len(ordered), field, SE.OTHER))
            ordered = [v for v in ordered if v in kept] + [SE.OTHER]
    column = table.by_name[field]
    labels = [SE.OTHER if index == len(ordered) - 1 and capped and order != "time" else SE._cat_label(v, column, spec, channel)
              for index, v in enumerate(ordered)]
    return ordered, labels, capped


def model(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
    x, y, value = spec["x"], spec["y"], spec.get("value")
    how = SE.how_of(spec, table, [x, y]) if value else "count"
    cells = _data.aggregate(table, list(range(table.rows)), [x, y], value, how if how != "none" else "last")
    x_parts: Dict[Any, List[float]] = {}
    y_parts: Dict[Any, List[float]] = {}
    for (kx, ky), v in cells.items():
        x_parts.setdefault(kx, []).append(v or 0.0)
        y_parts.setdefault(ky, []).append(v or 0.0)
    xs, x_labels, x_capped = _axis_values(spec, table, x, "x", {k: math.fsum(v) for k, v in x_parts.items()})
    ys, y_labels, y_capped = _axis_values(spec, table, y, "y", {k: math.fsum(v) for k, v in y_parts.items()})
    capped = x_capped + y_capped
    x_index = {v: i for i, v in enumerate(xs)}
    y_index = {v: i for i, v in enumerate(ys)}
    grid: List[List[Optional[float]]] = [[None] * len(xs) for _ in ys]
    parts: Dict[Tuple[int, int], List[float]] = {}
    additive = how in ("sum", "count", "none")
    for (kx, ky), v in cells.items():
        if v is None:
            continue
        i = x_index.get(kx, x_index.get(SE.OTHER))
        j = y_index.get(ky, y_index.get(SE.OTHER))
        if i is None or j is None:
            continue
        parts.setdefault((j, i), []).append(v)
    for (j, i), values in parts.items():
        grid[j][i] = _data.compact_number(math.fsum(values) if additive else _data.reduce(values, how))
    present = [v for row in grid for v in row if v is not None]
    return {"xs": x_labels, "ys": y_labels, "cells": grid, "how": how, "capped": capped,
            "min": min(present) if present else 0, "max": max(present) if present else 1,
            "_warnings": [{"code": "chart_capped", "message": c} for c in capped]}


def _diverging(spec: Mapping[str, Any]) -> bool:
    return spec.get("palette") == "diverging"


def paint(spec: Mapping[str, Any], model: Mapping[str, Any], value: float) -> str:
    lo, hi = float(model.get("min") or 0.0), float(model.get("max") or 1.0)
    if _diverging(spec):
        bound = max(abs(lo), abs(hi)) or 1.0
        return S.div(value / bound)
    return S.seq(0.0 if hi <= lo else (value - lo) / (hi - lo))


def _fmt(spec: Mapping[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    return (spec.get("format") or {}).get("value"), (spec.get("units") or {}).get("value") or (spec.get("_units") or {}).get("value")


def frame(spec: Dict[str, Any], model: Dict[str, Any], box: Tuple[float, float]) -> Frame:
    w, h = float(box[0]), float(box[1])
    base = FR.cartesian((w, max(60.0, h - RAMP_H)), {"kind": "category", "labels": list(model.get("xs") or []), "thin": False},
                        {"kind": "category", "labels": list(model.get("ys") or [])}, (), "none", horizontal=False)
    px, py, pw, ph = base.plot
    # The ramp's row: its low end's value, the bar, its high end's value, as ECharts lays a horizontal visualMap's `text`
    # out (so the page's picture and this one agree, QA phase34 low).
    lo_text, hi_text = _end_texts(spec, model)
    lo_w, hi_w = FR.text_w(lo_text), FR.text_w(hi_text)
    bar_w = max(24.0, min(220.0, pw - lo_w - hi_w - 2 * RAMP_GAP))
    ramp = [px + lo_w + RAMP_GAP, h - RAMP_H / 2.0 - RAMP_BAR / 2.0, bar_w, RAMP_BAR]
    return Frame(box=(w, h), plot=base.plot, axes=base.axes, legend=None, font=base.font, notes=base.notes, legible=base.legible,
                 extra={"ramp": ramp, "ramp_text": [lo_text, hi_text]})


def _ends(spec: Mapping[str, Any], model: Mapping[str, Any]) -> Tuple[float, float]:
    """The values at the ramp's two ends (symmetric around zero when the palette diverges)."""
    lo, hi = float(model.get("min") or 0), float(model.get("max") or 0)
    if _diverging(spec):
        bound = max(abs(lo), abs(hi))
        lo, hi = -bound, bound
    return lo, hi


def _end_texts(spec: Mapping[str, Any], model: Mapping[str, Any]) -> Tuple[str, str]:
    fmt, unit = _fmt(spec)
    return tuple(F.fmt_number(v, fmt or ("compact" if abs(v) >= 1e4 else None), unit) for v in _ends(spec, model))  # type: ignore[return-value]


def _labels_on(spec: Mapping[str, Any], model: Mapping[str, Any], cell_w: float, cell_h: float) -> bool:
    mode = spec.get("labels") or "auto"
    count = sum(1 for row in model.get("cells") or [] for v in row if v is not None)
    if mode == "none":
        return False
    return mode == "values" or (count <= AUTO_LABELS and cell_h >= FR.LH + 2 and cell_w >= 30)


def draw(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, slot: Tuple[float, float, float, float]) -> List[Dict[str, Any]]:
    pen = DR.Pen(slot[0], slot[1])
    px, py, pw, ph = frame.plot
    xs, ys = model.get("xs") or [], model.get("ys") or []
    cw, chh = pw / max(1, len(xs)), ph / max(1, len(ys))
    fmt, unit = _fmt(spec)
    labels = _labels_on(spec, model, cw, chh)
    for j, row in enumerate(model.get("cells") or []):
        for i, value in enumerate(row):
            if value is None:
                continue
            ref = paint(spec, model, float(value))
            pen.rect(px + i * cw + 0.5, py + j * chh + 0.5, max(0.5, cw - 1), max(0.5, chh - 1), ref, r=1 if cw > 8 and chh > 8 else 0)
            if labels:
                text = F.fmt_number(value, fmt or ("compact" if abs(value) >= 1e4 else None), unit)
                if FR.text_w(text) <= cw - 4:
                    pen.text(text, px + (i + 0.5) * cw, py + (j + 0.5) * chh, "middle", S.on(ref))
    DR.axes(pen, frame)
    rx, ry, rw, rh = (float(v) for v in frame.extra["ramp"])
    refs = S.div_refs() if _diverging(spec) else S.seq_refs()
    step = rw / len(refs)
    for index, ref in enumerate(refs):
        pen.rect(rx + index * step, ry, step + 0.25, rh, ref)
    lo_text, hi_text = frame.extra.get("ramp_text") or _end_texts(spec, model)
    pen.text(lo_text, rx - RAMP_GAP, ry + rh / 2.0, "end")
    pen.text(hi_text, rx + rw + RAMP_GAP, ry + rh / 2.0, "start")
    return pen.items


def doc(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    xs, ys = model.get("xs") or [], model.get("ys") or []
    source = [[xs[i], ys[j], v] for j, row in enumerate(model.get("cells") or []) for i, v in enumerate(row) if v is not None]
    return {"datasets": [{"id": "d0", "dimensions": ["x", "y", "value"], "source": source}], "refs": {}}


def option(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, gist: Sequence[str]) -> Dict[str, Any]:
    out = O.base(gist)
    x_axis, y_axis = O.axis(frame, "x"), O.axis(frame, "y")
    x_axis["data"] = list(model.get("xs") or [])
    y_axis["data"] = list(model.get("ys") or [])
    for axis in (x_axis, y_axis):
        axis["splitArea"] = {"show": False}
        axis["axisTick"]["alignWithLabel"] = True
    out.update(grid=O.grid(frame), xAxis=x_axis, yAxis=y_axis, tooltip=O.tooltip("item"), dataset=O.dataset(), legend={"show": False})
    lo, hi = float(model.get("min") or 0), float(model.get("max") or 1)
    if _diverging(spec):
        bound = max(abs(lo), abs(hi)) or 1.0
        lo, hi = -bound, bound
    rx, ry, rw, rh = (float(v) for v in frame.extra["ramp"])
    fmt, unit = _fmt(spec)
    lo_text, hi_text = frame.extra.get("ramp_text") or _end_texts(spec, model)
    # The end values sit either side of the bar (`text` is [high, low]); the component is placed by its whole box, so it
    # starts at the low value's left edge, and a line height of the bar's own keeps that box's top on the bar's.
    out["visualMap"] = {"type": "continuous", "show": True, "min": _data.compact_number(lo), "max": _data.compact_number(hi if hi > lo else lo + 1),
                        "dimension": 2, "calculable": False, "orient": "horizontal", "left": round(rx - RAMP_GAP - FR.text_w(lo_text), 2),
                        "top": round(ry, 2), "padding": 0, "itemWidth": round(rh, 2), "itemHeight": round(rw, 2),
                        "text": [hi_text, lo_text], "textGap": RAMP_GAP,
                        "inRange": {"color": S.div_refs() if _diverging(spec) else S.seq_refs()},
                        "textStyle": {"fontFamily": "$sans", "fontSize": FR.FONT, "color": "chart.muted", "lineHeight": round(rh, 2)},
                        "formatter": {"$fmt": F.fmt_id(fmt or "auto", unit)}}
    series: Dict[str, Any] = {"type": "heatmap", "datasetIndex": 0, "encode": {"x": "x", "y": "y", "value": "value"},
                              "itemStyle": {"borderColor": "chart.paper", "borderWidth": 1, "borderRadius": 1}}
    px, py, pw, ph = frame.plot
    if _labels_on(spec, model, pw / max(1, len(model.get("xs") or [])), ph / max(1, len(model.get("ys") or []))):
        series["label"] = {"show": True, "fontFamily": "$sans", "fontSize": FR.FONT, "formatter": {"$fmt": F.fmt_id(fmt or "auto", unit)}}
    out["series"] = [series]
    return out


def stats(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    xs, ys, grid = model.get("xs") or [], model.get("ys") or [], model.get("cells") or []
    cells = [(xs[i], ys[j], v) for j, row in enumerate(grid) for i, v in enumerate(row) if v is not None]
    out: Dict[str, Any] = {"cells": len(cells), "x": len(xs), "y": len(ys)}
    if cells:
        hi = max(range(len(cells)), key=lambda k: (cells[k][2], -k))
        lo = min(range(len(cells)), key=lambda k: (cells[k][2], k))
        out["max"], out["min"] = list(cells[hi]), list(cells[lo])
        row_means = [(ys[j], math.fsum(v for v in row if v is not None) / max(1, sum(1 for v in row if v is not None))) for j, row in enumerate(grid)
                     if any(v is not None for v in row)]
        col_means = [(xs[i], math.fsum(grid[j][i] for j in range(len(grid)) if grid[j][i] is not None) /
                      max(1, sum(1 for j in range(len(grid)) if grid[j][i] is not None))) for i in range(len(xs))
                     if any(grid[j][i] is not None for j in range(len(grid)))]
        out["row"] = list(max(row_means, key=lambda kv: (kv[1], kv[0]))) if row_means else None
        out["col"] = list(max(col_means, key=lambda kv: (kv[1], kv[0]))) if col_means else None
    return out


def gist(spec: Dict[str, Any], model: Dict[str, Any], stats: Mapping[str, Any]) -> List[str]:
    measure = SE.measure_label(spec, spec.get("value"), str(model.get("how") or "none"))
    lines = [G.join(["x={} ({})".format(spec["x"], stats.get("x", 0)), "y={} ({})".format(spec["y"], stats.get("y", 0)), "value={}".format(measure),
                     "{} cells".format(stats.get("cells", 0)), str(spec.get("_source") or "")])]
    if stats.get("max"):
        mx, mn = stats["max"], stats["min"]
        lines.append("max {} × {} {} · min {} × {} {}".format(mx[0], mx[1], G.num(mx[2], dict(spec), "value"), mn[0], mn[1],
                                                           G.num(mn[2], dict(spec), "value")))
    if stats.get("row") and stats.get("col"):
        means = (float(stats["row"][1]), float(stats["col"][1]))
        own = (spec.get("format") or {}).get("value") if isinstance(spec.get("format"), dict) else None
        if own is None and all(abs(m) < 1e4 for m in means):
            # The two means at one precision (QA phase34 L4): one decimal unless both are whole.
            whole = all(abs(m - round(m)) < 0.05 for m in means)
            shown = [G.num(m, dict(spec, format={"value": "0" if whole else "0.0"}), "value") for m in means]
        else:
            shown = [G.num(m, dict(spec), "value") for m in means]
        lines.append("highest mean: {} {} (row) · {} {} (column)".format(stats["row"][0], shown[0], stats["col"][0], shown[1]))
    for note in model.get("capped") or []:
        lines.append("capped: " + note)
    return lines


def vegalite(spec: Dict[str, Any], model: Dict[str, Any]) -> Dict[str, Any]:
    xs, ys = model.get("xs") or [], model.get("ys") or []
    rows = [{"x": xs[i], "y": ys[j], "value": v} for j, row in enumerate(model.get("cells") or []) for i, v in enumerate(row) if v is not None]
    scheme = "redblue" if _diverging(spec) else "blues"
    return VL.compat("rect", rows, {"x": {"field": "x", "type": "ordinal", "sort": None, "title": spec["x"]},
                                    "y": {"field": "y", "type": "ordinal", "sort": None, "title": spec["y"]},
                                    "color": {"field": "value", "type": "quantitative", "title": spec.get("value"), "scale": {"scheme": scheme}}},
                     {"width": "container"})


def min_box(spec: Dict[str, Any], model: Dict[str, Any]) -> Tuple[float, float]:
    nx, ny = len(model.get("xs") or []), len(model.get("ys") or [])
    widest = max((FR.text_w(t) for t in model.get("ys") or []), default=40.0)
    return max(280.0, min(widest, 180.0) + 30 + FR.min_plot_for(nx)), max(200.0, ny * (FR.LH / 1.0) * 0.6 + 70 + RAMP_H)


CHARTS = (
    ChartType(name="heatmap",
              channels=(Channel("x", required=True, types=("nominal", "ordinal", "temporal"), doc="categories along the bottom"),
                        Channel("y", required=True, types=("nominal", "ordinal", "temporal"), doc="categories down the side"),
                        Channel("value", required=True, types=("quantitative",), aggregate=True, doc="colours each cell (summed when rows repeat)")),
              options=("palette",), frame_kind="cartesian", model=model, stats=stats, doc=doc, frame=frame, option=option, draw=draw,
              gist=gist, vegalite=vegalite,
              echarts=("HeatmapChart", "VisualMapComponent", "GridComponent", "DatasetComponent", "TooltipComponent", "AriaComponent"),
              min_box=min_box, default_box=(600, 380), caps="60 × 60 cells", doc_line="a measure over two categorical axes as coloured cells",
              example='{"op": "chart", "id": "load", "intent": "when is the API busiest", "title": "Requests by weekday and hour", '
                      '"type": "heatmap", "data": "load.csv", "x": "hour", "y": "weekday", "value": "requests", "types": {"hour": "ordinal", '
                      '"weekday": "ordinal"}}',
              order=60),
)
