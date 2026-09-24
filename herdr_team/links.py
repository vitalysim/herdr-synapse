"""Links between teams: the registry, the endpoints, and their state (0.15.0).

A link is a session-level fact in ``sessions/<slug>/links.json``: two teams
of the same Herdr session whose **managers** may post to each other. The
teams are sorted, so a pair has one id whatever the order it was typed in.

A message across a link is an ordinary board record that lives on both
boards (``cmd_board``): the delivered copy on the receiving board addressed
to that team's manager, and a mirror on the sending board addressed to
``team:<other>``. ``team:`` is not a member, so nobody is nudged for the
mirror; it is there so the sending team can see what its manager said.

Linking needs both managers, and so does sending: a manager cleared later
*pauses* the link rather than dropping a message on the floor, and the state
says which team to fix. Cross-session links (two Herdr servers) are out of
scope.
"""
from __future__ import annotations

import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from herdr_team import roster as _roster
from herdr_team import store
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError
from herdr_team.paths import SessionPaths, validate_team_name
from herdr_team.roster import now_iso

TEAM_PREFIX = "team:"
STATUS_ACTIVE = "active"
STATUS_BROKEN = "broken"


def link_id(a: str, b: str) -> str:
    return "--".join(sorted((a, b)))


def is_team_recipient(token: Any) -> bool:
    return isinstance(token, str) and token.startswith(TEAM_PREFIX) and len(token) > len(TEAM_PREFIX)


def team_of_recipient(token: str) -> str:
    """``team:<name>`` -> ``<name>``, validated as a team name."""
    return validate_team_name(token[len(TEAM_PREFIX):])


def new_message_id() -> str:
    return uuid.uuid4().hex


@dataclass
class Link:
    id: str
    teams: List[str]
    status: str = STATUS_ACTIVE
    created_at: Optional[str] = None
    created_by: Optional[str] = None
    broken_at: Optional[str] = None
    broken_by: Optional[str] = None
    note: Optional[str] = None
    history: List[Dict[str, Any]] = field(default_factory=list)

    @property
    def active(self) -> bool:
        return self.status == STATUS_ACTIVE

    def other(self, team: str) -> str:
        return self.teams[1] if self.teams[0] == team else self.teams[0]

    def to_json(self) -> Dict[str, Any]:
        return {
            "id": self.id, "teams": list(self.teams), "status": self.status,
            "created_at": self.created_at, "created_by": self.created_by,
            "broken_at": self.broken_at, "broken_by": self.broken_by, "note": self.note,
            "history": [dict(h) for h in self.history],
        }

    @classmethod
    def from_json(cls, obj: Dict[str, Any]) -> Optional["Link"]:
        teams = obj.get("teams")
        if not isinstance(obj, dict) or not isinstance(teams, list) or len(teams) != 2 or not all(isinstance(t, str) and t for t in teams):
            return None
        status = obj.get("status") if obj.get("status") in (STATUS_ACTIVE, STATUS_BROKEN) else STATUS_BROKEN
        return cls(
            id=str(obj.get("id") or link_id(*teams)), teams=sorted(teams), status=str(status),
            created_at=obj.get("created_at"), created_by=obj.get("created_by"),
            broken_at=obj.get("broken_at"), broken_by=obj.get("broken_by"), note=obj.get("note"),
            history=[dict(h) for h in (obj.get("history") or []) if isinstance(h, dict)],
        )


# --------------------------------------------------------------------------
# the registry


def load(session: SessionPaths) -> List[Link]:
    doc = store.read_json(session.links_json, default=None)
    rows = doc.get("links") if isinstance(doc, dict) else None
    out: List[Link] = []
    for row in rows or []:
        link = Link.from_json(row) if isinstance(row, dict) else None
        if link is not None:
            out.append(link)
    return out


def _save(session: SessionPaths, links: List[Link]) -> None:
    store.write_json(session.links_json, {"v": 1, "links": [l.to_json() for l in sorted(links, key=lambda l: l.id)]})


def find(session: SessionPaths, a: str, b: str) -> Optional[Link]:
    wanted = link_id(a, b)
    for link in load(session):
        if link.id == wanted:
            return link
    return None


def active_for(session: SessionPaths, team: str) -> List[Link]:
    return [l for l in load(session) if l.active and team in l.teams]


def _check_team_exists(session: SessionPaths, team: str) -> None:
    if not session.team(validate_team_name(team)).team_json.is_file():
        raise HerdrTeamError("team_not_found", "team {!r} does not exist in this session".format(team), EXIT_REFUSED, {"team": team, "teams": session.list_teams()})


