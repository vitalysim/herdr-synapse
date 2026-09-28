"""Series over categories (canvas v2 phase 3): the shared shaping of bar, line and area charts.

``matrix`` turns a table into categories along x and one series per ``color``
value: aggregated (``sum`` when rows repeat, unless ``aggregate`` says
otherwise), ordered (nominal by total descending, time ascending with its gaps
filled), and capped (the tail of categories and series folded into "Other",
or the newest periods kept), each cut reported as a ``chart_capped`` warning.
``series_stats`` and ``series_gist`` read the result back in words.

Pure, stdlib only.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team.canvas_charts import _data
from herdr_team.canvas_charts import _format as F
from herdr_team.canvas_charts import _gist as G
from herdr_team.canvas_charts._data import Table, invalid

OTHER = "Other"
NULL = "(null)"
#: A time axis of one regular grain is drawn as that many periods (gaps filled) up to this many.
MAX_PERIODS = 2000


def how_of(spec: Mapping[str, Any], table: Table, by: Sequence[str]) -> str:
    """The aggregate: the spec's, else ``sum`` when rows repeat for a key, else ``none`` (``count`` needs no measure)."""
    how = spec.get("aggregate")
    if how is None:
        return "sum" if _data.repeats(table, list(range(table.rows)), by) else "none"
    if how == "none" and _data.repeats(table, list(range(table.rows)), by):
        raise invalid("aggregate", "{} repeats ({} rows for {} keys); pick how to combine them: aggregate: sum, mean, median, min, max or "
                      "count".format(" and ".join(by), table.rows, len(_data.group(table, list(range(table.rows)), by))))
    return how


def _cat_label(value: Any, column: _data.Column, spec: Mapping[str, Any], channel: str = "x") -> str:
    if value is None:
        return NULL
    fmt = (spec.get("format") or {}).get(channel)
    unit = (spec.get("units") or {}).get(channel)
    return F.label(value, fmt, unit, column.type)


def categories(spec: Mapping[str, Any], table: Table, field: str, totals: Mapping[Any, float], fill: bool) -> Tuple[List[Any], str]:
    """The x values in drawing order and how they are ordered (``time``, ``data``, ``sort``): time ascending (every period
    between the first and last when ``fill`` and the column has one grain), ordinal in data order, nominal by ``sort``
    (default: total descending)."""
    column = table.by_name[field]
    seen: List[Any] = []
    known = set()
    for value in column.values:
        if value not in known:
            known.add(value)
            seen.append(value)
    if column.type == "temporal":
        keys = {}
        for value, key in zip(column.values, column.keys or []):
            if value is not None:
                keys[value] = key
        ordered = sorted((v for v in seen if v is not None), key=lambda v: (keys[v], v))
        if fill and column.grain in ("year", "quarter", "month", "week", "day") and ordered:
            start, end = keys[ordered[0]], keys[ordered[-1]]
            periods = []
            current = start
            while current <= end + 1e-9 and len(periods) <= MAX_PERIODS:
                periods.append(_data.period_label(current, column.grain))
                current = _data.next_period(current, column.grain)
            if len(periods) <= MAX_PERIODS:
                ordered = periods
        if None in known:
            ordered.append(None)
        return ordered, "time"
    sort = spec.get("sort")
    if column.type == "ordinal" and sort in (None, "none"):
        return seen, "data"
    if column.type == "quantitative":
        ordered = sorted((v for v in seen if v is not None))
        return ordered + ([None] if None in known else []), "time"
    sort = sort if sort in ("x", "-x", "y", "-y", "none") else "-y"
    if sort == "none":
        return seen, "data"
    if sort in ("x", "-x"):
        ordered = sorted(seen, key=lambda v: (v is None, str(v).lower() if v is not None else "", str(v)), reverse=sort == "-x")
        return ordered, "sort"
    # Ties go by the label, never by where a row happens to sit (a shuffled file draws the same chart).
    ordered = sorted(range(len(seen)), key=lambda i: ((totals.get(seen[i]) or 0.0) * (-1 if sort == "-y" else 1), _tie(seen[i])))
    return [seen[i] for i in ordered], "sort"


