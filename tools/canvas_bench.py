#!/usr/bin/env python3
"""Canvas benchmark: 30 plain-language drawing requests, a scorer, and an offline and a live mode (canvas v2 phase 6, 6).

For a request an operator would give, does a drawing (a) apply on the first try, (b) come out with no overflow, overlap
or arrow through a mark, (c) avoid needless crossings, (d) contain what was asked (the labels and the relations), and
(e) read back to another member through ``look`` as it was drawn? And for edits: did what was not asked for stay put,
and did an edit of the operator's mark become a proposal?

Fixtures live in ``tests/fixtures/canvas_bench/`` (``README.md`` there has the format): ``prompts/NN-slug.json`` (the
request, what it must contain, a reference batch in the v2 language and for some a ``reference_v1`` in the 0.21
language), ``data/`` (the files a request names, copied into the team's ``artifacts/``) and ``controls/`` (drawings that
are wrong on purpose, one per scorer path: each must be flagged).

    python3 tools/canvas_bench.py --check                      # fixture schema, the 30 prompts, the counts, data files
    python3 tools/canvas_bench.py offline                      # every v2 reference and every control (the gate)
    python3 tools/canvas_bench.py offline --language both      # the v2 and the 0.21 language side by side
    python3 tools/canvas_bench.py offline --prompts 01,04 --no-controls --json
    python3 tools/canvas_bench.py offline --controls           # only the controls
    python3 tools/canvas_bench.py offline --pictures --out DIR # also look --image PNGs (resvg), for the rating form
    ./e.sh python3 tools/canvas_bench.py live --team l6 --member l6-drawer --member l6-coder --prompts live --out DIR
    python3 tools/canvas_bench.py live ... --dry-run           # the plan and the turn count, nothing sent
    python3 tools/canvas_bench.py report DIR [DIR ...] --out DIR   # merge runs: results.json, summary.md, rating.md

Offline mode never touches the real state: every prompt gets its own ``canvas_qa.QaTeam`` (a temporary state root). The
seed is applied (as the drawing member, or as the operator), then the reference as the member ``qa-drawer``, and the
board is scored with ``qa-peer`` as the second reader. It exits 0 only when every v2 reference passes with alignment 1.0
drawn and read back, no hard problem, and every control is flagged as its file says; ``reference_v1`` rows are
comparison data and never fail it.

Live mode drives real agents one at a time (``herdr agent prompt``, or an operator ``post`` typed into a shell pane)
and refuses to run outside the throwaway rig (``HERDR_SESSION`` set and ``XDG_STATE_HOME`` under ``/var/tmp/syn-e2e/``).
It reads first-try validity from the attempt trace (``bench.on`` / ``attempts.jsonl``, ``canvas.read_attempts``) and
tokens from the member's own harness files, read-only. ``--fake`` answers each prompt with its reference, through the
library, so the whole loop is testable without Herdr.

Timings are informational: nothing here fails because a machine is slow. Stdlib only; it imports ``herdr_team`` and
``tools/canvas_qa.py`` from this checkout, read-only.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import re
import shlex
import shutil
import sqlite3
import statistics
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence, Set, Tuple

REPO = Path(__file__).resolve().parent.parent
TOOLS = Path(__file__).resolve().parent
for _path in (REPO, TOOLS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import canvas_qa as Q  # noqa: E402
from herdr_team import canvas as C  # noqa: E402
from herdr_team import canvas_display as D  # noqa: E402
from herdr_team import features, paths, store  # noqa: E402
from herdr_team.errors import HerdrTeamError  # noqa: E402

BENCH_DIR = REPO / "tests" / "fixtures" / "canvas_bench"
PROMPTS_DIR = BENCH_DIR / "prompts"
DATA_DIR = BENCH_DIR / "data"
CONTROLS_DIR = BENCH_DIR / "controls"
OUT_DIR = REPO / ".local" / "qa" / "bench"

#: The categories of 6.3 and how many prompts each has (30 in all).
CATEGORIES = {"flowchart": 3, "architecture": 3, "mindmap": 2, "kanban": 2, "table": 2, "timeline": 2, "sequence": 2,
              "chart": 5, "3d": 2, "sticky": 2, "edit": 5}
AUDIENCES = ("dev", "nondev")
PROMPT_COUNT = 30
LIVE_COUNT = 10
NONDEV_MIN = 8
CONTROLS_MIN = 8
V1_MIN = 10
#: Chart prompts that read a data file (CSV, JSON and TSV must all be there).
DATA_PROMPTS_MIN = 3
DATA_MAX_BYTES = 50 * 1024

PROMPT_KEYS = ("id", "category", "audience", "prompt", "live", "artifacts", "seed", "expect", "reference", "reference_v1", "notes")
EXPECT_KEYS = ("kinds_any", "labels", "relations", "gone_relations", "tones", "status", "stable", "changes", "proposal", "chart",
               "scene3d", "absent_labels")
CONTROL_KEYS = ("id", "about", "prompt", "expect", "seed", "artifacts", "reference", "expect_fail", "expect_crossings_min",
                "events")
RELATION_KINDS = ("edge", "contains", "any")
SEED_AS = ("member", "operator")

#: The three problems that must be 0 on what a member drew.
HARD = ("label_overflow", "overlap", "arrow_through")
#: A seed element "stays put" when it moved and resized by at most this many units.
STABLE_UNITS = 20.0
#: Relations read back must reach this recall for a pass (labels must all be there).
RELATIONS_PASS = 0.8
#: Types whose size follows their content (a table grows by a row, a column by a card): only their position is kept.
GROWING_TYPES = ("frame", "table", "sequence")
#: A crossing this close to an end the two routes share is where they meet, not a crossing.
SHARED_END_SLACK = 8.0

#: The line every live request ends with.
DELIVERY_TAIL = "Use the team canvas. When you are done, run canvas check, fix what it lists, and reply done in one line."
#: Live mode runs only when the state root is under here (the rig of section 7).
RIG_ROOTS = ("/var/tmp/syn-e2e/", "/private/var/tmp/syn-e2e/")
TRACE_SWITCH = "bench.on"
QUIET_S = 5.0
POLL_S = 2.0
#: How long a delivered request may take to start before the bench stops waiting for "working".
START_GRACE_S = 90.0

_COORD_RE = re.compile(r"\bc-?[0-9]{1,5}r-?[0-9]{1,5}\b")
_EDGE_RE = re.compile(r"^\s*(.+?)\s*(<->|-->|->|--)\s*(.+?)\s*$")
_QUOTED_RE = re.compile(r'"((?:[^"\\]|\\.)*)"')
_OPERATOR_WORDS = ('"op"', "{", "}", "->")


class BenchError(Exception):
    """A fixture or usage problem: the message says what and where."""


# --------------------------------------------------------------------------
# text matching


def fold(text: Any) -> str:
    """Case-insensitive, whitespace-folded text."""
    return " ".join(str(text or "").split()).casefold()


def label_match(expected: Any, found: Any) -> bool:
    """A found text matches an expected label when they are equal after folding, or when either contains the other and
    the shorter is at least 4 characters (6.2)."""
    e, f = fold(expected), fold(found)
    if not e or not f:
        return False
    if e == f:
        return True
    if min(len(e), len(f)) < 4:
        return False
    return e in f or f in e


def _any_match(expected: str, texts: Iterable[str]) -> bool:
    return any(label_match(expected, t) for t in texts)


# --------------------------------------------------------------------------
# fixtures


def _read_json(path: Path) -> Any:
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as err:
        raise BenchError("{}: {}".format(path, err))


def _check_expect(where: str, expect: Any) -> List[str]:
    problems: List[str] = []
    if not isinstance(expect, dict):
        return ["{}: expect is an object".format(where)]
    for key in expect:
        if key not in EXPECT_KEYS:
            problems.append("{}: expect has an unknown key {!r} (one of {})".format(where, key, ", ".join(EXPECT_KEYS)))
    labels = expect.get("labels")
    if not isinstance(labels, list) or not labels or not all(isinstance(x, str) and x.strip() for x in labels):
        problems.append("{}: expect.labels is a non-empty list of texts".format(where))
    for key in ("relations", "gone_relations"):
        for rel in expect.get(key) or []:
            if not (isinstance(rel, list) and len(rel) in (2, 3) and all(isinstance(x, str) and x for x in rel)
                    and (len(rel) == 2 or rel[2] in RELATION_KINDS)):
                problems.append("{}: {} entry {} is [from, to] or [from, to, edge|contains|any]".format(where, key, rel))
    for key in ("tones", "status"):
        value = expect.get(key)
        if value is not None and (not isinstance(value, dict) or not all(isinstance(v, (str, list)) for v in value.values())):
            problems.append("{}: expect.{} maps a label to a value (or a list of accepted values)".format(where, key))
    for key in ("changes", "absent_labels", "kinds_any"):
        value = expect.get(key)
        if value is not None and (not isinstance(value, list) or not all(isinstance(x, str) for x in value)):
            problems.append("{}: expect.{} is a list of texts".format(where, key))
    proposal = expect.get("proposal")
    if proposal is not None and (not isinstance(proposal, dict) or not isinstance(proposal.get("count"), int)):
        problems.append("{}: expect.proposal is {{count, about?, text?}}".format(where))
    chart = expect.get("chart")
    if chart is not None and (not isinstance(chart, dict) or not isinstance(chart.get("type_any"), list)):
        problems.append("{}: expect.chart is {{type_any: [...], fields: [...]}}".format(where))
    scene = expect.get("scene3d")
    if scene is not None and (not isinstance(scene, dict) or not isinstance(scene.get("objects_min", 0), int)):
        problems.append("{}: expect.scene3d is {{objects_min, links_min}}".format(where))
    return problems


def _check_seed(where: str, seed: Any, ids: Set[str]) -> List[str]:
    if seed is None:
        return []
    if not isinstance(seed, dict) or seed.get("as") not in SEED_AS:
        return ["{}: seed is {{as: member|operator, ops: [...] | from: <prompt id>}}".format(where)]
    if seed.get("from") is not None:
        return [] if seed["from"] in ids else ["{}: seed.from names no prompt ({})".format(where, seed["from"])]
    ops = seed.get("ops")
    return [] if isinstance(ops, list) and ops else ["{}: seed needs ops or from".format(where)]


def _prompt_text_problems(where: str, text: str) -> List[str]:
    """A request is what an operator would say: no op names or fields, no JSON, no coordinates."""
    problems: List[str] = []
    for word in _OPERATOR_WORDS:
        if word in text:
            problems.append("{}: the request contains {!r} (write it as an operator would say it)".format(where, word))
    if _COORD_RE.search(text):
        problems.append("{}: the request names a cell ({})".format(where, _COORD_RE.search(text).group(0)))  # type: ignore[union-attr]
    for field in ('"shape"', '"arrow"', '"frame"', "shape:", "arrow:", "right_of", "if_version"):
        if field in text:
            problems.append("{}: the request uses the field {!r}".format(where, field))
    return problems


def load_prompts(select: str = "all") -> List[Dict[str, Any]]:
    """The prompts, in id order: ``all``, ``live``, or ids (``01,04`` or full ids)."""
    found = [_read_json(path) for path in sorted(PROMPTS_DIR.glob("*.json"))]
    for doc, path in zip(found, sorted(PROMPTS_DIR.glob("*.json"))):
        doc["_file"] = os.fspath(path)
    if select in ("", "all", None):
        return found
    if select == "live":
        return [p for p in found if p.get("live")]
    wanted = [s.strip() for s in str(select).split(",") if s.strip()]
    picked: List[Dict[str, Any]] = []
    for want in wanted:
        match = [p for p in found if p.get("id") == want or str(p.get("id", "")).split("-", 1)[0] == want]
        if not match:
            raise BenchError("no prompt {!r} (ids look like 01 or 01-flow-checkout)".format(want))
        picked.extend(m for m in match if m not in picked)
    return picked


def load_controls() -> List[Dict[str, Any]]:
    found = []
    for path in sorted(CONTROLS_DIR.glob("*.json")):
        doc = _read_json(path)
        doc["_file"] = os.fspath(path)
        found.append(doc)
    return found


def prompt_by_id(prompts: Sequence[Dict[str, Any]], pid: str) -> Dict[str, Any]:
    for p in prompts:
        if p.get("id") == pid:
            return p
    raise BenchError("no prompt {!r}".format(pid))


def check_fixtures() -> Dict[str, Any]:
    """Every rule of 6.2 and 6.3 over the fixture folder: ``{"problems": [...], "counts": {...}}``."""
    problems: List[str] = []
    prompts = load_prompts("all")
    ids = [str(p.get("id")) for p in prompts]
    id_set = set(ids)
    if len(prompts) != PROMPT_COUNT:
        problems.append("there are {} prompts, not {}".format(len(prompts), PROMPT_COUNT))
    if len(id_set) != len(ids):
        problems.append("prompt ids repeat: {}".format(sorted({i for i in ids if ids.count(i) > 1})))
    counts: Dict[str, int] = {}
    nondev = live = v1 = data_prompts = 0
    data_kinds: Set[str] = set()
    for p in prompts:
        where = Path(p["_file"]).name
        for key in p:
            if key not in PROMPT_KEYS and key != "_file":
                problems.append("{}: unknown key {!r}".format(where, key))
        if p.get("id") != Path(p["_file"]).stem:
            problems.append("{}: id {!r} is not the file stem".format(where, p.get("id")))
        category = p.get("category")
        if category not in CATEGORIES:
            problems.append("{}: category {!r} is not one of {}".format(where, category, ", ".join(CATEGORIES)))
        counts[str(category)] = counts.get(str(category), 0) + 1
        if p.get("audience") not in AUDIENCES:
            problems.append("{}: audience is dev or nondev".format(where))
        nondev += p.get("audience") == "nondev"
        live += bool(p.get("live"))
        text = p.get("prompt")
        if not isinstance(text, str) or len(text.split()) < 6:
            problems.append("{}: prompt is the operator's request in words".format(where))
        else:
            problems.extend(_prompt_text_problems(where, text))
        problems.extend(_check_expect(where, p.get("expect")))
        problems.extend(_check_seed(where, p.get("seed"), id_set))
        if category == "edit" and not p.get("seed"):
            problems.append("{}: an edit prompt needs a seed".format(where))
        ref = p.get("reference")
        if not isinstance(ref, list) or not ref or not all(isinstance(op, dict) and op.get("op") for op in ref):
            problems.append("{}: reference is a non-empty list of ops".format(where))
        if p.get("reference_v1") is not None:
            if not isinstance(p["reference_v1"], list) or not p["reference_v1"]:
                problems.append("{}: reference_v1 is a non-empty list of ops".format(where))
            else:
                v1 += 1
        for name in p.get("artifacts") or []:
            source = DATA_DIR / str(name)
            if not source.is_file():
                problems.append("{}: data file {} is missing from data/".format(where, name))
            else:
                data_kinds.add(source.suffix.lower())
        if p.get("artifacts") and category == "chart":
            data_prompts += 1
    for category, want in CATEGORIES.items():
        if counts.get(category, 0) != want:
            problems.append("category {} has {} prompts, not {}".format(category, counts.get(category, 0), want))
    if nondev < NONDEV_MIN:
        problems.append("{} prompts are for non-developer teams; at least {}".format(nondev, NONDEV_MIN))
    if live != LIVE_COUNT:
        problems.append("{} prompts are marked live, not {}".format(live, LIVE_COUNT))
    if v1 < V1_MIN:
        problems.append("{} prompts have reference_v1; at least {}".format(v1, V1_MIN))
    if data_prompts < DATA_PROMPTS_MIN or not {".csv", ".json", ".tsv"} <= data_kinds:
        problems.append("charts from data files need CSV, JSON and TSV ({} prompts, {})".format(data_prompts, sorted(data_kinds)))
    size = sum(f.stat().st_size for f in DATA_DIR.glob("*") if f.is_file()) if DATA_DIR.is_dir() else 0
    if size > DATA_MAX_BYTES:
        problems.append("data/ holds {} bytes; at most {}".format(size, DATA_MAX_BYTES))
    controls = load_controls()
    if len(controls) < CONTROLS_MIN:
        problems.append("{} negative controls; at least {}".format(len(controls), CONTROLS_MIN))
    for c in controls:
        where = "controls/" + Path(c["_file"]).name
        for key in c:
            if key not in CONTROL_KEYS and key != "_file":
                problems.append("{}: unknown key {!r}".format(where, key))
        if c.get("id") != Path(c["_file"]).stem:
            problems.append("{}: id is not the file stem".format(where))
        if not c.get("expect_fail") and c.get("expect_crossings_min") is None:
            problems.append("{}: a control says what must be flagged (expect_fail or expect_crossings_min)".format(where))
        if c.get("prompt") is not None and c["prompt"] not in id_set:
            problems.append("{}: prompt names no prompt".format(where))
        if c.get("prompt") is None:
            problems.extend(_check_expect(where, c.get("expect")))
        problems.extend(_check_seed(where, c.get("seed"), id_set))
        if not isinstance(c.get("reference"), list) and not c.get("events"):
            problems.append("{}: a control draws something (reference or events)".format(where))
    return {"problems": problems, "counts": {"prompts": len(prompts), "categories": counts, "nondev": nondev, "live": live,
                                             "reference_v1": v1, "data_prompts": data_prompts, "controls": len(controls)}}


# --------------------------------------------------------------------------
# what a board says: one model, built from the scene (drawn) or from look (read back)


class Model:
    """Texts, relations (edges and containment), kinds, tones, statuses, charts and 3D scenes on a board."""

    def __init__(self) -> None:
        self.texts: List[str] = []
        self.edges: List[Tuple[str, str, bool]] = []  # (from, to, directed)
        self.contains: List[Tuple[str, str]] = []
        self.kinds: Set[str] = set()
        self.tones: List[Tuple[str, str]] = []
        self.status: List[Tuple[str, str]] = []
        self.charts: List[Dict[str, Any]] = []
        self.scenes: List[Dict[str, Any]] = []

    def text(self, value: Any) -> None:
        if isinstance(value, str) and value.strip():
            self.texts.append(value.strip())

    def edge(self, a: Any, b: Any, directed: bool = True) -> None:
        if isinstance(a, str) and isinstance(b, str) and a.strip() and b.strip():
            self.edges.append((a.strip(), b.strip(), bool(directed)))

    def contain(self, outer: Any, inner: Any) -> None:
        if isinstance(outer, str) and isinstance(inner, str) and outer.strip() and inner.strip():
            self.contains.append((outer.strip(), inner.strip()))

    def tone(self, label: Any, tone: Any) -> None:
        if isinstance(label, str) and label.strip() and isinstance(tone, str) and tone:
            self.tones.append((label.strip(), tone))

    def to_json(self) -> Dict[str, Any]:
        return {"texts": self.texts, "edges": [list(e) for e in self.edges], "contains": [list(c) for c in self.contains],
                "kinds": sorted(self.kinds), "tones": [list(t) for t in self.tones], "status": [list(s) for s in self.status],
                "charts": self.charts, "scenes": self.scenes}


#: The channel fields a chart spec may name.
CHART_CHANNELS = ("x", "y", "z", "color", "size", "label", "category", "value", "source", "target", "path")


def _chart_fields(spec: Dict[str, Any]) -> List[str]:
    fields: List[str] = []
    for key in CHART_CHANNELS:
        value = spec.get(key)
        for item in (value if isinstance(value, list) else [value]):
            if isinstance(item, str) and item and item not in fields:
                fields.append(item)
    return fields


def _parse_edge(value: Any) -> Optional[Tuple[str, str, str, Optional[str]]]:
    """``"a -> b: label"`` as ``(a, arrow, b, label)``; an object ``{from, to, label, head, tail}`` too."""
    if isinstance(value, dict):
        a, b = value.get("from"), value.get("to")
        if not isinstance(a, str) or not isinstance(b, str):
            return None
        head, tail = value.get("head"), value.get("tail")
        arrow = "->"
        if head == "none" and tail in (None, "none"):
            arrow = "--"
        elif tail not in (None, "none"):
            arrow = "<->"
        return a, arrow, b, value.get("label") if isinstance(value.get("label"), str) else None
    if not isinstance(value, str):
        return None
    body, label = value, None
    match = _EDGE_RE.match(body)
    if match is None:
        return None
    a, arrow, rest = match.group(1), match.group(2), match.group(3)
    if ":" in rest:
        rest, _colon, label = rest.partition(":")
        rest, label = rest.strip(), label.strip()
    return a, arrow, rest, label


def _mindmap_topics(spec: Dict[str, Any], root: str, model: Model) -> None:
    def walk(parent: str, value: Any) -> None:
        if isinstance(value, dict):
            for key, children in value.items():
                model.text(key)
                model.edge(parent, str(key), directed=False)
                walk(str(key), children)
        elif isinstance(value, list):
            for item in value:
                if isinstance(item, str):
                    model.text(item)
                    model.edge(parent, item, directed=False)
                else:
                    walk(parent, item)

    walk(root, spec.get("tree"))
    topics = spec.get("topics")
    if isinstance(topics, list):
        names: Dict[str, str] = {}
        for t in topics:
            if isinstance(t, dict) and isinstance(t.get("text"), str):
                names[str(t.get("id") or t["text"])] = t["text"]
        for t in topics:
            if not isinstance(t, dict) or not isinstance(t.get("text"), str):
                continue
            model.text(t["text"])
            model.tone(t["text"], t.get("tone"))
            under = t.get("under")
            parent = names.get(str(under), str(under)) if under is not None else root
            model.edge(parent, t["text"], directed=False)


def spec_into(model: Model, spec: Dict[str, Any]) -> str:
    """Add one block spec (as ``look`` reads it back) to the model; the block's name (its title)."""
    op = str(spec.get("op") or "")
    title = spec.get("title") if isinstance(spec.get("title"), str) else ""
    model.kinds.add(op)
    model.text(title)
    model.tone(title, spec.get("tone"))
    if op == "graph":
        names: Dict[str, str] = {}
        for node in spec.get("nodes") or []:
            if isinstance(node, str):
                names[node] = node
                model.text(node)
                continue
            if not isinstance(node, dict):
                continue
            text = node.get("text") if isinstance(node.get("text"), str) else str(node.get("id") or "")
            names[str(node.get("id") or text)] = text
            model.text(text)
            model.tone(text, node.get("tone"))
        groups: Dict[str, str] = {}
        for group in spec.get("groups") or []:
            if isinstance(group, dict):
                gtitle = group.get("title") if isinstance(group.get("title"), str) else str(group.get("id") or "")
                groups[str(group.get("id"))] = gtitle
                model.text(gtitle)
                model.tone(gtitle, group.get("tone"))
                model.contain(title, gtitle)
        for group in spec.get("groups") or []:
            if isinstance(group, dict) and group.get("parent") is not None:
                model.contain(groups.get(str(group["parent"]), ""), groups.get(str(group.get("id")), ""))
        for node in spec.get("nodes") or []:
            if isinstance(node, dict):
                text = names.get(str(node.get("id") or node.get("text")), "")
                if node.get("in") is not None:
                    model.contain(groups.get(str(node["in"]), str(node["in"])), text)
                else:
                    model.contain(title, text)
        for edge in spec.get("edges") or []:
            parsed = _parse_edge(edge)
            if parsed is None:
                continue
            a, arrow, b, label = parsed
            model.edge(names.get(a, a), names.get(b, b), directed=arrow in ("->", "-->"))
            model.text(label)
    elif op == "mindmap":
        root = spec.get("root") if isinstance(spec.get("root"), str) else title
        model.text(root)
        _mindmap_topics(spec, root, model)
    elif op == "sequence":
        names = {}
        for part in spec.get("participants") or []:
            if isinstance(part, str):
                names[part] = part
            elif isinstance(part, dict):
                names[str(part.get("id") or part.get("text"))] = str(part.get("text") or part.get("id") or "")
        for name in names.values():
            model.text(name)
            model.contain(title, name)
        for message in spec.get("messages") or []:
            if isinstance(message, dict):
                a, b, label = message.get("from"), message.get("to"), message.get("text")
            else:
                parsed = _parse_edge(message)
                if parsed is None:
                    continue
                a, _arrow, b, label = parsed
            model.edge(names.get(str(a), str(a)), names.get(str(b), str(b)))
            model.text(label)
        for note in spec.get("notes") or []:
            if isinstance(note, dict):
                model.text(note.get("text"))
    elif op == "kanban":
        for column in spec.get("columns") or []:
            if not isinstance(column, dict):
                continue
            ctitle = column.get("title") if isinstance(column.get("title"), str) else str(column.get("id") or "")
            model.text(ctitle)
            model.tone(ctitle, column.get("tone"))
            model.contain(title, ctitle)
            for card in column.get("cards") or []:
                if isinstance(card, str):
                    model.text(card)
                    model.contain(ctitle, card)
                elif isinstance(card, dict):
                    _card_into(model, card)
                    model.contain(ctitle, card.get("title"))
    elif op == "table":
        keys: List[str] = []
        for n, column in enumerate(spec.get("columns") or []):
            if isinstance(column, str):
                keys.append(column)
                model.text(column)
            elif isinstance(column, dict):
                keys.append(str(column.get("key") or column.get("title") or n))
                model.text(column.get("title"))
        for row in spec.get("rows") or []:
            cells: List[Any] = []
            tone = None
            if isinstance(row, list):
                cells = row
            elif isinstance(row, dict):
                raw = row.get("cells")
                tone = row.get("tone")
                if isinstance(raw, list):
                    cells = raw
                elif isinstance(raw, dict):
                    cells = [raw.get(k) for k in keys]
            _row_into(model, [c for c in cells], tone)
    elif op == "timeline":
        for event in spec.get("events") or []:
            if isinstance(event, dict):
                model.text(event.get("title"))
                model.text(event.get("body"))
                model.tone(event.get("title"), event.get("tone"))
                model.contain(title, event.get("title"))
    elif op == "chart":
        model.charts.append({"type": spec.get("type"), "fields": _chart_fields(spec), "title": title})
    elif op == "scene3d":
        _scene3d_into(model, spec.get("objects") or [], spec.get("links") or [])
    return title


