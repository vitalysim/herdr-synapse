"""Line-like charts (canvas v2 phase 3): the frame, drawing, option and doc that ``line`` and ``area`` share.

The model is ``_series.xy`` thinned for drawing: per series the x keys (day
numbers on a time axis, numbers on a value axis, category indices) and the y
values, so the Python drawing and the ECharts option place every point on the
same axes the frame chose. ``area`` adds a filled band, stacking and 100 %
stacks; ``line`` adds point markers and smoothing.

Pure, stdlib only.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team.canvas_charts import _data
from herdr_team.canvas_charts import _draw as DR
from herdr_team.canvas_charts import _format as F
from herdr_team.canvas_charts import _frame as FR
from herdr_team.canvas_charts import _option as O
from herdr_team.canvas_charts import _scale as S
from herdr_team.canvas_charts import _series as SE
from herdr_team.canvas_charts import _vegalite as VL
from herdr_team.canvas_charts._frame import Frame

CAP_SERIES = 12
#: ``labels: auto`` writes values on the points when there are at most this many.
AUTO_LABELS = 12
#: Point markers are drawn when a series has at most this many points (``points: true`` always).
AUTO_POINTS = 30


def model(spec: Dict[str, Any], table: _data.Table, stacked: bool = False) -> Dict[str, Any]:
    found = SE.xy(spec, table, spec["x"], spec.get("y"), spec.get("color"), cap_series=CAP_SERIES, aligned=stacked)
    budget = SE.MODEL_POINTS
    out: Dict[str, Any] = {}
    for _try in range(6):
        out = {"xkind": found["xkind"], "grain": found["grain"], "cats": found["cats"], "series": SE.thin(found["series"], budget),
               "how": found["how"], "capped": list(found["capped"])}
        if len(_data.json_text(out)) <= 15 * 1024:
            break
        budget //= 2
    kept = sum(len(s["x"]) for s in out["series"])
    total = sum(len(s["x"]) for s in found["series"])
    if kept < total:
        out["thinned"] = [kept, total]
    out["_warnings"] = found["_warnings"]
    out["_full"] = found
    return out


def shown(spec: Mapping[str, Any], model: Mapping[str, Any]) -> List[List[Optional[float]]]:
    """Each series' drawn y values (a ``percent`` stack makes each x's values shares of 1)."""
    series = [list(s["y"]) for s in model.get("series") or []]
    if spec.get("stack") == "percent" and series:
        for index in range(len(series[0])):
            total = math.fsum(abs(s[index] or 0.0) for s in series if index < len(s))
            for s in series:
                if index < len(s):
                    s[index] = (s[index] or 0.0) / total if total else 0.0
    return series


def stacked_tops(spec: Mapping[str, Any], model: Mapping[str, Any]) -> List[List[Optional[float]]]:
    """Each series' upper edge: its own values, or the running sum when stacked."""
    series = shown(spec, model)
    if not spec.get("stack"):
        return series
    out: List[List[Optional[float]]] = []
    running = [0.0] * (len(series[0]) if series else 0)
    for values in series:
        running = [r + (v or 0.0) for r, v in zip(running, values)]
        out.append(list(running))
    return out


