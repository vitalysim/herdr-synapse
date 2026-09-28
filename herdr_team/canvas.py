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

Since 0.22 (canvas v2, ``.local/prd/canvas-v2-architecture.md``) a labelled
element is sized from its label before it is placed or laid out: its kind in
the ``canvas_kinds`` registry measures it with the bundled fonts' metrics
(``canvas_text``), an op's ``w``/``h`` are the minimum, and the element stores
a ``fit`` record. Colours come from a ``tone`` (``canvas_theme``), never from
the author. Growth makes room instead of overlap (section "making room", QA
R-1), and every arrow label gets a spot on its line clear of other marks and
labels (``canvas_labels``, section "arrow labels", QA R-3).
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
from herdr_team import canvas_display as _display
from herdr_team import canvas_kinds as _kinds
from herdr_team import canvas_labels as _labels
from herdr_team import canvas_layout as _layout
from herdr_team import canvas_mermaid as _mermaid
from herdr_team import canvas_render as _render
from herdr_team import canvas_text as _ctext
from herdr_team import canvas_theme as _theme
from herdr_team import features as _features
from herdr_team import sanitize as _sanitize
from herdr_team import store
from herdr_team.canvas_kinds import _common as _kc
# Op limits live with the op in its kind module (canvas v2 phase 1); re-exported here for callers and tests.
from herdr_team.canvas_kinds.arrow import MAX_ARROW_POINTS
from herdr_team.canvas_kinds.chart import MAX_CHART_SPEC_BYTES, MAX_SPEC_DEPTH
from herdr_team.canvas_kinds.diagram import MAX_GRAPH_EDGES, MAX_GRAPH_NODES, MAX_MERMAID_BYTES
from herdr_team.canvas_kinds.frame import FRAME_PAD, FRAME_TOP
from herdr_team.canvas_kinds.path import MAX_PATH_CHARS
from herdr_team.canvas_kinds.pen import MAX_PEN_POINTS
from herdr_team.canvas_kinds.viz import MAX_VIZ_BYTES, MAX_VIZ_DATA_BYTES, VIZ_LIBS
from herdr_team.errors import HerdrTeamError, exit_code_for
from herdr_team.paths import FILE_STEM_RE, TeamPaths, check_not_symlink, ensure_dir

SCHEMA = 1
GRID = _kc.GRID
HOME_W, HOME_H = 800, 600
HOME_STRIDE = 1000
HOME_TOP = -1000
DEFAULT_GAP = 40
AROUND_MARGIN = 200

MAX_ELEMENTS = 2000
MAX_BATCH_OPS = 100
MAX_BATCH_BYTES = 512 * 1024
MAX_OPS_PER_MINUTE = 300
MAX_TEXT_CHARS = 2000
MAX_LABEL_CHARS = 200
MAX_INTENT_CHARS = 200
MAX_COMMENT_CHARS = 1000
MAX_SVG_BYTES = 100 * 1024
MAX_SVG_NODES = 5000
MAX_CHART_DATA_BYTES = 5 * 1024 * 1024
MAX_IMAGE_BYTES = 5 * 1024 * 1024
MAX_STILL_BYTES = 2 * 1024 * 1024
MAX_EXPORT_BYTES = 8 * 1024 * 1024
MAX_CLAIMS_PER_AUTHOR = 3
CLAIM_TTL_S = 300
MAX_LEGEND = 50
MAX_PORTRAIT_STEPS = 12
MAX_COORD = 1_000_000
MAX_SIZE = _kc.MAX_SIZE
#: A free-text legend symbol ("red cross") and the operator's note on "send to member".
MAX_SYMBOL_CHARS = 80
MAX_SEND_TEXT_CHARS = 1500
MAX_BATCHES_KEPT = 500
#: What one applied op from an agent may append to ``events.jsonl`` (every touched element is written whole).
MAX_OP_CHANGE_BYTES = 2 * 1024 * 1024
#: What one agent author may append to the log in a rolling minute, next to ``MAX_OPS_PER_MINUTE``.
MAX_CHANGE_BYTES_PER_MINUTE = 8 * 1024 * 1024

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

#: The ops that act on any element rather than create one kind: ``canvas`` handles these itself. Every other op comes from
#: the kind registry (``canvas_kinds``: each module's ``OPS``); ``OPS`` is theirs by ``order``, then these (phase 1, 2.4).
CORE_OPS = ("claim", "release", "legend", "move", "restyle", "edit", "delete", "portrait", "resolve", "lock", "unlock", "undo", "refit",
            "patch", "place", "pin", "unpin")
ID_PATTERN = r"^(E|C|K|X|G|B)-([1-9][0-9]{0,6})\Z"
ALIAS_PATTERN = r"^[A-Za-z][A-Za-z0-9_.-]{0,63}\Z"
CELL_PATTERN = r"^c(-?[0-9]{1,5})r(-?[0-9]{1,5})\Z"

COLORS = _kc.COLORS
FILLS = _kc.FILLS
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

HEADS = _kc.HEADS
DASHES = _kc.DASHES
FONTS = ("hand", "normal", "code")
#: Colour by meaning (0.22): a tone picks an element's stroke, fill and label colour; a variant how strongly.
TONES = _theme.TONES
VARIANTS = _theme.VARIANTS
WIDTHS = {"thin": 1, "bold": 2, "extra": 4, "1": 1, "2": 2, "4": 4}
SIZES = {"s": 16, "m": 20, "l": 28, "xl": 36, "16": 16, "20": 20, "28": 28, "36": 36}
TEXT_MAX_W = 600
NODE_W, NODE_H = _layout.NODE_W, _layout.NODE_H
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


def _blocks() -> Any:
    """``canvas_blocks`` (it imports this module, so it is imported late)."""
    from herdr_team import canvas_blocks

    return canvas_blocks


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


STYLE_FIELDS = ("color", "fill", "width", "dash", "opacity", "font", "size", "rough", "tone", "variant", "route")
#: How a connector is routed (``style.route``, phase 2 3.4): ``elbow`` and ``curve`` are aliases.
ROUTES = ("straight", "orthogonal", "curved")
ROUTE_ALIASES = {"elbow": "orthogonal", "curve": "curved"}


def _default_style(kind: str) -> Dict[str, Any]:
    """A new element's style (0.22): its kind's default tone, the sans font, straight lines. Authorship is not a colour."""
    tone, variant = _theme.default_tone(kind)
    colours = _theme.resolve(tone, variant, kind)
    return {"stroke": colours["stroke"], "fill": colours["fill"], "text": colours["text"], "tone": tone, "variant": variant,
            "width": 2, "dash": "solid", "opacity": 100, "font": "normal", "size": 20, "rough": 0}


def _toned(style: Dict[str, Any], op: Dict[str, Any], kind: str) -> None:
    """Apply an op's ``tone``, ``variant`` and ``color`` to ``style`` in place.

    A tone (or a variant alone, over the element's tone) sets stroke, fill and
    label colour together. A named ``color`` is read as its tone (``blue`` is
    ``info``) and sets stroke and label colour; a hex ``color`` is the escape
    hatch, drawn as given, with the label in its tone's text colour when the hex
    is a known one and in ink otherwise (a free text takes the hex itself).
    """
    tone = style.get("tone") if style.get("tone") in TONES else None
    variant = style.get("variant") if style.get("variant") in VARIANTS else "soft"
    if op.get("tone") is not None or op.get("variant") is not None:
        tone = _choice(op.get("tone"), "tone", TONES, tone or _theme.default_tone(kind)[0])
        variant = _choice(op.get("variant"), "variant", VARIANTS, variant)
        style.update(_theme.resolve(tone, variant, kind), tone=tone, variant=variant)
    if op.get("color") is not None:
        raw = op["color"]
        name = raw.strip().lower() if isinstance(raw, str) else ""
        if name in COLORS:
            tone = _theme.tone_of(name) or "neutral"
            colours = _theme.resolve(tone, variant, kind)
            style.update(stroke=colours["stroke"], text=colours["text"], tone=tone)
        else:
            stroke = _stroke_color(raw)
            tone = _theme.tone_of(stroke)
            text = stroke if kind == "text" else _theme.resolve(tone or "neutral", variant, kind)["text"]
            style.update(stroke=stroke, text=text, tone=tone)


def _style(op: Dict[str, Any], base: Dict[str, Any], kind: str = "box") -> Dict[str, Any]:
    """The element ``style`` from an op's style inputs over ``base``; ``kind`` decides how a tone is used."""
    style = dict(base)
    _toned(style, op, kind)
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
        style["font"] = _choice(op["font"], "font", FONTS, "normal")
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
    if op.get("route") is not None:
        found = _kinds.get(kind)
        if found is None or found.role != "connector":
            raise _invalid("route", "route applies to arrows")
        raw = op["route"].strip().lower() if isinstance(op["route"], str) else op["route"]
        style["route"] = _choice(ROUTE_ALIASES.get(raw, raw) if isinstance(raw, str) else raw, "route", ROUTES, "straight")
    return style


def measure_text(text: str, size: float, max_w: float = TEXT_MAX_W, font: str = "normal") -> Tuple[float, float]:
    """A text element's size: its lines wrapped at ``max_w`` with the bundled font's metrics (``canvas_text``),
    as wide as its widest line (at least one font size) and 1.25 x size per line."""
    request = _ctext.FitRequest(text or " ", font=font, size=float(size), min_w=float(size), max_w=max(float(size), float(max_w)))
    result = _ctext.fit("hug", request)
    return float(result.w), float(result.h)


def text_size(text: str, size: float, wrap_w: Optional[float] = None, font: str = "normal") -> Tuple[float, float]:
    """``(w, h)`` of a text element. Its height always follows its lines; its width is the widest line
    (up to ``TEXT_MAX_W``), or ``wrap_w`` when the text wraps at a set width (wider only for a word that cannot break)."""
    probe = {"type": "text", "text": text, "style": {"size": size, "font": font}, "wrap": wrap_w is not None}
    fitted = _fitted(probe, (float(wrap_w), 1.0) if wrap_w is not None else (float(size), 1.0))
    return float(fitted["w"]), float(fitted["h"])


def _minimum(el: Dict[str, Any], w: Any = None, h: Any = None) -> Tuple[float, float]:
    """What a text-bearing element may not shrink below (0.22): an op's ``w``/``h``, else its stored
    ``fit.min``, else (an element from before 0.22) its current size, else its kind's default. A text's
    minimum width is its wrap width when it wraps, else one font size; its height follows its lines."""
    style = el.get("style") if isinstance(el.get("style"), dict) else {}
    fit = el.get("fit") if isinstance(el.get("fit"), dict) else {}
    stored = fit.get("min") if isinstance(fit.get("min"), list) and len(fit["min"]) == 2 and all(_is_number(v) for v in fit["min"]) else None
    if _free_text(_kinds.kind_of(el)):
        if w is not None:
            return float(w), 1.0
        if el.get("wrap"):
            return float(stored[0]) if stored else max(1.0, float(el.get("w") or 1)), 1.0
        return float(style.get("size") or 20), 1.0
    if stored:
        base_w, base_h = float(stored[0]), float(stored[1])
    elif _is_number(el.get("w")) and _is_number(el.get("h")):
        base_w, base_h = float(el["w"]), float(el["h"])
    else:
        base_w, base_h = (float(v) for v in SHAPE_SIZES.get(str(el.get("type")), (160, 80)))
    return (float(w) if w is not None else base_w), (float(h) if h is not None else base_h)


def _fitted(el: Dict[str, Any], minimum: Sequence[float]) -> Dict[str, Any]:
    """``w``, ``h`` and ``fit`` of an element sized from its label by its kind (0.22), or ``{}`` for a kind without text."""
    kind = _kinds.kind_of(el)
    if kind is None or kind.measure is None:
        return {}
    result = kind.measure(el, (float(minimum[0]), float(minimum[1])))
    return {"w": max(1, min(MAX_SIZE, int(math.ceil(result.w - 1e-9)))), "h": max(1, min(MAX_SIZE, int(math.ceil(result.h - 1e-9)))),
            "fit": _ctext.fit_record(result, minimum)}


def _text_fields(el: Dict[str, Any], text: str, style: Dict[str, Any], wrap_w: Optional[float] = None) -> Dict[str, Any]:
    """A text element's ``w``/``h``/``wrap``/``fit`` after its text, size or wrap width changed."""
    wraps = wrap_w is not None or bool(el.get("wrap"))
    probe = dict(el, text=text, style=style, wrap=wraps)
    fields = _fitted(probe, _minimum(probe, wrap_w))
    fields["wrap"] = wraps
    return fields


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


def _point(value: Any, field: str, lookup: Optional[Lookup] = None, pressure: bool = False,
           local: Optional[Tuple[float, float, float, float]] = None) -> List[float]:
    """A point from a cell, ``"x,y"``, ``[x, y]`` or (with ``lookup``) an element's centre. ``local`` is a free section's
    grid ``(x, y, cell_w, cell_h)`` (an op with ``in``, phase 2 4.3): cells are its cell corners and numbers its offsets."""
    ox, oy, cw, ch = local if local is not None else (0.0, 0.0, float(GRID), float(GRID))
    if isinstance(value, (list, tuple)):
        if len(value) not in ((2, 3) if pressure else (2,)):
            raise _invalid(field, "a point is [x, y]{}".format(" or [x, y, pressure]" if pressure else ""))
        point = [_r2(_coord(value[0], field) + ox), _r2(_coord(value[1], field) + oy)]
        if len(value) == 3:
            point.append(_r2(_num(value[2], field, 0, 1)))
        return point
    if isinstance(value, str):
        text = value.strip()
        match = _CELL_RE.match(text)
        if match:
            if local is not None:
                return [_r2(ox + int(match.group(1)) * cw), _r2(oy + int(match.group(2)) * ch)]
            return [int(match.group(1)) * GRID, int(match.group(2)) * GRID]
        match = _XY_RE.match(text)
        if match:
            return [_r2(_coord(match.group(1), field) + ox), _r2(_coord(match.group(2), field) + oy)]
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
        #: Where every element stood when the batch began (QA R-1: a mark already there hosts what is put on it).
        self.start: Dict[str, Tuple[float, float, float, float]] = {eid: bounds(el) for eid, el in state.elements.items()}
        #: What a block kind's ``prepare`` computed before ``canvas.lock`` was taken, by op index (phase 2, 1.3).
        self.prepared: Dict[int, Any] = {}
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
        #: Marks this op moved to make room (``_shift_group``), up to ``MAX_PUSHES``.
        self.pushes = 0
        #: How far ``_place`` moved the next element it creates from the point asked for (it followed a
        #: mark the engine had moved, QA R-1); ``element`` records it as that element's ``nudged``.
        self.placed_shift: Optional[Tuple[float, float]] = None
        # Blocks (canvas v2 phase 2, ``canvas_blocks``): the containers to arrange after this op and why, what the op's
        # ``block`` result says, what the arrangements moved and measured, and where a new block was placed.
        self.dirty: Dict[str, str] = {}
        self.detected = False
        self.block_info: Optional[Dict[str, Any]] = None
        self.block_stats: Dict[str, Dict[str, Any]] = {}
        self.block_moved: Dict[str, List[str]] = {}
        self.block_placed: Optional[Dict[str, Any]] = None
        #: A block growing pushes its neighbours and says what it could not move (``blocked_by_pin``, ``not_yours``).
        self.push_warn = False
        #: The local grid an op with ``in: <free section>`` resolves its cells and points in: ``(x, y, cell_w, cell_h)``.
        self.local: Optional[Tuple[float, float, float, float]] = None
        #: ``(container id, index)``: the element this op creates joins that container's layout (``in``).
        self.join: Optional[Tuple[str, Optional[int]]] = None

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
            "text": text or "", "style": style or _default_style(kind),
            "frame": frame, "group": group, "role": role, "z": self.next_z(),
            "author": self.author.name, "author_kind": KIND_HUMAN if self.author.is_human else KIND_MEMBER,
            "intent": self.intent, "batch": self.batch_id, "created_seq": self.seq, "updated_seq": self.seq,
            "created_at": self.ts, "updated_at": self.ts,
        }
        el.update(fields)
        for key in ("x", "y"):
            if abs(el[key]) > MAX_COORD:
                raise _invalid(key, "{} is outside the canvas (|{}| <= {})".format(key, key, MAX_COORD))
        if self.placed_shift is not None:
            el["nudged"] = [_r2(self.placed_shift[0]), _r2(self.placed_shift[1])]
            self.placed_shift = None
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


