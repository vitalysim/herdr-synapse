"""Fit to box with readable axes (canvas v2 phase 3, 2.5): Python decides the frame, the page and the drawing follow it.

A ``Frame`` says where the plot goes inside the slot, which ticks each value
axis shows, how each category axis writes its labels (horizontal, every k-th,
rotated 45 or 90 degrees, truncated), and where the legend goes. The ECharts
option takes it verbatim (``grid`` in px, explicit ``min``/``max``/``interval``,
``axisLabel.rotate``/``interval``/``width``) and so does the Python drawing, so
both pictures agree. Every label is measured with the real font metrics
(``canvas_text.measure`` at ``chart.font.axis``), so "readable axes" is a fact
``check`` can test (``legible``), not a hope.

Pure: a function of the axes' labels and the box, under 10 ms for the largest
chart (a resize re-frames from the stored model with no I/O).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team import canvas_text, canvas_theme
from herdr_team.canvas_charts import _format as F
from herdr_team.canvas_charts import _scale as S

_TOKENS = canvas_theme.section("chart", {}) or {}
_FONT = dict(_TOKENS.get("font") or {})
_SPACE = dict(_TOKENS.get("space") or {})
FONT = int(_FONT.get("axis", 12))
FONT_MIN = int(_FONT.get("min", 11))
LEGEND_FONT = int(_FONT.get("legend", 12))
LH = canvas_text.line_height(FONT)
TICK = float(_SPACE.get("tick_len", 4))
GAP = 4.0
PAD_TOP = 8.0
PAD_RIGHT = 12.0
LEGEND_GAP = float(_SPACE.get("legend_gap", 8))
CAT_MIN_PX = float(_SPACE.get("cat_min_px", 14))
BAR_MIN_PX = float(_SPACE.get("bar_min_px", 6))
SWATCH = 10.0
SWATCH_GAP = 6.0
ITEM_GAP = 16.0
LEGEND_ROW = 18.0
TITLE_ROW = 18.0
#: The left axis's title sits above the plot, starting at the axis line (right of the tick labels, so it never meets
#: the top one), its line box ending this far above the plot's top. The page draws it as that axis's ECharts ``name``
#: at the same place (``_option.axis``), so both pictures carry the measure and its unit.
NAME_GAP = 4.0
#: A category label is never truncated below this many pixels (below it the check ``chart_labels`` fires).
MIN_LABEL_PX = 48.0
#: Rotated labels take at most this share of the slot's height; past it they are truncated.
LABEL_HEIGHT_SHARE = 0.4
SIN45 = math.sqrt(0.5)


def text_w(text: str, size: float = FONT, weight: int = 400) -> float:
    return canvas_text.measure(str(text), "normal", size, weight).width


def fit_text(text: str, room: float, size: float = FONT, weight: int = 400) -> str:
    """``text`` cut with … to fit ``room`` (the page's ``overflow: truncate`` cuts the same way)."""
    from herdr_team import canvas_geometry

    return canvas_geometry.fit_line(str(text), max(0.0, room), size, weight)


@dataclass(frozen=True)
class Frame:
    """Where a chart draws inside its slot (2.3), stored on the element as JSON (``to_json``)."""

    #: The slot's ``(w, h)``.
    box: Tuple[float, float]
    #: The plot's ``(x, y, w, h)`` inside the slot.
    plot: Tuple[float, float, float, float]
    #: Per axis (``x``, ``y``): its kind, position, ticks or labels, rotation, interval, truncation width and title.
    axes: Mapping[str, Any]
    #: ``{"pos", "items": [{"name", "color", "w"}], "box": [x, y, w, h], "rows": [[index, ...]]}`` or None.
    legend: Optional[Mapping[str, Any]]
    #: The label size in px.
    font: int = FONT
    #: What the fit had to do (``x labels rotated 45°``).
    notes: Tuple[str, ...] = ()
    #: False when labels could not be made readable: the check ``chart_labels`` fires.
    legible: bool = True
    #: Kind-specific geometry the drawing and the option share (a pie's centre and radii).
    extra: Mapping[str, Any] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        return {"box": [_r(self.box[0]), _r(self.box[1])], "plot": [_r(v) for v in self.plot], "axes": _round_all(dict(self.axes)),
                "legend": _round_all(dict(self.legend)) if self.legend else None, "font": self.font, "notes": list(self.notes),
                "legible": bool(self.legible), "extra": _round_all(dict(self.extra))}

    @staticmethod
    def from_json(doc: Mapping[str, Any]) -> "Frame":
        box = doc.get("box") or [0, 0]
        plot = doc.get("plot") or [0, 0, 0, 0]
        return Frame(box=(float(box[0]), float(box[1])), plot=(float(plot[0]), float(plot[1]), float(plot[2]), float(plot[3])),
                     axes=dict(doc.get("axes") or {}), legend=dict(doc["legend"]) if isinstance(doc.get("legend"), dict) else None,
                     font=int(doc.get("font") or FONT), notes=tuple(str(n) for n in doc.get("notes") or ()), legible=bool(doc.get("legible", True)),
                     extra=dict(doc.get("extra") or {}))


def _r(value: float) -> Any:
    found = round(float(value), 2)
    return int(found) if found.is_integer() else found


def _round_all(obj: Any) -> Any:
    if isinstance(obj, float):
        return _r(obj)
    if isinstance(obj, dict):
        return {k: _round_all(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_round_all(v) for v in obj]
    return obj


# --------------------------------------------------------------------------
# legend


def legend_items(names: Sequence[str], colors: Sequence[str]) -> List[Dict[str, Any]]:
    return [{"name": str(n), "color": c} for n, c in zip(names, colors)]


def place_legend(items: Sequence[Mapping[str, Any]], mode: str, w: float, h: float) -> Optional[Dict[str, Any]]:
    """The legend's place (2.4): ``auto`` puts it right with 6 or fewer items and a slot 480 wide or more, else below,
    and shows none for a single item; ``right`` and ``bottom`` force a side; ``none`` hides it."""
    if mode == "none" or not items or (mode == "auto" and len(items) <= 1):
        return None
    pos = mode if mode in ("right", "bottom") else ("right" if len(items) <= 6 and w >= 480 else "bottom")
    out: List[Dict[str, Any]] = []
    if pos == "right":
        room = max(60.0, min(w * 0.3, 200.0)) - SWATCH - SWATCH_GAP
        for item in items:
            name = fit_text(item["name"], room, LEGEND_FONT)
            out.append({"name": name, "color": item["color"], "w": SWATCH + SWATCH_GAP + text_w(name, LEGEND_FONT)})
        width = max(i["w"] for i in out)
        height = len(out) * LEGEND_ROW
        rows = [[i] for i in range(len(out))]
        box = [w - width, PAD_TOP, width, min(height, h - PAD_TOP)]
        return {"pos": "right", "items": out, "rows": rows, "box": box}
    room = max(40.0, w - 2 * PAD_RIGHT)
    for item in items:
        name = fit_text(item["name"], min(room, 180.0) - SWATCH - SWATCH_GAP, LEGEND_FONT)
        out.append({"name": name, "color": item["color"], "w": SWATCH + SWATCH_GAP + text_w(name, LEGEND_FONT)})
    rows: List[List[int]] = [[]]
    used = 0.0
    for index, item in enumerate(out):
        need = item["w"] + (ITEM_GAP if rows[-1] else 0.0)
        if rows[-1] and used + need > room:
            rows.append([])
            used = 0.0
            need = item["w"]
        rows[-1].append(index)
        used += need
    height = len(rows) * LEGEND_ROW
    return {"pos": "bottom", "items": out, "rows": rows, "box": [PAD_RIGHT, h - height, room, height]}


# --------------------------------------------------------------------------
# axes


def value_axis(lo: float, hi: float, length: float, per_tick: float, fmt: Optional[str], unit: Optional[str], zero: bool,
               title: Optional[str] = None, prefix_only: bool = True) -> Dict[str, Any]:
    """A value axis over ``[lo, hi]`` with about one tick per ``per_tick`` px: nice ticks and their labels. The labels
    carry a currency prefix; a suffix unit goes into the axis title instead (``prefix_only``) so ticks stay narrow."""
    if zero:
        lo, hi = min(lo, 0.0), max(hi, 0.0)
    count = max(3, int(length / per_tick)) if per_tick else 5
    vmin, vmax, step, ticks = S.nice_ticks(lo, hi, count)
    shown_unit = unit if (not prefix_only or unit in F.CURRENCIES) else ""
    labels = [{"v": t, "t": F.fmt_number(t, fmt, shown_unit), "w": 0.0} for t in ticks]
    for label in labels:
        label["w"] = text_w(label["t"])
    return {"kind": "value", "min": vmin, "max": vmax, "step": step, "ticks": labels, "fmt": F.fmt_id(fmt, shown_unit), "title": title}


def _fits_horizontal(widths: Sequence[float], step: float, k: int) -> bool:
    shown = list(range(0, len(widths), k))
    for a, b in zip(shown, shown[1:]):
        if (widths[a] + widths[b]) / 2.0 + GAP > (b - a) * step:
            return False
    return True


def category_axis(labels: Sequence[str], length: float, height_budget: float, thin: bool = False, name: str = "x") -> Dict[str, Any]:
    """A category axis along ``length`` px (2.5 step 3), the first rung that fits (no two shown labels closer than
    ``GAP``): horizontal; every 2nd; rotated 45 degrees (every label, then every 2nd); rotated 90 (the same); every 3rd
    horizontal or at 45; and last on its side, truncated. An ordered axis (``thin``: time, sorted numbers) thins freely
    before it rotates. Each rung taken is a note."""
    n = max(1, len(labels))
    step = float(length) / n
    widths = [text_w(t) for t in labels]
    widest = max(widths) if widths else 0.0
    axis: Dict[str, Any] = {"kind": "category", "labels": [{"v": i, "t": t, "w": w} for i, (t, w) in enumerate(zip(labels, widths))],
                            "rotate": 0, "interval": 0, "width": None}
    need = LH + GAP
    extent45 = widest * SIN45 + LH * SIN45

    def done(rotate: int, k: int, extent: float) -> Dict[str, Any]:
        notes: List[str] = []
        if rotate:
            notes.append("{} labels rotated {}°".format(name, rotate))
        if k > 1:
            notes.append("every {} {} label shown".format(_ordinal(k), name))
        axis.update(rotate=rotate, interval=k - 1, extent=extent)
        return dict(axis, notes=notes, hidden=n - len(range(0, n, k)))

    def fits_at(rotate: int, k: int) -> bool:
        if rotate == 0:
            return _fits_horizontal(widths, step, k)
        room = step * k * (SIN45 if rotate == 45 else 1.0)
        return room + 1e-9 >= need and (extent45 if rotate == 45 else widest) <= height_budget

    ladder = [(0, k) for k in (range(1, n + 1) if thin else (1, 2))] + [(45, 1), (45, 2), (90, 1), (90, 2), (0, 3), (45, 3)]
    for rotate, k in ladder:
        if fits_at(rotate, k):
            return done(rotate, k, LH if rotate == 0 else (extent45 if rotate == 45 else widest))
    k90 = max(1, int(math.ceil(need / max(step, 1e-6) - 1e-9)))
    if widest <= height_budget:
        return done(90, k90, widest)
    width = max(MIN_LABEL_PX, height_budget)
    cut = [fit_text(t, width) for t in labels]
    axis["labels"] = [{"v": i, "t": t, "w": text_w(t)} for i, t in enumerate(cut)]
    found = done(90, k90, min(widest, width))
    found["width"] = width
    found["notes"].insert(1, "{} labels truncated to {} px".format(name, int(width)))
    return found


def _ordinal(k: int) -> str:
    return {2: "2nd", 3: "3rd"}.get(k, "{}th".format(k))


def side_category_axis(labels: Sequence[str], length: float, box_w: float) -> Dict[str, Any]:
    """Categories down the left side (horizontal bars, heatmap rows): labels truncated to 35 % of the width at most, thinned
    when the rows are closer than a line."""
    n = max(1, len(labels))
    step = float(length) / n
    room = max(MIN_LABEL_PX, box_w * 0.35)
    cut = [fit_text(t, room) for t in labels]
    widths = [text_w(t) for t in cut]
    k = max(1, int(math.ceil((LH + 2) / max(step, 1e-6) - 1e-9)))
    notes = []
    if any(c != t for c, t in zip(cut, labels)):
        notes.append("y labels truncated to {} px".format(int(room)))
    if k > 1:
        notes.append("every {} y label shown".format(_ordinal(k)))
    return {"kind": "category", "labels": [{"v": i, "t": t, "w": w} for i, (t, w) in enumerate(zip(cut, widths))], "rotate": 0,
            "interval": k - 1, "width": room if notes and "truncated" in notes[0] else None, "extent": max(widths) if widths else 0.0,
            "notes": notes, "hidden": n - len(range(0, n, k))}


def cartesian(box: Tuple[float, float], x: Mapping[str, Any], y: Mapping[str, Any], legend: Sequence[Mapping[str, Any]] = (),
              legend_mode: str = "auto", horizontal: bool = False, y_title: Optional[str] = None,
              extra: Optional[Mapping[str, Any]] = None) -> Frame:
    """The frame of an x/y chart (2.5): legend first, then the left axis, then the bottom axis, in two passes.

    ``x`` and ``y`` describe the data's axes: ``{"kind": "category", "labels": [...], "thin": bool}`` or ``{"kind": "value",
    "lo", "hi", "zero", "fmt", "unit"}``. With ``horizontal`` the category axis runs down the left (horizontal bars)."""
    w, h = float(box[0]), float(box[1])
    placed = place_legend(legend, legend_mode, w, h)
    avail_w, avail_h = w, h
    if placed is not None and placed["pos"] == "right":
        avail_w = w - placed["box"][2] - LEGEND_GAP * 2
    elif placed is not None:
        avail_h = h - placed["box"][3] - LEGEND_GAP
    top = PAD_TOP + (TITLE_ROW if y_title else 0.0)
    left_spec, bottom_spec = (x, y) if horizontal else (y, x)
    bottom_h = LH + TICK + GAP + 2
    left_w = 40.0
    left_axis: Dict[str, Any] = {}
    bottom_axis: Dict[str, Any] = {}
    for _pass in range(2):
        plot_h = max(20.0, avail_h - top - bottom_h)
        if left_spec["kind"] == "value":
            left_axis = value_axis(left_spec["lo"], left_spec["hi"], plot_h, 48.0, left_spec.get("fmt"), left_spec.get("unit"),
                                   bool(left_spec.get("zero")))
            left_w = max((t["w"] for t in left_axis["ticks"]), default=0.0) + TICK + GAP + 2
        else:
            left_axis = side_category_axis(left_spec["labels"], plot_h, w)
            left_w = left_axis["extent"] + TICK + GAP + 2
        plot_w = max(20.0, avail_w - left_w - PAD_RIGHT)
        if bottom_spec["kind"] == "value":
            bottom_axis = _bottom_value(bottom_spec, plot_w)
            bottom_h = LH + TICK + GAP + 2
        elif bottom_spec["kind"] == "time":
            bottom_axis = _bottom_time(bottom_spec, plot_w)
            bottom_h = LH + TICK + GAP + 2
        else:
            bottom_axis = category_axis(bottom_spec["labels"], plot_w, max(LH, avail_h * LABEL_HEIGHT_SHARE), bool(bottom_spec.get("thin")))
            bottom_h = bottom_axis["extent"] + TICK + GAP + 4
    plot_h = max(20.0, avail_h - top - bottom_h)
    plot_w = max(20.0, avail_w - left_w - PAD_RIGHT)
    notes: List[str] = []
    for axis in (bottom_axis, left_axis):
        notes += axis.pop("notes", []) if isinstance(axis.get("notes"), list) else []
    legible = True
    for axis in (bottom_axis, left_axis):
        if axis.get("kind") == "category":
            n = len(axis["labels"])
            if n and axis.get("hidden", 0) * 2 > n and not (axis is bottom_axis and bottom_spec.get("thin")):
                legible = False
            if axis.get("width") is not None and axis["width"] < MIN_LABEL_PX:
                legible = False
    if plot_w < 40 or plot_h < 30:
        legible = False
        notes.append("the plot is only {}x{} px".format(int(plot_w), int(plot_h)))
    left_axis["pos"], bottom_axis["pos"] = "left", "bottom"
    if y_title:
        left_axis["title"] = fit_text(y_title, max(40.0, plot_w))
    axes = {"x": left_axis if horizontal else bottom_axis, "y": bottom_axis if horizontal else left_axis}
    if placed is not None and placed["pos"] == "bottom":
        placed["box"] = [left_w, h - placed["box"][3], plot_w, placed["box"][3]]
    return Frame(box=(w, h), plot=(left_w, top, plot_w, plot_h), axes=axes, legend=placed, font=FONT, notes=tuple(notes),
                 legible=legible, extra=dict(extra or {}))


def _bottom_value(spec: Mapping[str, Any], plot_w: float) -> Dict[str, Any]:
    """A value axis along the bottom: fewer ticks until their labels stop touching."""
    per = 80.0
    axis: Dict[str, Any] = {}
    for _try in range(5):
        axis = value_axis(spec["lo"], spec["hi"], plot_w, per, spec.get("fmt"), spec.get("unit"), bool(spec.get("zero")))
        ticks = axis["ticks"]
        span = (axis["max"] - axis["min"]) or 1.0
        ok = all((a["w"] + b["w"]) / 2.0 + GAP <= (b["v"] - a["v"]) / span * plot_w for a, b in zip(ticks, ticks[1:]))
        if ok:
            break
        per *= 1.5
    axis["extent"] = LH
    return axis


def _bottom_time(spec: Mapping[str, Any], plot_w: float) -> Dict[str, Any]:
    """A time axis along the bottom (day numbers): calendar ticks, fewer until their labels stop touching."""
    lo, hi = float(spec["lo"]), float(spec["hi"])
    if hi <= lo:
        hi = lo + 1.0
    count = max(2.0, plot_w / 90.0)
    axis: Dict[str, Any] = {}
    for _try in range(6):
        ticks, fmt = S.time_ticks(lo, hi, count)
        labels = [{"v": t, "t": F.fmt_number(t, fmt), "w": 0.0} for t in ticks]
        for label in labels:
            label["w"] = text_w(label["t"])
        axis = {"kind": "value", "time": True, "min": lo, "max": hi, "step": None, "ticks": labels, "fmt": F.fmt_id(fmt, "")}
        ok = all((a["w"] + b["w"]) / 2.0 + GAP <= (b["v"] - a["v"]) / (hi - lo) * plot_w for a, b in zip(labels, labels[1:]))
        if ok:
            break
        count = max(2.0, count * 0.6)
    axis["extent"] = LH
    return axis


def radial(box: Tuple[float, float], legend: Sequence[Mapping[str, Any]] = (), legend_mode: str = "auto",
           extra: Optional[Mapping[str, Any]] = None) -> Frame:
    """The frame of a chart without axes (pie, donut, funnel, treemap, sankey): the box minus the legend."""
    w, h = float(box[0]), float(box[1])
    placed = place_legend(legend, legend_mode, w, h)
    x, y, pw, ph = 0.0, PAD_TOP, w - PAD_RIGHT, h - PAD_TOP - 4
    if placed is not None and placed["pos"] == "right":
        pw = w - placed["box"][2] - LEGEND_GAP * 2
    elif placed is not None:
        ph = h - PAD_TOP - placed["box"][3] - LEGEND_GAP
    legible = pw >= 60 and ph >= 60
    notes = () if legible else ("the plot is only {}x{} px".format(int(pw), int(ph)),)
    return Frame(box=(w, h), plot=(x, y, max(1.0, pw), max(1.0, ph)), axes={}, legend=placed, font=FONT, notes=notes, legible=legible,
                 extra=dict(extra or {}))


def none(box: Tuple[float, float], extra: Optional[Mapping[str, Any]] = None) -> Frame:
    """The frame of a chart that draws itself in the whole slot (a GL chart)."""
    w, h = float(box[0]), float(box[1])
    return Frame(box=(w, h), plot=(0.0, 0.0, w, h), axes={}, legend=None, font=FONT, extra=dict(extra or {}))


def min_plot_for(categories: int, rotated: bool = False) -> float:
    """The plot length a category axis needs so its labels fit with every other one shown: one shown label per
    ``cat_min_px``, or per line (the room a label on its side takes) when that is more."""
    return max(120.0, math.ceil(categories / 2.0) * max(CAT_MIN_PX, LH + GAP))
