"""The Vega-Lite escape hatch (canvas v2 phase 3, 2.8) and the frozen v1 page's charts (D18).

``spec`` is today's op field: a raw Vega-Lite spec the page renders through
``vega-interpreter`` (no eval), with today's checks (200 KB, depth 64, no
``url``/``href`` anywhere). It reads back as a gist (``gist_from_spec``) and,
when it is a plain single view, is drawn by Python through the flat model
(``to_flat``).

The other direction serves the frozen v1 (Excalidraw) page: every flat chart
also stores a small Vega-Lite spec over its model rows (each type's
``vegalite`` hook), or a text mark listing its gist lines (``gist_card``), so
v1 never shows "could not be drawn" for a chart made on v2.

Pure, stdlib only.
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Mapping, Optional, Sequence

from herdr_team.canvas_charts._data import Table, refuse

MAX_CHART_SPEC_BYTES = 200 * 1024
#: How deep a chart spec may nest (Vega-Lite ``layer``/``concat``); deeper is refused, never skipped.
MAX_SPEC_DEPTH = 64
MARKS = {"bar": "bar", "line": "line", "area": "area", "point": "scatter", "circle": "scatter", "square": "scatter", "tick": "scatter",
         "arc": "pie", "rect": "heatmap", "boxplot": "box"}
VIEWS = ("layer", "hconcat", "vconcat", "concat", "facet", "repeat")


def find_key(obj: Any, keys: Sequence[str], depth: int = 0) -> Optional[str]:
    """The first key named one of ``keys`` anywhere in ``obj``; a spec too deep to search is refused, never passed."""
    if depth > MAX_SPEC_DEPTH:
        raise refuse("chart_refused", "the chart spec nests deeper than {} levels; flatten it".format(MAX_SPEC_DEPTH),
                     limit="MAX_SPEC_DEPTH", max=MAX_SPEC_DEPTH)
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and key.lower() in keys:
                return key
            found = find_key(value, keys, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = find_key(value, keys, depth + 1)
            if found:
                return found
    return None


def check_spec(spec: Any) -> str:
    """A raw Vega-Lite spec checked as today (2.8): an object, 200 KB or less, nothing it loads itself; its JSON text."""
    if not isinstance(spec, dict):
        raise refuse("op_invalid", "spec is a Vega-Lite object", field="spec")
    raw = json.dumps(spec, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > MAX_CHART_SPEC_BYTES:
        raise refuse("chart_refused", "the chart spec is over {} KB; put the data in a file under artifacts/".format(MAX_CHART_SPEC_BYTES // 1024),
                     limit="MAX_CHART_SPEC_BYTES", max=MAX_CHART_SPEC_BYTES)
    found = find_key(spec, ("url", "href"))
    if found:
        raise refuse("chart_refused", "a chart spec may not load anything itself: remove {!r} and pass data (a file under artifacts/)".format(found),
                     key=found)
    return raw


def _field(enc: Any) -> Optional[str]:
    if isinstance(enc, dict) and isinstance(enc.get("field"), str):
        return enc["field"]
    return None


def _mark(spec: Mapping[str, Any]) -> Optional[str]:
    mark = spec.get("mark")
    if isinstance(mark, dict):
        mark = mark.get("type")
    return mark if isinstance(mark, str) else None


def inline_values(spec: Mapping[str, Any]) -> Optional[List[Any]]:
    data = spec.get("data")
    if isinstance(data, dict) and isinstance(data.get("values"), list):
        return data["values"]
    return None


def to_flat(spec: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """A single-view spec with a plain mark (bar, line, area, point, arc, rect) and plain field encodings as the flat
    chart spec Python draws, or None (a layered, faceted or transformed spec keeps the gist card)."""
    if any(key in spec for key in VIEWS + ("transform",)):
        return None
    mark = _mark(spec)
    kind = MARKS.get(mark or "")
    encoding = spec.get("encoding") if isinstance(spec.get("encoding"), dict) else None
    if kind is None or encoding is None or any(key in encoding for key in ("row", "column", "facet")):
        return None
    out: Dict[str, Any] = {"type": kind}
    agg = None
    for channel in ("x", "y", "color", "size", "theta"):
        enc = encoding.get(channel)
        if enc is None:
            continue
        name = _field(enc)
        if name is None:
            if isinstance(enc, dict) and enc.get("aggregate") == "count":
                agg = "count"
                continue
            return None
        if isinstance(enc, dict) and isinstance(enc.get("aggregate"), str):
            agg = enc["aggregate"] if enc["aggregate"] in ("sum", "mean", "median", "min", "max", "count") else None
            if agg is None:
                return None
        if isinstance(enc, dict) and (enc.get("bin") or enc.get("timeUnit")):
            if kind == "bar" and enc.get("bin"):
                out["type"] = "histogram"
            else:
                return None
        out[channel] = name
    if kind == "pie":
        if "theta" not in out or "color" not in out:
            return None
        out = {"type": "pie", "category": out["color"], "value": out["theta"]}
    elif kind == "heatmap":
        color = out.pop("color", None)
        if color is None or "x" not in out or "y" not in out:
            return None
        out["value"] = color
    elif out["type"] == "histogram":
        out.pop("y", None)
        if "x" not in out:
            return None
    elif "x" not in out or ("y" not in out and agg != "count"):
        return None
    if agg is not None:
        out["aggregate"] = agg
    x_enc = encoding.get("x") if isinstance(encoding.get("x"), dict) else {}
    if out["type"] in ("bar", "line", "area") and "sort" not in x_enc:
        out["sort"] = "x"  # Vega-Lite orders a nominal axis by its values unless told otherwise
    return out


def gist_from_spec(spec: Mapping[str, Any], table: Optional[Table]) -> List[str]:
    """What a raw Vega-Lite spec shows, in words: its mark and fields with their extremes, or its views."""
    views = [key for key in VIEWS if key in spec]
    fields: List[str] = []

    def collect(node: Any) -> None:
        if isinstance(node, dict):
            name = node.get("field")
            if isinstance(name, str) and name not in fields:
                fields.append(name)
            for value in node.values():
                collect(value)
        elif isinstance(node, list):
            for value in node:
                collect(value)

    collect(spec.get("encoding") if not views else spec)
    source = "{} {} row{}".format(table.label, table.rows, "" if table.rows == 1 else "s") if table is not None else \
        ("inline values" if inline_values(spec) else "no data")
    if views:
        count = sum(len(spec[key]) if isinstance(spec.get(key), list) else 1 for key in views)
        lines = ["Vega-Lite {} spec, {} view{} · fields {} · {}".format("/".join(views), count, "" if count == 1 else "s",
                                                                         ", ".join(fields[:8]) or "none", source)]
    else:
        lines = ["Vega-Lite {} · fields {} · {}".format(_mark(spec) or "chart", ", ".join(fields[:8]) or "none", source)]
    if table is not None:
        encoding = spec.get("encoding") if isinstance(spec.get("encoding"), dict) else {}
        for channel in ("x", "y"):
            name = _field(encoding.get(channel))
            column = table.by_name.get(name) if name else None
            if column is None:
                continue
            if column.type == "quantitative":
                present = [v for v in column.values if v is not None]
                if present:
                    from herdr_team.canvas_charts import _format as F

                    fmt = "compact" if max(abs(min(present)), abs(max(present))) >= 1e4 else None
                    lines.append("{}={} from {} to {}".format(channel, name, F.fmt_number(min(present), fmt), F.fmt_number(max(present), fmt)))
            else:
                lines.append("{}={} ({} {}: {})".format(channel, name, column.distinct, column.type, ", ".join(column.sample(4))))
        color = _field(encoding.get("color"))
        column = table.by_name.get(color) if color else None
        if column is not None and column.type != "quantitative":
            lines.append("color={} ({}: {})".format(color, column.distinct, ", ".join(column.sample(6))))
    return lines


def gist_card(lines: Sequence[str], title: str = "") -> Dict[str, Any]:
    """A Vega-Lite spec that draws a chart's gist lines as text (the v1 page's fallback for a type it cannot draw)."""
    rows = [{"i": index, "t": str(line)[:160]} for index, line in enumerate(list(lines)[:8])] or [{"i": 0, "t": title or "chart"}]
    return {"data": {"values": rows}, "mark": {"type": "text", "align": "left", "baseline": "top", "fontSize": 12},
            "encoding": {"y": {"field": "i", "type": "ordinal", "axis": None}, "text": {"field": "t"}},
            "width": 480, "config": {"view": {"stroke": None}}}


def compat(mark: Any, rows: List[Dict[str, Any]], encoding: Dict[str, Any], extra: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """A small Vega-Lite spec over model rows for the v1 page (D18)."""
    spec: Dict[str, Any] = {"data": {"values": rows}, "mark": mark, "encoding": encoding}
    if extra:
        spec.update(extra)
    return spec
