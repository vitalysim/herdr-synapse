"""Presence on the canvas (canvas v2 phase 5, 9): who is looking where, as small files, never in the log.

Agents run in their own processes (the CLI, the MCP server), not in the page server, so each presence source writes one
file under ``whiteboard/presence/``: a member ``<name>.json`` (written by ``apply_ops`` and ``look`` for a verified
member, and by ``canvas focus``), and the operator's pages ``human-<page>.json`` (only through the page server's
``POST /presence``, for a writable session). Each write is atomic (a temp file, then a rename), at most 4 KiB. Streams
poll the directory's ``signature`` on their existing half-second poll and send an SSE ``presence`` event.

Presence is a view, never authority (D12). The one thing it can do is refuse an agent op aimed at the element the
operator is editing right now (``element_busy``, retryable); her selection only adds a warning. Everything read back is
validated again (a hand-edited file is not trusted), and a stale file is ignored and, after ten minutes, removed.
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from herdr_team import canvas as C
from herdr_team import store
from herdr_team.errors import HerdrTeamError
from herdr_team.paths import FILE_STEM_RE, check_not_symlink, ensure_dir

PRESENCE_DIR = "presence"
MAX_BYTES = 4096
STATUSES = ("reading", "drawing", "waiting", "blocked", "idle")
MAX_IDS = 50
#: How long each source's presence stays fresh (seconds).
MEMBER_TTL_S = 90
LOOK_TTL_S = 60
FOCUS_TTL_S = 600
FOCUS_TTL_RANGE = (60, 3600)
HUMAN_TTL_S = 30
#: A file this old (by its modification time) is removed by ``read_all``, best-effort.
SWEEP_S = 600.0
#: A member in one of these roster states is not present, whatever its file says.
GONE_STATUSES = ("left", "missing", "failed")
#: The operator's pages kept at once: a new page id beyond this removes the oldest page's file.
MAX_HUMAN_PAGES = 8
#: A page's id: 16 hex characters from ``crypto.getRandomValues``.
PAGE_RE = re.compile(r"^[0-9a-f]{16}\Z")
_HUMAN_FILE_RE = re.compile(r"^human-([0-9a-f]{16})\.json\Z")
_ELEMENT_RE = re.compile(r"^[A-Z]-[1-9][0-9]{0,6}\Z")

_log = logging.getLogger("herdr_team.canvas_presence")
_log.addHandler(logging.NullHandler())


def presence_dir(team: Any) -> Path:
    return C._dir(team) / PRESENCE_DIR


def _invalid(field: str, message: str) -> HerdrTeamError:
    return C._error("usage", message, field=field)


# --------------------------------------------------------------------------
# validation (on write and again on read)


def _box(value: Any, field: str) -> Optional[List[float]]:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 4 or not all(C._is_number(v) and abs(float(v)) <= C.MAX_COORD for v in value):
        raise _invalid(field, "{} is [x0, y0, x1, y1]: four finite numbers within the canvas".format(field))
    x0, y0, x1, y1 = (float(v) for v in value)
    if x0 > x1 or y0 > y1:
        raise _invalid(field, "{} is [x0, y0, x1, y1] with x0 <= x1 and y0 <= y1".format(field))
    return [C._r2(x0), C._r2(y0), C._r2(x1), C._r2(y1)]


def _point(value: Any, field: str) -> Optional[List[float]]:
    if value is None:
        return None
    if not isinstance(value, list) or len(value) != 2 or not all(C._is_number(v) and abs(float(v)) <= C.MAX_COORD for v in value):
        raise _invalid(field, "{} is [x, y] within the canvas".format(field))
    return [C._r2(float(value[0])), C._r2(float(value[1]))]


def _ids(value: Any, field: str) -> List[str]:
    if value is None:
        return []
    if not isinstance(value, (list, tuple)) or len(value) > MAX_IDS or not all(isinstance(v, str) and _ELEMENT_RE.match(v) for v in value):
        raise _invalid(field, "{} is a list of up to {} element ids".format(field, MAX_IDS))
    return list(dict.fromkeys(value))


def _element(value: Any, field: str) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str) or not _ELEMENT_RE.match(value):
        raise _invalid(field, "{} is an element id or null".format(field))
    return value


def _intent(value: Any) -> str:
    if value is None:
        return ""
    return C._text(value, "intent", C.MAX_INTENT_CHARS, "MAX_INTENT_CHARS", one_line=True)


def _name_ok(name: Any) -> bool:
    return isinstance(name, str) and bool(FILE_STEM_RE.match(name)) and name != C.HUMAN and not name.startswith(("human-", "."))


def _encode(doc: Mapping[str, Any]) -> bytes:
    data = json.dumps(doc, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_BYTES:
        raise C._error("canvas_limit", "a presence record is at most {} bytes".format(MAX_BYTES), limit="MAX_BYTES", max=MAX_BYTES)
    return data


def _write(team: Any, name: str, doc: Mapping[str, Any]) -> None:
    folder = presence_dir(team)
    ensure_dir(folder)
    path = folder / (name + ".json")
    check_not_symlink(path)
    store.atomic_write(path, _encode(doc), fsync=False)


# --------------------------------------------------------------------------
# writing


def write_member(team: Any, author: Any, *, status: str, region: Any = None, ids: Sequence[str] = (), intent: Any = "",
                 ttl_s: float = MEMBER_TTL_S, via: str = "auto", now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """A verified member's presence (``<name>.json``); None, and nothing written, for anyone else."""
    if not (getattr(author, "is_member", False) and getattr(author, "verified", False)) or not _name_ok(getattr(author, "name", None)):
        return None
    if status not in STATUSES:
        raise _invalid("status", "status is one of: {}".format(", ".join(STATUSES)))
    moment = time.time() if now is None else float(now)
    doc = {"v": 1, "name": author.name, "kind": "member", "agent": getattr(author, "agent", None), "at": C._iso(moment),
           "ttl_s": int(max(1, min(FOCUS_TTL_RANGE[1], int(ttl_s)))), "status": status, "region": _box(region, "region"),
           "ids": _ids(list(ids)[:MAX_IDS], "ids"), "intent": _intent(intent), "via": via if via in ("auto", "focus", "look") else "auto"}
    _write(team, author.name, doc)
    return doc


