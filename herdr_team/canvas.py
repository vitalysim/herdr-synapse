"""The team canvas: operations, validation, the scene, events, limits, and what agents read back (0.21).

Contract ``.local/prd/canvas-contracts.md`` sections 4 to 9, 16 and 17;
rationale in ``freestyle-canvas.md``. Agents never draw pixels: they send
Synapse Sketch operations (``shape``, ``arrow``, ``frame``, ``pen``, ``svg``,
``graph``, ``mermaid``, ``chart``, ``viz``, ``comment``, ``claim`` ...), each
with a one-line ``intent``. This module validates them, applies a batch in
order under ``whiteboard/canvas.lock``, appends one event per applied
operation to ``whiteboard/events.jsonl`` (the source of truth) and rewrites
``whiteboard/scene.json`` (the folded scene). The canonical scene belongs to
Synapse, not to the engine: Excalidraw only displays and edits it through the
page's adapter, and the human's edits come back as the same operations.

Authority is set here from the verified origin, never by the operation:
members change their own elements, the manager any agent's, the operator
anything; unverified callers and hooks only read. The operator's locked
regions refuse agent operations; claims are advisory and only warn.

Board records (``canvas_changed``, coalesced to one per author per minute,
and ``canvas_sent`` for a comment's @mentions or the operator's "send to
member") are appended only after the canvas lock is released: ``flock`` is
not re-entrant and ``BoardStore.append`` takes ``team.lock`` itself.

What agents read back is ``look``: a three-level text listing (the region in
full, the rest one line each, far clusters as counts), changes since the
reader last looked, active claims, locks, the legend, comments that mention
the reader, and on request a rendered image with id marks
(``canvas_render``). Graph layout and the Mermaid flowchart subset live in
``canvas_layout`` and ``canvas_mermaid``.
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import secrets
import shutil
import stat
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, Iterator, List, Optional, Sequence, Set, Tuple

from herdr_team import canvas_check as _check
from herdr_team import canvas_layout as _layout
from herdr_team import canvas_mermaid as _mermaid
from herdr_team import canvas_render as _render
from herdr_team import features as _features
from herdr_team import sanitize as _sanitize
from herdr_team import store
from herdr_team.errors import HerdrTeamError, exit_code_for
from herdr_team.paths import FILE_STEM_RE, TeamPaths, check_not_symlink, ensure_dir

SCHEMA = 1
GRID = 20
HOME_W, HOME_H = 800, 600
HOME_STRIDE = 1000
HOME_TOP = -1000
DEFAULT_GAP = 40
AROUND_MARGIN = 200

MAX_ELEMENTS = 2000
MAX_BATCH_OPS = 100
MAX_BATCH_BYTES = 512 * 1024
MAX_OPS_PER_MINUTE = 300
MAX_PEN_POINTS = 500
MAX_ARROW_POINTS = 50
MAX_TEXT_CHARS = 2000
MAX_LABEL_CHARS = 200
MAX_INTENT_CHARS = 200
MAX_COMMENT_CHARS = 1000
MAX_SVG_BYTES = 100 * 1024
MAX_SVG_NODES = 5000
MAX_PATH_CHARS = 20000
MAX_MERMAID_BYTES = 20 * 1024
MAX_CHART_SPEC_BYTES = 200 * 1024
MAX_CHART_DATA_BYTES = 5 * 1024 * 1024
MAX_VIZ_BYTES = 200 * 1024
MAX_VIZ_DATA_BYTES = 64 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_STILL_BYTES = 2 * 1024 * 1024
MAX_EXPORT_BYTES = 8 * 1024 * 1024
MAX_GRAPH_NODES = 200
MAX_GRAPH_EDGES = 400
MAX_CLAIMS_PER_AUTHOR = 3
CLAIM_TTL_S = 300
MAX_LEGEND = 50
MAX_PORTRAIT_STEPS = 12
MAX_COORD = 1_000_000
MAX_SIZE = 20_000
#: A free-text legend symbol ("red cross") and the operator's note on "send to member".
MAX_SYMBOL_CHARS = 80
MAX_SEND_TEXT_CHARS = 1500
MAX_BATCHES_KEPT = 500
#: What one applied op from an agent may append to ``events.jsonl`` (every touched element is written whole).
MAX_OP_CHANGE_BYTES = 2 * 1024 * 1024
#: What one agent author may append to the log in a rolling minute, next to ``MAX_OPS_PER_MINUTE``.
MAX_CHANGE_BYTES_PER_MINUTE = 8 * 1024 * 1024
#: How deep a chart spec may nest (Vega-Lite ``layer``/``concat``); deeper is refused, never skipped.
MAX_SPEC_DEPTH = 64

NOTICE_INTERVAL_S = 60.0
LOOK_FULL_MAX = 60
LOOK_LINE_MAX = 200
LOOK_CHANGES_MAX = 50
MAX_CLUSTERS = 50
RENDER_KEEP = _render.RENDER_KEEP
EXACT_WAIT_S = 5.0
EXACT_POLL_S = 0.1
EXPORT_MAX_AGE_S = 30.0
LOCK_TIMEOUT_S = 5.0
RATE_WINDOW_S = 60.0

ELEMENT_TYPES = ("box", "ellipse", "diamond", "note", "text", "arrow", "frame", "pen", "path", "svg",
                 "mermaid", "chart", "viz", "image", "comment")
OPS = ("shape", "arrow", "frame", "pen", "path", "svg", "graph", "mermaid", "chart", "viz", "image", "comment",
       "claim", "release", "legend", "move", "restyle", "edit", "delete", "portrait", "resolve", "lock", "unlock", "undo")
VIZ_LIBS = ("d3", "three", "p5")
ID_PATTERN = r"^(E|C|K|X|G|B)-([1-9][0-9]{0,6})\Z"
ALIAS_PATTERN = r"^[A-Za-z][A-Za-z0-9_.-]{0,63}\Z"
CELL_PATTERN = r"^c(-?[0-9]{1,5})r(-?[0-9]{1,5})\Z"

COLORS = {"black": "#1e1e1e", "gray": "#868e96", "red": "#e03131", "pink": "#c2255c", "purple": "#9c36b5",
          "blue": "#1971c2", "teal": "#0c8599", "green": "#2f9e44", "orange": "#f08c00", "yellow": "#f59f00",
          "white": "#ffffff"}
FILLS = {"red": "#ffc9c9", "pink": "#fcc2d7", "purple": "#eebefa", "blue": "#a5d8ff", "teal": "#99e9f2",
         "green": "#b2f2bb", "yellow": "#ffec99", "orange": "#ffd8a8", "gray": "#e9ecef", "white": "#ffffff"}
NOTE_FILL = "#ffec99"
AUTHOR_PALETTE = ("#1971c2", "#2f9e44", "#9c36b5", "#f08c00", "#0c8599", "#c2255c", "#e03131", "#5c940d")
#: The light fill that goes with each author colour (a portrait's current step).
AUTHOR_FILLS = ("#a5d8ff", "#b2f2bb", "#eebefa", "#ffd8a8", "#99e9f2", "#fcc2d7", "#ffc9c9", "#d8f5a2")
HUMAN_COLOR = "#1e1e1e"

HUMAN = "human"
KIND_MEMBER = "member"
KIND_HUMAN = "human"
KIND_SYSTEM = "system"
#: The intent of an operator's operation that carries none (agents must always give one).
HUMAN_INTENT = "the operator's edit"
CLI = "herdr-synapse"

SHAPE_KINDS = ("box", "ellipse", "diamond", "note", "text")
TEXT_TYPES = frozenset(SHAPE_KINDS)
SHAPE_SIZES = {"box": (160, 80), "ellipse": (160, 80), "diamond": (160, 100), "note": (160, 100)}
HEADS = ("arrow", "triangle", "dot", "none")
DASHES = ("solid", "dashed", "dotted")
FONTS = ("hand", "normal", "code")
WIDTHS = {"thin": 1, "bold": 2, "extra": 4, "1": 1, "2": 2, "4": 4}
SIZES = {"s": 16, "m": 20, "l": 28, "xl": 36, "16": 16, "20": 20, "28": 28, "36": 36}
TEXT_MAX_W = 600
NODE_W, NODE_H = _layout.NODE_W, _layout.NODE_H
FRAME_PAD, FRAME_TOP = 20, 40
PORTRAIT_W, PORTRAIT_STEP_W, PORTRAIT_STEP_H, PORTRAIT_STEP_GAP = 360, 320, 28, 8
PORTRAIT_ROLE = "portrait"

EVENTS_FILE = "events.jsonl"
SCENE_FILE = "scene.json"
LOCK_FILE = "canvas.lock"
NOTICES_FILE = "notices.json"
RATE_FILE = "rate.json"
CURSORS_DIR = "cursors"
ASSETS_DIR = "assets"
STILLS_DIR = "stills"
RENDERS_DIR = "renders"
EXPORTS_DIR = "exports"
ARCHIVE_DIR = "archive"

_ID_RE = re.compile(ID_PATTERN)
_ALIAS_RE = re.compile(ALIAS_PATTERN)
_CANONICAL_LIKE_RE = re.compile(r"^[A-Z]-[0-9]+\Z")
_CELL_RE = re.compile(CELL_PATTERN)
_XY_RE = re.compile(r"^\s*(-?[0-9]+(?:\.[0-9]+)?)\s*,\s*(-?[0-9]+(?:\.[0-9]+)?)\s*\Z")
_XYXY_RE = re.compile(r"^\s*(-?[0-9]+(?:\.[0-9]+)?)\s*,\s*(-?[0-9]+(?:\.[0-9]+)?)\s*,\s*(-?[0-9]+(?:\.[0-9]+)?)\s*,\s*(-?[0-9]+(?:\.[0-9]+)?)\s*\Z")
_CLIENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,64}\Z")
_NODE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}\Z")
_ASSET_RE = re.compile(r"^[0-9a-f]{32}\.(png|jpg|svg|html|vl\.json)\Z")
_ELEMENT_ID_RE = re.compile(r"^E-[1-9][0-9]{0,6}\Z")
_REQUEST_ID_RE = re.compile(r"^[0-9a-f]{16}\Z")
_MENTION_RE = re.compile(r"(?<![A-Za-z0-9_.@-])@([A-Za-z][A-Za-z0-9_-]{0,63})")

_log = logging.getLogger("herdr_team.canvas")
_log.addHandler(logging.NullHandler())


# --------------------------------------------------------------------------
# small helpers


def _iso(ts: float) -> str:
    moment = datetime.fromtimestamp(ts, timezone.utc)
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + "{:03d}Z".format(moment.microsecond // 1000)


def _parse_iso(value: Any) -> Optional[float]:
    if not isinstance(value, str) or not value:
        return None
    text = value.strip().rstrip("Z")
    for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return None


def _is_number(value: Any) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(float(value))


def _id_number(value: Any) -> int:
    match = _ID_RE.match(str(value or ""))
    return int(match.group(2)) if match else 0


def _round(value: float) -> int:
    return int(math.floor(float(value) + 0.5))


def _r2(value: float) -> float:
    rounded = round(float(value), 2)
    return int(rounded) if rounded == int(rounded) else rounded


def cell_name(x: float, y: float) -> str:
    """``c<col>r<row>`` of the grid cell holding the point ``(x, y)``."""
    return "c{}r{}".format(int(math.floor(float(x) / GRID)), int(math.floor(float(y) / GRID)))


def region_cells(region: Sequence[float]) -> str:
    """``c10r4:c40r22`` for a region's corner points."""
    return "{}:{}".format(cell_name(region[0], region[1]), cell_name(region[2], region[3]))


def bounds(el: Dict[str, Any]) -> Tuple[float, float, float, float]:
    """``(x0, y0, x1, y1)`` of an element."""
    x, y = float(el.get("x") or 0), float(el.get("y") or 0)
    return x, y, x + max(1.0, float(el.get("w") or 1)), y + max(1.0, float(el.get("h") or 1))


def _center(el: Dict[str, Any]) -> Tuple[float, float]:
    x0, y0, x1, y1 = bounds(el)
    return (x0 + x1) / 2.0, (y0 + y1) / 2.0


def _intersects(a: Sequence[float], b: Sequence[float]) -> bool:
    return a[0] < b[2] and b[0] < a[2] and a[1] < b[3] and b[1] < a[3]


def _contains(outer: Sequence[float], inner: Sequence[float]) -> bool:
    return outer[0] <= inner[0] and outer[1] <= inner[1] and inner[2] <= outer[2] and inner[3] <= outer[3]


def _bbox(points: Sequence[Sequence[float]]) -> Tuple[float, float, float, float]:
    xs = [float(p[0]) for p in points]
    ys = [float(p[1]) for p in points]
    return min(xs), min(ys), max(xs), max(ys)


def _who(name: Any, reader: Optional[str]) -> str:
    """``you`` for the reader, ``the operator`` for the human, else the member name."""
    if reader is not None and name == reader:
        return "you"
    return "the operator" if name == HUMAN else str(name)


def _q(text: Any, limit: int = 0) -> str:
    value = " / ".join(str(text or "").split("\n"))
    if limit and len(value) > limit:
        value = value[: limit - 1].rstrip() + "…"
    return '"' + value.replace('"', '\\"') + '"'


def _error(code: str, message: str, **details: Any) -> HerdrTeamError:
    return HerdrTeamError(code, message, exit_code_for(code), details)


def _invalid(field: str, message: str, **details: Any) -> HerdrTeamError:
    return _error("op_invalid", message, field=field, **details)


def _too_big(field: str, limit: str, maximum: int, message: str) -> HerdrTeamError:
    return _error("canvas_limit", message, field=field, limit=limit, max=maximum)


def _team_dir_exists(team: TeamPaths) -> None:
    if not team.root.is_dir():
        raise _error("team_not_found", "team directory {} does not exist".format(team.root), team=team.name)


# --------------------------------------------------------------------------
# who is drawing


@dataclass(frozen=True)
class CanvasAuthor:
    """Who applies a batch: a member (``cli``/``mcp``) or the human (``cli``/``page``); set by the server, never by the op."""

    name: str
    kind: str  # "member" | "human" (| "system": hooks and startup processes, which only read)
    via: str  # "cli" | "mcp" | "page" | "system"
    verified: bool
    agent: Optional[str] = None
    manager: bool = False
    operator: bool = False
    #: The team the identity was resolved in, when known; a member of another team is refused ``not_a_member``.
    team: Optional[str] = None

    @property
    def is_human(self) -> bool:
        return self.kind == KIND_HUMAN

    @property
    def is_member(self) -> bool:
        return self.kind == KIND_MEMBER

    def to_event(self) -> Dict[str, Any]:
        return {"name": self.name, "kind": self.kind, "agent": self.agent, "via": self.via, "verified": bool(self.verified)}


def _agent_rows(doc: Any) -> List[Dict[str, Any]]:
    """The team's agent members that have not left, in roster order."""
    members = doc.get("members") if isinstance(doc, dict) else None
    out = []
    for member in members if isinstance(members, list) else []:
        if not isinstance(member, dict) or member.get("kind") == "human" or member.get("name") == HUMAN:
            continue
        if member.get("status") == "left" or not isinstance(member.get("name"), str):
            continue
        out.append(member)
    return out


def _member_row(doc: Any, name: str) -> Optional[Dict[str, Any]]:
    for member in _agent_rows(doc):
        if member.get("name") == name:
            return member
    return None


def author_from_identity(author: Any, doc: Dict[str, Any], via: str = "cli") -> CanvasAuthor:
    """A ``CanvasAuthor`` from ``identity.Author``: ``operator`` for a trusted human or a delegate, ``manager`` from the roster."""
    name = str(getattr(author, "name", "") or "")
    if getattr(author, "is_system", False) or name == "system":
        return CanvasAuthor("system", KIND_SYSTEM, "system", False)
    if getattr(author, "is_human", False) or name == HUMAN:
        return CanvasAuthor(HUMAN, KIND_HUMAN, via, bool(getattr(author, "verified", False)),
                            operator=bool(getattr(author, "trusted_human", False)))
    team = getattr(author, "team", None)
    row = _member_row(doc, name) if not team or team == doc.get("team") else None
    verified = bool(getattr(author, "verified", False))
    return CanvasAuthor(
        name, KIND_MEMBER, via, verified,
        agent=getattr(author, "kind", None) or (row.get("kind") if row else None),
        manager=bool(row and row.get("manager")),
        operator=bool(verified and getattr(author, "operator", False)),
        team=team if isinstance(team, str) else None,
    )


def page_author(writable: bool) -> CanvasAuthor:
    """The human editing through the page: name ``human``, via ``page``, operator only when the page is writable."""
    return CanvasAuthor(HUMAN, KIND_HUMAN, "page", bool(writable), operator=bool(writable))


def _check_writer(author: CanvasAuthor, doc: Dict[str, Any], team: TeamPaths) -> None:
    """Batch-level: may this author write at all? (Everyone may read.)"""
    if author.kind == KIND_SYSTEM or author.via == "system":
        raise _error("author_mismatch", "hooks and startup processes can read the canvas but not draw on it", author=author.name)
    if author.is_human:
        if author.operator:
            return
        if author.via == "page":
            raise _error("read_only", "this whiteboard page was opened read-only; the operator opens a writable one with: {} whiteboard open".format(CLI))
        raise _error("author_mismatch", "drawing as the operator needs a trusted origin (the console, a focused Herdr pane, "
                     "or outside Herdr); this shell could not be verified", author=author.name, via=author.via)
    if not author.is_member:
        raise _error("author_mismatch", "{} cannot draw on the canvas".format(author.name), author=author.name)
    if not author.verified:
        raise _error("author_unverified", "drawing on the canvas needs a verified member pane; {} could not be verified".format(author.name),
                     author=author.name)
    if _member_row(doc, author.name) is None or (author.team is not None and author.team != team.name):
        raise _error("not_a_member", "{} is not a member of team {}".format(author.name, team.name), author=author.name, team=team.name)


def _may_edit(author: CanvasAuthor, el: Dict[str, Any]) -> bool:
    """Editor: the element's author; the manager for any agent's element; the operator for anything."""
    if author.operator:
        return True
    if el.get("author_kind") == KIND_HUMAN:
        return False
    if author.is_member and el.get("author") == author.name:
        return True
    return bool(author.manager and author.is_member and el.get("author_kind") == KIND_MEMBER)


# --------------------------------------------------------------------------
# the folded scene


def _expired(claim: Dict[str, Any], now: float) -> bool:
    expires = _parse_iso(claim.get("expires_at"))
    return expires is not None and expires <= now


class _State:
    """The scene in memory, keyed by id; ``fold`` applies one event (also used to rebuild from the log)."""

    TARGETS = ("element", "claim", "lock", "legend", "home", "author")

    def __init__(self, team_name: str) -> None:
        self.team = team_name
        self.version = 0
        self.updated_at: Optional[str] = None
        self.elements: Dict[str, Dict[str, Any]] = {}
        self.claims: Dict[str, Dict[str, Any]] = {}
        self.locks: Dict[str, Dict[str, Any]] = {}
        self.legend: Dict[str, Dict[str, Any]] = {}
        self.homes: Dict[str, List[int]] = {}
        self.authors: Dict[str, Dict[str, Any]] = {}
        self.batches: Dict[str, Dict[str, Any]] = {}
        self.counters: Dict[str, int] = {prefix: 0 for prefix in "ECKXGB"}
        #: alias -> {author: element id}, for live elements
        self.aliases: Dict[str, Dict[str, str]] = {}
        self.max_z = 0

    # construction ------------------------------------------------------

    @classmethod
    def from_scene(cls, scene: Dict[str, Any], team_name: str) -> "_State":
        state = cls(team_name)
        state.version = int(scene.get("version") or 0)
        state.updated_at = scene.get("updated_at") if isinstance(scene.get("updated_at"), str) else None
        for el in scene.get("elements") or []:
            if isinstance(el, dict) and isinstance(el.get("id"), str):
                state._set_element(el["id"], el)
        for key, target in (("claims", state.claims), ("locks", state.locks), ("legend", state.legend)):
            for item in scene.get(key) or []:
                if isinstance(item, dict) and isinstance(item.get("id"), str):
                    target[item["id"]] = item
        for key, target in (("homes", state.homes), ("authors", state.authors), ("batches", state.batches)):
            raw = scene.get(key)
            if isinstance(raw, dict):
                target.update(raw)
        counters = scene.get("counters")
        if isinstance(counters, dict):
            for prefix in state.counters:
                if _is_number(counters.get(prefix)):
                    state.counters[prefix] = max(state.counters[prefix], int(counters[prefix]))
        return state

    # folding -----------------------------------------------------------

    def _set_element(self, eid: str, el: Optional[Dict[str, Any]]) -> None:
        old = self.elements.get(eid)
        if old is not None and old.get("alias"):
            owners = self.aliases.get(old["alias"])
            if owners is not None and owners.get(str(old.get("author"))) == eid:
                del owners[str(old.get("author"))]
                if not owners:
                    del self.aliases[old["alias"]]
        if el is None:
            self.elements.pop(eid, None)
            return
        self.elements[eid] = el
        if el.get("alias"):
            self.aliases.setdefault(el["alias"], {})[str(el.get("author"))] = eid
        if _is_number(el.get("z")):
            self.max_z = max(self.max_z, int(el["z"]))

    def _bump(self, identifier: Any) -> None:
        match = _ID_RE.match(str(identifier or ""))
        if match:
            prefix, number = match.group(1), int(match.group(2))
            self.counters[prefix] = max(self.counters.get(prefix, 0), number)

    def apply_change(self, change: Dict[str, Any]) -> None:
        target = change.get("target")
        identifier = change.get("id")
        if target not in self.TARGETS or not isinstance(identifier, str):
            return
        value = None if change.get("action") == "delete" else change.get("value")
        if target == "element":
            self._set_element(identifier, value if isinstance(value, dict) else None)
        else:
            store_map = {"claim": self.claims, "lock": self.locks, "legend": self.legend,
                         "home": self.homes, "author": self.authors}[target]
            if value is None:
                store_map.pop(identifier, None)
            else:
                store_map[identifier] = value
        self._bump(identifier)

    def fold(self, event: Dict[str, Any]) -> None:
        seq = int(event.get("seq") or 0)
        ts = event.get("ts") if isinstance(event.get("ts"), str) else None
        if event.get("op") == "clear":
            counters = dict(self.counters)
            carried = event.get("counters")
            if isinstance(carried, dict):
                for prefix in counters:
                    if _is_number(carried.get(prefix)):
                        counters[prefix] = max(counters[prefix], int(carried[prefix]))
            fresh = _State(self.team)
            fresh.counters = counters
            self.__dict__.update(fresh.__dict__)
            self.version = max(self.version, seq)
            self.updated_at = ts
            return
        for change in event.get("changes") or []:
            if isinstance(change, dict):
                self.apply_change(change)
        batch = event.get("batch")
        if isinstance(batch, str) and _ID_RE.match(batch):
            author = event.get("author") if isinstance(event.get("author"), dict) else {}
            entry = self.batches.get(batch)
            if entry is None:
                entry = {"author": author.get("name"), "first_seq": seq, "last_seq": seq, "at": ts, "undone": False}
            entry = dict(entry, last_seq=max(int(entry.get("last_seq") or seq), seq))
            self.batches[batch] = entry
            self._bump(batch)
        undoes = event.get("undoes")
        if isinstance(undoes, str) and undoes in self.batches:
            self.batches[undoes] = dict(self.batches[undoes], undone=True)
        self.version = max(self.version, seq)
        self.updated_at = ts

    # reading -----------------------------------------------------------

    def drop_expired(self, now: float) -> None:
        for cid in [cid for cid, claim in self.claims.items() if _expired(claim, now)]:
            del self.claims[cid]

    def active_claims(self, now: float) -> List[Dict[str, Any]]:
        return sorted((c for c in self.claims.values() if not _expired(c, now)), key=lambda c: _id_number(c.get("id")))

    def to_scene(self, now: Optional[float] = None) -> Dict[str, Any]:
        moment = time.time() if now is None else now
        elements = sorted(self.elements.values(), key=lambda e: (int(e.get("z") or 0), _id_number(e.get("id"))))
        batches = sorted(self.batches.items(), key=lambda item: int(item[1].get("first_seq") or 0))[-MAX_BATCHES_KEPT:]
        return {
            "v": SCHEMA, "team": self.team, "version": self.version, "updated_at": self.updated_at,
            "elements": elements,
            "claims": self.active_claims(moment),
            "locks": sorted(self.locks.values(), key=lambda item: _id_number(item.get("id"))),
            "legend": sorted(self.legend.values(), key=lambda item: _id_number(item.get("id"))),
            "homes": dict(self.homes),
            "authors": dict(self.authors),
            "batches": dict(batches),
            "counters": dict(self.counters),
        }


# --------------------------------------------------------------------------
# storage


def _dir(team: TeamPaths) -> Path:
    return _features.whiteboard_dir(team)


def _file(team: TeamPaths, name: str) -> Path:
    return _dir(team) / name


def _canvas_lock(team: TeamPaths, timeout: float = LOCK_TIMEOUT_S) -> store.FileLock:
    """``whiteboard/canvas.lock``; a timeout is ``canvas_busy`` (exit 5). Never resurrects a dissolved team."""
    _team_dir_exists(team)
    return store.FileLock(_file(team, LOCK_FILE), timeout=timeout, code="canvas_busy")


def _parse_event(line: bytes) -> Optional[Dict[str, Any]]:
    line = line.strip()
    if not line:
        return None
    try:
        obj = json.loads(line.decode("utf-8", "replace"))
    except ValueError:
        return None
    if isinstance(obj, dict) and _is_number(obj.get("seq")) and isinstance(obj.get("seq"), int):
        return obj
    return None


