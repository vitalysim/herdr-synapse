"""The raw ECharts escape hatch's sanitiser (canvas v2 phase 3, 2.8 and D7): one allow table, applied on both sides.

``sanitize(option) -> (clean, stripped)`` keeps an agent's raw ECharts option to
declarative data: allowed top-level keys and series types only, no key that
reaches outside the chart (``toolbox``, ``link``, ``extraCssText`` ...), no
formatter that is not a string template, no string that loads anything
(``https:``, ``data:``, ``javascript:``, ``image://`` ...), bounded in size, depth
and numbers, with tooltips forced into rich text. What it strips it lists (the
check ``chart_escape`` says so); what it cannot allow it refuses, naming the path.

The table lives in Python (``TOP``, ``SERIES``, ...) and is written to
``assets/canvas/echarts-allow.json`` by ``python3 -m herdr_team.canvas_charts._sanitize
--write`` (``--check`` fails when it is stale). The page imports that file and
runs the same rules again before ``setOption`` (``web/src/v2/charts/sanitize.js``),
and both are held to ``tests/fixtures/charts/sanitize-vectors.json``.

Pure, stdlib only (``main`` writes the file).
"""
from __future__ import annotations

import argparse
import copy
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team.canvas_charts._data import refuse

ALLOW_PATH = Path(__file__).resolve().parent.parent.parent / "assets" / "canvas" / "echarts-allow.json"
VECTORS_PATH = Path(__file__).resolve().parent.parent.parent / "tests" / "fixtures" / "charts" / "sanitize-vectors.json"

TOP = ("series", "dataset", "xAxis", "yAxis", "grid", "legend", "tooltip", "color", "radar", "polar", "radiusAxis", "angleAxis",
       "parallel", "parallelAxis", "singleAxis", "calendar", "visualMap", "dataZoom", "textStyle", "aria", "markLine", "markPoint")
#: The series the page bundles (2.4's types and the raw list); the GL types are drawn through echarts-gl (3.11).
SERIES = ("bar", "line", "scatter", "effectScatter", "pie", "heatmap", "boxplot", "funnel", "treemap", "sankey", "radar", "gauge",
          "parallel", "candlestick", "themeRiver", "bar3D", "scatter3D", "surface")
DENY_KEYS = ("extraCssText", "toolbox", "dataView", "link", "sublink", "target", "triggerEvent", "transform", "graphic", "title", "brush",
             "timeline", "geo", "bmap", "map", "html", "tooltipFormatter", "renderItem")
DENY_VALUES = ("http:", "https:", "//", "data:", "javascript:", "vbscript:", "blob:", "file:", "image://", "url(")
LIMITS = {"bytes": 64 * 1024, "depth": 32, "numbers": 20_000, "string": 1000}
FORCED = {"animation": False, "tooltip": {"renderMode": "richText", "confine": True}}


def table() -> Dict[str, Any]:
    """The allow table both sanitisers read (``assets/canvas/echarts-allow.json``)."""
    return {"v": 1, "source": "herdr_team/canvas_charts/_sanitize.py; run python3 -m herdr_team.canvas_charts._sanitize --write",
            "top": list(TOP), "series": list(SERIES), "deny_keys": list(DENY_KEYS), "deny_values": list(DENY_VALUES),
            "limits": dict(LIMITS), "forced": copy.deepcopy(FORCED)}


def _walk_limits(value: Any, path: str, depth: int, counts: Dict[str, int]) -> None:
    if depth > LIMITS["depth"]:
        raise refuse("chart_refused", "the ECharts option nests deeper than {} levels at {}".format(LIMITS["depth"], path), path=path,
                     limit="depth", max=LIMITS["depth"])
    if isinstance(value, bool) or value is None:
        return
    if isinstance(value, (int, float)):
        counts["numbers"] += 1
        if counts["numbers"] > LIMITS["numbers"]:
            raise refuse("chart_refused", "the ECharts option holds more than {} numbers; put the data in a file and use a chart type".format(
                LIMITS["numbers"]), limit="numbers", max=LIMITS["numbers"])
        return
    if isinstance(value, str):
        if len(value) > LIMITS["string"]:
            raise refuse("chart_refused", "{} is a string of {} characters; the limit is {}".format(path, len(value), LIMITS["string"]),
                         path=path, limit="string", max=LIMITS["string"])
        lowered = value.strip().lower()
        if any(lowered.startswith(prefix) for prefix in DENY_VALUES) or "javascript:" in lowered:
            raise refuse("chart_refused", "{} loads something ({}...); an option is data only".format(path, value.strip()[:24]), path=path)
        return
    if isinstance(value, dict):
        for key, item in value.items():
            _walk_limits(item, "{}.{}".format(path, key), depth + 1, counts)
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _walk_limits(item, "{}[{}]".format(path, index), depth + 1, counts)
        return
    raise refuse("chart_refused", "{} is not JSON data".format(path), path=path)