def matrix(spec: Dict[str, Any], table: Table, x: str, y: Optional[str], color: Optional[str], *, cap_x: int, cap_series: int,
           fill: bool = True, stack_other: bool = True) -> Dict[str, Any]:
    """Categories along ``x`` and one series per ``color`` value (2.4): ``{"keys", "cats", "series": [{"name", "values"}],
    "how", "order", "capped": [...]}``; the ``chart_capped`` warnings ride along in ``_warnings``."""
    by = [x] + ([color] if color else [])
    how = how_of(spec, table, by)
    if y is None:
        how = "count"
    cells = _data.aggregate(table, list(range(table.rows)), by, y, how if how != "none" else "last")
    x_col = table.by_name[x]
    parts: Dict[Any, List[float]] = {}
    series_parts: Dict[Any, List[float]] = {}
    for key, value in cells.items():
        parts.setdefault(key[0], []).append(value or 0.0)
        if color:
            series_parts.setdefault(key[1], []).append(abs(value or 0.0))
    totals = {key: math.fsum(values) for key, values in parts.items()}
    series_totals = {key: math.fsum(values) for key, values in series_parts.items()}
    cats, order = categories(spec, table, x, totals, fill)
    warnings: List[Dict[str, Any]] = []
    capped: List[str] = []
    other_cats: Optional[List[Any]] = []
    top = spec.get("top")
    limit = min(cap_x, int(top)) if isinstance(top, int) else cap_x
    if len(cats) > limit:
        if order == "time":
            dropped = len(cats) - limit
            cats = cats[-limit:]
            other_cats = None
            capped.append("kept the last {} of {} {} values".format(limit, limit + dropped, x))
        else:
            keep_n = limit - 1 if spec.get("other", True) else limit
            ranked = sorted(range(len(cats)), key=lambda i: (-abs(totals.get(cats[i]) or 0.0), _tie(cats[i])))
            kept = set(ranked[:keep_n])
            rest = [cats[i] for i in range(len(cats)) if i not in kept]
            cats = [cats[i] for i in range(len(cats)) if i in kept]
            if spec.get("other", True):
                other_cats = rest
                capped.append("{} of {} {} values folded into {}".format(len(rest), len(rest) + len(cats), x, OTHER))
            else:
                other_cats = None
                capped.append("dropped {} of {} {} values (other: false)".format(len(rest), len(rest) + len(cats), x))
    else:
        other_cats = None
    names: List[Any] = [None]
    other_series: List[Any] = []
    if isinstance(top, int):
        cap_series = max(2, min(cap_series, top))  # top keeps the biggest series too (the crowded fix)
    if color:
        seen = []
        for key in cells:
            if key[1] not in seen:
                seen.append(key[1])
        names = sorted(seen, key=lambda v: (-series_totals.get(v, 0.0), _tie(v)))
        if len(names) > cap_series:
            other_series = names[cap_series - 1:]
            names = names[:cap_series - 1] + [OTHER]
            capped.append("{} of {} {} values folded into {}".format(len(other_series), len(other_series) + cap_series - 1, color, OTHER))
    keys = list(cats) + ([OTHER] if other_cats else [])
    series = []
    merge = how in ("sum", "count", "none", "last")
    for name in names:
        members = other_series if (color and name == OTHER and other_series) else [name]
        values: List[Optional[float]] = []
        for cat in keys:
            cat_members = other_cats if (cat == OTHER and other_cats) else [cat]
            found = [cells.get((c,) + ((m,) if color else ())) for c in cat_members for m in members]
            present = [v for v in found if v is not None]
            if not present:
                values.append(None)
            elif len(present) == 1:
                values.append(present[0])
            elif merge:
                values.append(math.fsum(present))
            else:
                values.append(_data.reduce(present, how))
        series.append({"name": (NULL if name is None and color else (str(name) if name is not None else (y or "count"))), "values": values})
    for note in capped:
        warnings.append({"code": "chart_capped", "message": note})
    labels = [OTHER if (cat == OTHER and other_cats) else _cat_label(cat, x_col, spec) for cat in keys]
    return {"keys": keys, "cats": labels, "series": series, "how": how if y else "count", "order": order, "capped": capped,
            "_warnings": warnings}


