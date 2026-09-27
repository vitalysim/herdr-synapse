"""The Mermaid flowchart subset the canvas turns into native, editable shapes (0.21).

Contract section 6, ``mermaid``. A ``flowchart``/``graph`` source this
module can parse is expanded by ``canvas`` like a ``graph`` operation, so
the team can move and restyle every node afterwards. Any other diagram kind
(sequence, state, class, ...) stays one ``mermaid`` element that the page
renders with Mermaid's own strict security level.

The subset, per statement (newline or ``;`` separated):

* nodes ``A``, ``A[t]``, ``A(t)``, ``A([t])``, ``A{t}``, ``A((t))``,
  ``A>t]``, ``A[[t]]``, ``A[(t)]``, ``A{{t}}``; text may be quoted;
* edges ``-->``, ``---``, ``-.->``, ``==>`` (and their ``-.-``/``===``/
  ``--x``/``--o`` cousins) with ``|label|`` after the arrow or
  ``-- label -->`` / ``-. label .->`` / ``== label ==>``;
* chains ``A --> B --> C`` and groups ``A & B --> C``;
* ``subgraph id [title]`` ... ``end`` (nested frames);
* ``%%`` comments, ``classDef``, ``class``, ``style``, ``linkStyle`` and
  ``direction`` are ignored; ``click`` is refused (it binds URLs and
  callbacks), which ``canvas`` reports as ``op_invalid``.

Anything else in a flowchart raises ``MermaidSyntax`` and the caller keeps
the source as a page-rendered ``mermaid`` element instead.
"""
from __future__ import annotations

import re
from typing import Any, Dict, List, Optional, Tuple

DIAGRAM_KINDS = ("sequence", "state", "class", "er", "gantt", "mindmap", "timeline", "pie", "quadrant", "other")
_HEADER_KINDS = (
    ("sequencediagram", "sequence"), ("statediagram-v2", "state"), ("statediagram", "state"),
    ("classdiagram", "class"), ("erdiagram", "er"), ("gantt", "gantt"), ("mindmap", "mindmap"),
    ("timeline", "timeline"), ("pie", "pie"), ("quadrantchart", "quadrant"),
)
_FLOW_HEADER = re.compile(r"^(?:flowchart|graph)(?:\s+(TD|TB|BT|LR|RL))?\s*;?\s*$", re.IGNORECASE)
_DIRECTIONS = {"TD": "down", "TB": "down", "BT": "up", "LR": "right", "RL": "left"}
_IGNORED = ("classdef", "class", "style", "linkstyle", "direction")
_ID_RE = re.compile(r"[A-Za-z0-9_]+")
#: (opening, closing, node kind), longest openings first.
_SHAPES = (
    ("((", "))", "ellipse"), ("([", "])", "ellipse"), ("[[", "]]", "box"), ("[(", ")]", "box"),
    ("{{", "}}", "diamond"), ("[", "]", "box"), ("(", ")", "box"), ("{", "}", "diamond"), (">", "]", "box"),
)
#: Plain arrows (no inline label), longest first: (token, dashed, head, thick).
_ARROWS = (
    ("-.->", True, "arrow", False), ("<-->", False, "arrow", False), ("-->", False, "arrow", False),
    ("==>", False, "arrow", True), ("--x", False, "arrow", False), ("--o", False, "dot", False),
    ("-.-", True, "none", False), ("===", False, "none", True), ("---", False, "none", False),
)
#: Arrows that carry the label inside: opener -> (closers, dashed, thick).
_LABEL_ARROWS = (
    ("--", (("-->", "arrow"), ("---", "none")), False, False),
    ("-.", ((".->", "arrow"), (".-", "none")), True, False),
    ("==", (("==>", "arrow"), ("===", "none")), False, True),
)
MAX_TEXT = 200


class MermaidSyntax(ValueError):
    """The flowchart uses something outside the subset; keep it as a page-rendered element."""


class MermaidRefused(ValueError):
    """The source asks for something the canvas never accepts (``click``)."""


def _lines(source: str) -> List[str]:
    return [line.strip() for line in source.replace("\r\n", "\n").replace("\r", "\n").split("\n")]