def _read_events(team: TeamPaths) -> List[Dict[str, Any]]:
    """Every event in the log, oldest first (torn or foreign lines skipped)."""
    raw = store.read_bytes(_file(team, EVENTS_FILE))
    if not raw:
        return []
    out = []
    for line in raw.split(b"\n"):
        event = _parse_event(line)
        if event is not None:
            out.append(event)
    return out


def _events_backwards(team: TeamPaths, chunk: int = 65536) -> Iterator[Dict[str, Any]]:
    """Events newest first, reading the log from its end in chunks (cheap for recent versions)."""
    path = _file(team, EVENTS_FILE)
    try:
        fd = store.secure_open(path, os.O_RDONLY)
    except FileNotFoundError:
        return
    try:
        pos = os.fstat(fd).st_size
        # The pieces of the line cut at ``pos``, newest first. They are joined once, when a
        # newline shows where that line starts: re-joining the tail on every chunk made one
        # multi-megabyte line cost quadratic time on every version check.
        carry: List[bytes] = []
        while pos > 0:
            start = max(0, pos - chunk)
            block = os.pread(fd, pos - start, start)
            pos = start
            if pos > 0 and b"\n" not in block:
                carry.append(block)
                continue
            parts = (block + b"".join(reversed(carry))).split(b"\n")
            # parts[0] may be the cut end of a line that started before ``pos``.
            carry = [parts[0]] if pos > 0 else []
            for part in reversed(parts if pos == 0 else parts[1:]):
                event = _parse_event(part)
                if event is not None:
                    yield event
    finally:
        os.close(fd)


def _last_seq(team: TeamPaths) -> int:
    for event in _events_backwards(team):
        return int(event["seq"])
    return 0


def _load_state(team: TeamPaths) -> _State:
    """``scene.json`` when it is current, else the scene folded from ``events.jsonl``."""
    scene = store.read_json(_file(team, SCENE_FILE), default=None)
    last = _last_seq(team)
    if isinstance(scene, dict) and scene.get("v") == SCHEMA and _is_number(scene.get("version")):
        if int(scene["version"]) == last or (last == 0 and not _file(team, EVENTS_FILE).exists()):
            return _State.from_scene(scene, team.name)
    state = _State(team.name)
    for event in _read_events(team):
        state.fold(event)
    return state


def _write_scene(team: TeamPaths, state: _State, now: float) -> None:
    store.write_json(_file(team, SCENE_FILE), state.to_scene(now))