#: Field names agents reach for that mean an accepted one (QA phase 2, F20): the refusal names it.
_FIELD_MEANS = {"alias": "id", "name": "id", "label": "text", "title": "text", "kind": "type", "inside": "in", "into": "in",
                "parent": "in", "route_style": "route"}


def _check_fields(op: Dict[str, Any], allowed: Sequence[str]) -> None:
    for key in op:
        if key not in allowed:
            meant = _FIELD_MEANS.get(str(key))
            hint = " (did you mean {!r}?)".format(meant) if meant in allowed else ""
            raise _invalid(str(key), "{} does not take {!r}{}; it takes: {}".format(op.get("op"), key, hint,
                                                                            ", ".join(k for k in allowed if k != "op")))


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
    """Locks, then record, then the claim and legibility warnings (not for what joins a container that arranges it:
    it is laid out after the op, and the batch's check says what is still wrong)."""
    _check_locks(ctx, [bounds(el)])
    ctx.put(el)
    _warn_claims(ctx, [el["id"]], bounds(el))
    if isinstance(el.get("frame"), str) and _blocks().arranged(ctx.el(el["frame"])):
        return el
    _warn_overlap(ctx, el)
    _warn_frame_edge(ctx, el)
    return el


# --------------------------------------------------------------------------
# placement


def _free_slot(ctx: _Ctx, area: Sequence[float], w: float, h: float, exclude: Optional[str] = None,
               reserve: Sequence[Sequence[float]] = (), widens: bool = False) -> Tuple[float, float]:
    """The first position in ``area`` (rows top to bottom, 20-unit steps) whose bounds plus a 20-unit margin hit nothing.

    Obstacles are the live elements that reach into the area, except the
    container itself and anything that wholly encloses the area (a parent
    frame), plus the ``reserve`` boxes (the author's portrait corner). A
    child of the container is always an obstacle, even one that fills the
    whole area: taken for a parent, the next child was put on top of it (QA F-3).

    When the area is full the element goes in the next free row below it, or,
    when the area ``widens`` (a frame grows right as well as down), in a new
    column to its right if that keeps the filled area nearer square (QA F-13).
    """
    x0, y0, x1, y1 = area

    def encloses(el: Dict[str, Any], box: Sequence[float]) -> bool:
        return _contains(box, area) and (exclude is None or el.get("frame") != exclude)

    obstacles = [tuple(box) for box in reserve]
    for el in ctx.live():
        box = bounds(el)
        if el["id"] != exclude and _intersects(box, (x0 - GRID, y0 - GRID, x1 + GRID, y1 + GRID)) and not encloses(el, box):
            obstacles.append(box)

    def scan(start: float, limit: float, blocking: Sequence[Sequence[float]], right: float = x1) -> Optional[Tuple[float, float]]:
        y = start
        while y + h <= limit:
            x = x0
            while x + w <= right:
                grown = (x - GRID, y - GRID, x + w + GRID, y + h + GRID)
                hit = next((ob for ob in blocking if _intersects(grown, ob)), None)
                if hit is None:
                    return x, y
                # Jump past the obstacle, still on the grid.
                x = max(x + GRID, x0 + math.ceil((hit[2] + GRID - x0) / GRID) * GRID)
            y += GRID
        return None

    found = scan(y0, y1, obstacles)
    if found is not None:
        return found
    below = [bounds(el) for el in ctx.live() if el["id"] != exclude and not encloses(el, bounds(el))] + [tuple(b) for b in reserve]
    # Nothing fits: keep packing rows below the area, clear of everything there too (a frame then grows
    # down to hold them), so a full frame fills its width rather than one ragged column (QA F-13).
    lowest = max([ob[3] for ob in obstacles] + [b[3] for b in below if _intersects(b, (x0, y0, x1, b[3] + 1))] + [y1])
    tried = math.floor((y1 - h - y0) / GRID) if y1 - h >= y0 else -1  # the last row the first scan tried
    found = scan(y0 + (tried + 1) * GRID, lowest + GRID + h, list(obstacles) + below)
    if widens:
        beside = scan(y0, max(y1, y0 + h), list(obstacles) + below, x1 + w + GRID)

        def squareness(spot: Optional[Tuple[float, float]]) -> float:
            if spot is None:
                return float("inf")
            width, height = max(x1, spot[0] + w) - x0, max(y1, spot[1] + h) - y0
            return max(width / max(height, 1.0), height / max(width, 1.0))

        if squareness(beside) < squareness(found):
            found = beside
    if found is not None:
        return found
    # Still nothing: content-left, 20 below the lowest child, then further down past
    # anything that already overflowed there (so two big items never stack).
    y = max((ob[3] for ob in obstacles), default=y0 - GRID) + GRID
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
        if el.get("block") and _blocks().joined_only(el):
            continue  # a stack or a positional block takes members only on purpose (in, a drop), never by where they land
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


def _grow_parents(ctx: _Ctx, el: Dict[str, Any]) -> None:
    """After ``el`` grew to fit its label: grow each frame up its chain that no longer holds it, where the
    author may and no lock is in the way; any other frame keeps its size (``frame_edge`` then says so)."""
    child = el
    seen: Set[str] = set()
    while isinstance(child.get("frame"), str) and child["frame"] not in seen:
        seen.add(child["frame"])
        frame = ctx.el(child["frame"])
        if frame is None or frame.get("type") != "frame" or frame.get("role") == PORTRAIT_ROLE:
            return
        box = bounds(child)
        if _contains(bounds(frame), box):
            return
        if not _may_edit(ctx.author, frame):
            return
        try:
            _grow_frame(ctx, frame, box)
        except HerdrTeamError:
            return
        child = ctx.el(frame["id"]) or frame


def _asked_box(el: Dict[str, Any]) -> Tuple[float, float, float, float]:
    """The box an element was asked to take: its corner and its ``fit.min`` (its bounds when it has none)."""
    fit = el.get("fit") if isinstance(el.get("fit"), dict) else {}
    found = fit.get("min") if isinstance(fit.get("min"), list) and len(fit["min"]) == 2 else None
    if not found or el.get("type") == "text":
        return bounds(el)
    x, y = float(el.get("x") or 0), float(el.get("y") or 0)
    return x, y, x + min(float(found[0]), float(el.get("w") or 1)), y + min(float(found[1]), float(el.get("h") or 1))


# --------------------------------------------------------------------------
# making room (QA R-1): a shape that grows to fit its text never ends up covering a neighbour it did not
# cover at the size the author asked for. The rules, in the order the ops meet them:
#
# - A mark placed on another (its asked box inside that one's asked box: a window on the walls) sits on
#   it: that one is its host. The host grows to hold it, like a frame holds a child, and carries it when
#   it moves. Only kinds whose registry record says ``hosts`` host (``canvas_kinds``).
# - A new mark that covers a neighbour only because one of them grew moves to the free spot nearest where
#   it was put: beside the neighbour, or elsewhere on its own host (the later sibling makes way).
# - A mark that grows after it was placed (a host holding a grown child, a label edited longer, an
#   arrow's end making room for its label) pushes the neighbours it now covers along the way it grew,
#   each with what sits on it; they push theirs in turn.
# - The author's coordinates stay the author's. Every mark the engine moved keeps how far (``nudged``,
#   cleared when someone moves it on purpose), so a later op that puts a mark at a point on where that
#   mark was asked to be puts it on the mark (``_follow``), in the same batch or a later one: an agent
#   that draws one op at a time gets the picture a whole batch gets.
#
# A collision the author asked for (the asked boxes overlap) is left alone, for ``check`` to report.

#: Room a host keeps between its edge and a mark on it, and between marks on it.
HOST_PAD = GRID // 2
#: How deep one growth may push neighbours that push theirs, and how many marks one op may move to make room.
MAX_PUSH_DEPTH = 12
MAX_PUSHES = 200


def _nudged(el: Dict[str, Any]) -> Tuple[float, float]:
    """How far the engine moved ``el`` from where it was asked to go (``nudged``), to make room."""
    found = el.get("nudged")
    if isinstance(found, list) and len(found) == 2 and all(_is_number(v) for v in found):
        return float(found[0]), float(found[1])
    return 0.0, 0.0


def _nudge(el: Dict[str, Any], dx: float, dy: float) -> Optional[List[float]]:
    """``el``'s ``nudged`` after the engine moves it by ``(dx, dy)`` (None when it is back where it was asked to go)."""
    nx, ny = _nudged(el)
    total = [_r2(nx + dx), _r2(ny + dy)]
    return total if total[0] or total[1] else None


def _author_box(ctx: _Ctx, el: Dict[str, Any]) -> Tuple[float, float, float, float]:
    """Where the author meant ``el`` to be: the box its op asked for (``fit.min`` at its corner), before it
    grew to fit its label and before the engine moved it to make room. Agents place marks without looking
    at the sizes they grew to, so two marks whose asked boxes do not meet were meant apart."""
    dx, dy = _nudged(el)
    x0, y0, x1, y1 = _asked_box(el)
    return x0 - dx, y0 - dy, x1 - dx, y1 - dy