def _card_into(model: Model, card: Dict[str, Any]) -> None:
    text = card.get("title") if isinstance(card.get("title"), str) else card.get("text")
    model.text(text)
    model.text(card.get("body"))
    model.tone(text, card.get("tone"))
    if isinstance(card.get("status"), str):
        model.status.append((str(text or ""), card["status"]))
    for badge in card.get("badges") or []:
        model.text(badge.get("text") if isinstance(badge, dict) else badge)


def _row_into(model: Model, cells: Sequence[Any], tone: Any) -> None:
    texts = [str(c) for c in cells if isinstance(c, (str, int, float)) and str(c).strip()]
    for t in texts:
        model.text(t)
    if texts:
        model.tone(texts[0], tone)
        for t in texts[1:]:
            model.contain(texts[0], t)


def _scene3d_into(model: Model, objects: Sequence[Any], links: Sequence[Any]) -> None:
    names: Dict[str, str] = {}
    for obj in objects:
        if isinstance(obj, dict):
            label = obj.get("label") if isinstance(obj.get("label"), str) else obj.get("text")
            names[str(obj.get("id"))] = label if isinstance(label, str) and label else str(obj.get("id"))
            model.text(label)
            model.tone(label, obj.get("tone"))
    count = 0
    for link in links:
        parsed = _parse_edge(link)
        if parsed is None:
            continue
        a, arrow, b, label = parsed
        count += 1
        model.edge(names.get(a, a), names.get(b, b), directed=arrow in ("->", "-->"))
        model.text(label)
    model.scenes.append({"objects": len([o for o in objects if isinstance(o, dict)]), "links": count})


