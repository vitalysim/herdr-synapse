"""Area charts (canvas v2 phase 3, 2.4): lines with the band under them filled, stacked or to 100 %.

The channels are the line chart's. ``stack: true`` piles the series up (every
series on the union of x values, a missing one counted 0); ``stack: "percent"``
makes each x add up to 100 %, and the gist then says the shares at the last x.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence, Tuple

from herdr_team.canvas_charts import Channel, ChartType, _data
from herdr_team.canvas_charts import _xy as XY
from herdr_team.canvas_charts._frame import Frame

ORDER = 30


def model(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
    return XY.model(spec, table, stacked=bool(spec.get("stack")))


def frame(spec: Dict[str, Any], model: Dict[str, Any], box: Tuple[float, float]) -> Frame:
    return XY.frame(spec, model, box, zero=True)


def draw(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, slot: Tuple[float, float, float, float]) -> List[Dict[str, Any]]:
    return XY.draw(spec, model, frame, slot, area=True)


def option(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, gist: Sequence[str]) -> Dict[str, Any]:
    return XY.option(spec, model, frame, gist, area=True)


def vegalite(spec: Dict[str, Any], model: Dict[str, Any]) -> Dict[str, Any]:
    return XY.vegalite(spec, model, "area")


def gist(spec: Dict[str, Any], model: Dict[str, Any], stats: Mapping[str, Any]) -> List[str]:
    return XY.gist(spec, model, stats)


CHARTS = (
    ChartType(name="area",
              channels=(Channel("x", required=True, types=("temporal", "quantitative", "ordinal"),
                                doc="time, a number or ordered categories along the bottom"),
                        Channel("y", required=True, types=("quantitative",), aggregate=True, doc="the measure (summed when rows repeat)"),
                        Channel("color", types=("nominal", "ordinal"), doc="one band per value (series)")),
              options=("stack", "smooth"), frame_kind="cartesian", model=model, stats=XY.stats, doc=XY.doc, frame=frame, option=option,
              draw=draw, gist=gist, vegalite=vegalite,
              echarts=("LineChart", "GridComponent", "DatasetComponent", "LegendComponent", "TooltipComponent", "MarkLineComponent",
                       "AriaComponent"),
              min_box=XY.min_box, default_box=(560, 360), caps="12 series × 2,000 points (LTTB)", doc_line="a measure over time with the area under it, stacked or to 100 %",
              example='{"op": "chart", "id": "mix-trend", "intent": "how the traffic mix moved", "title": "Visits by channel", "type": "area", '
                      '"rows": [{"week": "2026-W30", "channel": "Search", "visits": 520}, {"week": "2026-W30", "channel": "Social", "visits": 210}, '
                      '{"week": "2026-W31", "channel": "Search", "visits": 560}, {"week": "2026-W31", "channel": "Social", "visits": 260}, '
                      '{"week": "2026-W32", "channel": "Search", "visits": 610}, {"week": "2026-W32", "channel": "Social", "visits": 330}], '
                      '"x": "week", "y": "visits", "color": "channel", "stack": true}',
              order=30),
)