def clear_member(team: Any, name: str) -> bool:
    if not _name_ok(name):
        return False
    try:
        os.unlink(presence_dir(team) / (name + ".json"))
        return True
    except OSError:
        return False


def write_human(team: Any, page: Any, body: Any, now: float) -> Dict[str, Any]:
    """One of the operator's pages (``human-<page>.json``), from ``POST /presence``: validated, stored, returned."""
    if not isinstance(body, dict):
        raise _invalid("body", "presence is {page, viewport, selection, editing, cursor, away}")
    unknown = [key for key in body if key not in ("page", "viewport", "selection", "editing", "cursor", "away")]
    if unknown:
        raise _invalid(str(unknown[0]), "presence does not take {}".format(unknown[0]))
    page = page if page is not None else body.get("page")
    if not isinstance(page, str) or not PAGE_RE.match(page):
        raise _invalid("page", "page is 16 hex characters")
    away = body.get("away")
    if away is not None and not isinstance(away, bool):
        raise _invalid("away", "away is true or false")
    doc = {"v": 1, "name": C.HUMAN, "kind": "human", "page": page, "at": C._iso(now), "ttl_s": HUMAN_TTL_S,
           "viewport": _box(body.get("viewport"), "viewport"), "selection": _ids(body.get("selection"), "selection"),
           "editing": _element(body.get("editing"), "editing"), "cursor": _point(body.get("cursor"), "cursor"), "away": bool(away)}
    _cap_pages(team, page)
    _write(team, "human-" + page, doc)
    return doc


def _cap_pages(team: Any, page: str) -> None:
    """A new page id beyond ``MAX_HUMAN_PAGES`` removes the oldest pages' files first (QA phase 5 L11): one session cannot
    make every stream stat and parse an unbounded directory."""
    folder = presence_dir(team)
    if (folder / ("human-" + page + ".json")).exists():
        return
    pages = []
    for entry in _files(team):
        if _HUMAN_FILE_RE.match(entry.name):
            try:
                pages.append((entry.stat(follow_symlinks=False).st_mtime, entry.name, entry.path))
            except OSError:
                continue
    pages.sort()
    while len(pages) >= MAX_HUMAN_PAGES:
        _mtime, _name, path = pages.pop(0)
        try:
            os.unlink(path)
        except OSError:
            pass