def _el_name(el: Optional[Dict[str, Any]]) -> str:
    return str((el or {}).get("text") or "").strip()


def _arrow_edge(model: Model, el: Dict[str, Any], by_id: Dict[str, Dict[str, Any]]) -> None:
    a, b = _el_name(by_id.get(str(el.get("from")))), _el_name(by_id.get(str(el.get("to"))))
    if not a or not b:
        return
    head, tail = el.get("head"), el.get("tail")
    if head not in (None, "none") and tail in (None, "none"):
        model.edge(a, b)
    elif tail not in (None, "none") and head in (None, "none"):
        model.edge(b, a)
    else:
        model.edge(a, b, directed=False)


def _element_into(model: Model, el: Dict[str, Any], by_id: Dict[str, Dict[str, Any]], texts: bool) -> None:
    """One scene element's facts: kind, tone, status, its container, and what a single-element kind holds inside."""
    kind = str(el.get("type") or "")
    model.kinds.add(kind)
    if isinstance(el.get("block"), str):
        model.kinds.add(el["block"])
    name = _el_name(el)
    if texts:
        model.text(name)
        model.text(el.get("body"))
        for badge in el.get("badges") or []:
            model.text(badge.get("text") if isinstance(badge, dict) else badge)
    tone = (el.get("style") or {}).get("tone") if isinstance(el.get("style"), dict) else None
    if kind != "arrow":
        model.tone(name, tone)
        model.tone(el.get("body"), tone)  # a callout's point is often its body
    if isinstance(el.get("status"), str):
        model.status.append((name, el["status"]))
    if kind == "arrow":
        _arrow_edge(model, el, by_id)
    frame = by_id.get(str(el.get("frame"))) if el.get("frame") else None
    if frame is not None and kind != "arrow":
        model.contain(_el_name(frame), name)
    if kind == "table":
        columns = [str(c.get("id")) for c in el.get("columns") or [] if isinstance(c, dict)]
        if texts:
            for c in el.get("columns") or []:
                if isinstance(c, dict):
                    model.text(c.get("title"))
        for row in el.get("rows") or []:
            if isinstance(row, dict) and isinstance(row.get("cells"), dict):
                cells = [row["cells"].get(k) for k in columns]
                if texts:
                    _row_into(model, cells, row.get("tone"))
                else:
                    values = [str(c) for c in cells if isinstance(c, (str, int, float)) and str(c).strip()]
                    if values:
                        model.tone(values[0], row.get("tone"))
                        for v in values[1:]:
                            model.contain(values[0], v)
    elif kind == "sequence":
        names = {str(p.get("id")): str(p.get("text") or p.get("id")) for p in el.get("participants") or [] if isinstance(p, dict)}
        for part in names.values():
            model.contain(name, part)
        for message in el.get("messages") or []:
            if isinstance(message, dict):
                model.edge(names.get(str(message.get("from")), ""), names.get(str(message.get("to")), ""))
    elif kind == "chart":
        settings = el.get("settings") if isinstance(el.get("settings"), dict) else {}
        model.charts.append({"type": settings.get("type"), "fields": _chart_fields(settings), "title": name})
    elif kind == "scene3d":
        _scene3d_into(model, el.get("objects") or [], el.get("links") or [])


def _dl_texts(items: Any, out: List[str]) -> None:
    for item in items or []:
        if not isinstance(item, dict):
            continue
        if item.get("k") == "text":
            joined = " ".join(str(line.get("t") or "") for line in item.get("lines") or [] if isinstance(line, dict))
            if joined.strip():
                out.append(joined.strip())
        for key in ("items", "fallback"):
            if isinstance(item.get(key), list):
                _dl_texts(item[key], out)


def drawn_model(scene: Dict[str, Any], display: Optional[Dict[str, Any]] = None) -> Model:
    """What is drawn: the texts the display list paints (the page and the agents' picture draw that list), the relations,
    kinds, tones and data from the scene's elements. A 3D scene's labels are painted by WebGL, so they come from its
    objects."""
    model = Model()
    elements = [el for el in scene.get("elements") or [] if isinstance(el, dict)]
    by_id = {str(el.get("id")): el for el in elements}
    for el in elements:
        if el.get("type") == "comment":
            continue
        _element_into(model, el, by_id, texts=False)
    display = display if display is not None else D.display_list(scene)
    texts: List[str] = []
    for entry in display.get("entries") or []:
        # Overlays (claims, presence halos and cursors) and proposal ghosts are not the drawing.
        if isinstance(entry, dict) and entry.get("layer") != "overlays" and entry.get("kind") not in ("claim", "comment", "proposal"):
            _dl_texts(entry.get("items"), texts)
    for t in texts:
        model.text(t)
    return model


def _elsewhere_texts(model: Model, entry: Dict[str, Any]) -> None:
    model.text(entry.get("text"))
    for quoted in _QUOTED_RE.findall(str(entry.get("line") or "")):
        model.text(quoted.replace('\\"', '"'))
    if isinstance(entry.get("type"), str):
        model.kinds.add(entry["type"])


def readback_model(look: Dict[str, Any], specs: Optional[Dict[str, Dict[str, Any]]] = None) -> Model:
    """What another member reads: each block as the op ``look --block`` prints (``specs``, else the specs in ``look``),
    every other element from ``look``'s JSON (text, body, badges, tone, status, arrow ends, container), the one-line
    entries of an overview, and the gist lines (a chart's extremes, a 3D scene's objects)."""
    model = Model()
    specs = dict(look.get("blocks") or {}, **(specs or {}))
    elements = [el for el in look.get("elements") or [] if isinstance(el, dict)]
    by_id = {str(el.get("id")): el for el in elements}
    roots = set(specs)
    names: Dict[str, str] = {}
    for rid, spec in specs.items():
        el = by_id.get(rid)
        if el is not None and el.get("group") in roots:
            continue  # a kanban's columns are sections inside it: its spec already holds them
        names[rid] = spec_into(model, spec) if isinstance(spec, dict) else ""
    for el in elements:
        eid = str(el.get("id"))
        if el.get("type") == "comment":
            continue
        if eid in roots:
            frame = by_id.get(str(el.get("frame"))) if el.get("frame") else None
            if frame is not None:
                model.contain(_el_name(frame), names.get(eid) or _el_name(el))
            continue
        group = el.get("group")
        if isinstance(group, str) and group in roots and (specs.get(group) or {}).get("op") != "section":
            continue  # folded into its block's spec
        _element_into(model, el, by_id, texts=True)
    for entry in look.get("elsewhere") or []:
        if isinstance(entry, dict) and str(entry.get("id")) not in by_id:
            _elsewhere_texts(model, entry)
    for lines in (look.get("gist") or {}).values():
        for line in lines or []:
            model.text(line)
    return model


# --------------------------------------------------------------------------
# alignment of a model with what the prompt asked


def _rel(rel: Sequence[str]) -> Tuple[str, str, str]:
    return str(rel[0]), str(rel[1]), str(rel[2]) if len(rel) > 2 else "edge"


def _edge_found(model: Model, a: str, b: str) -> bool:
    for x, y, directed in model.edges:
        if label_match(a, x) and label_match(b, y):
            return True
        if not directed and label_match(a, y) and label_match(b, x):
            return True
    return False


def _contains_found(model: Model, a: str, b: str) -> bool:
    return any(label_match(a, x) and label_match(b, y) for x, y in model.contains)


def relation_found(model: Model, rel: Sequence[str]) -> bool:
    a, b, kind = _rel(rel)
    if kind == "contains":
        return _contains_found(model, a, b)
    if kind == "any":
        return _edge_found(model, a, b) or _edge_found(model, b, a) or _contains_found(model, a, b) or _contains_found(model, b, a)
    return _edge_found(model, a, b)


def _accepts(value: Any, found: str) -> bool:
    accepted = value if isinstance(value, list) else [value]
    return found in [str(v) for v in accepted]


