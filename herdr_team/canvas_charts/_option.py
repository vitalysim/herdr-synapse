"""ECharts option pieces from a ``Frame`` (canvas v2 phase 3, 2.6): the Python side of the option contract.

The option an element stores is complete except for three kinds of markers
the page resolves (``web/src/v2/charts/resolve.js``): token references
(``chart.cat.3``, any string matching ``^(chart|tone|base|mat)\\.[a-z0-9_.]+$``)
become the theme's colours, ``{"$doc": name}`` becomes the doc asset's
``refs[name]`` (``datasets`` is the doc's own datasets), ``"$sans"``/``"$mono"``
become the page's fonts, and ``{"$fmt": "<format>|<unit>"}`` becomes one of the
fixed formatters in ``charts/format.js``. No function, no data, no remote
reference ever travels in an option.

Fixed keys everywhere: ``animation: false``, ``tooltip`` in ``richText`` and
``confine``, ``aria`` from the gist, ``grid`` in px from the frame, explicit axis
``min``/``max``/``interval``, the left axis's title as its ``name``, and no ``toolbox``, ``title`` or ``graphic``.

Pure, stdlib only.
"""
from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence

from herdr_team.canvas_charts import _frame as FR
from herdr_team.canvas_charts._frame import Frame

TEXT = {"fontFamily": "$sans", "fontSize": FR.FONT, "color": "chart.muted"}


def base(gist: Sequence[str], colors: Optional[Sequence[str]] = None) -> Dict[str, Any]:
    """The keys every option carries."""
    return {"animation": False, "backgroundColor": "transparent", "color": list(colors or ["chart.cat.{}".format(i) for i in range(10)]),
            "textStyle": {"fontFamily": "$sans", "fontSize": FR.FONT, "color": "chart.ink"},
            "aria": {"enabled": True, "label": {"description": aria(gist)}}}


def size_maps(colors: Sequence[str], count: int, dimension: int, lo: float, hi: float, sizes: Sequence[float]) -> List[Dict[str, Any]]:
    """A bubble size channel as one hidden ``visualMap`` per series, each keeping its series' colour (QA phase34 M6). A
    map that sets only ``symbolSize`` would take the page theme's colour ramp too (ECharts fills a component's missing
    keys from the theme), and every point would be coloured by its size instead of by its series."""
    out = []
    for index in range(max(1, count)):
        color = colors[index % len(colors)] if colors else "chart.cat.0"
        out.append({"show": False, "type": "continuous", "dimension": dimension, "min": lo, "max": hi if hi > lo else lo + 1,
                    "seriesIndex": index, "inRange": {"symbolSize": [sizes[0], sizes[1]], "color": [color, color]}})
    return out


def aria(gist: Sequence[str]) -> str:
    text = " ".join(str(line) for line in gist)
    return text if len(text) <= 900 else text[:899] + "…"


def tooltip(trigger: str = "axis") -> Dict[str, Any]:
    return {"trigger": trigger, "renderMode": "richText", "confine": True, "textStyle": {"fontFamily": "$sans", "fontSize": 12}}


def grid(frame: Frame) -> Dict[str, Any]:
    px, py, pw, ph = frame.plot
    return {"left": round(px, 2), "top": round(py, 2), "width": round(pw, 2), "height": round(ph, 2), "containLabel": False}


def _label_style() -> Dict[str, Any]:
    return dict(TEXT)


