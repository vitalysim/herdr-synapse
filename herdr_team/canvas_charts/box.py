"""Box plots (canvas v2 phase 3, 2.4): the spread of a measure, one box per group.

``y`` is the measure and ``x`` the groups (optional: one box without it). Each
box runs from the first to the third quartile with the median across it; the
whiskers reach the furthest values within 1.5 IQR (``whiskers: "minmax"``: the
extremes), and the points past them are drawn as outliers. At most 30 groups
(the ones with the most values; the rest fold into "Other").
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

ORDER = 80
CAP_GROUPS = 30
#: Outliers each box keeps in the model (the extremes first); the count is always exact.
MODEL_OUTLIERS = 20


def _fmt(spec: Mapping[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    return (spec.get("format") or {}).get("y"), (spec.get("units") or {}).get("y") or (spec.get("_units") or {}).get("y")


def summary(values: Sequence[float], whiskers: str) -> Dict[str, Any]:
    """Quartiles, whiskers and outliers of one group's values."""
    ordered = sorted(values)
    q1, med, q3 = (_data.quantile(ordered, q) for q in (0.25, 0.5, 0.75))
    if whiskers == "minmax":
        lo, hi, out = ordered[0], ordered[-1], []
    else:
        fence_lo, fence_hi = q1 - 1.5 * (q3 - q1), q3 + 1.5 * (q3 - q1)
        inside = [v for v in ordered if fence_lo <= v <= fence_hi]
        lo, hi = (inside[0], inside[-1]) if inside else (q1, q3)
        out = [v for v in ordered if v < fence_lo or v > fence_hi]
    return {"n": len(ordered), "q1": q1, "med": med, "q3": q3, "lo": lo, "hi": hi, "out": out}


def _groups(spec: Mapping[str, Any], table: _data.Table) -> Tuple[List[Tuple[str, List[float]]], List[str]]:
    ys = table.by_name[spec["y"]].values
    if not spec.get("x"):
        return [(spec["y"], [v for v in ys if v is not None])], []
    column = table.by_name[spec["x"]]
    by: Dict[str, List[float]] = {}
    for key, v in zip(column.values, ys):
        if v is None:
            continue
        by.setdefault(SE._cat_label(key, column, spec), []).append(v)
    names = sorted(by, key=lambda g: (-len(by[g]), g))
    capped: List[str] = []
    if len(names) > CAP_GROUPS:
        rest = names[CAP_GROUPS - 1:]
        names = names[:CAP_GROUPS - 1]
        by[SE.OTHER] = [v for g in rest for v in by[g]]
        names.append(SE.OTHER)
        capped.append("{} of {} {} values folded into {}".format(len(rest), len(rest) + CAP_GROUPS - 1, spec["x"], SE.OTHER))
    return [(g, by[g]) for g in names], capped