def align(expect: Dict[str, Any], model: Model) -> Dict[str, Any]:
    """The alignment of a model with ``expect`` (6.4): recall of labels and relations, precision of the edges drawn
    between expected labels, the ``*_ok`` facts, and an F1 over labels and relations together."""
    labels = [str(x) for x in expect.get("labels") or []]
    relations = [list(r) for r in expect.get("relations") or []]
    missing_labels = [x for x in labels if not _any_match(x, model.texts)]
    missing_relations = [r for r in relations if not relation_found(model, r)]
    expected_pairs = {(fold(_rel(r)[0]), fold(_rel(r)[1])) for r in relations if _rel(r)[2] == "edge"}
    reversed_relations = [r for r in missing_relations if _rel(r)[2] == "edge" and (fold(r[1]), fold(r[0])) not in expected_pairs
                          and _edge_found(model, _rel(r)[1], _rel(r)[0])]
    # Precision: every edge whose two ends are expected labels must be an expected relation (either way when undrawn
    # heads leave it undirected).
    among = 0
    wrong: List[List[Any]] = []
    for x, y, directed in model.edges:
        if not (_any_match(x, labels) and _any_match(y, labels)):
            continue
        among += 1
        ok = False
        for r in relations:
            a, b, kind = _rel(r)
            if kind == "contains":
                continue
            if label_match(a, x) and label_match(b, y):
                ok = True
            elif (not directed or kind == "any") and label_match(a, y) and label_match(b, x):
                ok = True
            if ok:
                break
        if not ok:
            wrong.append([x, y, "edge" if directed else "undirected"])
    # Not measured when the request names no edge at all (a timeline, a table): extra arrows there are a style, not an error.
    measured = any(_rel(r)[2] != "contains" for r in relations)
    precision = 1.0 if among == 0 or not measured else (among - len(wrong)) / float(among)
    if not measured:
        wrong = []
    labels_recall = 1.0 if not labels else (len(labels) - len(missing_labels)) / float(len(labels))
    relations_recall = 1.0 if not relations else (len(relations) - len(missing_relations)) / float(len(relations))
    total = len(labels) + len(relations)
    recall = 1.0 if not total else (len(labels) - len(missing_labels) + len(relations) - len(missing_relations)) / float(total)
    f1 = 0.0 if precision + recall == 0 else 2 * precision * recall / (precision + recall)
    out: Dict[str, Any] = {"labels_recall": round(labels_recall, 4), "relations_recall": round(relations_recall, 4),
                           "relations_precision": round(precision, 4), "alignment": round(f1, 4),
                           "missing_labels": missing_labels, "missing_relations": missing_relations,
                           "reversed_relations": reversed_relations, "wrong_relations": wrong}
    if expect.get("kinds_any"):
        out["kinds_ok"] = any(k in model.kinds for k in expect["kinds_any"])
    if expect.get("tones"):
        bad = [label for label, tone in expect["tones"].items()
               if not any(label_match(label, found) and _accepts(tone, t) for found, t in model.tones)]
        out["tones_ok"] = not bad
        if bad:
            out["tones_missing"] = bad
    if expect.get("status"):
        bad = [label for label, st in expect["status"].items()
               if not any(label_match(label, found) and _accepts(st, s) for found, s in model.status)]
        out["status_ok"] = not bad
        if bad:
            out["status_missing"] = bad
    if expect.get("gone_relations"):
        still = [r for r in expect["gone_relations"] if relation_found(model, r)]
        out["gone_ok"] = not still
        if still:
            out["gone_still"] = still
    if expect.get("absent_labels"):
        present = [x for x in expect["absent_labels"] if _any_match(x, model.texts)]
        out["absent_ok"] = not present
        if present:
            out["absent_present"] = present
    chart = expect.get("chart")
    if chart:
        types = [str(t) for t in chart.get("type_any") or []]
        fields = [str(f) for f in chart.get("fields") or []]
        out["chart_ok"] = any((not types or str(c.get("type")) in types) and all(f in (c.get("fields") or []) for f in fields)
                              for c in model.charts)
    scene = expect.get("scene3d")
    if scene:
        out["scene3d_ok"] = any(s["objects"] >= int(scene.get("objects_min") or 0) and s["links"] >= int(scene.get("links_min") or 0)
                                for s in model.scenes)
    return out


# --------------------------------------------------------------------------
# geometry: crossings among what a member drew


def _polyline(entry: Optional[Dict[str, Any]], el: Dict[str, Any]) -> List[Tuple[float, float]]:
    hit = (entry or {}).get("hit") if isinstance(entry, dict) else None
    points = hit.get("points") if isinstance(hit, dict) and hit.get("shape") == "line" else None
    points = points or el.get("points") or []
    return [(float(p[0]), float(p[1])) for p in points if isinstance(p, (list, tuple)) and len(p) >= 2]


def _cross_point(p1: Tuple[float, float], p2: Tuple[float, float], p3: Tuple[float, float], p4: Tuple[float, float]) -> Optional[Tuple[float, float]]:
    """Where two segments properly cross (touching or collinear is not a crossing)."""
    def orient(a: Tuple[float, float], b: Tuple[float, float], c: Tuple[float, float]) -> int:
        value = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(value) < 1e-9 else (1 if value > 0 else -1)

    if orient(p1, p2, p3) * orient(p1, p2, p4) >= 0 or orient(p3, p4, p1) * orient(p3, p4, p2) >= 0:
        return None
    d = (p2[0] - p1[0]) * (p4[1] - p3[1]) - (p2[1] - p1[1]) * (p4[0] - p3[0])
    if abs(d) < 1e-12:
        return None
    t = ((p3[0] - p1[0]) * (p4[1] - p3[1]) - (p3[1] - p1[1]) * (p4[0] - p3[0])) / d
    return (p1[0] + t * (p2[0] - p1[0]), p1[1] + t * (p2[1] - p1[1]))


def _near_box(point: Tuple[float, float], el: Optional[Dict[str, Any]], slack: float) -> bool:
    if not el:
        return False
    x, y, w, h = (float(el.get(k) or 0) for k in ("x", "y", "w", "h"))
    return x - slack <= point[0] <= x + w + slack and y - slack <= point[1] <= y + h + slack


def route_crossings(routes: Sequence[Dict[str, Any]], by_id: Optional[Dict[str, Dict[str, Any]]] = None) -> int:
    """Pairs of routes (``{"ends": (from, to), "points": [...]}``) that cross away from an end they share."""
    by_id = by_id or {}
    count = 0
    for i in range(len(routes)):
        for j in range(i + 1, len(routes)):
            a, b = routes[i], routes[j]
            shared = {e for e in a.get("ends") or () if e} & {e for e in b.get("ends") or () if e}
            ends = [pts[k] for pts in (a["points"], b["points"]) if pts for k in (0, -1)]
            crossed = False
            for s0, s1 in zip(a["points"], a["points"][1:]):
                for t0, t1 in zip(b["points"], b["points"][1:]):
                    point = _cross_point(s0, s1, t0, t1)
                    if point is None:
                        continue
                    if any(math.hypot(point[0] - e[0], point[1] - e[1]) <= SHARED_END_SLACK for e in ends):
                        continue
                    if any(_near_box(point, by_id.get(e), SHARED_END_SLACK) for e in shared):
                        continue
                    crossed = True
                    break
                if crossed:
                    break
            count += crossed
    return count


def crossings(scene: Dict[str, Any], display: Dict[str, Any], ids: Set[str]) -> Dict[str, int]:
    """Crossings among the arrows (and block edges) in ``ids``, and the graphs' own count (the layout's view)."""
    from herdr_team.canvas_kinds import graph as _graph

    elements = [el for el in scene.get("elements") or [] if isinstance(el, dict)]
    by_id = {str(el.get("id")): el for el in elements}
    entries = {str(e.get("id")): e for e in display.get("entries") or [] if isinstance(e, dict)}
    routes = [{"ends": (el.get("from"), el.get("to")), "points": _polyline(entries.get(str(el["id"])), el)}
              for el in elements if el.get("type") == "arrow" and str(el.get("id")) in ids]
    blocks = 0
    for el in elements:
        if el.get("block") == "graph" and str(el.get("id")) in ids:
            edges = [((str(a.get("from")), str(a.get("to"))), _polyline(entries.get(str(a["id"])), a))
                     for a in elements if a.get("type") == "arrow" and a.get("group") == el.get("id")]
            blocks += _graph.edge_crossings([e for e in edges if len(e[1]) >= 2])
    return {"crossings": route_crossings([r for r in routes if len(r["points"]) >= 2], by_id), "block_crossings": blocks}


# --------------------------------------------------------------------------
# the window: what a member did after the seed


def _author_name(author: Any) -> str:
    return str(author.get("name") if isinstance(author, dict) else author or "")


def window_changes(team: Any, since: int, member: str) -> Dict[str, Any]:
    """Ids the member created or changed after version ``since``, and the proposals it made (from ``events.jsonl``)."""
    got = C.changes_since(team, since, limit=100000)
    ids: Set[str] = set()
    proposals: List[str] = []
    for event in got.get("events") or []:
        if _author_name(event.get("author")) != member:
            continue
        for eid in event.get("ids") or []:
            ids.add(str(eid))
        for change in event.get("changes") or []:
            if not isinstance(change, dict):
                continue
            if change.get("target") == "element" and change.get("id"):
                ids.add(str(change["id"]))
            elif change.get("target") == "proposal" and change.get("action") == "add" and change.get("id"):
                proposals.append(str(change["id"]))
    return {"ids": ids, "proposals": proposals}


def stability(before: Sequence[Dict[str, Any]], after: Sequence[Dict[str, Any]], changes: Sequence[str]) -> Dict[str, Any]:
    """Every seed element not named in ``changes`` kept its place (and its size, unless its content grows) within
    ``STABLE_UNITS``; arrows reroute freely."""
    now = {str(el.get("id")): el for el in after}
    moved: List[str] = []
    for el in before:
        if el.get("type") in ("arrow", "comment"):
            continue
        if any(label_match(name, _el_name(el)) for name in changes):
            continue
        eid = str(el.get("id"))
        cur = now.get(eid)
        if cur is None:
            moved.append("{} {!r} deleted".format(eid, _el_name(el)))
            continue
        dx = abs(float(cur.get("x") or 0) - float(el.get("x") or 0))
        dy = abs(float(cur.get("y") or 0) - float(el.get("y") or 0))
        dw = abs(float(cur.get("w") or 0) - float(el.get("w") or 0))
        dh = abs(float(cur.get("h") or 0) - float(el.get("h") or 0))
        grows = el.get("type") in GROWING_TYPES
        if dx > STABLE_UNITS or dy > STABLE_UNITS or (not grows and (dw > STABLE_UNITS or dh > STABLE_UNITS)):
            moved.append("{} {!r} moved {:.0f},{:.0f} resized {:.0f},{:.0f}".format(eid, _el_name(el), dx, dy, dw, dh))
    return {"stable": not moved, "moved": moved}


def proposal_facts(scene: Dict[str, Any], made: Sequence[str], expect: Dict[str, Any]) -> Dict[str, Any]:
    """Whether the proposals the member made are the ones asked for: the count, what they are about, what they say."""
    want = expect.get("proposal") or {}
    by_id = {str(p.get("id")): p for p in scene.get("proposals") or [] if isinstance(p, dict)}
    elements = {str(el.get("id")): el for el in scene.get("elements") or [] if isinstance(el, dict)}
    found = [by_id[p] for p in made if p in by_id]
    ok = len(found) == int(want.get("count") or 0)
    about = want.get("about")
    if ok and about:
        ok = all(any(label_match(about, _el_name(elements.get(str(t)))) for t in p.get("targets") or []) for p in found)
    text = want.get("text")
    if ok and text:
        ok = all(fold(text) in fold(" ".join(str(s) for s in p.get("summary") or [])) for p in found)
    return {"proposal_ok": bool(ok), "proposals": [{"id": p.get("id"), "status": p.get("status"), "targets": p.get("targets"),
                                                    "summary": p.get("summary")} for p in found]}


