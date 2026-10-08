"""What a canvas says, as searchable rows: the text extraction behind ``recall``'s ``canvas`` kind (0.22.1).

A diagram used to be the one thing a team could not find again. The board, the
facts, the work items and the artifact files were all in the recall index; the
canvas was not, so the frame that holds the whole login flow was unreachable by
searching for "login flow".

This module is the extraction half of that fix, kept apart from ``recall`` and
from ``canvas`` on purpose:

* it is **pure** — one folded scene in, rows out. No team, no paths, no lock,
  no reader, no cursor and no presence, so indexing never counts as looking at
  the board (``canvas.look`` writes presence and a cursor; see the spec's S1);
* it imports ``canvas_kinds`` and nothing else of the canvas, so the index
  cannot make the canvas do work, and ``recall`` never has to know an element's
  storage shape;
* it reads the **kind registry** rather than a list of kinds, so a kind added
  later is searchable without touching this file or ``recall``.

How the text is found. A kind keeps its words in its own fields, and new kinds
keep inventing fields, so a fixed per-kind list would go stale on the next
kind. Instead each element is walked twice:

1. the fields every kind uses for words a person would search for (``text``,
   ``title``, ``label``, ``body``, ``caption``, ``detail`` ... - `TEXT_KEYS`),
   at any depth. These always reach the row;
2. then everything else that is still a string (a table's cells, a sequence's
   messages, a chart's axis field names), until the row's budget runs out.

Keys that never hold words - geometry, authorship, ids, style, derived caches
and content-addressed asset names - are skipped in both passes (`SKIP_KEYS`),
and a container whose shape says "machine data" rather than "what someone
wrote" is skipped whole (`SKIP_CONTAINERS`). The budgets keep one element's row
bounded whatever a kind stores, so a chart with ten thousand inline data points
cannot crowd the board's words out of the index.
"""

from __future__ import annotations

import re
from typing import Any, Dict, Iterable, List, Mapping, Optional

#: Fields that hold words a person would search for, whatever kind they belong to. Pass 1
#: collects these first so a budget spent on data values can never drop a caption or a title.
TEXT_KEYS = frozenset({
    "text", "title", "label", "body", "caption", "detail", "meaning", "symbol", "statement",
    "summary", "note", "notes", "message", "messages", "heading", "topic", "mentions", "source",
})

#: Fields that never hold words: layout, style, identity, authorship, bookkeeping, derived
#: caches and content-addressed asset names. Skipped in both passes. Numeric fields need no
#: entry - the walk only collects strings - which is why ``x`` and ``y`` are absent: a chart
#: keeps the *name* of its x field there, and that is a series name worth finding.
SKIP_KEYS = frozenset({
    # identity and structure (the row carries these itself, or they name another element)
    "id", "alias", "intent", "frame", "group", "part", "role", "batch", "via", "client_id",
    "author", "author_kind", "author_gen", "owner", "owner_known", "resolved_by", "reply_to",
    "from", "to", "on", "under", "imported", "item", "parent", "ids", "keep",
    # layout
    "points", "point", "nudged", "bounds", "axis", "ticks", "fixed_w", "clamp", "order", "pin",
    "lod", "fit", "wrap", "label_at", "scale", "size", "gap", "padding", "align", "cols", "grid",
    "layout", "direction", "side", "band", "level", "closed", "smooth", "curve", "head", "tail",
    "route", "d", "depth", "inner", "orient", "camera", "lights", "ground", "units", "limit",
    "count", "seq", "max_lines", "header", "zebra", "milestone", "labels", "legend",
    # style
    "style", "color", "fill", "tone", "variant", "opacity", "dash", "width", "font", "rough",
    "palette", "shading", "wireframe", "highlight", "icon", "tones", "branch_tones",
    # derived, timestamps and assets
    "created_seq", "updated_seq", "created_at", "updated_at", "at", "asset", "html_asset",
    "spec_asset", "echarts_asset", "data_key", "sha256", "still", "stills", "engine", "solved",
    "block", "resolved", "status", "diagram",
})

