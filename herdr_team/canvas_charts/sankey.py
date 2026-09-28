"""Sankey diagrams (canvas v2 phase 3, 2.4): flows between stages, each band as thick as what it carries.

Each row is a flow: ``source`` to ``target`` of ``value`` (summed when rows
repeat). Nodes stand in columns by their longest path from a start (a cycle is
refused, naming it), stacked by size; bands leave a node in the order of their
targets and arrive in the order of their sources, as cubic curves. ``orient:
"vertical"`` runs the columns top to bottom. At most 150 flows and 80 nodes.
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

ORDER = 110
CAP_LINKS = 150
CAP_NODES = 80
NODE_W = 12.0
NODE_GAP = 10.0
LABEL_ROOM = 110.0


def _flows(spec: Mapping[str, Any], table: _data.Table) -> Tuple[List[List[Any]], List[str]]:
    src, dst, value = spec["source"], spec["target"], spec.get("value")
    how = SE.how_of(spec, table, [src, dst]) if value else "count"
    cells = _data.aggregate(table, list(range(table.rows)), [src, dst], value, how if how != "none" else "last")
    flows = []
    for (a, b), v in cells.items():
        if a is None or b is None or v is None or v <= 0:
            continue
        if str(a) == str(b):
            raise _data.refuse("chart_refused", "{} flows into itself; a sankey's flows go from one node to another".format(a), field="source")
        flows.append([str(a), str(b), float(v)])
    flows.sort(key=lambda f: (-f[2], f[0], f[1]))
    capped: List[str] = []
    if len(flows) > CAP_LINKS:
        capped.append("drew the {} largest of {} flows".format(CAP_LINKS, len(flows)))
        flows = flows[:CAP_LINKS]
    return flows, capped


def _depths(names: Sequence[str], flows: Sequence[Sequence[Any]]) -> Dict[str, int]:
    """Each node's column: the longest path to it from a node nothing flows into; a cycle is refused, naming it."""
    out_edges: Dict[str, List[str]] = {n: [] for n in names}
    indeg: Dict[str, int] = {n: 0 for n in names}
    for a, b, _v in flows:
        out_edges[a].append(b)
        indeg[b] += 1
    depth = {n: 0 for n in names}
    ready = sorted(n for n in names if indeg[n] == 0)
    seen = 0
    while ready:
        node = ready.pop(0)
        seen += 1
        for nxt in sorted(out_edges[node]):
            depth[nxt] = max(depth[nxt], depth[node] + 1)
            indeg[nxt] -= 1
            if indeg[nxt] == 0:
                ready.append(nxt)
                ready.sort()
    if seen < len(names):
        stuck = [n for n in names if indeg[n] > 0]
        cycle = _cycle(stuck, out_edges)
        raise _data.refuse("chart_refused", "the flows loop ({}); a sankey's flows run one way: drop one of those links".format(
            " → ".join(cycle)), cycle=cycle)
    return depth


def _cycle(stuck: Sequence[str], out_edges: Mapping[str, Sequence[str]]) -> List[str]:
    remaining = set(stuck)
    start = sorted(remaining)[0]
    path = [start]
    while True:
        nxt = sorted(n for n in out_edges[path[-1]] if n in remaining)
        if not nxt:
            return path
        if nxt[0] in path:
            return path[path.index(nxt[0]):] + [nxt[0]]
        path.append(nxt[0])
        if len(path) > len(remaining) + 1:
            return path


def model(spec: Dict[str, Any], table: _data.Table) -> Dict[str, Any]:
    flows, capped = _flows(spec, table)
    names: List[str] = []
    for a, b, _v in flows:
        for n in (a, b):
            if n not in names:
                names.append(n)
    if len(names) > CAP_NODES:
        raise _data.too_big("source", "MAX_SANKEY_NODES", CAP_NODES, "the flows name {} nodes; a sankey draws at most {}: filter the data or "
                            "group small nodes".format(len(names), CAP_NODES))
    depth = _depths(names, flows)
    inflow: Dict[str, float] = {n: 0.0 for n in names}
    outflow: Dict[str, float] = {n: 0.0 for n in names}
    for a, b, v in flows:
        outflow[a] += v
        inflow[b] += v
    nodes = [{"name": n, "depth": depth[n], "value": _data.compact_number(max(inflow[n], outflow[n]))} for n in names]
    nodes.sort(key=lambda n: (n["depth"], -float(n["value"] or 0.0), n["name"]))
    return {"nodes": nodes, "links": [[a, b, _data.compact_number(v)] for a, b, v in flows], "capped": capped,
            "_warnings": [{"code": "chart_capped", "message": c} for c in capped]}


