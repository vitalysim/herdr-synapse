"""Bar charts (canvas v2 phase 3, 2.4): categories along x, one bar per ``color`` series, grouped or stacked.

The reference chart type: ``x`` is the categories (nominal, ordinal or time),
``y`` the measure (summed when rows repeat, or ``aggregate``), ``color`` splits
it into series. ``stack`` stacks them (``percent`` to 100 %), ``horizontal``
lays the bars along x with the categories down the left. At most 60 categories
and 12 series (the rest fold into "Other"; ``top`` asks for fewer).
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

ORDER = 10
CAP_X = 60
CAP_SERIES = 12
#: ``labels: auto`` writes values on the bars when there are at most this many.
AUTO_LABELS = 12


def normalize(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
    spec.setdefault("stack", False)
    return spec


def model(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
    found = SE.matrix(spec, table, spec["x"], spec.get("y"), spec.get("color"), cap_x=CAP_X, cap_series=CAP_SERIES)
    return {"keys": [k if isinstance(k, (str, int, float)) or k is None else str(k) for k in found["keys"]], "cats": found["cats"],
            "series": SE.compacted(found["series"]), "how": found["how"], "order": found["order"], "capped": found["capped"],
            "_warnings": found["_warnings"]}


def shown(spec: Mapping[str, Any], model: Mapping[str, Any]) -> List[List[Optional[float]]]:
    """Each series' drawn values (``stack: percent`` makes each category's bars shares of 1)."""
    series = [list(s["values"]) for s in model.get("series") or []]
    if spec.get("stack") == "percent" and series:
        for index in range(len(model.get("cats") or [])):
            total = math.fsum(abs(s[index] or 0.0) for s in series)
            for s in series:
                s[index] = (s[index] or 0.0) / total if total else 0.0
    return series


def extent(spec: Mapping[str, Any], model: Mapping[str, Any]) -> Tuple[float, float]:
    series = shown(spec, model)
    if spec.get("stack") and series:
        highs, lows = [], []
        for index in range(len(model.get("cats") or [])):
            highs.append(math.fsum(max(0.0, s[index] or 0.0) for s in series))
            lows.append(math.fsum(min(0.0, s[index] or 0.0) for s in series))
        return min(lows + [0.0]), max(highs + [0.0])
    return S.value_range([v for s in series for v in s if v is not None], True)


def _fmt(spec: Mapping[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    if spec.get("stack") == "percent":
        return "percent", None
    fmt = (spec.get("format") or {}).get("y")
    unit = (spec.get("units") or {}).get("y") or (spec.get("_units") or {}).get("y")
    return fmt, unit


def y_title(spec: Mapping[str, Any], model: Mapping[str, Any]) -> str:
    fmt, unit = _fmt(spec)
    title = SE.measure_label(spec, spec.get("y"), str(model.get("how") or "none"))
    title = F.with_unit(title, unit)
    if spec.get("stack") == "percent":
        title = "share of " + title
    return title


def colors(spec: Mapping[str, Any], model: Mapping[str, Any]) -> List[str]:
    names = [s["name"] for s in model.get("series") or []]
    wanted = set(spec.get("highlight") or [])
    if wanted and len(names) > 1 and wanted & set(names):
        return ["chart.highlight" if n in wanted else "chart.dim" for n in names]
    return [S.cat(i) for i in range(len(names))]


def cat_colors(spec: Mapping[str, Any], model: Mapping[str, Any]) -> Optional[List[str]]:
    """Per-category colours when ``highlight`` names categories of a single series."""
    wanted = set(spec.get("highlight") or [])
    cats = model.get("cats") or []
    if not wanted or len(model.get("series") or []) != 1 or not wanted & set(cats):
        return None
    return ["chart.highlight" if c in wanted else "chart.dim" for c in cats]


def frame(spec: Dict[str, Any], model: Dict[str, Any], box: Tuple[float, float]) -> Frame:
    lo, hi = extent(spec, model)
    fmt, unit = _fmt(spec)
    cats = {"kind": "category", "labels": list(model.get("cats") or []), "thin": model.get("order") == "time"}
    value = {"kind": "value", "lo": lo, "hi": hi, "zero": True, "fmt": fmt, "unit": unit}
    names = [s["name"] for s in model.get("series") or []]
    legend = FR.legend_items(names, colors(spec, model)) if len(names) > 1 else []
    return FR.cartesian(box, cats, value, legend, spec.get("legend") or "auto", horizontal=bool(spec.get("horizontal")),
                        y_title=y_title(spec, model))


def _labels_on(spec: Mapping[str, Any], model: Mapping[str, Any]) -> bool:
    mode = spec.get("labels") or "auto"
    count = sum(1 for s in model.get("series") or [] for v in s["values"] if v is not None)
    return mode == "values" or (mode == "auto" and count <= AUTO_LABELS)


def draw(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, slot: Tuple[float, float, float, float]) -> List[Dict[str, Any]]:
    pen = DR.Pen(slot[0], slot[1])
    DR.axes(pen, frame)
    px, py, pw, ph = frame.plot
    horizontal = bool(spec.get("horizontal"))
    axis = frame.axes["y"]  # the value axis, whichever side it is on
    series = shown(spec, model)
    cats = model.get("cats") or []
    paints = colors(spec, model)
    per_cat = cat_colors(spec, model)
    length = pw if not horizontal else ph
    step, bandwidth, offset = S.band(len(cats), length, 0.2)
    stacked = bool(spec.get("stack"))
    groups = 1 if stacked else max(1, len(series))
    bar = bandwidth / groups
    fmt, unit = _fmt(spec)
    labels: List[Tuple[float, float, str, str]] = []
    for index in range(len(cats)):
        up = down = 0.0
        for s_index, values in enumerate(series):
            value = values[index]
            if value is None:
                continue
            if stacked:
                base = up if value >= 0 else down
                top = base + value
                if value >= 0:
                    up = top
                else:
                    down = top
            else:
                base, top = 0.0, value
            start = (step * index) + offset + (0 if stacked else s_index * bar)
            paint = per_cat[index] if per_cat else paints[s_index % len(paints)]
            if not horizontal:
                y0 = DR.value_pos(axis, base, py, ph, True)
                y1 = DR.value_pos(axis, top, py, ph, True)
                pen.rect(px + start, min(y0, y1), max(bar - 1, 1.0), abs(y0 - y1), paint, r=2 if bar > 8 else 0)
                labels.append((px + start + bar / 2.0, min(y0, y1) - FR.LH / 2.0 - 1, _label(value, fmt, unit, stacked), "middle"))
            else:
                x0 = DR.value_pos(axis, base, px, pw, False)
                x1 = DR.value_pos(axis, top, px, pw, False)
                pen.rect(min(x0, x1), py + start, abs(x1 - x0), max(bar - 1, 1.0), paint, r=2 if bar > 8 else 0)
                labels.append((max(x0, x1) + 4, py + start + bar / 2.0, _label(value, fmt, unit, stacked), "start"))
    if _labels_on(spec, model) and not stacked:
        DR.value_labels(pen, labels, frame)
    for note in spec.get("annotations") or []:
        if note.get("y") is not None and isinstance(note["y"], (int, float)):
            if horizontal:
                DR.annotation_x(pen, DR.value_pos(axis, float(note["y"]), px, pw, False), frame, note.get("text") or "")
            else:
                DR.annotation_y(pen, DR.value_pos(axis, float(note["y"]), py, ph, True), frame, note.get("text") or "")
        elif note.get("x") is not None and str(note["x"]) in cats:
            centre = (step * cats.index(str(note["x"]))) + step / 2.0
            if horizontal:
                DR.annotation_y(pen, py + centre, frame, note.get("text") or "")
            else:
                DR.annotation_x(pen, px + centre, frame, note.get("text") or "")
    DR.legend(pen, frame)
    return pen.items


def _label(value: float, fmt: Optional[str], unit: Optional[str], stacked: bool) -> str:

    if fmt is None and abs(value) >= 1e4:
        fmt = "compact"
    return F.fmt_number(value, fmt, unit)


def doc(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    series = shown(spec, model)
    dims = ["x"] + ["s{}".format(i) for i in range(len(series))]
    rows = [[cat] + [s[i] for s in series] for i, cat in enumerate(model.get("cats") or [])]
    return {"datasets": [{"id": "d0", "dimensions": dims, "source": rows}], "refs": {}}


def option(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, gist: Sequence[str]) -> Dict[str, Any]:
    horizontal = bool(spec.get("horizontal"))
    names = [s["name"] for s in model.get("series") or []]
    out = O.base(gist, colors(spec, model))
    x_axis, y_axis = (O.axis(frame, "y"), O.axis(frame, "x")) if horizontal else (O.axis(frame, "x"), O.axis(frame, "y"))
    out.update(grid=O.grid(frame), xAxis=x_axis, yAxis=y_axis, legend=O.legend(frame), tooltip=O.tooltip("axis"), dataset=O.dataset())
    fmt, unit = _fmt(spec)

    label_id = F.fmt_id(fmt or "auto", unit)
    stacked = bool(spec.get("stack"))
    per_cat = cat_colors(spec, model)
    series = []
    for index, name in enumerate(names):
        item: Dict[str, Any] = {"type": "bar", "name": name, "barCategoryGap": "20%", "barGap": "0%",
                                "itemStyle": {"borderRadius": 2}}
        item.update(datasetIndex=0, encode={"x": "s{}".format(index), "y": "x"} if horizontal else {"x": "x", "y": "s{}".format(index)})
        if per_cat and index == 0:
            item.update(colorBy="data", color=per_cat)
        if stacked:
            item["stack"] = "total"
        if _labels_on(spec, model) and not stacked:
            item["label"] = O.value_label(label_id, "right" if horizontal else "top")
        series.append(item)
    marks = O.mark_lines([{("x" if horizontal else "y"): n["y"], "text": n.get("text")} if n.get("y") is not None else
                          {("y" if horizontal else "x"): n["x"], "text": n.get("text")} for n in spec.get("annotations") or []], True)
    if marks and series:
        series[0]["markLine"] = marks
    out["series"] = series
    return out


def stats(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    return SE.series_stats(model)


def gist(spec: Dict[str, Any], model: Dict[str, Any], stats: Mapping[str, Any]) -> List[str]:
    lines = [SE.first_line(spec, model, spec["x"], spec.get("y"), spec.get("color"), str(spec.get("_source") or ""))]
    parts = []
    multi = len(model.get("series") or []) > 1
    for key in ("max", "min"):
        found = stats.get(key)
        if found:
            name, cat, value = found
            parts.append("{} {}{} {}".format(key, name + " " if multi else "", cat, G.num(value, spec)))
    if stats.get("total") is not None and model.get("how") in ("sum", "count", "none"):
        parts.append("total {}".format(G.num(stats["total"], spec)))
    if parts:
        lines.append(G.join(parts))
    trends = SE.trends_line(spec, stats)
    if trends:
        lines.append(trends)
    elif stats.get("top") and model.get("order") != "time":
        lines.append("top: " + " · ".join("{} {}".format(k, G.num(v, spec)) for k, v in stats["top"]))
    if spec.get("stack") == "percent":
        lines.append("stacked to 100 % per {}".format(spec["x"]))
    for note in model.get("capped") or []:
        lines.append("capped: " + note)
    return lines


def vegalite(spec: Dict[str, Any], model: Dict[str, Any]) -> Dict[str, Any]:
    rows = [{"x": cat, "series": s["name"], "value": s["values"][i]} for s in model.get("series") or [] for i, cat in enumerate(model.get("cats") or [])
            if s["values"][i] is not None]
    enc: Dict[str, Any] = {"x": {"field": "x", "type": "nominal", "sort": None, "title": spec["x"]},
                           "y": {"field": "value", "type": "quantitative", "title": spec.get("y") or "count",
                                 "stack": "normalize" if spec.get("stack") == "percent" else (True if spec.get("stack") else None)}}
    if len(model.get("series") or []) > 1:
        enc["color"] = {"field": "series", "type": "nominal", "title": spec.get("color")}
        if not spec.get("stack"):
            enc["xOffset"] = {"field": "series"}
    if spec.get("horizontal"):
        enc["x"], enc["y"] = enc["y"], enc["x"]
    return VL.compat("bar", rows, enc, {"width": "container"})


def min_box(spec: Dict[str, Any], model: Dict[str, Any]) -> Tuple[float, float]:
    count = len(model.get("cats") or [])
    groups = 1 if spec.get("stack") else max(1, len(model.get("series") or []))
    along = max(FR.min_plot_for(count), count * FR.BAR_MIN_PX * groups / 0.8)
    if spec.get("horizontal"):
        return 280.0, max(160.0, along + 60)
    widest = max((FR.text_w(str(c)) for c in model.get("cats") or []), default=0.0)
    return max(240.0, along + 90), max(200.0, min(widest, 160.0) / FR.LABEL_HEIGHT_SHARE)


CHARTS = (
    ChartType(name="bar", aliases=("column",),
              channels=(Channel("x", required=True, types=("nominal", "ordinal", "temporal"), doc="the categories"),
                        Channel("y", required=True, types=("quantitative",), aggregate=True, doc="the measure (summed when rows repeat)"),
                        Channel("color", types=("nominal", "ordinal"), doc="one bar per value (series)")),
              options=("stack", "horizontal"), frame_kind="cartesian", normalize=normalize, model=model, stats=stats, doc=doc, frame=frame,
              option=option, draw=draw, gist=gist, vegalite=vegalite,
              echarts=("BarChart", "GridComponent", "DatasetComponent", "LegendComponent", "TooltipComponent", "MarkLineComponent",
                       "AriaComponent"),
              min_box=min_box, default_box=(640, 400), caps="60 categories × 12 series", doc_line="compare a measure across categories (grouped or stacked bars)",
              example='{"op": "chart", "id": "rev", "intent": "compare regions by month", "title": "Revenue by region, 2026", '
                      '"type": "bar", "data": "rev.csv", "x": "month", "y": "revenue", "color": "region", "format": {"y": "compact"}, '
                      '"units": {"y": "$"}}',
              order=10),
)