#: Containers that carry machine data rather than what someone wrote: skipped whole when the
#: value is a dict or a list. A string under the same key is authored text and is kept, which is
#: how a chart's derived ``source`` record is dropped while a mermaid diagram's ``source`` is not.
#: A table's ``rows`` are deliberately *not* here: they are the cells someone typed, and what
#: keeps a chart's inline data from crowding them out is the budget, not a list of kinds.
#:
#: The second group is the view models the page draws from (a chart's whole ECharts ``option``,
#: its solved ``model`` and ``stats``, a Vega-Lite spec): derived from the fields above them, so
#: indexing them would file the same words twice and spend the row's budget on ``$sans`` and
#: ``circle``. The kind's own one-line ``gist`` is kept, which is where its series and axis
#: names read back from.
SKIP_CONTAINERS = frozenset({
    "source", "data", "spec", "echarts",
    "option", "model", "stats", "chart_frame", "vl", "stripped", "resolved", "chart", "libs",
})

#: One element's row holds at most this many characters of harvested text.
MAX_ROW_CHARS = 1200
#: One harvested string is clipped here, so a 2000-character free text cannot fill a row alone.
MAX_VALUE_CHARS = 400
#: How deep the walk goes into an element's own structure.
MAX_DEPTH = 6
#: How many items of one list the walk looks at.
MAX_ITEMS = 60
#: How many values one element's walk looks at before it stops. The row budget would discard most
#: of them anyway; this is what keeps the *walk* bounded, because nesting multiplies: a list of
#: lists of lists is no shape a kind stores today, and this file should not depend on that staying
#: true. Counted in values visited rather than strings collected, so a deep structure holding no
#: words at all cannot cost anything either.
MAX_VISITS = 20000

#: Weights, in the same 0.5-2.0 band the other recall kinds use. Registry-driven: a container's
#: title is the heading of a region, a kind the browser draws live and an element someone bothered
#: to name are worth more than a loose mark, and a comment somebody closed is worth less.
WEIGHT_CONTAINER = 1.5
WEIGHT_NAMED = 1.2
WEIGHT_PLAIN = 1.0
WEIGHT_RESOLVED = 0.5

_NOISE_RE = re.compile(r"^(?:[0-9a-f]{32}(?:\.[a-z0-9]{1,6})?|[A-Z]-[0-9]+|#[0-9a-fA-F]{3,8}|[-+0-9.,:eE]+)\Z")


def _kinds() -> Any:
    """The registry, imported on use: ``rows`` is often called once a day, and loading every kind
    module to answer a ``--kind fact`` search would be work nobody asked for."""
    from herdr_team import canvas_kinds

    return canvas_kinds


def _words(value: Any) -> str:
    """One harvested string, flattened and clipped; "" for anything that is not words."""
    text = " ".join(str(value or "").split())
    if not text or _NOISE_RE.match(text):
        return ""
    return text if len(text) <= MAX_VALUE_CHARS else text[:MAX_VALUE_CHARS].rstrip()


def _harvest(value: Any, named: List[str], other: List[str], depth: int = 0, in_text_key: bool = False,
             visits: Optional[List[int]] = None) -> None:
    """Collect strings reachable from ``value`` into ``named`` (pass 1) and ``other`` (pass 2).

    ``visits`` is the walk's remaining budget, a one-element list so every branch shares it.
    """
    if visits is None:
        visits = [MAX_VISITS]
    visits[0] -= 1
    if depth > MAX_DEPTH or visits[0] <= 0:
        return
    if isinstance(value, str):
        (named if in_text_key else other).append(value)
        return
    if isinstance(value, Mapping):
        for key in value:
            if not isinstance(key, str) or key in SKIP_KEYS:
                continue
            child = value[key]
            if key in SKIP_CONTAINERS and not isinstance(child, str):
                continue
            _harvest(child, named, other, depth + 1, in_text_key or key in TEXT_KEYS, visits)
        return
    if isinstance(value, (list, tuple)):
        for item in list(value)[:MAX_ITEMS]:
            _harvest(item, named, other, depth + 1, in_text_key, visits)


def _body(parts: Iterable[str]) -> str:
    """The row's text: the parts in order, each once, up to the row budget."""
    out: List[str] = []
    seen = set()
    size = 0
    for part in parts:
        text = _words(part)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        if size + len(text) + 1 > MAX_ROW_CHARS:
            continue
        seen.add(key)
        out.append(text)
        size += len(text) + 1
    return " ".join(out)


