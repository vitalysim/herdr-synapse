"""Treemaps (canvas v2 phase 3, 2.4): a hierarchy of parts as nested rectangles, each as big as its value.

``path`` names one to three columns, outermost first (``["team", "service"]``),
and ``value`` sizes the leaves (summed when rows repeat). ``depth`` draws fewer
levels than the path has. The layout is squarified in Python (the same aspect
target ECharts uses), so the agent's picture and the page agree on which part is
biggest and where it sits. At most 400 leaves: the smallest past that fold into
"Other".
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team.canvas_charts import Channel, ChartType, _data
from herdr_team.canvas_charts import _draw as DR
from herdr_team.canvas_charts import _format as F
from herdr_team.canvas_charts import _frame as FR
from herdr_team.canvas_charts import _gist as G
from herdr_team.canvas_charts import _option as O
from herdr_team.canvas_charts import _scale as S
from herdr_team.canvas_charts import _series as SE
from herdr_team.canvas_charts import _vegalite as VL
from herdr_team.canvas_charts._frame import Frame

ORDER = 100
CAP_LEAVES = 400
#: ECharts' squarify aspect target (the golden ratio), so both layouts cut the same way.
SQUARE_RATIO = 0.5 * (1 + math.sqrt(5))
HEADER = 18.0
GAP = 2.0


def normalize(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
    path = spec.get("path") or []
    if spec.get("depth") is not None and int(spec["depth"]) > len(path):
        raise _data.invalid("depth", "depth is at most the path's length ({})".format(len(path)))
    value = spec.get("value")
    if value and any(v is not None and v < 0 for v in table.by_name[value].values):
        raise _data.refuse("chart_refused", "a treemap sizes parts by value; {} has negative values".format(value), field="value")
    return spec


def _tree(spec: Mapping[str, Any], table: _data.Table) -> Tuple[List[Dict[str, Any]], List[str], int]:
    path = list(spec.get("path") or [])[:int(spec.get("depth") or len(spec.get("path") or []))]
    value = spec.get("value")
    how = SE.how_of(spec, table, path) if value else "count"
    cells = _data.aggregate(table, list(range(table.rows)), path, value, how if how != "none" else "last")
    leaves = [([SE.NULL if v is None else str(v) for v in key], float(v or 0.0)) for key, v in cells.items() if (v or 0.0) > 0]
    leaves.sort(key=lambda kv: (-kv[1], kv[0]))
    capped: List[str] = []
    count = len(leaves)
    if len(leaves) > CAP_LEAVES:
        rest = leaves[CAP_LEAVES - 1:]
        leaves = leaves[:CAP_LEAVES - 1] + [([SE.OTHER] * len(path), math.fsum(v for _k, v in rest))]
        capped.append("{} of {} leaves folded into {}".format(len(rest), count, SE.OTHER))
    root: Dict[str, Any] = {"children": {}}
    for names, v in leaves:
        node = root
        for name in names:
            node = node["children"].setdefault(name, {"name": name, "children": {}})
        node["leaf"] = node.get("leaf", 0.0) + v

    def build(node: Mapping[str, Any]) -> List[Dict[str, Any]]:
        out = []
        for child in node["children"].values():
            kids = build(child)
            total = math.fsum(k["value"] for k in kids) if kids else float(child.get("leaf") or 0.0)
            item: Dict[str, Any] = {"name": child["name"], "value": _data.compact_number(total)}
            if kids:
                item["children"] = kids
            out.append(item)
        out.sort(key=lambda n: (-float(n["value"] or 0.0), n["name"]))
        return out

    return build(root), capped, count


def model(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
    tree, capped, count = _tree(spec, table)
    return {"tree": tree, "leaves": count, "capped": capped, "_warnings": [{"code": "chart_capped", "message": c} for c in capped]}


def squarify(values: Sequence[float], x: float, y: float, w: float, h: float) -> List[Tuple[float, float, float, float]]:
    """Rectangles for ``values`` (sorted biggest first) filling ``(x, y, w, h)``, rows laid along the shorter side and
    kept while they bring the worst aspect ratio closer to ``SQUARE_RATIO``."""
    total = math.fsum(values)
    out: List[Tuple[float, float, float, float]] = []
    if total <= 0 or w <= 0 or h <= 0:
        return [(x, y, 0.0, 0.0) for _ in values]
    scale = w * h / total
    areas = [v * scale for v in values]
    index = 0
    while index < len(areas):
        side = min(w, h)
        row = [areas[index]]
        index += 1

        def worst(row_areas: Sequence[float]) -> float:
            # ECharts' squarify: the worst aspect of the row against the square ratio target.
            s = math.fsum(row_areas)
            present = [a for a in row_areas if a > 0]
            if s <= 0 or not present:
                return float("inf")
            ls, ss = side * side * SQUARE_RATIO, s * s
            return max(ls * max(present) / ss, ss / (ls * min(present)))

        while index < len(areas) and worst(row + [areas[index]]) <= worst(row):
            row.append(areas[index])
            index += 1
        s = math.fsum(row)
        thick = s / side if side else 0.0
        offset = 0.0
        for a in row:
            length = a / thick if thick else 0.0
            if w >= h:
                out.append((x, y + offset, thick, length))
            else:
                out.append((x + offset, y, length, thick))
            offset += length
        if w >= h:
            x, w = x + thick, w - thick
        else:
            y, h = y + thick, h - thick
    return out


def layout(tree: Sequence[Mapping[str, Any]], box: Tuple[float, float, float, float], depth: int = 0,
           top: int = -1) -> List[Dict[str, Any]]:
    """Every node's rectangle: ``{"name", "value", "depth", "top" (its top-level index), "box", "leaf"}``, parents before
    children; a parent keeps a header band for its name when there is room."""
    x, y, w, h = box
    rects = squarify([float(n.get("value") or 0.0) for n in tree], x, y, w, h)
    out: List[Dict[str, Any]] = []
    for index, (node, rect) in enumerate(zip(tree, rects)):
        owner = index if depth == 0 else top
        kids = node.get("children") or []
        out.append({"name": node["name"], "value": node.get("value"), "depth": depth, "top": owner, "box": list(rect), "leaf": not kids})
        rx, ry, rw, rh = rect
        if kids and rw > 2 * GAP + 4 and rh > HEADER + 2 * GAP + 4:
            out += layout(kids, (rx + GAP, ry + HEADER, rw - 2 * GAP, rh - HEADER - GAP), depth + 1, owner)
        elif kids:
            out[-1]["leaf"] = True
    return out


def frame(spec: Dict[str, Any], model: Dict[str, Any], box: Tuple[float, float]) -> Frame:
    base = FR.radial(box, (), "none")
    px, py, pw, ph = base.plot
    rects = layout(model.get("tree") or [], (px, py, pw, ph))
    hidden = sum(1 for r in rects if r["leaf"] and (r["box"][2] < 24 or r["box"][3] < FR.LH))
    leaves = sum(1 for r in rects if r["leaf"])
    notes = list(base.notes)
    if hidden:
        notes.append("{} of {} parts too small to name".format(hidden, leaves))
    return Frame(box=base.box, plot=base.plot, axes={}, legend=None, font=base.font, notes=tuple(notes),
                 legible=base.legible and hidden * 2 <= max(1, leaves), extra={})


def _paint(spec: Mapping[str, Any], rect: Mapping[str, Any]) -> str:
    if rect["name"] in (spec.get("highlight") or []):
        return "chart.highlight"
    return S.cat(int(rect["top"]))


def draw(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, slot: Tuple[float, float, float, float]) -> List[Dict[str, Any]]:
    pen = DR.Pen(slot[0], slot[1])
    for rect in layout(model.get("tree") or [], frame.plot):
        x, y, w, h = rect["box"]
        if w <= 0.5 or h <= 0.5:
            continue
        paint = _paint(spec, rect)
        pen.rect(x, y, w, h, paint, "chart.paper", 1, op=1.0 if rect["leaf"] else 0.35)
        if rect["leaf"]:
            name = FR.fit_text(rect["name"], w - 8)
            if name and h >= FR.LH + 4 and w >= 24:
                pen.text(name, x + 4, y + FR.LH / 2.0 + 2, "start", S.on(paint), FR.FONT, 600)
                value = G.num(float(rect["value"] or 0.0), dict(spec), "value")
                if h >= 2 * FR.LH + 6 and FR.text_w(value) <= w - 8:
                    pen.text(value, x + 4, y + FR.LH * 1.5 + 4, "start", S.on(paint))
        else:
            name = FR.fit_text(rect["name"], w - 8)
            if name:
                pen.text(name, x + 4, y + HEADER / 2.0, "start", "chart.ink", FR.FONT, 600)
    return pen.items


def _echarts_tree(spec: Mapping[str, Any], tree: Sequence[Mapping[str, Any]], top: int = -1, depth: int = 0) -> List[Dict[str, Any]]:
    out = []
    for index, node in enumerate(tree):
        owner = index if depth == 0 else top
        item: Dict[str, Any] = {"name": node["name"], "value": node.get("value")}
        if node.get("children"):
            item["children"] = _echarts_tree(spec, node["children"], owner, depth + 1)
        out.append(item)
    return out


def doc(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    return {"datasets": [], "refs": {"tree": _echarts_tree(spec, model.get("tree") or [])}}


def option(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, gist: Sequence[str]) -> Dict[str, Any]:
    out = O.base(gist)
    px, py, pw, ph = frame.plot
    out.update(tooltip=O.tooltip("item"), legend={"show": False})
    tops = ["chart.highlight" if n["name"] in (spec.get("highlight") or []) else S.cat(i) for i, n in enumerate(model.get("tree") or [])]
    levels = [{"color": tops, "colorMappingBy": "index", "itemStyle": {"borderColor": "chart.paper", "borderWidth": 1, "gapWidth": GAP}},
              {"itemStyle": {"borderColor": "chart.paper", "borderWidth": 1, "gapWidth": 1}, "upperLabel": {"show": True, "height": HEADER,
                                                                                                            "color": "chart.ink"}},
              {"itemStyle": {"borderColor": "chart.paper", "borderWidth": 1, "gapWidth": 1}}]
    out["series"] = [{"type": "treemap", "name": " / ".join(spec.get("path") or []), "left": round(px, 2), "top": round(py, 2),
                      "width": round(pw, 2), "height": round(ph, 2), "roam": False, "nodeClick": False, "breadcrumb": {"show": False},
                      "squareRatio": round(SQUARE_RATIO, 6), "data": {"$doc": "tree"}, "levels": levels,
                      "label": {"show": True, "fontFamily": "$sans", "fontSize": FR.FONT, "fontWeight": 600,
                                "position": "insideTopLeft"},
                      "upperLabel": {"show": len(spec.get("path") or []) > 1, "height": HEADER, "color": "chart.ink", "fontFamily": "$sans"}}]
    return out


def stats(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    tree = model.get("tree") or []
    total = math.fsum(float(n.get("value") or 0.0) for n in tree)
    leaves: List[Tuple[str, float]] = []

    def walk(nodes: Sequence[Mapping[str, Any]], prefix: str) -> None:
        for node in nodes:
            name = prefix + node["name"]
            if node.get("children"):
                walk(node["children"], name + " / ")
            else:
                leaves.append((name, float(node.get("value") or 0.0)))

    walk(tree, "")
    leaves.sort(key=lambda kv: (-kv[1], kv[0]))
    return {"total": total, "top": [[k, v, v / total if total else 0.0] for k, v in leaves[:3]],
            "shares": [[n["name"], float(n.get("value") or 0.0) / total if total else 0.0] for n in tree[:6]], "groups": len(tree)}


def gist(spec: Dict[str, Any], model: Dict[str, Any], stats: Mapping[str, Any]) -> List[str]:
    lines = [G.join(["path={}".format(" / ".join(spec.get("path") or [])), "value={}".format(spec.get("value") or "count"),
                     "{} leaves".format(model.get("leaves", 0)), str(spec.get("_source") or "")])]
    if stats.get("top"):
        lines.append("largest: " + " · ".join("{} {} ({})".format(k, G.num(v, dict(spec), "value"), F.share(s)) for k, v, s in stats["top"]))
    if stats.get("shares") and len(spec.get("path") or []) > 1:
        lines.append("{}: ".format((spec.get("path") or ["top"])[0]) + " · ".join("{} {}".format(k, F.share(s)) for k, s in stats["shares"]))
    lines.append("total {}".format(G.num(float(stats.get("total") or 0.0), dict(spec), "value")))
    for note in model.get("capped") or []:
        lines.append("capped: " + note)
    return lines


def vegalite(spec: Dict[str, Any], model: Dict[str, Any]) -> Dict[str, Any]:
    rows = []
    for node in model.get("tree") or []:
        rows.append({"part": node["name"], "value": node.get("value")})
    return VL.compat("bar", rows, {"y": {"field": "part", "type": "nominal", "sort": None, "title": (spec.get("path") or [""])[0]},
                                   "x": {"field": "value", "type": "quantitative", "title": spec.get("value") or "count"}}, {"width": "container"})


def min_box(spec: Dict[str, Any], model: Dict[str, Any]) -> Tuple[float, float]:
    return 320.0, 220.0


CHARTS = (
    ChartType(name="treemap",
              channels=(Channel("path", required=True, types=("nominal", "ordinal", "temporal"), many=True,
                                doc="one to three columns, outermost first"),
                        Channel("value", required=True, types=("quantitative",), aggregate=True, doc="sizes the parts (summed when rows repeat)")),
              options=("depth",), frame_kind="radial", normalize=normalize, model=model, stats=stats, doc=doc, frame=frame, option=option,
              draw=draw, gist=gist, vegalite=vegalite, echarts=("TreemapChart", "TooltipComponent", "AriaComponent"), min_box=min_box,
              default_box=(560, 380), caps="400 leaves", doc_line="a hierarchy of parts as nested rectangles sized by value",
              example='{"op": "chart", "id": "cloud", "intent": "where the cloud bill goes", "title": "Cloud spend by team and service", '
                      '"type": "treemap", "rows": [{"team": "Data", "service": "Warehouse", "usd": 4200}, {"team": "Data", "service": "Pipelines", '
                      '"usd": 1800}, {"team": "Web", "service": "CDN", "usd": 1300}, {"team": "Web", "service": "Compute", "usd": 2600}, '
                      '{"team": "ML", "service": "GPUs", "usd": 5100}, {"team": "ML", "service": "Storage", "usd": 700}], '
                      '"path": ["team", "service"], "value": "usd", "units": {"value": "$"}}',
              order=100),
)