def _event_line(event: Dict[str, Any]) -> bytes:
    return json.dumps(event, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _append_events(team: TeamPaths, events: List[Dict[str, Any]], lines: Optional[List[bytes]] = None) -> None:
    """One line per event; ``lines`` when the caller already serialised them (``apply_ops`` measures each)."""
    encoded = lines if lines is not None else [_event_line(e) for e in events]
    store.append_line(_file(team, EVENTS_FILE), b"\n".join(encoded))


def load_scene(team: TeamPaths) -> Dict[str, Any]:
    """The folded scene (contract 5.7), rebuilt from ``events.jsonl`` when ``scene.json`` is missing or behind."""
    return _load_state(team).to_scene(time.time())


def current_version(team: TeamPaths) -> int:
    """The canvas version: the seq of the last event, 0 for an empty canvas."""
    last = _last_seq(team)
    if last:
        return last
    scene = store.read_json(_file(team, SCENE_FILE), default=None)
    return int(scene.get("version") or 0) if isinstance(scene, dict) and _is_number(scene.get("version")) else 0


def changes_since(team: TeamPaths, version: int, limit: int = 500) -> Dict[str, Any]:
    """``{"version", "since", "events", "complete", "reset"}`` for events after ``version``."""
    if not _is_number(version) or int(version) < 0:
        raise _error("usage", "since must be a version number >= 0", since=version)
    version = int(version)
    current = current_version(team)
    if version > current:
        return {"version": current, "since": version, "events": [], "complete": True, "reset": True}
    collected: List[Dict[str, Any]] = []
    for event in _events_backwards(team):
        if int(event["seq"]) <= version:
            break
        collected.append(event)
    collected.reverse()
    reset = any(event.get("op") == "clear" for event in collected)
    limit = max(1, int(limit))
    return {"version": current, "since": version, "events": collected[:limit], "complete": len(collected) <= limit, "reset": reset}


# --------------------------------------------------------------------------
# validation: text, numbers, style


def _find_secret(text: str) -> Optional[str]:
    from herdr_team.cmd_board import find_secret  # the board's own patterns; imported lazily (heavy module)

    return find_secret(text)


def _guard_code(text: str, field: str) -> None:
    """Secrets refuse source-like fields too (svg, html, mermaid, a chart spec); markers do not apply to code."""
    hit = _find_secret(text)
    if hit:
        raise _error("secret_detected", "{} looks like it contains a secret ({}); never put credentials on the canvas".format(field, hit),
                     field=field, pattern=hit)


def _text(value: Any, field: str, limit: int, limit_name: str, one_line: bool = False, required: bool = False) -> str:
    """Board cleaning for a text field: controls stripped, markers refused (exit 4), secrets refused, length checked."""
    if value is None:
        value = ""
    if _is_number(value):
        value = str(value)
    if not isinstance(value, str):
        raise _invalid(field, "{} must be text".format(field))
    try:
        text = _sanitize.sanitize_text(value, 10 * limit + 1000)
    except HerdrTeamError as err:
        if err.code == "text_too_long":
            raise _too_big(field, limit_name, limit, "{} is longer than {} characters".format(field, limit))
        raise
    text = " ".join(text.split()) if one_line else text.strip()
    if len(text) > limit:
        raise _too_big(field, limit_name, limit, "{} is {} characters; the limit is {}".format(field, len(text), limit))
    if _sanitize.is_marker_text(text):
        raise _error("echo_rejected", "{} starts with [herdr-team or carries a [n<digits>] nonce; that is a nudge echo".format(field), field=field)
    hit = _find_secret(text) if text else None
    if hit:
        raise _error("secret_detected", "{} looks like it contains a secret ({}); never put credentials on the canvas".format(field, hit),
                     field=field, pattern=hit)
    if required and not text:
        raise _invalid(field, "{} is required".format(field))
    return text


def _intent(op: Dict[str, Any], author: CanvasAuthor) -> str:
    raw = op.get("intent")
    if (raw is None or (isinstance(raw, str) and not raw.strip())) and author.is_human:
        return HUMAN_INTENT
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        raise _invalid("intent", "every operation from an agent carries an intent: one line saying why")
    return _text(raw, "intent", MAX_INTENT_CHARS, "MAX_INTENT_CHARS", one_line=True, required=True)


def _num(value: Any, field: str, low: float, high: float) -> float:
    if isinstance(value, str):
        try:
            value = float(value.strip())
        except ValueError:
            raise _invalid(field, "{} must be a number".format(field))
    if not _is_number(value):
        raise _invalid(field, "{} must be a number".format(field))
    number = float(value)
    if number < low or number > high:
        raise _invalid(field, "{} must be between {:g} and {:g}".format(field, low, high))
    return number


def _coord(value: Any, field: str) -> float:
    return _num(value, field, -MAX_COORD, MAX_COORD)


def _size(op: Dict[str, Any], default_w: float, default_h: float) -> Tuple[float, float]:
    w = _num(op["w"], "w", 1, MAX_SIZE) if op.get("w") is not None else float(default_w)
    h = _num(op["h"], "h", 1, MAX_SIZE) if op.get("h") is not None else float(default_h)
    return float(max(1, min(MAX_SIZE, _round(w)))), float(max(1, min(MAX_SIZE, _round(h))))


def _bool(value: Any, field: str, default: bool) -> bool:
    if value is None:
        return default
    if isinstance(value, bool):
        return value
    if value in (0, 1) and not isinstance(value, float):
        return bool(value)
    if isinstance(value, str) and value.lower() in ("true", "false"):
        return value.lower() == "true"
    raise _invalid(field, "{} must be true or false".format(field))


def _choice(value: Any, field: str, choices: Sequence[str], default: str) -> str:
    if value is None:
        return default
    if isinstance(value, str) and value in choices:
        return value
    raise _invalid(field, "{} must be one of: {}".format(field, ", ".join(choices)))


def _hex(value: str) -> Optional[str]:
    text = value.strip().lower()
    return text if re.match(r"^#[0-9a-f]{6}\Z", text) else None


def _stroke_color(value: Any) -> str:
    if isinstance(value, str):
        named = COLORS.get(value.strip().lower())
        if named:
            return named
        found = _hex(value)
        if found:
            return found
    raise _invalid("color", "color must be one of {} or #rrggbb".format(", ".join(COLORS)))


def _fill_color(value: Any) -> Optional[str]:
    if isinstance(value, str):
        text = value.strip().lower()
        if text == "none":
            return None
        if text in FILLS:
            return FILLS[text]
        found = _hex(text)
        if found:
            return found
    raise _invalid("fill", "fill must be one of {}, none, or #rrggbb".format(", ".join(FILLS)))


STYLE_FIELDS = ("color", "fill", "width", "dash", "opacity", "font", "size", "rough")


def _default_style(color: str, kind: str) -> Dict[str, Any]:
    return {"stroke": color, "fill": NOTE_FILL if kind == "note" else None, "width": 2, "dash": "solid",
            "opacity": 100, "font": "hand", "size": 20, "rough": 1}


def _style(op: Dict[str, Any], base: Dict[str, Any]) -> Dict[str, Any]:
    """The element ``style`` from an op's style inputs over ``base``."""
    style = dict(base)
    if op.get("color") is not None:
        style["stroke"] = _stroke_color(op["color"])
    if "fill" in op:
        style["fill"] = None if op["fill"] is None else _fill_color(op["fill"])
    if op.get("width") is not None:
        raw_width = op["width"]
        key = str(int(raw_width)) if _is_number(raw_width) and float(raw_width) == int(raw_width) else str(raw_width).strip().lower()
        width = WIDTHS.get(key)
        if width is None:
            raise _invalid("width", "width must be 1, 2, 4, thin, bold or extra")
        style["width"] = width
    if op.get("dash") is not None:
        style["dash"] = _choice(op["dash"], "dash", DASHES, "solid")
    if op.get("opacity") is not None:
        style["opacity"] = _round(_num(op["opacity"], "opacity", 10, 100))
    if op.get("font") is not None:
        style["font"] = _choice(op["font"], "font", FONTS, "hand")
    if op.get("size") is not None:
        raw = op["size"]
        size = SIZES.get(str(int(raw)) if _is_number(raw) and float(raw) == int(raw) else str(raw).strip().lower())
        if size is None:
            raise _invalid("size", "size must be s, m, l, xl or 16, 20, 28, 36")
        style["size"] = size
    if op.get("rough") is not None:
        rough = op["rough"]
        if not _is_number(rough) or int(rough) not in (0, 1, 2) or float(rough) != int(rough):
            raise _invalid("rough", "rough must be 0, 1 or 2")
        style["rough"] = int(rough)
    return style


def measure_text(text: str, size: float, max_w: float = TEXT_MAX_W) -> Tuple[float, float]:
    """A text element's size: its lines word-wrapped at ``max_w`` the way the renderer wraps them,
    ``CHAR_W``·size per character and 1.25·size per line; the width is the widest line's."""
    lines = _render.wrap_text(text or " ", _render.chars_per_line(max_w, size))
    widest = max(len(line) for line in lines) * _render.CHAR_W * size
    # round before ceil: 5 x 0.55 x 28 is 77.00000000000001 in floating point
    return float(max(_round(size), int(math.ceil(round(widest, 6))))), float(int(math.ceil(round(len(lines) * 1.25 * size, 6))))


def text_size(text: str, size: float, wrap_w: Optional[float] = None) -> Tuple[float, float]:
    """``(w, h)`` of a text element. Its height always follows its lines; its width is the
    widest line (up to ``TEXT_MAX_W``), or ``wrap_w`` when the text wraps at a set width."""
    if wrap_w is None:
        return measure_text(text, size)
    return float(max(1, _round(wrap_w))), measure_text(text, size, wrap_w)[1]


def _text_fields(el: Dict[str, Any], text: str, style: Dict[str, Any], wrap_w: Optional[float] = None) -> Dict[str, Any]:
    """A text element's ``w``/``h``/``wrap`` after its text, size or wrap width changed."""
    if wrap_w is None and el.get("wrap"):
        wrap_w = float(el.get("w") or 1)
    w, h = text_size(text, float(style.get("size") or 20), wrap_w)
    return {"w": max(1, _round(w)), "h": max(1, _round(h)), "wrap": wrap_w is not None}


# --------------------------------------------------------------------------
# points, regions, references

Lookup = Callable[[str, str], Dict[str, Any]]


def _lookup_in(state: _State, ref: Any, author: Optional[str], field: str) -> Dict[str, Any]:
    """Resolve an element reference: canonical id, the author's own alias, or an alias exactly one other author uses."""
    if not isinstance(ref, str) or not ref.strip():
        raise _invalid(field, "{} must name an element (an id like E-3 or an alias)".format(field))
    ref = ref.strip()
    if _ID_RE.match(ref):
        el = state.elements.get(ref)
        if el is None:
            raise _error("element_unknown", "{} is not on the canvas (deleted, cleared, or never drawn)".format(ref), ref=ref, field=field)
        return el
    if not _ALIAS_RE.match(ref) or _CELL_RE.match(ref) or _CANONICAL_LIKE_RE.match(ref):
        raise _invalid(field, "{!r} is not an element id, alias, cell or point".format(ref[:80]))
    owners = state.aliases.get(ref) or {}
    if author is not None and author in owners:
        return state.elements[owners[author]]
    if len(owners) == 1:
        return state.elements[next(iter(owners.values()))]
    if len(owners) > 1:
        candidates = [{"id": eid, "author": name} for name, eid in sorted(owners.items())]
        raise _error("element_unknown", "{} is ambiguous: {}".format(ref, ", ".join("{} by {}".format(c["id"], c["author"]) for c in candidates)),
                     ref=ref, field=field, candidates=candidates)
    raise _error("element_unknown", "no element is called {}".format(ref), ref=ref, field=field)


def _is_point_form(value: Any) -> bool:
    if isinstance(value, (list, tuple)):
        return True
    return isinstance(value, str) and bool(_CELL_RE.match(value.strip()) or _XY_RE.match(value))


def _point(value: Any, field: str, lookup: Optional[Lookup] = None, pressure: bool = False) -> List[float]:
    if isinstance(value, (list, tuple)):
        if len(value) not in ((2, 3) if pressure else (2,)):
            raise _invalid(field, "a point is [x, y]{}".format(" or [x, y, pressure]" if pressure else ""))
        point = [_r2(_coord(value[0], field)), _r2(_coord(value[1], field))]
        if len(value) == 3:
            point.append(_r2(_num(value[2], field, 0, 1)))
        return point
    if isinstance(value, str):
        text = value.strip()
        match = _CELL_RE.match(text)
        if match:
            return [int(match.group(1)) * GRID, int(match.group(2)) * GRID]
        match = _XY_RE.match(text)
        if match:
            return [_r2(_coord(match.group(1), field)), _r2(_coord(match.group(2), field))]
        if lookup is not None:
            cx, cy = _center(lookup(text, field))
            return [_r2(cx), _r2(cy)]
    raise _invalid(field, "{} must be a cell (c17r6), \"x,y\", [x, y]{}".format(field, " or an element" if lookup else ""))


def _region(value: Any, field: str, lookup: Optional[Lookup] = None) -> List[int]:
    if isinstance(value, (list, tuple)) and len(value) == 4:
        x0, y0, x1, y1 = (_coord(v, field) for v in value)
    elif isinstance(value, str) and ":" in value and value.count(":") == 1:
        a, b = value.split(":")
        (x0, y0), (x1, y1) = _point(a.strip(), field)[:2], _point(b.strip(), field)[:2]
    elif isinstance(value, str) and _XYXY_RE.match(value):
        x0, y0, x1, y1 = (_coord(v, field) for v in _XYXY_RE.match(value).groups())  # type: ignore[union-attr]
    elif isinstance(value, str) and lookup is not None:
        x0, y0, x1, y1 = bounds(lookup(value.strip(), field))
    else:
        raise _invalid(field, "{} must be \"c10r4:c40r22\", \"x0,y0,x1,y1\", [x0, y0, x1, y1]{}".format(field, " or an element" if lookup else ""))
    box = [_round(min(x0, x1)), _round(min(y0, y1)), _round(max(x0, x1)), _round(max(y0, y1))]
    if box[2] - box[0] < 1 or box[3] - box[1] < 1:
        raise _invalid(field, "{} must be at least 1 unit wide and tall".format(field))
    return box


def _scene_lookup(scene: Optional[Dict[str, Any]], author: Optional[str]) -> Optional[Lookup]:
    if not isinstance(scene, dict):
        return None
    state = _State.from_scene(scene, str(scene.get("team") or ""))
    return lambda ref, field: _lookup_in(state, ref, author, field)


def parse_point(value: Any, scene: Optional[Dict[str, Any]] = None, author: Optional[str] = None) -> List[float]:
    """A cell name, ``"x,y"``, ``[x, y]`` or an element ref (its centre) -> ``[x, y]``; ``op_invalid``/``element_unknown`` otherwise."""
    return [float(v) for v in _point(value, "point", _scene_lookup(scene, author))[:2]]


def parse_region(value: Any, scene: Optional[Dict[str, Any]] = None, author: Optional[str] = None) -> List[float]:
    """``"c1r2:c3r4"``, ``"x0,y0,x1,y1"``, a list, or an element ref (its bounds) -> normalised ``[x0, y0, x1, y1]``."""
    return [float(v) for v in _region(value, "region", _scene_lookup(scene, author))]


# --------------------------------------------------------------------------
# one operation's working set


class _Ctx:
    """What one batch knows while it applies: the state, the author, and the current op's pending changes."""

    def __init__(self, layout: Any, team: TeamPaths, author: CanvasAuthor, doc: Dict[str, Any], state: _State, now: float, batch_id: str) -> None:
        self.layout = layout
        self.team = team
        self.author = author
        self.doc = doc
        self.state = state
        self.now = now
        self.ts = _iso(now)
        self.batch_id = batch_id
        #: Graph layouts ``apply_ops`` computed before taking ``canvas.lock`` (``_layout_key`` -> positions).
        self.layouts: Dict[Tuple[Any, ...], Dict[str, Tuple[float, float]]] = {}
        self.begin(0, "")

    def begin(self, index: int, op_name: str) -> None:
        self.index = index
        self.op_name = op_name
        self.seq = self.state.version + 1
        self.intent = ""
        self.pending: Dict[str, Optional[Dict[str, Any]]] = {}
        self.others: List[Dict[str, Any]] = []
        self.created: List[str] = []
        self.changed: List[str] = []
        self.warnings: List[Dict[str, Any]] = []
        self.aliases: Dict[str, str] = {}
        self.alias: Optional[str] = None
        self.counters: Dict[str, int] = {}
        self.extra: Dict[str, Any] = {}
        self.mention: Optional[Dict[str, Any]] = None
        self.new_author: Optional[Dict[str, Any]] = None
        self.z = self.state.max_z

    # ids, z, lookups ----------------------------------------------------

    def new_id(self, prefix: str) -> str:
        number = max(self.state.counters.get(prefix, 0), self.counters.get(prefix, 0)) + 1
        self.counters[prefix] = number
        return "{}-{}".format(prefix, number)

    def next_z(self) -> int:
        self.z += 1
        return self.z

    def el(self, eid: str) -> Optional[Dict[str, Any]]:
        return self.pending[eid] if eid in self.pending else self.state.elements.get(eid)

    def live(self) -> Iterable[Dict[str, Any]]:
        """Every live element as this op would leave it."""
        for eid, el in self.state.elements.items():
            if eid in self.pending:
                continue
            yield el
        for el in self.pending.values():
            if el is not None:
                yield el

    def lookup(self, ref: Any, field: str) -> Dict[str, Any]:
        el = _lookup_in(self.state, ref, self.author.name, field)
        current = self.el(el["id"])
        if current is None:
            raise _error("element_unknown", "{} was deleted by this operation".format(el["id"]), ref=ref, field=field)
        if isinstance(ref, str) and not _ID_RE.match(ref.strip()):
            self.aliases[ref.strip()] = el["id"]
        return current

    # recording ----------------------------------------------------------

    def put(self, el: Dict[str, Any]) -> Dict[str, Any]:
        eid = el["id"]
        if eid not in self.pending and eid not in self.state.elements:
            if eid not in self.created:
                self.created.append(eid)
        elif eid not in self.created and eid not in self.changed:
            self.changed.append(eid)
        self.pending[eid] = el
        if el.get("alias"):
            self.aliases[el["alias"]] = eid
        if el.get("client_id"):
            self.aliases[el["client_id"]] = eid
        return el

    def update(self, el: Dict[str, Any], **fields: Any) -> Dict[str, Any]:
        new = dict(el)
        new.update(fields)
        new["updated_seq"] = self.seq
        new["updated_at"] = self.ts
        return self.put(new)

    def drop(self, eid: str) -> None:
        if eid not in self.changed and eid not in self.created:
            self.changed.append(eid)
        self.pending[eid] = None

    def other(self, target: str, action: str, identifier: str, value: Any) -> None:
        self.others.append({"target": target, "action": action, "id": identifier, "value": value})

    def warn(self, code: str, message: str, ids: Sequence[str]) -> None:
        self.warnings.append({"index": self.index, "code": code, "message": message, "ids": list(ids)})

    def changes(self) -> List[Dict[str, Any]]:
        first = [c for c in self.others if c["target"] in ("author", "home")]
        rest = [c for c in self.others if c["target"] not in ("author", "home")]
        elements = []
        for eid, value in self.pending.items():
            if value is None:
                if eid in self.state.elements:
                    elements.append({"target": "element", "action": "delete", "id": eid, "value": None})
                continue
            action = "update" if eid in self.state.elements else "add"
            elements.append({"target": "element", "action": action, "id": eid, "value": value})
        return first + elements + rest

    def live_delta(self) -> int:
        delta = 0
        for eid, value in self.pending.items():
            if value is None and eid in self.state.elements:
                delta -= 1
            elif value is not None and eid not in self.state.elements:
                delta += 1
        return delta

    # the author ----------------------------------------------------------

    def author_color(self) -> str:
        """The author's colour; registers the author (and a member's home) on its first applied op."""
        name = self.author.name
        info = self.state.authors.get(name) or self.new_author
        if info:
            return str(info.get("color") or HUMAN_COLOR)
        if self.author.is_human:
            info = {"color": HUMAN_COLOR, "index": -1, "kind": KIND_HUMAN, "agent": None}
        else:
            index = sum(1 for entry in self.state.authors.values() if isinstance(entry, dict) and entry.get("kind") == KIND_MEMBER)
            info = {"color": AUTHOR_PALETTE[index % len(AUTHOR_PALETTE)], "index": index, "kind": KIND_MEMBER, "agent": self.author.agent}
            if name not in self.state.homes:
                self.other("home", "add", name, [index * HOME_STRIDE, HOME_TOP, index * HOME_STRIDE + HOME_W, HOME_TOP + HOME_H])
        self.other("author", "add", name, info)
        self.new_author = info
        return str(info["color"])

    def author_fill(self) -> str:
        info = self.state.authors.get(self.author.name) or self.new_author or {}
        index = info.get("index") if isinstance(info, dict) else None
        return AUTHOR_FILLS[index % len(AUTHOR_FILLS)] if isinstance(index, int) and index >= 0 else FILLS["gray"]

    def home(self) -> Optional[List[int]]:
        home = self.state.homes.get(self.author.name)
        if home is None:
            for change in self.others:
                if change["target"] == "home" and change["id"] == self.author.name:
                    home = change["value"]
        return list(home) if isinstance(home, list) and len(home) == 4 else None

    # building elements ---------------------------------------------------

    def element(self, kind: str, x: float, y: float, w: float, h: float, text: str = "", style: Optional[Dict[str, Any]] = None,
                alias: Optional[str] = None, client_id: Optional[str] = None, frame: Optional[str] = None,
                group: Optional[str] = None, role: Optional[str] = None, eid: Optional[str] = None, **fields: Any) -> Dict[str, Any]:
        """A new element with the common fields in contract order, then the type's own fields."""
        el: Dict[str, Any] = {
            "id": eid or self.new_id("C" if kind == "comment" else "E"), "type": kind, "alias": alias, "client_id": client_id,
            "x": _round(x), "y": _round(y), "w": max(1, _round(w)), "h": max(1, _round(h)),
            "text": text or "", "style": style or _default_style(self.author_color(), kind),
            "frame": frame, "group": group, "role": role, "z": self.next_z(),
            "author": self.author.name, "author_kind": KIND_HUMAN if self.author.is_human else KIND_MEMBER,
            "intent": self.intent, "batch": self.batch_id, "created_seq": self.seq, "updated_seq": self.seq,
            "created_at": self.ts, "updated_at": self.ts,
        }
        el.update(fields)
        for key in ("x", "y"):
            if abs(el[key]) > MAX_COORD:
                raise _invalid(key, "{} is outside the canvas (|{}| <= {})".format(key, key, MAX_COORD))
        return el

    def event(self, author: CanvasAuthor) -> Dict[str, Any]:
        event: Dict[str, Any] = {
            "v": SCHEMA, "seq": self.seq, "ts": self.ts, "batch": self.batch_id, "author": author.to_event(),
            "op": self.op_name, "index": self.index, "intent": self.intent, "ids": self.created + self.changed,
            "changes": self.changes(),
        }
        event.update(self.extra)
        return event


# --------------------------------------------------------------------------
# checks shared by the operations


def _alias(ctx: _Ctx, op: Dict[str, Any], field: str = "id", value: Any = None) -> Optional[str]:
    raw = op.get(field) if value is None else value
    if raw is None:
        return None
    if not isinstance(raw, str) or not _ALIAS_RE.match(raw) or _CANONICAL_LIKE_RE.match(raw) or _CELL_RE.match(raw):
        raise _invalid(field, "an alias is a letter then up to 63 letters, digits, . _ or -, and cannot look like an id (E-3) or a cell (c3r4)")
    owners = ctx.state.aliases.get(raw) or {}
    existing = owners.get(ctx.author.name)
    if (existing is not None and ctx.el(existing) is not None) or any(
            el is not None and el.get("alias") == raw and el.get("author") == ctx.author.name for el in ctx.pending.values()):
        raise _error("alias_taken", "{} already names your {}; use move, restyle or edit to change it".format(raw, existing or "element"),
                     alias=raw, id=existing)
    return raw


def _client_id(op: Dict[str, Any]) -> Optional[str]:
    raw = op.get("client_id")
    if raw is None:
        return None
    if not isinstance(raw, str) or not _CLIENT_ID_RE.match(raw):
        raise _invalid("client_id", "client_id is 1 to 64 letters, digits, _ or -")
    return raw


def _check_fields(op: Dict[str, Any], allowed: Sequence[str]) -> None:
    for key in op:
        if key not in allowed:
            raise _invalid(str(key), "{} does not take {!r}; it takes: {}".format(op.get("op"), key, ", ".join(k for k in allowed if k != "op")))


def _lock_by(lock: Dict[str, Any]) -> str:
    return "the operator" if lock.get("by") == HUMAN else str(lock.get("by") or "the operator")


def _check_locks(ctx: _Ctx, boxes: Iterable[Sequence[float]]) -> None:
    """Refuse ``canvas_locked`` when anyone but the operator touches a locked region."""
    if ctx.author.operator or not ctx.state.locks:
        return
    for box in boxes:
        for lock in ctx.state.locks.values():
            region = lock.get("region")
            if isinstance(region, list) and len(region) == 4 and _intersects(box, region):
                raise _error("canvas_locked", "{} is inside {} locked by {}".format(cell_name(box[0], box[1]), lock.get("id"), _lock_by(lock)),
                             lock=lock.get("id"))


def _warn_claims(ctx: _Ctx, ids: Sequence[str], box: Sequence[float]) -> None:
    for claim in ctx.state.active_claims(ctx.now):
        region = claim.get("region")
        if claim.get("author") == ctx.author.name or not isinstance(region, list) or len(region) != 4:
            continue
        if _intersects(box, region):
            ctx.warn("inside_claim", "{} is inside {} claimed by {}: {}".format(ids[0] if ids else "this", claim.get("id"),
                                                                              _who(claim.get("author"), None), claim.get("label") or ""), ids)


def _warn_overlap(ctx: _Ctx, el: Dict[str, Any]) -> None:
    if el.get("type") not in TEXT_TYPES or not el.get("text"):
        return
    box = bounds(el)
    for other in ctx.live():
        if other["id"] == el["id"] or other.get("type") not in TEXT_TYPES or not other.get("text"):
            continue
        if _intersects(box, bounds(other)):
            ctx.warn("overlap", "{} overlaps {}; move one so both texts read".format(el["id"], other["id"]), [el["id"], other["id"]])
            return


def _warn_frame_edge(ctx: _Ctx, el: Dict[str, Any]) -> None:
    """An element half inside a frame belongs to neither side: say which edges it crosses."""
    if el.get("type") in ("frame", "comment", "arrow"):
        return
    box = bounds(el)
    for frame in ctx.live():
        if frame.get("type") != "frame" or frame["id"] == el["id"] or frame.get("role") == PORTRAIT_ROLE:
            continue
        outer = bounds(frame)
        if not _intersects(outer, box) or _contains(outer, box) or _contains(box, outer):
            continue
        sides = [side for side, out in (("left", box[0] < outer[0]), ("top", box[1] < outer[1]),
                                        ("right", box[2] > outer[2]), ("bottom", box[3] > outer[3])) if out]
        ctx.warn("frame_edge", "{} sticks out of frame {} past its {} edge; move it inside or grow the frame".format(
            el["id"], frame["id"], " and ".join(sides)), [el["id"], frame["id"]])
        return


def _after_create(ctx: _Ctx, el: Dict[str, Any]) -> Dict[str, Any]:
    """Locks, then record, then the claim and legibility warnings."""
    _check_locks(ctx, [bounds(el)])
    ctx.put(el)
    _warn_claims(ctx, [el["id"]], bounds(el))
    _warn_overlap(ctx, el)
    _warn_frame_edge(ctx, el)
    return el


# --------------------------------------------------------------------------
# placement


def _free_slot(ctx: _Ctx, area: Sequence[float], w: float, h: float, exclude: Optional[str] = None,
               reserve: Sequence[Sequence[float]] = ()) -> Tuple[float, float]:
    """The first position in ``area`` (rows top to bottom, 20-unit steps) whose bounds plus a 20-unit margin hit nothing.

    Obstacles are the live elements that reach into the area, except the
    container itself and anything that wholly encloses the area (a parent
    frame), plus the ``reserve`` boxes (the author's portrait corner).
    """
    x0, y0, x1, y1 = area
    obstacles = [tuple(box) for box in reserve]
    for el in ctx.live():
        box = bounds(el)
        if el["id"] != exclude and _intersects(box, (x0 - GRID, y0 - GRID, x1 + GRID, y1 + GRID)) and not _contains(box, area):
            obstacles.append(box)
    y = y0
    while y + h <= y1:
        x = x0
        while x + w <= x1:
            grown = (x - GRID, y - GRID, x + w + GRID, y + h + GRID)
            hit = next((ob for ob in obstacles if _intersects(grown, ob)), None)
            if hit is None:
                return x, y
            # Jump past the obstacle, still on the grid.
            x = max(x + GRID, x0 + math.ceil((hit[2] + GRID - x0) / GRID) * GRID)
        y += GRID
    # Nothing fits: content-left, 20 below the lowest child, then further down past
    # anything that already overflowed there (so two big items never stack).
    y = max((ob[3] for ob in obstacles), default=y0 - GRID) + GRID
    below = [bounds(el) for el in ctx.live() if el["id"] != exclude and not _contains(bounds(el), area)] + [tuple(b) for b in reserve]
    for _attempt in range(len(below) + 1):
        grown = (x0 - GRID, y - GRID, x0 + w + GRID, y + h + GRID)
        hits = [box for box in below if _intersects(grown, box)]
        if not hits:
            break
        y = max(box[3] for box in hits) + GRID
    return x0, y


def _content_area(container: Dict[str, Any]) -> Tuple[float, float, float, float]:
    x0, y0, x1, y1 = bounds(container)
    top = FRAME_TOP if container.get("type") == "frame" else FRAME_PAD
    return x0 + FRAME_PAD, y0 + top, x1 - FRAME_PAD, y1 - FRAME_PAD


def _enclosing_frame(ctx: _Ctx, box: Sequence[float], exclude: Optional[str] = None) -> Optional[str]:
    """The smallest frame (not a portrait) wholly containing ``box``: an element placed there joins it."""
    best: Optional[Tuple[float, str]] = None
    for el in ctx.live():
        if el.get("type") != "frame" or el["id"] == exclude or el.get("role") == PORTRAIT_ROLE:
            continue
        frame_box = bounds(el)
        if _contains(frame_box, box):
            area = (frame_box[2] - frame_box[0]) * (frame_box[3] - frame_box[1])
            if best is None or area < best[0]:
                best = (area, el["id"])
    return best[1] if best else None


def _grow_frame(ctx: _Ctx, frame: Dict[str, Any], box: Sequence[float]) -> None:
    """Grow ``frame`` to hold ``box``: a change to the frame, so only its editor may, and never into a lock (7.1)."""
    x0, y0, x1, y1 = bounds(frame)
    nx1, ny1 = max(x1, box[2] + FRAME_PAD), max(y1, box[3] + FRAME_PAD)
    if (nx1, ny1) == (x1, y1):
        return
    if not _may_edit(ctx.author, frame):
        raise _error("element_not_yours", "{} has no room left and is {}'s, so it cannot grow for you; place this next to it, "
                     "or make it smaller".format(frame["id"], _who(frame.get("author"), None)), id=frame["id"], author=frame.get("author"))
    _check_locks(ctx, [(x0, y0, nx1, ny1)])
    ctx.update(frame, w=_round(nx1 - x0), h=_round(ny1 - y0))


def _portrait_of(ctx: _Ctx, name: str) -> Optional[Dict[str, Any]]:
    return next((el for el in ctx.live() if el.get("type") == "frame" and el.get("role") == PORTRAIT_ROLE and el.get("author") == name), None)


#: The home corner kept free for a portrait that does not exist yet (room for four steps).
PORTRAIT_RESERVE_STEPS = 4


def _portrait_corner(ctx: _Ctx, home: Sequence[float]) -> Tuple[float, float, float, float]:
    """The portrait's bounds, or the top-left corner of the home kept for it, so unplaced items never land there."""
    portrait = _portrait_of(ctx, ctx.author.name)
    if portrait is not None:
        return bounds(portrait)
    height = FRAME_TOP + PORTRAIT_RESERVE_STEPS * (PORTRAIT_STEP_H + PORTRAIT_STEP_GAP) - PORTRAIT_STEP_GAP + FRAME_PAD
    return float(home[0]), float(home[1]), float(home[0] + PORTRAIT_W), float(home[1] + height)


PLACE_KEYS = ("at", "right_of", "left_of", "below", "above", "inside")
PLACE_FIELDS = PLACE_KEYS + ("gap",)


def _place(ctx: _Ctx, op: Dict[str, Any], w: float, h: float) -> Tuple[float, float, Optional[str]]:
    """``(x, y, frame)`` for a new element of size ``w`` x ``h`` from the op's placement (contract 5.3)."""
    keys = [key for key in PLACE_KEYS if op.get(key) is not None]
    if len(keys) > 1:
        raise _invalid(keys[1], "use at most one of at, right_of, left_of, below, above, inside")
    gap = _num(op["gap"], "gap", 0, 5000) if op.get("gap") is not None else float(DEFAULT_GAP)
    if not keys:
        home = ctx.home()
        if home is None:
            raise _invalid("at", "the operator has no home region: pass at, or right_of/below/inside an element")
        x, y = _free_slot(ctx, (home[0] + FRAME_PAD, home[1] + FRAME_PAD, home[2] - FRAME_PAD, home[3] - FRAME_PAD), w, h,
                          reserve=[_portrait_corner(ctx, home)])
        return x, y, None
    key = keys[0]
    if key == "inside":
        container = ctx.lookup(op["inside"], "inside")
        x, y = _free_slot(ctx, _content_area(container), w, h, exclude=container["id"])
        if container.get("type") == "frame":
            _grow_frame(ctx, container, (x, y, x + w, y + h))
            return x, y, container["id"]
        return x, y, None
    if key == "at":
        x, y = _point(op["at"], "at", lambda ref, field: ctx.lookup(ref, field))[:2]
    else:
        ref = ctx.lookup(op[key], key)
        rx0, ry0, rx1, ry1 = bounds(ref)
        if key == "right_of":
            x, y = rx1 + gap, ry0
        elif key == "left_of":
            x, y = rx0 - gap - w, ry0
        elif key == "below":
            x, y = rx0, ry1 + gap
        else:
            x, y = rx0, ry0 - gap - h
    for field, value in (("x", x), ("y", y)):
        if abs(value) > MAX_COORD:
            raise _invalid(key, "that places the element outside the canvas ({} = {:g})".format(field, value))
    return x, y, _enclosing_frame(ctx, (x, y, x + w, y + h))


# --------------------------------------------------------------------------
# arrows


def _clip(el: Dict[str, Any], toward: Sequence[float], gap: float = 4.0) -> List[float]:
    """Where the line from ``el``'s centre toward ``toward`` leaves its outline, plus a small gap."""
    cx, cy = _center(el)
    dx, dy = float(toward[0]) - cx, float(toward[1]) - cy
    length = math.hypot(dx, dy)
    x0, y0, x1, y1 = bounds(el)
    a, b = (x1 - x0) / 2.0, (y1 - y0) / 2.0
    if length == 0:
        return [_r2(cx), _r2(cy)]
    kind = el.get("type")
    if kind == "ellipse":
        t = 1.0 / math.sqrt((dx / a) ** 2 + (dy / b) ** 2)
    elif kind == "diamond":
        t = 1.0 / (abs(dx) / a + abs(dy) / b)
    else:
        t = min(a / abs(dx) if dx else float("inf"), b / abs(dy) if dy else float("inf"))
    if t >= 1.0:
        return [_r2(cx), _r2(cy)]  # the target lies inside this element
    t = min(1.0, t + gap / length)
    return [_r2(cx + dx * t), _r2(cy + dy * t)]


End = Tuple[str, Any]  # ("element", element) | ("point", [x, y])


def _route(start: End, end: End, middle: Sequence[Sequence[float]] = ()) -> List[List[float]]:
    a = _center(start[1]) if start[0] == "element" else start[1]
    b = _center(end[1]) if end[0] == "element" else end[1]
    first_toward = middle[0] if middle else b
    last_toward = middle[-1] if middle else a
    p0 = _clip(start[1], first_toward) if start[0] == "element" else [_r2(a[0]), _r2(a[1])]
    pn = _clip(end[1], last_toward) if end[0] == "element" else [_r2(b[0]), _r2(b[1])]
    return [p0] + [[_r2(p[0]), _r2(p[1])] for p in middle] + [pn]


def _geometry(points: Sequence[Sequence[float]]) -> Dict[str, int]:
    x0, y0, x1, y1 = _bbox(points)
    return {"x": _round(x0), "y": _round(y0), "w": max(1, _round(x1 - x0)), "h": max(1, _round(y1 - y0))}


def _end(ctx: _Ctx, value: Any, field: str) -> End:
    if _is_point_form(value):
        return "point", _point(value, field)
    return "element", ctx.lookup(value, field)


def _reroute(ctx: _Ctx, arrow: Dict[str, Any]) -> None:
    """Re-run a bound arrow's ends after the elements it binds moved or changed."""
    points = arrow.get("points") or []
    if len(points) < 2:
        return
    start_el = ctx.el(arrow["from"]) if arrow.get("from") else None
    end_el = ctx.el(arrow["to"]) if arrow.get("to") else None
    start: End = ("element", start_el) if start_el is not None else ("point", points[0])
    end: End = ("element", end_el) if end_el is not None else ("point", points[-1])
    new_points = _route(start, end, points[1:-1])
    fields: Dict[str, Any] = {"points": new_points}
    fields.update(_geometry(new_points))
    if start_el is None and arrow.get("from"):
        fields["from"] = None
    if end_el is None and arrow.get("to"):
        fields["to"] = None
    ctx.update(arrow, **fields)


def _reroute_bound(ctx: _Ctx, changed: Iterable[str], skip: Iterable[str] = ()) -> None:
    ids = set(changed)
    skipped = set(skip)
    if not ids:
        return
    for el in list(ctx.live()):
        if el.get("type") == "arrow" and el["id"] not in skipped and (el.get("from") in ids or el.get("to") in ids):
            _reroute(ctx, ctx.el(el["id"]) or el)


# --------------------------------------------------------------------------
# the operations (contract section 6)

#: ``if_version`` is accepted on every op (the page sends it on all of them) and enforced on move, restyle, edit and delete.
_COMMON = ("op", "intent", "if_version")
_STYLE = STYLE_FIELDS
_FIELDS: Dict[str, Tuple[str, ...]] = {
    "shape": _COMMON + ("kind", "text", "w", "h", "id", "client_id") + PLACE_FIELDS + _STYLE,
    "arrow": _COMMON + ("from", "to", "points", "label", "head", "tail", "curve", "id", "client_id") + _STYLE,
    "frame": _COMMON + ("title", "w", "h", "children", "region", "id", "client_id") + PLACE_FIELDS + _STYLE,
    "pen": _COMMON + ("points", "closed", "style", "width", "color", "fill", "opacity", "dash", "id", "client_id"),
    "path": _COMMON + ("d", "scale", "id", "client_id") + PLACE_FIELDS + _STYLE,
    "svg": _COMMON + ("svg", "w", "h", "sketchy", "title", "id", "client_id") + PLACE_FIELDS,
    "graph": _COMMON + ("nodes", "edges", "layout", "direction", "title", "id") + PLACE_FIELDS + _STYLE,
    "mermaid": _COMMON + ("source", "w", "h", "title", "id", "client_id") + PLACE_FIELDS + _STYLE,
    "chart": _COMMON + ("spec", "data", "title", "w", "h", "id", "client_id") + PLACE_FIELDS,
    "viz": _COMMON + ("html", "libs", "data", "data_path", "title", "w", "h", "id", "client_id") + PLACE_FIELDS,
    "image": _COMMON + ("path", "asset", "w", "h", "id", "client_id") + PLACE_FIELDS,
    "comment": _COMMON + ("at", "text", "mentions", "reply_to", "client_id"),
    "claim": _COMMON + ("region", "label"),
    "release": _COMMON + ("id",),
    "legend": _COMMON + ("symbol", "meaning", "remove"),
    "move": _COMMON + ("id", "ids", "to", "by", "w", "h", "points", "from", "to_element", "frame",
                       "right_of", "left_of", "below", "above", "inside", "gap"),
    "restyle": _COMMON + ("id", "ids") + _STYLE,
    "edit": _COMMON + ("id", "text"),
    "delete": _COMMON + ("id", "ids", "with_children"),
    "portrait": _COMMON + ("steps", "current", "title"),
    "resolve": _COMMON + ("id",),
    "lock": _COMMON + ("region", "label"),
    "unlock": _COMMON + ("id",),
    "undo": _COMMON + ("batch",),
}


def _point_list(value: Any, field: str, maximum: int, limit_name: str, pressure: bool = False, minimum: int = 2) -> List[List[float]]:
    if not isinstance(value, (list, tuple)):
        raise _invalid(field, "{} must be a list of points".format(field))
    if len(value) > maximum:
        raise _too_big(field, limit_name, maximum, "{} has {} points; the limit is {}".format(field, len(value), maximum))
    if len(value) < minimum:
        raise _invalid(field, "{} needs at least {} points".format(field, minimum))
    return [_point(p, field, None, pressure) for p in value]


def _op_shape(ctx: _Ctx, op: Dict[str, Any]) -> None:
    kind = _choice(op.get("kind"), "kind", SHAPE_KINDS, "box")
    text = _text(op.get("text"), "text", MAX_TEXT_CHARS, "MAX_TEXT_CHARS", required=kind == "text")
    style = _style(op, _default_style(ctx.author_color(), kind))
    fields: Dict[str, Any] = {}
    if kind == "text":
        # A text's w is the width it wraps at; its height is what its lines need, so an h is ignored.
        wrap_w = _size(op, 1, 1)[0] if op.get("w") is not None else None
        w, h = text_size(text, float(style["size"]), wrap_w)
        fields["wrap"] = wrap_w is not None
    else:
        w, h = _size(op, *SHAPE_SIZES[kind])
    alias = _alias(ctx, op)
    client = _client_id(op)
    x, y, frame = _place(ctx, op, w, h)
    ctx.alias = alias
    _after_create(ctx, ctx.element(kind, x, y, w, h, text=text, style=style, alias=alias, client_id=client, frame=frame, **fields))


def _op_arrow(ctx: _Ctx, op: Dict[str, Any]) -> None:
    style = _style(op, _default_style(ctx.author_color(), "arrow"))
    label = _text(op.get("label"), "label", MAX_LABEL_CHARS, "MAX_LABEL_CHARS", one_line=True)
    head = _choice(op.get("head"), "head", HEADS, "arrow")
    tail = _choice(op.get("tail"), "tail", HEADS, "none")
    curve = _bool(op.get("curve"), "curve", False)
    alias = _alias(ctx, op)
    client = _client_id(op)
    start_id: Optional[str] = None
    end_id: Optional[str] = None
    if op.get("points") is not None:
        if op.get("from") is not None or op.get("to") is not None:
            raise _invalid("points", "an arrow takes points, or from and to, not both")
        points = _point_list(op["points"], "points", MAX_ARROW_POINTS, "MAX_ARROW_POINTS")
    else:
        if op.get("from") is None or op.get("to") is None:
            raise _invalid("from" if op.get("from") is None else "to", "an arrow needs from and to (an element or a point), or points")
        start, end = _end(ctx, op["from"], "from"), _end(ctx, op["to"], "to")
        points = _route(start, end)
        start_id = start[1]["id"] if start[0] == "element" else None
        end_id = end[1]["id"] if end[0] == "element" else None
    geo = _geometry(points)
    frame = _enclosing_frame(ctx, (geo["x"], geo["y"], geo["x"] + geo["w"], geo["y"] + geo["h"]))
    el = ctx.element("arrow", geo["x"], geo["y"], geo["w"], geo["h"], text=label, style=style, alias=alias, client_id=client,
                     frame=frame, **{"from": start_id, "to": end_id, "points": points, "head": head, "tail": tail, "curve": curve})
    ctx.alias = alias
    _after_create(ctx, el)


def _op_frame(ctx: _Ctx, op: Dict[str, Any]) -> None:
    title = _text(op.get("title"), "title", MAX_LABEL_CHARS, "MAX_LABEL_CHARS", one_line=True)
    style = _style(op, _default_style(ctx.author_color(), "frame"))
    alias = _alias(ctx, op)
    client = _client_id(op)
    children_raw, region_raw = op.get("children"), op.get("region")
    if children_raw is not None and region_raw is not None:
        raise _invalid("region", "a frame takes children or a region, not both")
    placed = [key for key in PLACE_KEYS + ("w", "h") if op.get(key) is not None]
    children: List[Dict[str, Any]] = []
    if children_raw is not None:
        if placed:
            raise _invalid(placed[0], "a frame around children takes its bounds from them")
        if not isinstance(children_raw, list) or not children_raw:
            raise _invalid("children", "children must be a non-empty list of elements")
        for ref in children_raw:
            child = ctx.lookup(ref, "children")
            if child.get("type") == "comment":
                raise _invalid("children", "comments stay pinned where they are; frame the element they point at")
            if all(c["id"] != child["id"] for c in children):
                children.append(child)
        for child in children:
            if not _may_edit(ctx.author, child):
                raise _error("element_not_yours", "{} is {}'s; a frame can only take elements you may edit".format(child["id"], _who(child.get("author"), None)),
                             id=child["id"], author=child.get("author"))
        boxes = [bounds(c) for c in children]
        x0, y0 = min(b[0] for b in boxes) - FRAME_PAD, min(b[1] for b in boxes) - FRAME_TOP
        x1, y1 = max(b[2] for b in boxes) + FRAME_PAD, max(b[3] for b in boxes) + FRAME_PAD
        parent = _enclosing_frame(ctx, (x0, y0, x1, y1))
    elif region_raw is not None:
        if placed:
            raise _invalid(placed[0], "a frame over a region takes its bounds from the region")
        x0, y0, x1, y1 = _region(region_raw, "region", ctx.lookup)
        parent = _enclosing_frame(ctx, (x0, y0, x1, y1))
        inside = [el for el in ctx.live() if el.get("type") != "comment" and el["id"] != parent and _contains((x0, y0, x1, y1), bounds(el))]
        inside_ids = {el["id"] for el in inside}
        # Only the top level moves in: what sits in a frame that is itself inside keeps that frame.
        children = [el for el in inside if el.get("frame") not in inside_ids and _may_edit(ctx.author, el)]
    else:
        w, h = _size(op, 400, 300)
        x0, y0, parent = _place(ctx, op, w, h)
        x1, y1 = x0 + w, y0 + h
    frame = ctx.element("frame", x0, y0, x1 - x0, y1 - y0, text=title, style=style, alias=alias, client_id=client, frame=parent)
    _check_locks(ctx, [bounds(frame)])
    ctx.put(frame)
    for child in children:
        ctx.update(ctx.el(child["id"]) or child, frame=frame["id"])
    ctx.alias = alias
    _warn_claims(ctx, [frame["id"]], bounds(frame))


def _op_pen(ctx: _Ctx, op: Dict[str, Any]) -> None:
    points = _point_list(op.get("points"), "points", MAX_PEN_POINTS, "MAX_PEN_POINTS", pressure=True)
    closed = _bool(op.get("closed"), "closed", False)
    mode = _choice(op.get("style"), "style", ("smooth", "straight"), "smooth")
    style = _style(op, _default_style(ctx.author_color(), "pen"))
    if style.get("fill") and not closed:
        raise _invalid("fill", "a pen stroke is filled only when closed: true")
    alias = _alias(ctx, op)
    client = _client_id(op)
    geo = _geometry(points)
    frame = _enclosing_frame(ctx, (geo["x"], geo["y"], geo["x"] + geo["w"], geo["y"] + geo["h"]))
    ctx.alias = alias
    _after_create(ctx, ctx.element("pen", geo["x"], geo["y"], geo["w"], geo["h"], style=style, alias=alias, client_id=client,
                                   frame=frame, points=points, closed=closed, smooth=mode == "smooth"))


def _op_path(ctx: _Ctx, op: Dict[str, Any]) -> None:
    d = op.get("d")
    if not isinstance(d, str) or not d.strip():
        raise _invalid("d", "path needs d: SVG path data")
    if len(d) > MAX_PATH_CHARS:
        raise _too_big("d", "MAX_PATH_CHARS", MAX_PATH_CHARS, "path data is {} characters; the limit is {}".format(len(d), MAX_PATH_CHARS))
    normalized, natural_w, natural_h = _render.normalize_path(d)
    scale = _num(op["scale"], "scale", 0.01, 100) if op.get("scale") is not None else 1.0
    w, h = natural_w * scale, natural_h * scale
    if w > MAX_SIZE or h > MAX_SIZE:
        raise _invalid("scale", "the path would be {:g} x {:g}; the limit is {} per side".format(w, h, MAX_SIZE))
    style = _style(op, _default_style(ctx.author_color(), "path"))
    alias = _alias(ctx, op)
    client = _client_id(op)
    x, y, frame = _place(ctx, op, w, h)
    ctx.alias = alias
    _after_create(ctx, ctx.element("path", x, y, w, h, style=style, alias=alias, client_id=client, frame=frame, d=normalized, scale=_r2(scale)))


def _op_svg(ctx: _Ctx, op: Dict[str, Any]) -> None:
    markup = op.get("svg")
    if not isinstance(markup, str) or not markup.strip():
        raise _invalid("svg", "svg needs svg: the markup")
    clean = _render.sanitize_svg(markup)
    _guard_code(markup, "svg")
    natural = _render.svg_size(clean) or _render.DEFAULT_SVG_SIZE
    w, h = _size(op, min(natural[0], MAX_SIZE), min(natural[1], MAX_SIZE))
    title = _text(op.get("title"), "title", MAX_LABEL_CHARS, "MAX_LABEL_CHARS", one_line=True)
    sketchy = _bool(op.get("sketchy"), "sketchy", False)
    alias = _alias(ctx, op)
    client = _client_id(op)
    x, y, frame = _place(ctx, op, w, h)
    el = ctx.element("svg", x, y, w, h, text=title, style=_default_style(ctx.author_color(), "svg"), alias=alias, client_id=client,
                     frame=frame, asset=None, sketchy=sketchy)
    _check_locks(ctx, [bounds(el)])
    el["asset"] = store_asset(ctx.team, clean.encode("utf-8"), "svg")["asset"]
    ctx.alias = alias
    _after_create(ctx, el)


def _node_alias(ctx: _Ctx, alias: Optional[str], node_id: str) -> Optional[str]:
    if alias is None:
        return None
    name = "{}.{}".format(alias, node_id)
    if not _ALIAS_RE.match(name):
        raise _invalid("id", "{} is too long to name its nodes ({}.<node id> must stay within 64 characters)".format(alias, alias))
    return _alias(ctx, {}, value=name)


def _depth(subgraphs: Sequence[Dict[str, Any]]) -> Dict[str, int]:
    parents = {sg["id"]: sg.get("parent") for sg in subgraphs}
    out: Dict[str, int] = {}
    for sid in parents:
        depth, cursor, seen = 0, parents.get(sid), {sid}
        while cursor and cursor not in seen:
            seen.add(cursor)
            depth += 1
            cursor = parents.get(cursor)
        out[sid] = depth
    return out


def _layout_key(nodes: Sequence[str], pairs: Sequence[Tuple[str, str]], algorithm: str, direction: str) -> Tuple[Any, ...]:
    return tuple(nodes), tuple((a, b) for a, b in pairs), algorithm, direction


#: Layouts slow enough to matter under ``canvas.lock`` (O(n^2) per iteration); the others take milliseconds.
_PRELAID = ("force",)


def _prelayout(ops: Sequence[Dict[str, Any]]) -> Dict[Tuple[Any, ...], Dict[str, Tuple[float, float]]]:
    """Force layouts for the batch's ``graph`` ops, computed before ``canvas.lock`` is taken.

    A 200-node force layout takes seconds, and a page write waiting on the
    lock gives up after ``LOCK_TIMEOUT_S``. Only well-formed inputs are laid
    out here; ``_op_graph`` still validates everything and lays out itself
    whatever this skipped.
    """
    out: Dict[Tuple[Any, ...], Dict[str, Tuple[float, float]]] = {}
    for op in ops:
        if op.get("op") != "graph" or op.get("layout") not in _PRELAID:
            continue
        nodes_raw, edges_raw = op.get("nodes"), op.get("edges") if op.get("edges") is not None else []
        if not isinstance(nodes_raw, list) or not isinstance(edges_raw, list) or len(nodes_raw) > MAX_GRAPH_NODES or len(edges_raw) > MAX_GRAPH_EDGES:
            continue
        nodes = [n if isinstance(n, str) else n.get("id") if isinstance(n, dict) else None for n in nodes_raw]
        pairs = []
        for edge in edges_raw:
            if isinstance(edge, (list, tuple)) and len(edge) in (2, 3):
                pairs.append((edge[0], edge[1]))
            elif isinstance(edge, dict):
                pairs.append((edge.get("from"), edge.get("to")))
            else:
                pairs = []
                break
        direction = op.get("direction") if op.get("direction") is not None else "down"
        if not all(isinstance(n, str) for n in nodes) or not all(isinstance(a, str) and isinstance(b, str) for a, b in pairs) or not isinstance(direction, str):
            continue
        if len(set(nodes)) != len(nodes) or len(pairs) != len(edges_raw):
            continue
        key = _layout_key(nodes, pairs, str(op["layout"]), direction)
        if key not in out:
            try:
                out[key] = _layout.layout(key[0], key[1], key[2], direction)
            except _OP_FAULTS:
                continue  # the op itself reports what is wrong with it
    return out


def _expand_graph(ctx: _Ctx, op: Dict[str, Any], title: str, nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]],
                  algorithm: str, direction: str, subgraphs: Sequence[Dict[str, Any]] = ()) -> None:
    """A frame (the group) holding native shapes and bound arrows, laid out in Python; Mermaid subgraphs become nested frames."""
    color = ctx.author_color()
    base = _style(op, _default_style(color, "box"))
    alias = _alias(ctx, op)
    key = _layout_key([n["id"] for n in nodes], [(e["from"], e["to"]) for e in edges], algorithm, direction)
    positions = ctx.layouts.get(key) or _layout.layout(key[0], key[1], algorithm, direction)
    node_box = {nid: (px, py, px + NODE_W, py + NODE_H) for nid, (px, py) in positions.items()}
    depth = _depth(subgraphs)
    sub_box: Dict[str, Tuple[float, float, float, float]] = {}
    for sg in sorted(subgraphs, key=lambda s: -depth[s["id"]]):
        boxes = [node_box[n["id"]] for n in nodes if n.get("subgraph") == sg["id"]]
        boxes += [sub_box[s["id"]] for s in subgraphs if s.get("parent") == sg["id"] and s["id"] in sub_box]
        if boxes:
            sub_box[sg["id"]] = (min(b[0] for b in boxes) - FRAME_PAD, min(b[1] for b in boxes) - FRAME_TOP,
                                 max(b[2] for b in boxes) + FRAME_PAD, max(b[3] for b in boxes) + FRAME_PAD)
    everything = list(node_box.values()) + list(sub_box.values())
    min_x, min_y = min(b[0] for b in everything), min(b[1] for b in everything)
    max_x, max_y = max(b[2] for b in everything), max(b[3] for b in everything)
    frame_w, frame_h = (max_x - min_x) + 2 * FRAME_PAD, (max_y - min_y) + FRAME_TOP + FRAME_PAD
    if frame_w > MAX_SIZE or frame_h > MAX_SIZE:
        raise _invalid("nodes", "the laid-out graph is {:g} x {:g}; split it (the limit is {} per side)".format(frame_w, frame_h, MAX_SIZE))
    x, y, parent = _place(ctx, op, frame_w, frame_h)
    ox, oy = x + FRAME_PAD - min_x, y + FRAME_TOP - min_y
    frame_style = _default_style(color, "frame")
    frame = ctx.element("frame", x, y, frame_w, frame_h, text=title, style=frame_style, alias=alias, frame=parent)
    _check_locks(ctx, [bounds(frame)])
    ctx.put(frame)
    ctx.alias = alias
    group = frame["id"]
    sub_ids: Dict[str, str] = {}
    for sg in sorted(subgraphs, key=lambda s: depth[s["id"]]):
        box = sub_box.get(sg["id"])
        if box is None:
            continue
        sub = ctx.element("frame", box[0] + ox, box[1] + oy, box[2] - box[0], box[3] - box[1], text=sg.get("title") or sg["id"],
                          style=frame_style, alias=_node_alias(ctx, alias, sg["id"]), frame=sub_ids.get(sg.get("parent") or "", group), group=group)
        ctx.put(sub)
        sub_ids[sg["id"]] = sub["id"]
    shapes: Dict[str, Dict[str, Any]] = {}
    for node in nodes:
        px, py = positions[node["id"]]
        style = dict(base)
        if node.get("color"):
            style["stroke"] = node["color"]
        if node.get("fill_set"):
            style["fill"] = node.get("fill")
        elif node["kind"] == "note" and "fill" not in op:
            style["fill"] = NOTE_FILL
        el = ctx.element(node["kind"], px + ox, py + oy, NODE_W, NODE_H, text=node["text"], style=style,
                         alias=_node_alias(ctx, alias, node["id"]), frame=sub_ids.get(node.get("subgraph") or "", group), group=group)
        shapes[node["id"]] = ctx.put(el)
    for edge in edges:
        a, b = shapes[edge["from"]], shapes[edge["to"]]
        middle: List[List[float]] = []
        if a["id"] == b["id"]:
            ax0, ay0, ax1, ay1 = bounds(a)
            cx, cy = _center(a)
            middle = [[ax1 + 40, cy], [ax1 + 40, ay0 - 20], [cx, ay0 - 20]]
        points = _route(("element", a), ("element", b), middle)
        style = dict(base, fill=None, dash=edge.get("dash") or "solid")
        if edge.get("thick"):
            style["width"] = 4
        geo = _geometry(points)
        ctx.put(ctx.element("arrow", geo["x"], geo["y"], geo["w"], geo["h"], text=edge.get("label") or "", style=style, frame=group, group=group,
                            **{"from": a["id"], "to": b["id"], "points": points, "head": edge.get("head") or "arrow", "tail": "none", "curve": False}))
    _warn_claims(ctx, [group], bounds(frame))