# --------------------------------------------------------------------------
# reading


def _valid(doc: Any, file_name: str) -> Optional[Dict[str, Any]]:
    """A stored record as trusted as a new write would be; None for anything malformed or misnamed."""
    if not isinstance(doc, dict) or doc.get("v") != 1:
        return None
    try:
        at = C._parse_iso(doc.get("at"))
        ttl = doc.get("ttl_s")
        if at is None or not C._is_number(ttl) or not 1 <= float(ttl) <= FOCUS_TTL_RANGE[1]:
            return None
        match = _HUMAN_FILE_RE.match(file_name)
        if match:
            if doc.get("kind") != "human" or doc.get("page") != match.group(1) or doc.get("name") != C.HUMAN:
                return None
            return {"v": 1, "name": C.HUMAN, "kind": "human", "page": doc["page"], "at": doc["at"], "ttl_s": int(ttl),
                    "viewport": _box(doc.get("viewport"), "viewport"), "selection": _ids(doc.get("selection"), "selection"),
                    "editing": _element(doc.get("editing"), "editing"), "cursor": _point(doc.get("cursor"), "cursor"),
                    "away": bool(doc.get("away") is True)}
        name = file_name[:-5] if file_name.endswith(".json") else ""
        if doc.get("kind") != "member" or doc.get("name") != name or not _name_ok(name) or doc.get("status") not in STATUSES:
            return None
        agent = doc.get("agent") if isinstance(doc.get("agent"), str) and len(doc["agent"]) <= 40 else None
        return {"v": 1, "name": name, "kind": "member", "agent": agent, "at": doc["at"], "ttl_s": int(ttl), "status": doc["status"],
                "region": _box(doc.get("region"), "region"), "ids": _ids(doc.get("ids"), "ids"), "intent": _intent(doc.get("intent")),
                "via": doc.get("via") if doc.get("via") in ("auto", "focus", "look") else "auto"}
    except HerdrTeamError:
        return None


def fresh(doc: Mapping[str, Any], now: float) -> bool:
    at = C._parse_iso(doc.get("at"))
    return at is not None and at + float(doc.get("ttl_s") or 0) > now and not doc.get("away")


def _files(team: Any) -> List[os.DirEntry]:  # type: ignore[type-arg]
    try:
        return [entry for entry in os.scandir(presence_dir(team)) if entry.name.endswith(".json") and not entry.name.startswith(".")]
    except OSError:
        return []


def _present_members(team: Any) -> Optional[Set[str]]:
    """The roster's members that can be present (not left, missing or failed), or None when the roster cannot be read."""
    path = getattr(team, "team_json", None)
    doc = store.read_json(path, default=None) if path is not None else None
    if not isinstance(doc, dict) or not isinstance(doc.get("members"), list):
        return None
    return {str(m["name"]) for m in doc["members"] if isinstance(m, dict) and isinstance(m.get("name"), str) and m.get("status") not in GONE_STATUSES}


def read_all(team: Any, now: float) -> Dict[str, Any]:
    """``{"at", "entries"}``: every fresh record, sorted by name then page (files older than ten minutes are removed). A
    member no longer on the roster (removed, left, gone) is not present, whatever its file says (QA phase 5 M3)."""
    entries: List[Dict[str, Any]] = []
    roster: Optional[Set[str]] = None
    roster_read = False
    for entry in _files(team):
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        if not entry.is_file(follow_symlinks=False) or info.st_size > MAX_BYTES:
            continue
        if now - info.st_mtime > SWEEP_S:
            try:
                os.unlink(entry.path)
            except OSError:
                pass
            continue
        doc = _valid(store.read_json(entry.path, default=None), entry.name)
        if doc is None or not fresh(doc, now):
            continue
        if doc["kind"] == "member":
            if not roster_read:
                roster, roster_read = _present_members(team), True
            if roster is not None and doc["name"] not in roster:
                continue
        entries.append(doc)
    entries.sort(key=lambda d: (str(d.get("name")), str(d.get("page") or "")))
    return {"at": C._iso(now), "entries": entries}