def axis(frame: Frame, name: str, dataset_dim: Optional[str] = None) -> Dict[str, Any]:
    """An ``xAxis`` or ``yAxis`` from the frame's axis ``name`` (``x`` or ``y``), wherever it sits."""
    found = frame.axes.get(name) or {}
    pos = found.get("pos") or ("bottom" if name == "x" else "left")
    out: Dict[str, Any] = {"position": pos, "axisLine": {"show": True, "lineStyle": {"color": "chart.axis"}},
                           "axisTick": {"show": found.get("kind") == "category", "alignWithLabel": True, "length": FR.TICK,
                                        "lineStyle": {"color": "chart.axis"}},
                           "splitLine": {"show": found.get("kind") == "value", "lineStyle": {"color": "chart.gridline"}}}
    label = _label_style()
    label["margin"] = FR.TICK + FR.GAP
    if found.get("kind") == "value":
        out.update(type="value", min=found.get("min"), max=found.get("max"))
        label["formatter"] = {"$fmt": found.get("fmt") or "auto|"}
        out["axisLine"]["show"] = pos == "bottom"
        if found.get("time"):
            # Calendar ticks (month starts, years): not evenly spaced, so they travel as custom values.
            values = [t["v"] for t in found.get("ticks") or []]
            label["customValues"] = values
            out["axisTick"] = {"show": True, "customValues": values, "length": FR.TICK, "lineStyle": {"color": "chart.axis"}}
            out["splitLine"] = {"show": False}
        else:
            out["interval"] = found.get("step")
    else:
        out["type"] = "category"
        out["boundaryGap"] = True
        label["interval"] = int(found.get("interval") or 0)
        label["rotate"] = int(found.get("rotate") or 0)
        width = found.get("width")
        if width:
            label.update(width=round(float(width), 2), overflow="truncate", ellipsis="…")
        if pos == "left":
            out["inverse"] = True
    out["axisLabel"] = label
    if dataset_dim is not None:
        out["name"] = ""
    if found.get("title") and pos == "left":
        # The measure and its unit, where the agent's drawing puts it (``_draw.axes``): above the plot, starting at the
        # axis line, its line box ending NAME_GAP above the plot. An inverse (category) axis starts at the top.
        out.update(name=str(found["title"]), nameLocation="start" if out.get("inverse") else "end", nameGap=FR.NAME_GAP,
                   nameMoveOverlap=False,
                   nameTextStyle={"fontFamily": "$sans", "fontSize": FR.FONT, "color": "chart.muted", "align": "left",
                                  "verticalAlign": "bottom", "lineHeight": FR.LH})
    return out


def legend(frame: Frame) -> Any:
    """The frame's legend: one vertical legend on the right, or below one horizontal legend per row the frame wrapped
    (a list), so the page breaks the rows where Python measured them, whatever widths its own text measure gives."""
    placed = frame.legend
    if not placed:
        return {"show": False}
    x, y, w, h = placed["box"]
    items = placed.get("items") or []
    common = {"show": True, "itemWidth": FR.SWATCH, "itemHeight": FR.SWATCH, "icon": "roundRect", "padding": 0,
              "textStyle": {"fontFamily": "$sans", "fontSize": FR.LEGEND_FONT, "color": "chart.ink"}}
    if placed["pos"] == "right":
        return dict(common, orient="vertical", left=round(float(x), 2), top=round(float(y) + (FR.LEGEND_ROW - FR.SWATCH) / 2.0, 2),
                    itemGap=FR.LEGEND_ROW - FR.SWATCH,
                    data=[item["name"] for item in items])
    rows = placed.get("rows") or [list(range(len(items)))]
    out = []
    for number, row in enumerate(rows):
        top = float(y) + number * FR.LEGEND_ROW + (FR.LEGEND_ROW - FR.SWATCH) / 2.0
        out.append(dict(common, orient="horizontal", left=round(float(x), 2), top=round(top, 2), itemGap=FR.ITEM_GAP,
                        data=[items[i]["name"] for i in row]))
    return out if len(out) > 1 else out[0]


def dataset() -> Dict[str, Any]:
    """The datasets the page takes from the doc asset (2.7)."""
    return {"$doc": "datasets"}


def value_label(fmt_id: str, position: str = "top") -> Dict[str, Any]:
    return {"show": True, "position": position, "formatter": {"$fmt": fmt_id}, "fontFamily": "$sans", "fontSize": FR.FONT, "color": "chart.ink"}


def mark_lines(annotations: Sequence[Mapping[str, Any]], x_is_category: bool) -> Optional[Dict[str, Any]]:
    """Annotations as a ``markLine`` (dashed, ink, labelled with their text)."""
    data: List[Dict[str, Any]] = []
    for note in annotations:
        if note.get("x") is not None:
            entry = {"xAxis": note["x"]}
        elif note.get("y") is not None:
            entry = {"yAxis": note["y"]}
        else:
            continue
        if note.get("text"):
            entry["label"] = {"formatter": str(note["text"]), "color": "chart.ink", "fontFamily": "$sans", "fontSize": FR.FONT}
        data.append(entry)
    if not data:
        return None
    return {"silent": True, "symbol": "none", "lineStyle": {"type": "dashed", "color": "chart.ink", "opacity": 0.7}, "data": data}