def _op_graph(ctx: _Ctx, op: Dict[str, Any]) -> None:
    nodes_raw, edges_raw = op.get("nodes"), op.get("edges") if op.get("edges") is not None else []
    if not isinstance(nodes_raw, list) or not nodes_raw:
        raise _invalid("nodes", "graph needs nodes: [{\"id\": \"a\", \"text\": \"...\"}, ...]")
    if len(nodes_raw) > MAX_GRAPH_NODES:
        raise _too_big("nodes", "MAX_GRAPH_NODES", MAX_GRAPH_NODES, "{} nodes; the limit is {}".format(len(nodes_raw), MAX_GRAPH_NODES))
    if not isinstance(edges_raw, list):
        raise _invalid("edges", "edges must be a list of {\"from\", \"to\"}")
    if len(edges_raw) > MAX_GRAPH_EDGES:
        raise _too_big("edges", "MAX_GRAPH_EDGES", MAX_GRAPH_EDGES, "{} edges; the limit is {}".format(len(edges_raw), MAX_GRAPH_EDGES))
    nodes: List[Dict[str, Any]] = []
    seen = set()
    for index, node in enumerate(nodes_raw):
        field = "nodes[{}]".format(index)
        if isinstance(node, str):
            node = {"id": node}
        if not isinstance(node, dict):
            raise _invalid(field, "{} must be an object with id and text".format(field))
        for key in node:
            if key not in ("id", "text", "kind", "color", "fill"):
                raise _invalid("{}.{}".format(field, key), "a node takes id, text, kind, color, fill")
        nid = node.get("id")
        if not isinstance(nid, str) or not _NODE_ID_RE.match(nid):
            raise _invalid(field + ".id", "a node id is 1 to 32 letters, digits, _ or -")
        if nid in seen:
            raise _invalid(field + ".id", "node id {} appears twice".format(nid))
        seen.add(nid)
        nodes.append({
            "id": nid,
            "text": _text(node.get("text", nid), field + ".text", MAX_LABEL_CHARS, "MAX_LABEL_CHARS", one_line=True) or nid,
            "kind": _choice(node.get("kind"), field + ".kind", SHAPE_KINDS, "box"),
            "color": _stroke_color(node["color"]) if node.get("color") is not None else None,
            "fill": _fill_color(node["fill"]) if node.get("fill") is not None else None,
            "fill_set": "fill" in node,
        })
    edges: List[Dict[str, Any]] = []
    for index, edge in enumerate(edges_raw):
        field = "edges[{}]".format(index)
        if isinstance(edge, (list, tuple)) and len(edge) in (2, 3):
            edge = {"from": edge[0], "to": edge[1], "label": edge[2] if len(edge) == 3 else None}
        if not isinstance(edge, dict):
            raise _invalid(field, "{} must be {{\"from\", \"to\", \"label\"}}".format(field))
        for key in edge:
            if key not in ("from", "to", "label", "dash"):
                raise _invalid("{}.{}".format(field, key), "an edge takes from, to, label, dash")
        for end in ("from", "to"):
            value = edge.get(end)
            if not isinstance(value, str) or value not in seen:
                raise _invalid("{}.{}".format(field, end), "{} is not one of the graph's node ids".format(
                    value if isinstance(value, str) else "{}.{}".format(field, end)))
        dash = edge.get("dash")
        dash_style = ("dashed" if dash else "solid") if isinstance(dash, bool) or dash is None else _choice(dash, field + ".dash", DASHES, "solid")
        edges.append({"from": edge["from"], "to": edge["to"], "dash": dash_style, "head": "arrow", "thick": False,
                      "label": _text(edge.get("label"), field + ".label", MAX_LABEL_CHARS, "MAX_LABEL_CHARS", one_line=True)})
    algorithm = _choice(op.get("layout"), "layout", _layout.LAYOUTS, "layered")
    direction = _choice(op.get("direction"), "direction", ("down", "right"), "down")
    title = _text(op.get("title"), "title", MAX_LABEL_CHARS, "MAX_LABEL_CHARS", one_line=True)
    _expand_graph(ctx, op, title or (op.get("id") if isinstance(op.get("id"), str) else "") or "graph", nodes, edges, algorithm, direction)


