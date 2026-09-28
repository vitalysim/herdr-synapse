"""Diagrams (canvas v2): the ``mermaid`` op, and the ``mermaid`` kind the page renders.

A Mermaid flowchart becomes a ``graph`` block (``canvas_kinds.graph``): its
subgraphs are the graph's groups and its direction maps, so every node is a
native shape the team can move, restyle and patch. Any other Mermaid diagram
(a sequence diagram included) is a ``mermaid`` element the page renders.

The graph limits live here, shared by the ``graph`` op and Mermaid flowcharts.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from herdr_team import canvas_mermaid
from herdr_team.canvas_kinds import Kind, OpSpec
from herdr_team.canvas_kinds._common import Element, bounds_text, quote
from herdr_team.canvas_kinds._slot import slot_emit

#: Its place in the registration order (``canvas_kinds.DEFAULT_ORDER``).
ORDER = 80

MAX_GRAPH_NODES = 200
MAX_GRAPH_EDGES = 400
MAX_MERMAID_BYTES = 20 * 1024
_NODE_ID = __import__("re").compile(r"^[A-Za-z0-9_-]{1,32}\Z")


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

            graph: Dict[str, Any] = {key: op[key] for key in op if key not in ("op", "source", "w", "h", "title", "client_id")}
            graph.update(op="graph", title=title or "flowchart", direction=parsed["direction"])
            graph["groups"] = [dict({"id": sg["id"], "title": label(sg.get("title")) or sg["id"]},
                                    **({"parent": sg["parent"]} if sg.get("parent") else {})) for sg in parsed["subgraphs"]]
            graph["nodes"] = [dict({"id": n["id"], "text": label(n["text"]) or n["id"]}, **({"kind": n["kind"]} if n["kind"] != "box" else {}),
                                   **({"in": n["subgraph"]} if n.get("subgraph") else {})) for n in parsed["nodes"]]
            edges = []
            for e in parsed["edges"]:
                edge: Dict[str, Any] = {"from": e["from"], "to": e["to"]}
                text = label(e.get("label"))
                if text:
                    edge["label"] = text
                if e["dash"]:
                    edge["style"] = "dashed"
                if e["head"] != "arrow":
                    edge["head"] = e["head"]
                if e["thick"]:
                    edge["thick"] = True
                edges.append(edge)
            graph["edges"] = edges
            if not graph["groups"]:
                graph.pop("groups")
            ctx.block("graph", graph)
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
    OpSpec(name="mermaid", fields=("source", "w", "h", "title", "id", "client_id"), create=create_mermaid, style=True, place=True, order=80,
           doc="a Mermaid diagram: a flowchart becomes a graph block of native shapes, anything else the page renders", mcp="mermaid {source}"),
)

KINDS = (
    Kind(name="mermaid", role="leaf", ops=("mermaid",), page=True, noun=("mermaid diagram", "mermaid diagrams"), solid=True, cell=True,
         slot="mermaid", connectable=True, text_edit=lambda el: None, readback=readback,
         emit=slot_emit(lambda el: "mermaid {} · rendered on the page".format(el.get("diagram") or "diagram")),
         doc="a Mermaid diagram the page renders (flowcharts become native shapes)"),
)
