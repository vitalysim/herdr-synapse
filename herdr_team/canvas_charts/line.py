"""Line charts (canvas v2 phase 3, 2.4): a measure along time, a number or ordered categories, one line per series.

``x`` is time (a date column: drawn on a calendar axis), a number, or ordered
categories; ``y`` the measure (summed when rows repeat); ``color`` splits it
into up to 12 lines (the rest fold into "Other"). Long series are thinned by
LTTB (Largest-Triangle-Three-Buckets) for drawing: 2,000 points a series on
the page, fewer in the Python drawing; the gist always reads every point.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Sequence, Tuple

from herdr_team.canvas_charts import Channel, ChartType, _data
from herdr_team.canvas_charts import _xy as XY
from herdr_team.canvas_charts._frame import Frame

ORDER = 20


def model(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
    return XY.model(spec, table, stacked=False)


def frame(spec: Dict[str, Any], model: Dict[str, Any], box: Tuple[float, float]) -> Frame:
    return XY.frame(spec, model, box, zero=False)


def draw(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, slot: Tuple[float, float, float, float]) -> List[Dict[str, Any]]:
    return XY.draw(spec, model, frame, slot, area=False)


def option(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, gist: Sequence[str]) -> Dict[str, Any]:
    return XY.option(spec, model, frame, gist, area=False)


def vegalite(spec: Dict[str, Any], model: Dict[str, Any]) -> Dict[str, Any]:
    return XY.vegalite(spec, model, "line")


def gist(spec: Dict[str, Any], model: Dict[str, Any], stats: Mapping[str, Any]) -> List[str]:
    return XY.gist(spec, model, stats)


CHARTS = (
    ChartType(name="line",
              channels=(Channel("x", required=True, types=("temporal", "quantitative", "ordinal"),
                                doc="time, a number or ordered categories along the bottom"),
                        Channel("y", required=True, types=("quantitative",), aggregate=True, doc="the measure (summed when rows repeat)"),
                        Channel("color", types=("nominal", "ordinal"), doc="one line per value (series)")),
              options=("smooth", "points"), frame_kind="cartesian", model=model, stats=XY.stats, doc=XY.doc, frame=frame, option=option,
              draw=draw, gist=gist, vegalite=vegalite,
              echarts=("LineChart", "GridComponent", "DatasetComponent", "LegendComponent", "TooltipComponent", "MarkLineComponent",
                       "AriaComponent"),
              min_box=XY.min_box, default_box=(560, 360), caps="12 series × 2,000 points (LTTB)", doc_line="a measure over time or an ordered axis, one line per series",
              example='{"op": "chart", "id": "p95", "intent": "latency after the cache change", "title": "p95 latency (ms)", "type": "line", '
                      '"rows": [{"day": "2026-09-01", "p95": 412}, {"day": "2026-09-02", "p95": 405}, {"day": "2026-09-03", "p95": 398}, '
                      '{"day": "2026-09-04", "p95": 251}, {"day": "2026-09-05", "p95": 240}, {"day": "2026-09-06", "p95": 236}], '
                      '"x": "day", "y": "p95", "units": {"y": "ms"}, "annotations": [{"x": "2026-09-04", "text": "cache on"}]}',
              order=20),
)