def _op_mermaid(ctx: _Ctx, op: Dict[str, Any]) -> None:
    source = op.get("source")
    if not isinstance(source, str) or not source.strip():
        raise _invalid("source", "mermaid needs source")
    if len(source.encode("utf-8")) > MAX_MERMAID_BYTES:
        raise _too_big("source", "MAX_MERMAID_BYTES", MAX_MERMAID_BYTES, "the Mermaid source is over {} KB".format(MAX_MERMAID_BYTES // 1024))
    _guard_code(source, "source")
    title = _text(op.get("title"), "title", MAX_LABEL_CHARS, "MAX_LABEL_CHARS", one_line=True)
    kind = _mermaid.diagram_kind(source)
    diagram = kind
    if kind == "flowchart":
        try:
            parsed: Optional[Dict[str, Any]] = _mermaid.parse_flowchart(source)
        except _mermaid.MermaidRefused as err:
            raise _invalid("source", str(err))
        except _mermaid.MermaidSyntax:
            parsed = None
        if parsed is not None:
            if len(parsed["nodes"]) > MAX_GRAPH_NODES:
                raise _too_big("source", "MAX_GRAPH_NODES", MAX_GRAPH_NODES, "{} nodes; the limit is {}".format(len(parsed["nodes"]), MAX_GRAPH_NODES))
            if len(parsed["edges"]) > MAX_GRAPH_EDGES:
                raise _too_big("source", "MAX_GRAPH_EDGES", MAX_GRAPH_EDGES, "{} edges; the limit is {}".format(len(parsed["edges"]), MAX_GRAPH_EDGES))
            for node in parsed["nodes"]:
                if not _NODE_ID_RE.match(node["id"]):
                    raise _invalid("source", "node id {} is longer than 32 characters".format(node["id"][:40]))
            nodes = [{"id": n["id"], "kind": n["kind"], "subgraph": n.get("subgraph"), "color": None, "fill": None, "fill_set": False,
                      "text": _text(n["text"], "source", MAX_LABEL_CHARS, "MAX_LABEL_CHARS", one_line=True) or n["id"]} for n in parsed["nodes"]]
            edges = [{"from": e["from"], "to": e["to"], "dash": "dashed" if e["dash"] else "solid", "head": e["head"], "thick": e["thick"],
                      "label": _text(e.get("label"), "source", MAX_LABEL_CHARS, "MAX_LABEL_CHARS", one_line=True)} for e in parsed["edges"]]
            subgraphs = [{"id": s["id"], "parent": s.get("parent"),
                          "title": _text(s.get("title"), "source", MAX_LABEL_CHARS, "MAX_LABEL_CHARS", one_line=True)} for s in parsed["subgraphs"]]
            _expand_graph(ctx, op, title or "flowchart", nodes, edges, "layered", parsed["direction"], subgraphs)
            return
        diagram = "other"
    w, h = _size(op, 480, 320)
    alias = _alias(ctx, op)
    client = _client_id(op)
    x, y, frame = _place(ctx, op, w, h)
    ctx.alias = alias
    _after_create(ctx, ctx.element("mermaid", x, y, w, h, text=title, style=_style(op, _default_style(ctx.author_color(), "mermaid")),
                                   alias=alias, client_id=client, frame=frame, source=source, diagram=diagram, still=None))


def _find_key(obj: Any, keys: Sequence[str], depth: int = 0) -> Optional[str]:
    """The first key named one of ``keys`` anywhere in ``obj``; a spec too deep to search is refused, never passed."""
    if depth > MAX_SPEC_DEPTH:
        raise _error("chart_refused", "the chart spec nests deeper than {} levels; flatten it".format(MAX_SPEC_DEPTH),
                     limit="MAX_SPEC_DEPTH", max=MAX_SPEC_DEPTH)
    if isinstance(obj, dict):
        for key, value in obj.items():
            if isinstance(key, str) and key.lower() in keys:
                return key
            found = _find_key(value, keys, depth + 1)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_key(value, keys, depth + 1)
            if found:
                return found
    return None


def _artifact_rel(ctx: _Ctx, path: Path) -> str:
    root = artifacts_dir(ctx.layout, ctx.team, ctx.doc)
    base = Path(os.path.realpath(os.fspath(root))) if root is not None else path.parent
    return path.relative_to(base).as_posix()


def _op_chart(ctx: _Ctx, op: Dict[str, Any]) -> None:
    spec = op.get("spec")
    if not isinstance(spec, dict):
        raise _invalid("spec", "chart needs spec: a Vega-Lite object")
    raw = json.dumps(spec, ensure_ascii=False, separators=(",", ":"))
    if len(raw.encode("utf-8")) > MAX_CHART_SPEC_BYTES:
        raise _error("chart_refused", "the chart spec is over {} KB; put the data in a file under artifacts/".format(MAX_CHART_SPEC_BYTES // 1024),
                     limit="MAX_CHART_SPEC_BYTES", max=MAX_CHART_SPEC_BYTES)
    found = _find_key(spec, ("url", "href"))
    if found:
        raise _error("chart_refused", "a chart spec may not load anything itself: remove {!r} and pass data (a file under artifacts/)".format(found), key=found)
    _guard_code(raw, "spec")
    data_rel: Optional[str] = None
    if op.get("data") is not None:
        inline = spec.get("data")
        if isinstance(inline, dict) and "values" in inline:
            raise _invalid("data", "give the data as a file (data) or inline (spec.data.values), not both")
        data_rel = _artifact_rel(ctx, artifact_file(ctx.layout, ctx.team, op["data"], ctx.doc))
    title = _text(op.get("title"), "title", MAX_LABEL_CHARS, "MAX_LABEL_CHARS", one_line=True)
    w, h = _size(op, 480, 320)
    alias = _alias(ctx, op)
    client = _client_id(op)
    x, y, frame = _place(ctx, op, w, h)
    el = ctx.element("chart", x, y, w, h, text=title, style=_default_style(ctx.author_color(), "chart"), alias=alias, client_id=client,
                     frame=frame, spec_asset=None, data=data_rel, still=None)
    _check_locks(ctx, [bounds(el)])
    el["spec_asset"] = store_asset(ctx.team, raw.encode("utf-8"), "json")["asset"]
    ctx.alias = alias
    _after_create(ctx, el)


def _op_viz(ctx: _Ctx, op: Dict[str, Any]) -> None:
    _features.require_viz(ctx.layout.session, ctx.team, ctx.doc)
    html = op.get("html")
    if not isinstance(html, str) or not html.strip():
        raise _invalid("html", "viz needs html: the page body that draws it")
    if len(html.encode("utf-8")) > MAX_VIZ_BYTES:
        raise _too_big("html", "MAX_VIZ_BYTES", MAX_VIZ_BYTES, "the viz html is over {} KB".format(MAX_VIZ_BYTES // 1024))
    _guard_code(html, "html")
    libs_raw = op.get("libs") if op.get("libs") is not None else []
    if not isinstance(libs_raw, list) or any(lib not in VIZ_LIBS for lib in libs_raw):
        raise _invalid("libs", "libs is a list of: {}".format(", ".join(VIZ_LIBS)))
    libs = list(dict.fromkeys(libs_raw))
    data, data_path = op.get("data"), op.get("data_path")
    if data is not None and data_path is not None:
        raise _invalid("data", "give data inline or as data_path, not both")
    if data is not None:
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
        if len(raw.encode("utf-8")) > MAX_VIZ_DATA_BYTES:
            raise _too_big("data", "MAX_VIZ_DATA_BYTES", MAX_VIZ_DATA_BYTES, "inline viz data is over {} KB; use data_path".format(MAX_VIZ_DATA_BYTES // 1024))
        _guard_code(raw, "data")
    rel = _artifact_rel(ctx, artifact_file(ctx.layout, ctx.team, data_path, ctx.doc)) if data_path is not None else None
    title = _text(op.get("title"), "title", MAX_LABEL_CHARS, "MAX_LABEL_CHARS", one_line=True, required=True)
    w, h = _size(op, 480, 360)
    alias = _alias(ctx, op)
    client = _client_id(op)
    x, y, frame = _place(ctx, op, w, h)
    el = ctx.element("viz", x, y, w, h, text=title, style=_default_style(ctx.author_color(), "viz"), alias=alias, client_id=client,
                     frame=frame, html_asset=None, libs=libs, data=data, data_path=rel, still=None)
    _check_locks(ctx, [bounds(el)])
    el["html_asset"] = store_asset(ctx.team, html.encode("utf-8"), "html")["asset"]
    ctx.alias = alias
    _after_create(ctx, el)


def _under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def _image_source(ctx: _Ctx, raw: Any) -> Path:
    """A PNG/JPEG path under the artifacts dir, ``whiteboard/renders/``, or the author's roster cwd."""
    if not isinstance(raw, str) or not raw.strip():
        raise _invalid("path", "path must name a PNG or JPEG file")
    text = raw.strip()
    art = artifacts_dir(ctx.layout, ctx.team, ctx.doc)
    roots: List[Path] = [_dir(ctx.team) / RENDERS_DIR]
    if art is not None:
        roots.append(art)
    row = _member_row(ctx.doc, ctx.author.name) if ctx.author.is_member else None
    cwd = row.get("cwd") if row else None
    home = os.path.realpath(os.path.expanduser("~"))
    if isinstance(cwd, str) and cwd and os.path.realpath(cwd) not in ("/", home):
        roots.append(Path(cwd))
    real_roots = [Path(os.path.realpath(os.fspath(root))) for root in roots]
    candidate = Path(os.path.expanduser(text))
    if candidate.is_absolute():
        candidates = [candidate]
    else:
        candidates = []
        if art is not None:
            candidates.append(art / (text[len("artifacts/"):] if text.startswith("artifacts/") else text))
        if isinstance(cwd, str) and cwd:
            candidates.append(Path(cwd) / text)
    for path in candidates:
        real = Path(os.path.realpath(os.fspath(path)))
        if any(_under(real, root) for root in real_roots) and real.is_file():
            return real
    raise _error("path_refused", "{} is not a file under the team's artifacts/, whiteboard/renders/, or your working directory".format(text), path=text)


def _op_image(ctx: _Ctx, op: Dict[str, Any]) -> None:
    path_raw, asset_raw = op.get("path"), op.get("asset")
    if (path_raw is None) == (asset_raw is None):
        raise _invalid("path", "image takes path (a PNG or JPEG file) or asset (a page upload), one of them")
    if asset_raw is not None:
        if not ctx.author.is_human:
            raise _error("operator_only", "asset names come from the page's upload; agents pass path")
        data = store.read_bytes(asset_path(ctx.team, str(asset_raw))) or b""
    else:
        location = _image_source(ctx, path_raw)
        if location.stat().st_size > MAX_IMAGE_BYTES:
            raise _error("image_refused", "the image is over {} MB".format(MAX_IMAGE_BYTES // (1024 * 1024)), limit="MAX_IMAGE_BYTES", max=MAX_IMAGE_BYTES)
        data = store.read_bytes(location) or b""
    if len(data) > MAX_IMAGE_BYTES:
        raise _error("image_refused", "the image is over {} MB".format(MAX_IMAGE_BYTES // (1024 * 1024)), limit="MAX_IMAGE_BYTES", max=MAX_IMAGE_BYTES)
    sniffed = _render.sniff_image(data)
    if sniffed is None or sniffed[1] <= 0 or sniffed[2] <= 0:
        raise _error("image_refused", "not a PNG or JPEG (checked by its bytes, not its name)")
    mime, px_w, px_h = sniffed
    shrink = min(1.0, 480.0 / px_w)
    w, h = _size(op, max(1.0, min(MAX_SIZE, px_w * shrink)), max(1.0, min(MAX_SIZE, px_h * shrink)))
    alias = _alias(ctx, op)
    client = _client_id(op)
    x, y, frame = _place(ctx, op, w, h)
    el = ctx.element("image", x, y, w, h, style=_default_style(ctx.author_color(), "image"), alias=alias, client_id=client, frame=frame,
                     asset=None, mime=mime, px_w=px_w, px_h=px_h)
    _check_locks(ctx, [bounds(el)])
    el["asset"] = store_asset(ctx.team, data, "image")["asset"]
    ctx.alias = alias
    _after_create(ctx, el)


def _resolve_member(ctx: _Ctx, name: str) -> Optional[str]:
    """A mention to a member name: exact, ``<team>-<name>``, or the only holder of that role; ``human`` for the operator."""
    if name in (HUMAN, "operator"):
        return HUMAN
    rows = _agent_rows(ctx.doc)
    for candidate in (name, "{}-{}".format(ctx.team.name, name)):
        if any(row.get("name") == candidate for row in rows):
            return candidate
    holders = [row for row in rows if row.get("role") == name]
    return str(holders[0]["name"]) if len(holders) == 1 else None


def _mentions(ctx: _Ctx, explicit: Any, text: str) -> List[str]:
    names: List[str] = []
    if explicit is not None:
        if isinstance(explicit, str):
            explicit = [explicit]
        if not isinstance(explicit, list):
            raise _invalid("mentions", "mentions is a list of member names (or human)")
        for raw in explicit:
            if not isinstance(raw, str) or not raw.strip():
                raise _invalid("mentions", "mentions is a list of member names (or human)")
            name = _resolve_member(ctx, raw.strip().lstrip("@"))
            if name is None:
                raise _error("mention_unknown", "{} is not a member of team {} (nor human)".format(raw.strip(), ctx.team.name),
                             mention=raw.strip(), roster=[row.get("name") for row in _agent_rows(ctx.doc)])
            if name not in names:
                names.append(name)
    for token in _MENTION_RE.findall(text or ""):
        name = _resolve_member(ctx, token)
        if name and name not in names:
            names.append(name)
    return names


def _comment_ref(ctx: _Ctx, ref: Any, field: str) -> Dict[str, Any]:
    el = ctx.lookup(ref, field)
    if el.get("type") != "comment":
        raise _error("element_unknown", "{} is not a comment".format(ref), ref=ref, field=field)
    return el


def _op_comment(ctx: _Ctx, op: Dict[str, Any]) -> None:
    at = op.get("at")
    if at is None:
        raise _invalid("at", "comment needs at: an element, a comment to reply to, or a point")
    text = _text(op.get("text"), "text", MAX_COMMENT_CHARS, "MAX_COMMENT_CHARS", required=True)
    client = _client_id(op)
    reply_to = _comment_ref(ctx, op["reply_to"], "reply_to")["id"] if op.get("reply_to") is not None else None
    if _is_point_form(at):
        on, point = None, _point(at, "at")[:2]
    else:
        target = ctx.lookup(at, "at")
        if target.get("type") == "comment":
            if reply_to is not None and reply_to != target["id"]:
                raise _invalid("reply_to", "at names comment {} but reply_to names {}".format(target["id"], reply_to))
            reply_to = target["id"]
            on = target.get("on")
            base = target.get("point") if isinstance(target.get("point"), list) else [target.get("x"), target.get("y")]
            point = [float(base[0]) + 16, float(base[1]) + 16]
        else:
            on = target["id"]
            x0, y0, x1, _y1 = bounds(target)
            point = [x1, y0]
    mentions = _mentions(ctx, op.get("mentions"), text)
    el = ctx.element("comment", point[0], point[1], 1, 1, text=text, style=_default_style(ctx.author_color(), "comment"), client_id=client,
                     on=on, point=[_r2(point[0]), _r2(point[1])], mentions=mentions, reply_to=reply_to, resolved=False, resolved_by=None)
    _check_locks(ctx, [bounds(el)])
    ctx.put(el)
    if mentions:
        ctx.mention = el


def _op_claim(ctx: _Ctx, op: Dict[str, Any]) -> None:
    if op.get("region") is None:
        raise _invalid("region", "claim needs region: where you are about to draw")
    region = _region(op["region"], "region", ctx.lookup)
    label = _text(op.get("label"), "label", MAX_LABEL_CHARS, "MAX_LABEL_CHARS", one_line=True) or ctx.intent
    _check_locks(ctx, [region])
    own = sorted((c for c in ctx.state.active_claims(ctx.now) if c.get("author") == ctx.author.name),
                 key=lambda c: (str(c.get("at")), _id_number(c.get("id"))))
    released = []
    while len(own) >= MAX_CLAIMS_PER_AUTHOR:
        oldest = own.pop(0)
        ctx.other("claim", "delete", oldest["id"], None)
        released.append(oldest["id"])
    cid = ctx.new_id("K")
    ctx.other("claim", "add", cid, {"id": cid, "author": ctx.author.name, "region": region, "label": label, "intent": ctx.intent,
                                    "at": ctx.ts, "expires_at": _iso(ctx.now + CLAIM_TTL_S)})
    ctx.created.append(cid)
    ctx.changed.extend(released)
    _warn_claims(ctx, [cid], region)


def _op_release(ctx: _Ctx, op: Dict[str, Any]) -> None:
    raw = op.get("id")
    active = {c["id"]: c for c in ctx.state.active_claims(ctx.now)}
    if raw is not None and raw != "all":
        claim = active.get(raw.strip()) if isinstance(raw, str) else None
        if claim is None:
            raise _error("element_unknown", "{} is not an active claim (expired or released)".format(raw), ref=raw)
        if claim.get("author") != ctx.author.name and not ctx.author.operator:
            raise _error("element_not_yours", "{} is {}'s claim; it expires on its own".format(claim["id"], _who(claim.get("author"), None)),
                         id=claim["id"], author=claim.get("author"))
        targets = [claim]
    else:
        targets = [c for c in active.values() if c.get("author") == ctx.author.name]
        if not targets:
            raise _error("element_unknown", "you hold no active claims")
    for claim in targets:
        ctx.other("claim", "delete", claim["id"], None)
        ctx.changed.append(claim["id"])


def _legend_symbol(ctx: _Ctx, raw: Any) -> str:
    if isinstance(raw, str) and _ID_RE.match(raw.strip()):
        return ctx.lookup(raw, "symbol")["id"]
    if isinstance(raw, str) and _ALIAS_RE.match(raw.strip()) and not _CELL_RE.match(raw.strip()):
        try:
            return ctx.lookup(raw, "symbol")["id"]
        except HerdrTeamError:
            pass  # free text that happens to look like an alias
    return _text(raw, "symbol", MAX_SYMBOL_CHARS, "MAX_SYMBOL_CHARS", one_line=True, required=True)


def _op_legend(ctx: _Ctx, op: Dict[str, Any]) -> None:
    if op.get("remove") is not None:
        if op.get("symbol") is not None or op.get("meaning") is not None:
            raise _invalid("remove", "remove takes only the legend entry's G- id")
        gid = op["remove"]
        entry = ctx.state.legend.get(gid) if isinstance(gid, str) else None
        if entry is None:
            raise _error("element_unknown", "{} is not in the legend".format(gid), ref=gid)
        if entry.get("author") != ctx.author.name and not (ctx.author.manager or ctx.author.operator):
            raise _error("element_not_yours", "{} is {}'s legend entry".format(gid, _who(entry.get("author"), None)), id=gid, author=entry.get("author"))
        ctx.other("legend", "delete", gid, None)
        ctx.changed.append(gid)
        return
    meaning = _text(op.get("meaning"), "meaning", MAX_LABEL_CHARS, "MAX_LABEL_CHARS", one_line=True, required=True)
    if op.get("symbol") is None:
        raise _invalid("symbol", "legend needs symbol: an element (E-19) or a few words (\"red cross\")")
    symbol = _legend_symbol(ctx, op["symbol"])
    if len(ctx.state.legend) >= MAX_LEGEND:
        raise _too_big("legend", "MAX_LEGEND", MAX_LEGEND, "the legend holds {} entries already; remove one first".format(MAX_LEGEND))
    gid = ctx.new_id("G")
    ctx.other("legend", "add", gid, {"id": gid, "symbol": symbol, "meaning": meaning, "author": ctx.author.name, "at": ctx.ts})
    ctx.created.append(gid)


def _targets(ctx: _Ctx, op: Dict[str, Any], single: bool = False) -> List[Dict[str, Any]]:
    """The elements an edit op names (``id`` or ``ids``), checked for authority and ``if_version``."""
    if op.get("id") is not None and op.get("ids") is not None:
        raise _invalid("ids", "use id or ids, not both")
    if op.get("ids") is not None:
        if single:
            raise _invalid("ids", "{} takes one id".format(op.get("op")))
        refs = op["ids"]
        if not isinstance(refs, list) or not refs:
            raise _invalid("ids", "ids must be a non-empty list")
        if len(refs) > MAX_ELEMENTS:
            raise _too_big("ids", "MAX_ELEMENTS", MAX_ELEMENTS, "too many ids")
        field = "ids"
    elif op.get("id") is not None:
        refs, field = [op["id"]], "id"
    else:
        raise _invalid("id", "{} needs id (or ids)".format(op.get("op")))
    out: List[Dict[str, Any]] = []
    for ref in refs:
        el = ctx.lookup(ref, field)
        if all(o["id"] != el["id"] for o in out):
            out.append(el)
    for el in out:
        if not _may_edit(ctx.author, el):
            raise _error("element_not_yours", "{} is {}'s; members change their own elements, the manager any agent's, the operator anything".format(
                el["id"], _who(el.get("author"), None)), id=el["id"], author=el.get("author"))
    if op.get("if_version") is not None:
        expected = int(_num(op["if_version"], "if_version", 0, 10 ** 12))
        for el in out:
            if int(el.get("updated_seq") or 0) != expected:
                raise _error("canvas_stale", "{} changed since v{} (it is at v{}); look again".format(el["id"], expected, el.get("updated_seq")),
                             id=el["id"], current=el.get("updated_seq"))
    return out


def _descendants(ctx: _Ctx, roots: Iterable[str]) -> List[str]:
    """Every element inside the given frames (recursively) or in the given groups."""
    frontier = set(roots)
    found: List[str] = []
    seen = set(frontier)
    live = list(ctx.live())
    while frontier:
        nxt = set()
        for el in live:
            if el["id"] not in seen and (el.get("frame") in frontier or el.get("group") in frontier):
                seen.add(el["id"])
                found.append(el["id"])
                nxt.add(el["id"])
        frontier = nxt
    return found


def _on_canvas(el: Dict[str, Any], fields: Dict[str, Any], field: str = "by") -> Dict[str, Any]:
    """``fields`` unless they put the element's corner or any of its points past ``MAX_COORD`` (contract 5.1)."""
    values = [fields.get("x", el.get("x")), fields.get("y", el.get("y"))]
    for key in ("points", "point"):
        raw = fields.get(key)
        if isinstance(raw, list):
            values.extend(v for p in (raw if key == "points" else [raw]) for v in list(p)[:2])
    if any(_is_number(v) and abs(float(v)) > MAX_COORD for v in values):
        raise _invalid(field, "that takes {} outside the canvas (|x|, |y| <= {})".format(el.get("id"), MAX_COORD), id=el.get("id"))
    return fields


def _translated(el: Dict[str, Any], dx: float, dy: float, unbind: bool) -> Dict[str, Any]:
    fields: Dict[str, Any] = {"x": _round(float(el.get("x") or 0) + dx), "y": _round(float(el.get("y") or 0) + dy)}
    if isinstance(el.get("points"), list):
        fields["points"] = [[_r2(p[0] + dx), _r2(p[1] + dy)] + list(p[2:]) for p in el["points"]]
    if el.get("type") == "comment" and isinstance(el.get("point"), list):
        fields["point"] = [_r2(el["point"][0] + dx), _r2(el["point"][1] + dy)]
    if unbind and el.get("type") == "arrow":
        fields["from"] = None
        fields["to"] = None
    return _on_canvas(el, fields)


def _resized(el: Dict[str, Any], w: Any, h: Any) -> Dict[str, Any]:
    if el.get("type") == "comment":
        raise _invalid("w", "a comment is a pin; it has no size")
    if el.get("type") == "text":
        # Resizing a text sets the width it wraps at; its height follows from its lines.
        if w is None:
            raise _invalid("h", "a text's height follows its lines; give w to set the width it wraps at")
        style = el.get("style") if isinstance(el.get("style"), dict) else {}
        return _on_canvas(el, _text_fields(el, str(el.get("text") or ""), style, _num(w, "w", 1, MAX_SIZE)), "w")
    old_w, old_h = max(1.0, float(el.get("w") or 1)), max(1.0, float(el.get("h") or 1))
    new_w = _num(w, "w", 1, MAX_SIZE) if w is not None else old_w
    new_h = _num(h, "h", 1, MAX_SIZE) if h is not None else old_h
    fields: Dict[str, Any] = {"w": max(1, _round(new_w)), "h": max(1, _round(new_h))}
    if el.get("type") in ("pen", "arrow") and isinstance(el.get("points"), list):
        x0, y0 = float(el.get("x") or 0), float(el.get("y") or 0)
        sx, sy = new_w / old_w, new_h / old_h
        fields["points"] = [[_r2(x0 + (p[0] - x0) * sx), _r2(y0 + (p[1] - y0) * sy)] + list(p[2:]) for p in el["points"]]
    elif el.get("type") == "path":
        scale = float(el.get("scale") or 1) or 1.0
        natural_w, natural_h = old_w / scale, old_h / scale
        scale = new_w / natural_w
        fields.update(w=max(1, _round(new_w)), h=max(1, _round(natural_h * scale)), scale=round(scale, 4))
    return _on_canvas(el, fields, "w")


def _op_move(ctx: _Ctx, op: Dict[str, Any]) -> None:
    targets = _targets(ctx, op)
    modes = [key for key in ("to", "by", "right_of", "left_of", "below", "above", "inside") if op.get(key) is not None]
    if len(modes) > 1:
        raise _invalid(modes[1], "use one of to, by, right_of, left_of, below, above, inside")
    resize = op.get("w") is not None or op.get("h") is not None
    has_points = op.get("points") is not None
    rebind = "from" in op or "to_element" in op
    reframe = "frame" in op
    if not (modes or resize or has_points or rebind or reframe):
        raise _invalid("to", "move needs to, by, a relative placement, w/h, points, from/to_element, or frame")
    if (has_points or rebind) and len(targets) != 1:
        raise _invalid("points" if has_points else "from", "points and from/to_element change one arrow or stroke at a time")
    first = targets[0]
    explicit = [t["id"] for t in targets]
    dx = dy = 0.0
    adopt: Optional[str] = None
    if modes:
        mode = modes[0]
        if mode == "to":
            px, py = _point(op["to"], "to", ctx.lookup)[:2]
            dx, dy = px - float(first.get("x") or 0), py - float(first.get("y") or 0)
        elif mode == "by":
            delta = op["by"]
            if isinstance(delta, str) and _XY_RE.match(delta):
                delta = [float(v) for v in _XY_RE.match(delta).groups()]  # type: ignore[union-attr]
            if not isinstance(delta, (list, tuple)) or len(delta) != 2:
                raise _invalid("by", "by is [dx, dy]")
            dx, dy = _coord(delta[0], "by"), _coord(delta[1], "by")
        else:
            placement = {mode: op[mode]}
            if op.get("gap") is not None:
                placement["gap"] = op["gap"]
            if mode == "inside":
                container = ctx.lookup(op["inside"], "inside")
                if container["id"] in explicit or container["id"] in _descendants(ctx, explicit):
                    raise _invalid("inside", "an element cannot move inside itself or its own children")
            x, y, placed_in = _place(ctx, placement, float(first.get("w") or 1), float(first.get("h") or 1))
            dx, dy = x - float(first.get("x") or 0), y - float(first.get("y") or 0)
            # Placing inside a frame makes the element its child (contract 5.3), for a move as for a new element.
            adopt = placed_in if mode == "inside" else None
    boxes: List[Tuple[float, float, float, float]] = []
    changed: List[str] = []
    if dx or dy:
        for eid in explicit + [d for d in _descendants(ctx, explicit) if d not in explicit]:
            el = ctx.el(eid)
            if el is None:
                continue
            if eid not in explicit and not _may_edit(ctx.author, el):
                raise _error("element_not_yours", "moving {} would move {} ({}'s)".format(first["id"], eid, _who(el.get("author"), None)),
                             id=eid, author=el.get("author"))
            boxes.append(bounds(el))
            moved = ctx.update(el, **_translated(el, dx, dy, unbind=eid in explicit))
            boxes.append(bounds(moved))
            changed.append(eid)
    if resize:
        for eid in explicit:
            el = ctx.el(eid)
            boxes.append(bounds(el))  # type: ignore[arg-type]
            resized = ctx.update(el, **_resized(el, op.get("w"), op.get("h")))  # type: ignore[arg-type]
            boxes.append(bounds(resized))
            changed.append(eid)
    repointed: List[str] = []
    if has_points:
        el = ctx.el(first["id"]) or first
        if el.get("type") not in ("arrow", "pen"):
            raise _invalid("points", "points belong to arrows and pen strokes")
        pen = el.get("type") == "pen"
        points = _point_list(op["points"], "points", MAX_PEN_POINTS if pen else MAX_ARROW_POINTS,
                             "MAX_PEN_POINTS" if pen else "MAX_ARROW_POINTS", pressure=pen)
        boxes.append(bounds(el))
        el = ctx.update(el, points=points, **_geometry(points))
        boxes.append(bounds(el))
        repointed.append(el["id"])
    if rebind:
        el = ctx.el(first["id"]) or first
        if el.get("type") != "arrow":
            raise _invalid("from", "from and to_element rebind an arrow's ends")
        fields = {}
        if "from" in op:
            fields["from"] = None if op["from"] is None else ctx.lookup(op["from"], "from")["id"]
        if "to_element" in op:
            fields["to"] = None if op["to_element"] is None else ctx.lookup(op["to_element"], "to_element")["id"]
        el = ctx.update(el, **fields)
        _reroute(ctx, el)
        boxes.append(bounds(ctx.el(el["id"]) or el))
        repointed.append(el["id"])
    if reframe:
        target_frame: Optional[str] = None
        if op["frame"] is not None:
            frame_el = ctx.lookup(op["frame"], "frame")
            if frame_el.get("type") != "frame":
                raise _invalid("frame", "{} is not a frame".format(frame_el["id"]))
            if frame_el["id"] in explicit or frame_el["id"] in _descendants(ctx, explicit):
                raise _invalid("frame", "an element cannot join its own frame or group")
            target_frame = frame_el["id"]
        for eid in explicit:
            el = ctx.el(eid)
            if el is not None and el.get("type") != "comment":
                ctx.update(el, frame=target_frame)
    elif adopt is not None:
        container = ctx.el(adopt)
        for eid in explicit:
            el = ctx.el(eid)
            if el is not None and container is not None and el.get("type") != "comment" and el.get("frame") != adopt \
                    and _contains(bounds(container), bounds(el)):
                ctx.update(el, frame=adopt)
    _check_locks(ctx, boxes)
    _reroute_bound(ctx, changed, skip=set(changed) | set(repointed))
    for eid in explicit:
        el = ctx.el(eid)
        if el is not None:
            _warn_claims(ctx, [eid], bounds(el))
            _warn_frame_edge(ctx, el)


def _op_restyle(ctx: _Ctx, op: Dict[str, Any]) -> None:
    targets = _targets(ctx, op)
    if not any(op.get(key) is not None for key in STYLE_FIELDS) and "fill" not in op:
        raise _invalid("color", "restyle needs at least one of: {}".format(", ".join(STYLE_FIELDS)))
    boxes = []
    for target in targets:
        el = ctx.el(target["id"]) or target
        style = _style(op, el.get("style") if isinstance(el.get("style"), dict) else _default_style(HUMAN_COLOR, str(el.get("type"))))
        if el.get("type") == "pen" and style.get("fill") and not el.get("closed"):
            raise _invalid("fill", "{} is an open stroke; only closed strokes take a fill".format(el["id"]))
        boxes.append(bounds(el))
        fields: Dict[str, Any] = {"style": style}
        if el.get("type") == "text":
            fields.update(_text_fields(el, str(el.get("text") or ""), style))
        restyled = ctx.update(el, **fields)
        boxes.append(bounds(restyled))
        if el.get("type") == "text":
            _warn_frame_edge(ctx, restyled)
    _check_locks(ctx, boxes)


def _op_edit(ctx: _Ctx, op: Dict[str, Any]) -> None:
    el = _targets(ctx, op, single=True)[0]
    kind = el.get("type")
    if "text" not in op:
        raise _invalid("text", "edit needs text")
    if kind in TEXT_TYPES:
        text = _text(op["text"], "text", MAX_TEXT_CHARS, "MAX_TEXT_CHARS", required=kind == "text")
    elif kind == "comment":
        text = _text(op["text"], "text", MAX_COMMENT_CHARS, "MAX_COMMENT_CHARS", required=True)
    elif kind in ("arrow", "frame", "svg", "chart", "viz", "mermaid", "image"):
        text = _text(op["text"], "text", MAX_LABEL_CHARS, "MAX_LABEL_CHARS", one_line=True, required=kind == "viz")
    else:
        raise _invalid("text", "a {} has no text".format(kind))
    fields: Dict[str, Any] = {"text": text}
    if kind == "text":
        fields.update(_text_fields(el, text, el.get("style") if isinstance(el.get("style"), dict) else {}))
    _check_locks(ctx, [bounds(el)])
    edited = ctx.update(el, **fields)
    _warn_overlap(ctx, edited)
    _warn_frame_edge(ctx, edited)


def _op_delete(ctx: _Ctx, op: Dict[str, Any]) -> None:
    targets = _targets(ctx, op)
    with_children = _bool(op.get("with_children"), "with_children", False)
    doomed = [t["id"] for t in targets]
    if with_children:
        for eid in _descendants(ctx, doomed):
            el = ctx.el(eid)
            if el is not None and not _may_edit(ctx.author, el):
                raise _error("element_not_yours", "deleting with children would delete {} ({}'s)".format(eid, _who(el.get("author"), None)),
                             id=eid, author=el.get("author"))
            doomed.append(eid)
    _check_locks(ctx, [bounds(ctx.el(eid) or {}) for eid in doomed])
    for eid in doomed:
        ctx.drop(eid)
    _unbind(ctx, ctx.live(), set(doomed))


def _unbind(ctx: _Ctx, elements: Iterable[Dict[str, Any]], gone: Optional[Set[str]] = None) -> None:
    """Clear the references ``elements`` hold to deleted ids (contract 6, ``delete``).

    Arrows keep their points and lose the bound end, a frame's children stay
    unframed, a comment stays where it was pinned. ``gone`` names the deleted
    ids; without it any reference to an element no longer on the canvas goes.
    """
    def dead(ref: Any) -> bool:
        if not isinstance(ref, str) or not ref:
            return False
        return ref in gone if gone is not None else ctx.el(ref) is None

    for el in list(elements):
        fields: Dict[str, Any] = {key: None for key in ("frame", "group") if dead(el.get(key))}
        if el.get("type") == "arrow":
            fields.update({key: None for key in ("from", "to") if dead(el.get(key))})
        if el.get("type") == "comment" and dead(el.get("on")):
            fields["on"] = None
        if fields:
            ctx.update(ctx.el(el["id"]) or el, **fields)


STEP_STATUSES = ("pending", "in_progress", "completed")


def _op_portrait(ctx: _Ctx, op: Dict[str, Any]) -> None:
    if not ctx.author.is_member:
        raise _invalid("op", "the operator has no home, so no portrait; draw a frame instead")
    steps_raw = op.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise _invalid("steps", "portrait needs steps: your plan, one short line each")
    if len(steps_raw) > MAX_PORTRAIT_STEPS:
        raise _too_big("steps", "MAX_PORTRAIT_STEPS", MAX_PORTRAIT_STEPS, "{} steps; a portrait shows at most {}".format(len(steps_raw), MAX_PORTRAIT_STEPS))
    steps: List[List[Any]] = []
    for index, step in enumerate(steps_raw):
        field = "steps[{}]".format(index)
        status = None
        if isinstance(step, dict):
            for key in step:
                if key not in ("text", "status"):
                    raise _invalid("{}.{}".format(field, key), "a step takes text and status")
            status = _choice(step.get("status"), field + ".status", STEP_STATUSES, "pending")
            step = step.get("text")
        steps.append([_text(step, field, MAX_LABEL_CHARS, "MAX_LABEL_CHARS", one_line=True, required=True), status or "pending"])
    current = int(_num(op["current"], "current", 1, len(steps))) if op.get("current") is not None else None
    if current is not None:
        for index, step in enumerate(steps):
            step[1] = "completed" if index < current - 1 else ("in_progress" if index == current - 1 else "pending")
    else:
        current = next((i + 1 for i, s in enumerate(steps) if s[1] == "in_progress"), None)
    title = _text(op.get("title"), "title", MAX_LABEL_CHARS, "MAX_LABEL_CHARS", one_line=True) or "{}'s plan".format(ctx.author.name)
    color = ctx.author_color()
    home = ctx.home() or [0, HOME_TOP, HOME_W, HOME_TOP + HOME_H]
    height = FRAME_TOP + len(steps) * (PORTRAIT_STEP_H + PORTRAIT_STEP_GAP) - PORTRAIT_STEP_GAP + FRAME_PAD
    existing = _portrait_of(ctx, ctx.author.name)
    if existing is not None:
        frame = ctx.update(existing, text=title, w=PORTRAIT_W, h=height)
    else:
        # The home's top-left corner, unless something already sits there: then the home's first free slot.
        x, y = float(home[0]), float(home[1])
        if any(_intersects((x, y, x + PORTRAIT_W, y + height), bounds(el)) for el in ctx.live()):
            x, y = _free_slot(ctx, (home[0] + FRAME_PAD, home[1] + FRAME_PAD, home[2] - FRAME_PAD, home[3] - FRAME_PAD), PORTRAIT_W, height)
        frame = ctx.put(ctx.element("frame", x, y, PORTRAIT_W, height, text=title, style=_default_style(color, "frame"), role=PORTRAIT_ROLE))
    fx, fy = float(frame["x"]), float(frame["y"])
    old_steps = sorted((el for el in ctx.live() if el.get("group") == frame["id"] and el.get("role") == PORTRAIT_ROLE and el["id"] != frame["id"]),
                       key=lambda el: (float(el.get("y") or 0), _id_number(el.get("id"))))
    for index, (text, status) in enumerate(steps):
        style = dict(_default_style(color, "box"), size=16)
        label = text
        if status == "completed":
            style["fill"] = FILLS["gray"]
            label = "✓ " + text
        elif status == "in_progress":
            style.update(fill=ctx.author_fill(), width=4)
        x, y = fx + FRAME_PAD, fy + FRAME_TOP + index * (PORTRAIT_STEP_H + PORTRAIT_STEP_GAP)
        if index < len(old_steps):
            ctx.update(old_steps[index], x=_round(x), y=_round(y), w=PORTRAIT_STEP_W, h=PORTRAIT_STEP_H, text=label, style=style)
        else:
            ctx.put(ctx.element("box", x, y, PORTRAIT_STEP_W, PORTRAIT_STEP_H, text=label, style=style, frame=frame["id"],
                                group=frame["id"], role=PORTRAIT_ROLE))
    for extra in old_steps[len(steps):]:
        ctx.drop(extra["id"])
    _check_locks(ctx, [bounds(frame)])
    ctx.extra["portrait"] = {"current": current, "total": len(steps)}


def _op_resolve(ctx: _Ctx, op: Dict[str, Any]) -> None:
    el = _comment_ref(ctx, op.get("id"), "id")
    name = ctx.author.name
    allowed = ctx.author.operator or (ctx.author.is_member and (el.get("author") == name or name in (el.get("mentions") or []) or ctx.author.manager))
    if not allowed:
        raise _error("element_not_yours", "resolving {} is for its author, a member it mentions, the manager or the operator".format(el["id"]),
                     id=el["id"], author=el.get("author"))
    ctx.update(el, resolved=True, resolved_by=name)


def _op_lock(ctx: _Ctx, op: Dict[str, Any]) -> None:
    if not ctx.author.operator:
        raise _error("operator_only", "locking a region is for the operator")
    if op.get("region") is None:
        raise _invalid("region", "lock needs region")
    region = _region(op["region"], "region", ctx.lookup)
    label = _text(op.get("label"), "label", MAX_LABEL_CHARS, "MAX_LABEL_CHARS", one_line=True) or "hands off"
    xid = ctx.new_id("X")
    ctx.other("lock", "add", xid, {"id": xid, "region": region, "label": label, "by": ctx.author.name, "at": ctx.ts})
    ctx.created.append(xid)


def _op_unlock(ctx: _Ctx, op: Dict[str, Any]) -> None:
    if not ctx.author.operator:
        raise _error("operator_only", "unlocking a region is for the operator")
    xid = op.get("id")
    if not isinstance(xid, str) or xid not in ctx.state.locks:
        raise _error("element_unknown", "{} is not a lock".format(xid), ref=xid)
    ctx.other("lock", "delete", xid, None)
    ctx.changed.append(xid)


def _op_undo(ctx: _Ctx, op: Dict[str, Any]) -> None:
    bid = op.get("batch")
    if not isinstance(bid, str) or not re.match(r"^B-[1-9][0-9]{0,6}\Z", bid.strip()):
        raise _invalid("batch", "undo needs batch: a B- id")
    bid = bid.strip()
    entry = ctx.state.batches.get(bid)
    if entry is None:
        raise _error("element_unknown", "{} is not in the canvas history (too old, or cleared)".format(bid), ref=bid)
    if entry.get("undone"):
        raise _invalid("batch", "{} is already undone".format(bid))
    events = _read_events(ctx.team)
    mine = [e for e in events if e.get("batch") == bid]
    if not mine:
        raise _error("element_unknown", "{} is not in the canvas log (cleared)".format(bid), ref=bid)
    by = mine[0].get("author") if isinstance(mine[0].get("author"), dict) else {}
    agent_batch = by.get("kind") == KIND_MEMBER
    allowed = (ctx.author.operator
               or (ctx.author.is_member and agent_batch and by.get("name") == ctx.author.name)
               or (ctx.author.is_member and ctx.author.manager and agent_batch))
    if not allowed:
        raise _error("operator_only", "undoing {} ({}'s batch) is for the manager (agents' batches) or the operator".format(bid, _who(by.get("name"), None)),
                     batch=bid, author=by.get("name"))
    first = min(int(e["seq"]) for e in mine)
    touched: List[Tuple[str, str]] = []
    for event in mine:
        for change in event.get("changes") or []:
            key = (change.get("target"), change.get("id"))
            if key[0] in ("element", "claim", "legend") and isinstance(key[1], str) and key not in touched:
                touched.append(key)  # type: ignore[arg-type]
    before: Dict[Tuple[str, str], Optional[Dict[str, Any]]] = {key: None for key in touched}
    for event in events:
        if int(event["seq"]) >= first:
            break
        if event.get("op") == "clear":
            before = {key: None for key in touched}
            continue
        for change in event.get("changes") or []:
            key = (change.get("target"), change.get("id"))
            if key in before:
                before[key] = None if change.get("action") == "delete" else change.get("value")  # type: ignore[index]
    boxes = []
    restored: List[str] = []
    dropped: Set[str] = set()
    skipped: List[str] = []
    for target, ident in touched:
        value = before[(target, ident)]
        if target == "element":
            current = ctx.el(ident)
            # Undo never crosses an authority boundary. A batch "touches" whatever it re-routed or
            # re-framed (a peer's or the operator's bound arrow), and whatever changed later is written
            # back too, so an element this author could not edit directly keeps its current state.
            if (current is not None and not _may_edit(ctx.author, current)) or (value is not None and not _may_edit(ctx.author, value)):
                skipped.append(ident)
                continue
            if current is not None:
                boxes.append(bounds(current))
            if value is None:
                if current is not None:
                    ctx.drop(ident)
                    dropped.add(ident)
                continue
            new = dict(value, updated_seq=ctx.seq, updated_at=ctx.ts)
            if new.get("alias"):
                owner = (ctx.state.aliases.get(new["alias"]) or {}).get(str(new.get("author")))
                if owner is not None and owner != ident and ctx.el(owner) is not None:
                    new["alias"] = None
            ctx.put(new)
            boxes.append(bounds(new))
            restored.append(ident)
        else:
            store_map = ctx.state.claims if target == "claim" else ctx.state.legend
            current = store_map.get(ident)
            if value is None:
                if current is not None:
                    ctx.other(target, "delete", ident, None)
                    ctx.changed.append(ident)
            elif not (target == "claim" and _expired(value, ctx.now)):
                ctx.other(target, "update" if current is not None else "add", ident, value)
                ctx.changed.append(ident)
    # Nothing may point at what the undo deleted, and a restored element may name one deleted since.
    if dropped:
        _unbind(ctx, ctx.live(), dropped)
    _unbind(ctx, [el for el in (ctx.el(ident) for ident in restored) if el is not None])
    _check_locks(ctx, boxes)
    _reroute_bound(ctx, restored, skip=restored)
    if skipped:
        ctx.warn("undo_skipped", "undo left {} as {} now: changing {} is for its editor".format(
            _ids_text(skipped), "it is" if len(skipped) == 1 else "they are", "it" if len(skipped) == 1 else "them"), skipped)
    ctx.extra["undoes"] = bid


_HANDLERS: Dict[str, Callable[[_Ctx, Dict[str, Any]], None]] = {
    "shape": _op_shape, "arrow": _op_arrow, "frame": _op_frame, "pen": _op_pen, "path": _op_path, "svg": _op_svg,
    "graph": _op_graph, "mermaid": _op_mermaid, "chart": _op_chart, "viz": _op_viz, "image": _op_image,
    "comment": _op_comment, "claim": _op_claim, "release": _op_release, "legend": _op_legend, "move": _op_move,
    "restyle": _op_restyle, "edit": _op_edit, "delete": _op_delete, "portrait": _op_portrait, "resolve": _op_resolve,
    "lock": _op_lock, "unlock": _op_unlock, "undo": _op_undo,
}


# --------------------------------------------------------------------------
# applying a batch (contract section 7)


def _check_batch(ops: Any) -> List[Dict[str, Any]]:
    if not isinstance(ops, list):
        raise _invalid("ops", "a batch is a list of operations")
    if len(ops) > MAX_BATCH_OPS:
        raise _too_big("ops", "MAX_BATCH_OPS", MAX_BATCH_OPS, "{} operations in one batch; the limit is {} (keep batches under about 40)".format(len(ops), MAX_BATCH_OPS))
    for index, op in enumerate(ops):
        if not isinstance(op, dict):
            raise _invalid("ops", "operation {} is not an object".format(index), index=index)
    try:
        size = len(json.dumps(ops, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
    except (TypeError, ValueError):
        raise _invalid("ops", "the batch is not plain JSON")
    except RecursionError:
        raise _invalid("ops", "the batch nests too deeply to read")
    if size > MAX_BATCH_BYTES:
        raise _too_big("ops", "MAX_BATCH_BYTES", MAX_BATCH_BYTES, "the batch is {} KB; the limit is {} KB".format(size // 1024, MAX_BATCH_BYTES // 1024))
    return ops


def parse_batch(raw: Any) -> Tuple[List[Dict[str, Any]], bool]:
    """``{"ops": [...], "atomic": bool}`` or a bare list -> ``(ops, atomic)``; batch-shape and size checks only."""
    if isinstance(raw, (bytes, bytearray)):
        raw = raw.decode("utf-8", "replace")
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError as err:
            raise _invalid("ops", "the batch is not valid JSON ({})".format(err))
        except RecursionError:
            raise _invalid("ops", "the batch nests too deeply to read")
    if isinstance(raw, list):
        return _check_batch(raw), False
    if isinstance(raw, dict):
        for key in raw:
            if key not in ("ops", "atomic"):
                raise _invalid(str(key), "a batch is {\"ops\": [...], \"atomic\": false}")
        if "ops" not in raw:
            raise _invalid("ops", "a batch is {\"ops\": [...], \"atomic\": false}")
        return _check_batch(raw["ops"]), _bool(raw.get("atomic"), "atomic", False)
    raise _invalid("ops", "a batch is {\"ops\": [...]} or a list of operations")


def _rate_entries(doc: Any, name: str, now: float) -> List[List[float]]:
    """``[ts, ops, bytes]`` rows of ``name`` inside the window (rows written before bytes were counted read as 0 bytes)."""
    authors = doc.get("authors") if isinstance(doc, dict) and isinstance(doc.get("authors"), dict) else {}
    out = []
    for entry in authors.get(name) or []:
        if not isinstance(entry, list) or len(entry) not in (2, 3) or not all(_is_number(v) for v in entry) or now - entry[0] >= RATE_WINDOW_S:
            continue
        out.append([float(entry[0]), float(entry[1]), float(entry[2]) if len(entry) == 3 else 0.0])
    return out


def _retry_after(entries: List[List[float]], column: int, need: float, now: float) -> int:
    """Seconds until the oldest rows free ``need`` units of ``column`` (1 ops, 2 bytes)."""
    freed = 0.0
    retry = RATE_WINDOW_S
    for entry in sorted(entries):
        freed += entry[column]
        if freed >= need:
            retry = entry[0] + RATE_WINDOW_S - now
            break
    return max(1, int(math.ceil(retry)))


def _check_rate(team: TeamPaths, name: str, count: int, now: float, size: int = 0) -> None:
    """``canvas_rate`` when this batch's ops, or the bytes it writes to the log, would pass the agent's minute."""
    entries = _rate_entries(store.read_json(_file(team, RATE_FILE), default=None), name, now)
    used = int(sum(entry[1] for entry in entries))
    if used + count > MAX_OPS_PER_MINUTE:
        raise _error("canvas_rate", "{} ops in the last minute plus {} now is over {}; wait and send a smaller batch".format(used, count, MAX_OPS_PER_MINUTE),
                     retry_after_s=_retry_after(entries, 1, used + count - MAX_OPS_PER_MINUTE, now), limit=MAX_OPS_PER_MINUTE, used=used)
    written = int(sum(entry[2] for entry in entries))
    if written + size > MAX_CHANGE_BYTES_PER_MINUTE or (size == 0 and written >= MAX_CHANGE_BYTES_PER_MINUTE):
        raise _error("canvas_rate", "your changes wrote {} KB to the canvas log in the last minute{}; the limit is {} KB. Wait, and change fewer or "
                     "smaller elements at a time".format(written // 1024, " and this batch would add {} KB".format(size // 1024) if size else "",
                                                         MAX_CHANGE_BYTES_PER_MINUTE // 1024),
                     retry_after_s=_retry_after(entries, 2, max(1, written + size - MAX_CHANGE_BYTES_PER_MINUTE), now),
                     limit="MAX_CHANGE_BYTES_PER_MINUTE", max=MAX_CHANGE_BYTES_PER_MINUTE, used=written)


def _record_rate(team: TeamPaths, name: str, count: int, now: float, size: int = 0) -> None:
    doc = store.read_json(_file(team, RATE_FILE), default=None)
    authors = doc.get("authors") if isinstance(doc, dict) and isinstance(doc.get("authors"), dict) else {}
    kept = {author: _rate_entries(doc, author, now) for author in authors}
    kept = {author: entries for author, entries in kept.items() if entries}
    kept.setdefault(name, []).append([now, float(count), float(size)])
    store.write_json(_file(team, RATE_FILE), {"v": SCHEMA, "authors": kept}, fsync=False)


#: Malformed input a validator missed: that op's ``op_invalid``, never a crash that loses the whole batch.
_OP_FAULTS = (TypeError, ValueError, KeyError, IndexError, RecursionError)


def apply_ops(layout: Any, team: TeamPaths, ops: List[Dict[str, Any]], author: CanvasAuthor, atomic: bool = False,
              doc: Optional[Dict[str, Any]] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Validate and apply a batch under ``canvas.lock``; the apply result (contract 7.2). Batch-level refusals raise."""
    moment = time.time() if now is None else float(now)
    doc = doc if isinstance(doc, dict) else store.RosterStore(team).load()
    _features.require_on(layout.session, team, doc)
    _check_writer(author, doc, team)
    ops = _check_batch(ops)
    if author.is_member:
        _check_rate(team, author.name, len(ops), moment)
    result: Dict[str, Any] = {"team": team.name, "version": 0, "batch": None, "atomic": bool(atomic), "applied": [], "refused": [],
                              "aliases": {}, "warnings": [], "notices": {"canvas_changed": None, "canvas_sent": []}}
    if not ops:
        result["version"] = current_version(team)
        return result
    layouts = _prelayout(ops)  # seconds for a big force graph: never under canvas.lock
    lock = _canvas_lock(team)
    lock.acquire()
    notice: Optional[Dict[str, Any]] = None
    mentions: List[Dict[str, Any]] = []
    try:
        ensure_dir(_dir(team))
        state = _load_state(team)
        state.drop_expired(moment)
        batch_id = "B-{}".format(state.counters.get("B", 0) + 1)
        ctx = _Ctx(layout, team, author, doc, state, moment, batch_id)
        ctx.layouts = layouts
        events: List[Dict[str, Any]] = []
        lines: List[bytes] = []
        for index, op in enumerate(ops):
            name = op.get("op")
            ctx.begin(index, name if isinstance(name, str) else "")
            try:
                handler = _HANDLERS.get(name) if isinstance(name, str) else None
                if handler is None:
                    raise _invalid("op", "unknown op {!r}; one of: {}".format(name, ", ".join(OPS)))
                _check_fields(op, _FIELDS[name])
                ctx.intent = _intent(op, author)
                ctx.author_color()  # an author's first applied op registers its colour and home
                handler(ctx, op)
                if ctx.live_delta() > 0 and len(state.elements) + ctx.live_delta() > MAX_ELEMENTS:
                    raise _too_big("ops", "MAX_ELEMENTS", MAX_ELEMENTS, "the canvas holds {} elements; the limit is {}".format(len(state.elements), MAX_ELEMENTS))
                event = ctx.event(author)
                line = _event_line(event)
                if author.is_member and len(line) > MAX_OP_CHANGE_BYTES:
                    # Every touched element is written whole, so one move over many large elements is megabytes of log.
                    raise _too_big("ids", "MAX_OP_CHANGE_BYTES", MAX_OP_CHANGE_BYTES, "this op would write {} KB to the canvas log; the limit is {} KB: "
                                   "change fewer elements at a time".format(len(line) // 1024, MAX_OP_CHANGE_BYTES // 1024))
            except HerdrTeamError as err:
                result["refused"].append({"index": index, "op": name if isinstance(name, str) else None, "code": err.code,
                                          "message": err.message, "details": dict(err.details)})
                continue
            except _OP_FAULTS as err:
                _log.warning("canvas op %s (%s) failed: %s", index, name, err, exc_info=True)
                result["refused"].append({"index": index, "op": name if isinstance(name, str) else None, "code": "op_invalid",
                                          "message": "{} could not be applied: {}".format(name, type(err).__name__),
                                          "details": {"field": "op", "error": type(err).__name__}})
                continue
            state.fold(event)
            events.append(event)
            lines.append(line)
            entry: Dict[str, Any] = {"index": index, "op": name, "ids": list(event["ids"])}
            if ctx.alias:
                entry["alias"] = ctx.alias
            result["applied"].append(entry)
            result["warnings"].extend(ctx.warnings)
            result["aliases"].update(ctx.aliases)
            if ctx.mention is not None:
                mentions.append(ctx.mention)
        result["version"] = state.version
        result["batch"] = batch_id if events else None
        if atomic and result["refused"]:
            first = result["refused"][0]
            raise _error("canvas_refused", "atomic batch refused at op {} ({}: {}); nothing was applied".format(first["index"], first["code"], first["message"]),
                         **dict(result, version=current_version(team), batch=None, applied_nothing=True))
        size = sum(len(line) for line in lines)
        if author.is_member and size:
            _check_rate(team, author.name, 0, moment, size)  # the log bytes are known only now; refusing here writes nothing
        if events:
            _append_events(team, events, lines)
            _write_scene(team, state, moment)
            notice = _queue_notice(team, author, events, moment)
        if author.is_member:
            _record_rate(team, author.name, len(ops), moment, size)
    finally:
        lock.release()
    # Board records only after the canvas lock is released (BoardStore.append takes team.lock).
    if notice is not None:
        result["notices"]["canvas_changed"] = _post_changed(layout, team, doc, notice)
    for comment in mentions:
        seq = _post_mention(layout, team, doc, author, comment, result["version"], state)
        if seq is not None:
            result["notices"]["canvas_sent"].append(seq)
    return result


def check_applied(result: Dict[str, Any]) -> Dict[str, Any]:
    """``canvas_refused`` (exit 1, details = the result) when a non-empty batch applied nothing; else the result."""
    if result.get("refused") and not result.get("applied"):
        first = result["refused"][0]
        raise _error("canvas_refused", "nothing applied: op {} {}: {}".format(first["index"], first["code"], first["message"]), **result)
    return result


# --------------------------------------------------------------------------
# board records: canvas_changed (coalesced awareness) and canvas_sent (a named wake)

_NOUNS = {
    "box": ("box", "boxes"), "ellipse": ("ellipse", "ellipses"), "diamond": ("diamond", "diamonds"), "note": ("note", "notes"),
    "text": ("text", "texts"), "arrow": ("arrow", "arrows"), "frame": ("frame", "frames"), "pen": ("pen stroke", "pen strokes"),
    "path": ("path", "paths"), "svg": ("svg block", "svg blocks"), "mermaid": ("mermaid diagram", "mermaid diagrams"),
    "chart": ("chart", "charts"), "viz": ("live visual", "live visuals"), "image": ("image", "images"),
    "comments": ("comment", "comments"), "claims": ("claim", "claims"), "legend": ("legend entry", "legend entries"),
    "locks": ("lock", "locks"),
}
_VERB_COUNTS = {"move": "moved", "restyle": "restyled", "edit": "edited", "release": "released", "resolve": "resolved",
                "undo": "undone", "unlock": "unlocked"}
_COUNT_ORDER = tuple(t for t in ELEMENT_TYPES if t != "comment") + ("comments", "claims", "legend", "locks", "portrait",
                                                                    "moved", "restyled", "edited", "deleted", "released", "resolved", "undone", "unlocked")


def _event_counts(event: Dict[str, Any]) -> Dict[str, int]:
    counts: Dict[str, int] = {}
    op = event.get("op")
    changes = [c for c in event.get("changes") or [] if isinstance(c, dict)]

    def bump(key: str, n: int = 1) -> None:
        if n:
            counts[key] = counts.get(key, 0) + n

    if op == "comment":
        bump("comments")
    elif op == "claim":
        bump("claims")
    elif op == "legend":
        bump("legend", sum(1 for c in changes if c.get("target") == "legend" and c.get("action") == "add"))
        bump("deleted", sum(1 for c in changes if c.get("target") == "legend" and c.get("action") == "delete"))
    elif op == "lock":
        bump("locks")
    elif op == "portrait":
        bump("portrait")
    elif op == "delete":
        bump("deleted", sum(1 for c in changes if c.get("target") == "element" and c.get("action") == "delete"))
    elif op == "undo":
        bump("undone")
    elif op in _VERB_COUNTS:
        # A moved frame counts its children and the arrows that followed: what readers will see changed.
        bump(_VERB_COUNTS[op], max(1, len(event.get("ids") or [])))
    else:
        for change in changes:
            value = change.get("value")
            if change.get("target") == "element" and change.get("action") == "add" and isinstance(value, dict):
                bump(str(value.get("type")))
    return counts


def counts_text(counts: Dict[str, int]) -> str:
    """``3 notes, 2 arrows, 1 pen stroke, moved 2``."""
    parts = []
    for key in _COUNT_ORDER + tuple(k for k in counts if k not in _COUNT_ORDER):
        n = counts.get(key, 0)
        if not n:
            continue
        if key in _NOUNS:
            singular, plural = _NOUNS[key]
            parts.append("{} {}".format(n, singular if n == 1 else plural))
        elif key == "portrait":
            parts.append("updated its portrait")
        elif key == "undone":
            parts.append("undid {} {}".format(n, "batch" if n == 1 else "batches"))
        else:
            parts.append("{} {}".format(key, n))
    return ", ".join(parts) or "small changes"


def _merge_pending(pending: Optional[Dict[str, Any]], summary: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(pending, dict):
        return summary
    counts = dict(pending.get("counts") or {})
    for key, n in summary["counts"].items():
        counts[key] = int(counts.get(key, 0)) + int(n)
    batches = list(dict.fromkeys(list(pending.get("batches") or []) + summary["batches"]))[-50:]
    return {"from": min(int(pending.get("from") or summary["from"]), summary["from"]),
            "to": max(int(pending.get("to") or 0), summary["to"]), "counts": counts, "batches": batches}


def _queue_notice(team: TeamPaths, author: CanvasAuthor, events: List[Dict[str, Any]], now: float) -> Optional[Dict[str, Any]]:
    """Under the canvas lock: fold this batch into the author's pending summary; what to post now, if the window allows."""
    counts: Dict[str, int] = {}
    for event in events:
        for key, n in _event_counts(event).items():
            counts[key] = counts.get(key, 0) + n
    summary = {"from": int(events[0]["seq"]), "to": int(events[-1]["seq"]), "counts": counts,
               "batches": list(dict.fromkeys(str(e.get("batch")) for e in events if e.get("batch")))}
    doc = store.read_json(_file(team, NOTICES_FILE), default=None)
    authors = dict(doc.get("authors")) if isinstance(doc, dict) and isinstance(doc.get("authors"), dict) else {}
    entry = authors.get(author.name) if isinstance(authors.get(author.name), dict) else {}
    pending = _merge_pending(entry.get("pending"), summary)
    last = entry.get("last_at")
    post: Optional[Dict[str, Any]] = None
    if not _is_number(last) or now - float(last) >= NOTICE_INTERVAL_S:
        post = dict(pending, author=author.name, kind=author.kind)
        authors[author.name] = {"last_at": now, "pending": None, "kind": author.kind}
    else:
        authors[author.name] = {"last_at": last, "pending": pending, "kind": author.kind}
    store.write_json(_file(team, NOTICES_FILE), {"v": SCHEMA, "authors": authors}, fsync=False)
    return post


def _post_changed(layout: Any, team: TeamPaths, doc: Dict[str, Any], notice: Dict[str, Any]) -> Optional[int]:
    name = str(notice.get("author"))
    to = [str(row["name"]) for row in _agent_rows(doc) if row.get("name") != name]
    if notice.get("kind") == KIND_MEMBER:
        to.append(HUMAN)
    if not to:
        return None
    first, last = int(notice["from"]), int(notice["to"])
    versions = "v{}".format(last) if first == last else "v{}–v{}".format(first, last)
    who = "the operator" if name == HUMAN else name
    text = "{} drew on the canvas: {} ({}). Look: {} canvas look --since {}".format(who, counts_text(notice.get("counts") or {}), versions, CLI, first - 1)
    extra = {"canvas": {"author": name, "from": first, "to": last, "counts": dict(notice.get("counts") or {}), "batches": list(notice.get("batches") or [])}}
    from herdr_team import roster as _roster

    try:
        return _roster.append_system_record(team, "canvas_changed", text, to=to, extra=extra, socket=os.fspath(layout.socket))
    except (HerdrTeamError, OSError) as err:
        _log.warning("canvas_changed not posted for %s: %s", team.name, err)
        return None


def flush_notices(layout: Any, team: TeamPaths, now: Optional[float] = None) -> List[int]:
    """Post every pending ``canvas_changed`` summary whose 60 s window has passed; the board seqs posted."""
    moment = time.time() if now is None else float(now)
    path = _file(team, NOTICES_FILE)
    if not team.root.is_dir() or not path.is_file():
        return []
    try:
        doc = store.RosterStore(team).load()
        if not _features.team_switch(layout.session, team, doc).on:
            return []
        lock = _canvas_lock(team, timeout=1.0)
        lock.acquire()
    except (HerdrTeamError, OSError):
        return []
    due: List[Dict[str, Any]] = []
    try:
        notices = store.read_json(path, default=None)
        authors = dict(notices.get("authors")) if isinstance(notices, dict) and isinstance(notices.get("authors"), dict) else {}
        for name, entry in authors.items():
            if not isinstance(entry, dict) or not isinstance(entry.get("pending"), dict):
                continue
            last = entry.get("last_at")
            if _is_number(last) and moment - float(last) < NOTICE_INTERVAL_S:
                continue
            due.append(dict(entry["pending"], author=name, kind=entry.get("kind") or KIND_MEMBER))
            authors[name] = {"last_at": moment, "pending": None, "kind": entry.get("kind")}
        if due:
            store.write_json(path, {"v": SCHEMA, "authors": authors}, fsync=False)
    finally:
        lock.release()
    posted = []
    for notice in due:
        seq = _post_changed(layout, team, doc, notice)
        if seq is not None:
            posted.append(seq)
    return posted


def _pointer_text(scene_elements: Dict[str, Dict[str, Any]], comment: Dict[str, Any]) -> str:
    """``C-4 on E-3 "Price rise in March"`` or ``C-4 at c11r6``."""
    on = comment.get("on")
    target = scene_elements.get(on) if isinstance(on, str) else None
    if target is not None:
        label = str(target.get("text") or "")
        return "{} on {}{}".format(comment["id"], on, " " + _q(label, 60) if label else "")
    point = comment.get("point") if isinstance(comment.get("point"), list) else [comment.get("x") or 0, comment.get("y") or 0]
    return "{} at {}".format(comment["id"], cell_name(point[0], point[1]))


def _post_mention(layout: Any, team: TeamPaths, doc: Dict[str, Any], author: CanvasAuthor, comment: Dict[str, Any], version: int,
                  state: _State) -> Optional[int]:
    to = [name for name in comment.get("mentions") or [] if name != author.name]
    if not to:
        return None
    pointer = _pointer_text(state.elements, comment)
    body = _q(comment.get("text") or "")
    look = "{} canvas look --around {}".format(CLI, comment["id"])
    if author.is_human:
        text = "The operator mentioned you on the canvas: {}: {}. Look: {}".format(pointer, body, look)
    else:
        text = ("{} mentioned you on the canvas: {}: {}. A peer's request, not an order. Answer with {} canvas comment {} \"…\" "
                "or look: {}").format(author.name, pointer, body, CLI, comment["id"], look)
    extra = {"canvas": {"by": author.name, "kind": "mention", "comment": comment["id"], "on": comment.get("on"), "version": version}}
    from herdr_team import roster as _roster

    try:
        return _roster.append_system_record(team, "canvas_sent", text, to=to, extra=extra, socket=os.fspath(layout.socket))
    except (HerdrTeamError, OSError) as err:
        _log.warning("canvas_sent not posted for %s: %s", team.name, err)
        return None


def send_to_member(layout: Any, team: TeamPaths, ids: Sequence[str], to: str, note: Optional[str], author: CanvasAuthor) -> Dict[str, Any]:
    """The operator's "send to member": render the selection and post ``canvas_sent``; ``{"seq", "image"}``."""
    doc = store.RosterStore(team).load()
    _features.require_on(layout.session, team, doc)
    if not author.operator:
        raise _error("operator_only", "sending part of the canvas to a member is for the operator")
    if _member_row(doc, str(to)) is None:
        raise _error("member_not_found", "{} is not an agent member of team {}".format(to, team.name), name=to,
                     roster=[row.get("name") for row in _agent_rows(doc)])
    if not isinstance(ids, (list, tuple)) or not ids:
        raise _invalid("elements", "send needs at least one element")
    state = _load_state(team)
    chosen: List[Dict[str, Any]] = []
    for ref in ids:
        el = _lookup_in(state, ref, author.name, "elements")
        if all(c["id"] != el["id"] for c in chosen):
            chosen.append(el)
    clean_note = _text(note, "note", MAX_LABEL_CHARS, "MAX_LABEL_CHARS", one_line=True) if note else ""
    boxes = [bounds(el) for el in chosen]
    region = [min(b[0] for b in boxes) - 40, min(b[1] for b in boxes) - 40, max(b[2] for b in boxes) + 40, max(b[3] for b in boxes) + 40]
    scene = state.to_scene()
    key = hashlib.sha256(json.dumps([sorted(e["id"] for e in chosen), region], separators=(",", ":")).encode("utf-8")).hexdigest()[:8]
    rendered = _render.render_region(team, scene, region, _dir(team) / RENDERS_DIR, "send-{}-v{}-{}".format(to, state.version, key), marks=True)
    image = rendered.get("png") or None
    listing = text_form(scene, [e["id"] for e in chosen], reader=str(to))
    if len(listing) > MAX_SEND_TEXT_CHARS:
        listing = listing[: MAX_SEND_TEXT_CHARS - 1].rstrip() + "…"
    first = chosen[0]
    names = ", ".join(e["id"] for e in chosen[:8]) + (", …" if len(chosen) > 8 else "")
    who = "The operator" if author.is_human else author.name
    picture = "Image: {}".format(image) if image else "Image: unavailable ({}); svg: {}".format(rendered.get("image_error"), rendered.get("svg"))
    text = "{} sent you part of the canvas ({} around {}){}. {}. {}. Look: {} canvas look --around {}".format(
        who, names, cell_name(first.get("x") or 0, first.get("y") or 0), ": " + _q(clean_note) if clean_note else "",
        listing.replace("\n", " · "), picture, CLI, first["id"])
    extra = {"canvas": {"by": author.name, "kind": "send", "elements": [e["id"] for e in chosen], "image": image,
                        "note": clean_note or None, "version": state.version}}
    from herdr_team import roster as _roster

    seq = _roster.append_system_record(team, "canvas_sent", text, to=[str(to)], extra=extra, socket=os.fspath(layout.socket))
    return {"seq": seq, "image": image, "svg": rendered.get("svg"), "elements": [e["id"] for e in chosen]}


# --------------------------------------------------------------------------
# since you looked (contract 8.4)


def _cursor_path(team: TeamPaths, reader: str) -> Path:
    if not isinstance(reader, str) or not FILE_STEM_RE.match(reader):
        raise _error("name_invalid", "reader {!r} is not a member name or human".format(reader), reader=reader)
    return _dir(team) / CURSORS_DIR / (reader + ".json")


def cursor(team: TeamPaths, reader: str) -> int:
    """The version ``reader`` last looked at (0 when it never did)."""
    doc = store.read_json(_cursor_path(team, reader), default=None)
    return int(doc["version"]) if isinstance(doc, dict) and _is_number(doc.get("version")) else 0


def advance_cursor(team: TeamPaths, reader: str, version: int) -> None:
    """Record that ``reader`` has seen the canvas up to ``version``."""
    _team_dir_exists(team)
    path = _cursor_path(team, reader)
    ensure_dir(path.parent)
    store.write_json(path, {"version": int(version), "at": store.now_iso()}, fsync=False)


def _since_value(since: Any) -> int:
    if isinstance(since, str) and since.strip().isdigit():
        return int(since.strip())
    if _is_number(since) and int(since) == since and since >= 0:
        return int(since)
    raise _error("usage", "since is a version number or last", since=since)


# --------------------------------------------------------------------------
# the text form (contract 8.1)

CELL_TYPES = frozenset(TEXT_TYPES | {"frame", "svg", "chart", "viz", "mermaid", "image"})


def _bounds_text(el: Dict[str, Any]) -> str:
    return "[{},{} {}x{}]".format(el.get("x"), el.get("y"), el.get("w"), el.get("h"))


def _color_name(value: Any) -> str:
    for name, hex_value in COLORS.items():
        if hex_value == value:
            return name
    return str(value or "")


def _end_name(el: Dict[str, Any], key: str, index: int) -> str:
    if el.get(key):
        return str(el[key])
    points = el.get("points") or []
    if points:
        point = points[index]
        return cell_name(point[0], point[1])
    return "?"


def describe(el: Dict[str, Any], reader: Optional[str] = None, full: bool = True) -> str:
    """One element as ``look`` prints it: ``full`` adds the cell (for boxy things) and `` — intent``."""
    kind = str(el.get("type"))
    eid = str(el.get("id"))
    who = _who(el.get("author"), reader)
    text = str(el.get("text") or "")
    limit = 0 if full else 80
    if kind == "comment":
        point = el.get("point") if isinstance(el.get("point"), list) else [el.get("x") or 0, el.get("y") or 0]
        where = "on {}".format(el["on"]) if el.get("on") else "at {}".format(cell_name(point[0], point[1]))
        if el.get("reply_to"):
            where = "reply to {} {}".format(el["reply_to"], where)
        mentions = " → " + ", ".join("@" + str(m) for m in el.get("mentions") or []) if el.get("mentions") else ""
        status = "(resolved by {})".format(_who(el.get("resolved_by"), reader)) if el.get("resolved") else "(open)"
        return "{} comment {} by {}{}: {} {}".format(eid, where, who, mentions, _q(text, 0 if full else 60), status)
    if kind == "arrow":
        core = "{} arrow {} → {}{}".format(eid, _end_name(el, "from", 0), _end_name(el, "to", -1), " " + _q(text, limit) if text else "")
    elif kind == "pen":
        style = el.get("style") if isinstance(el.get("style"), dict) else {}
        core = "{} pen {} pts{} {} {}".format(eid, len(el.get("points") or []), " closed" if el.get("closed") else "",
                                               _color_name(style.get("stroke")), _bounds_text(el))
    elif kind == "path":
        core = "{} path {}".format(eid, _bounds_text(el))
    elif kind == "chart":
        core = "{} chart{} {} {}".format(eid, " " + _q(text, limit) if text else "", _bounds_text(el),
                                         "data {}".format(el["data"]) if el.get("data") else "inline data")
    elif kind == "viz":
        libs = ", ".join(str(lib) for lib in el.get("libs") or [])
        core = "{} viz {} ({}) {}".format(eid, _q(text, limit), "live: " + libs if libs else "live", _bounds_text(el))
    elif kind == "mermaid":
        lines = len(str(el.get("source") or "").strip().splitlines())
        core = "{} mermaid {}{} ({} lines) {}".format(eid, el.get("diagram") or "other", " " + _q(text, limit) if text else "", lines, _bounds_text(el))
    elif kind == "image":
        core = "{} image {}x{}px {}".format(eid, el.get("px_w"), el.get("px_h"), _bounds_text(el))
    else:
        core = "{} {}{} {}".format(eid, kind, " " + _q(text, limit) if text else "", _bounds_text(el))
    if full and kind in CELL_TYPES:
        core += " " + cell_name(el.get("x") or 0, el.get("y") or 0)
    line = "{} by {}".format(core, who)
    intent = el.get("intent")
    if full and intent and intent != HUMAN_INTENT:
        line += " — {}".format(intent)
    return line


def _tree_lines(elements: Sequence[Dict[str, Any]], reader: Optional[str], base: int = 1) -> List[str]:
    """Full lines with children indented under their frame (comments last)."""
    ids = {el["id"] for el in elements}
    ordered = sorted(elements, key=lambda e: (e.get("type") == "comment", int(e.get("z") or 0), _id_number(e.get("id"))))
    children: Dict[str, List[Dict[str, Any]]] = {}
    roots: List[Dict[str, Any]] = []
    for el in ordered:
        parent = el.get("frame")
        if isinstance(parent, str) and parent in ids and parent != el["id"]:
            children.setdefault(parent, []).append(el)
        else:
            roots.append(el)
    lines: List[str] = []
    seen = set()

    by_id = {el["id"]: el for el in elements}

    def walk(el: Dict[str, Any], depth: int) -> None:
        if el["id"] in seen:
            return
        seen.add(el["id"])
        line = describe(el, reader, full=True)
        parent = by_id.get(el.get("frame")) if isinstance(el.get("frame"), str) else None
        intent = el.get("intent")
        if parent is not None and intent and intent == parent.get("intent") and line.endswith(" — {}".format(intent)):
            line = line[: -len(" — {}".format(intent))]  # a graph's nodes repeat the graph's intent: say it once
        lines.append("  " * depth + line)
        for child in children.get(el["id"], []):
            walk(child, depth + 1)

    for root in roots:
        walk(root, base)
    for el in ordered:  # anything caught in a frame cycle still gets a line
        walk(el, base)
    return lines


def text_form(scene: Dict[str, Any], ids: Sequence[str], reader: Optional[str] = None) -> str:
    """The text-form lines of the given elements, as ``look`` prints them."""
    wanted = set(ids)
    chosen = [el for el in scene.get("elements") or [] if isinstance(el, dict) and el.get("id") in wanted]
    return "\n".join(_tree_lines(chosen, reader, base=0))


def _ids_text(ids: Sequence[str], limit: int = 5) -> str:
    ids = [str(i) for i in ids]
    if not ids:
        return ""
    numbers = [_id_number(i) for i in ids]
    prefixes = {i.split("-", 1)[0] for i in ids}
    if len(ids) >= 3 and len(prefixes) == 1 and all(b == a + 1 for a, b in zip(numbers, numbers[1:])):
        return "{}..{}".format(ids[0], ids[-1])
    if len(ids) > limit:
        return "{} and {} more".format(", ".join(ids[:limit]), len(ids) - limit)
    return ", ".join(ids)


def _over(state: _State, el: Dict[str, Any]) -> Optional[str]:
    """The smallest other element a stroke was drawn over (frames only when nothing else is under it)."""
    box = bounds(el)
    best: Optional[Tuple[int, float, str]] = None
    for other in state.elements.values():
        if other["id"] == el["id"] or other.get("type") == "comment" or not _intersects(box, bounds(other)):
            continue
        ox0, oy0, ox1, oy1 = bounds(other)
        # Shapes first, then arrows and strokes, frames last; the smallest of its kind.
        tier = 2 if other.get("type") == "frame" else (1 if other.get("type") in ("arrow", "pen", "path") else 0)
        rank = (tier, (ox1 - ox0) * (oy1 - oy0), other["id"])
        if best is None or rank < best:
            best = rank
    return best[2] if best else None


def summarize(event: Dict[str, Any], state: Optional[_State] = None) -> str:
    """What one event did, in a few words: ``added E-19 pen over E-5``, ``moved E-3``, ``commented C-4 on E-3 → @skeptic``."""
    op = event.get("op")
    ids = [str(i) for i in event.get("ids") or []]
    changes = [c for c in event.get("changes") or [] if isinstance(c, dict)]
    added = [c["value"] for c in changes if c.get("target") == "element" and c.get("action") == "add" and isinstance(c.get("value"), dict)]
    if op == "clear":
        return "cleared the canvas"
    if op in ("graph", "mermaid") and len(added) > 1:
        frame = added[0]
        name = frame.get("alias") or frame.get("text") or op
        return "added {} {} {} ({} elements)".format(_ids_text([a["id"] for a in added]), op, _q(name, 60), len(added))
    if op in ("shape", "arrow", "frame", "pen", "path", "svg", "mermaid", "chart", "viz", "image") and added:
        el = added[0]
        kind = el.get("type")
        detail = ""
        if kind == "arrow":
            detail = " {} → {}".format(_end_name(el, "from", 0), _end_name(el, "to", -1))
        elif el.get("text"):
            detail = " " + _q(el["text"], 60)
        if kind in ("pen", "path") and state is not None:
            over = _over(state, el)
            if over:
                detail += " over " + over
        elif el.get("frame"):
            detail += " in " + str(el["frame"])
        if op == "frame" and len(ids) > 1:
            detail += " around {} element{}".format(len(ids) - 1, "" if len(ids) == 2 else "s")
        return "added {} {}{}".format(el["id"], kind, detail)
    if op == "comment" and added:
        el = added[0]
        if el.get("reply_to"):
            base = "replied {} to {}".format(el["id"], el["reply_to"])
        elif el.get("on"):
            base = "commented {} on {}".format(el["id"], el["on"])
        else:
            point = el.get("point") or [el.get("x") or 0, el.get("y") or 0]
            base = "commented {} at {}".format(el["id"], cell_name(point[0], point[1]))
        if el.get("mentions"):
            base += " → " + ", ".join("@" + str(m) for m in el["mentions"])
        return base
    if op == "claim":
        claim = next((c.get("value") for c in changes if c.get("target") == "claim" and c.get("action") == "add"), None) or {}
        released = [c["id"] for c in changes if c.get("target") == "claim" and c.get("action") == "delete"]
        region = claim.get("region") or [0, 0, 1, 1]
        base = "claimed {} {} {}".format(claim.get("id"), region_cells(region), _q(claim.get("label"), 60))
        return base + (", released " + ", ".join(released) if released else "")
    if op == "legend":
        entry = next((c.get("value") for c in changes if c.get("target") == "legend" and c.get("action") == "add"), None)
        if entry:
            return "added legend {}: {} = {}".format(entry.get("id"), entry.get("symbol"), _q(entry.get("meaning"), 60))
        return "removed legend {}".format(_ids_text(ids))
    if op == "lock":
        lock = next((c.get("value") for c in changes if c.get("target") == "lock"), None) or {}
        return "locked {} {} {}".format(lock.get("id"), region_cells(lock.get("region") or [0, 0, 1, 1]), _q(lock.get("label"), 60))
    if op == "portrait":
        info = event.get("portrait") if isinstance(event.get("portrait"), dict) else {}
        frame = next((c["id"] for c in changes if c.get("target") == "element" and isinstance(c.get("value"), dict)
                      and c["value"].get("type") == "frame"), ids[0] if ids else "")
        step = " (step {}/{})".format(info["current"], info["total"]) if info.get("current") else ""
        return "updated its portrait {}{}".format(frame, step)
    if op == "undo":
        return "undid {}".format(event.get("undoes") or "a batch")
    verbs = {"release": "released", "move": "moved", "restyle": "restyled", "edit": "edited", "delete": "deleted",
             "resolve": "resolved", "unlock": "unlocked"}
    return "{} {}".format(verbs.get(str(op), str(op)), _ids_text(ids)).strip()


def _change_entry(event: Dict[str, Any], state: Optional[_State]) -> Dict[str, Any]:
    author = event.get("author") if isinstance(event.get("author"), dict) else {}
    return {"seq": int(event["seq"]), "author": author.get("name"), "op": event.get("op"), "ids": list(event.get("ids") or []),
            "intent": event.get("intent"), "summary": summarize(event, state)}


def change_line(change: Dict[str, Any], reader: Optional[str]) -> str:
    """``v38 alpha-reviewer added E-19 pen over E-5 — I disagree``."""
    intent = change.get("intent")
    tail = " — {}".format(intent) if intent and intent != HUMAN_INTENT and change.get("op") != "clear" else ""
    return "v{} {} {}{}".format(change.get("seq"), _who(change.get("author"), reader), change.get("summary"), tail)


def _changes_block(result: Dict[str, Any], reader: Optional[str]) -> List[str]:
    since = result.get("since")
    changes = result.get("changes") or []
    if since is None:
        return []
    if not changes:
        lines = ["changes since v{}: none".format(since)]
    else:
        lines = ["changes since v{} ({}):".format(since, len(changes))]
        shown = changes[-LOOK_CHANGES_MAX:]
        if len(changes) > len(shown):
            lines.append("  … {} earlier changes: {} canvas changes --since {}".format(len(changes) - len(shown), CLI, since))
        lines += ["  " + change_line(c, reader) for c in shown]
    if result.get("reset"):
        lines.append("  (the canvas was cleared since then; what follows is the whole canvas now)")
    if result.get("complete") is False and changes:
        lines.append("  … more: {} canvas changes --since {}".format(CLI, changes[-1].get("seq")))
    return lines


def _direction(origin: Sequence[float], target: Sequence[float]) -> str:
    dx, dy = float(target[0]) - float(origin[0]), float(target[1]) - float(origin[1])
    if math.hypot(dx, dy) < 50:
        return "here"
    angle = math.degrees(math.atan2(-dy, dx)) % 360
    names = ("east", "north-east", "north", "north-west", "west", "south-west", "south", "south-east")
    return names[int(((angle + 22.5) % 360) // 45)]


def _clusters(elements: Sequence[Dict[str, Any]], origin: Sequence[float]) -> List[Dict[str, Any]]:
    buckets: Dict[Tuple[str, int, int], List[Tuple[float, float]]] = {}
    for el in elements:
        cx, cy = _center(el)
        key = (str(el.get("author")), int(math.floor(cx / 1000.0)), int(math.floor(cy / 1000.0)))
        buckets.setdefault(key, []).append((cx, cy))
    out = []
    for (author, _bx, _by), centres in buckets.items():
        mx = sum(c[0] for c in centres) / len(centres)
        my = sum(c[1] for c in centres) / len(centres)
        out.append({"author": author, "count": len(centres), "center": [_round(mx), _round(my)], "cell": cell_name(mx, my),
                    "direction": _direction(origin, (mx, my))})
    out.sort(key=lambda c: (-c["count"], c["author"], c["cell"]))
    return out


def _claim_left(claim: Dict[str, Any]) -> str:
    expires = _parse_iso(claim.get("expires_at"))
    if expires is None:
        return "no expiry"
    minutes = int(math.ceil((expires - time.time()) / 60.0))
    return "{}m left".format(minutes) if minutes >= 1 else "<1m left"


def look_text(result: Dict[str, Any]) -> str:
    """The text listing of a ``look`` result (contract 8.1)."""
    reader = result.get("reader")
    lines = ["canvas of {} · v{} · {} elements · you are {}".format(
        result.get("team"), result.get("version"), result.get("total", len(result.get("elements") or [])),
        "the operator" if reader == HUMAN else reader)]
    claims = result.get("claims") or []
    if claims:
        lines.append("claims: " + "; ".join("{} {} {} {} ({})".format(c.get("id"), _who(c.get("author"), reader), _q(c.get("label"), 60),
                                                                  region_cells(c.get("region") or [0, 0, 1, 1]), _claim_left(c)) for c in claims))
    locks = result.get("locks") or []
    if locks:
        lines.append("locks: " + "; ".join("{} by {} {} {}".format(lock.get("id"), _lock_by(lock) if lock.get("by") != reader else "you",
                                                                   _q(lock.get("label"), 60), region_cells(lock.get("region") or [0, 0, 1, 1])) for lock in locks))
    legend = result.get("legend") or []
    if legend:
        lines.append("legend: " + "; ".join("{} {} = {} ({})".format(g.get("id"), g.get("symbol"), _q(g.get("meaning"), 80), _who(g.get("author"), reader))
                                            for g in legend))
    lines += _changes_block(result, reader)
    full = result.get("elements") or []
    region = result.get("region")
    printed = {el.get("id") for el in full}
    if region:
        lines.append("region {}:".format(result.get("region_cells") or region_cells(region)))
        lines += _tree_lines(full, reader) or ["  (nothing here)"]
    elif result.get("level") == "full":
        lines.append("elements:")
        lines += _tree_lines(full, reader) or ["  (the canvas is empty)"]
    elsewhere = result.get("elsewhere") or []
    if elsewhere:
        lines.append("elsewhere:" if region else "elements (one line each):")
        lines += ["  " + str(entry.get("line") or entry.get("id")) for entry in elsewhere]
    clusters = result.get("clusters") or []
    if clusters:
        lines.append("nearby:" if region else "more:")
        for cluster in clusters:
            lines.append("  {} element{} by {}, {} (around {})".format(cluster["count"], "" if cluster["count"] == 1 else "s",
                                                                      _who(cluster["author"], reader), cluster["direction"], cluster["cell"]))
    if result.get("omitted"):
        lines.append("  … {} more elements".format(result["omitted"]))
    for_you = [c for c in result.get("comments_for_you") or [] if c.get("id") not in printed]
    if for_you:
        lines.append("for you:")
        lines += ["  " + describe(c, reader, full=True) for c in for_you]
    found = result.get("problems") or []
    if found:
        lines.append("problems ({}; each fix is an operation you can apply):".format(len(found)))
        lines += _check.problem_lines(found, _check.LOOK_MAX)
    if result.get("image"):
        lines.append("image: {}".format(result["image"]))
    elif result.get("image_error"):
        lines.append("image: none ({}){}".format(result["image_error"], "; svg: {}".format(result["svg"]) if result.get("svg") else ""))
    return "\n".join(lines)


def _exact_export(layout: Any, team: TeamPaths, region: Sequence[float], grid: bool, reader: str) -> Optional[str]:
    """Ask an open page for Excalidraw's own export and wait up to ``EXACT_WAIT_S``; None when no page answers."""
    if not (layout.session.root / "whiteboard.json").is_file():
        return None  # no page server, so nobody could answer
    request = request_export(team, list(region), True, grid, reader)
    path = _dir(team) / EXPORTS_DIR / (request + ".png")
    deadline = time.monotonic() + EXACT_WAIT_S
    while time.monotonic() < deadline:
        if path.is_file():
            return os.fspath(path)
        time.sleep(EXACT_POLL_S)
    if path.is_file():
        return os.fspath(path)
    try:
        os.unlink(_dir(team) / EXPORTS_DIR / (request + ".json"))  # withdrawn: a late page answer has nobody to go to
    except OSError:
        pass
    return None


def check(layout: Any, team: TeamPaths, reader: str, region: Any = None, around: Optional[str] = None, mine: bool = False,
          doc: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Every layout problem on the canvas (or in a region), each with ids and a fix to apply (``canvas_check``)."""
    doc = doc if isinstance(doc, dict) else store.RosterStore(team).load()
    _features.require_on(layout.session, team, doc)
    if around is not None and region is not None:
        raise _error("usage", "use --region or --around, not both")
    state = _load_state(team)
    state.drop_expired(time.time())
    elements = state.to_scene(time.time())["elements"]

    def lookup(ref: str, field: str) -> Dict[str, Any]:
        return _lookup_in(state, ref, reader, field)

    box: Optional[List[int]] = None
    if around is not None:
        x0, y0, x1, y1 = bounds(lookup(around, "around"))
        box = [_round(x0 - AROUND_MARGIN), _round(y0 - AROUND_MARGIN), _round(x1 + AROUND_MARGIN), _round(y1 + AROUND_MARGIN)]
    elif region is not None:
        box = _region(region, "region", lookup)
    found = _check.problems(elements, reader, box)
    if mine:
        found = [p for p in found if p["yours"]]
    head = "canvas of {} · v{} · {}".format(team.name, state.version, "{} problem{}".format(len(found), "" if len(found) == 1 else "s") if found else "no layout problems")
    lines = [head + (" in {}".format(region_cells(box)) if box else "") + (" (yours only)" if mine else "")]
    lines += _check.problem_lines(found)
    if found:
        lines.append("apply a fix: {} canvas draw --op '<fix>' (or canvas_draw); then check again, and look --image for a last visual pass".format(CLI))
    return {"team": team.name, "version": state.version, "reader": reader, "region": box, "mine": bool(mine),
            "problems": found, "text": "\n".join(lines)}


def look(layout: Any, team: TeamPaths, reader: str, region: Any = None, around: Optional[str] = None, since: Any = None,
         image: bool = False, grid: bool = False, exact: bool = False, advance: bool = True,
         doc: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """What ``reader`` sees (contract 8.1 and 8.2): the three-level listing, changes, claims, legend, and an optional image."""
    doc = doc if isinstance(doc, dict) else store.RosterStore(team).load()
    switch = _features.require_on(layout.session, team, doc)
    _cursor_path(team, reader)  # validates the reader name before anything is written under it
    if around is not None and region is not None:
        raise _error("usage", "use --region or --around, not both")
    now = time.time()
    state = _load_state(team)
    state.drop_expired(now)
    scene = state.to_scene(now)
    elements = scene["elements"]

    def lookup(ref: str, field: str) -> Dict[str, Any]:
        return _lookup_in(state, ref, reader, field)

    box: Optional[List[int]] = None
    if around is not None:
        x0, y0, x1, y1 = bounds(lookup(around, "around"))
        box = [_round(x0 - AROUND_MARGIN), _round(y0 - AROUND_MARGIN), _round(x1 + AROUND_MARGIN), _round(y1 + AROUND_MARGIN)]
    elif region is not None:
        box = _region(region, "region", lookup)
    view = box if box is not None else _render.view_box(scene)
    origin = ((view[0] + view[2]) / 2.0, (view[1] + view[3]) / 2.0)
    if box is not None:
        full = [el for el in elements if _intersects(bounds(el), box)]
        rest = sorted((el for el in elements if not _intersects(bounds(el), box)),
                      key=lambda el: math.hypot(_center(el)[0] - origin[0], _center(el)[1] - origin[1]))
        level = "full"
    elif len(elements) <= LOOK_FULL_MAX:
        full, rest, level = list(elements), [], "full"
    else:
        full = []
        rest = [el for el in elements if el.get("type") == "frame"] + [el for el in elements if el.get("type") != "frame"]
        level = "overview"
    one_line, remainder = rest[:LOOK_LINE_MAX], rest[LOOK_LINE_MAX:]
    clusters = _clusters(remainder, origin)
    omitted = sum(c["count"] for c in clusters[MAX_CLUSTERS:])
    elsewhere = [{"id": el["id"], "type": el.get("type"), "bounds": [el.get("x"), el.get("y"), el.get("w"), el.get("h")],
                  "cell": cell_name(el.get("x") or 0, el.get("y") or 0), "text": el.get("text") or "", "author": el.get("author"),
                  "line": describe(el, reader, full=False)} for el in one_line]
    since_version: Optional[int] = None
    changes: List[Dict[str, Any]] = []
    complete, reset = True, False
    if since is not None:
        since_version = cursor(team, reader) if since == "last" else _since_value(since)
        got = changes_since(team, since_version)
        changes = [_change_entry(event, state) for event in got["events"]]
        complete, reset = got["complete"], got["reset"]
    result: Dict[str, Any] = {
        "team": team.name, "version": state.version, "reader": reader, "switch": switch.to_json(),
        "region": box, "region_cells": region_cells(box) if box else None, "level": level, "total": len(elements),
        "elements": full, "elsewhere": elsewhere, "clusters": clusters[:MAX_CLUSTERS], "omitted": omitted,
        "since": since_version, "changes": changes, "complete": complete, "reset": reset,
        "claims": scene["claims"], "locks": scene["locks"], "legend": scene["legend"],
        "comments_for_you": [el for el in elements if el.get("type") == "comment" and not el.get("resolved") and reader in (el.get("mentions") or [])],
        "image": None, "svg": None, "image_error": None, "exact": False,
        "problems": _check.problems(elements, reader, box),
    }
    if image:
        if exact:
            png = _exact_export(layout, team, view, grid, reader)
            if png:
                result.update(image=png, exact=True)
            else:
                result["image_error"] = "exact_timeout"
        if not result["exact"]:
            key = hashlib.sha256(json.dumps([box, bool(grid)], separators=(",", ":")).encode("utf-8")).hexdigest()[:8]
            rendered = _render.render_region(team, scene, box, _dir(team) / RENDERS_DIR, "look-{}-v{}-{}".format(reader, state.version, key),
                                             marks=True, grid=grid, reader=reader)
            result["image"] = rendered["png"]
            result["svg"] = rendered["svg"]
            result["image_error"] = result["image_error"] or rendered["image_error"]
    result["text"] = look_text(result)
    if advance:
        advance_cursor(team, reader, state.version)
    return result


def read_changes(layout: Any, team: TeamPaths, reader: str, since: Any = "last", advance: bool = True,
                 doc: Optional[Dict[str, Any]] = None, limit: int = 500) -> Dict[str, Any]:
    """``canvas changes`` and MCP ``canvas_changes``: summaries after ``since`` (default: the reader's cursor), then advance it."""
    doc = doc if isinstance(doc, dict) else store.RosterStore(team).load()
    _features.require_on(layout.session, team, doc)
    _cursor_path(team, reader)
    since_version = cursor(team, reader) if since in (None, "last") else _since_value(since)
    got = changes_since(team, since_version, limit=limit)
    state = _load_state(team)
    changes = [_change_entry(event, state) for event in got["events"]]
    result = {"team": team.name, "version": got["version"], "since": since_version, "reader": reader, "changes": changes,
              "complete": got["complete"], "reset": got["reset"]}
    lines = ["canvas of {} · v{}".format(team.name, got["version"])] + _changes_block(result, reader)
    result["text"] = "\n".join(lines)
    if advance:
        advance_cursor(team, reader, changes[-1]["seq"] if changes and not got["complete"] else got["version"])
    return result


def apply_text(result: Dict[str, Any]) -> str:
    """The human text of an apply result: ``v44 · B-12 · applied 6, refused 1`` then one line per op."""
    lines = ["v{} · {} · applied {}, refused {}".format(result.get("version"), result.get("batch") or "no batch",
                                                       len(result.get("applied") or []), len(result.get("refused") or []))]
    for entry in result.get("applied") or []:
        alias = " ({})".format(entry["alias"]) if entry.get("alias") else ""
        lines.append("#{} {} {}{}".format(entry.get("index"), entry.get("op"), _ids_text(entry.get("ids") or []) or "-", alias))
    if result.get("refused"):
        lines.append("refused:")
        lines += ["  #{} {} {}: {}".format(r.get("index"), r.get("op"), r.get("code"), r.get("message")) for r in result["refused"]]
    if result.get("warnings"):
        lines.append("warnings:")
        lines += ["  #{} {}: {}".format(w.get("index"), w.get("code"), w.get("message")) for w in result["warnings"]]
    notices = result.get("notices") or {}
    if notices.get("canvas_sent"):
        lines.append("sent: board #{}".format(", #".join(str(s) for s in notices["canvas_sent"])))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# assets, stills and exports (what the page server reads and writes)

_ASSET_KINDS = {"png": ("png", "image/png"), "jpeg": ("jpg", "image/jpeg"), "svg": ("svg", "image/svg+xml"),
                "html": ("html", "text/html"), "json": ("vl.json", "application/json")}


def store_asset(team: TeamPaths, data: bytes, kind: str) -> Dict[str, Any]:
    """Store bytes content-addressed under ``assets/``; ``{"asset", "mime", "px_w", "px_h"}`` (images checked by magic bytes)."""
    _team_dir_exists(team)
    if not isinstance(data, (bytes, bytearray)):
        raise _invalid("asset", "asset data must be bytes")
    data = bytes(data)
    px_w = px_h = None
    if kind in ("png", "jpeg", "image"):
        if len(data) > MAX_IMAGE_BYTES:
            raise _error("image_refused", "the image is over {} MB".format(MAX_IMAGE_BYTES // (1024 * 1024)), limit="MAX_IMAGE_BYTES", max=MAX_IMAGE_BYTES)
        sniffed = _render.sniff_image(data)
        if sniffed is None or (kind == "png" and sniffed[0] != "image/png") or (kind == "jpeg" and sniffed[0] != "image/jpeg"):
            raise _error("image_refused", "not a {} (checked by its bytes, not its name)".format("PNG or JPEG" if kind == "image" else kind.upper()))
        mime, px_w, px_h = sniffed
        ext = "png" if mime == "image/png" else "jpg"
    elif kind == "svg":
        try:
            markup = data.decode("utf-8")
        except UnicodeDecodeError:
            raise _render._svg_refused("not_utf8", "not UTF-8")
        data = _render.sanitize_svg(markup).encode("utf-8")
        ext, mime = _ASSET_KINDS["svg"]
    elif kind == "html":
        if len(data) > MAX_VIZ_BYTES:
            raise _too_big("html", "MAX_VIZ_BYTES", MAX_VIZ_BYTES, "the viz html is over {} KB".format(MAX_VIZ_BYTES // 1024))
        ext, mime = _ASSET_KINDS["html"]
    elif kind == "json":
        if len(data) > MAX_CHART_SPEC_BYTES:
            raise _error("chart_refused", "the chart spec is over {} KB".format(MAX_CHART_SPEC_BYTES // 1024), limit="MAX_CHART_SPEC_BYTES", max=MAX_CHART_SPEC_BYTES)
        try:
            json.loads(data.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            raise _error("chart_refused", "the chart spec is not JSON")
        ext, mime = _ASSET_KINDS["json"]
    else:
        raise _invalid("kind", "asset kind is png, jpeg, image, svg, html or json")
    name = "{}.{}".format(hashlib.sha256(data).hexdigest()[:32], ext)
    path = _dir(team) / ASSETS_DIR / name
    check_not_symlink(path)
    if not path.is_file():
        store.atomic_write(path, data, fsync=False)
    return {"asset": name, "mime": mime, "px_w": px_w, "px_h": px_h}


def _regular_file(path: Path) -> bool:
    try:
        st = os.lstat(path)
    except OSError:
        return False
    return stat.S_ISREG(st.st_mode)


def asset_path(team: TeamPaths, name: str) -> Path:
    """The validated path of a stored asset (no traversal, no symlinks); ``element_unknown`` when absent."""
    if not isinstance(name, str) or not _ASSET_RE.match(name):
        raise _error("element_unknown", "{!r} is not an asset name".format(str(name)[:80]), asset=name)
    path = _dir(team) / ASSETS_DIR / name
    if not _regular_file(path):
        raise _error("element_unknown", "no asset called {}".format(name), asset=name)
    return path


def _still_name(element_id: str, version: int) -> str:
    return "{}-v{}.png".format(element_id, int(version))


def store_still(team: TeamPaths, element_id: str, version: int, png: bytes) -> Path:
    """Keep a page-captured PNG of a chart, mermaid or viz element at its ``updated_seq``."""
    if not isinstance(element_id, str) or not _ELEMENT_ID_RE.match(element_id):
        raise _error("element_unknown", "{!r} is not an element id".format(element_id), id=element_id)
    if not _is_number(version) or int(version) < 1:
        raise _invalid("v", "a still needs the element's version (updated_seq)")
    if not isinstance(png, (bytes, bytearray)) or not bytes(png).startswith(_render.PNG_MAGIC):
        raise _error("image_refused", "a still is a PNG")
    if len(png) > MAX_STILL_BYTES:
        raise _error("image_refused", "a still is at most {} MB".format(MAX_STILL_BYTES // (1024 * 1024)), limit="MAX_STILL_BYTES", max=MAX_STILL_BYTES)
    el = _load_state(team).elements.get(element_id)
    if el is None or el.get("type") not in ("chart", "mermaid", "viz"):
        raise _error("element_unknown", "{} is not a chart, mermaid or viz element on the canvas".format(element_id), id=element_id)
    directory = _dir(team) / STILLS_DIR
    path = directory / _still_name(element_id, int(version))
    store.atomic_write(path, bytes(png), fsync=False)
    prefix = element_id + "-v"
    try:
        for entry in os.listdir(directory):
            if entry.startswith(prefix) and entry.endswith(".png") and entry != path.name:
                stem = entry[len(prefix):-4]
                if stem.isdigit() and int(stem) < int(version):
                    os.unlink(directory / entry)
    except OSError:
        pass
    return path


def still_path(team: TeamPaths, element_id: str, version: int) -> Optional[Path]:
    """The still for that element version, when the page captured one."""
    if not isinstance(element_id, str) or not _ELEMENT_ID_RE.match(element_id) or not _is_number(version):
        return None
    path = _dir(team) / STILLS_DIR / _still_name(element_id, int(version))
    return path if _regular_file(path) else None


def viz_source(team: TeamPaths, element_id: str) -> Dict[str, Any]:
    """``{"html", "libs", "title", "version", "data", "data_path"}`` of a ``viz`` element, for the page server's sealed frame."""
    el = _load_state(team).elements.get(element_id) if isinstance(element_id, str) else None
    if el is None or el.get("type") != "viz":
        raise _error("element_unknown", "{} is not a viz element on the canvas".format(element_id), id=element_id)
    raw = store.read_bytes(asset_path(team, str(el.get("html_asset")))) or b""
    return {"html": raw.decode("utf-8", "replace"), "libs": list(el.get("libs") or []), "title": el.get("text") or "",
            "version": int(el.get("updated_seq") or 0), "data": el.get("data"), "data_path": el.get("data_path")}


def request_export(team: TeamPaths, region: List[float], marks: bool, grid: bool, by: str) -> str:
    """Ask an open page for Excalidraw's own export of ``region``; the request id (contract 8.5)."""
    _team_dir_exists(team)
    if not isinstance(region, (list, tuple)) or len(region) != 4 or not all(_is_number(v) for v in region):
        raise _invalid("region", "an export region is [x0, y0, x1, y1]")
    request_id = secrets.token_hex(8)
    store.write_json(_dir(team) / EXPORTS_DIR / (request_id + ".json"),
                     {"id": request_id, "region": [float(v) for v in region], "marks": bool(marks), "grid": bool(grid), "by": by,
                      "at": store.now_iso()}, fsync=False)
    return request_id


def pending_exports(team: TeamPaths, max_age_s: float = EXPORT_MAX_AGE_S) -> List[Dict[str, Any]]:
    """Export requests no page has answered yet (older ones are deleted)."""
    directory = _dir(team) / EXPORTS_DIR
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return []
    now = time.time()
    out = []
    for name in names:
        if not name.endswith(".json") or not _REQUEST_ID_RE.match(name[:-5]):
            continue
        request_id = name[:-5]
        doc = store.read_json(directory / name, default=None)
        at = _parse_iso(doc.get("at")) if isinstance(doc, dict) else None
        if at is None or now - at > max_age_s:
            for stale in (name, request_id + ".png"):
                try:
                    os.unlink(directory / stale)
                except OSError:
                    pass
            continue
        if not (directory / (request_id + ".png")).exists():
            out.append(doc)
    out.sort(key=lambda d: str(d.get("at")))
    return out


def complete_export(team: TeamPaths, request_id: str, png: bytes) -> Path:
    """Store the page's PNG answer to an export request."""
    if not isinstance(request_id, str) or not _REQUEST_ID_RE.match(request_id):
        raise _error("element_unknown", "{!r} is not an export request id".format(str(request_id)[:40]))
    directory = _dir(team) / EXPORTS_DIR
    if not _regular_file(directory / (request_id + ".json")):
        raise _error("element_unknown", "no pending export {}".format(request_id), request_id=request_id)
    if not isinstance(png, (bytes, bytearray)) or not bytes(png).startswith(_render.PNG_MAGIC):
        raise _error("image_refused", "an export is a PNG")
    if len(png) > MAX_EXPORT_BYTES:
        raise _error("image_refused", "an export is at most {} MB".format(MAX_EXPORT_BYTES // (1024 * 1024)), limit="MAX_EXPORT_BYTES", max=MAX_EXPORT_BYTES)
    path = directory / (request_id + ".png")
    store.atomic_write(path, bytes(png), fsync=False)
    return path


# --------------------------------------------------------------------------
# team artifacts (read only)


def artifacts_dir(layout: Any, team: TeamPaths, doc: Optional[Dict[str, Any]] = None) -> Optional[Path]:
    """The team's ``<project>/.herdr-synapse/<team>/artifacts`` directory, or None without a project dir."""
    from herdr_team import workdir as _workdir

    doc = doc if isinstance(doc, dict) else store.RosterStore(team).load()
    project = _workdir.project_dir_of(doc)
    if not project:
        return None
    return Path(_workdir.paths_for(project, team.name)["artifacts"])


def artifact_file(layout: Any, team: TeamPaths, rel: str, doc: Optional[Dict[str, Any]] = None) -> Path:
    """A chart data file under the artifacts dir (csv/tsv/json, <= 5 MB); ``path_refused`` / ``artifacts_unset`` otherwise."""
    root = artifacts_dir(layout, team, doc)
    if root is None:
        raise _error("artifacts_unset", "team {} has no project folder, so no artifacts/; the operator sets one with: {} project set <path>".format(team.name, CLI),
                     team=team.name)
    if not isinstance(rel, str) or not rel.strip():
        raise _error("path_refused", "data must name a file under artifacts/")
    text = rel.strip()
    if text.startswith("artifacts/"):
        text = text[len("artifacts/"):]
    parts = Path(text).parts
    if Path(text).is_absolute() or ".." in parts or "\\" in text or not parts:
        raise _error("path_refused", "{} must be a path relative to the team's artifacts/ folder".format(rel), path=rel)
    base = Path(os.path.realpath(os.fspath(root)))
    real = Path(os.path.realpath(os.fspath(root / text)))
    if not _under(real, base) or not real.is_file():
        raise _error("path_refused", "{} is not a file under the team's artifacts/ folder".format(rel), path=rel)
    if real.suffix.lower() not in (".csv", ".tsv", ".json"):
        raise _error("chart_refused", "chart and viz data are .csv, .tsv or .json files", path=rel)
    if real.stat().st_size > MAX_CHART_DATA_BYTES:
        raise _error("chart_refused", "{} is over {} MB".format(rel, MAX_CHART_DATA_BYTES // (1024 * 1024)), path=rel,
                     limit="MAX_CHART_DATA_BYTES", max=MAX_CHART_DATA_BYTES)
    return real


# --------------------------------------------------------------------------
# summary, clear, purge


def summary(team: TeamPaths) -> Dict[str, Any]:
    """``{"version", "elements", "comments_open", "claims_active", "updated_at"}`` for me, doctor and the views."""
    if not _dir(team).is_dir():
        return {"version": 0, "elements": 0, "comments_open": 0, "claims_active": 0, "updated_at": None}
    scene = load_scene(team)
    elements = scene["elements"]
    return {"version": scene["version"], "elements": len(elements),
            "comments_open": sum(1 for el in elements if el.get("type") == "comment" and not el.get("resolved")),
            "claims_active": len(scene["claims"]), "updated_at": scene["updated_at"]}


def clear(layout: Any, team: TeamPaths, by: str) -> Dict[str, Any]:
    """Archive the canvas under ``whiteboard/archive/<ts>/`` and start empty (ids and version keep counting)."""
    lock = _canvas_lock(team)
    with lock:
        state = _load_state(team)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        archive = _dir(team) / ARCHIVE_DIR / stamp
        suffix = 1
        while archive.exists():
            suffix += 1
            archive = _dir(team) / ARCHIVE_DIR / "{}-{}".format(stamp, suffix)
        ensure_dir(archive)
        for name in (EVENTS_FILE, SCENE_FILE, ASSETS_DIR, STILLS_DIR):
            source = _dir(team) / name
            if os.path.lexists(source):
                os.replace(source, archive / name)
        now = time.time()
        human = by == HUMAN
        event = {"v": SCHEMA, "seq": state.version + 1, "ts": _iso(now), "batch": None,
                 "author": {"name": by, "kind": KIND_HUMAN if human else KIND_MEMBER, "agent": None, "via": "cli", "verified": True},
                 "op": "clear", "index": 0, "intent": "cleared the canvas", "ids": [], "changes": [], "counters": dict(state.counters)}
        _append_events(team, [event])
        state.fold(event)
        _write_scene(team, state, now)
    return {"archived": os.fspath(archive), "version": state.version}


def purge(team: TeamPaths) -> bool:
    """Delete everything the team's canvas stored; True when something was deleted.

    ``whiteboard/mcp.json`` stays: members Synapse started carry
    ``--mcp-config`` pointing at it, and a controlled restart keeps that flag,
    so deleting it would relaunch them against a missing file. It holds only
    the launcher command. Without it the whole directory goes.
    """
    directory = _dir(team)
    try:
        st = os.lstat(directory)
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        raise _error("path_symlink", "refusing to delete {}: not a plain directory".format(directory), path=os.fspath(directory))
    keep = (_features.MCP_CONFIG_FILE, LOCK_FILE)
    lock = _canvas_lock(team)
    with lock:
        if not os.path.lexists(directory / _features.MCP_CONFIG_FILE):
            shutil.rmtree(directory)
            return True
        removed = False
        for name in sorted(os.listdir(directory)):
            if name in keep:
                continue
            path = directory / name
            if stat.S_ISDIR(os.lstat(path).st_mode):
                shutil.rmtree(path)
            else:
                os.unlink(path)
            removed = True
    return removed


def portrait_op(plan: Dict[str, Any], intent: str, title: Optional[str] = None) -> Dict[str, Any]:
    """A ``portrait`` op from an ``activity`` plan (``{"steps", "current", ...}``)."""
    raw_steps = plan.get("steps") if isinstance(plan, dict) else None
    steps: List[Dict[str, Any]] = []
    for step in raw_steps or []:
        if isinstance(step, dict) and step.get("text"):
            status = step.get("status") if step.get("status") in STEP_STATUSES else "pending"
            steps.append({"text": " ".join(str(step["text"]).split())[:MAX_LABEL_CHARS], "status": status})
        elif isinstance(step, str) and step.strip():
            steps.append({"text": " ".join(step.split())[:MAX_LABEL_CHARS], "status": "pending"})
    current = plan.get("current") if isinstance(plan, dict) and _is_number(plan.get("current")) else None
    if current is None:
        current = next((i + 1 for i, s in enumerate(steps) if s["status"] == "in_progress"), None)
    if len(steps) > MAX_PORTRAIT_STEPS:
        # Keep a window of the plan around the current step.
        anchor = (int(current) - 1) if current else 0
        start = max(0, min(anchor - 3, len(steps) - MAX_PORTRAIT_STEPS))
        steps = steps[start:start + MAX_PORTRAIT_STEPS]
        current = int(current) - start if current else None
        if current is not None and not 1 <= current <= len(steps):
            current = None
    op: Dict[str, Any] = {"op": "portrait", "steps": steps, "intent": intent}
    if current:
        op["current"] = int(current)
    if title:
        op["title"] = title
    return op