def _host_of(ctx: _Ctx, el: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """The mark ``el`` was placed on: the smallest hosting mark under it whose author box holds ``el``'s, or,
    for a mark that was there before this batch, whose whole box as it then stood does (a chimney put on
    walls that grew in an earlier batch)."""
    mine = _author_box(ctx, el)
    z = int(el.get("z") or 0)
    best: Optional[Tuple[float, int, Dict[str, Any]]] = None
    for other in ctx.live():
        if other["id"] == el["id"] or not _kinds.hosts(other) or int(other.get("z") or 0) >= z:
            continue
        box = _author_box(ctx, other)
        if tuple(box) == tuple(mine) or not _contains(box, mine):
            # A mark already there when the batch began hosts what lies inside its outline as it then stood.
            start = ctx.start.get(other["id"])
            if start is None or tuple(start) == tuple(mine) or not _kinds.outline_holds(start, _kinds.outline(other), mine):
                continue
            box = start
        key = ((box[2] - box[0]) * (box[3] - box[1]), -int(other.get("z") or 0), other)
        if best is None or key[:2] < best[:2]:
            best = key
    return best[2] if best else None


def _host_chain(ctx: _Ctx, el: Dict[str, Any]) -> List[str]:
    """``el``'s host, that one's host, and so on."""
    chain: List[str] = []
    host = _host_of(ctx, el)
    while host is not None and host["id"] not in chain and len(chain) < MAX_PUSH_DEPTH:
        chain.append(host["id"])
        host = _host_of(ctx, host)
    return chain


def _riders(ctx: _Ctx, el: Dict[str, Any]) -> List[str]:
    """What moves with ``el``: a frame's children, or the marks drawn on a hosting mark (inside it, above it).
    An arrow bound to an element is left out: it is rerouted instead."""
    if el.get("type") == "frame":
        return _descendants(ctx, [el["id"]])
    if not _kinds.hosts(el):
        return []
    box, z = bounds(el), int(el.get("z") or 0)
    return [other["id"] for other in ctx.live()
            if other["id"] != el["id"] and other.get("type") != "frame" and int(other.get("z") or 0) > z and _contains(box, bounds(other))
            and not (other.get("type") == "arrow" and (other.get("from") or other.get("to")))]


def _follow(ctx: _Ctx, box: Sequence[float]) -> Tuple[float, float]:
    """How far a mark asked for at ``box`` moves with a mark the engine moved: the ``nudged`` of the smallest such
    mark whose author box holds it, unless it lies on a mark where that mark stands now (that is where it was put)."""
    best: Optional[Tuple[float, Tuple[float, float], str]] = None
    live = list(ctx.live())
    for el in live:
        shift = _nudged(el)
        if not (shift[0] or shift[1]) or el.get("type") in ("arrow", "comment", "pen"):
            continue
        author = _author_box(ctx, el)
        if _contains(author, box) and tuple(author) != tuple(box):
            area = (author[2] - author[0]) * (author[3] - author[1])
            if best is None or area < best[0]:
                best = (area, shift, el["id"])
    if best is None:
        return 0.0, 0.0
    if any(other["id"] != best[2] and _kinds.hosts(other) and _kinds.outline_holds(bounds(other), _kinds.outline(other), box) for other in live):
        return 0.0, 0.0
    return best[1]


def _shift_group(ctx: _Ctx, el: Dict[str, Any], dx: float, dy: float) -> bool:
    """Move ``el`` and what rides on it by ``(dx, dy)`` to make room; False (nothing moved) when one of them is
    not the author's to move, a lock is in the way, or it would leave the canvas."""
    ids = [el["id"]] + [r for r in _riders(ctx, el) if r != el["id"]]
    members = [m for m in (ctx.el(i) for i in ids) if m is not None]
    # A pin is never moved by growth push-out (canvas v2 phase 2, D6).
    pinned = next((m for m in members if isinstance(m.get("pin"), dict)), None)
    if pinned is not None:
        if ctx.push_warn and (dx or dy):
            ctx.warn("blocked_by_pin", "{} is pinned, so it stays where it is; it may now be covered".format(pinned["id"]), [pinned["id"]])
        return False
    if not (dx or dy) or ctx.pushes >= MAX_PUSHES or any(not _may_edit(ctx.author, m) for m in members):
        blocked = next((m for m in members if not _may_edit(ctx.author, m)), None)
        if ctx.push_warn and blocked is not None and (dx or dy):
            ctx.warn("not_yours", "{} is {}'s, so it was not moved out of the way; it may now be covered".format(
                blocked["id"], _who(blocked.get("author"), None)), [blocked["id"]])
        return False
    ctx.pushes += 1
    try:
        moves = [(m, dict(_translated(m, dx, dy, unbind=False), nudged=_nudge(m, dx, dy))) for m in members]
        _check_locks(ctx, [(f["x"], f["y"], f["x"] + bounds(m)[2] - bounds(m)[0], f["y"] + bounds(m)[3] - bounds(m)[1]) for m, f in moves])
    except HerdrTeamError:
        return False
    for member, fields in moves:
        ctx.update(member, **fields)
    moved = ctx.el(el["id"]) or el
    if moved.get("frame"):
        _grow_parents(ctx, moved)
    _reroute_bound(ctx, ids, skip=[i for i in ids if (ctx.el(i) or {}).get("type") == "arrow"])
    return True


def _push_vector(old: Sequence[float], new: Sequence[float], other: Sequence[float], ways: Sequence[str]) -> Tuple[int, int]:
    """The shortest move of ``other`` along a way the grower grew that clears it, keeping the gap they had (at most a grid step)."""
    options: List[Tuple[int, int]] = []

    def gap(value: float) -> float:
        return float(GRID) if value < 0 else min(value, float(GRID))

    if "right" in ways:
        options.append((_grid_step(new[2] + gap(other[0] - old[2]) - other[0]), 0))
    if "down" in ways:
        options.append((0, _grid_step(new[3] + gap(other[1] - old[3]) - other[1])))
    if "left" in ways:
        options.append((_grid_step(new[0] - gap(old[0] - other[2]) - other[2]), 0))
    if "up" in ways:
        options.append((0, _grid_step(new[1] - gap(old[1] - other[3]) - other[3])))
    return min(options, key=lambda v: (abs(v[0]) + abs(v[1]), options.index(v)))


def _grid_step(value: float) -> int:
    """A move rounded away from zero to whole grid steps, so a mark on the grid stays on it and never falls short."""
    steps = math.ceil(abs(value) / GRID - 1e-9)
    return int(math.copysign(steps * GRID, value)) if steps else 0


def _push_from(ctx: _Ctx, grower: Dict[str, Any], old: Sequence[float], depth: int = 0) -> None:
    """``grower`` grew or moved from ``old``: push each neighbour it now covers, and did not before, out of its way."""
    if depth > MAX_PUSH_DEPTH:
        return
    new = bounds(grower)
    ways = [way for way, grew in (("right", new[2] > old[2]), ("down", new[3] > old[3]), ("left", new[0] < old[0]),
                                  ("up", new[1] < old[1])) if grew]
    if not ways:
        return
    carried = {grower["id"]} | set(_riders(ctx, grower)) | set(_host_chain(ctx, grower))
    mine = _author_box(ctx, grower)
    neighbours = sorted((o for o in ctx.live() if o.get("type") in _check.solid_kinds() and o["id"] not in carried),
                        key=lambda o: (int(o.get("created_seq") or 0), _id_number(o["id"])))
    ancestors = _ancestors(ctx, grower)
    seen: Set[str] = set()
    for neighbour in neighbours:
        other = ctx.el(neighbour["id"])
        if other is None:
            continue
        # A member of a block moves with its block: the outermost block container around it is what makes way (phase 2).
        other = _block_container(ctx, other, ancestors) if isinstance(other.get("frame"), str) else other
        if other is None or other["id"] in carried or other["id"] in seen:
            continue
        seen.add(other["id"])
        box = bounds(other)
        if not _check._intersects(new, box, _check.TOUCH) or _check._intersects(old, box, _check.TOUCH) or _contains(box, new):
            continue
        if _check._intersects(mine, _author_box(ctx, other), _check.TOUCH):
            continue  # the author put them together
        dx, dy = _push_vector(old, new, box, ways)
        if not _shift_group(ctx, other, dx, dy):
            continue
        ctx.warn("moved_to_fit", "{} moved {} to make room for {}".format(other["id"], _way_text(dx, dy), grower["id"]),
                 [other["id"], grower["id"]])
        moved = ctx.el(other["id"]) or other
        _hold_on_host(ctx, moved, depth + 1)
        _push_from(ctx, moved, box, depth + 1)


def _ancestors(ctx: _Ctx, el: Dict[str, Any]) -> Set[str]:
    """The frames around ``el``, up its chain."""
    found: Set[str] = set()
    cursor = el
    while isinstance(cursor.get("frame"), str) and cursor["frame"] not in found:
        found.add(cursor["frame"])
        parent = ctx.el(cursor["frame"])
        if parent is None:
            break
        cursor = parent
    return found


def _block_container(ctx: _Ctx, el: Dict[str, Any], ancestors: Set[str]) -> Optional[Dict[str, Any]]:
    """``el`` itself, or the outermost block container it sits in that is not around the grower (None: it sits in the
    grower's own chain, so it is not pushed)."""
    current = el
    seen: Set[str] = set()
    while isinstance(current.get("frame"), str) and current["frame"] not in seen:
        seen.add(current["frame"])
        parent = ctx.el(current["frame"])
        if parent is None or not parent.get("block"):
            break
        if parent["id"] in ancestors:
            # A sibling in the grower's own container: a stack arranges it, anything else is pushed as itself.
            return None if _blocks().stack_of(parent) is not None else current
        current = parent
    return current


def _way_text(dx: float, dy: float) -> str:
    parts = [("right" if dx > 0 else "left", abs(dx)), ("down" if dy > 0 else "up", abs(dy))]
    return " and ".join("{} {:g}".format(way, amount) for way, amount in parts if amount)


def _hold_on_host(ctx: _Ctx, el: Dict[str, Any], depth: int = 0) -> None:
    """``el`` sits on a host it no longer fits in: the host grows (right and down, keeping ``HOST_PAD``) to hold
    it, where its author may and no lock is in the way, and pushes what that growth covers."""
    host = _host_of(ctx, el)
    if host is None or depth > MAX_PUSH_DEPTH:
        return
    box, old = bounds(el), bounds(host)
    x1, y1 = max(old[2], box[2] + HOST_PAD), max(old[3], box[3] + HOST_PAD)
    if (x1, y1) == (old[2], old[3]) or not _may_edit(ctx.author, host):
        return
    try:
        _check_locks(ctx, [(old[0], old[1], x1, y1)])
    except HerdrTeamError:
        return
    grown = ctx.update(host, w=_round(x1 - old[0]), h=_round(y1 - old[1]))
    if grown.get("frame"):
        _grow_parents(ctx, grown)
    _reroute_bound(ctx, [grown["id"]])
    _push_from(ctx, grown, old, depth + 1)
    _hold_on_host(ctx, ctx.el(grown["id"]) or grown, depth + 1)


def _after_growth(ctx: _Ctx, el: Dict[str, Any], old: Sequence[float]) -> None:
    """An existing mark grew to fit its label: it pushes what it now covers, and its host grows to hold it."""
    if bounds(el) == tuple(old):
        return
    _push_from(ctx, el, old)
    _hold_on_host(ctx, ctx.el(el["id"]) or el)


def _make_way(ctx: _Ctx, el: Dict[str, Any]) -> None:
    """A new mark that covers a neighbour only because one of them grew (their author boxes do not meet) moves,
    before it is recorded, to the nearest free spot: on its own host when it has one (the host then grows
    to hold it), else beside the neighbour inside its frame, and says so (0.22, QA R-1)."""
    box = bounds(el)
    x, y = float(el["x"]), float(el["y"])
    mine = _author_box(ctx, el)
    chain = set(_host_chain(ctx, el))
    around = _ancestors(ctx, el)
    # A block's zone (a kanban, a timeline) takes room like a solid mark does (phase 2): a new mark makes way for it.
    solid = [other for other in ctx.live() if (other.get("type") in _check.solid_kinds() or (other.get("block") and other.get("frame") is None
             and _blocks().joined_only(other))) and other["id"] != el["id"] and other["id"] not in chain and other["id"] not in around]
    hit = next((other for other in solid if _check._intersects(box, bounds(other), _check.TOUCH)
                and not _check._intersects(mine, _author_box(ctx, other), _check.TOUCH)), None)
    if hit is None:
        return
    boxes = [(str(other["id"]), bounds(other)) for other in solid]
    host = _host_of(ctx, el) if chain else None
    frame = ctx.el(el["frame"]) if el.get("frame") else None
    if host is not None:
        hx0, hy0, hx1, hy1 = bounds(host)
        area: Optional[Tuple[float, float, float, float]] = (hx0 + HOST_PAD, hy0 + HOST_PAD, hx1 - HOST_PAD, hy1 - HOST_PAD)
        spot = _check.free_spot(el, hit, boxes, area, near=(x, y), frame_grows=_may_edit(ctx.author, host), clearance=HOST_PAD)
    else:
        # The free spot nearest where the author put it; in a frame its author may grow, past its right or bottom edge.
        grows = frame is not None and _may_edit(ctx.author, frame)
        spot = _check.free_spot(el, hit, boxes, bounds(frame) if frame is not None else None, near=(x, y), frame_grows=grows,
                                clearance=GRID)
    if spot is None:
        return
    moved = (float(spot[0]), float(spot[1]), float(spot[0]) + box[2] - box[0], float(spot[1]) + box[3] - box[1])
    try:
        _check_locks(ctx, [moved])
    except HerdrTeamError:
        return
    ctx.warn("moved_to_fit", "{} would cover {} now that labels grow to fit; it went to {} instead".format(
        el["id"], hit["id"], cell_name(spot[0], spot[1])), [el["id"], hit["id"]])
    nudged = _nudge(el, _round(spot[0]) - x, _round(spot[1]) - y)
    if nudged is None:
        el.pop("nudged", None)
    else:
        el["nudged"] = nudged
    el["x"], el["y"] = _round(spot[0]), _round(spot[1])
    if frame is None:
        el["frame"] = _enclosing_frame(ctx, moved)
    elif not _contains(bounds(frame), moved):
        _grow_parents(ctx, el)


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


PLACE_KEYS = ("at", "right_of", "left_of", "below", "above", "inside", "in")
#: ``in`` joins a container's layout (a section, a kanban column), at ``index`` when given (canvas v2 phase 2, 4.1).
PLACE_FIELDS = PLACE_KEYS + ("gap", "index")
#: Gap tokens (``s``, ``m``, ``l``); a number is units.
GAPS = ("s", "m", "l")


def _gap(value: Any) -> float:
    """A placement gap: ``s`` 20, ``m`` 40, ``l`` 80, a number of units, or ``DEFAULT_GAP``."""
    if value is None:
        return float(DEFAULT_GAP)
    if isinstance(value, str) and value.strip().lower() in GAPS:
        return _theme.gap(value.strip().lower())
    return _num(value, "gap", 0, 5000)


def _local_grid(ctx: "_Ctx", container: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    """A free section's local grid (4.3): its content corner and cell size; None for anything else."""
    kind = _kinds.kind_of(container)
    if kind is None or kind.name != "section" or _blocks().stack_of(container) is not None:
        return None
    from herdr_team.canvas_kinds import _zone

    x0, y0, _x1, _y1 = _zone.content_box(container)
    cell = (container.get("settings") or {}).get("cell") if isinstance(container.get("settings"), dict) else None
    if isinstance(cell, list) and len(cell) == 2 and all(_is_number(v) for v in cell):
        return float(x0), float(y0), float(cell[0]), float(cell[1])
    return float(x0), float(y0), float(GRID), float(GRID)


def _place_in(ctx: _Ctx, op: Dict[str, Any], w: float, h: float) -> Tuple[float, float, Optional[str]]:
    """``in: <container>`` (phase 2, 4.1): the element joins the container's layout once it is created (``ctx.join``); a
    stack re-stacks it, a free section keeps it at ``at`` in its local grid or at a free spot, a plain frame takes it as
    ``inside`` would."""
    container = ctx.lookup(op["in"], "in")
    if container.get("type") != "frame":
        raise _invalid("in", "in names a container (a section, a kanban column, a frame); {} is a {}".format(container["id"], container.get("type")))
    blocks = _blocks()
    if not container.get("block"):
        x, y, frame = _place(ctx, dict({k: v for k, v in op.items() if k not in ("in", "index", "at")}, inside=op["in"]), w, h)
        return x, y, frame
    if op.get("at") is not None:
        local = _local_grid(ctx, container)
        if local is None:
            raise _invalid("at", "at goes with in only for a free section (layout free); a stack places what joins it")
        x, y = _point(op["at"], "at", None, False, local)[:2]
    else:
        x, y = blocks._entry_point(ctx, container, w, h)
    index = int(_num(op["index"], "index", 0, 10 ** 6)) if op.get("index") is not None else None
    ctx.join = (container["id"], index)
    return x, y, container["id"]


def _place(ctx: _Ctx, op: Dict[str, Any], w: float, h: float,
           asked: Optional[Tuple[float, float]] = None) -> Tuple[float, float, Optional[str]]:
    """``(x, y, frame)`` for a new element of size ``w`` x ``h`` from the op's placement (contract 5.3).

    ``asked`` is the size the op asked for when the element grew past it to fit
    its label (0.22): an element whose asked-for box lay in a frame still joins
    that frame, and the frame grows to hold it where its author may.
    """
    keys = [key for key in PLACE_KEYS if op.get(key) is not None]
    if keys == ["at", "in"]:
        keys = ["in"]  # at in a free section's local grid (4.3)
    if len(keys) > 1:
        raise _invalid(keys[1], "use at most one of at, right_of, left_of, below, above, inside, in")
    if op.get("index") is not None and keys != ["in"]:
        raise _invalid("index", "index goes with in")
    gap = _gap(op.get("gap"))
    if keys == ["in"]:
        return _place_in(ctx, op, w, h)
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
        x, y = _free_slot(ctx, _content_area(container), w, h, exclude=container["id"], widens=container.get("type") == "frame")
        if container.get("type") == "frame":
            _grow_frame(ctx, container, (x, y, x + w, y + h))
            # A frame that grew may no longer fit its own frame: each one up the chain grows too (QA F-3).
            _grow_parents(ctx, ctx.el(container["id"]) or container)
            return x, y, container["id"]
        return x, y, None
    if key == "at":
        x, y = _point(op["at"], "at", lambda ref, field: ctx.lookup(ref, field))[:2]
        if _is_point_form(op["at"]):
            # A point on a mark this batch moved to make room is a point on that mark (QA R-1).
            aw, ah = asked if asked is not None else (w, h)
            dx, dy = _follow(ctx, (x, y, x + aw, y + ah))
            if dx or dy:
                x, y = x + dx, y + dy
                ctx.placed_shift = (dx, dy)
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
    frame = _enclosing_frame(ctx, (x, y, x + w, y + h))
    if frame is None and asked is not None and (asked[0] < w or asked[1] < h):
        frame = _enclosing_frame(ctx, (x, y, x + asked[0], y + asked[1]))
        container = ctx.el(frame) if frame else None
        if container is not None:
            _grow_parents(ctx, {"frame": frame, "x": x, "y": y, "w": w, "h": h})
            if not _contains(bounds(ctx.el(frame) or container), (x, y, x + w, y + h)):
                frame = None  # it could not grow (not the author's, or locked): frame_edge says so
    return x, y, frame


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


def _reroute_env(ctx: _Ctx, arrow: Dict[str, Any], start_el: Optional[Dict[str, Any]], end_el: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """What a connector's ``reroute`` routes around (phase 2, 3.4): for a block member, its block's solid members; else the
    solid elements near both ends. Never the ends, nor the containers of the ends."""
    ends = [e["id"] for e in (start_el, end_el) if e is not None]
    exclude = set(ends)
    for end in (start_el, end_el):
        if end is not None:
            exclude |= _ancestors(ctx, end)
    root = ctx.el(arrow["group"]) if isinstance(arrow.get("group"), str) else None
    if root is not None and root.get("block"):
        obstacles = [(o["id"], bounds(o), _kinds.outline(o)) for o in ctx.live()
                     if o.get("group") == root["id"] and o["id"] not in exclude and o.get("type") in _check.solid_kinds()]
    else:
        boxes = [bounds(e) for e in (start_el, end_el) if e is not None] or [bounds(arrow)]
        area = (min(b[0] for b in boxes) - 200, min(b[1] for b in boxes) - 200, max(b[2] for b in boxes) + 200, max(b[3] for b in boxes) + 200)
        obstacles = _obstacles(ctx, area, list(exclude))
    others = [[tuple(p[:2]) for p in (o.get("points") or [])] for o in ctx.live()
              if o.get("type") == "arrow" and o["id"] != arrow["id"] and o.get("group") and o.get("group") == arrow.get("group")]
    return {"obstacles": obstacles, "others": others, "tokens": _theme.tokens()}


def _obstacles(ctx: _Ctx, box: Sequence[float], exclude: Sequence[str]) -> List[Tuple[str, Tuple[float, float, float, float], str]]:
    """Solid elements meeting ``box``, leaving out ``exclude`` and the frames around those (``OpContext.obstacles``)."""
    skip = set(exclude)
    for eid in exclude:
        el = ctx.el(eid)
        if el is not None:
            skip |= _ancestors(ctx, el)
    return [(o["id"], bounds(o), _kinds.outline(o)) for o in ctx.live()
            if o["id"] not in skip and o.get("type") in _check.solid_kinds() and _intersects(bounds(o), box)]


def _reroute(ctx: _Ctx, arrow: Dict[str, Any]) -> None:
    """Re-run a bound arrow's ends after the elements it binds moved or changed (its kind's ``reroute`` when it has one:
    the route style's router, phase 2 3.4)."""
    points = arrow.get("points") or []
    if len(points) < 2:
        return
    start_el = ctx.el(arrow["from"]) if arrow.get("from") else None
    end_el = ctx.el(arrow["to"]) if arrow.get("to") else None
    kind = _kinds.kind_of(arrow)
    if kind is not None and kind.reroute is not None:
        fields = dict(kind.reroute(dict(arrow), start_el, end_el, _reroute_env(ctx, arrow, start_el, end_el)) or {})
        if start_el is None and arrow.get("from"):
            fields["from"] = None
        if end_el is None and arrow.get("to"):
            fields["to"] = None
        if fields and any(arrow.get(k) != v for k, v in fields.items()):
            ctx.update(arrow, **fields)
        return
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
# arrow labels (QA R-3): each label sits in a pill on its route, clear of marks and of other labels


#: How many times a new arrow moves its later end to make room for its label before the label settles beside the line.
MAX_ROOM_TRIES = 3


def _label_reach(el: Dict[str, Any], size: Tuple[float, float]) -> Tuple[float, float, float, float]:
    """The box an arrow's label may end up anywhere in: its route, grown by the farthest spot beside it."""
    x0, y0, x1, y1 = _bbox(_render.arrow_route(el) or [(float(el.get("x") or 0), float(el.get("y") or 0))])
    reach = max(size) + _labels.CLEARANCE + _labels.SIDE_STEPS * _labels.STEP
    return x0 - reach, y0 - reach, x1 + reach, y1 + reach


def _label_marks(live: Sequence[Dict[str, Any]]) -> List[Tuple[Tuple[float, float, float, float], str]]:
    """What a label keeps clear of besides other labels: every solid mark's outline, and frame titles."""
    marks = [_labels.obstacle_of(el) for el in live if el.get("type") in _check.solid_kinds()]
    for el in live:
        if el.get("type") == "frame" and el.get("text"):
            title = _render.frame_title_box(el, 1.0)
            if title is not None:
                marks.append((title, "rect"))
    return marks


def _label_size(el: Dict[str, Any]) -> Optional[Tuple[float, float]]:
    found = _render.arrow_label_text(el)
    return found[0] if found is not None else None


def _room_for_label(ctx: _Ctx, arrow: Dict[str, Any], start_id: str, end_id: str, points: List[List[float]]) -> List[List[float]]:
    """A new arrow whose label has no clear spot on its route moves its later end (and what rides on it) away
    along the arrow, just far enough for the label to sit on the line next to that end, beside anything
    already on the line; a mark the move then covers is pushed on in turn. Returns the route it ends with."""
    for _attempt in range(MAX_ROOM_TRIES):
        start, end = ctx.el(start_id), ctx.el(end_id)
        if start is None or end is None or start_id == end_id or _check._intersects(bounds(start), bounds(end)):
            return points
        probe = dict(arrow, points=points, **_geometry(points))
        size = _label_size(probe)
        flat = _render.arrow_route(probe)
        if size is None or _labels.length(flat) == 0:
            return points
        live = list(ctx.live())
        reach = _label_reach(probe, size)
        pills = [box for box in (_pill_box(other) for other in live) if box and _check._intersects(box, reach)]
        near = [el for el in live if _check._intersects(bounds(el), reach)]
        if not _labels.blocked_on_route(flat, size, _label_marks(near), pills):
            return points
        # The later end moves (the author placed it knowing the earlier one); the other only when it cannot.
        ends = sorted([(end, False), (start, True)], key=lambda item: -int(item[0].get("created_seq") or 0))
        mover, at_start = ends[0]
        carried = {mover["id"]} | set(_riders(ctx, mover))
        need = _labels.room_at_end(flat, size, _label_marks([el for el in near if el["id"] not in carried]), pills, at_start)
        (x0, y0), (x1, y1) = flat[0], flat[-1]
        if need <= 0:
            return points  # the line is blocked in its middle (it runs through a mark): moving an end does not help
        # Along the arrow's main axis, away from the other end, so rows and columns stay lined up.
        ax, ay = (x0 - x1, y0 - y1) if at_start else (x1 - x0, y1 - y0)
        span = max(abs(ax), abs(ay))
        move = (_grid_step(math.copysign(need * math.hypot(ax, ay) / span, ax)), 0) if abs(ax) >= abs(ay) else \
            (0, _grid_step(math.copysign(need * math.hypot(ax, ay) / span, ay)))
        for candidate, _at_start in ends:
            old = bounds(candidate)
            if _shift_group(ctx, candidate, *(move if candidate is mover else (-move[0], -move[1]))):
                moved = ctx.el(candidate["id"]) or candidate
                ctx.warn("moved_to_fit", "{} moved {} to make room for the label of the arrow to it".format(
                    candidate["id"], _way_text(bounds(moved)[0] - old[0], bounds(moved)[1] - old[1])), [candidate["id"]])
                _push_from(ctx, moved, old)
                _hold_on_host(ctx, moved)
                break
        else:
            return points
        start, end = ctx.el(start_id), ctx.el(end_id)
        if start is None or end is None:
            return points
        points = _route(("element", start), ("element", end))
    return points


def _pill_box(el: Dict[str, Any]) -> Optional[Tuple[float, float, float, float]]:
    pill = _render.arrow_label_pill(el) if el.get("type") == "arrow" and el.get("text") else None
    if pill is None:
        return None
    (px, py, pw, ph), _size, _lines = pill
    return px, py, px + pw, py + ph


def _settle_labels(ctx: _Ctx) -> None:
    """After each op: every labelled arrow the op touched, or whose label's reach meets a mark the op touched,
    gets its label's spot (``label_at``) and lines (``fit``) again; one still on its route and clear stays
    where it is. Labels far from the op are never measured."""
    if not ctx.pending:
        return
    dirty: List[Tuple[float, float, float, float]] = []
    for eid, value in ctx.pending.items():
        for el in (ctx.state.elements.get(eid), value):
            if el is not None:
                dirty.append(_render.drawn_bounds(el))
    live = list(ctx.live())
    arrows = sorted((el for el in live if el.get("type") == "arrow" and str(el.get("text") or "") and len(el.get("points") or []) >= 2),
                    key=lambda el: (int(el.get("created_seq") or 0), _id_number(el["id"])))
    rough = {el["id"]: _label_reach(el, (_LABEL_BOUND, _LABEL_BOUND)) for el in arrows}
    todo = [el for el in arrows if el["id"] in ctx.pending or any(_check._intersects(box, rough[el["id"]]) for box in dirty)]
    if not todo:
        return
    sizes: Dict[str, Optional[Tuple[float, float]]] = {}
    centers: Dict[str, Tuple[float, float]] = {}

    def size_of(el: Dict[str, Any]) -> Optional[Tuple[float, float]]:
        if el["id"] not in sizes:
            sizes[el["id"]] = _label_size(el)
        return sizes[el["id"]]

    def center_of(el: Dict[str, Any]) -> Optional[Tuple[float, float]]:
        # An arrow this op drew has no spot yet: it blocks nothing until it gets one (it comes last).
        if el["id"] not in centers and (el.get("label_at") or el["id"] not in ctx.created):
            centers[el["id"]] = _render.arrow_label_center(el)
        return centers.get(el["id"])

    marks = _label_marks(live)
    for arrow in todo:
        arrow = ctx.el(arrow["id"]) or arrow
        size = size_of(arrow)
        if size is None:
            continue
        reach = _label_reach(arrow, size)
        near = [m for m in marks if _check._intersects(m[0], reach)]
        pills = []
        for other in arrows:
            if other["id"] == arrow["id"] or not _check._intersects(rough[other["id"]], reach):
                continue
            other_size, other_center = size_of(other), center_of(other)
            if other_size is not None and other_center is not None:
                box = _labels.pill_box(other_center, other_size)
                if _check._intersects(box, reach):
                    pills.append(box)
        stored = arrow.get("label_at")
        current = (float(stored[0]), float(stored[1])) if isinstance(stored, list) and len(stored) == 2 else None
        spot, _ring = _labels.place(_render.arrow_route(arrow), size, near, pills, current)
        at = [_r2(spot[0]), _r2(spot[1])]
        centers[arrow["id"]] = (at[0], at[1])
        fields: Dict[str, Any] = {}
        if stored != at:
            fields["label_at"] = at
        fit = _label_fit(arrow)
        if fit is not None and arrow.get("fit") != fit:
            fields["fit"] = fit
        if fields:
            ctx.update(arrow, **fields)


#: The most an arrow label's pill reaches from its centre before it is measured: its wrap width plus padding.
_LABEL_BOUND = float(_render.ARROW_LABEL_EMS * 36 * _render.ARROW_LABEL_SCALE + 2 * _render.ARROW_LABEL_PAD[0])


def _label_fit(el: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """An arrow label's ``fit`` record: the lines the picture draws and their size, which the page draws too."""
    found = _render.arrow_label_text(el)
    if found is None:
        return None
    _size, size, lines = found
    return {"lines": list(lines), "size": _r2(size)}


# --------------------------------------------------------------------------
# the operations (contract section 6)

#: ``if_version`` is accepted on every op (the page sends it on all of them) and enforced on move, restyle, edit and delete.
_COMMON = ("op", "intent", "if_version")
_STYLE = STYLE_FIELDS
#: One line on each core op, for ``docs/reference.md``.
CORE_OP_DOCS = {
    "claim": "an advisory claim on a region you are about to draw in (expires after 5 minutes)",
    "release": "release your claim (or all of them)", "legend": "say what a symbol means, or remove a legend entry",
    "move": "move, resize, re-point or re-frame elements, or rebind an arrow's ends",
    "restyle": "change the tone, variant, colour, size, font or dash of elements", "edit": "change an element's text",
    "delete": "delete elements (a frame with its children when asked)", "portrait": "your plan as a frame of steps in your home",
    "resolve": "resolve a comment", "lock": "the operator locks a region against agents", "unlock": "the operator lifts a lock",
    "undo": "undo a batch (yours; the manager any agent's; the operator anything)",
    "refit": "size labels again from their minimum, under the fonts and the page's measurements (none named: all you may edit)",
    "patch": "add, update, remove or re-set items inside a block (a kanban's cards, a table's rows); it re-lays out",
    "place": "move elements as one group beside another, to a point, or into a container at an index",
    "pin": "hold elements where they are: no layout, growth or other author moves them",
    "unpin": "let go of pins (an agent cannot lift the operator's); the block re-lays out",
}
#: The fields of the core ops; a kind module's op takes ``_COMMON`` + its ``OpSpec.fields`` (+ placement, + style).
CORE_FIELDS: Dict[str, Tuple[str, ...]] = {
    "claim": _COMMON + ("region", "label"),
    "release": _COMMON + ("id",),
    "legend": _COMMON + ("symbol", "meaning", "remove"),
    "move": _COMMON + ("id", "ids", "to", "by", "w", "h", "points", "from", "to_element", "frame",
                       "right_of", "left_of", "below", "above", "inside", "gap"),
    "restyle": _COMMON + ("id", "ids") + _STYLE,
    "edit": _COMMON + ("id", "text", "part"),
    "delete": _COMMON + ("id", "ids", "with_children"),
    "portrait": _COMMON + ("steps", "current", "title"),
    "resolve": _COMMON + ("id",),
    "lock": _COMMON + ("region", "label"),
    "unlock": _COMMON + ("id",),
    "undo": _COMMON + ("batch",),
    "refit": _COMMON + ("id", "ids"),
    "patch": _COMMON + ("id", "add", "update", "remove", "set", "relayout"),
    "place": _COMMON + ("id", "ids", "right_of", "left_of", "below", "above", "in", "at", "gap", "align", "index"),
    "pin": _COMMON + ("id", "ids"),
    "unpin": _COMMON + ("id", "ids", "relayout"),
}


def _point_list(value: Any, field: str, maximum: int, limit_name: str, pressure: bool = False, minimum: int = 2,
                local: Optional[Tuple[float, float, float, float]] = None) -> List[List[float]]:
    if not isinstance(value, (list, tuple)):
        raise _invalid(field, "{} must be a list of points".format(field))
    if len(value) > maximum:
        raise _too_big(field, limit_name, maximum, "{} has {} points; the limit is {}".format(field, len(value), maximum))
    if len(value) < minimum:
        raise _invalid(field, "{} needs at least {} points".format(field, minimum))
    return [_point(p, field, None, pressure, local) for p in value]


def _under_labels(ctx: _Ctx, el: Dict[str, Any]) -> None:
    """An unlabelled shape is a background others sit on (walls behind a door): drawn after a labelled shape
    it covers, its fill hid that shape's label (QA F-2, the walls over the roof's second line). It goes
    just under the lowest labelled shape it covers, never under its own frame; one it sits wholly inside
    (a panel it is drawn on) keeps it on top."""
    kind = _kinds.kind_of(el)
    if kind is None or not kind.hosts or kind.tone_group != "shape" or str(el.get("text") or "").strip():
        return  # only a shape drawn as a background (box, ellipse, diamond): a note is paper, not a wall
    box = bounds(el)
    covered = [int(other.get("z") or 0) for other in ctx.live()
               if other["id"] != el["id"] and other.get("type") in TEXT_TYPES and str(other.get("text") or "").strip()
               and _check._intersects(box, bounds(other), _check.TOUCH) and not _contains(bounds(other), box)]
    if not covered:
        return
    frame = ctx.el(el["frame"]) if el.get("frame") else None
    floor = int(frame.get("z") or 0) if frame is not None else -(10 ** 9)
    el["z"] = max(min(covered) - 1, floor)


def _graph_nodes(nodes_raw: List[Any]) -> List[Dict[str, Any]]:
    """A ``graph`` op's nodes, validated: ``{"id", "text", "kind", "tone", "color", "fill", "fill_set"}``."""
    nodes: List[Dict[str, Any]] = []
    seen = set()
    for index, node in enumerate(nodes_raw):
        field = "nodes[{}]".format(index)
        if isinstance(node, str):
            node = {"id": node}
        if not isinstance(node, dict):
            raise _invalid(field, "{} must be an object with id and text".format(field))
        for key in node:
            if key not in ("id", "text", "kind", "tone", "color", "fill"):
                raise _invalid("{}.{}".format(field, key), "a node takes id, text, kind, tone, color, fill")
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
            "tone": _choice(node["tone"], field + ".tone", TONES, "neutral") if node.get("tone") is not None else None,
            "color": node["color"] if node.get("color") is not None and _stroke_color(node["color"]) else None,
            "fill": _fill_color(node["fill"]) if node.get("fill") is not None else None,
            "fill_set": "fill" in node,
        })
    return nodes


def _artifact_rel(ctx: _Ctx, path: Path) -> str:
    root = artifacts_dir(ctx.layout, ctx.team, ctx.doc)
    base = Path(os.path.realpath(os.fspath(root))) if root is not None else path.parent
    return path.relative_to(base).as_posix()


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
    """The fields a move by ``(dx, dy)`` changes: its corner, and whatever its kind moves with it (``Kind.translate``: an
    arrow's points and label spot, a comment's pin). ``unbind``: a connector moved on purpose lets go of its ends."""
    fields: Dict[str, Any] = {"x": _round(float(el.get("x") or 0) + dx), "y": _round(float(el.get("y") or 0) + dy)}
    kind = _kinds.kind_of(el)
    if kind is not None and kind.translate is not None:
        fields.update(kind.translate(el, dx, dy))
    elif isinstance(el.get("points"), list):
        fields["points"] = _kc.shifted_points(el, dx, dy)  # a kind this build does not know keeps its points with it
    if unbind and kind is not None and kind.role == "connector":
        fields["from"] = None
        fields["to"] = None
    return _on_canvas(el, fields)


def _resized(ctx: _Ctx, el: Dict[str, Any], w: Any, h: Any) -> Dict[str, Any]:
    """The fields a resize to ``w`` x ``h`` changes: its kind's (``Kind.resize``), else its size, or for a kind sized from
    its label the size that label needs over the new minimum (0.22)."""
    kind = _kinds.kind_of(el)
    if kind is not None and kind.resize is not None:
        return _on_canvas(el, kind.resize(el, w, h, _KindCtx(ctx)), "w")
    old_w, old_h = max(1.0, float(el.get("w") or 1)), max(1.0, float(el.get("h") or 1))
    new_w = _num(w, "w", 1, MAX_SIZE) if w is not None else old_w
    new_h = _num(h, "h", 1, MAX_SIZE) if h is not None else old_h
    if kind is not None and kind.measure is not None:
        # A new minimum (0.22): the shape takes the size asked for, or more when its label needs more.
        minimum = _minimum(el, max(1, _round(new_w)) if w is not None else None, max(1, _round(new_h)) if h is not None else None)
        return _on_canvas(el, _fitted(el, minimum), "w")
    return _on_canvas(el, {"w": max(1, _round(new_w)), "h": max(1, _round(new_h))}, "w")


def _op_move(ctx: _Ctx, op: Dict[str, Any]) -> None:
    targets = _targets(ctx, op)
    _blocks()._check_pins(ctx, targets)
    before = {t["id"]: dict(t) for t in targets}
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
            fields = _translated(el, dx, dy, unbind=eid in explicit)
            if eid in explicit and el.get("nudged"):
                fields["nudged"] = None  # put where it is on purpose: that is where its author wants it now
            moved = ctx.update(el, **fields)
            boxes.append(bounds(moved))
            changed.append(eid)
    if resize:
        for eid in explicit:
            el = ctx.el(eid)
            boxes.append(bounds(el))  # type: ignore[arg-type]
            resized = ctx.update(el, **_resized(ctx, el, op.get("w"), op.get("h")))  # type: ignore[arg-type]
            boxes.append(bounds(resized))
            changed.append(eid)
            if resized.get("type") in TEXT_TYPES or resized.get("type") == "frame":
                _grow_parents(ctx, resized)
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
    # Blocks (phase 2, 1.5): a moved member of a positional block is pinned, one in a stack reordered.
    _blocks().after_move(ctx, explicit, before)
    for eid in explicit:
        el = ctx.el(eid)
        if el is not None:
            _warn_claims(ctx, [eid], bounds(el))
            # A block's member is framed by the block (its groups hug it once the op settles).
            if not (isinstance(el.get("frame"), str) and _blocks().arranged(ctx.el(el["frame"]))) and _blocks().root_of(ctx, el) is None:
                _warn_frame_edge(ctx, el)


def _op_restyle(ctx: _Ctx, op: Dict[str, Any]) -> None:
    targets = _targets(ctx, op)
    if not any(op.get(key) is not None for key in STYLE_FIELDS) and "fill" not in op:
        raise _invalid("color", "restyle needs at least one of: {}".format(", ".join(STYLE_FIELDS)))
    boxes = []
    resized: List[str] = []
    rerouted: List[str] = []
    for target in targets:
        el = ctx.el(target["id"]) or target
        kind = str(el.get("type"))
        style = _style(op, el.get("style") if isinstance(el.get("style"), dict) else _default_style(kind), kind)
        if op.get("route") is not None:
            rerouted.append(el["id"])
        if el.get("type") == "pen" and style.get("fill") and not el.get("closed"):
            raise _invalid("fill", "{} is an open stroke; only closed strokes take a fill".format(el["id"]))
        boxes.append(bounds(el))
        fields: Dict[str, Any] = {"style": style}
        if op.get("route") is not None and kind == "arrow" and el.get("curve") and style.get("route") != "curved":
            fields["curve"] = False  # a curve is a route style too: the new one replaces it (QA phase 2, F11)
        if _free_text(_kinds.kind_of(el)):
            fields.update(_text_fields(el, str(el.get("text") or ""), style))
        elif kind in TEXT_TYPES:
            # A new font or size refits the label (0.22).
            restyled_el = dict(el, style=style)
            fields.update(_fitted(restyled_el, _minimum(restyled_el)))
        if isinstance(el.get("pin"), dict) and el["pin"].get("by") == HUMAN and not ctx.author.is_human and \
                (fields.get("w", el.get("w")), fields.get("h", el.get("h"))) != (el.get("w"), el.get("h")):
            raise _error("pin_held", "{} was placed by the operator; restyling it would resize it. Ask them".format(el["id"]), id=el["id"])
        restyled = ctx.update(el, **fields)
        boxes.append(bounds(restyled))
        if kind in TEXT_TYPES and bounds(restyled) != bounds(el):
            resized.append(el["id"])
            _grow_parents(ctx, restyled)
            _after_growth(ctx, ctx.el(el["id"]) or restyled, bounds(el))
            _warn_frame_edge(ctx, ctx.el(el["id"]) or restyled)
    _check_locks(ctx, boxes)
    _reroute_bound(ctx, resized)
    for eid in rerouted:
        arrow = ctx.el(eid)
        if arrow is not None and len(arrow.get("points") or []) >= 2:
            _reroute(ctx, arrow)


def _op_edit(ctx: _Ctx, op: Dict[str, Any]) -> None:
    el = _targets(ctx, op, single=True)[0]
    kind = _kinds.kind_of(el)
    if "text" not in op:
        raise _invalid("text", "edit needs text")
    if op.get("part") is not None:
        _blocks().edit_part(ctx, el, op["part"], op["text"])  # an inline part, or a card's title or body (phase 2, 5.1)
        return
    if kind is None or kind.edit_field is None:
        raise _invalid("text", "a {} has no text".format(el.get("type")))
    maximum, limit_name = _KindCtx.LIMITS.get(kind.edit_limit, _KindCtx.LIMITS["label"])
    text = _text(op["text"], "text", maximum, limit_name, one_line=kind.edit_limit == "label", required=kind.edit_required)
    fields: Dict[str, Any] = {"text": text}
    if _free_text(kind):
        fields.update(_text_fields(el, text, el.get("style") if isinstance(el.get("style"), dict) else {}))
    elif kind.measure is not None:
        # The label is refitted from the element's minimum (0.22), so a shorter text shrinks the box back.
        fields.update(_fitted(dict(el, text=text), _minimum(el)))
    _check_locks(ctx, [bounds(el)])
    if isinstance(el.get("pin"), dict) and el["pin"].get("by") == HUMAN and not ctx.author.is_human and \
            (fields.get("w", el.get("w")), fields.get("h", el.get("h"))) != (el.get("w"), el.get("h")):
        raise _error("pin_held", "{} was placed by the operator; this edit would resize it. Ask them".format(el["id"]), id=el["id"])
    edited = ctx.update(el, **fields)
    _check_locks(ctx, [bounds(edited)])
    if bounds(edited) != bounds(el):
        _grow_parents(ctx, edited)
        _reroute_bound(ctx, [el["id"]])
        _after_growth(ctx, ctx.el(el["id"]) or edited, bounds(el))
    _warn_overlap(ctx, ctx.el(el["id"]) or edited)
    _warn_frame_edge(ctx, ctx.el(el["id"]) or edited)


def _op_delete(ctx: _Ctx, op: Dict[str, Any]) -> None:
    targets = _targets(ctx, op)
    with_children = _bool(op.get("with_children"), "with_children", False)
    if not ctx.author.is_human:
        held = next((t for t in targets if isinstance(t.get("pin"), dict) and t["pin"].get("by") == HUMAN), None)
        if held is not None:
            raise _error("pin_held", "{} was placed by the operator; ask them before deleting it".format(held["id"]), id=held["id"])
    doomed = [t["id"] for t in targets]
    # A block member's removal takes the block's arrows bound only to it (phase 2, 1.7).
    members = {t["id"] for t in targets if isinstance(t.get("group"), str) and isinstance(t.get("part"), str)}
    for other in list(ctx.live()):
        if other.get("type") == "arrow" and other["id"] not in doomed and isinstance(other.get("group"), str) and \
                (other.get("from") in members or other.get("to") in members) and _may_edit(ctx.author, other):
            doomed.append(other["id"])
    if with_children:
        for eid in _descendants(ctx, doomed):
            el = ctx.el(eid)
            if el is not None and not ctx.author.is_human and isinstance(el.get("pin"), dict) and el["pin"].get("by") == HUMAN:
                raise _error("pin_held", "deleting with children would delete {}, which the operator placed; ask them".format(eid), id=eid)
            if el is not None and not _may_edit(ctx.author, el):
                raise _error("element_not_yours", "deleting with children would delete {} ({}'s)".format(eid, _who(el.get("author"), None)),
                             id=eid, author=el.get("author"))
            doomed.append(eid)
    else:
        _blocks().rehome(ctx, doomed)
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
        if "group" in fields and el.get("part") is not None:
            fields["part"] = None  # a member of a deleted block is a loose element now (phase 2, 1.7)
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
        frame = ctx.put(ctx.element("frame", x, y, PORTRAIT_W, height, text=title, style=dict(_default_style("frame"), stroke=color), role=PORTRAIT_ROLE))
    fx, fy = float(frame["x"]), float(frame["y"])
    old_steps = sorted((el for el in ctx.live() if el.get("group") == frame["id"] and el.get("role") == PORTRAIT_ROLE and el["id"] != frame["id"]),
                       key=lambda el: (float(el.get("y") or 0), _id_number(el.get("id"))))
    for index, (text, status) in enumerate(steps):
        # A portrait is about its author, so its steps keep the author's colour (the one place colour is authorship).
        style = dict(_default_style("box"), stroke=color, size=16)
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


#: The most elements one ``refit`` sizes again (the whole canvas when it names none: every element the author may edit).
MAX_REFIT = 500


def _refittable(el: Dict[str, Any]) -> bool:
    kind = _kinds.kind_of(el)
    return (kind is not None and kind.measure is not None and bool(str(el.get("text") or "").strip())) or \
        (el.get("type") == "arrow" and bool(el.get("text")))


def _op_refit(ctx: _Ctx, op: Dict[str, Any]) -> None:
    """Size labelled elements again from their minimum under today's metrics and the page's corrections (canvas v2
    phase 1, 2.6): a board stored before 0.22 gets the sizes its labels need (QA R-9), and a line the browser measured
    wider than the metrics said gets its room. It only ever grows a box; a grown shape makes room like any other."""
    explicit = op.get("id") is not None or op.get("ids") is not None
    if explicit:
        if isinstance(op.get("ids"), list) and len(op["ids"]) > MAX_REFIT:
            raise _too_big("ids", "MAX_REFIT", MAX_REFIT, "refit takes at most {} ids".format(MAX_REFIT))
        targets = _targets(ctx, op)
    else:
        targets = [el for el in ctx.live() if _may_edit(ctx.author, el) and _refittable(el)]
        targets.sort(key=lambda el: (int(el.get("z") or 0), _id_number(el.get("id"))))
        targets = targets[:MAX_REFIT]
    for target in targets:
        el = ctx.el(target["id"]) or target
        if explicit and _blocks()._is_root(el):
            _blocks().refit_root(ctx, el)  # a block rebuilds from its own spec (phase 2, 1.8)
            continue
        if not _refittable(el):
            continue
        old = bounds(el)
        if el.get("type") == "arrow":
            fit = _label_fit(el)
            if fit is not None and el.get("fit") != fit:
                ctx.update(el, fit=fit)  # the label settles (label_at, lines) after the op
            continue
        style = el.get("style") if isinstance(el.get("style"), dict) else {}
        if el.get("type") == "text":
            fields = _text_fields(el, str(el.get("text") or ""), style)
        else:
            asked = _minimum(el)
            fields = _fitted(el, (max(asked[0], float(el.get("w") or 1)), max(asked[1], float(el.get("h") or 1))))
            if fields.get("fit"):
                fields["fit"]["min"] = [int(round(asked[0])), int(round(asked[1]))]
        if not fields:
            continue
        # Grow only: a box keeps any room it had (a refit never moves what sits beside it back).
        fields["w"] = max(int(fields["w"]), int(math.ceil(float(el.get("w") or 1) - 1e-9)))
        fields["h"] = max(int(fields["h"]), int(math.ceil(float(el.get("h") or 1) - 1e-9)))
        if all(el.get(key) == value for key, value in fields.items()):
            continue
        grown = (old[0], old[1], old[0] + fields["w"], old[1] + fields["h"])
        if grown != tuple(old):
            if explicit:
                _check_locks(ctx, [grown])
            else:
                try:
                    _check_locks(ctx, [grown])
                except HerdrTeamError:
                    continue  # a whole-canvas refit leaves what sits in a lock as it is
        refitted = ctx.update(el, **fields)
        if bounds(refitted) != tuple(old):
            _grow_parents(ctx, refitted)
            _reroute_bound(ctx, [el["id"]])
            _after_growth(ctx, ctx.el(el["id"]) or refitted, old)


def _op_patch(ctx: _Ctx, op: Dict[str, Any]) -> None:
    _blocks().op_patch(ctx, op)


def _op_place(ctx: _Ctx, op: Dict[str, Any]) -> None:
    _blocks().op_place(ctx, op)


def _op_pin(ctx: _Ctx, op: Dict[str, Any]) -> None:
    _blocks().op_pin(ctx, op)


def _op_unpin(ctx: _Ctx, op: Dict[str, Any]) -> None:
    _blocks().op_unpin(ctx, op)


CORE_HANDLERS: Dict[str, Callable[[_Ctx, Dict[str, Any]], None]] = {
    "claim": _op_claim, "release": _op_release, "legend": _op_legend, "move": _op_move,
    "restyle": _op_restyle, "edit": _op_edit, "delete": _op_delete, "portrait": _op_portrait, "resolve": _op_resolve,
    "lock": _op_lock, "unlock": _op_unlock, "undo": _op_undo, "refit": _op_refit,
    "patch": _op_patch, "place": _op_place, "pin": _op_pin, "unpin": _op_unpin,
}


# --------------------------------------------------------------------------
# the kind registry's ops (canvas v2 phase 1, 2): a kind module draws through ``_KindCtx``, never through ``canvas``


def _free_text(kind: Optional[_kinds.Kind]) -> bool:
    """A kind whose whole content is its text (a free ``text``): its box is its lines, and it needs a text."""
    return kind is not None and kind.measure is not None and not kind.labelled


class _KindCtx:
    """The ``canvas_kinds.sdk.OpContext`` an op handler of a kind module gets: the batch's ``_Ctx``, narrowed."""

    #: ``limit`` names of ``text``: the most characters, and the limit's name in a refusal.
    LIMITS = {"text": (MAX_TEXT_CHARS, "MAX_TEXT_CHARS"), "label": (MAX_LABEL_CHARS, "MAX_LABEL_CHARS"),
              "comment": (MAX_COMMENT_CHARS, "MAX_COMMENT_CHARS"), "symbol": (MAX_SYMBOL_CHARS, "MAX_SYMBOL_CHARS")}

    def __init__(self, ctx: _Ctx) -> None:
        self._ctx = ctx

    @property
    def author_name(self) -> str:
        return self._ctx.author.name

    @property
    def author_is_human(self) -> bool:
        return self._ctx.author.is_human

    @property
    def intent(self) -> str:
        return self._ctx.intent

    @property
    def now(self) -> float:
        return self._ctx.now

    @property
    def team_name(self) -> str:
        return self._ctx.team.name

    # refusals
    def invalid(self, field: str, message: str, **details: Any) -> HerdrTeamError:
        return _invalid(field, message, **details)

    def error(self, code: str, message: str, **details: Any) -> HerdrTeamError:
        return _error(code, message, **details)

    def too_big(self, field: str, limit: str, maximum: int, message: str) -> HerdrTeamError:
        return _too_big(field, limit, maximum, message)

    # reading fields
    def text(self, op: Dict[str, Any], field: str, *, limit: str = "text", one_line: bool = False, required: bool = False,
             value: Any = ..., label: Optional[str] = None) -> str:
        maximum, name = self.LIMITS[limit]
        return _text(op.get(field) if value is ... else value, label or field, maximum, name, one_line=one_line, required=required)

    def choice(self, op: Dict[str, Any], field: str, choices: Sequence[str], default: str, *, value: Any = ..., label: Optional[str] = None) -> str:
        return _choice(op.get(field) if value is ... else value, label or field, choices, default)

    def number(self, op: Dict[str, Any], field: str, low: float, high: float, default: Optional[float] = None) -> Optional[float]:
        return default if op.get(field) is None else _num(op[field], field, low, high)

    def boolean(self, op: Dict[str, Any], field: str, default: bool) -> bool:
        return _bool(op.get(field), field, default)

    def points(self, op: Dict[str, Any], field: str, maximum: int, *, pressure: bool = False, minimum: int = 2, limit_name: str = "") -> List[List[float]]:
        return _point_list(op.get(field), field, maximum, limit_name or "MAX_POINTS", pressure, minimum, self._ctx.local)

    def point(self, value: Any, field: str) -> List[float]:
        return _point(value, field, None, False, self._ctx.local)

    def is_point(self, value: Any) -> bool:
        return _is_point_form(value)

    def color(self, value: Any) -> str:
        return _stroke_color(value)

    def fill(self, value: Any) -> Optional[str]:
        return _fill_color(value)

    def guard_code(self, text: str, field: str) -> None:
        _guard_code(text, field)

    # style and size
    def style(self, op: Dict[str, Any], kind: str, base: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return _style(op, base if base is not None else _default_style(kind), kind)

    def default_style(self, kind: str) -> Dict[str, Any]:
        return _default_style(kind)

    def size(self, op: Dict[str, Any], default_w: float, default_h: float) -> Tuple[float, float]:
        return _size(op, default_w, default_h)

    def fit(self, el: Dict[str, Any], minimum: Sequence[float]) -> Dict[str, Any]:
        return _fitted(el, minimum)

    def text_fields(self, el: Dict[str, Any], text: str, style: Dict[str, Any], wrap_w: Optional[float] = None) -> Dict[str, Any]:
        return _text_fields(el, text, style, wrap_w)

    def minimum(self, el: Dict[str, Any], w: Any = None, h: Any = None) -> Tuple[float, float]:
        return _minimum(el, w, h)

    def shape_size(self, kind: str) -> Tuple[float, float]:
        found = SHAPE_SIZES.get(kind) or _theme.size_min(kind)
        return float(found[0]), float(found[1])

    # the canvas
    def lookup(self, ref: Any, field: str) -> Dict[str, Any]:
        return self._ctx.lookup(ref, field)

    def live(self) -> List[Dict[str, Any]]:
        return list(self._ctx.live())

    def el(self, eid: str) -> Optional[Dict[str, Any]]:
        return self._ctx.el(eid)

    def may_edit(self, el: Dict[str, Any]) -> bool:
        return _may_edit(self._ctx.author, el)

    def update(self, el: Dict[str, Any], **fields: Any) -> Dict[str, Any]:
        return self._ctx.update(el, **fields)

    def region(self, value: Any, field: str) -> List[int]:
        return _region(value, field, self._ctx.lookup)

    def place(self, op: Dict[str, Any], w: float, h: float, asked: Optional[Tuple[float, float]] = None) -> Tuple[float, float, Optional[str]]:
        return _place(self._ctx, op, w, h, asked)

    def enclosing_frame(self, box: Sequence[float]) -> Optional[str]:
        return _enclosing_frame(self._ctx, box)

    def geometry(self, points: Sequence[Sequence[float]]) -> Dict[str, int]:
        return _geometry(points)

    def create(self, kind: str, x: float, y: float, w: float, h: float, *, op: Dict[str, Any], text: str = "", style: Optional[Dict[str, Any]] = None,
               frame: Optional[str] = None, store: Optional[Dict[str, Tuple[bytes, str]]] = None, **fields: Any) -> Dict[str, Any]:
        ctx = self._ctx
        registered = _kinds.get(kind)
        alias = _alias(ctx, op)
        client = _client_id(op)
        extra: Dict[str, Any] = {name: None for name in store or {}}
        extra.update(fields)
        ctx.alias = alias
        el = ctx.element(kind, x, y, w, h, text=text, style=style, alias=alias, client_id=client, frame=frame, **extra)
        role = registered.role if registered is not None else "leaf"
        if role == "container":
            _check_locks(ctx, [bounds(el)])
            ctx.put(el)
            _warn_claims(ctx, [el["id"]], bounds(el))
            return el
        if store:
            _check_locks(ctx, [bounds(el)])  # before anything is stored: a refused op leaves no asset behind
            for name, (data, asset_kind) in store.items():
                el[name] = store_asset(ctx.team, data, asset_kind)["asset"]
        if role == "overlay":
            _check_locks(ctx, [bounds(el)])
            ctx.put(el)
            return el
        # A kind sized from its label makes room for its neighbours and sits on its host (QA R-1).
        sized = registered is not None and registered.measure is not None
        if sized and op.get("inside") is None and op.get("in") is None:
            _make_way(ctx, el)
        if sized:
            _under_labels(ctx, el)
        _after_create(ctx, el)
        if sized:
            _hold_on_host(ctx, el)
        if role == "connector" and registered is not None and registered.reroute is not None and \
                (style or {}).get("route") not in (None, "straight") and len(el.get("points") or []) >= 2:
            _reroute(ctx, ctx.el(el["id"]) or el)  # a route style routes it now (phase 2, 3.4)
        return ctx.el(el["id"]) or el

    def route(self, op: Dict[str, Any], *, label: str, style: Dict[str, Any], curve: bool) -> Tuple[List[List[float]], Optional[str], Optional[str]]:
        ctx = self._ctx
        if op.get("points") is not None:
            if op.get("from") is not None or op.get("to") is not None:
                raise _invalid("points", "an arrow takes points, or from and to, not both")
            return _point_list(op["points"], "points", MAX_ARROW_POINTS, "MAX_ARROW_POINTS"), None, None
        if op.get("from") is None or op.get("to") is None:
            raise _invalid("from" if op.get("from") is None else "to", "an arrow needs from and to (an element or a point), or points")
        start, end = _end(ctx, op["from"], "from"), _end(ctx, op["to"], "to")
        points = _route(start, end)
        start_id = start[1]["id"] if start[0] == "element" else None
        end_id = end[1]["id"] if end[0] == "element" else None
        if label and start_id and end_id:
            points = _room_for_label(ctx, {"type": "arrow", "text": label, "style": style, "curve": curve}, start_id, end_id, points)
        return points, start_id, end_id

    def image(self, op: Dict[str, Any]) -> Tuple[bytes, str, int, int]:
        ctx = self._ctx
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
        return data, sniffed[0], sniffed[1], sniffed[2]

    def artifact(self, rel: Any) -> str:
        ctx = self._ctx
        return _artifact_rel(ctx, artifact_file(ctx.layout, ctx.team, rel, ctx.doc))

    def require_viz(self) -> None:
        _features.require_viz(self._ctx.layout.session, self._ctx.team, self._ctx.doc)

    def expand_graph(self, op: Dict[str, Any], title: str, nodes: List[Dict[str, Any]], edges: List[Dict[str, Any]], algorithm: str,
                     direction: str, subgraphs: Sequence[Dict[str, Any]] = ()) -> None:
        """Phase 1's graph expansion, kept for one release as a thin wrapper over the ``graph`` block (phase 2, I-15)."""
        spec: Dict[str, Any] = {key: op[key] for key in PLACE_FIELDS + ("id", "client_id") if op.get(key) is not None}
        spec.update(op="graph", title=title, layout=algorithm, direction=direction,
                    nodes=[{key: node[key] for key in ("id", "text", "kind", "tone") if node.get(key) is not None}
                           | ({"in": node["subgraph"]} if node.get("subgraph") else {}) for node in nodes],
                    edges=[{"from": edge["from"], "to": edge["to"], "label": edge.get("label") or None,
                            "style": edge.get("dash") or "solid"} for edge in edges],
                    groups=[{"id": sg["id"], "title": sg.get("title") or sg["id"], "parent": sg.get("parent")} for sg in subgraphs])
        spec["edges"] = [{k: v for k, v in edge.items() if v is not None} for edge in spec["edges"]]
        spec["groups"] = [{k: v for k, v in group.items() if v is not None} for group in spec["groups"]]
        _blocks().run(self._ctx, "graph", spec)

    def graph_nodes(self, raw: List[Any]) -> List[Dict[str, Any]]:
        return _graph_nodes(raw)

    def mentions(self, explicit: Any, text: str) -> List[str]:
        return _mentions(self._ctx, explicit, text)

    def warn(self, code: str, message: str, ids: Sequence[str]) -> None:
        self._ctx.warn(code, message, ids)

    def comment(self, ref: Any, field: str) -> Dict[str, Any]:
        return _comment_ref(self._ctx, ref, field)

    def mention(self, comment: Dict[str, Any]) -> None:
        self._ctx.mention = comment

    def block(self, kind: str, spec: Dict[str, Any]) -> Dict[str, Any]:
        return _blocks().run(self._ctx, kind, spec)

    def obstacles(self, box: Sequence[float], exclude: Sequence[str]) -> List[Tuple[str, Tuple[float, float, float, float], str]]:
        return _obstacles(self._ctx, box, exclude)


def _kind_handler(spec: _kinds.OpSpec) -> Callable[[_Ctx, Dict[str, Any]], None]:
    def handler(ctx: _Ctx, op: Dict[str, Any]) -> None:
        if op.get("in") is not None:
            if "in" not in _FIELDS.get(spec.name, ()):
                raise _invalid("in", "{} does not take in".format(spec.name))
            container = ctx.lookup(op["in"], "in")
            ctx.local = _local_grid(ctx, container) if op.get("at") is not None or op.get("points") is not None else None
            if ctx.local is None and (op.get("points") is not None and op.get("at") is None) and container.get("block"):
                raise _invalid("points", "points go with in only for a free section (layout free)")
        spec.create(_KindCtx(ctx), op)
        if op.get("in") is not None and ctx.join is None and ctx.created:
            # A kind that places itself (a pen from its points) still joins the container it was put in.
            first = ctx.el(ctx.created[0])
            if first is not None and first.get("frame") != container["id"] and not isinstance(first.get("group"), str):
                ctx.update(first, frame=container["id"])
    handler.__name__ = "_op_" + spec.name
    return handler


ELEMENT_TYPES: Tuple[str, ...] = ()
SHAPE_KINDS: Tuple[str, ...] = ()
TEXT_TYPES: frozenset = frozenset()
CELL_TYPES: frozenset = frozenset()
#: The minimum size of each shape kind (design tokens ``size_min``): an op's ``w``/``h`` replaces it, and a
#: shape grows past it to fit its label (0.22).
SHAPE_SIZES: Dict[str, Tuple[int, int]] = {}
OPS: Tuple[str, ...] = ()
_FIELDS: Dict[str, Tuple[str, ...]] = {}
_HANDLERS: Dict[str, Callable[[_Ctx, Dict[str, Any]], None]] = {}
_NOUNS: Dict[str, Tuple[str, str]] = {}
_COUNT_ORDER: Tuple[str, ...] = ()


def _derive() -> None:
    """Every table a kind used to be listed in, from the registry (phase 1, 2.4): the element types, the shape kinds, the
    kinds with text and with a cell, the ops with their fields and handlers, and the nouns change summaries use."""
    global ELEMENT_TYPES, SHAPE_KINDS, TEXT_TYPES, CELL_TYPES, SHAPE_SIZES, OPS, _FIELDS, _NOUNS, _COUNT_ORDER
    kinds = _kinds.kinds()
    ELEMENT_TYPES = tuple(_kinds.element_types())  # a section is a kind, stored as a frame (phase 2, D2)
    SHAPE_KINDS = tuple(kind.name for kind in _kinds.subkinds("shape"))
    TEXT_TYPES = frozenset(kind.name for kind in kinds if kind.measure is not None)
    CELL_TYPES = frozenset(kind.name for kind in kinds if kind.cell)
    SHAPE_SIZES = {kind.name: (int(w), int(h)) for kind, (w, h) in ((k, _theme.size_min(k.name)) for k in _kinds.subkinds("shape"))
                   if kind.handles == "box"}
    specs = _kinds.ops()
    clash = [spec.name for spec in specs if spec.name in CORE_OPS]
    if clash:
        raise ValueError("kind ops {} clash with the core ops".format(", ".join(clash)))
    OPS = tuple(spec.name for spec in specs) + CORE_OPS
    # A field an op names itself and also takes as placement or style (a section's gap, a graph's route) is listed once.
    fields = {spec.name: tuple(dict.fromkeys(_COMMON + spec.fields + (PLACE_FIELDS if spec.place else ()) + (STYLE_FIELDS if spec.style else ())))
              for spec in specs}
    fields.update(CORE_FIELDS)
    _FIELDS = fields
    _HANDLERS.clear()  # the same dict object: tests patch it in place
    _HANDLERS.update({spec.name: _kind_handler(spec) for spec in specs})
    _HANDLERS.update(CORE_HANDLERS)
    _NOUNS = {kind.name: kind.nouns() for kind in kinds if kind.role != "overlay"}
    _NOUNS.update(_OTHER_NOUNS)
    _COUNT_ORDER = tuple(kind.name for kind in kinds if kind.role != "overlay") + _COUNT_TAIL



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
    """Validate and apply a batch under ``canvas.lock``; the apply result (contract 7.2). Batch-level refusals raise.

    Labels are measured under the team's browser corrections (``measure.json``, canvas v2 phase 1)."""
    with _corrected(team):
        return _apply_ops(layout, team, ops, author, atomic, doc, now)


def _apply_ops(layout: Any, team: TeamPaths, ops: List[Dict[str, Any]], author: CanvasAuthor, atomic: bool,
               doc: Optional[Dict[str, Any]], now: Optional[float]) -> Dict[str, Any]:
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
    prepared = _blocks().prepare(ops)  # a block kind's own precomputation (a big layout, phase 2 1.3): never under canvas.lock
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
        ctx.prepared = prepared
        touched: List[str] = []
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
                _blocks().settle(ctx)  # containers whose members changed are arranged again (phase 2, 1.5)
                _blocks().refresh_note(ctx)
                _settle_labels(ctx)
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
                                          "message": "{} could not be applied: an internal error ({}), not a fault in the op; the rest of the "
                                                     "batch went ahead".format(name, type(err).__name__),
                                          "details": {"field": "op", "error": type(err).__name__}})
                continue
            geometry = _sized(ctx)
            state.fold(event)
            events.append(event)
            lines.append(line)
            entry: Dict[str, Any] = {"index": index, "op": name, "ids": list(event["ids"])}
            if ctx.alias:
                entry["alias"] = ctx.alias
            if geometry:
                entry["geometry"] = geometry[:MAX_GEOMETRY]
                if len(geometry) > MAX_GEOMETRY:
                    entry["geometry_omitted"] = len(geometry) - MAX_GEOMETRY
            if ctx.block_info is not None:
                entry["block"] = ctx.block_info
            touched.extend(i for i in event["ids"] if i not in touched)
            result["applied"].append(entry)
            result["warnings"].extend(ctx.warnings)
            result["aliases"].update(ctx.aliases)
            if ctx.mention is not None:
                mentions.append(ctx.mention)
        result["version"] = state.version
        result["batch"] = batch_id if events else None
        if events:
            # The batch's final geometry and its layout check (phase 2, 5.2): what an agent reads instead of guessing.
            found, omitted = _blocks().batch_geometry(state, touched, MAX_GEOMETRY)
            result["geometry"] = found
            if omitted:
                result["geometry_omitted"] = omitted
            result["check"] = _blocks().batch_check(state, touched, author.name if not author.is_human else HUMAN)
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


#: Sized elements an apply result lists per op; a big graph lists this many and counts the rest.
MAX_GEOMETRY = 50


def _sized(ctx: _Ctx) -> List[Dict[str, Any]]:
    """Where the op put every labelled element it created or refitted, and how its label was fitted (0.22),
    so an agent never has to guess the size a shape grew to."""
    out: List[Dict[str, Any]] = []
    for eid in ctx.created + ctx.changed:
        el = ctx.pending.get(eid)
        if el is None or el.get("type") not in TEXT_TYPES:
            continue
        before = ctx.state.elements.get(eid)
        if before is not None and (before.get("w"), before.get("h")) == (el.get("w"), el.get("h")) and before.get("fit") == el.get("fit"):
            continue  # moved, not sized
        entry: Dict[str, Any] = {"id": eid, "x": el.get("x"), "y": el.get("y"), "w": el.get("w"), "h": el.get("h")}
        fit = el.get("fit") if isinstance(el.get("fit"), dict) else None
        if fit:
            entry["fit"] = {"policy": fit.get("policy"), "size": fit.get("size"), "lines": len(fit.get("lines") or [])}
            for flag in ("truncated", "estimated"):
                if fit.get(flag):
                    entry["fit"][flag] = True
        out.append(entry)
    return out


def check_applied(result: Dict[str, Any]) -> Dict[str, Any]:
    """``canvas_refused`` (exit 1, details = the result) when a non-empty batch applied nothing; else the result."""
    if result.get("refused") and not result.get("applied"):
        first = result["refused"][0]
        raise _error("canvas_refused", "nothing applied: op {} {}: {}".format(first["index"], first["code"], first["message"]), **result)
    return result


# --------------------------------------------------------------------------
# board records: canvas_changed (coalesced awareness) and canvas_sent (a named wake)

#: What change summaries call the things that are not element kinds (the kinds say their own ``noun``).
_OTHER_NOUNS = {"comments": ("comment", "comments"), "claims": ("claim", "claims"), "legend": ("legend entry", "legend entries"),
                "locks": ("lock", "locks")}
_VERB_COUNTS = {"move": "moved", "restyle": "restyled", "edit": "edited", "release": "released", "resolve": "resolved",
                "undo": "undone", "unlock": "unlocked", "patch": "patched", "place": "moved", "pin": "pinned", "unpin": "unpinned"}
_COUNT_TAIL = ("comments", "claims", "legend", "locks", "portrait", "moved", "restyled", "edited", "deleted", "released", "resolved", "undone",
               "unlocked", "patched", "pinned", "unpinned")


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
                kind = _kinds.kind_of(value)  # a kanban is a kanban, not a frame (phase 2)
                bump(kind.name if kind is not None else str(value.get("type")))
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
    """One element as ``look`` prints it: ``full`` adds the cell (for boxy things) and `` — intent``. Each kind reads itself
    back (``Kind.readback``, or its whole ``line``); a kind this build does not know gets its type, text and box."""
    kind = str(el.get("type"))
    eid = str(el.get("id"))
    who = _who(el.get("author"), reader)
    text = str(el.get("text") or "")
    limit = 0 if full else 80
    registered = _kinds.kind_of(el)
    if registered is not None and registered.line is not None:
        return registered.line(el, reader, full)
    if registered is not None and registered.readback is not None:
        core = registered.readback(el, full)
    else:
        core = "{} {}{} {}".format(eid, kind, " " + _q(text, limit) if text else "", _bounds_text(el))
    if full and kind in CELL_TYPES:
        core += " " + cell_name(el.get("x") or 0, el.get("y") or 0)
    line = "{} by {}".format(core, who)
    intent = el.get("intent")
    if full and intent and intent != HUMAN_INTENT:
        line += " — {}".format(intent)
    return line


def _tree_lines(elements: Sequence[Dict[str, Any]], reader: Optional[str], base: int = 1, scene: Optional[Sequence[Dict[str, Any]]] = None,
                verbose: bool = False) -> List[str]:
    """Full lines with children indented under their frame (comments last). A block reads back as its spec, its members
    folded into it (phase 2, 5.3); an element in a container says where (`` in arch#1/2 (row)``, `` part c2 of work``);
    ``verbose`` (``look --full``) adds a block's part ids and a top-level element's neighbours."""
    blocks = _blocks()
    everything = list(scene) if scene is not None else list(elements)
    scene_by_id = {el["id"]: el for el in everything}
    folded = blocks.member_ids(everything)
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
        if el["id"] in folded:
            return
        if blocks._is_root(el) and blocks.kind_of(el).name != "section":
            block = blocks.block_lines(everything, el, reader, verbose)
            lines.extend("  " * depth + line for line in block)
            for child in children.get(el["id"], []):
                walk(child, depth + 1)
            return
        line = describe(el, reader, full=True)
        parent = by_id.get(el.get("frame")) if isinstance(el.get("frame"), str) else None
        intent = el.get("intent")
        tail = ""
        if parent is not None and intent and intent == parent.get("intent") and line.endswith(" — {}".format(intent)):
            line = line[: -len(" — {}".format(intent))]  # a graph's nodes repeat the graph's intent: say it once
        if " — " in line and not line.startswith(el["id"] + " comment"):
            line, _dash, tail = line.partition(" — ")
            tail = " — " + tail
        line += blocks.relation(everything, el, scene_by_id)
        if blocks.kind_of(el) is not None and blocks.kind_of(el).name == "section":
            kids = [scene_by_id[i] for i in blocks.stack_order(el, [o for o in everything if o.get("frame") == el["id"]])] \
                if blocks.stack_of(el) else [o for o in everything if o.get("frame") == el["id"] and o.get("type") != "comment"]
            if kids:
                line += ": " + " ".join(str(k.get("alias") or k["id"]) for k in kids[:20]) + (" …" if len(kids) > 20 else "")
        if verbose and el.get("frame") is None and el.get("type") != "comment":
            line += blocks.neighbours(el, everything)
        lines.append("  " * depth + line + tail)
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
    everything = [el for el in scene.get("elements") or [] if isinstance(el, dict)]
    chosen = [el for el in everything if el.get("id") in wanted]
    return "\n".join(_tree_lines(chosen, reader, base=0, scene=everything))


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
    if len(added) > 1 and (op in ("graph", "mermaid") or _blocks()._is_root(added[0])):
        frame = added[0]
        name = frame.get("alias") or frame.get("text") or op
        kind = _kinds.kind_of(frame)
        return "added {} {} {} ({} elements)".format(_ids_text([a["id"] for a in added]), op if op in ("graph", "mermaid") or kind is None else kind.name,
                                                     _q(name, 60), len(added))
    first = _kinds.kind_of(added[0]) if added else None
    if op not in CORE_OPS and first is not None and first.role != "overlay":
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
             "resolve": "resolved", "unlock": "unlocked", "patch": "patched", "place": "placed", "pin": "pinned", "unpin": "unpinned"}
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
    scene_elements = result.get("_scene") or full
    verbose = bool(result.get("full"))
    if result.get("block_text"):
        lines.append(result["block_text"])
    elif region:
        lines.append("region {}:".format(result.get("region_cells") or region_cells(region)))
        lines += _tree_lines(full, reader, scene=scene_elements, verbose=verbose) or ["  (nothing here)"]
    elif result.get("level") == "full":
        lines.append("elements:")
        lines += _tree_lines(full, reader, scene=scene_elements, verbose=verbose) or ["  (the canvas is empty)"]
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
    with _corrected(team):
        return _check_canvas(layout, team, reader, region, around, mine, doc)