def placement(spec: Mapping[str, Any], model: Mapping[str, Any], plot: Sequence[float]) -> Dict[str, Any]:
    """Where every node and band goes inside ``plot``: node boxes by name, and each link's four edge points."""
    px, py, pw, ph = (float(v) for v in plot)
    vertical = spec.get("orient") == "vertical"
    nodes = model.get("nodes") or []
    columns = max((int(n["depth"]) for n in nodes), default=0) + 1
    across, along = (ph, pw) if vertical else (pw, ph)
    by_depth: Dict[int, List[Mapping[str, Any]]] = {}
    for n in nodes:
        by_depth.setdefault(int(n["depth"]), []).append(n)
    room = [along - NODE_GAP * (len(by_depth.get(d, [])) - 1) for d in range(columns)]
    totals = [math.fsum(float(n["value"] or 0.0) for n in by_depth.get(d, [])) for d in range(columns)]
    scale = min([r / t for r, t in zip(room, totals) if t > 0] or [1.0])
    label_room = 0.0 if vertical else min(LABEL_ROOM, across * 0.22)
    span = max(1.0, across - NODE_W - label_room)
    boxes: Dict[str, List[float]] = {}
    for d in range(columns):
        members = by_depth.get(d, [])
        used = math.fsum(float(n["value"] or 0.0) * scale for n in members) + NODE_GAP * (len(members) - 1)
        offset = (along - used) / 2.0
        at = (span * d / (columns - 1)) if columns > 1 else span / 2.0
        for n in members:
            size = max(1.0, float(n["value"] or 0.0) * scale)
            if vertical:
                boxes[n["name"]] = [px + offset, py + at, size, NODE_W]
            else:
                boxes[n["name"]] = [px + at, py + offset, NODE_W, size]
            offset += size + NODE_GAP
    out_used = {name: 0.0 for name in boxes}
    in_used = {name: 0.0 for name in boxes}

    def along_of(name: str) -> float:
        box = boxes[name]
        return box[0] if vertical else box[1]

    links = sorted(model.get("links") or [], key=lambda l: (along_of(l[0]), along_of(l[1]), l[0], l[1]))
    placed = []
    for a, b, v in sorted(links, key=lambda l: (along_of(l[0]), along_of(l[1]))):
        thick = max(0.5, float(v) * scale)
        sa, sb = boxes[a], boxes[b]
        if vertical:
            s0 = sa[0] + out_used[a]
            t0 = sb[0] + in_used[b]
            placed.append({"from": a, "to": b, "value": v, "src": [s0, sa[1] + NODE_W, thick], "dst": [t0, sb[1], thick]})
        else:
            s0 = sa[1] + out_used[a]
            t0 = sb[1] + in_used[b]
            placed.append({"from": a, "to": b, "value": v, "src": [sa[0] + NODE_W, s0, thick], "dst": [sb[0], t0, thick]})
        out_used[a] += thick
        in_used[b] += thick
    return {"boxes": boxes, "links": placed, "vertical": vertical, "scale": scale}


def frame(spec: Dict[str, Any], model: Dict[str, Any], box: Tuple[float, float]) -> Frame:
    base = FR.radial(box, (), "none")
    px, py, pw, ph = base.plot
    found = placement(spec, model, base.plot)
    thin = sum(1 for b in found["boxes"].values() if (b[2] if found["vertical"] else b[3]) < FR.LH * 0.5)
    notes = list(base.notes)
    if thin:
        notes.append("{} node{} thinner than half a line".format(thin, "" if thin == 1 else "s"))
    return Frame(box=base.box, plot=base.plot, axes={}, legend=None, font=base.font, notes=tuple(notes), legible=base.legible, extra={})