def _strip(value: Any, path: str, stripped: List[str]) -> Any:
    if isinstance(value, dict):
        out: Dict[str, Any] = {}
        for key, item in value.items():
            where = "{}.{}".format(path, key) if path else str(key)
            if key in DENY_KEYS and not (key == "target" and _node_ref(value, item)):
                stripped.append(where)
                continue
            if key == "formatter" and not isinstance(item, str):
                stripped.append(where)
                continue
            if key in ("image", "backgroundColor", "color") and isinstance(item, dict) and "image" in item:
                stripped.append(where)
                continue
            out[key] = _strip(item, where, stripped)
        return out
    if isinstance(value, list):
        return [_strip(item, "{}[{}]".format(path, index), stripped) for index, item in enumerate(value)]
    return value


def _node_ref(holder: Mapping[str, Any], value: Any) -> bool:
    """A ``target`` naming a node (a sankey or graph link's end), not a link's window target."""
    return "link" not in holder and "sublink" not in holder and "source" in holder and \
        str(value).strip().lower() not in ("blank", "self", "_blank", "_self", "parent", "_parent", "top", "_top")


def sanitize(option: Any) -> Tuple[Dict[str, Any], List[str]]:
    """``(clean option, stripped paths)``; ``chart_refused`` for what cannot be allowed (naming the path)."""
    if not isinstance(option, dict):
        raise refuse("chart_refused", "echarts is an ECharts option: a JSON object")
    try:
        raw = json.dumps(option, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        raise refuse("chart_refused", "the ECharts option is not JSON data")
    if len(raw.encode("utf-8")) > LIMITS["bytes"]:
        raise refuse("chart_refused", "the ECharts option is over {} KB; put the data in a file under artifacts/ and use a chart type".format(
            LIMITS["bytes"] // 1024), limit="bytes", max=LIMITS["bytes"])
    _walk_limits(option, "echarts", 0, {"numbers": 0})
    stripped: List[str] = []
    top: Dict[str, Any] = {}
    for key, value in option.items():
        if key not in TOP:
            stripped.append(str(key))
            continue
        top[key] = value
    series = top.get("series")
    if series is None:
        raise refuse("chart_refused", "the ECharts option has no series; one of: " + ", ".join(SERIES))
    if isinstance(series, dict):
        series = [series]
    if not isinstance(series, list) or not series:
        raise refuse("chart_refused", "echarts.series is a list of series")
    for index, item in enumerate(series):
        if not isinstance(item, dict) or item.get("type") not in SERIES:
            kind = item.get("type") if isinstance(item, dict) else None
            raise refuse("chart_refused", "echarts.series[{}] is a {} series; the page draws: {}".format(index, json.dumps(kind), ", ".join(SERIES)),
                         path="echarts.series[{}].type".format(index), allowed=list(SERIES))
    top["series"] = series
    clean = _strip(top, "", stripped)
    tooltip = clean.get("tooltip") if isinstance(clean.get("tooltip"), dict) else {}
    clean["tooltip"] = dict(tooltip, **FORCED["tooltip"])
    clean["animation"] = FORCED["animation"]
    return clean, stripped


def vectors() -> List[Dict[str, Any]]:
    """The shared hostile corpus (``sanitize-vectors.json``): each input with its expected output, or its refusal."""
    cases: List[Tuple[str, Any]] = [
        ("a radar", {"radar": {"indicator": [{"name": "Rust", "max": 5}, {"name": "Web", "max": 5}]},
                     "series": [{"type": "radar", "data": [{"name": "a", "value": [4, 3]}]}]}),
        ("a bar with a template formatter", {"xAxis": {"type": "category", "data": ["a", "b"]}, "yAxis": {"type": "value"},
                                             "series": [{"type": "bar", "data": [1, 2], "label": {"show": True, "formatter": "{c} ms"}}]}),
        ("a title and a toolbox are stripped", {"title": {"text": "x"}, "toolbox": {"feature": {"saveAsImage": {}}},
                                                "series": [{"type": "pie", "data": [{"name": "a", "value": 1}]}]}),
        ("an unknown top-level key is stripped", {"graphic": [{"type": "image", "style": {"image": "x.png"}}], "series": [{"type": "line", "data": [1]}]}),
        ("a function-like formatter object is stripped", {"series": [{"type": "bar", "data": [1], "label": {"formatter": {"fn": "alert(1)"}}}]}),
        ("extraCssText anywhere is stripped", {"tooltip": {"extraCssText": "background:url(x)"}, "series": [{"type": "line", "data": [1, 2]}]}),
        ("a link is stripped", {"series": [{"type": "treemap", "data": [{"name": "a", "value": 1, "link": "x", "target": "blank"}]}]}),
        ("a sankey link's target stays", {"series": [{"type": "sankey", "data": [{"name": "a"}, {"name": "b"}],
                                                      "links": [{"source": "a", "target": "b", "value": 3}]}]}),
        ("an image background is stripped", {"series": [{"type": "bar", "data": [1], "label": {"rich": {"a": {"backgroundColor": {"image": "icon"}}}}}]}),
        ("html in a formatter is only text", {"series": [{"type": "bar", "data": [1], "label": {"formatter": "<img src=x onerror=alert(1)>"}}]}),
        ("tooltip is forced to rich text", {"tooltip": {"renderMode": "html", "confine": False}, "series": [{"type": "scatter", "data": [[1, 2]]}]}),
        ("an https url is refused", {"series": [{"type": "scatter", "symbol": "https://evil.example/x.png", "data": [[1, 2]]}]}),
        ("an image symbol is refused", {"series": [{"type": "scatter", "symbol": "image://x", "data": [[1, 2]]}]}),
        ("a data uri is refused", {"series": [{"type": "bar", "data": [1]}], "color": ["data:image/png;base64,AAAA"]}),
        ("javascript: is refused", {"series": [{"type": "bar", "data": [1], "name": " JavaScript:alert(1)"}]}),
        ("a protocol-relative url is refused", {"series": [{"type": "line", "data": [1], "name": "//evil.example"}]}),
        ("a map series is refused", {"series": [{"type": "map", "map": "world"}]}),
        ("a custom series is refused", {"series": [{"type": "custom", "renderItem": "x"}]}),
        ("no series is refused", {"xAxis": {}}),
        ("not an object is refused", ["series"]),
        ("too deep is refused", {"series": [{"type": "bar", "data": [1]}], "legend": _deep(40)}),
        ("a long string is refused", {"series": [{"type": "bar", "data": [1], "name": "x" * 1001}]}),
        ("too many numbers are refused", {"series": [{"type": "line", "data": list(range(20001))}]}),
        ("a gl bar3D passes", {"series": [{"type": "bar3D", "data": [[0, 0, 1]], "shading": "lambert"}]}),
    ]
    out = []
    for name, value in cases:
        try:
            clean, stripped = sanitize(copy.deepcopy(value))
            out.append({"name": name, "input": value, "output": clean, "stripped": stripped, "refused": None})
        except Exception as err:  # noqa: BLE001 - the corpus records any refusal by its code
            out.append({"name": name, "input": value, "output": None, "stripped": [], "refused": getattr(err, "code", type(err).__name__)})
    return out


def _deep(n: int) -> Any:
    value: Any = {"leaf": 1}
    for _ in range(n):
        value = {"a": value}
    return value


def _dump(doc: Any) -> str:
    return json.dumps(doc, indent=1, ensure_ascii=False) + "\n"


def main(argv: Sequence[str] = ()) -> int:
    parser = argparse.ArgumentParser(prog="python3 -m herdr_team.canvas_charts._sanitize",
                                     description="Write the ECharts allow table (assets/canvas/echarts-allow.json) and the shared vectors.")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--write", action="store_true", help="write assets/canvas/echarts-allow.json and tests/fixtures/charts/sanitize-vectors.json")
    mode.add_argument("--check", action="store_true", help="exit 1 when either file is stale")
    args = parser.parse_args(list(argv))
    files = {ALLOW_PATH: _dump(table()), VECTORS_PATH: _dump(vectors())}
    stale = []
    for path, text in files.items():
        try:
            current = path.read_text(encoding="utf-8")
        except OSError:
            current = ""
        if current != text:
            stale.append(path)
            if args.write:
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text(text, encoding="utf-8")
    if args.check and stale:
        sys.stderr.write("stale: {}; run python3 -m herdr_team.canvas_charts._sanitize --write\n".format(", ".join(str(p) for p in stale)))
        return 1
    if not args.write and not args.check:
        sys.stdout.write(files[ALLOW_PATH])
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
