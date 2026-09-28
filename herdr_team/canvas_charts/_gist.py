"""What a chart says in words (canvas v2 phase 3, section 5): the statistics its gist lines are written from.

A chart's gist is how an agent that cannot read images reads it: the channels
and the source, then the extremes with their categories, trends, shares and
outliers. Every number here is computed from the whole filtered table, never
from the sampled model, so extremes are exact; the results are plain JSON
(``stats``) kept on the element, so a visual change (a format, a title)
rewrites the gist with no file read.

Pure, stdlib only.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Sequence, Tuple

from herdr_team.canvas_charts import _format as F

#: A change within this share of the start reads as flat (``≈``).
FLAT = 0.03
ARROW_UP, ARROW_DOWN, FLAT_SIGN = "↑", "↓", "≈"


def extremes(pairs: Sequence[Tuple[Any, Optional[float]]]) -> Optional[Dict[str, Any]]:
    """``{"max": [key, v], "min": [key, v]}`` over ``(key, value)`` (the first key wins a tie), or None."""
    present = [(k, v) for k, v in pairs if v is not None and math.isfinite(v)]
    if not present:
        return None
    hi = max(range(len(present)), key=lambda i: (present[i][1], -i))
    lo = min(range(len(present)), key=lambda i: (present[i][1], i))
    return {"max": [present[hi][0], present[hi][1]], "min": [present[lo][0], present[lo][1]]}


def trend(values: Sequence[Optional[float]]) -> Optional[Dict[str, Any]]:
    """First to last: ``{"first", "last", "change" (a share or None), "slope" (least squares per step), "sign"}``;
    ``sign`` is ``flat`` within ±3 % of the first value."""
    present = [(i, v) for i, v in enumerate(values) if v is not None and math.isfinite(v)]
    if len(present) < 2:
        return None
    first, last = present[0][1], present[-1][1]
    change = F.percent_change(first, last)
    n = len(present)
    mx = math.fsum(i for i, _ in present) / n
    my = math.fsum(v for _, v in present) / n
    den = math.fsum((i - mx) ** 2 for i, _ in present)
    slope = math.fsum((i - mx) * (v - my) for i, v in present) / den if den else 0.0
    if change is None:
        sign = "flat" if last == first else ("up" if last > first else "down")
    else:
        sign = "flat" if abs(change) <= FLAT else ("up" if change > 0 else "down")
    return {"first": first, "last": last, "change": change, "slope": slope, "sign": sign}


def arrow(sign: str) -> str:
    return {"up": ARROW_UP, "down": ARROW_DOWN}.get(sign, FLAT_SIGN)


def shares(pairs: Sequence[Tuple[Any, float]]) -> List[Tuple[Any, float]]:
    """``(key, share of the total)`` in the order given; an all-zero total gives zeros."""
    total = math.fsum(v for _, v in pairs if v is not None)
    return [(k, (v / total if total else 0.0)) for k, v in pairs]


def top_k(pairs: Sequence[Tuple[Any, Optional[float]]], k: int = 3) -> List[Tuple[Any, float]]:
    present = [(i, key, v) for i, (key, v) in enumerate(pairs) if v is not None]
    present.sort(key=lambda item: (-item[2], item[0]))
    return [(key, v) for _i, key, v in present[:k]]


def outliers(values: Sequence[float]) -> Tuple[float, float, List[int]]:
    """``(low fence, high fence, indices outside them)`` at 1.5 IQR."""
    from herdr_team.canvas_charts import _data

    ordered = sorted(v for v in values if v is not None)
    if len(ordered) < 4:
        return (ordered[0] if ordered else 0.0), (ordered[-1] if ordered else 0.0), []
    q1, q3 = _data.quantile(ordered, 0.25), _data.quantile(ordered, 0.75)
    iqr = q3 - q1
    lo, hi = q1 - 1.5 * iqr, q3 + 1.5 * iqr
    return lo, hi, [i for i, v in enumerate(values) if v is not None and (v < lo or v > hi)]


def pearson(xs: Sequence[float], ys: Sequence[float]) -> Optional[Tuple[float, float, float]]:
    """``(r, slope, intercept)`` of y on x, or None under 3 points or with no spread."""
    pairs = [(x, y) for x, y in zip(xs, ys) if x is not None and y is not None]
    n = len(pairs)
    if n < 3:
        return None
    mx = math.fsum(p[0] for p in pairs) / n
    my = math.fsum(p[1] for p in pairs) / n
    sxx = math.fsum((p[0] - mx) ** 2 for p in pairs)
    syy = math.fsum((p[1] - my) ** 2 for p in pairs)
    sxy = math.fsum((p[0] - mx) * (p[1] - my) for p in pairs)
    if sxx <= 0 or syy <= 0:
        return None
    slope = sxy / sxx
    return sxy / math.sqrt(sxx * syy), slope, my - slope * mx


def moments(values: Sequence[float]) -> Optional[Dict[str, float]]:
    """``n``, mean, median, sd (sample) and a skew sign (+1, 0, -1 from mean against median by a tenth of an sd)."""
    from herdr_team.canvas_charts import _data

    present = sorted(v for v in values if v is not None)
    n = len(present)
    if not n:
        return None
    mean = math.fsum(present) / n
    median = _data.quantile(present, 0.5)
    sd = math.sqrt(math.fsum((v - mean) ** 2 for v in present) / (n - 1)) if n > 1 else 0.0
    skew = 0
    if sd > 0 and abs(mean - median) > 0.1 * sd:
        skew = 1 if mean > median else -1
    return {"n": float(n), "mean": mean, "median": median, "sd": sd, "skew": float(skew), "min": present[0], "max": present[-1]}


def num(value: Optional[float], spec: Dict[str, Any], channel: str = "y") -> str:
    """A value as the chart shows it on ``channel`` (its format and unit)."""
    fmt = (spec.get("format") or {}).get(channel) if isinstance(spec.get("format"), dict) else None
    unit = (spec.get("units") or {}).get(channel) if isinstance(spec.get("units"), dict) else None
    if unit is None and isinstance(spec.get("_units"), dict):
        unit = spec["_units"].get(channel)
    if fmt is None and value is not None and abs(value) >= 1e4:
        fmt = "compact"
    return F.fmt_number(value, fmt, unit) if value is not None else "n/a"


def join(parts: Sequence[str]) -> str:
    return " · ".join(p for p in parts if p)