def _paint_of(spec: Mapping[str, Any], model: Mapping[str, Any]) -> Dict[str, str]:
    wanted = set(spec.get("highlight") or [])
    names = [n["name"] for n in model.get("nodes") or []]
    if wanted and wanted & set(names):
        return {n: ("chart.highlight" if n in wanted else "chart.dim") for n in names}
    return {n: S.cat(i) for i, n in enumerate(names)}


def draw(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, slot: Tuple[float, float, float, float]) -> List[Dict[str, Any]]:
    pen = DR.Pen(slot[0], slot[1])
    found = placement(spec, model, frame.plot)
    paints = _paint_of(spec, model)
    vertical = found["vertical"]
    for link in found["links"]:
        (sx, sy, st), (tx, ty, tt) = link["src"], link["dst"]
        if vertical:
            mid = (sy + ty) / 2.0
            d = "M {} C {} {} {} L {} C {} {} {} Z".format(pen.pt(sx, sy), pen.pt(sx, mid), pen.pt(tx, mid), pen.pt(tx, ty), pen.pt(tx + tt, ty),
                                                         pen.pt(tx + tt, mid), pen.pt(sx + st, mid), pen.pt(sx + st, sy))
        else:
            mid = (sx + tx) / 2.0
            d = "M {} C {} {} {} L {} C {} {} {} Z".format(pen.pt(sx, sy), pen.pt(mid, sy), pen.pt(mid, ty), pen.pt(tx, ty), pen.pt(tx, ty + tt),
                                                         pen.pt(mid, ty + tt), pen.pt(mid, sy + st), pen.pt(sx, sy + st))
        pen.path(d, paints.get(link["from"], "chart.cat.0"), None, None, 0.35)
    columns = max((int(n["depth"]) for n in model.get("nodes") or []), default=0)
    for node in model.get("nodes") or []:
        x, y, w, h = found["boxes"][node["name"]]
        pen.rect(x, y, w, h, paints.get(node["name"], "chart.cat.0"), r=1)
        text = "{} {}".format(node["name"], G.num(float(node["value"] or 0.0), dict(spec), "value"))
        if vertical:
            if w >= FR.text_w(text) + 4:
                pen.text(text, x + w / 2.0, y + h + FR.LH / 2.0 + 2, "middle", "chart.ink")
            else:
                pen.text(FR.fit_text(node["name"], max(24.0, w + NODE_GAP - 2)), x + w / 2.0, y + h + FR.LH / 2.0 + 2, "middle", "chart.ink")
        elif int(node["depth"]) == columns and columns > 0:
            pen.text(FR.fit_text(text, LABEL_ROOM - 6), x + w + 4, y + h / 2.0, "start", "chart.ink")
        else:
            pen.text(FR.fit_text(text, LABEL_ROOM - 6), x + w + 4, y + h / 2.0, "start", "chart.ink")
    return pen.items