def diagram_kind(source: str) -> str:
    """``flowchart`` for a flowchart/graph header, else one of ``DIAGRAM_KINDS``."""
    for line in _lines(source):
        if not line or line.startswith("%%") or line == "---":
            continue
        if _FLOW_HEADER.match(line):
            return "flowchart"
        head = line.split()[0].lower().rstrip(":;")
        for prefix, kind in _HEADER_KINDS:
            if head == prefix:
                return kind
        return "other"
    return "other"


def _statements(lines: List[str]) -> List[str]:
    """Split lines on ``;`` outside brackets and quotes."""
    out: List[str] = []
    for line in lines:
        if not line or line.startswith("%%"):
            continue
        depth = 0
        quoted = False
        current = []
        for ch in line:
            if ch == '"':
                quoted = not quoted
            elif not quoted and ch in "[({":
                depth += 1
            elif not quoted and ch in "])}":
                depth = max(0, depth - 1)
            if ch == ";" and depth == 0 and not quoted:
                piece = "".join(current).strip()
                if piece:
                    out.append(piece)
                current = []
                continue
            current.append(ch)
        piece = "".join(current).strip()
        if piece:
            out.append(piece)
    return out


def _clean_text(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == '"' and text[-1] == '"':
        text = text[1:-1]
    text = text.replace("<br>", " ").replace("<br/>", " ").replace("<br />", " ")
    return " ".join(text.split())[:MAX_TEXT]


class _Parser:
    def __init__(self) -> None:
        self.nodes: Dict[str, Dict[str, Any]] = {}
        self.edges: List[Dict[str, Any]] = []
        self.subgraphs: List[Dict[str, Any]] = []
        self.stack: List[str] = []

    # nodes ---------------------------------------------------------------

    def _node(self, text: str, pos: int) -> Tuple[str, int]:
        match = _ID_RE.match(text, pos)
        if match is None:
            raise MermaidSyntax("expected a node id at {!r}".format(text[pos:pos + 20]))
        node_id = match.group(0)
        pos = match.end()
        label: Optional[str] = None
        kind: Optional[str] = None
        for opening, closing, shape in _SHAPES:
            if text.startswith(opening, pos):
                end = self._closing(text, pos + len(opening), closing)
                label = _clean_text(text[pos + len(opening):end])
                kind = shape
                pos = end + len(closing)
                break
        existing = self.nodes.get(node_id)
        if existing is None:
            self.nodes[node_id] = {"id": node_id, "text": label if label is not None else node_id, "kind": kind or "box",
                                   "subgraph": self.stack[-1] if self.stack else None}
        elif label is not None:
            existing["text"] = label
            existing["kind"] = kind or existing["kind"]
        return node_id, pos

    @staticmethod
    def _closing(text: str, start: int, closing: str) -> int:
        index = start
        quoted = False
        while index < len(text):
            if text[index] == '"':
                quoted = not quoted
            elif not quoted and text.startswith(closing, index):
                return index
            index += 1
        raise MermaidSyntax("unclosed node text")

    def _group(self, text: str, pos: int) -> Tuple[List[str], int]:
        ids: List[str] = []
        while True:
            pos = _skip(text, pos)
            node_id, pos = self._node(text, pos)
            ids.append(node_id)
            after = _skip(text, pos)
            if after < len(text) and text[after] == "&":
                pos = after + 1
                continue
            return ids, pos

    # edges ---------------------------------------------------------------

    def _edge(self, text: str, pos: int) -> Tuple[Optional[Dict[str, Any]], int]:
        pos = _skip(text, pos)
        if pos >= len(text):
            return None, pos
        for token, dashed, head, thick in _ARROWS:
            if text.startswith(token, pos):
                pos = _skip(text, pos + len(token))
                label = None
                if pos < len(text) and text[pos] == "|":
                    end = text.find("|", pos + 1)
                    if end < 0:
                        raise MermaidSyntax("unclosed |label|")
                    label = _clean_text(text[pos + 1:end])
                    pos = end + 1
                return {"dash": dashed, "head": head, "thick": thick, "label": label or None}, pos
        for opener, closers, dashed, thick in _LABEL_ARROWS:
            if text.startswith(opener, pos):
                start = pos + len(opener)
                best: Optional[Tuple[int, str, str]] = None
                for closer, head in closers:
                    found = text.find(closer, start)
                    if found >= 0 and (best is None or found < best[0]):
                        best = (found, closer, head)
                if best is None:
                    raise MermaidSyntax("an edge label needs a closing arrow")
                found, closer, head = best
                label = _clean_text(text[start:found])
                return {"dash": dashed, "head": head, "thick": thick, "label": label or None}, found + len(closer)
        raise MermaidSyntax("expected an arrow at {!r}".format(text[pos:pos + 20]))

    # statements ----------------------------------------------------------

    def statement(self, text: str) -> None:
        words = text.split()
        head = words[0].lower() if words else ""
        if head == "click":
            raise MermaidRefused("click binds a URL or a callback; the canvas never runs it")
        if head in _IGNORED:
            return
        if head == "subgraph":
            self._subgraph(text[len(words[0]):].strip())
            return
        if head == "end" and len(words) == 1:
            if not self.stack:
                raise MermaidSyntax("end without subgraph")
            self.stack.pop()
            return
        groups: List[List[str]] = []
        group, pos = self._group(text, 0)
        groups.append(group)
        while True:
            edge, pos = self._edge(text, pos)
            if edge is None:
                break
            target, pos = self._group(text, pos)
            for a in groups[-1]:
                for b in target:
                    self.edges.append({"from": a, "to": b, "label": edge["label"], "dash": edge["dash"],
                                       "head": edge["head"], "thick": edge["thick"]})
            groups.append(target)

    def _subgraph(self, rest: str) -> None:
        if not rest:
            raise MermaidSyntax("subgraph needs a name")
        title: Optional[str] = None
        match = _ID_RE.match(rest)
        if rest.startswith('"'):
            sub_id = "sg{}".format(len(self.subgraphs) + 1)
            title = _clean_text(rest)
        elif match is not None and match.end() == len(rest):
            sub_id = match.group(0)
        elif match is not None and rest[match.end():].lstrip().startswith("["):
            sub_id = match.group(0)
            tail = rest[match.end():].strip()
            if not tail.endswith("]"):
                raise MermaidSyntax("subgraph title needs a closing ]")
            title = _clean_text(tail[1:-1])
        else:
            sub_id = "sg{}".format(len(self.subgraphs) + 1)
            title = _clean_text(rest)
        self.subgraphs.append({"id": sub_id, "title": title or sub_id, "parent": self.stack[-1] if self.stack else None})
        self.stack.append(sub_id)


def _skip(text: str, pos: int) -> int:
    while pos < len(text) and text[pos] in " \t":
        pos += 1
    return pos


def parse_flowchart(source: str) -> Dict[str, Any]:
    """``{"direction", "nodes", "edges", "subgraphs"}``; ``MermaidSyntax`` or ``MermaidRefused`` otherwise.

    ``nodes``: ``{"id", "text", "kind", "subgraph"}`` in first-mention order;
    ``edges``: ``{"from", "to", "label", "dash", "head", "thick"}``;
    ``subgraphs``: ``{"id", "title", "parent"}`` in opening order.
    """
    lines = _lines(source)
    header_index = None
    for index, line in enumerate(lines):
        if line and not line.startswith("%%"):
            header_index = index
            break
    if header_index is None:
        raise MermaidSyntax("empty source")
    header = _FLOW_HEADER.match(lines[header_index])
    if header is None:
        raise MermaidSyntax("not a flowchart")
    direction = _DIRECTIONS.get((header.group(1) or "TD").upper(), "down")
    parser = _Parser()
    for statement in _statements(lines[header_index + 1:]):
        parser.statement(statement)
    if parser.stack:
        raise MermaidSyntax("subgraph without end")
    if not parser.nodes:
        raise MermaidSyntax("no nodes")
    return {"direction": direction, "nodes": list(parser.nodes.values()), "edges": parser.edges, "subgraphs": parser.subgraphs}
