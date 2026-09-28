"""Funnels (canvas v2 phase 3, 2.4): stages of a process and how many make it from one to the next.

``category`` names the stages and ``value`` counts them (summed when rows
repeat). Stages run biggest first (``sort: "none"`` keeps the data's order, for
a funnel whose stages are not monotone); at most 12. Each band is as wide as its
stage and narrows to the next one; the gist gives each step's conversion, the
biggest drop and the overall conversion.
"""
from __future__ import annotations

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

ORDER = 90
CAP = 12
GAP = 2.0
#: The last stage is never narrower than this share of the widest.
MIN_SHARE = 0.08


def normalize(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
    value = spec.get("value")
    if value and any(v is not None and v < 0 for v in table.by_name[value].values):
        raise _data.refuse("chart_refused", "a funnel counts what reaches each stage; {} has negative values".format(value), field="value")
    return spec


def model(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
    order = spec.get("sort") or "-value"
    found = SE.pairs(dict(spec, other=False), table, spec["category"], spec.get("value"), cap=CAP, top=spec.get("top"),
                     order="none" if order == "none" else order)
    capped = [c.replace(" (other: false)", "") for c in found["capped"]]
    return {"stages": found["items"], "how": found["how"], "capped": capped,
            "_warnings": [{"code": "chart_capped", "message": c} for c in capped]}


def colors(spec: Mapping[str, Any], model: Mapping[str, Any]) -> List[str]:
    names = [k for k, _v in model.get("stages") or []]
    wanted = set(spec.get("highlight") or [])
    if wanted and wanted & set(names):
        return ["chart.highlight" if n in wanted else "chart.dim" for n in names]
    return [S.cat(i) for i in range(len(names))]


def frame(spec: Dict[str, Any], model: Dict[str, Any], box: Tuple[float, float]) -> Frame:
    mode = spec.get("legend") or "auto"
    names = [k for k, _v in model.get("stages") or []]
    base = FR.radial(box, FR.legend_items(names, colors(spec, model)), mode if mode != "auto" else "none")
    count = max(1, len(names))
    px, py, pw, ph = base.plot
    along = pw if spec.get("horizontal") else ph
    band = (along - GAP * (count - 1)) / count
    legible = base.legible and band >= FR.LH + 2
    notes = list(base.notes) + ([] if band >= FR.LH + 2 else ["stages are only {} px apart".format(int(band))])
    return Frame(box=base.box, plot=base.plot, axes={}, legend=base.legend, font=base.font, notes=tuple(notes), legible=legible,
                 extra={"band": band})


def _widths(model: Mapping[str, Any]) -> List[float]:
    values = [float(v or 0.0) for _k, v in model.get("stages") or []]
    top = max(values) if values else 1.0
    return [max(MIN_SHARE, v / top) if top else MIN_SHARE for v in values]


def _label(spec: Mapping[str, Any], name: str, value: float) -> str:
    return "{} {}".format(name, G.num(value, dict(spec), "value"))


def draw(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, slot: Tuple[float, float, float, float]) -> List[Dict[str, Any]]:
    pen = DR.Pen(slot[0], slot[1])
    px, py, pw, ph = frame.plot
    stages = model.get("stages") or []
    widths = _widths(model)
    paints = colors(spec, model)
    band = float(frame.extra.get("band") or 20.0)
    horizontal = bool(spec.get("horizontal"))
    for index, (name, value) in enumerate(stages):
        top = widths[index]
        bottom = widths[index + 1] if index + 1 < len(widths) else widths[index] * 0.85
        start = index * (band + GAP)
        if horizontal:
            cy = py + ph / 2.0
            x0, x1 = px + start, px + start + band
            d = "M {} L {} L {} L {} Z".format(pen.pt(x0, cy - ph * top / 2.0), pen.pt(x1, cy - ph * bottom / 2.0),
                                               pen.pt(x1, cy + ph * bottom / 2.0), pen.pt(x0, cy + ph * top / 2.0))
            pen.path(d, paints[index % len(paints)])
            text = FR.fit_text(_label(spec, name, float(value or 0.0)), band - 6)
            pen.text(text, (x0 + x1) / 2.0, cy, "middle", S.on(paints[index]), FR.FONT, 600)
        else:
            cx = px + pw / 2.0
            y0, y1 = py + start, py + start + band
            d = "M {} L {} L {} L {} Z".format(pen.pt(cx - pw * top / 2.0, y0), pen.pt(cx + pw * top / 2.0, y0),
                                               pen.pt(cx + pw * bottom / 2.0, y1), pen.pt(cx - pw * bottom / 2.0, y1))
            pen.path(d, paints[index % len(paints)])
            text = _label(spec, name, float(value or 0.0))
            room = pw * min(top, bottom) - 8
            if FR.text_w(text) <= room:
                pen.text(text, cx, (y0 + y1) / 2.0, "middle", S.on(paints[index]), FR.FONT, 600)
            else:
                pen.text(FR.fit_text(text, max(40.0, pw / 2.0 - pw * top / 2.0 - 8)), cx + pw * max(top, bottom) / 2.0 + 6, (y0 + y1) / 2.0,
                         "start", "chart.ink")
    DR.legend(pen, frame)
    return pen.items


def doc(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    return {"datasets": [], "refs": {"stages": [{"name": k, "value": v, "label": {"formatter": _label(spec, k, float(v or 0.0))}}
                                                for k, v in model.get("stages") or []]}}


def option(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, gist: Sequence[str]) -> Dict[str, Any]:
    out = O.base(gist, colors(spec, model))
    px, py, pw, ph = frame.plot
    out.update(legend=O.legend(frame), tooltip=O.tooltip("item"))
    widths = _widths(model)
    out["series"] = [{"type": "funnel", "name": spec.get("category"), "left": round(px, 2), "top": round(py, 2), "width": round(pw, 2),
                      "height": round(ph, 2), "sort": "none", "gap": GAP, "orient": "horizontal" if spec.get("horizontal") else "vertical",
                      "minSize": "{}%".format(round(min(widths or [1]) * 100, 1)), "maxSize": "100%", "data": {"$doc": "stages"}, "color": colors(spec, model),
                      "label": {"show": True, "position": "inside", "fontFamily": "$sans", "fontSize": FR.FONT,
                                "fontWeight": 600},
                      "labelLine": {"show": False}, "itemStyle": {"borderWidth": 0}}]
    return out


def stats(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    stages = [(k, float(v or 0.0)) for k, v in model.get("stages") or []]
    steps = []
    for (a, va), (b, vb) in zip(stages, stages[1:]):
        steps.append([a, b, (vb / va) if va else None, vb - va])
    out: Dict[str, Any] = {"steps": steps, "stages": len(stages)}
    if len(stages) >= 2 and stages[0][1]:
        out["overall"] = stages[-1][1] / stages[0][1]
    drops = [s for s in steps if s[2] is not None]
    if drops:
        worst = min(drops, key=lambda s: (s[2], s[0]))
        out["drop"] = worst
    return out


def gist(spec: Dict[str, Any], model: Dict[str, Any], stats: Mapping[str, Any]) -> List[str]:
    stages = model.get("stages") or []
    lines = [G.join(["category={} ({} stages)".format(spec["category"], len(stages)),
                     "value={}".format(SE.measure_label(spec, spec.get("value"), str(model.get("how") or "none"))), str(spec.get("_source") or "")])]
    if stages:
        lines.append(" → ".join("{} {}".format(k, G.num(float(v or 0.0), dict(spec), "value")) for k, v in stages[:8]))
    steps = [s for s in stats.get("steps") or [] if s[2] is not None]
    if steps:
        lines.append("conversion: " + " · ".join("{}→{} {}".format(a, b, F.share(r)) for a, b, r, _d in steps[:6]))
    tail = []
    if stats.get("drop"):
        a, b, r, _d = stats["drop"]
        tail.append("biggest drop {}→{} (keeps {})".format(a, b, F.share(r)))
    if stats.get("overall") is not None:
        tail.append("overall {}".format(F.share(float(stats["overall"]))))
    if tail:
        lines.append(G.join(tail))
    for note in model.get("capped") or []:
        lines.append("capped: " + note)
    return lines


def vegalite(spec: Dict[str, Any], model: Dict[str, Any]) -> Dict[str, Any]:
    rows = [{"stage": k, "value": v} for k, v in model.get("stages") or []]
    return VL.compat("bar", rows, {"y": {"field": "stage", "type": "nominal", "sort": None, "title": spec.get("category")},
                                   "x": {"field": "value", "type": "quantitative", "title": spec.get("value") or "count"}}, {"width": "container"})


def min_box(spec: Dict[str, Any], model: Dict[str, Any]) -> Tuple[float, float]:
    count = len(model.get("stages") or [])
    along = count * (FR.LH + 2 + GAP) + 20
    return (max(280.0, along), 200.0) if spec.get("horizontal") else (280.0, max(160.0, along))


CHARTS = (
    ChartType(name="funnel",
              channels=(Channel("category", required=True, types=("nominal", "ordinal"), doc="the stages"),
                        Channel("value", required=True, types=("quantitative",), aggregate=True, doc="how many reach each stage")),
              options=("horizontal",), frame_kind="radial", normalize=normalize, model=model, stats=stats, doc=doc, frame=frame, option=option,
              draw=draw, gist=gist, vegalite=vegalite, echarts=("FunnelChart", "LegendComponent", "TooltipComponent", "AriaComponent"),
              min_box=min_box, default_box=(480, 340), caps="12 stages", doc_line="stages of a process and the conversion between them",
              example='{"op": "chart", "id": "signup", "intent": "where people drop out of signup", "title": "Signup funnel", "type": "funnel", '
                      '"rows": [{"stage": "Visit", "n": 12000}, {"stage": "Signup form", "n": 5400}, {"stage": "Verified", "n": 4300}, '
                      '{"stage": "First project", "n": 2100}], "category": "stage", "value": "n"}',
              order=90),
)