def doc(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    return {"datasets": [], "refs": {"nodes": [{"name": n["name"], "depth": n["depth"]} for n in model.get("nodes") or []],
                                     "links": [{"source": a, "target": b, "value": v} for a, b, v in model.get("links") or []]}}


def option(spec: Dict[str, Any], model: Dict[str, Any], frame: Frame, gist: Sequence[str]) -> Dict[str, Any]:
    out = O.base(gist)
    px, py, pw, ph = frame.plot
    vertical = spec.get("orient") == "vertical"
    label_room = 0.0 if vertical else min(LABEL_ROOM, pw * 0.22)
    out.update(tooltip=O.tooltip("item"), legend={"show": False})
    out["series"] = [{"type": "sankey", "left": round(px, 2), "top": round(py, 2), "width": round(pw - label_room, 2), "height": round(ph, 2),
                      "orient": "vertical" if vertical else "horizontal", "nodeWidth": NODE_W, "nodeGap": NODE_GAP, "nodeAlign": "justify",
                      "layoutIterations": 0, "draggable": False, "links": {"$doc": "links"},
                      # The nodes (80 at most) travel in the option, each with its paint: a highlight changes them.
                      "data": [{"name": n["name"], "depth": n["depth"], "itemStyle": {"color": _paint_of(spec, model)[n["name"]]}}
                               for n in model.get("nodes") or []],
                      "lineStyle": {"color": "source", "opacity": 0.35, "curveness": 0.5},
                      "label": {"show": True, "position": "bottom" if vertical else "right", "color": "chart.ink", "fontFamily": "$sans",
                                "fontSize": FR.FONT}}]
    return out


def stats(spec: Dict[str, Any], table: _data.Table, model: Dict[str, Any]) -> Dict[str, Any]:
    links = model.get("links") or []
    out_total: Dict[str, float] = {}
    in_total: Dict[str, float] = {}
    for a, b, v in links:
        out_total[a] = out_total.get(a, 0.0) + float(v)
        in_total[b] = in_total.get(b, 0.0) + float(v)
    sources = sorted(((n, v) for n, v in out_total.items()), key=lambda kv: (-kv[1], kv[0]))
    sinks = sorted(((n, v) for n, v in in_total.items() if n not in out_total), key=lambda kv: (-kv[1], kv[0]))
    return {"flows": [list(l) for l in links[:3]], "sources": [list(s) for s in sources[:6]], "sinks": [list(s) for s in sinks[:6]],
            "nodes": len(model.get("nodes") or []), "links": len(links),
            "columns": max((int(n["depth"]) for n in model.get("nodes") or []), default=0) + 1}


def gist(spec: Dict[str, Any], model: Dict[str, Any], stats: Mapping[str, Any]) -> List[str]:
    def n(value: Any) -> str:
        return G.num(float(value), dict(spec), "value")

    lines = [G.join(["{} → {}".format(spec["source"], spec["target"]), "value={}".format(spec.get("value") or "count"),
                     "{} nodes in {} columns, {} flows".format(stats.get("nodes", 0), stats.get("columns", 0), stats.get("links", 0)),
                     str(spec.get("_source") or "")])]
    if stats.get("flows"):
        lines.append("largest: " + " · ".join("{} → {} {}".format(a, b, n(v)) for a, b, v in stats["flows"]))
    if stats.get("sources"):
        lines.append("out of: " + " · ".join("{} {}".format(k, n(v)) for k, v in stats["sources"]))
    if stats.get("sinks"):
        lines.append("ends: " + " · ".join("{} {}".format(k, n(v)) for k, v in stats["sinks"]))
    for note in model.get("capped") or []:
        lines.append("capped: " + note)
    return lines


def min_box(spec: Dict[str, Any], model: Dict[str, Any]) -> Tuple[float, float]:
    nodes = model.get("nodes") or []
    columns = max((int(n["depth"]) for n in nodes), default=0) + 1
    per: Dict[int, int] = {}
    for node in nodes:
        per[int(node["depth"])] = per.get(int(node["depth"]), 0) + 1
    busiest = max(per.values() or [1])
    along = busiest * (FR.LH + NODE_GAP) + 40
    across = columns * 90.0 + LABEL_ROOM
    return (max(320.0, along), max(200.0, across)) if spec.get("orient") == "vertical" else (max(320.0, across), max(200.0, along))


CHARTS = (
    ChartType(name="sankey",
              channels=(Channel("source", required=True, types=("nominal", "ordinal"), doc="where a flow starts"),
                        Channel("target", required=True, types=("nominal", "ordinal"), doc="where it goes"),
                        Channel("value", required=True, types=("quantitative",), aggregate=True, doc="how much flows (summed when rows repeat)")),
              options=("orient",), frame_kind="radial", model=model, stats=stats, doc=doc, frame=frame, option=option, draw=draw, gist=gist,
              vegalite=None, echarts=("SankeyChart", "TooltipComponent", "AriaComponent"), min_box=min_box, default_box=(600, 360),
              caps="150 flows, 80 nodes", doc_line="flows between stages, each band as thick as what it carries",
              example='{"op": "chart", "id": "flows", "intent": "where signups come from and go", "title": "Signup flows", "type": "sankey", '
                      '"rows": [{"from": "Search", "to": "Signup", "n": 5400}, {"from": "Ads", "to": "Signup", "n": 2100}, '
                      '{"from": "Signup", "to": "Active", "n": 4300}, {"from": "Signup", "to": "Churned", "n": 3200}], '
                      '"source": "from", "target": "to", "value": "n"}',
              order=110),
)
