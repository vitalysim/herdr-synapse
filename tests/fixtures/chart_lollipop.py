"""A chart type dropped in as one module (canvas v2 phase 3, 8.1, the one-module proof): a lollipop, a dot on a stem per
category. Loaded only by ``tests/test_charts_registry.py`` through ``canvas_charts._load_extra``; it reuses the shared
helpers and adds one option of its own (``dot``), which the chart op then takes with no other file changed."""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence, Tuple

from herdr_team.canvas_charts import Channel, ChartType, _data
from herdr_team.canvas_charts import _draw as DR
from herdr_team.canvas_charts import _frame as FR
from herdr_team.canvas_charts import _option as O
from herdr_team.canvas_charts import _scale as S
from herdr_team.canvas_charts import _series as SE
from herdr_team.canvas_charts._frame import Frame

ORDER = 5000


def _dot(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not 4 <= value <= 20:
        raise _data.invalid(name, "dot is the dot's size in px, 4 to 20")
    return float(value)


def model(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
    found = SE.matrix(spec, table, spec["x"], spec.get("y"), None, cap_x=40, cap_series=1)
    return {"cats": found["cats"], "values": SE.compacted(found["series"])[0]["values"], "how": found["how"], "capped": found["capped"],
            "_warnings": found["_warnings"]}


def frame(spec: Dict[str, Any], model: Dict[str, Any], box: Tuple[float, float]) -> Frame:
    lo, hi = S.value_range([v for v in model["values"] if v is not None], True)
    return FR.cartesian(box, {"kind": "category", "labels": model["cats"]}, {"kind": "value", "lo": lo, "hi": hi, "zero": True}, (), "none",
                        y_title=spec["y"])


def draw(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, slot: Tuple[float, float, float, float]) -> List[Dict[str, Any]]:
    pen = DR.Pen(slot[0], slot[1])
    DR.axes(pen, frame)
    px, py, pw, ph = frame.plot
    axis = frame.axes["y"]
    size = float(spec.get("dot") or 10)
    for index, value in enumerate(model["values"]):
        if value is None:
            continue
        x = DR.band_centre(index, len(model["values"]), px, pw)
        y0, y1 = DR.value_pos(axis, 0, py, ph, True), DR.value_pos(axis, value, py, ph, True)
        pen.line([[x, y0], [x, y1]], "chart.cat.0", 2)
        pen.rect(x - size / 2, y1 - size / 2, size, size, "chart.cat.0", r=size / 2)
    return pen.items


def doc(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    return {"datasets": [{"id": "d0", "dimensions": ["x", "y"], "source": [[c, v] for c, v in zip(model["cats"], model["values"])]}], "refs": {}}


def option(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, gist: Sequence[str]) -> Dict[str, Any]:
    out = O.base(gist)
    out.update(grid=O.grid(frame), xAxis=O.axis(frame, "x"), yAxis=O.axis(frame, "y"), tooltip=O.tooltip("axis"), dataset=O.dataset())
    out["series"] = [{"type": "scatter", "datasetIndex": 0, "encode": {"x": "x", "y": "y"}, "symbolSize": float(spec.get("dot") or 10)}]
    return out


def gist(spec: Dict[str, Any], model: Dict[str, Any], stats: Mapping[str, Any]) -> List[str]:
    pairs = [(c, v) for c, v in zip(model["cats"], model["values"]) if v is not None]
    top = max(pairs, key=lambda kv: kv[1]) if pairs else ("-", 0)
    return ["lollipop x={} ({}) · y={}".format(spec["x"], len(model["cats"]), spec["y"]), "max {} {}".format(top[0], top[1])]


CHARTS = (
    ChartType(name="lollipop", channels=(Channel("x", required=True, types=("nominal", "ordinal")), Channel("y", required=True, types=("quantitative",),
                                                                                                         aggregate=True)),
              options=("dot",), option_rules={"dot": _dot}, model=model, doc=doc, frame=frame, option=option, draw=draw, gist=gist,
              echarts=("ScatterChart", "GridComponent", "DatasetComponent", "TooltipComponent", "AriaComponent"),
              doc_line="a dot on a stem per category",
              example='{"op": "chart", "id": "pop", "intent": "t", "title": "Stars by repo", "type": "lollipop", '
                      '"rows": [{"repo": "a", "stars": 30}, {"repo": "b", "stars": 12}, {"repo": "c", "stars": 21}], "x": "repo", "y": "stars", "dot": 12}',
              order=5000),
)