def _check_canvas(layout: Any, team: TeamPaths, reader: str, region: Any, around: Optional[str], mine: bool,
                  doc: Optional[Dict[str, Any]]) -> Dict[str, Any]:
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
         doc: Optional[Dict[str, Any]] = None, theme: str = "light", block: Optional[str] = None, full: bool = False) -> Dict[str, Any]:
    """What ``reader`` sees (contract 8.1 and 8.2): the three-level listing, changes, claims, legend, and an optional image
    (drawn in ``theme``, ``light`` or ``dark``). ``block`` prints one block's whole spec, one item per line; ``full`` adds
    part ids, neighbours and details (phase 2, 5.3)."""
    with _corrected(team):
        return _look(layout, team, reader, region, around, since, image, grid, exact, advance, doc, theme, block, full)


def _look(layout: Any, team: TeamPaths, reader: str, region: Any, around: Optional[str], since: Any, image: bool, grid: bool,
          exact: bool, advance: bool, doc: Optional[Dict[str, Any]], theme: str, block: Optional[str] = None,
          full_detail: bool = False) -> Dict[str, Any]:
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
    blocks = _blocks()
    folded = blocks.member_ids(elements)
    one_line = [el for el in one_line if el["id"] not in folded]
    elsewhere = [{"id": el["id"], "type": el.get("type"), "bounds": [el.get("x"), el.get("y"), el.get("w"), el.get("h")],
                  "cell": cell_name(el.get("x") or 0, el.get("y") or 0), "text": el.get("text") or "", "author": el.get("author"),
                  "line": blocks.block_lines(elements, el, reader, False, 0)[0] if blocks._is_root(el) and blocks.kind_of(el).name != "section"
                  else describe(el, reader, full=False)} for el in one_line]
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
        "blocks": {el["id"]: blocks.spec_of(elements, el) for el in full if blocks._is_root(el)},
        "full": bool(full_detail), "_scene": elements,
    }
    if block is not None:
        target = lookup(block, "block")
        root = target if blocks._is_root(target) else (blocks.root_of(state_ctx(state), target) or target)
        if not blocks._is_root(root):
            raise _invalid("block", "{} is a {}, not a block".format(target["id"], target.get("type")))
        result["block"] = blocks.spec_of(elements, root, True)
        result["block_text"] = blocks.pretty(elements, root)
    if image:
        if exact:
            png = _exact_export(layout, team, view, grid, reader)
            if png:
                result.update(image=png, exact=True)
            else:
                result["image_error"] = "exact_timeout"
        if not result["exact"]:
            key = hashlib.sha256(json.dumps([box, bool(grid), theme], separators=(",", ":")).encode("utf-8")).hexdigest()[:8]
            rendered = _render.render_region(team, scene, box, _dir(team) / RENDERS_DIR, "look-{}-v{}-{}".format(reader, state.version, key),
                                             marks=True, grid=grid, reader=reader, theme=theme if theme in _theme.THEMES else "light")
            result["image"] = rendered["png"]
            result["svg"] = rendered["svg"]
            result["image_error"] = result["image_error"] or rendered["image_error"]
    result["text"] = look_text(result)
    result.pop("_scene", None)
    if advance:
        advance_cursor(team, reader, state.version)
    return result


