"""Diagrams (canvas v2): ``graph`` and ``mermaid`` ops, and the ``mermaid`` kind the page renders.

A ``graph`` (nodes and edges) and a Mermaid flowchart become native shapes and
bound arrows in a frame, laid out in Python (``OpContext.expand_graph``); any
other Mermaid diagram is a ``mermaid`` element the page renders.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from herdr_team import canvas_layout, canvas_mermaid
from herdr_team.canvas_kinds import Kind, OpSpec
from herdr_team.canvas_kinds._common import DASHES, Element, bounds_text, quote
from herdr_team.canvas_kinds._slot import slot_emit

MAX_GRAPH_NODES = 200
MAX_GRAPH_EDGES = 400
MAX_MERMAID_BYTES = 20 * 1024
_NODE_ID = __import__("re").compile(r"^[A-Za-z0-9_-]{1,32}\Z")


def create_graph(ctx: Any, op: Dict[str, Any]) -> None:
    """The ``graph`` op: nodes and edges laid out in Python as native shapes and bound arrows in a frame."""
    nodes_raw, edges_raw = op.get("nodes"), op.get("edges") if op.get("edges") is not None else []
    if not isinstance(nodes_raw, list) or not nodes_raw:
        raise ctx.invalid("nodes", "graph needs nodes: [{\"id\": \"a\", \"text\": \"...\"}, ...]")
    if len(nodes_raw) > MAX_GRAPH_NODES:
        raise ctx.too_big("nodes", "MAX_GRAPH_NODES", MAX_GRAPH_NODES, "{} nodes; the limit is {}".format(len(nodes_raw), MAX_GRAPH_NODES))
    if not isinstance(edges_raw, list):
        raise ctx.invalid("edges", "edges must be a list of {\"from\", \"to\"}")
    if len(edges_raw) > MAX_GRAPH_EDGES:
        raise ctx.too_big("edges", "MAX_GRAPH_EDGES", MAX_GRAPH_EDGES, "{} edges; the limit is {}".format(len(edges_raw), MAX_GRAPH_EDGES))
    nodes = ctx.graph_nodes(nodes_raw)
    seen = {node["id"] for node in nodes}
    edges: List[Dict[str, Any]] = []
    for index, edge in enumerate(edges_raw):
        field = "edges[{}]".format(index)
        if isinstance(edge, (list, tuple)) and len(edge) in (2, 3):
            edge = {"from": edge[0], "to": edge[1], "label": edge[2] if len(edge) == 3 else None}
        if not isinstance(edge, dict):
            raise ctx.invalid(field, "{} must be {{\"from\", \"to\", \"label\"}}".format(field))
        for key in edge:
            if key not in ("from", "to", "label", "dash"):
                raise ctx.invalid("{}.{}".format(field, key), "an edge takes from, to, label, dash")
        for end in ("from", "to"):
            value = edge.get(end)
            if not isinstance(value, str) or value not in seen:
                raise ctx.invalid("{}.{}".format(field, end), "{} is not one of the graph's node ids".format(
                    value if isinstance(value, str) else "{}.{}".format(field, end)))
        dash = edge.get("dash")
        dash_style = ("dashed" if dash else "solid") if isinstance(dash, bool) or dash is None else \
            ctx.choice(edge, "dash", DASHES, "solid", label=field + ".dash")
        edges.append({"from": edge["from"], "to": edge["to"], "dash": dash_style, "head": "arrow", "thick": False,
                      "label": ctx.text(edge, "label", limit="label", one_line=True, label=field + ".label")})
    algorithm = ctx.choice(op, "layout", canvas_layout.LAYOUTS, "layered")
    direction = ctx.choice(op, "direction", ("down", "right"), "down")
    title = ctx.text(op, "title", limit="label", one_line=True)
    ctx.expand_graph(op, title or (op.get("id") if isinstance(op.get("id"), str) else "") or "graph", nodes, edges, algorithm, direction)


def create_mermaid(ctx: Any, op: Dict[str, Any]) -> None:
    """The ``mermaid`` op: a flowchart becomes native shapes (a frame of them); any other diagram the page renders."""
    source = op.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ctx.invalid("source", "mermaid needs source")
    if len(source.encode("utf-8")) > MAX_MERMAID_BYTES:
        raise ctx.too_big("source", "MAX_MERMAID_BYTES", MAX_MERMAID_BYTES, "the Mermaid source is over {} KB".format(MAX_MERMAID_BYTES // 1024))
    ctx.guard_code(source, "source")
    title = ctx.text(op, "title", limit="label", one_line=True)
    kind = canvas_mermaid.diagram_kind(source)
    diagram = kind
    if kind == "flowchart":
        try:
            parsed: Optional[Dict[str, Any]] = canvas_mermaid.parse_flowchart(source)
        except canvas_mermaid.MermaidRefused as err:
            raise ctx.invalid("source", str(err))
        except canvas_mermaid.MermaidSyntax:
            parsed = None
        if parsed is not None:
            if len(parsed["nodes"]) > MAX_GRAPH_NODES:
                raise ctx.too_big("source", "MAX_GRAPH_NODES", MAX_GRAPH_NODES, "{} nodes; the limit is {}".format(len(parsed["nodes"]), MAX_GRAPH_NODES))
            if len(parsed["edges"]) > MAX_GRAPH_EDGES:
                raise ctx.too_big("source", "MAX_GRAPH_EDGES", MAX_GRAPH_EDGES, "{} edges; the limit is {}".format(len(parsed["edges"]), MAX_GRAPH_EDGES))
            for node in parsed["nodes"]:
                if not _NODE_ID.match(node["id"]):
                    raise ctx.invalid("source", "node id {} is longer than 32 characters".format(node["id"][:40]))

            def label(value: Any) -> str:
                return ctx.text({}, "source", limit="label", one_line=True, value=value)

            nodes = [{"id": n["id"], "kind": n["kind"], "subgraph": n.get("subgraph"), "color": None, "fill": None, "fill_set": False,
                      "text": label(n["text"]) or n["id"]} for n in parsed["nodes"]]
            edges = [{"from": e["from"], "to": e["to"], "dash": "dashed" if e["dash"] else "solid", "head": e["head"], "thick": e["thick"],
                      "label": label(e.get("label"))} for e in parsed["edges"]]
            subgraphs = [{"id": sg["id"], "parent": sg.get("parent"), "title": label(sg.get("title"))} for sg in parsed["subgraphs"]]
            ctx.expand_graph(op, title or "flowchart", nodes, edges, "layered", parsed["direction"], subgraphs)
            return
        diagram = "other"
    w, h = ctx.size(op, 480, 320)
    x, y, frame = ctx.place(op, w, h)
    ctx.create("mermaid", x, y, w, h, op=op, text=title, style=ctx.style(op, "mermaid"), frame=frame, source=source, diagram=diagram, still=None)


def readback(el: Element, full: bool) -> str:
    text = str(el.get("text") or "")
    lines = len(str(el.get("source") or "").strip().splitlines())
    return "{} mermaid {}{} ({} lines) {}".format(el.get("id"), el.get("diagram") or "other", " " + quote(text, 0 if full else 80) if text else "",
                                                  lines, bounds_text(el))


OPS = (
    OpSpec(name="graph", fields=("nodes", "edges", "layout", "direction", "title", "id"), create=create_graph, style=True, place=True, order=70,
           doc="nodes and edges laid out in Python as native shapes and arrows in a frame",
           mcp="graph {nodes [{id,text,kind,tone}], edges [{from,to,label}], layout layered|radial|force|grid}"),
    OpSpec(name="mermaid", fields=("source", "w", "h", "title", "id", "client_id"), create=create_mermaid, style=True, place=True, order=80,
           doc="a Mermaid diagram: a flowchart becomes native shapes, anything else the page renders", mcp="mermaid {source}"),
)

KINDS = (
    Kind(name="mermaid", role="leaf", ops=("mermaid",), page=True, noun=("mermaid diagram", "mermaid diagrams"), solid=True, cell=True,
         slot="mermaid", connectable=True, text_edit=lambda el: None, readback=readback,
         emit=slot_emit(lambda el: "mermaid {} · rendered on the page".format(el.get("diagram") or "diagram")),
         doc="a Mermaid diagram the page renders (flowcharts become native shapes)"),
)
