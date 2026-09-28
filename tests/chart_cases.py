"""Helpers the per-type chart tests share: compile a spec over rows or a guide file, draw it, count what it drew."""
from __future__ import annotations

import json
from collections import Counter
from typing import Any, Dict, List, Optional, Tuple

from herdr_team import canvas_charts as CC
from herdr_team.canvas_charts import _data
from chart_conformance import GUIDES

BOX = (560.0, 340.0)


def run(spec: Dict[str, Any], box: Tuple[float, float] = BOX) -> Tuple[CC.Compiled, List[Dict[str, Any]]]:
    normal, _warnings = CC.normalize_spec(spec)
    if normal.get("data"):
        path = GUIDES / normal["data"]
        table = _data.parse(path.read_bytes(), path.suffix, normal["data"])
    else:
        table = _data.inline(normal["rows"], normal.get("columns"))
    found = CC.compile(normal, table, box, normal.get("data"))
    chart = CC.get(normal["type"])
    drawing = chart.draw(found.spec, found.model, found.frame, (0.0, 0.0, box[0], box[1])) if chart.draw else []
    return found, drawing


def example(name: str) -> Dict[str, Any]:
    return json.loads(CC.get(name).example)


def kinds(drawing: List[Dict[str, Any]]) -> Counter:
    return Counter(p["k"] for p in drawing)


def texts(drawing: List[Dict[str, Any]]) -> List[str]:
    out: List[str] = []
    for prim in drawing:
        if prim["k"] == "text":
            out += [line["t"] for line in prim["lines"]]
        out += texts(prim.get("items") or [])
    return out


def series(option: Dict[str, Any], kind: Optional[str] = None) -> List[Dict[str, Any]]:
    return [s for s in option["series"] if kind is None or s["type"] == kind]