def _tie(value: Any) -> Tuple[int, str]:
    """A tie-break that depends on the value only (nulls last)."""
    return (1, "") if value is None else (0, str(value))


def compacted(series: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    return [{"name": s["name"], "values": [_data.compact_number(v) for v in s["values"]]} for s in series]


def measure_label(spec: Mapping[str, Any], y: Optional[str], how: str) -> str:
    if y is None or how == "count":
        return "count"
    return "{}({})".format(how, y) if how not in ("none", "last") else y


def series_stats(model: Mapping[str, Any]) -> Dict[str, Any]:
    """Exact extremes, totals, trends and the top categories over the full (uncapped-by-drawing) matrix."""
    cats = model.get("cats") or []
    series = model.get("series") or []
    cells = [(s["name"], cats[i], v) for s in series for i, v in enumerate(s["values"]) if v is not None]
    out: Dict[str, Any] = {"n_cats": len(cats), "n_series": len(series)}
    if cells:
        hi = max(range(len(cells)), key=lambda i: (cells[i][2], -i))
        lo = min(range(len(cells)), key=lambda i: (cells[i][2], i))
        out["max"] = list(cells[hi])
        out["min"] = list(cells[lo])
        out["total"] = math.fsum(c[2] for c in cells)
    totals = [(cats[i], math.fsum(s["values"][i] or 0.0 for s in series)) for i in range(len(cats))]
    out["top"] = [[k, v] for k, v in G.top_k(totals, 3)]
    if model.get("order") == "time":
        out["trends"] = [[s["name"], G.trend(s["values"])] for s in series[:6]]
        out["span"] = [cats[0], cats[-1]] if cats else None
    return out


def first_line(spec: Mapping[str, Any], model: Mapping[str, Any], x: str, y: Optional[str], color: Optional[str], source: str) -> str:
    cats = model.get("cats") or []
    span = "{}…{}".format(cats[0], cats[-1]) if len(cats) > 1 else (cats[0] if cats else "")
    parts = ["x={} ({}{})".format(x, len(cats), ", " + span if span and model.get("order") == "time" else ""),
             "y={}".format(measure_label(spec, y, str(model.get("how") or "none")))]
    if color:
        parts.append("color={} ({})".format(color, len(model.get("series") or [])))
    parts.append(source)
    return G.join(parts)


def trends_line(spec: Mapping[str, Any], stats: Mapping[str, Any]) -> Optional[str]:
    trends = [t for t in stats.get("trends") or [] if t[1]]
    if not trends:
        return None
    parts = []
    for name, found in trends:
        change = F.pct(found.get("change")) if found.get("change") is not None else G.num(found["last"] - found["first"], dict(spec))
        parts.append("{} {} {}".format(name, G.arrow(found["sign"]), change))
    span = stats.get("span")
    tail = " ({}→{})".format(span[0], span[1]) if span else ""
    return " · ".join(parts) + tail


# --------------------------------------------------------------------------
# series over a continuous or ordered x (line, area)

#: Points a line chart's model keeps across all its series (what Python draws); the doc keeps ``DOC_POINTS`` per series.
MODEL_POINTS = 900
DOC_POINTS = 2000


def x_kind(column: _data.Column) -> str:
    """How a line's x axis runs: ``time`` (day numbers), ``value`` (numbers) or ``category`` (ordered labels)."""
    if column.type == "temporal":
        return "time"
    if column.type == "quantitative":
        return "value"
    return "category"


def x_label(value: Any, key: float, kind: str, spec: Mapping[str, Any]) -> str:
    """The text of one x value in the gist and on category axes."""
    if kind == "value":
        return F.fmt_number(key, (spec.get("format") or {}).get("x"), (spec.get("units") or {}).get("x"))
    if kind == "time" and isinstance(value, str):
        return F.temporal_label(value, (spec.get("format") or {}).get("x")) if (spec.get("format") or {}).get("x") in F.DATE_FORMATS else value
    return str(value) if value is not None else NULL


def xy(spec: Dict[str, Any], table: Table, x: str, y: Optional[str], color: Optional[str], *, cap_series: int,
       aligned: bool = False) -> Dict[str, Any]:
    """Series of ``(x key, y)`` points sorted by x (2.4 line and area): aggregated per x (and ``color``), the series past
    ``cap_series`` folded into "Other"; ``aligned`` puts every series on the union of x keys (missing is 0, for stacking).

    ``{"xkind", "grain", "cats" (category x), "labels" {key: text}, "series": [{"name", "x", "y"}], "how", "capped",
    "_warnings"}``."""
    x_col = table.by_name[x]
    kind = x_kind(x_col)
    if isinstance(spec.get("top"), int):
        cap_series = max(2, min(cap_series, int(spec["top"])))
    by = [x] + ([color] if color else [])
    how = how_of(spec, table, by) if y else "count"
    cells = _data.aggregate(table, list(range(table.rows)), by, y, how if how != "none" else "last")
    keys: Dict[Any, float] = {}
    cats: List[str] = []
    if kind == "time":
        for value, key in zip(x_col.values, x_col.keys or []):
            if value is not None and key is not None:
                keys[value] = key
    elif kind == "value":
        for value in x_col.values:
            if value is not None:
                keys[value] = float(value)
    else:
        parts: Dict[Any, List[float]] = {}
        for key_tuple, value in cells.items():
            parts.setdefault(key_tuple[0], []).append(value or 0.0)
        ordered, _order = categories(spec, table, x, {k: math.fsum(v) for k, v in parts.items()}, False)
        for index, value in enumerate(ordered):
            keys[value] = float(index)
        cats = [str(v) if v is not None else NULL for v in ordered]
    labels = {keys[v]: x_label(v, keys[v], kind, spec) for v in keys}
    per: Dict[Any, Dict[float, float]] = {}
    series_parts: Dict[Any, List[float]] = {}
    for key_tuple, value in cells.items():
        if key_tuple[0] not in keys or value is None:
            continue
        name = key_tuple[1] if color else None
        per.setdefault(name, {})[keys[key_tuple[0]]] = value
        series_parts.setdefault(name, []).append(abs(value))
    names = sorted(per, key=lambda n: (-math.fsum(series_parts.get(n) or []), _tie(n)))
    capped: List[str] = []
    if color and len(names) > cap_series:
        rest = names[cap_series - 1:]
        names = names[:cap_series - 1]
        merged: Dict[float, List[float]] = {}
        for name in rest:
            for key, value in per[name].items():
                merged.setdefault(key, []).append(value)
        additive = how in ("sum", "count", "none")
        per[OTHER] = {k: (math.fsum(v) if additive else (_data.reduce(v, how) or 0.0)) for k, v in merged.items()}
        names.append(OTHER)
        capped.append("{} of {} {} values folded into {}".format(len(rest), len(rest) + cap_series - 1, color, OTHER))
    union = sorted({k for name in names for k in per[name]})
    series = []
    for name in names:
        points = per[name]
        xs = union if aligned else sorted(points)
        series.append({"name": (str(name) if name is not None else (NULL if color else (y or "count"))),
                       "x": list(xs), "y": [points.get(k, 0.0 if aligned else None) for k in xs]})
    return {"xkind": kind, "grain": x_col.grain if kind == "time" else "", "cats": cats, "labels": labels, "series": series,
            "how": how if y else "count", "capped": capped, "_warnings": [{"code": "chart_capped", "message": n} for n in capped]}


def thin(series: List[Dict[str, Any]], budget: int) -> List[Dict[str, Any]]:
    """Each series cut to its share of ``budget`` points by LTTB (first and last kept; gaps are left as they are)."""
    per = max(24, budget // max(1, len(series)))
    out = []
    shared = None
    if len(series) > 1 and all(s["x"] == series[0]["x"] for s in series) and len(series[0]["x"]) > per:
        # Aligned series (a stack) keep the same x keys: thinned once, over their sum.
        xs = [float(v) for v in series[0]["x"]]
        total = [math.fsum(float(s["y"][i] or 0.0) for s in series) for i in range(len(xs))]
        shared = _data.lttb(xs, total, per)
    for s in series:
        xs, ys = s["x"], s["y"]
        if shared is not None:
            keep = shared
        elif len(xs) > per and all(v is not None for v in ys):
            keep = _data.lttb([float(v) for v in xs], [float(v) for v in ys], per)
        elif len(xs) > per:
            keep = _data.stride(len(xs), per)
        else:
            keep = list(range(len(xs)))
        out.append({"name": s["name"], "x": [_data.compact_number(xs[i]) for i in keep], "y": [_data.compact_number(ys[i]) for i in keep]})
    return out


def xy_stats(spec: Mapping[str, Any], found: Mapping[str, Any]) -> Dict[str, Any]:
    """Exact per-series facts over every point (never the thinned ones): first and last, change, trend, extremes, the
    largest step, and how often the two biggest series cross."""
    labels = found.get("labels") or {}

    def lab(key: Any) -> str:
        return str(labels.get(key, key))

    out: Dict[str, Any] = {"series": []}
    for s in (found.get("series") or [])[:12]:
        pairs = [(k, v) for k, v in zip(s["x"], s["y"]) if v is not None]
        if not pairs:
            continue
        values = [v for _k, v in pairs]
        ext = G.extremes([(lab(k), v) for k, v in pairs])
        step = None
        for (k0, v0), (k1, v1) in zip(pairs, pairs[1:]):
            delta = v1 - v0
            if step is None or abs(delta) > abs(step[1]) + 1e-12:
                step = (lab(k1), delta, k1)
        out["series"].append({"name": s["name"], "n": len(pairs), "first": [lab(pairs[0][0]), pairs[0][1]], "last": [lab(pairs[-1][0]), pairs[-1][1]],
                              "trend": G.trend(values), "max": ext["max"] if ext else None, "min": ext["min"] if ext else None,
                              "step": [step[0], step[1], step[2]] if step else None})
    series = found.get("series") or []
    if len(series) >= 2:
        a = {k: v for k, v in zip(series[0]["x"], series[0]["y"]) if v is not None}
        b = {k: v for k, v in zip(series[1]["x"], series[1]["y"]) if v is not None}
        common = sorted(set(a) & set(b))
        signs = [(a[k] > b[k]) - (a[k] < b[k]) for k in common]
        signs = [s for s in signs if s != 0]
        out["crossings"] = [series[0]["name"], series[1]["name"], sum(1 for p, q in zip(signs, signs[1:]) if p != q)]
    keys = sorted({k for s in series for k in s["x"]})
    out["span"] = [lab(keys[0]), lab(keys[-1])] if keys else None
    out["points"] = len(keys)
    return out


def xy_lines(spec: Mapping[str, Any], stats: Mapping[str, Any], annotations: Sequence[Mapping[str, Any]] = ()) -> List[str]:
    """A line per series: ``p95 412 → 236 ms (−43%) · max 2026-09-01 412 ms · min … · largest step −147 ms at … (cache on)``."""
    notes = {str(n.get("x")): n.get("text") for n in annotations if n.get("x") is not None and n.get("text")}
    lines = []
    for s in stats.get("series") or []:
        first, last = s["first"][1], s["last"][1]
        change = F.percent_change(first, last)
        unit = _unit_of(spec)
        bare = dict(spec, units={}, _units={}) if unit and unit not in F.CURRENCIES else dict(spec)
        parts = ["{} {} → {}{}".format(s["name"], G.num(first, bare), G.num(last, dict(spec)),
                                        " ({})".format(F.pct(change)) if change is not None else "")]
        if s.get("max") and s["n"] > 2:
            parts.append("max {} {}".format(s["max"][0], G.num(s["max"][1], dict(spec))))
            parts.append("min {} {}".format(s["min"][0], G.num(s["min"][1], dict(spec))))
        elif s.get("trend"):
            parts.append(G.arrow(s["trend"]["sign"]))
        step = s.get("step")
        if step and s["n"] > 2:
            note = notes.get(str(step[0]))
            fmt = _fmt_of(spec) or ("compact" if abs(step[1]) >= 1e4 else None)
            parts.append("largest step {} at {}{}".format(F.signed(step[1], fmt, _unit_of(spec)), step[0],
                                                          " ({})".format(note) if note else ""))
        lines.append(G.join(parts))
    cross = stats.get("crossings")
    if cross:
        lines.append("{} and {} cross {} time{}".format(cross[0], cross[1], cross[2], "" if cross[2] == 1 else "s") if cross[2]
                     else "{} stays {} {}".format(cross[0], "above" if _above(stats) else "below", cross[1]))
    return lines


def _above(stats: Mapping[str, Any]) -> bool:
    found = stats.get("series") or []
    return len(found) >= 2 and found[0]["last"][1] >= found[1]["last"][1]


def _fmt_of(spec: Mapping[str, Any]) -> Optional[str]:
    fmt = (spec.get("format") or {}).get("y") if isinstance(spec.get("format"), dict) else None
    return fmt


def _unit_of(spec: Mapping[str, Any]) -> Optional[str]:
    unit = (spec.get("units") or {}).get("y") if isinstance(spec.get("units"), dict) else None
    if unit is None and isinstance(spec.get("_units"), dict):
        unit = spec["_units"].get("y")
    return unit


# --------------------------------------------------------------------------
# parts of a whole (pie, donut, funnel)


def pairs(spec: Mapping[str, Any], table: Table, category: str, value: Optional[str], *, cap: int, top: Optional[int] = None,
          order: str = "-value") -> Dict[str, Any]:
    """``(label, value)`` per category, aggregated (``sum`` when rows repeat), ordered (``-value`` by default, ``none``
    keeps the data's order), and cut to ``top`` (at most ``cap``): the rest merged into "Other", or dropped with
    ``other: false``. ``{"items": [[label, value]], "how", "capped", "_warnings", "total"}``."""
    how = how_of(spec, table, [category]) if value else "count"
    cells = _data.aggregate(table, list(range(table.rows)), [category], value, how if how != "none" else "last")
    column = table.by_name[category]
    items = [(_cat_label(key[0], column, spec, "category"), v if v is not None else 0.0) for key, v in cells.items()]
    merged: Dict[str, List[float]] = {}
    firsts: List[str] = []
    for label, v in items:
        if label not in merged:
            firsts.append(label)
        merged.setdefault(label, []).append(v)
    items = [(label, math.fsum(merged[label])) for label in firsts]
    if order in ("value", "-value", "y", "-y"):
        items.sort(key=lambda kv: ((-1 if order.startswith("-") else 1) * kv[1], kv[0]))
    elif order in ("x", "-x"):
        items.sort(key=lambda kv: (kv[0].lower(), kv[0]), reverse=order == "-x")
    total = math.fsum(v for _k, v in items)
    limit = min(cap, int(top)) if isinstance(top, int) else cap
    capped: List[str] = []
    if len(items) > limit:
        keep_n = limit if spec.get("other") is False else max(1, limit)
        ranked = sorted(range(len(items)), key=lambda i: (-abs(items[i][1]), items[i][0]))
        kept = set(ranked[:keep_n])
        rest = [items[i] for i in range(len(items)) if i not in kept]
        items = [items[i] for i in range(len(items)) if i in kept]
        if spec.get("other") is False:
            capped.append("dropped {} of {} {} values (other: false)".format(len(rest), len(rest) + len(items), category))
        else:
            items.append((OTHER, math.fsum(v for _k, v in rest)))
            capped.append("{} of {} {} values folded into {}".format(len(rest), len(rest) + len(items) - 1, category, OTHER))
    return {"items": [[k, _data.compact_number(v)] for k, v in items], "how": how, "capped": capped, "total": total,
            "_warnings": [{"code": "chart_capped", "message": n} for n in capped], "count": len(merged)}