class state_ctx:  # noqa: N801 - a small read-only view with the ``el`` and ``live`` a block helper asks for
    def __init__(self, state: "_State") -> None:
        self._state = state

    def el(self, eid: Any) -> Optional[Dict[str, Any]]:
        return self._state.elements.get(eid) if isinstance(eid, str) else None

    def live(self) -> Iterable[Dict[str, Any]]:
        return self._state.elements.values()


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


def _geometry_text(entry: Dict[str, Any]) -> str:
    """`` → 212x64 (hug, 2 lines)`` for one sized element, `` → 28 sized`` for more."""
    geometry = entry.get("geometry") or []
    if not geometry:
        return ""
    if len(geometry) > 1 or entry.get("geometry_omitted"):
        return " → {} sized".format(len(geometry) + int(entry.get("geometry_omitted") or 0))
    one = geometry[0]
    fit = one.get("fit") or {}
    notes = []
    if fit.get("policy"):
        count = int(fit.get("lines") or 0)
        notes.append("{}, {} line{}".format(fit["policy"], count, "" if count == 1 else "s"))
    for flag in ("truncated", "estimated"):
        if fit.get(flag):
            notes.append(flag)
    return " → {}x{}{}".format(one.get("w"), one.get("h"), " ({})".format(", ".join(notes)) if notes else "")