def operator(team: Any, now: float) -> Optional[Dict[str, Any]]:
    """The freshest of the operator's pages that is not away, or None. ``editing_all`` adds what she is editing on any of
    her fresh pages, so a second tab's heartbeat never lifts the busy check of the first (QA phase 5 L6)."""
    found = [d for d in read_all(team, now)["entries"] if d.get("kind") == "human"]
    found.sort(key=lambda d: C._parse_iso(d.get("at")) or 0.0)
    if not found:
        return None
    best = dict(found[-1])
    editing = sorted({str(d["editing"]) for d in found if isinstance(d.get("editing"), str)})
    if editing:
        best["editing_all"] = editing
    return best


def members(team: Any, now: float) -> List[Dict[str, Any]]:
    return [d for d in read_all(team, now)["entries"] if d.get("kind") == "member"]


def signature(team: Any) -> Tuple[Any, ...]:
    """The presence directory's stat signature, for streams: changes with every write, rename or removal."""
    out = []
    for entry in _files(team):
        try:
            info = entry.stat(follow_symlinks=False)
        except OSError:
            continue
        out.append((entry.name, info.st_mtime_ns, info.st_size))
    return tuple(sorted(out))


def pointing_at(human: Mapping[str, Any], elements: Sequence[Mapping[str, Any]]) -> Optional[str]:
    """The topmost element whose box holds the operator's cursor (computed at read time)."""
    cursor = human.get("cursor")
    if not isinstance(cursor, list) or len(cursor) != 2:
        return None
    from herdr_team import canvas_kinds

    x, y = float(cursor[0]), float(cursor[1])
    layers = {name: index for index, name in enumerate(canvas_kinds.LAYERS)}
    best: Optional[Tuple[int, int, int, str]] = None
    for el in elements:
        if el.get("type") == "comment":
            continue
        x0, y0, x1, y1 = C.bounds(dict(el))
        if x0 <= x <= x1 and y0 <= y <= y1:
            kind = canvas_kinds.kind_of(el)
            # In drawing order: a frame's zone lies under the marks in it, whatever their z.
            key = (layers.get(kind.layer if kind is not None else "marks", 1), int(el.get("z") or 0), C._id_number(el.get("id")), str(el.get("id")))
            if best is None or key > best:
                best = key
    return best[3] if best else None


def operator_view(human: Optional[Mapping[str, Any]], elements: Sequence[Mapping[str, Any]], now: float) -> Optional[Dict[str, Any]]:
    """What an agent reads of the operator (9.2): viewport, selection, editing, pointing at, age."""
    if not human:
        return None
    at = C._parse_iso(human.get("at")) or now
    return {"viewport": human.get("viewport"), "selection": list(human.get("selection") or []), "editing": human.get("editing"),
            "pointing_at": pointing_at(human, elements), "age_s": max(0, int(math.floor(now - at))), "at": human.get("at")}


def after_apply(team: Any, author: Any, result: Mapping[str, Any], boxes: Sequence[Sequence[float]], intent: str, now: float) -> None:
    """``drawing`` presence for a verified member after its batch applied (best-effort: never fails the batch)."""
    if not boxes:
        return
    try:
        region = [min(b[0] for b in boxes), min(b[1] for b in boxes), max(b[2] for b in boxes), max(b[3] for b in boxes)]
        ids = []
        for entry in list(result.get("applied") or []) + list(result.get("proposed") or []):
            for eid in entry.get("ids") or entry.get("targets") or []:
                if isinstance(eid, str) and _ELEMENT_RE.match(eid) and eid.startswith(("E-", "C-")) and eid not in ids:
                    ids.append(eid)
        write_member(team, author, status="drawing", region=region, ids=ids[:20], intent=intent[: C.MAX_INTENT_CHARS], ttl_s=MEMBER_TTL_S,
                     via="auto", now=now)
    except (HerdrTeamError, OSError, ValueError, TypeError) as err:
        _log.debug("presence not written for %s: %s", getattr(author, "name", "?"), err)