def model(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
    groups, capped = _groups(spec, table)
    whiskers = spec.get("whiskers") or "1.5iqr"
    boxes = []
    for name, values in groups:
        if not values:
            continue
        found = summary(values, whiskers)
        outs = sorted(found["out"], key=lambda v: (-abs(v - found["med"]), v))[:MODEL_OUTLIERS]
        boxes.append({"name": name, "n": found["n"], "q": [_data.compact_number(found[k]) for k in ("lo", "q1", "med", "q3", "hi")],
                      "out": sorted(_data.compact_number(v) for v in outs), "outs": len(found["out"])})
    if spec.get("sort") in ("y", "-y"):
        boxes.sort(key=lambda b: ((1 if spec["sort"] == "y" else -1) * b["q"][2], b["name"]))
    elif spec.get("sort") in ("x", "-x"):
        boxes.sort(key=lambda b: b["name"].lower(), reverse=spec["sort"] == "-x")
    return {"boxes": boxes, "capped": capped, "_warnings": [{"code": "chart_capped", "message": c} for c in capped]}


def _extent(model: Mapping[str, Any]) -> Tuple[float, float]:
    values = [v for b in model.get("boxes") or [] for v in list(b["q"]) + list(b["out"]) if v is not None]
    return (min(values), max(values)) if values else (0.0, 1.0)


def frame(spec: Dict[str, Any], model: Dict[str, Any], box: Tuple[float, float]) -> Frame:
    lo, hi = _extent(model)
    fmt, unit = _fmt(spec)
    title = F.with_unit(spec["y"], unit)
    return FR.cartesian(box, {"kind": "category", "labels": [b["name"] for b in model.get("boxes") or []]},
                        {"kind": "value", "lo": lo, "hi": hi, "zero": False, "fmt": fmt, "unit": unit}, (), "none",
                        horizontal=bool(spec.get("horizontal")), y_title=title)


def draw(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, slot: Tuple[float, float, float, float]) -> List[Dict[str, Any]]:
    pen = DR.Pen(slot[0], slot[1])
    DR.axes(pen, frame)
    px, py, pw, ph = frame.plot
    axis = frame.axes["y"]
    boxes = model.get("boxes") or []
    horizontal = bool(spec.get("horizontal"))
    length = ph if horizontal else pw
    step, bandwidth, offset = S.band(len(boxes), length, 0.4)
    width = min(bandwidth, 60.0)
    for index, b in enumerate(boxes):
        paint = "chart.highlight" if b["name"] in (spec.get("highlight") or []) else S.cat(0)
        centre = step * index + step / 2.0
        lo, q1, med, q3, hi = b["q"]

        def at(v: float) -> float:
            return DR.value_pos(axis, v, px, pw, False) if horizontal else DR.value_pos(axis, v, py, ph, True)

        if horizontal:
            cy = py + centre
            pen.line([[at(lo), cy], [at(q1), cy]], "chart.ink", 1)
            pen.line([[at(q3), cy], [at(hi), cy]], "chart.ink", 1)
            for v in (lo, hi):
                pen.line([[at(v), cy - width / 4.0], [at(v), cy + width / 4.0]], "chart.ink", 1)
            pen.rect(min(at(q1), at(q3)), cy - width / 2.0, max(1.0, abs(at(q3) - at(q1))), width, paint, "chart.ink", 1, r=2, op=0.85)
            pen.line([[at(med), cy - width / 2.0], [at(med), cy + width / 2.0]], "chart.paper", 2)
            for v in b["out"]:
                pen.rect(at(v) - 2.5, cy - 2.5, 5, 5, "chart.paper", "chart.ink", 1, r=2.5)
        else:
            cx = px + centre
            pen.line([[cx, at(lo)], [cx, at(q1)]], "chart.ink", 1)
            pen.line([[cx, at(q3)], [cx, at(hi)]], "chart.ink", 1)
            for v in (lo, hi):
                pen.line([[cx - width / 4.0, at(v)], [cx + width / 4.0, at(v)]], "chart.ink", 1)
            pen.rect(cx - width / 2.0, min(at(q1), at(q3)), width, max(1.0, abs(at(q3) - at(q1))), paint, "chart.ink", 1, r=2, op=0.85)
            pen.line([[cx - width / 2.0, at(med)], [cx + width / 2.0, at(med)]], "chart.paper", 2)
            for v in b["out"]:
                pen.rect(cx - 2.5, at(v) - 2.5, 5, 5, "chart.paper", "chart.ink", 1, r=2.5)
    return pen.items


def doc(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    boxes = model.get("boxes") or []
    return {"datasets": [], "refs": {"boxes": [{"name": b["name"], "value": list(b["q"])} for b in boxes],
                                     "outliers": [[index, v] for index, b in enumerate(boxes) for v in b["out"]]}}


def option(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, gist: Sequence[str]) -> Dict[str, Any]:
    out = O.base(gist)
    horizontal = bool(spec.get("horizontal"))
    cat_axis, value_axis = O.axis(frame, "x"), O.axis(frame, "y")
    cat_axis["data"] = [b["name"] for b in model.get("boxes") or []]
    x_axis, y_axis = (value_axis, cat_axis) if horizontal else (cat_axis, value_axis)
    out.update(grid=O.grid(frame), xAxis=x_axis, yAxis=y_axis, tooltip=O.tooltip("item"), legend={"show": False})
    boxes = model.get("boxes") or []
    step = (frame.plot[3] if horizontal else frame.plot[2]) / max(1, len(boxes))
    width = min(step * 0.6, 60.0)
    out["series"] = [{"type": "boxplot", "name": spec["y"], "data": {"$doc": "boxes"}, "boxWidth": [round(width, 2), round(width, 2)],
                      "itemStyle": {"color": "chart.cat.0", "borderColor": "chart.ink", "borderWidth": 1, "opacity": 0.85}},
                     {"type": "scatter", "name": "outliers", "data": {"$doc": "outliers"}, "symbolSize": 5,
                      "encode": {"x": 1, "y": 0} if horizontal else {"x": 0, "y": 1},
                      "itemStyle": {"color": "chart.paper", "borderColor": "chart.ink", "borderWidth": 1}}]
    return out


def stats(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    groups, _capped = _groups(spec, table)
    whiskers = spec.get("whiskers") or "1.5iqr"
    rows = []
    for name, values in groups:
        if values:
            found = summary(values, whiskers)
            rows.append([name, found["med"], found["q3"] - found["q1"], len(found["out"]), found["n"]])
    out: Dict[str, Any] = {"groups": rows}
    if rows:
        out["high"] = max(rows, key=lambda r: (r[1], r[0]))[:2]
        out["low"] = min(rows, key=lambda r: (r[1], r[0]))[:2]
    return out


def gist(spec: Dict[str, Any], model: Dict[str, Any], stats: Mapping[str, Any]) -> List[str]:
    fmt, unit = _fmt(spec)

    def n(value: Any) -> str:
        return F.fmt_number(value, fmt or ("compact" if abs(float(value)) >= 1e4 else None), unit)

    rows = stats.get("groups") or []
    head = ["y={}".format(spec["y"])]
    if spec.get("x"):
        head.append("x={} ({} groups)".format(spec["x"], len(rows)))
    head.append("whiskers {}".format(spec.get("whiskers") or "1.5iqr"))
    head.append(str(spec.get("_source") or ""))
    lines = [G.join(head)]
    if stats.get("high") and len(rows) > 1:
        lines.append("highest median {} {} · lowest {} {}".format(stats["high"][0], n(stats["high"][1]), stats["low"][0], n(stats["low"][1])))
    for name, med, iqr, outs, count in rows[:4]:
        lines.append("{}: median {} · IQR {} · {} outlier{} of {}".format(name, n(med), n(iqr), outs, "" if outs == 1 else "s", count))
    if len(rows) > 4:
        lines.append("… +{} groups".format(len(rows) - 4))
    for note in model.get("capped") or []:
        lines.append("capped: " + note)
    return lines


def vegalite(spec: Dict[str, Any], model: Dict[str, Any]) -> Dict[str, Any]:
    rows = []
    for b in model.get("boxes") or []:
        lo, q1, med, q3, hi = b["q"]
        rows.append({"group": b["name"], "lo": lo, "q1": q1, "med": med, "q3": q3, "hi": hi})
    return {"data": {"values": rows}, "width": "container",
            "encoding": {"x": {"field": "group", "type": "nominal", "sort": None, "title": spec.get("x") or ""}},
            "layer": [{"mark": "rule", "encoding": {"y": {"field": "lo", "type": "quantitative", "title": spec["y"], "scale": {"zero": False}},
                                                    "y2": {"field": "hi"}}},
                      {"mark": {"type": "bar", "size": 24}, "encoding": {"y": {"field": "q1", "type": "quantitative"}, "y2": {"field": "q3"}}},
                      {"mark": {"type": "tick", "color": "white", "size": 24}, "encoding": {"y": {"field": "med", "type": "quantitative"}}}]}


def min_box(spec: Dict[str, Any], model: Dict[str, Any]) -> Tuple[float, float]:
    count = len(model.get("boxes") or [])
    along = max(120.0, count * 24.0)
    return (280.0, along + 60) if spec.get("horizontal") else (along + 90, 200.0)


CHARTS = (
    ChartType(name="box", aliases=("boxplot",),
              channels=(Channel("x", types=("nominal", "ordinal", "temporal"), doc="the groups (one box each); leave out for one box"),
                        Channel("y", required=True, types=("quantitative",), doc="the measure whose spread is drawn")),
              options=("whiskers", "horizontal"), frame_kind="cartesian", model=model, stats=stats, doc=doc, frame=frame, option=option,
              draw=draw, gist=gist, vegalite=vegalite,
              echarts=("BoxplotChart", "ScatterChart", "GridComponent", "TooltipComponent", "AriaComponent"),
              min_box=min_box, default_box=(560, 360), caps="30 groups", doc_line="the spread of a measure per group (quartiles, whiskers, outliers)",
              example='{"op": "chart", "id": "build", "intent": "which CI runner is slowest", "title": "Build time by runner", "type": "box", '
                      '"rows": [{"runner": "linux", "minutes": 6.1}, {"runner": "linux", "minutes": 6.8}, {"runner": "linux", "minutes": 7.2}, '
                      '{"runner": "linux", "minutes": 6.5}, {"runner": "linux", "minutes": 12.9}, {"runner": "mac", "minutes": 9.4}, {"runner": "mac", "minutes": 10.1}, '
                      '{"runner": "mac", "minutes": 8.8}, {"runner": "mac", "minutes": 9.9}, {"runner": "win", "minutes": 11.2}, {"runner": "win", "minutes": 12.4}, '
                      '{"runner": "win", "minutes": 10.7}, {"runner": "win", "minutes": 11.9}], "x": "runner", "y": "minutes", "units": {"y": "min"}}',
              order=80),
)