def link(session: SessionPaths, a: str, b: str, by: str, note: Optional[str] = None) -> Tuple[Link, bool]:
    """Create the link, or re-activate a broken one. ``(link, created)``."""
    a, b = validate_team_name(a), validate_team_name(b)
    if a == b:
        raise HerdrTeamError("link_self", "a team cannot be linked to itself", EXIT_REFUSED, {"team": a})
    _check_team_exists(session, a)
    _check_team_exists(session, b)
    with store.FileLock(session.links_lock):
        links = load(session)
        for existing in links:
            if existing.id == link_id(a, b):
                if existing.active:
                    return existing, False
                existing.status = STATUS_ACTIVE
                existing.broken_at = None
                existing.broken_by = None
                existing.history.append({"at": now_iso(), "by": by, "event": "relinked"})
                if note:
                    existing.note = note
                _save(session, links)
                return existing, True
        created = Link(id=link_id(a, b), teams=sorted((a, b)), created_at=now_iso(), created_by=by, note=note,
                       history=[{"at": now_iso(), "by": by, "event": "linked"}])
        links.append(created)
        _save(session, links)
        return created, True


def unlink(session: SessionPaths, a: str, b: str, by: str) -> Link:
    """Break the link; it stays in the registry as ``broken`` so ``links`` shows the history."""
    a, b = validate_team_name(a), validate_team_name(b)
    with store.FileLock(session.links_lock):
        links = load(session)
        for existing in links:
            if existing.id == link_id(a, b):
                if not existing.active:
                    raise HerdrTeamError("link_broken", "{} and {} are not linked (the link was broken {})".format(a, b, existing.broken_at or "earlier"), EXIT_REFUSED, {"link": existing.id})
                existing.status = STATUS_BROKEN
                existing.broken_at = now_iso()
                existing.broken_by = by
                existing.history.append({"at": now_iso(), "by": by, "event": "broken"})
                _save(session, links)
                return existing
    raise HerdrTeamError("link_missing", "{} and {} are not linked; link them with: herdr-synapse link {} {}".format(a, b, a, b), EXIT_REFUSED, {"teams": [a, b]})


def break_all_for(session: SessionPaths, team: str, by: str, why: str = "dissolved") -> List[Link]:
    """Every active link of ``team`` becomes broken (a dissolved team has no manager to speak for it)."""
    broken: List[Link] = []
    with store.FileLock(session.links_lock):
        links = load(session)
        for existing in links:
            if existing.active and team in existing.teams:
                existing.status = STATUS_BROKEN
                existing.broken_at = now_iso()
                existing.broken_by = by
                existing.history.append({"at": now_iso(), "by": by, "event": why})
                broken.append(existing)
        if broken:
            _save(session, links)
    return broken


# --------------------------------------------------------------------------
# endpoints


def manager_of(session: SessionPaths, team: str) -> Optional[str]:
    """The team's manager, or None (also None when the team is gone)."""
    try:
        doc = _roster.load_team(session.team(team))
    except HerdrTeamError:
        return None
    current = doc.manager()
    return current.name if current is not None else None


def state(session: SessionPaths, link: Link) -> str:
    """``active`` | ``paused: <team> has no manager`` | ``broken``."""
    if not link.active:
        return STATUS_BROKEN
    for team in link.teams:
        if manager_of(session, team) is None:
            return "paused: {} has no manager".format(team)
    return STATUS_ACTIVE


def require_endpoint(session: SessionPaths, team: str, other: str) -> Tuple[Link, str, str]:
    """The active link plus both managers, or why not: ``link_missing``, ``link_broken``, ``link_no_manager``."""
    found = find(session, team, other)
    if found is None:
        raise HerdrTeamError("link_missing", "{} is not linked to {}; the operator links them with: herdr-synapse link {} {}".format(team, other, team, other), EXIT_REFUSED, {"teams": [team, other]})
    if not found.active:
        raise HerdrTeamError("link_broken", "the link between {} and {} was broken {}; relink with: herdr-synapse link {} {}".format(team, other, found.broken_at or "earlier", team, other), EXIT_REFUSED, {"link": found.id})
    mine = manager_of(session, team)
    theirs = manager_of(session, other)
    for name, holder in ((team, mine), (other, theirs)):
        if holder is None:
            raise HerdrTeamError("link_no_manager", "{} has no manager, so the link is paused; set one with: herdr-synapse --team {} manager <name>".format(name, name), EXIT_REFUSED, {"team": name, "link": found.id})
    return found, str(mine), str(theirs)