# --------------------------------------------------------------------------
# scoring one board


def read_back(layout: Any, team: Any, reader: str) -> Tuple[Dict[str, Any], Dict[str, Dict[str, Any]]]:
    """``look`` as ``reader`` (never advancing its cursor), and ``look --block`` for every block it names."""
    look = C.look(layout, team, reader, advance=False)
    specs: Dict[str, Dict[str, Any]] = {}
    candidates = list(look.get("blocks") or {})
    for entry in look.get("elsewhere") or []:
        if isinstance(entry, dict) and entry.get("type") not in ("arrow", "box", "ellipse", "diamond", "note", "text", "pen", "path",
                                                                 "comment", "card", "sticky", "callout", "heading", "badge", "icon"):
            candidates.append(str(entry.get("id")))
    for bid in candidates[:80]:
        try:
            got = C.look(layout, team, reader, advance=False, block=bid)
        except HerdrTeamError:
            continue
        if isinstance(got.get("block"), dict):
            specs[bid] = got["block"]
    return look, specs


def _problem_counts(problems: Sequence[Dict[str, Any]], ids: Set[str]) -> Tuple[Dict[str, int], Dict[str, int]]:
    hard = {code: 0 for code in HARD}
    soft: Dict[str, int] = {}
    for p in problems:
        if not isinstance(p, dict) or not (set(str(i) for i in p.get("ids") or []) & ids):
            continue
        code = str(p.get("code"))
        if code in hard:
            hard[code] += 1
        else:
            soft[code] = soft.get(code, 0) + 1
    return hard, soft


def _ok_keys(facts: Dict[str, Any]) -> List[str]:
    return [k for k in facts if k.endswith("_ok")]


def score(layout: Any, team: Any, expect: Dict[str, Any], member: str, reader: str, since: int,
          seed_elements: Sequence[Dict[str, Any]], valid_first_try: Optional[bool], attempts: int, refused_total: int,
          applied_any: Optional[bool] = None) -> Dict[str, Any]:
    """One row of 6.4 for the board as it is now, what ``member`` did after version ``since`` in its window."""
    scene = C.load_scene(team)
    display = D.display_list(scene)
    window = window_changes(team, since, member)
    touched = set(window["ids"])
    problems = C.check(layout, team, reader)["problems"]
    hard, soft = _problem_counts(problems, touched)
    look, specs = read_back(layout, team, reader)
    drawn = align(expect, drawn_model(scene, display))
    readback = align(expect, readback_model(look, specs))
    if expect.get("proposal"):
        facts = proposal_facts(scene, window["proposals"], expect)
        drawn["proposal_ok"] = readback["proposal_ok"] = facts["proposal_ok"]
    else:
        facts = {"proposals": []}
    stable: Optional[bool] = None
    moved: List[str] = []
    if expect.get("stable"):
        found = stability(seed_elements, scene.get("elements") or [], expect.get("changes") or [])
        stable, moved = found["stable"], found["moved"]
    valid = bool(touched or window["proposals"]) if applied_any is None else bool(applied_any)
    if expect.get("proposal"):
        valid = bool(window["proposals"])
    cross = crossings(scene, display, touched)
    reasons: List[str] = []
    if not valid:
        reasons.append("invalid")
    if valid_first_try is False:
        reasons.append("first_try")
    reasons.extend(code for code in HARD if hard[code])
    if readback["labels_recall"] < 1.0:
        reasons.append("labels")
    if readback["relations_recall"] < RELATIONS_PASS:
        reasons.append("relations")
    if readback["reversed_relations"]:
        reasons.append("reversed")
    for key in _ok_keys(readback):
        if not readback[key] or not drawn.get(key, True):
            reasons.append(key[: -len("_ok")])
    if stable is False:
        reasons.append("stable")
    if drawn["alignment"] >= 1.0 and readback["alignment"] < 1.0:
        reasons.append("readback")
    passed = (valid and not any(hard.values()) and readback["labels_recall"] >= 1.0 and readback["relations_recall"] >= RELATIONS_PASS
              and all(readback[k] and drawn.get(k, True) for k in _ok_keys(readback)) and stable is not False)
    return {"valid": valid, "valid_first_try": valid_first_try, "attempts": attempts, "refused_total": refused_total,
            "touched": len(touched), "proposals": facts["proposals"], "hard": hard, "soft": soft,
            "crossings": cross["crossings"], "block_crossings": cross["block_crossings"],
            # One number for every report to print: route crossings plus the ones inside the blocks it drew. The two
            # parts stay in the record; printing one of them in the console line and their sum in the table had the
            # same control report two different counts in one run (QA phase 6, F12).
            "crossings_total": cross["crossings"] + cross["block_crossings"],
            "drawn": drawn, "readback": readback, "alignment": {"drawn": drawn["alignment"], "readback": readback["alignment"]},
            "stable": stable, "moved": moved, "pass": bool(passed), "reasons": sorted(set(reasons), key=reasons.index),
            "version": scene.get("version")}


# --------------------------------------------------------------------------
# offline mode


def _seed_ops(item: Dict[str, Any], prompts: Sequence[Dict[str, Any]]) -> Tuple[Optional[str], List[Dict[str, Any]]]:
    seed = item.get("seed")
    if not seed:
        return None, []
    if seed.get("from"):
        return str(seed["as"]), list(prompt_by_id(prompts, seed["from"]).get("reference") or [])
    return str(seed["as"]), list(seed.get("ops") or [])


def copy_data(names: Sequence[str], dest: Path) -> None:
    dest.mkdir(parents=True, exist_ok=True)
    for name in names:
        source = DATA_DIR / name
        if not source.is_file():
            raise BenchError("data file {} is missing (tests/fixtures/canvas_bench/data)".format(name))
        shutil.copyfile(os.fspath(source), os.fspath(dest / name))


def _qa_artifacts(qa: Any) -> Path:
    from herdr_team import workdir

    project = qa.tmp / "project"
    project.mkdir(exist_ok=True)
    doc = store.read_json(qa.team.team_json)
    doc.setdefault("config", {})["project_dir"] = os.fspath(project)
    store.write_json(qa.team.team_json, doc)
    return Path(workdir.paths_for(os.fspath(project), Q.TEAM)["artifacts"])


def _append_raw_events(team: Any, author: Any, elements: Sequence[Dict[str, Any]], intent: str = "replayed from 0.21") -> None:
    """Append 0.21-style element events as ``author`` and drop the cached scene, so the next read replays them (a mark
    drawn before 0.22 sized labels, as the migration fixtures hold)."""
    version = C.current_version(team)
    lines = []
    for n, el in enumerate(elements):
        value = dict(el)
        value.setdefault("author", author.name)
        value.setdefault("author_kind", "member" if author.is_member else "human")
        now = C._iso(time.time())
        value.setdefault("created_at", now)
        value.setdefault("updated_at", now)
        value.setdefault("created_seq", version + n + 1)
        value.setdefault("updated_seq", version + n + 1)
        event = {"v": C.SCHEMA, "seq": version + n + 1, "ts": now, "batch": None,
                 "author": {"name": author.name, "kind": "member" if author.is_member else "human", "agent": author.agent,
                            "via": "cli", "verified": True},
                 "op": "shape", "index": 0, "intent": intent, "ids": [value["id"]],
                 "changes": [{"target": "element", "action": "add", "id": value["id"], "value": value}]}
        lines.append(json.dumps(event, separators=(",", ":")) + "\n")
    folder = features.whiteboard_dir(team)
    folder.mkdir(parents=True, exist_ok=True)
    with (folder / C.EVENTS_FILE).open("a", encoding="utf-8") as handle:
        handle.writelines(lines)
    scene = folder / C.SCENE_FILE
    if scene.exists():
        scene.unlink()


def _batch_bytes(ops: Sequence[Dict[str, Any]]) -> int:
    return len(json.dumps(list(ops), separators=(",", ":"), ensure_ascii=False).encode("utf-8"))


def run_offline_item(item: Dict[str, Any], prompts: Sequence[Dict[str, Any]], language: str = "v2",
                     pictures: Optional[Path] = None, control: bool = False) -> Dict[str, Any]:
    """Apply one prompt's reference (or a control's drawing) to a throwaway team and score it. A control's ``prompt``
    names the prompt whose expectation, seed and data it borrows (its own keys win)."""
    linked = prompt_by_id(prompts, item["prompt"]) if control and item.get("prompt") else None
    expect = dict(item.get("expect") or (linked or {}).get("expect") or {})
    if language == "v1":
        # The 0.21 language has no blocks and no card status: those facts compare engines, not languages (D15).
        expect.pop("kinds_any", None)
        expect.pop("status", None)
    ops = list(item.get("reference_v1") if language == "v1" else item.get("reference") or [])
    row: Dict[str, Any] = {"id": item.get("id"), "language": language, "member": Q.MEMBER, "mode": "offline",
                           "ops": len(ops), "bytes": _batch_bytes(ops)}
    with Q.QaTeam() as qa:
        names = list(item.get("artifacts") or (linked or {}).get("artifacts") or [])
        seed_as, seed = _seed_ops(item, prompts)
        if seed_as is None and linked is not None:
            seed_as, seed = _seed_ops(linked, prompts)
        if names:
            copy_data(names, _qa_artifacts(qa))
        if seed:
            author = qa.author_for("lead" if seed_as == "operator" else "drawer")
            got = C.apply_ops(qa.layout, qa.team, seed, author)
            if got.get("refused"):
                raise BenchError("{}: the seed was refused: {}".format(item.get("id"), got["refused"]))
        since = C.current_version(qa.team)
        seed_elements = [dict(el) for el in C.load_scene(qa.team).get("elements") or []]
        refused: List[Dict[str, Any]] = []
        error: Optional[str] = None
        applied_any = False
        started = time.monotonic()
        if item.get("events"):
            _append_raw_events(qa.team, qa.author, item["events"])
            applied_any = True
        if ops:
            try:
                C.look(qa.layout, qa.team, Q.MEMBER)  # the routine: look, then draw against what was read
                result = C.apply_ops(qa.layout, qa.team, ops, qa.author, base="last")
                refused = list(result.get("refused") or [])
                applied_any = applied_any or bool(result.get("applied") or result.get("proposed"))
            except HerdrTeamError as err:
                error = err.code
        seconds = time.monotonic() - started
        row.update(score(qa.layout, qa.team, expect, Q.MEMBER, Q.PEER, since, seed_elements,
                         valid_first_try=not refused and error is None, attempts=1, refused_total=len(refused),
                         applied_any=applied_any))
        row["seconds"] = round(seconds, 3)
        row["tokens"] = None
        row["error"] = error
        row["refused"] = [{"index": r.get("index"), "op": r.get("op"), "code": r.get("code")} for r in refused]
        if pictures is not None:
            row["picture"] = _picture(qa.layout, qa.team, Q.PEER, pictures, "{}-{}".format(item.get("id"), language))
    return row


def _picture(layout: Any, team: Any, reader: str, folder: Path, name: str) -> Optional[str]:
    """``look --image`` as a PNG in ``folder`` (only when resvg is installed; silently nothing otherwise)."""
    if not shutil.which("resvg") and not Path("/opt/homebrew/bin/resvg").exists():
        return None
    try:
        got = C.look(layout, team, reader, advance=False, image=True)
    except HerdrTeamError:
        return None
    image = got.get("image")
    if not image or not Path(str(image)).is_file():
        return None
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / "{}.png".format(name)
    shutil.copyfile(str(image), os.fspath(target))
    return os.fspath(target)


def _crossings(n: int) -> str:
    return "{} crossing{}".format(n, "" if n == 1 else "s")


def total_crossings(row: Mapping[str, Any]) -> int:
    """Every crossing a row counted: the routes' plus the blocks' own. The one number the reports print (F12)."""
    if row.get("crossings_total") is not None:
        return int(row["crossings_total"])
    return int(row.get("crossings") or 0) + int(row.get("block_crossings") or 0)


