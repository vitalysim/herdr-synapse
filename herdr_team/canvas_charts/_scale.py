"""Scales for charts (canvas v2 phase 3, 2.3): nice ticks, bands, day ticks and colour ramps as token references.

``nice_ticks`` is Heckbert's "nice numbers for graph labels": the page draws the
very ticks Python chose (the ECharts option carries explicit ``min``, ``max``
and ``interval``), so both pictures agree on every axis. Colours are token
references (``chart.cat.3``, ``chart.seq.6``) the display list and the option
both carry; a renderer resolves them in its theme's palette.

Pure, stdlib only.
"""
from __future__ import annotations

import math
from typing import List, Tuple

CAT = 10
SEQ = 9
DIV = 9


def _nice(value: float, round_: bool) -> float:
    exp = math.floor(math.log10(value))
    base = 10.0 ** exp
    f = value / base
    if round_:
        nf = 1.0 if f < 1.5 else 2.0 if f < 3.0 else 5.0 if f < 7.0 else 10.0
    else:
        nf = 1.0 if f <= 1.0 else 2.0 if f <= 2.0 else 5.0 if f <= 5.0 else 10.0
    return nf * base


def _clean(value: float) -> float:
    found = float("{:.12g}".format(value))
    return 0.0 if found == 0 else found


def nice_ticks(lo: float, hi: float, count: float = 5) -> Tuple[float, float, float, List[float]]:
    """``(min, max, step, ticks)`` covering ``lo`` to ``hi`` with about ``count`` nice ticks (at least 2)."""
    count = max(2, int(count))
    if not (math.isfinite(lo) and math.isfinite(hi)):
        lo, hi = 0.0, 1.0
    if hi < lo:
        lo, hi = hi, lo
    if hi == lo:
        pad = abs(lo) * 0.1 or 1.0
        lo, hi = (0.0, hi + pad) if lo >= 0 and lo - pad < 0 else (lo - pad, hi + pad)
    span = _nice(hi - lo, False)
    step = _nice(span / (count - 1), True)
    gmin = math.floor(lo / step + 1e-9) * step
    gmax = math.ceil(hi / step - 1e-9) * step
    ticks = []
    n = int(round((gmax - gmin) / step))
    for i in range(n + 1):
        ticks.append(_clean(gmin + i * step))
    return _clean(gmin), _clean(gmax), _clean(step), ticks


def value_range(values: List[float], zero: bool) -> Tuple[float, float]:
    """The range an axis must show: the values' extent, taken to 0 for bars and areas (``zero``)."""
    present = [v for v in values if v is not None and math.isfinite(v)]
    if not present:
        return 0.0, 1.0
    lo, hi = min(present), max(present)
    if zero:
        lo, hi = min(lo, 0.0), max(hi, 0.0)
    return lo, hi


def band(n: int, width: float, padding: float = 0.2) -> Tuple[float, float, float]:
    """``(step, bandwidth, offset)`` of ``n`` bands across ``width``: each band is ``step`` wide, its bar ``bandwidth``
    wide, starting ``offset`` into the step (the page's category axis puts band centres at the same places)."""
    n = max(1, int(n))
    step = float(width) / n
    bandwidth = step * (1.0 - padding)
    return step, bandwidth, (step - bandwidth) / 2.0


def cat(index: int) -> str:
    """The categorical paint of series ``index`` (they repeat after 10)."""
    return "chart.cat.{}".format(int(index) % CAT)


def seq(t: float) -> str:
    """The sequential paint at ``t`` in 0..1 (low to high)."""
    t = min(1.0, max(0.0, float(t) if math.isfinite(t) else 0.0))
    return "chart.seq.{}".format(int(math.floor(t * (SEQ - 1) + 0.5)))


def div(t: float) -> str:
    """The diverging paint at ``t`` in -1..1 (low, middle, high)."""
    t = min(1.0, max(-1.0, float(t) if math.isfinite(t) else 0.0))
    return "chart.div.{}".format(int(math.floor((t + 1.0) / 2.0 * (DIV - 1) + 0.5)))


def on(ref: str) -> str:
    """The label paint on a filled mark of paint ``ref`` (``chart.cat.3`` -> ``chart.on_cat.3``): the chart's paper or ink,
    whichever reads better on it in the theme drawn."""
    parts = str(ref).split(".")
    if len(parts) >= 2 and parts[0] == "chart" and parts[1] in ("cat", "seq", "div", "highlight", "dim"):
        return ".".join(["chart", "on_" + parts[1]] + parts[2:])
    return "chart.ink"


def seq_refs() -> List[str]:
    return ["chart.seq.{}".format(i) for i in range(SEQ)]


def div_refs() -> List[str]:
    return ["chart.div.{}".format(i) for i in range(DIV)]


def cat_refs() -> List[str]:
    return ["chart.cat.{}".format(i) for i in range(CAT)]


def lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def position(value: float, lo: float, hi: float, start: float, end: float) -> float:
    """``value`` on a linear scale from ``[lo, hi]`` to ``[start, end]``."""
    if hi == lo:
        return (start + end) / 2.0
    return start + (value - lo) / (hi - lo) * (end - start)


_TIME_STEPS = (("day", 1), ("day", 2), ("day", 7), ("day", 14), ("month", 1), ("month", 3), ("month", 6), ("year", 1), ("year", 2),
               ("year", 5), ("year", 10), ("year", 25), ("year", 50), ("year", 100))


def time_ticks(lo: float, hi: float, count: float = 6) -> Tuple[List[float], str]:
    """Calendar ticks over day numbers ``lo`` to ``hi`` (days since 1970-01-01): about ``count`` of them on day, week,
    month or year boundaries; ``(ticks, format)`` where the format names them (``date``, ``month`` or ``year``)."""
    import datetime

    count = max(2, int(count))
    span = max(1.0, hi - lo)
    epoch = datetime.date(1970, 1, 1).toordinal()
    chosen = _TIME_STEPS[-1]
    for unit, step in _TIME_STEPS:
        approx = step * {"day": 1.0, "month": 30.44, "year": 365.25}[unit]
        if span / approx <= count:
            chosen = (unit, step)
            break
    unit, step = chosen
    start = datetime.date.fromordinal(int(math.floor(lo)) + epoch)
    ticks: List[float] = []
    if unit == "day":
        day = start
        if step == 7:
            day = day - datetime.timedelta(days=day.weekday())
        current = day.toordinal() - epoch
        while current <= hi + 1e-9 and len(ticks) < 400:
            if current >= lo - 1e-9:
                ticks.append(float(current))
            current += step
        return ticks, "date"
    if unit == "month":
        year, month = start.year, start.month
        month = ((month - 1) // step) * step + 1
        while len(ticks) < 400:
            current = datetime.date(year, month, 1).toordinal() - epoch
            if current > hi + 1e-9:
                break
            if current >= lo - 1e-9:
                ticks.append(float(current))
            month += step
            while month > 12:
                month -= 12
                year += 1
        return ticks, "month"
    year = (start.year // step) * step
    while len(ticks) < 400:
        if year < 1:
            year += step
            continue
        current = datetime.date(year, 1, 1).toordinal() - epoch
        if current > hi + 1e-9 or year > 9999 - step:
            break
        if current >= lo - 1e-9:
            ticks.append(float(current))
        year += step
    return ticks, "year"