def apply_text(result: Dict[str, Any]) -> str:
    """The human text of an apply result: ``v44 · B-12 · applied 6, refused 1`` then one line per op."""
    lines = ["v{} · {} · applied {}, refused {}".format(result.get("version"), result.get("batch") or "no batch",
                                                       len(result.get("applied") or []), len(result.get("refused") or []))]
    for entry in result.get("applied") or []:
        alias = " ({})".format(entry["alias"]) if entry.get("alias") else ""
        lines.append("#{} {} {}{}{}".format(entry.get("index"), entry.get("op"), _ids_text(entry.get("ids") or []) or "-", alias,
                                          _geometry_text(entry)))
        block = entry.get("block") if isinstance(entry.get("block"), dict) else None
        if block and block.get("kind"):
            # The block's result (phase 2, 5.2): what it is now, what moved, the pins nobody may move.
            box = block.get("box") or [0, 0, 0, 0]
            parts = ["{} {} {}x{} at {}".format(block["id"], block["kind"] + ("/" + str(block["layout"]) if block.get("layout") else "") +
                                               ("/" + str(block["direction"]) if block.get("direction") else ""), box[2], box[3],
                                               cell_name(box[0], box[1])), "v{}".format(block.get("version"))]
            if block.get("upsert"):
                parts.append("updated in place")
            if block.get("moved"):
                parts.append("moved {}".format(len(block["moved"])))
            if "crossings" in block:
                parts.append("crossings {}".format(block["crossings"]))
            if block.get("pins"):
                names = block.get("pin_parts") or {}
                parts.append("pins " + ", ".join("{}({})".format(names.get(k, k), v) for k, v in sorted(block["pins"].items())))
            lines.append("   " + " · ".join(parts))
    if result.get("refused"):
        lines.append("refused:")
        lines += ["  #{} {} {}: {}".format(r.get("index"), r.get("op"), r.get("code"), r.get("message")) for r in result["refused"]]
    if result.get("warnings"):
        lines.append("warnings:")
        lines += ["  #{} {}: {}".format(w.get("index"), w.get("code"), w.get("message")) for w in result["warnings"]]
    check = result.get("check") if isinstance(result.get("check"), dict) else None
    if check is not None:
        counts = check.get("counts") or {}
        listed = " · ".join("{} {}".format(code, n) for code, n in counts.items())
        lines.append("check: {}".format(listed if any(counts.values()) else "clean"))
        for problem in (check.get("problems") or [])[:5]:
            lines.append("  - {}: {}".format(problem.get("code"), problem.get("message")))
            if problem.get("fix"):
                lines.append("    fix: {}".format(json.dumps(problem["fix"], separators=(",", ":"), ensure_ascii=False)))
    notices = result.get("notices") or {}
    if notices.get("canvas_sent"):
        lines.append("sent: board #{}".format(", #".join(str(s) for s in notices["canvas_sent"])))
    return "\n".join(lines)


