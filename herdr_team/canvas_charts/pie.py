"""Pie and donut charts (canvas v2 phase 3, 2.4): shares of a whole, the biggest first, the tail folded into "Other".

``category`` names the slices and ``value`` sizes them (summed when rows
repeat). By default the 6 biggest slices are drawn and the rest become "Other"
(``top`` asks for more or fewer, at most 12; ``other: false`` drops them). A
``donut`` is a pie with a hole (``inner``, 0.3 to 0.8 of the radius) that shows
the total. Slices read clockwise from 12 o'clock; each one's share is written
on it when it has room, and the legend names them.
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

ORDER = 40
CAP = 12
DEFAULT_TOP = 6
DONUT_INNER = 0.55
#: A slice gets its share written on it when it is at least this big a part of the whole.
LABEL_SHARE = 0.05
#: More slices than this reads badly (the check ``chart_pie_slices``).
MAX_SLICES = 7


def normalize(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
    spec.setdefault("top", DEFAULT_TOP)
    value = spec.get("value")
    if value:
        negative = [v for v in table.by_name[value].values if v is not None and v < 0]
        if negative:
            raise _data.refuse("chart_refused", "a {} shows parts of a whole, and {} has {} negative value{}; filter them out or use a bar "
                               "chart".format(spec["type"], value, len(negative), "" if len(negative) == 1 else "s"), field="value")
    return spec


def model(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
    found = SE.pairs(spec, table, spec["category"], spec.get("value"), cap=CAP, top=spec.get("top"), order="-value")
    return {"slices": found["items"], "how": found["how"], "capped": found["capped"], "total": _data.compact_number(found["total"]),
            "count": found["count"], "_warnings": found["_warnings"]}


def _shares(model: Mapping[str, Any]) -> List[float]:
    values = [max(0.0, float(v or 0.0)) for _k, v in model.get("slices") or []]
    total = math.fsum(values)
    return [v / total if total else 0.0 for v in values]


def colors(spec: Mapping[str, Any], model: Mapping[str, Any]) -> List[str]:
    names = [k for k, _v in model.get("slices") or []]
    wanted = set(spec.get("highlight") or [])
    if wanted and wanted & set(names):
        return ["chart.highlight" if n in wanted else "chart.dim" for n in names]
    return [S.cat(i) for i in range(len(names))]


def frame(spec: Dict[str, Any], model: Dict[str, Any], box: Tuple[float, float]) -> Frame:
    names = [k for k, _v in model.get("slices") or []]
    items = FR.legend_items(names, colors(spec, model))
    mode = spec.get("legend") or "auto"
    base = FR.radial(box, items, mode if not (mode == "auto" and len(items) == 1) else "none")
    px, py, pw, ph = base.plot
    r = max(8.0, (min(pw, ph) / 2.0 - 4.0) / DR.ARC_OVERSHOOT)
    inner = float(spec.get("inner") or (DONUT_INNER if spec.get("type") == "donut" else 0.0))
    extra = {"cx": px + pw / 2.0, "cy": py + ph / 2.0, "r": r, "r0": r * inner}
    notes = list(base.notes)
    legible = base.legible and r >= 40
    if r < 40:
        notes.append("the {} is only {} px across".format(spec["type"], int(2 * r)))
    return Frame(box=base.box, plot=base.plot, axes={}, legend=base.legend, font=base.font, notes=tuple(notes), legible=legible, extra=extra)


def _angles(model: Mapping[str, Any]) -> List[Tuple[float, float]]:
    out = []
    start = 0.0
    for share in _shares(model):
        end = start + share * 2.0 * math.pi
        out.append((start, end))
        start = end
    return out


def draw(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, slot: Tuple[float, float, float, float]) -> List[Dict[str, Any]]:
    pen = DR.Pen(slot[0], slot[1])
    cx, cy, r, r0 = (float(frame.extra[k]) for k in ("cx", "cy", "r", "r0"))
    paints = colors(spec, model)
    shares = _shares(model)
    for index, (a0, a1) in enumerate(_angles(model)):
        if a1 - a0 <= 1e-9:
            continue
        if a1 - a0 >= 2 * math.pi - 1e-9:
            a1 = a0 + 2 * math.pi - 1e-4
        pen.path(DR.arc_path(pen, cx, cy, r0, r, a0, a1), paints[index % len(paints)], "chart.paper", 1.5)
    if (spec.get("labels") or "auto") != "none":
        mid_r = (r0 + r) / 2.0 if r0 > 0 else r * 0.62
        for index, (a0, a1) in enumerate(_angles(model)):
            share = shares[index]
            if share < LABEL_SHARE and spec.get("labels") != "values":
                continue
            mid = (a0 + a1) / 2.0
            text = F.share(share)
            x, y = cx + mid_r * math.sin(mid), cy - mid_r * math.cos(mid)
            if FR.text_w(text) + 6 > (a1 - a0) * mid_r and spec.get("labels") != "values":
                continue
            pen.text(text, x, y, "middle", S.on(paints[index]), FR.FONT, 600)
    if r0 > 0 and FR.text_w(_total_text(spec, model)) < 2 * r0 - 8:
        pen.text(_total_text(spec, model), cx, cy - FR.LH / 2.0, "middle", "chart.ink", 16, 600)
        pen.text("total", cx, cy + FR.LH / 2.0 + 2, "middle", "chart.muted", FR.FONT)
    DR.legend(pen, frame)
    return pen.items


def _total_text(spec: Mapping[str, Any], model: Mapping[str, Any]) -> str:
    return G.num(float(model.get("total") or 0.0), dict(spec), "value")


def doc(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    """The slices with their share written as each one's label (colours and which labels show are the option's: a visual
    change never rewrites the doc)."""
    return {"datasets": [], "refs": {"slices": [{"name": name, "value": value, "label": {"formatter": F.share(share)}}
                                                for (name, value), share in zip(model.get("slices") or [], _shares(model))]}}


def option(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, gist: Sequence[str]) -> Dict[str, Any]:
    out = O.base(gist, colors(spec, model))
    cx, cy, r, r0 = (float(frame.extra[k]) for k in ("cx", "cy", "r", "r0"))
    out.update(legend=O.legend(frame), tooltip=O.tooltip("item"))
    out["series"] = [{"type": "pie", "name": spec.get("category"), "center": [round(cx, 2), round(cy, 2)], "radius": [round(r0, 2), round(r, 2)],
                      "startAngle": 90, "clockwise": True, "avoidLabelOverlap": False, "data": {"$doc": "slices"}, "color": colors(spec, model),
                      "minShowLabelAngle": 0 if spec.get("labels") == "values" else round(LABEL_SHARE * 360, 2),
                      "label": {"show": (spec.get("labels") or "auto") != "none", "position": "inside", "fontFamily": "$sans", "fontSize": FR.FONT,
                                "fontWeight": 600},
                      "labelLine": {"show": False}, "itemStyle": {"borderColor": "chart.paper", "borderWidth": 1.5}}]
    return out


def stats(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    slices = model.get("slices") or []
    total = float(model.get("total") or 0.0)
    return {"total": total, "shares": [[k, (float(v or 0.0) / total if total else 0.0)] for k, v in slices], "count": model.get("count")}


def gist(spec: Dict[str, Any], model: Dict[str, Any], stats: Mapping[str, Any]) -> List[str]:
    slices = model.get("slices") or []
    count = int(stats.get("count") or len(slices))
    shown = "{} → {} + Other".format(count, len(slices) - 1) if slices and slices[-1][0] == SE.OTHER and count > len(slices) - 1 else str(count)
    head = SE.G.join(["category={} ({})".format(spec["category"], shown),
                      "value={}".format(SE.measure_label(spec, spec.get("value"), str(model.get("how") or "none"))), str(spec.get("_source") or "")])
    shares = [(k, v) for k, v in stats.get("shares") or []]
    top = [(k, v) for k, v in shares if k != SE.OTHER][:3]
    parts = ["{} {}".format(k, F.share(v)) for k, v in top]
    other = next((v for k, v in shares if k == SE.OTHER), None)
    if other is not None:
        parts.append("Other {}".format(F.share(other)))
    parts.append("total {}".format(G.num(float(stats.get("total") or 0.0), dict(spec), "value")))
    lines = [head, G.join(parts)]
    for note in model.get("capped") or []:
        lines.append("capped: " + note)
    return lines


def slices_check(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame) -> List[Dict[str, Any]]:
    """``chart_pie_slices``: more than 7 slices, or a labelled slice under 2 %."""
    slices = model.get("slices") or []
    shares = _shares(model)
    tiny = [k for (k, _v), s in zip(slices, shares) if s < 0.02]
    if len(slices) > MAX_SLICES:
        fix = {"set": {"type": "bar"}} if len(slices) > 12 else {"set": {"top": DEFAULT_TOP}}
        return [{"code": "chart_pie_slices", "message": "{} slices are too many to compare as angles; keep the top {} (the rest become "
                 "Other){}".format(len(slices), DEFAULT_TOP, ", or draw it as bars" if len(slices) > 12 else ""), "fix": fix}]
    if tiny and spec.get("labels") == "values":
        return [{"code": "chart_pie_slices", "message": "{} slice{} under 2 % carr{} a label ({})".format(
            len(tiny), "" if len(tiny) == 1 else "s", "ies" if len(tiny) == 1 else "y", ", ".join(tiny[:4])), "fix": {"set": {"labels": "auto"}}}]
    return []


def vegalite(spec: Dict[str, Any], model: Dict[str, Any]) -> Dict[str, Any]:
    rows = [{"category": k, "value": v} for k, v in model.get("slices") or []]
    mark: Dict[str, Any] = {"type": "arc"}
    if spec.get("inner") or spec.get("type") == "donut":
        mark["innerRadius"] = 50
    return VL.compat(mark, rows, {"theta": {"field": "value", "type": "quantitative", "stack": True},
                                  "color": {"field": "category", "type": "nominal", "sort": None, "title": spec.get("category")},
                                  "order": {"field": "value", "type": "quantitative", "sort": "descending"}}, {"width": "container"})


def min_box(spec: Dict[str, Any], model: Dict[str, Any]) -> Tuple[float, float]:
    return 280.0, 200.0


_CHANNELS = (Channel("category", required=True, types=("nominal", "ordinal", "temporal"), doc="the slices"),
             Channel("value", required=True, types=("quantitative",), aggregate=True, doc="the slice sizes (summed when rows repeat)"))
_MODULES = ("PieChart", "LegendComponent", "TooltipComponent", "AriaComponent")

CHARTS = (
    ChartType(name="pie", channels=_CHANNELS, options=(), frame_kind="radial", normalize=normalize, model=model, stats=stats, doc=doc,
              frame=frame, option=option, draw=draw, gist=gist, vegalite=vegalite, checks=(slices_check,), echarts=_MODULES, min_box=min_box,
              default_box=(420, 320), caps="12 slices (6 and Other by default)", doc_line="shares of a whole (up to 6 slices and Other by default)",
              example='{"op": "chart", "id": "share", "intent": "who sells the most", "title": "Revenue share by region", "type": "pie", '
                      '"data": "rev.csv", "category": "region", "value": "revenue"}',
              order=40),
    ChartType(name="donut", aliases=("doughnut",), channels=_CHANNELS, options=("inner",), frame_kind="radial", normalize=normalize,
              model=model, stats=stats, doc=doc, frame=frame, option=option, draw=draw, gist=gist, vegalite=vegalite, checks=(slices_check,),
              echarts=_MODULES, min_box=min_box, default_box=(420, 320), caps="12 slices (6 and Other by default)", doc_line="a pie with a hole that shows the total",
              example='{"op": "chart", "id": "mix", "intent": "traffic share by channel", "title": "Visits by channel", "type": "donut", '
                      '"data": "traffic.json", "category": "channel", "value": "visits", "top": 5}',
              order=41),
)