def element_text(el: Mapping[str, Any]) -> str:
    """Everything searchable about one element: its kind word, its alias, its intent, its own
    text, then the words its kind keeps in its own fields.

    The kind word is included so "find the frame" works; ``intent`` is included because it is the
    one field every op carries and is often the richest sentence on the board ("show the link
    shortener flow while leaving room for teammates to extend it").
    """
    named: List[str] = []
    other: List[str] = []
    _harvest(el, named, other)
    always = [str(el.get("type") or ""), str(el.get("alias") or ""), str(el.get("text") or ""), str(el.get("intent") or "")]
    return _body(always + named + other)


def weight_of(el: Mapping[str, Any]) -> float:
    """How much this element's row is worth, from the kind registry rather than a list of kinds."""
    kind = _kinds().kind_of(el)
    if kind is not None and kind.name == "comment":
        return WEIGHT_RESOLVED if el.get("resolved") else WEIGHT_PLAIN
    if kind is not None and kind.role == "container":
        return WEIGHT_CONTAINER
    if (kind is not None and kind.slot) or el.get("alias"):
        return WEIGHT_NAMED
    return WEIGHT_PLAIN


def _about(el: Mapping[str, Any], frames: Mapping[str, str]) -> str:
    """What the element is about: the title of the frame it sits in, else its own name.

    Mirrors how ``about`` behaves for a fact (its subject) and a file (its path), so
    ``recall --about "login flow"`` finds the marks inside that frame.
    """
    parent = el.get("frame")
    if isinstance(parent, str) and frames.get(parent):
        return frames[parent]
    return str(el.get("alias") or "")


def rows(scene: Optional[Mapping[str, Any]]) -> List[Dict[str, Any]]:
    """One searchable row per element, comments and legend entries included.

    Each row carries ``id`` (the element id, which is also recall's ``ref``), ``type``, ``body``
    (everything searchable about it), ``about`` (its frame's title or its own name), and the
    ``author``, ``ts``, ``weight`` and ``info`` recall stores beside them. Pure: no clock, no
    I/O, and the same scene always gives the same rows, so the index's signature decides when
    to rebuild and nothing else does.
    """
    if not isinstance(scene, Mapping):
        return []
    elements = scene.get("elements")
    elements = list(elements) if isinstance(elements, (list, tuple)) else []
    frames = {}
    for el in elements:
        if isinstance(el, Mapping) and isinstance(el.get("id"), str) and el.get("text"):
            frames[el["id"]] = " ".join(str(el["text"]).split())
    out: List[Dict[str, Any]] = []
    for el in elements:
        if not isinstance(el, Mapping) or not isinstance(el.get("id"), str):
            continue
        body = element_text(el)
        if not body:
            continue
        imported = el.get("imported") if isinstance(el.get("imported"), Mapping) else None
        out.append({
            "id": el["id"], "type": str(el.get("type") or ""), "body": body, "about": _about(el, frames),
            "author": str(el.get("author") or ""), "ts": str(el.get("updated_at") or el.get("created_at") or ""),
            "weight": weight_of(el),
            "info": {"type": str(el.get("type") or ""), "alias": el.get("alias"), "frame": el.get("frame"),
                     "imported": dict(imported) if imported else None},
        })
    out.extend(_legend_rows(scene.get("legend")))
    return out



def _legend_rows(legend: Any) -> List[Dict[str, Any]]:
    """The team's own vocabulary for its marks (``G-n``): a symbol and what it means. Searchable
    because "what does the dashed border mean" is a question about the board, asked in words."""
    out: List[Dict[str, Any]] = []
    if not isinstance(legend, (list, tuple)):
        return out
    for entry in legend:
        if not isinstance(entry, Mapping) or not isinstance(entry.get("id"), str):
            continue
        body = _body(["legend", str(entry.get("symbol") or ""), str(entry.get("meaning") or "")])
        if not body:
            continue
        out.append({
            "id": entry["id"], "type": "legend", "body": body, "about": "",
            "author": str(entry.get("author") or ""), "ts": str(entry.get("at") or ""),
            "weight": WEIGHT_PLAIN,
            "info": {"type": "legend", "alias": None, "frame": None, "imported": None},
        })
    return out