# --------------------------------------------------------------------------
# the display list and the browser's check (canvas v2 phase 1: ``GET /display``, ``POST /measure``)

MEASURE_FILE = "measure.json"
#: Lines the browser measured wider than the metrics, kept per team (the oldest go first).
MAX_CORRECTIONS = 5000
#: One ``/measure`` request carries at most this many lines (and ``MAX_MEASURE_BYTES``).
MAX_MEASURE_LINES = 200
MAX_MEASURE_BYTES = 64 * 1024
#: A browser width is believed only between the metrics' width + ``MEASURE_SLACK`` and ``MEASURE_MAX_RATIO`` times it.
MEASURE_SLACK = 0.5
MEASURE_MAX_RATIO = 4.0
#: ``GET /display?since=`` sends the whole list instead of a delta past this many changed ids.
MAX_DELTA_IDS = 400
MEASURE_INTENT = "the page measured wider text"
_METRICS_SHA: Dict[str, Any] = {}
#: The last full display list per team, patched forward by deltas: ``{team root: {"version", "token", "doc"}}``.
_DISPLAY_CACHE: Dict[str, Dict[str, Any]] = {}


def metrics_sha() -> str:
    """The sha256 of the font metrics the server measures with (``assets/fonts/font-metrics.json``)."""
    path = _ctext.METRICS_PATH
    try:
        stat_result = path.stat()
        signature = (stat_result.st_mtime_ns, stat_result.st_size)
    except OSError:
        return ""
    if _METRICS_SHA.get("signature") != signature:
        data = store.read_bytes(path) or b""
        _METRICS_SHA.update(signature=signature, sha=hashlib.sha256(data).hexdigest())
    return str(_METRICS_SHA.get("sha") or "")


