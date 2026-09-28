"""Histograms (canvas v2 phase 3, 2.4): how one measure is distributed, in bins of equal width.

``x`` is the measure; the bins are Freedman-Diaconis (5 to 60 of them, on a
nice step) unless ``bins`` says how many. ``color`` stacks up to 6 groups (the
rest "Other"). The gist reads the whole column: n, mean, median, standard
deviation, the modal bin and which way it is skewed.
"""
from __future__ import annotations

import bisect
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

ORDER = 70
CAP_GROUPS = 6


def _fmt(spec: Mapping[str, Any]) -> Tuple[Optional[str], Optional[str]]:
    return (spec.get("format") or {}).get("x"), (spec.get("units") or {}).get("x") or (spec.get("_units") or {}).get("x")


def model(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
    values = table.by_name[spec["x"]].values
    groups_col = table.by_name[spec["color"]].values if spec.get("color") else None
    present = [(v, (str(groups_col[i]) if groups_col[i] is not None else SE.NULL) if groups_col is not None else "")
               for i, v in enumerate(values) if v is not None]
    if not present:
        raise _data.refuse("chart_empty", "{} has no numbers in {} to bin".format(table.label, spec["x"]))
    edges = _data.bin_numeric([v for v, _g in present], spec.get("bins"))
    groups: List[str] = [""]
    capped: List[str] = []
    if groups_col is not None:
        counts: Dict[str, int] = {}
        for _v, g in present:
            counts[g] = counts.get(g, 0) + 1
        groups = sorted(counts, key=lambda g: (-counts[g], g))
        if len(groups) > CAP_GROUPS:
            rest = set(groups[CAP_GROUPS - 1:])
            groups = groups[:CAP_GROUPS - 1] + [SE.OTHER]
            present = [(v, SE.OTHER if g in rest else g) for v, g in present]
            capped.append("{} of {} {} values folded into {}".format(len(rest), len(rest) + CAP_GROUPS - 1, spec["color"], SE.OTHER))
    index = {g: i for i, g in enumerate(groups)}
    counts_by = [[0] * (len(edges) - 1) for _ in groups]
    for v, g in present:
        slot = min(len(edges) - 2, max(0, bisect.bisect_right(edges, v) - 1))
        counts_by[index[g]][slot] += 1
    return {"edges": [_data.compact_number(e) for e in edges], "counts": counts_by, "groups": groups if groups_col is not None else [],
            "n": len(present), "capped": capped, "_warnings": [{"code": "chart_capped", "message": c} for c in capped]}


def colors(spec: Mapping[str, Any], model: Mapping[str, Any]) -> List[str]:
    groups = model.get("groups") or []
    wanted = set(spec.get("highlight") or [])
    if wanted and wanted & set(groups):
        return ["chart.highlight" if g in wanted else "chart.dim" for g in groups]
    return [S.cat(i) for i in range(max(1, len(groups)))]


def _totals(model: Mapping[str, Any]) -> List[int]:
    counts = model.get("counts") or []
    return [sum(c[i] for c in counts) for i in range(len(model.get("edges") or []) - 1)]


def frame(spec: Dict[str, Any], model: Dict[str, Any], box: Tuple[float, float]) -> Frame:
    edges = model.get("edges") or [0, 1]
    fmt, unit = _fmt(spec)
    groups = model.get("groups") or []
    legend = FR.legend_items(groups, colors(spec, model)) if len(groups) > 1 else []
    return FR.cartesian(box, {"kind": "value", "lo": float(edges[0]), "hi": float(edges[-1]), "zero": False, "fmt": fmt, "unit": unit},
                        {"kind": "value", "lo": 0.0, "hi": float(max(_totals(model) or [1])), "zero": True, "fmt": "integer", "unit": None},
                        legend, spec.get("legend") or "auto", y_title="count")


def draw(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, slot: Tuple[float, float, float, float]) -> List[Dict[str, Any]]:
    pen = DR.Pen(slot[0], slot[1])
    DR.axes(pen, frame)
    px, py, pw, ph = frame.plot
    x_axis, y_axis = frame.axes["x"], frame.axes["y"]
    edges = model.get("edges") or []
    paints = colors(spec, model)
    base = [0] * (len(edges) - 1)
    for g, counts in enumerate(model.get("counts") or []):
        for i, count in enumerate(counts):
            if not count:
                continue
            x0 = DR.value_pos(x_axis, edges[i], px, pw, False)
            x1 = DR.value_pos(x_axis, edges[i + 1], px, pw, False)
            y0 = DR.value_pos(y_axis, base[i], py, ph, True)
            y1 = DR.value_pos(y_axis, base[i] + count, py, ph, True)
            pen.rect(x0 + 0.5, y1, max(0.5, x1 - x0 - 1), max(0.0, y0 - y1), paints[g % len(paints)])
            base[i] += count
    for note in spec.get("annotations") or []:
        if note.get("x") is not None and _data.number(note["x"]) is not None:
            DR.annotation_x(pen, DR.value_pos(x_axis, _data.number(note["x"])[0], px, pw, False), frame, note.get("text") or "")  # type: ignore[index]
    DR.legend(pen, frame)
    return pen.items


def doc(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    edges = model.get("edges") or []
    centres = [_data.compact_number((edges[i] + edges[i + 1]) / 2.0) for i in range(len(edges) - 1)]
    rows = [[c] + [counts[i] for counts in model.get("counts") or []] for i, c in enumerate(centres)]
    dims = ["x"] + ["s{}".format(g) for g in range(len(model.get("counts") or []))]
    return {"datasets": [{"id": "d0", "dimensions": dims, "source": rows}], "refs": {}}


def option(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, gist: Sequence[str]) -> Dict[str, Any]:
    out = O.base(gist, colors(spec, model))
    out.update(grid=O.grid(frame), xAxis=O.axis(frame, "x"), yAxis=O.axis(frame, "y"), legend=O.legend(frame), tooltip=O.tooltip("axis"),
               dataset=O.dataset())
    edges = model.get("edges") or [0, 1]
    x_axis = frame.axes["x"]
    span = float(x_axis.get("max", 1)) - float(x_axis.get("min", 0)) or 1.0
    bar_px = max(1.0, (float(edges[1]) - float(edges[0])) / span * frame.plot[2] - 1.0) if len(edges) > 1 else 4.0
    groups = model.get("groups") or ["count"]
    out["series"] = [{"type": "bar", "name": g, "datasetIndex": 0, "encode": {"x": "x", "y": "s{}".format(i)}, "stack": "bins",
                      "barWidth": round(bar_px, 2), "barGap": "-100%"} for i, g in enumerate(groups)]
    return out


def stats(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    values = [v for v in table.by_name[spec["x"]].values if v is not None]
    out: Dict[str, Any] = {"moments": G.moments(values)}
    totals = _totals(model)
    if totals:
        edges = model.get("edges") or []
        mode = max(range(len(totals)), key=lambda i: (totals[i], -i))
        out["mode"] = [edges[mode], edges[mode + 1], totals[mode]]
        out["bins"] = len(totals)
        out["width"] = _data.compact_number(float(edges[1]) - float(edges[0]))
    return out


def gist(spec: Dict[str, Any], model: Dict[str, Any], stats: Mapping[str, Any]) -> List[str]:
    fmt, unit = _fmt(spec)

    def n(value: Any) -> str:
        return F.fmt_number(value, fmt or ("compact" if abs(float(value)) >= 1e4 else None), unit)

    parts = ["x={} in {} bins of {}".format(spec["x"], stats.get("bins", 0), n(stats.get("width") or 0))]
    asked = spec.get("bins")
    if isinstance(asked, int) and not isinstance(asked, bool) and asked != stats.get("bins"):
        # The edges sit on a nice step, so the count may differ from the one asked for (QA phase34 L4).
        parts[0] += " ({} asked; edges on a round step)".format(asked)
    if model.get("groups"):
        parts.append("color={} ({})".format(spec["color"], len(model["groups"])))
    parts.append(str(spec.get("_source") or ""))
    lines = [G.join(parts)]
    m = stats.get("moments")
    if m:
        skew = {1.0: "skewed right", -1.0: "skewed left"}.get(float(m["skew"]), "symmetric")
        lines.append("n {} · mean {} · median {} · sd {} · {}".format(F.fmt_number(m["n"], "integer"), n(m["mean"]), n(m["median"]), n(m["sd"]), skew))
        lines.append("range {}…{} · mode {}–{} ({})".format(n(m["min"]), n(m["max"]), n(stats["mode"][0]), n(stats["mode"][1]),
                                                           F.fmt_number(stats["mode"][2], "integer")) if stats.get("mode") else
                     "range {}…{}".format(n(m["min"]), n(m["max"])))
    for note in model.get("capped") or []:
        lines.append("capped: " + note)
    return lines


def vegalite(spec: Dict[str, Any], model: Dict[str, Any]) -> Dict[str, Any]:
    edges = model.get("edges") or []
    groups = model.get("groups") or [""]
    rows = [{"lo": edges[i], "hi": edges[i + 1], "group": groups[g], "count": c} for g, counts in enumerate(model.get("counts") or [])
            for i, c in enumerate(counts) if c]
    enc: Dict[str, Any] = {"x": {"field": "lo", "type": "quantitative", "title": spec["x"], "bin": {"binned": True}}, "x2": {"field": "hi"},
                           "y": {"field": "count", "type": "quantitative", "stack": True}}
    if model.get("groups"):
        enc["color"] = {"field": "group", "type": "nominal", "title": spec.get("color")}
    return VL.compat("bar", rows, enc, {"width": "container"})


def min_box(spec: Dict[str, Any], model: Dict[str, Any]) -> Tuple[float, float]:
    return max(300.0, (len(model.get("edges") or []) - 1) * FR.BAR_MIN_PX + 90), 200.0


CHARTS = (
    ChartType(name="histogram",
              channels=(Channel("x", required=True, types=("quantitative",), doc="the measure to bin"),
                        Channel("color", types=("nominal", "ordinal"), doc="stacked groups (up to 6)")),
              options=("bins",), frame_kind="cartesian", model=model, stats=stats, doc=doc, frame=frame, option=option, draw=draw,
              gist=gist, vegalite=vegalite,
              echarts=("BarChart", "GridComponent", "DatasetComponent", "LegendComponent", "TooltipComponent", "AriaComponent"),
              min_box=min_box, default_box=(560, 340), caps="60 bins × 6 groups", doc_line="how one measure is distributed, in equal bins",
              example='{"op": "chart", "id": "lat", "intent": "how slow are requests", "title": "Request latency", "type": "histogram", '
                      '"rows": [{"ms": 110}, {"ms": 124}, {"ms": 131}, {"ms": 118}, {"ms": 142}, {"ms": 155}, {"ms": 128}, {"ms": 138}, '
                      '{"ms": 121}, {"ms": 176}, {"ms": 133}, {"ms": 149}, {"ms": 240}, {"ms": 127}, {"ms": 119}, {"ms": 136}], '
                      '"x": "ms", "units": {"x": "ms"}}',
              order=70),
)