def fmt_of(spec: Mapping[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    if spec.get("stack") == "percent":
        return "percent", None
    return SE._fmt_of(spec), SE._unit_of(spec)


def y_title(spec: Mapping[str, Any], model: Mapping[str, Any]) -> str:
    _fmt, unit = fmt_of(spec)
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


def x_spec(spec: Mapping[str, Any], model: Mapping[str, Any]) -> Dict[str, Any]:
    kind = model.get("xkind")
    if kind == "category":
        return {"kind": "category", "labels": list(model.get("cats") or []), "thin": True}
    xs = [float(v) for s in model.get("series") or [] for v in s["x"] if v is not None]
    lo, hi = (min(xs), max(xs)) if xs else (0.0, 1.0)
    if kind == "time":
        return {"kind": "time", "lo": lo, "hi": hi if hi > lo else lo + 1.0}
    return {"kind": "value", "lo": lo, "hi": hi, "zero": False, "fmt": (spec.get("format") or {}).get("x"), "unit": (spec.get("units") or {}).get("x")}


def frame(spec: Dict[str, Any], model: Dict[str, Any], box: Tuple[float, float], zero: bool = False) -> Frame:
    tops = stacked_tops(spec, model)
    values = [v for s in tops for v in s if v is not None]
    if spec.get("stack"):
        values += [0.0]
    lo, hi = S.value_range(values, zero)
    fmt, unit = fmt_of(spec)
    names = [s["name"] for s in model.get("series") or []]
    legend = FR.legend_items(names, colors(spec, model)) if len(names) > 1 else []
    return FR.cartesian(box, x_spec(spec, model), {"kind": "value", "lo": lo, "hi": hi, "zero": zero, "fmt": fmt, "unit": unit}, legend,
                        spec.get("legend") or "auto", y_title=y_title(spec, model))


def _x_at(frame: Frame, model: Mapping[str, Any], key: float) -> float:
    px, _py, pw, _ph = frame.plot
    axis = frame.axes.get("x") or {}
    if axis.get("kind") == "category":
        return DR.band_centre(int(key), len(axis.get("labels") or []), px, pw)
    return DR.value_pos(axis, float(key), px, pw, False)


def labels_on(spec: Mapping[str, Any], model: Mapping[str, Any]) -> bool:
    mode = spec.get("labels") or "auto"
    count = sum(1 for s in model.get("series") or [] for v in s["y"] if v is not None)
    return mode == "values" or (mode == "auto" and count <= AUTO_LABELS and not spec.get("stack"))


def annotation_key(note: Mapping[str, Any], model: Mapping[str, Any]) -> Optional[float]:
    """Where an ``x`` annotation sits on the x axis (a day number, a number or a category index), or None."""
    value = note.get("x")
    kind = model.get("xkind")
    if kind == "time":
        found = _data.temporal(value if isinstance(value, str) else _data.as_label(value))
        return found[1] if found else None
    if kind == "value":
        found_n = _data.number(value)
        return found_n[0] if found_n else None
    cats = model.get("cats") or []
    return float(cats.index(str(value))) if str(value) in cats else None


def draw(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, slot: Tuple[float, float, float, float], area: bool = False) -> List[Dict[str, Any]]:
    pen = DR.Pen(slot[0], slot[1])
    DR.axes(pen, frame)
    px, py, pw, ph = frame.plot
    axis = frame.axes["y"]
    paints = colors(spec, model)
    tops = stacked_tops(spec, model)
    smooth = bool(spec.get("smooth"))
    fmt, unit = fmt_of(spec)
    base_y = DR.value_pos(axis, max(float(axis.get("min", 0)), min(0.0, float(axis.get("max", 0)))), py, ph, True)
    previous: Optional[List[Tuple[float, float]]] = None
    labels: List[Tuple[float, float, str, str]] = []
    for index, s in enumerate(model.get("series") or []):
        paint = paints[index % len(paints)]
        points = [(_x_at(frame, model, k), DR.value_pos(axis, v, py, ph, True)) for k, v in zip(s["x"], tops[index]) if v is not None and k is not None]
        if area and len(points) >= 2:
            bottom = previous if (spec.get("stack") and previous is not None and len(previous) == len(points)) else [(x, base_y) for x, _ in points]
            DR.area(pen, points, bottom, paint, 0.22 if not spec.get("stack") else 0.55, smooth)
        DR.polyline(pen, points, paint, 2.0, smooth)
        markers = spec.get("points") or (not area and len(points) <= AUTO_POINTS)
        if markers:
            for x, y in points:
                pen.rect(x - 3, y - 3, 6, 6, "chart.paper", paint, 1.5, r=3)
        if labels_on(spec, model):
            for (x, y), value in zip(points, [v for v in s["y"] if v is not None]):
                labels.append((x, y - FR.LH / 2.0 - 4, F.fmt_number(value, fmt if fmt or abs(value) < 1e4 else "compact", unit), "middle"))
        previous = points
    DR.value_labels(pen, labels, frame)
    for note in spec.get("annotations") or []:
        if note.get("y") is not None and _data.number(note["y"]) is not None:
            DR.annotation_y(pen, DR.value_pos(axis, _data.number(note["y"])[0], py, ph, True), frame, note.get("text") or "")  # type: ignore[index]
        elif note.get("x") is not None:
            key = annotation_key(note, model)
            if key is not None:
                DR.annotation_x(pen, _x_at(frame, model, key), frame, note.get("text") or "")
    DR.legend(pen, frame)
    return pen.items


def doc(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    """One dataset per series (``x``, ``y``) at page resolution (2,000 points a series, LTTB), or on a category axis one
    dataset with a column per series."""
    full = model.get("_full") or model
    series = SE.thin(full["series"], SE.DOC_POINTS * max(1, len(full["series"])))
    if spec.get("stack") == "percent":
        series = [dict(s, y=v) for s, v in zip(series, shown(spec, {"series": series}))]
    if model.get("xkind") == "category":
        cats = model.get("cats") or []
        rows = []
        for index, cat in enumerate(cats):
            row: List[Any] = [cat]
            for s in series:
                lookup = dict(zip(s["x"], s["y"]))
                row.append(lookup.get(index, lookup.get(float(index))))
            rows.append(row)
        return {"datasets": [{"id": "d0", "dimensions": ["x"] + ["s{}".format(i) for i in range(len(series))], "source": rows}], "refs": {}}
    return {"datasets": [{"id": "d{}".format(i), "dimensions": ["x", "y"], "source": [[x, y] for x, y in zip(s["x"], s["y"])]}
                         for i, s in enumerate(series)], "refs": {}}


def option(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, gist: Sequence[str], area: bool = False) -> Dict[str, Any]:
    out = O.base(gist, colors(spec, model))
    out.update(grid=O.grid(frame), xAxis=O.axis(frame, "x"), yAxis=O.axis(frame, "y"), legend=O.legend(frame), tooltip=O.tooltip("axis"),
               dataset=O.dataset())
    fmt, unit = fmt_of(spec)
    category = model.get("xkind") == "category"
    series = []
    for index, s in enumerate(model.get("series") or []):
        item: Dict[str, Any] = {"type": "line", "name": s["name"], "datasetIndex": 0 if category else index,
                                "encode": {"x": "x", "y": "s{}".format(index) if category else "y"},
                                "showSymbol": bool(spec.get("points") or (not area and len(s["x"]) <= AUTO_POINTS)), "symbol": "circle",
                                "symbolSize": 6, "smooth": bool(spec.get("smooth")), "connectNulls": False, "lineStyle": {"width": 2},
                                "emphasis": {"focus": "series"}}
        if area:
            item["areaStyle"] = {"opacity": 0.22 if not spec.get("stack") else 0.55}
        if spec.get("stack"):
            item["stack"] = "total"
        if labels_on(spec, model):
            item["label"] = O.value_label(F.fmt_id(fmt or "auto", unit), "top")
        series.append(item)
    marks = []
    for note in spec.get("annotations") or []:
        if note.get("y") is not None:
            marks.append({"y": note["y"], "text": note.get("text")})
        elif note.get("x") is not None:
            key = annotation_key(note, model)
            if key is not None:
                marks.append({"x": (model.get("cats") or [])[int(key)] if category else _data.compact_number(key), "text": note.get("text")})
    found = O.mark_lines(marks, category)
    if found and series:
        series[0]["markLine"] = found
    out["series"] = series
    return out


def gist(spec: Dict[str, Any], model: Dict[str, Any], stats: Mapping[str, Any]) -> List[str]:
    span = stats.get("span")
    parts = ["x={} ({} point{}{})".format(spec["x"], stats.get("points", 0), "" if stats.get("points") == 1 else "s",
                                          ", {}…{}".format(span[0], span[1]) if span and span[0] != span[1] else ""),
             "y={}".format(SE.measure_label(spec, spec.get("y"), str(model.get("how") or "none")))]
    if spec.get("color"):
        parts.append("color={} ({})".format(spec["color"], len(model.get("series") or [])))
    parts.append(str(spec.get("_source") or ""))
    lines = [SE.G.join(parts)] + SE.xy_lines(spec, stats, spec.get("annotations") or [])
    if spec.get("stack") and stats.get("last_shares"):
        lines.append("at {}: ".format(stats["last_shares"][0]) + " · ".join("{} {}".format(n, F.share(v)) for n, v in stats["last_shares"][1]))
    if model.get("thinned"):
        lines.append("drawn from {} of {} points (LTTB)".format(*model["thinned"]))
    for note in model.get("capped") or []:
        lines.append("capped: " + note)
    return lines


def stats(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    full = model.get("_full") or model
    out = SE.xy_stats(spec, full)
    if spec.get("stack"):
        series = full.get("series") or []
        if series and series[0]["x"]:
            last = [s["y"][-1] or 0.0 for s in series]
            total = math.fsum(abs(v) for v in last)
            key = series[0]["x"][-1]
            out["last_shares"] = [str((full.get("labels") or {}).get(key, key)), [[s["name"], (abs(v) / total if total else 0.0)]
                                                                                   for s, v in zip(series, last)]]
    return out


def vegalite(spec: Dict[str, Any], model: Dict[str, Any], mark: str) -> Dict[str, Any]:
    kind = model.get("xkind")
    rows = []
    for s in model.get("series") or []:
        for k, v in zip(s["x"], s["y"]):
            if v is None:
                continue
            if kind == "time":
                x_value: Any = F.fmt_number(k, "date")
            elif kind == "category":
                x_value = (model.get("cats") or [])[int(k)]
            else:
                x_value = k
            rows.append({"x": x_value, "series": s["name"], "value": v})
    enc: Dict[str, Any] = {"x": {"field": "x", "type": {"time": "temporal", "value": "quantitative"}.get(str(kind), "ordinal"), "title": spec["x"],
                                 "sort": None},
                           "y": {"field": "value", "type": "quantitative", "title": spec.get("y") or "count"}}
    if spec.get("stack"):
        enc["y"]["stack"] = "normalize" if spec.get("stack") == "percent" else True
    if len(model.get("series") or []) > 1:
        enc["color"] = {"field": "series", "type": "nominal", "title": spec.get("color")}
    return VL.compat({"type": mark, "point": bool(spec.get("points"))} if mark == "line" else mark, rows, enc, {"width": "container"})


def min_box(spec: Dict[str, Any], model: Dict[str, Any]) -> Tuple[float, float]:
    count = len(model.get("cats") or []) if model.get("xkind") == "category" else 0
    return max(320.0, FR.min_plot_for(count) + 90 if count else 320.0), 200.0