def corrections(team: TeamPaths) -> Tuple[Dict[str, float], str]:
    """``(table, token)``: the browser's corrections for this team, empty when they were measured against other metrics."""
    doc = store.read_json(_file(team, MEASURE_FILE), default=None)
    if not isinstance(doc, dict) or doc.get("v") != 1 or not doc.get("metrics") or doc.get("metrics") != metrics_sha():
        return {}, ""
    raw = doc.get("em") if isinstance(doc.get("em"), dict) else {}
    table = {str(key): float(value) for key, value in raw.items() if _is_number(value) and float(value) > 0}
    token = hashlib.sha256(json.dumps(sorted(table.items()), separators=(",", ":")).encode("utf-8")).hexdigest()[:16] if table else ""
    return table, token


class _corrected:  # noqa: N801 - used as a context manager, like the canvas_text one it wraps
    """Measure under this team's corrections inside the block (a no-op for a team without any)."""

    def __init__(self, team: TeamPaths) -> None:
        try:
            table, token = corrections(team)
        except (OSError, HerdrTeamError, ValueError):
            table, token = {}, ""
        self._inner = _ctext.corrected(table, token)

    def __enter__(self) -> None:
        self._inner.__enter__()

    def __exit__(self, *exc: Any) -> None:
        self._inner.__exit__(*exc)


def _stills(team: TeamPaths) -> Set[str]:
    try:
        return {entry.name for entry in os.scandir(_dir(team) / STILLS_DIR) if entry.name.endswith(".png")}
    except OSError:
        return set()


def display(team: TeamPaths) -> Dict[str, Any]:
    """The team's whole display list (``canvas_display``), cached per team by version and corrections."""
    table, token = corrections(team)
    version = current_version(team)
    key = os.fspath(team.root)
    cached = _DISPLAY_CACHE.get(key)
    if cached is not None and cached["version"] == version and cached["token"] == token:
        return cached["doc"]
    doc: Optional[Dict[str, Any]] = None
    if cached is not None and cached["token"] == token and cached["version"] < version:
        doc = _patched(team, cached["doc"], table, token)
    if doc is None:
        with _ctext.corrected(table, token):
            doc = _display.display_list(load_scene(team), stills=_stills(team))
    _DISPLAY_CACHE[key] = {"version": int(doc["version"]), "token": token, "doc": doc}
    while len(_DISPLAY_CACHE) > 16:
        del _DISPLAY_CACHE[next(iter(_DISPLAY_CACHE))]
    return doc


def _changed_ids(team: TeamPaths, since: int) -> Optional[Tuple[int, Set[str]]]:
    """``(version, ids)`` changed after ``since``, or None when only a whole list will do (a reset, a lock or an author
    changed, too many changes, or ``since`` is ahead)."""
    got = changes_since(team, since, limit=500)
    if got["reset"] or not got["complete"]:
        return None
    ids: Set[str] = set()
    for event in got["events"]:
        for change in event.get("changes") or []:
            target = change.get("target") if isinstance(change, dict) else None
            if target in ("lock", "author"):
                return None
            if target in ("element", "claim") and isinstance(change.get("id"), str):
                ids.add(change["id"])
        if len(ids) > MAX_DELTA_IDS:
            return None
    return int(got["version"]), ids


def _patched(team: TeamPaths, doc: Dict[str, Any], table: Dict[str, float], token: str) -> Optional[Dict[str, Any]]:
    """``doc`` brought up to the current version by rebuilding only the entries that changed, or None."""
    found = _changed_ids(team, int(doc["version"]))
    if found is None:
        return None
    version, ids = found
    scene = load_scene(team)
    with _ctext.corrected(table, token):
        fresh = {e["id"]: e for e in _display.entries(scene, ids, stills=_stills(team))}
    kept = [e for e in doc["entries"] if e["id"] not in ids] + list(fresh.values())
    kept.sort(key=_display.order_key)
    return dict(doc, version=version, bbox=[_display.r2(v) for v in _display.bbox_of(kept)], entries=kept)


def display_delta(team: TeamPaths, since: int) -> Dict[str, Any]:
    """What changed in the display list after version ``since`` (phase 1, 3.2): upserts and removes, or the whole list
    with ``full: true`` when a delta cannot say it."""
    if not _is_number(since) or int(since) < 0:
        raise _error("usage", "since must be a version number >= 0", since=since)
    since = int(since)
    doc = display(team)
    found = _changed_ids(team, since) if 0 < since <= int(doc["version"]) else None
    if found is None:
        return dict(doc, since=since, full=True)
    _version, ids = found
    upserts = [e for e in doc["entries"] if e["id"] in ids]
    live = {e["id"] for e in upserts}
    return {"dl": _display.DL_VERSION, "version": doc["version"], "since": since, "full": False, "bbox": doc["bbox"],
            "upserts": upserts, "removes": sorted((i for i in ids if i not in live), key=lambda i: (_id_number(i), i))}


def _measure_lines(body: Any) -> Tuple[int, str, List[Dict[str, Any]]]:
    if not isinstance(body, dict):
        raise _invalid("lines", "a measure report is {version, metrics, lines: [...]}")
    lines = body.get("lines")
    if not isinstance(lines, list):
        raise _invalid("lines", "lines must be a list")
    if len(lines) > MAX_MEASURE_LINES:
        raise _too_big("lines", "MAX_MEASURE_LINES", MAX_MEASURE_LINES, "at most {} lines per report".format(MAX_MEASURE_LINES))
    version = body.get("version")
    if not _is_number(version) or int(version) < 0:
        raise _invalid("version", "version is the display list version the page measured")
    return int(version), str(body.get("metrics") or ""), lines


def _widest_known(entry: Dict[str, Any]) -> Dict[Tuple[str, int, float, str], float]:
    """Every text line an entry draws: ``(font, weight, size, text) -> the metrics' width``."""
    out: Dict[Tuple[str, int, float, str], float] = {}

    def walk(items: Any) -> None:
        for item in items or []:
            if not isinstance(item, dict):
                continue
            if item.get("k") == "text" and item.get("zoom") is None:
                for line in item.get("lines") or []:
                    out[(str(item.get("font")), int(item.get("weight") or 400), float(item.get("size") or 0), str(line.get("t")))] = \
                        float(line.get("w") or 0)
            walk(item.get("items"))
    walk(entry.get("items"))
    return out


def _outgrown(el: Dict[str, Any]) -> bool:
    """Whether an element's label no longer fits its box as stored (or an arrow's pill its lines) under the corrections in force."""
    if el.get("type") == "arrow":
        return bool(el.get("text")) and el.get("fit") != _label_fit(el)
    found = _kinds.drawn(el)
    if found is None:
        return False
    fit = el.get("fit") if isinstance(el.get("fit"), dict) else {}
    # A box that still holds its label may need new line breaks: the stored lines are what every renderer draws.
    return found.w > float(el.get("w") or 1) + 1e-6 or found.h > float(el.get("h") or 1) + 1e-6 or \
        (isinstance(fit.get("lines"), list) and not fit.get("truncated") and list(found.lines) != list(fit["lines"]))


def measure_report(layout: Any, team: TeamPaths, body: Any, doc: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """The page's browser check (``POST /measure``, phase 1 3.3): keep each line it measured wider than the metrics say,
    and refit what no longer fits. Grow only: a correction only ever widens a line, so a refit only grows a box."""
    doc = doc if isinstance(doc, dict) else store.RosterStore(team).load()
    _features.require_on(layout.session, team, doc)
    version, metrics, lines = _measure_lines(body)
    server_sha = metrics_sha()
    ignored: List[Dict[str, Any]] = []
    accepted: Dict[str, float] = {}
    #: Per accepted key, the widest em a correction may hold (``MEASURE_MAX_RATIO`` times the metrics' own).
    ceilings: Dict[str, float] = {}
    touched: List[str] = []
    lock = _canvas_lock(team)
    lock.acquire()
    try:
        state = _load_state(team)
        scene = state.to_scene(time.time())
        table, token = corrections(team)
        wanted = {str(line.get("id")) for line in lines if isinstance(line, dict)}
        with _ctext.corrected(table, token):
            known = {e["id"]: _widest_known(e) for e in _display.entries(scene, wanted)}
        for index, line in enumerate(lines):
            if not isinstance(line, dict) or not all(isinstance(line.get(k), str) for k in ("id", "t")) or \
                    not all(_is_number(line.get(k)) for k in ("weight", "size", "w")) or line.get("font") not in ("sans", "mono"):
                ignored.append({"i": index, "why": "not_a_line"})
                continue
            el = state.elements.get(line["id"])
            if metrics != server_sha or el is None or int(el.get("updated_seq") or 0) > version:
                ignored.append({"i": index, "why": "stale"})
                continue
            server_w = (known.get(line["id"]) or {}).get((line["font"], int(line["weight"]), float(line["size"]), line["t"]))
            if server_w is None:
                ignored.append({"i": index, "why": "not_a_line"})
                continue
            width = float(line["w"])
            if width <= server_w + MEASURE_SLACK:
                ignored.append({"i": index, "why": "not_wider"})
                continue
            # Plausible against the metrics alone, never against a width that already carries an earlier correction:
            # else each report could widen the line by the ratio again, without limit (phase 1 QA, finding 5).
            with _ctext.corrected(None):
                raw_w = _ctext.measure(line["t"], line["font"], float(line["size"]), int(line["weight"])).width
            if width > MEASURE_MAX_RATIO * max(raw_w, 1e-6):
                ignored.append({"i": index, "why": "implausible"})
                continue
            key = _ctext.correction_key(line["font"], int(line["weight"]), line["t"])
            accepted[key] = max(accepted.get(key, 0.0), width / float(line["size"]))
            ceilings[key] = MEASURE_MAX_RATIO * raw_w / float(line["size"])
            if line["id"] not in touched:
                touched.append(line["id"])
        if accepted:
            merged = dict(table)
            for key, em in accepted.items():
                # Capped, so a table written before the cap (or by hand) cannot keep a runaway width either.
                best = min(max(em, float(merged.pop(key, 0.0))), ceilings[key])
                # Rounded up (never narrower than the page measured), and re-inserted: the newest last, so the oldest go first.
                merged[key] = math.ceil(best * 1e6) / 1e6
            while len(merged) > MAX_CORRECTIONS:
                del merged[next(iter(merged))]
            store.write_json(_file(team, MEASURE_FILE), {"v": 1, "metrics": server_sha, "em": merged}, fsync=False)
    finally:
        lock.release()
    refit: List[str] = []
    result: Optional[Dict[str, Any]] = None
    if accepted:
        with _corrected(team):
            live = _load_state(team).elements
            refit = [eid for eid in touched if eid in live and _outgrown(live[eid])]
        if refit:
            result = apply_ops(layout, team, [{"op": "refit", "ids": refit, "intent": MEASURE_INTENT}], page_author(True), doc=doc)
    return {"accepted": len(lines) - len(ignored), "ignored": ignored, "refit": refit,
            "batch": result.get("batch") if result else None, "version": result.get("version") if result else current_version(team)}


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


# The tables the registry answers, derived once every name they use exists (and again when a test adds a kind).
_derive()
_kinds.on_change(_derive)