def summary(session: SessionPaths, team: str, include_broken: bool = False) -> List[Dict[str, Any]]:
    """``[{team, manager, state, id, note}]`` for ``me``, ``who`` and the picker."""
    out: List[Dict[str, Any]] = []
    for link in load(session):
        if team not in link.teams or (not link.active and not include_broken):
            continue
        other = link.other(team)
        out.append({"team": other, "manager": manager_of(session, other), "state": state(session, link), "id": link.id, "note": link.note})
    return out


# --------------------------------------------------------------------------
# one message, two boards: the outbox
#
# The delivered copy and the mirror are two appends to two boards under two
# locks, so a failure between them (a lock timeout, a dissolve, a full disk)
# once left a message on one board only, and a manager who retried it
# duplicated the half that had landed. Each send is journalled first; the
# journal is removed once both copies are on their boards, and ``flush_outbox``
# (the notifier, or the next link post) finishes any that were cut short.

#: A journal younger than this belongs to a send that may still be running.
OUTBOX_GRACE_S = 30.0


def outbox_dir(session: SessionPaths) -> Path:
    return session.root / "link-outbox"


def _outbox_files(session: SessionPaths, message_id: str) -> Tuple[Path, Path]:
    stem = outbox_dir(session) / message_id
    return stem.with_suffix(".json"), stem.with_suffix(".lock")


def _landed(team_paths: Any, message_id: str, since: int) -> bool:
    for record in store.BoardStore(team_paths).read(since_seq=since, include_retracted=True):
        link = record.get("link")
        if isinstance(link, dict) and link.get("id") == message_id:
            return True
    return False


def send(session: SessionPaths, message_id: str, copies: List[Tuple[str, Dict[str, Any]]]) -> Tuple[Dict[str, int], Optional[str], Optional[HerdrTeamError]]:
    """Append each ``(team, record)`` in order, journalled; ``(seqs, failed_team, error)``.

    A failure on the first copy leaves nothing behind (the journal is
    dropped, the send can simply be retried). A failure after that keeps the
    journal, so the remaining copies are completed later and never twice.
    """
    journal, lock_path = _outbox_files(session, message_id)
    entry = {
        "v": 1, "id": message_id, "created": time.time(),
        "copies": [{"team": team, "since": store.BoardStore(session.team(team)).max_seq(), "record": record} for team, record in copies],
    }
    seqs: Dict[str, int] = {}
    with store.FileLock(lock_path, code="link_outbox_locked"):
        store.write_json(journal, entry)
        for team, record in copies:
            try:
                seqs[team] = int(store.BoardStore(session.team(team)).append(record))
            except HerdrTeamError as err:
                if not seqs:
                    _remove(journal, lock_path)
                return seqs, team, err
        _remove(journal, lock_path)
    return seqs, None, None


def _remove(*files: Path) -> None:
    for path in files:
        try:
            os.unlink(path)
        except OSError:
            pass


def flush_outbox(session: SessionPaths, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Finish journalled sends that stopped half way; one report per journal acted on."""
    directory = outbox_dir(session)
    try:
        names = sorted(n for n in os.listdir(directory) if n.endswith(".json"))
    except OSError:
        return []
    now = time.time() if now is None else now
    reports: List[Dict[str, Any]] = []
    for name in names:
        journal, lock_path = _outbox_files(session, name[: -len(".json")])
        lock = store.FileLock(lock_path, timeout=0.0, code="link_outbox_locked")
        try:
            lock.acquire()
        except HerdrTeamError:
            continue  # the sender still holds it
        try:
            entry = store.read_json(journal, default=None)
            if not isinstance(entry, dict) or not isinstance(entry.get("copies"), list):
                _remove(journal, lock_path)
                continue
            if now - float(entry.get("created") or 0) < OUTBOX_GRACE_S:
                continue
            report: Dict[str, Any] = {"id": entry.get("id"), "appended": {}, "dropped": [], "pending": []}
            for copy in entry["copies"]:
                team = copy.get("team")
                record = copy.get("record")
                if not isinstance(team, str) or not isinstance(record, dict):
                    continue
                team_paths = session.team(team)
                if not team_paths.root.is_dir():
                    report["dropped"].append(team)  # dissolved since: nobody left to read it
                    continue
                try:
                    if not _landed(team_paths, str(entry.get("id")), int(copy.get("since") or 0)):
                        report["appended"][team] = int(store.BoardStore(team_paths).append(record))
                except HerdrTeamError as err:
                    report["pending"].append({"team": team, "error": err.code})
            if not report["pending"]:
                _remove(journal)
            reports.append(report)
        finally:
            lock.release()
            if not journal.exists():
                _remove(lock_path)
    return reports