def control_flagged(control: Dict[str, Any], row: Dict[str, Any]) -> Dict[str, Any]:
    """A control is flagged when every reason its file names is among the row's, and its crossings reach the minimum."""
    want = [str(x) for x in control.get("expect_fail") or []]
    missing = [w for w in want if w not in row.get("reasons", [])]
    low = control.get("expect_crossings_min")
    crossings = total_crossings(row)
    crossed = low is None or crossings >= int(low)
    # ``expects`` reads as a sentence in the report: a control judged by its crossings alone (c08) names that, so no
    # row is ever printed as flagged with nothing said about why. ``flagged_for`` records the same in the row itself,
    # because a control flagged with an empty ``reasons`` list reads as a scorer that flagged without a reason (F12).
    expects = list(want) + (["at least {}".format(_crossings(int(low)))] if low is not None else [])
    found = [w for w in want if w in row.get("reasons", [])]
    if low is not None and crossed:
        found.append(_crossings(crossings))
    return {"flagged": not missing and crossed, "expected": want, "expects": expects, "unflagged": missing,
            "flagged_for": found, "crossings_ok": crossed, "crossings_min": None if low is None else int(low)}


def run_offline(prompts: Sequence[Dict[str, Any]], all_prompts: Sequence[Dict[str, Any]], language: str = "v2",
                controls: Optional[Sequence[Dict[str, Any]]] = None, pictures: Optional[Path] = None,
                progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Every selected reference in ``language`` (``v2``, ``v1`` or ``both``) and every control; rows and the verdict."""
    rows: List[Dict[str, Any]] = []
    languages = ["v2", "v1"] if language == "both" else [language]
    for prompt in prompts:
        for lang in languages:
            if lang == "v1" and not prompt.get("reference_v1"):
                continue
            row = run_offline_item(prompt, all_prompts, lang, pictures)
            row["category"], row["audience"], row["prompt"] = prompt.get("category"), prompt.get("audience"), prompt.get("prompt")
            rows.append(row)
            if progress:
                progress(summary_line(row))
    control_rows: List[Dict[str, Any]] = []
    for control in controls or []:
        row = run_offline_item(control, all_prompts, "v2", None, control=True)
        row["mode"] = "control"
        row["about"] = control.get("about")
        row.update(control_flagged(control, row))
        control_rows.append(row)
        if progress:
            progress(summary_line(row))
    failures = [r for r in rows if r["language"] == "v2" and not gate_ok(r)]
    unflagged = [r for r in control_rows if not r["flagged"]]
    return {"mode": "offline", "rows": rows, "controls": control_rows, "ok": not failures and not unflagged,
            "failures": [{"id": r["id"], "reasons": gate_reasons(r)} for r in failures],
            "unflagged": [{"id": r["id"], "missing": r["unflagged"], "reasons": r["reasons"]} for r in unflagged]}


def gate_reasons(row: Dict[str, Any]) -> List[str]:
    """Why a v2 reference does not hold the offline gate (pass, alignment 1.0 both ways, hard 0, first try)."""
    reasons = list(row.get("reasons") or [])
    if not row.get("pass") and not reasons:
        reasons.append("fail")
    for side in ("drawn", "readback"):
        if (row.get("alignment") or {}).get(side, 0) < 1.0:
            reasons.append("{}_alignment".format(side))
    if row.get("valid_first_try") is not True:
        reasons.append("first_try")
    return sorted(set(reasons), key=reasons.index)


def gate_ok(row: Dict[str, Any]) -> bool:
    return bool(row.get("pass")) and not gate_reasons(row)


# --------------------------------------------------------------------------
# live mode


def guard_rig(env: Dict[str, str]) -> None:
    """Refuse to run anywhere but the throwaway rig (a guard against the owner's session)."""
    if not env.get("HERDR_SESSION"):
        raise BenchError("live mode runs only inside the rig: HERDR_SESSION is unset (use ./e.sh)")
    state = env.get("XDG_STATE_HOME") or ""
    real = os.path.realpath(state) if state else ""
    if not state or not any((p + "/").startswith(root) or p.startswith(root) for p in (state, real) for root in RIG_ROOTS):
        raise BenchError("live mode runs only inside the rig: XDG_STATE_HOME ({}) is not under /var/tmp/syn-e2e/".format(state or "unset"))


class HerdrRunner:
    """Talks to the rig through the ``herdr`` and ``herdr-synapse`` on ``PATH`` (never a socket of its own)."""

    def __init__(self, team: str, deliver: str = "prompt", shell_pane: Optional[str] = None, env: Optional[Dict[str, str]] = None) -> None:
        if deliver not in ("prompt", "post"):
            raise BenchError("--deliver is prompt or post")
        if deliver == "post" and not shell_pane:
            raise BenchError("--deliver post needs --shell-pane (the operator's shell pane)")
        self.team, self.mode, self.shell_pane = team, deliver, shell_pane
        self.env = dict(env if env is not None else os.environ)

    def _run(self, argv: List[str], timeout: float = 30.0) -> str:
        done = subprocess.run(argv, env=self.env, capture_output=True, text=True, timeout=timeout, check=False)
        if done.returncode != 0:
            raise BenchError("{} failed ({}): {}".format(" ".join(argv[:3]), done.returncode, (done.stderr or done.stdout).strip()[:300]))
        return done.stdout

    def deliver(self, member: str, pane: str, text: str) -> None:
        if self.mode == "prompt":
            self._run(["herdr", "agent", "prompt", pane, text])
            return
        command = "herdr-synapse --team {} post {}".format(shlex.quote(self.team), shlex.quote("@{} {}".format(member, text)))
        self._run(["herdr", "pane", "send-text", str(self.shell_pane), command])
        self._run(["herdr", "pane", "send-keys", str(self.shell_pane), "enter"])

    def status(self, pane: str) -> str:
        try:
            out = self._run(["herdr", "agent", "get", pane], timeout=15.0)
            doc = json.loads(out)
        except (BenchError, ValueError, subprocess.SubprocessError):
            return "unknown"
        for holder in (doc.get("result", {}).get("agent") if isinstance(doc.get("result"), dict) else None, doc.get("agent"), doc):
            if isinstance(holder, dict) and holder.get("agent_status"):
                return str(holder["agent_status"])
        return "unknown"

    def now(self) -> float:
        return time.monotonic()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


class FakeRunner:
    """Answers a delivered request by applying that prompt's reference as the member, through the library; statuses go
    working then idle. No Herdr, no clock: ``sleep`` only advances a counter."""

    def __init__(self, layout: Any, team: Any, prompts: Sequence[Dict[str, Any]], kinds: Optional[Dict[str, str]] = None,
                 answers: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> None:
        self.layout, self.team, self.prompts = layout, team, list(prompts)
        self.kinds = dict(kinds or {})
        self.answers = dict(answers or {})
        self.clock = 0.0
        self.delivered: List[Tuple[str, str, str]] = []
        self._states: Dict[str, List[str]] = {}

    def deliver(self, member: str, pane: str, text: str) -> None:
        self.delivered.append((member, pane, text))
        prompt = next((p for p in self.prompts if text.startswith(str(p.get("prompt")))), None)
        if prompt is None:
            raise BenchError("the fake runner got a request it does not know")
        ops = self.answers.get(str(prompt["id"]), prompt.get("reference") or [])
        author = C.CanvasAuthor(member, C.KIND_MEMBER, "mcp", True, agent=self.kinds.get(member, "claude"), team=self.team.name)
        C.look(self.layout, self.team, member)
        try:
            C.apply_ops(self.layout, self.team, list(ops), author, base="last")
        except HerdrTeamError:
            pass  # a refused batch is a finding, scored from the attempt trace
        self._states[pane] = ["working", "working", "idle"]

    def status(self, pane: str) -> str:
        states = self._states.get(pane) or ["idle"]
        return states.pop(0) if len(states) > 1 else states[0]

    def now(self) -> float:
        return self.clock

    def sleep(self, seconds: float) -> None:
        self.clock += seconds


def _roster_member(team: Any, name: str) -> Dict[str, Any]:
    doc = store.RosterStore(team).load()
    for row in doc.get("members") or []:
        if isinstance(row, dict) and row.get("name") == name:
            return row
    raise BenchError("{} is not a member of team {}".format(name, team.name))


def _trace_switch(team: Any) -> Path:
    return features.whiteboard_dir(team) / TRACE_SWITCH


def read_trace(team: Any, since_ts: Optional[float], member: str) -> Optional[List[Dict[str, Any]]]:
    """The member's attempt records since ``since_ts`` (6.7), or None when this build has no attempt trace."""
    reader = getattr(C, "read_attempts", None)
    if reader is None:
        return None
    records = reader(team, since_ts=since_ts)
    return [r for r in records or [] if isinstance(r, dict) and _author_name(r.get("author")) == member]


def first_try(records: Optional[Sequence[Dict[str, Any]]]) -> Optional[bool]:
    """``True`` when the member's first attempt had no refused op and no batch-level error; None with no record."""
    if not records:
        return None
    first = records[0]
    return not first.get("refused") and not first.get("error")


def wait_settled(runner: Any, pane: str, team: Any, timeout: float) -> Dict[str, Any]:
    """Until the agent has worked and is idle or done again (or the timeout), then until the canvas stops changing."""
    start = runner.now()
    worked = False
    state = "unknown"
    before = C.current_version(team)
    while runner.now() - start < timeout:
        state = runner.status(pane)
        if state == "working":
            worked = True
        elif state in ("idle", "done"):
            # A turn quicker than one poll is seen by what it drew; one that never starts is given up after the grace.
            worked = worked or C.current_version(team) != before
            if worked or runner.now() - start > START_GRACE_S:
                break
        runner.sleep(POLL_S)
    timed_out = runner.now() - start >= timeout
    version, quiet_since = C.current_version(team), runner.now()
    while runner.now() - quiet_since < QUIET_S and runner.now() - start < timeout + 60.0:
        runner.sleep(1.0)
        current = C.current_version(team)
        if current != version:
            version, quiet_since = current, runner.now()
    return {"seconds": round(runner.now() - start, 1), "worked": worked, "state": state, "timed_out": timed_out}


def _jsonl_window(path: Path, start: float, end: float) -> Iterable[Dict[str, Any]]:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    record = json.loads(line)
                except ValueError:
                    continue
                stamp = C._parse_iso(record.get("timestamp")) if isinstance(record, dict) else None
                if stamp is not None and start <= stamp <= end:
                    yield record
    except OSError:
        return


def member_tokens(layout: Any, team: Any, member: str, start: float, end: float, env: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    """Tokens a member's harness spent between ``start`` and ``end`` (epoch seconds), read-only from its own files:
    Claude transcript usage records, Codex rollout ``token_count`` deltas, OpenCode ``opencode.db`` messages. A kind or
    a file this cannot read gives ``{"total": None, "reason": ...}``."""
    from herdr_team import context as ctx
    from herdr_team import roster as _roster

    env = dict(env if env is not None else os.environ)
    try:
        row = _roster_member(team, member)
    except BenchError as err:
        return {"total": None, "reason": str(err)}
    kind = str(row.get("kind") or "")
    session = row.get("session") if isinstance(row.get("session"), dict) else {}
    value = session.get("value") if isinstance(session, dict) else None
    home = ctx.home_dir(env)
    try:
        if kind == "claude":
            record = _roster.read_pane_record(layout.session, row.get("terminal_id")) or {}
            transcript = record.get("transcript_path") if isinstance(record.get("transcript_path"), str) else None
            transcript = transcript or (ctx.claude_transcript_by_id(str(value), home, env) if value else None)
            if not transcript:
                return {"total": None, "reason": "no Claude transcript for {}".format(member)}
            seen: Set[str] = set()
            sums = {"input": 0, "output": 0, "cache": 0}
            for rec in _jsonl_window(Path(transcript), start, end):
                message = rec.get("message")
                if not isinstance(message, dict) or not isinstance(message.get("usage"), dict):
                    continue
                mid = str(message.get("id") or rec.get("uuid") or len(seen))
                if mid in seen:
                    continue
                seen.add(mid)
                usage = message["usage"]
                sums["input"] += int(usage.get("input_tokens") or 0)
                sums["output"] += int(usage.get("output_tokens") or 0)
                sums["cache"] += int(usage.get("cache_read_input_tokens") or 0) + int(usage.get("cache_creation_input_tokens") or 0)
            return dict(sums, total=sum(sums.values()), source="claude transcript")
        if kind == "codex":
            path = ctx.codex_rollout(str(value) if value else None, home, env)
            if path is None:
                return {"total": None, "reason": "no Codex rollout for {}".format(member)}
            before: Optional[Dict[str, Any]] = None
            last: Optional[Dict[str, Any]] = None
            for rec in _jsonl_window(path, 0.0, end):
                payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
                info = payload.get("info") if payload.get("type") == "token_count" else None
                total = info.get("total_token_usage") if isinstance(info, dict) else None
                if not isinstance(total, dict):
                    continue
                stamp = C._parse_iso(rec.get("timestamp")) or 0.0
                if stamp < start:
                    before = total
                else:
                    last = total
            if last is None:
                return {"total": None, "reason": "no Codex token_count in the window"}
            base = before or {}

            def delta(key: str) -> int:
                return int(last.get(key) or 0) - int(base.get(key) or 0)  # type: ignore[union-attr]

            return {"input": delta("input_tokens"), "output": delta("output_tokens"), "cache": delta("cached_input_tokens"),
                    "total": delta("total_tokens"), "source": "codex rollout"}
        if kind == "opencode":
            db = ctx.opencode_db(home, env)
            if not value or not db.is_file():
                return {"total": None, "reason": "no OpenCode session or database for {}".format(member)}
            conn = ctx.connect_readonly(db)
            try:
                rows = conn.execute("SELECT data, time_created FROM message WHERE session_id = ? AND time_created BETWEEN ? AND ?",
                                    (str(value), int(start * 1000), int(end * 1000))).fetchall()
            finally:
                conn.close()
            sums = {"input": 0, "output": 0, "cache": 0}
            for blob, _created in rows:
                try:
                    data = json.loads(blob)
                except (TypeError, ValueError):
                    continue
                tokens = data.get("tokens") if isinstance(data, dict) and data.get("role") == "assistant" else None
                if not isinstance(tokens, dict):
                    continue
                cache = tokens.get("cache") if isinstance(tokens.get("cache"), dict) else {}
                sums["input"] += int(tokens.get("input") or 0)
                sums["output"] += int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0)
                sums["cache"] += int(cache.get("read") or 0) + int(cache.get("write") or 0)
            return dict(sums, total=sum(sums.values()), source="opencode.db")
    except (OSError, ValueError, sqlite3.Error) as err:
        return {"total": None, "reason": "{}: {}".format(kind, err)}
    return {"total": None, "reason": "no token reader for kind {!r}".format(kind)}


#: The members of ``live --fake`` (the QA team's drawer and peer).
FAKE_MEMBERS = (Q.MEMBER, Q.PEER)


def run_fake(prompts: Sequence[Dict[str, Any]], all_prompts: Sequence[Dict[str, Any]], out: Path,
             progress: Optional[Callable[[str], None]] = None, answers: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> Dict[str, Any]:
    """Live mode end to end in a throwaway team: the fake runner answers each request with its reference."""
    with Q.QaTeam() as qa:
        _qa_artifacts(qa)
        runner = FakeRunner(qa.layout, qa.team, all_prompts, kinds={Q.MEMBER: "claude", Q.PEER: "codex"}, answers=answers)
        results = run_live(qa.layout, qa.team, prompts, all_prompts, FAKE_MEMBERS, runner, out, env=dict(qa.env), progress=progress)
        results["fake"] = True
        results["delivered"] = len(runner.delivered)
    return results


def live_plan(prompts: Sequence[Dict[str, Any]], members: Sequence[str]) -> Dict[str, Any]:
    steps = [{"prompt": p["id"], "member": m} for p in prompts for m in members]
    return {"turns": len(steps), "steps": steps}


def run_live(layout: Any, team: Any, prompts: Sequence[Dict[str, Any]], all_prompts: Sequence[Dict[str, Any]], members: Sequence[str],
             runner: Any, out: Path, timeout: float = 420.0, pictures: bool = True, env: Optional[Dict[str, str]] = None,
             progress: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Each prompt for each member, one at a time: clear, seed, trace on, deliver, wait, score, picture (6.6)."""
    if not members:
        raise BenchError("name at least one --member")
    rows: List[Dict[str, Any]] = []
    doc = store.RosterStore(team).load()
    art = C.artifacts_dir(layout, team, doc)
    names = sorted({n for p in prompts for n in p.get("artifacts") or []})
    if names:
        if art is None:
            raise BenchError("team {} has no artifacts folder (no project folder); the chart prompts need one".format(team.name))
        copy_data(names, Path(art))
    switch = _trace_switch(team)
    try:
        (switch.parent / getattr(C, "ATTEMPTS_FILE", "attempts.jsonl")).unlink()  # a fresh trace per run (it stops at 5 MB)
    except OSError:
        pass
    for prompt in prompts:
        for number, member in enumerate(members):
            row_member = _roster_member(team, member)
            pane = str(row_member.get("pane_id") or "")
            if not pane:
                raise BenchError("{} has no pane".format(member))
            reader = members[(number + 1) % len(members)] if len(members) > 1 else "bench"
            C.clear(layout, team, by="bench")
            seed_as, seed = _seed_ops(prompt, all_prompts)
            if seed:
                author = (C.CanvasAuthor(C.HUMAN, C.KIND_HUMAN, "cli", True, operator=True) if seed_as == "operator"
                          else C.CanvasAuthor(member, C.KIND_MEMBER, "cli", True, agent=row_member.get("kind"), team=team.name))
                C.apply_ops(layout, team, seed, author)
            since = C.current_version(team)
            seed_elements = [dict(el) for el in C.load_scene(team).get("elements") or []]
            switch.parent.mkdir(parents=True, exist_ok=True)
            switch.write_text("bench\n", encoding="utf-8")
            started = time.time()
            try:
                runner.deliver(member, pane, "{} {}".format(prompt["prompt"], DELIVERY_TAIL))
                waited = wait_settled(runner, pane, team, timeout)
                records = read_trace(team, started - 1.0, member)
            finally:
                ended = time.time()
                try:
                    switch.unlink()
                except OSError:
                    pass
            valid_first = first_try(records)
            refused_total = sum(len(r.get("refused") or []) + (1 if r.get("error") else 0) for r in records or [])
            row = {"id": prompt["id"], "mode": "live", "language": "v2", "member": member, "kind": row_member.get("kind"),
                   "category": prompt.get("category"), "audience": prompt.get("audience"), "prompt": prompt.get("prompt")}
            row.update(score(layout, team, dict(prompt.get("expect") or {}), member, reader, since, seed_elements,
                             valid_first_try=valid_first, attempts=len(records or []), refused_total=refused_total))
            if records is None:
                row["trace"] = "no attempt trace in this build (canvas.read_attempts)"
            row["seconds"] = waited["seconds"]
            row["wait"] = waited
            row["tokens"] = member_tokens(layout, team, member, started, ended, env)
            if pictures:
                row["picture"] = _picture(layout, team, reader if reader != "bench" else member, out / "pictures",
                                          "{}-{}".format(prompt["id"], member))
            rows.append(row)
            if progress:
                progress(summary_line(row))
    return {"mode": "live", "rows": rows, "controls": [], "ok": True, "failures": [], "unflagged": []}


# --------------------------------------------------------------------------
# outputs


def summary_line(row: Dict[str, Any]) -> str:
    hard = row.get("hard") or {}
    align = row.get("alignment") or {}
    if row.get("mode") == "control":
        verdict = "FLAGGED" if row.get("flagged") else "unflagged"
    else:
        verdict = "pass" if row.get("pass") else "FAIL"
    head = "{:<22} {:<8} {}".format(str(row.get("id")), str(row.get("member") if row.get("mode") == "live" else row.get("language")), verdict)
    stable = " · stable {}".format("yes" if row["stable"] else "no") if row.get("stable") is not None else ""
    said = list(row["reasons"]) if row.get("reasons") else []
    if row.get("mode") == "control" and not said:
        said = list(row.get("flagged_for") or [])
    reasons = " · " + ",".join(said) if said else ""
    return "{} · first try {} · hard {}/{}/{} · crossings {} · align {:.2f}/{:.2f}{}{}".format(
        head, {True: "yes", False: "no", None: "-"}[row.get("valid_first_try")], hard.get("overlap", 0), hard.get("arrow_through", 0),
        hard.get("label_overflow", 0), total_crossings(row), align.get("drawn", 0.0), align.get("readback", 0.0), stable, reasons)


def _mean(values: Sequence[float]) -> Optional[float]:
    return round(sum(values) / len(values), 3) if values else None


def aggregate(rows: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    n = len(rows)
    tokens = [int(r["tokens"]["total"]) for r in rows if isinstance(r.get("tokens"), dict) and isinstance(r["tokens"].get("total"), int)]
    first = [r for r in rows if r.get("valid_first_try") is not None]
    return {"rows": n, "pass_rate": round(sum(1 for r in rows if r.get("pass")) / float(n), 3) if n else None,
            "first_try_rate": round(sum(1 for r in first if r["valid_first_try"]) / float(len(first)), 3) if first else None,
            "hard_total": sum(sum((r.get("hard") or {}).values()) for r in rows),
            "mean_crossings": _mean([float(total_crossings(r)) for r in rows]),
            "mean_drawn_alignment": _mean([float((r.get("alignment") or {}).get("drawn") or 0) for r in rows]),
            "mean_readback_alignment": _mean([float((r.get("alignment") or {}).get("readback") or 0) for r in rows]),
            "median_seconds": round(statistics.median([float(r.get("seconds") or 0) for r in rows]), 2) if n else None,
            "total_tokens": sum(tokens) if tokens else None}


def _groups(rows: Sequence[Dict[str, Any]]) -> Dict[str, List[Dict[str, Any]]]:
    out: Dict[str, List[Dict[str, Any]]] = {}
    for r in rows:
        key = ("{} ({})".format(r.get("member"), r.get("kind")) if r.get("mode") == "live" else "language {}".format(r.get("language")))
        out.setdefault(key, []).append(r)
    return out


def _cell(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "yes" if value else "no"
    if isinstance(value, float):
        return "{:.2f}".format(value)
    return str(value).replace("|", "\\|")


def summary_markdown(results: Dict[str, Any]) -> str:
    rows = [r for r in results.get("rows") or [] if r.get("mode") in ("offline", "live")]
    lines = ["# Canvas benchmark", "", "Generated {} by `tools/canvas_bench.py`. Hard problems (overlap, arrow through, label overflow) "
             "must be 0; timings are informational.".format(datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")), ""]
    for mode in ("live", "offline"):
        part = [r for r in rows if r.get("mode") == mode]
        if not part:
            continue
        lines += ["## {} runs".format(mode.capitalize()), "",
                  "| prompt | {} | pass | first try | hard o/a/l | crossings | drawn | readback | stable | s | tokens |".format(
                      "member" if mode == "live" else "language"),
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for r in part:
            hard = r.get("hard") or {}
            tokens = (r.get("tokens") or {}).get("total") if isinstance(r.get("tokens"), dict) else None
            lines.append("| {} | {} | {} | {} | {}/{}/{} | {} | {} | {} | {} | {} | {} |".format(
                r.get("id"), r.get("member") if mode == "live" else r.get("language"), _cell(r.get("pass")), _cell(r.get("valid_first_try")),
                hard.get("overlap", 0), hard.get("arrow_through", 0), hard.get("label_overflow", 0), total_crossings(r),
                _cell((r.get("alignment") or {}).get("drawn")), _cell((r.get("alignment") or {}).get("readback")), _cell(r.get("stable")),
                _cell(r.get("seconds")), _cell(tokens)))
        lines += ["", "### Aggregates", "", "| group | rows | pass rate | first try | hard total | mean crossings | drawn | readback | median s | tokens |",
                  "|---|---|---|---|---|---|---|---|---|---|"]
        for key, group in _groups(part).items():
            a = aggregate(group)
            lines.append("| {} | {} | {} | {} | {} | {} | {} | {} | {} | {} |".format(
                key, a["rows"], _cell(a["pass_rate"]), _cell(a["first_try_rate"]), a["hard_total"], _cell(a["mean_crossings"]),
                _cell(a["mean_drawn_alignment"]), _cell(a["mean_readback_alignment"]), _cell(a["median_seconds"]), _cell(a["total_tokens"])))
        lines.append("")
    both = compare_languages(rows)
    if both:
        lines += ["## v2 language vs 0.21 language", "",
                  "Comparison data, not a gate (D15): the same engine draws both; 0.21 drawings are loose shapes with no block readback.", "",
                  "| prompt | hard v2 | hard 0.21 | crossings v2 | crossings 0.21 | readback v2 | readback 0.21 | ops v2 | ops 0.21 "
                  "| bytes v2 | bytes 0.21 |",
                  "|---|---|---|---|---|---|---|---|---|---|---|"]
        for c in both:
            lines.append("| {id} | {hard_v2} | {hard_v1} | {crossings_v2} | {crossings_v1} | {readback_v2:.2f} | {readback_v1:.2f} | {ops_v2} | {ops_v1} | "
                         "{bytes_v2} | {bytes_v1} |".format(**c))
        lines.append("")
    failed = [r for r in rows if not r.get("pass")]
    lines += ["## Failures", ""]
    if not failed:
        lines.append("None.")
    for r in failed:
        detail = []
        rb = r.get("readback") or {}
        if rb.get("missing_labels"):
            detail.append("missing labels {}".format(rb["missing_labels"]))
        if rb.get("missing_relations"):
            detail.append("missing relations {}".format(rb["missing_relations"]))
        if r.get("moved"):
            detail.append("moved {}".format(r["moved"][:3]))
        if r.get("refused"):
            detail.append("refused {}".format(r["refused"][:3]))
        lines.append("- {} ({}): {}{}".format(r.get("id"), r.get("member") if r.get("mode") == "live" else r.get("language"),
                                              ", ".join(r.get("reasons") or ["fail"]), "; " + "; ".join(detail) if detail else ""))
    controls = results.get("controls") or []
    if controls:
        lines += ["", "## Negative controls", "", "| control | flagged | expected | reasons |", "|---|---|---|---|"]
        for c in controls:
            found = list(c.get("reasons") or []) or list(c.get("flagged_for") or [])
            if c.get("crossings_min") is not None and _crossings(total_crossings(c)) not in found:
                found.append(_crossings(total_crossings(c)))
            lines.append("| {} | {} | {} | {} |".format(c.get("id"), _cell(c.get("flagged")),
                                                        ", ".join(c.get("expects") or c.get("expected") or ["-"]),
                                                        ", ".join(found or ["-"])))
    return "\n".join(lines) + "\n"


def compare_languages(rows: Sequence[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Side by side rows for the prompts scored in both languages (offline)."""
    by: Dict[str, Dict[str, Dict[str, Any]]] = {}
    for r in rows:
        if r.get("mode") == "offline":
            by.setdefault(str(r.get("id")), {})[str(r.get("language"))] = r
    out = []
    for pid, pair in sorted(by.items()):
        if "v2" in pair and "v1" in pair:
            v2, v1 = pair["v2"], pair["v1"]
            out.append({"id": pid, "hard_v2": sum((v2.get("hard") or {}).values()), "hard_v1": sum((v1.get("hard") or {}).values()),
                        "crossings_v2": total_crossings(v2), "crossings_v1": total_crossings(v1),
                        "readback_v2": float((v2.get("alignment") or {}).get("readback") or 0),
                        "readback_v1": float((v1.get("alignment") or {}).get("readback") or 0),
                        "ops_v2": v2.get("ops"), "ops_v1": v1.get("ops"), "bytes_v2": v2.get("bytes"), "bytes_v1": v1.get("bytes")})
    return out


def rating_markdown(rows: Sequence[Dict[str, Any]]) -> str:
    lines = ["# Clarity rating", "", "One row per picture. Rate how clearly the board answers the request (1 = unclear, 5 = clear at a glance).", ""]
    pictured = [r for r in rows if r.get("picture")]
    if not pictured:
        lines.append("No pictures in these runs (live runs save them; offline needs --pictures and resvg).")
    for r in pictured:
        lines += ["## {} ({})".format(r.get("id"), r.get("member") if r.get("mode") == "live" else r.get("language")), "",
                  "- Request: {}".format(r.get("prompt") or ""), "- Picture: `{}`".format(r.get("picture")), "- Clarity 1-5:",
                  "- Would you change anything:", ""]
    return "\n".join(lines) + "\n"


def write_outputs(results: Dict[str, Any], out: Path) -> Dict[str, str]:
    out.mkdir(parents=True, exist_ok=True)
    written = {"results": os.fspath(out / "results.json"), "summary": os.fspath(out / "summary.md"), "rating": os.fspath(out / "rating.md")}
    Path(written["results"]).write_text(json.dumps(results, indent=1, ensure_ascii=False, default=str) + "\n", encoding="utf-8")
    Path(written["summary"]).write_text(summary_markdown(results), encoding="utf-8")
    Path(written["rating"]).write_text(rating_markdown(results.get("rows") or []), encoding="utf-8")
    return written


def merge_results(folders: Sequence[Path]) -> Dict[str, Any]:
    rows: List[Dict[str, Any]] = []
    controls: List[Dict[str, Any]] = []
    for folder in folders:
        path = Path(folder) / "results.json" if Path(folder).is_dir() else Path(folder)
        doc = _read_json(path)
        rows.extend(doc.get("rows") or [])
        controls.extend(doc.get("controls") or [])
    return {"mode": "report", "rows": rows, "controls": controls, "ok": True, "failures": [], "unflagged": [],
            "sources": [os.fspath(f) for f in folders]}


# --------------------------------------------------------------------------
# command line


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="canvas_bench.py", description="The canvas benchmark (offline, live, report).")
    parser.add_argument("--check", action="store_true", help="check the fixtures and exit")
    parser.add_argument("--json", action="store_true", help="print JSON")
    sub = parser.add_subparsers(dest="mode")
    off = sub.add_parser("offline", help="score the reference batches and the negative controls (no agents)")
    off.add_argument("--prompts", default="all", help="all, live, or ids (01,04)")
    off.add_argument("--language", default="v2", choices=("v2", "v1", "both"))
    group = off.add_mutually_exclusive_group()
    group.add_argument("--controls", action="store_true", help="only the negative controls")
    group.add_argument("--no-controls", action="store_true", help="skip the negative controls")
    off.add_argument("--pictures", action="store_true", help="save look --image PNGs (needs resvg)")
    off.add_argument("--out", help="write results.json, summary.md and rating.md here")
    off.add_argument("--json", action="store_true", help="print JSON")
    live = sub.add_parser("live", help="drive real agents in the rig (run by the E2E stage only)")
    live.add_argument("--team", required=True)
    live.add_argument("--member", action="append", default=[], help="a member to test (repeat); --fake uses its own")
    live.add_argument("--prompts", default="live")
    live.add_argument("--deliver", default="prompt", choices=("prompt", "post"))
    live.add_argument("--shell-pane")
    live.add_argument("--timeout", type=float, default=420.0)
    live.add_argument("--out", required=True)
    live.add_argument("--dry-run", action="store_true", help="print the plan and the turn count; send nothing")
    live.add_argument("--fake", action="store_true",
                      help="a throwaway team whose members answer with the references (no agents; --team and --member are ignored)")
    live.add_argument("--json", action="store_true", help="print JSON")
    rep = sub.add_parser("report", help="merge runs and write summary.md and rating.md")
    rep.add_argument("dirs", nargs="+")
    rep.add_argument("--out", required=True)
    rep.add_argument("--json", action="store_true", help="print JSON")
    return parser


def main(argv: Optional[Sequence[str]] = None, env: Optional[Dict[str, str]] = None) -> int:
    args = _parser().parse_args(list(argv) if argv is not None else None)
    env = dict(env if env is not None else os.environ)
    try:
        if args.check or args.mode is None:
            found = check_fixtures()
            if args.json:
                print(json.dumps(found, indent=1))
            else:
                for problem in found["problems"]:
                    print("problem: " + problem)
                c = found["counts"]
                print("{} prompts ({} live, {} non-dev, {} with reference_v1, {} from data files), {} controls: {}".format(
                    c["prompts"], c["live"], c["nondev"], c["reference_v1"], c["data_prompts"], c["controls"],
                    "ok" if not found["problems"] else "{} problems".format(len(found["problems"]))))
            return 0 if not found["problems"] else 1
        if args.mode == "offline":
            found = check_fixtures()
            if found["problems"]:
                raise BenchError("the fixtures have problems; run --check ({})".format(found["problems"][0]))
            all_prompts = load_prompts("all")
            prompts = [] if args.controls else load_prompts(args.prompts)
            controls = [] if args.no_controls else load_controls()
            out = Path(args.out) if args.out else None
            pictures = (out or OUT_DIR) / "pictures" if args.pictures else None
            results = run_offline(prompts, all_prompts, args.language, controls, pictures,
                                  progress=None if args.json else print)
            if out is not None:
                results["written"] = write_outputs(results, out)
            if args.json:
                print(json.dumps(results, indent=1, default=str))
            else:
                for failure in results["failures"]:
                    print("gate: {} {}".format(failure["id"], ", ".join(failure["reasons"])))
                for miss in results["unflagged"]:
                    print("control not flagged: {} (missing {})".format(miss["id"], ", ".join(miss["missing"]) or "crossings"))
                v2 = [r for r in results["rows"] if r["language"] == "v2"]
                print("offline: {}/{} v2 references hold the gate, {}/{} controls flagged: {}".format(
                    sum(1 for r in v2 if gate_ok(r)), len(v2), sum(1 for c in results["controls"] if c["flagged"]),
                    len(results["controls"]), "ok" if results["ok"] else "FAIL"))
            return 0 if results["ok"] else 1
        if args.mode == "live":
            all_prompts = load_prompts("all")
            prompts = load_prompts(args.prompts)
            plan = live_plan(prompts, args.member or list(FAKE_MEMBERS))
            if args.dry_run:
                print(json.dumps(plan, indent=1) if args.json else "{} turns: {}".format(
                    plan["turns"], ", ".join("{}/{}".format(s["prompt"], s["member"]) for s in plan["steps"])))
                return 0
            out = Path(args.out)
            if args.fake:
                results = run_fake(prompts, all_prompts, out, progress=None if args.json else print)
            else:
                if not args.member:
                    raise BenchError("name at least one --member")
                guard_rig(env)
                layout = paths.resolve_layout(env)
                team = layout.session.team(args.team)
                runner = HerdrRunner(args.team, args.deliver, args.shell_pane, env)
                results = run_live(layout, team, prompts, all_prompts, args.member, runner, out, args.timeout, env=env,
                                   progress=None if args.json else print)
            results["written"] = write_outputs(results, out)
            print(json.dumps(results, indent=1, default=str) if args.json else "live: {} rows written to {}".format(len(results["rows"]), out))
            return 0
        if args.mode == "report":
            results = merge_results([Path(d) for d in args.dirs])
            results["written"] = write_outputs(results, Path(args.out))
            print(json.dumps(results["written"], indent=1) if args.json else "report: {} rows written to {}".format(len(results["rows"]), args.out))
            return 0
    except (BenchError, HerdrTeamError) as err:
        print("canvas_bench: {}".format(err), file=sys.stderr)
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())
