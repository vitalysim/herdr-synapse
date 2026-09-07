"""Operator delegation: which members may act with the operator's authority.

The charter, the team rules and each member's instructions are human-only,
because they are the one channel that reaches an agent carrying authority.
That gate used to be satisfied by *not being in a pane*, which any member
could arrange by unsetting one variable; ``identity.hosting_agent_pane``
closes that by asking the process tree instead of the environment.

Closing it leaves a real need unmet: an operator who wants an agent to build
and run a team end to end. This module is the sanctioned way to say so. The
operator names a member, that member's writes are then accepted, and every
one of them is audited and announced on the board. Nothing here is implicit:
no member holds authority until the operator grants it, and a grant expires.

What this is not: a security boundary against a determined agent. A grant is
a file in the session state dir, and an agent that can run a shell as your
user can read or write any file you can. It raises the bar from "unset one
variable" to "forge the operator's own records", makes the intended path
explicit, and leaves a trail in ``audit.jsonl`` and on the board. Treat it
as a way to be deliberate, not as a sandbox.
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from herdr_team import store
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError
from herdr_team.paths import SessionPaths

SCHEMA = 1
#: A grant lasts this long unless the operator says otherwise. Bounded by
#: default so a delegation made for one run does not outlive the session that
#: needed it; ``--ttl 0`` opts out for an automation that must not expire.
DEFAULT_TTL_S = 12 * 60 * 60


def _iso_at(epoch: float) -> str:
    """UTC ISO-8601 for an epoch, in the same shape ``store.now_iso`` writes."""
    import datetime

    return datetime.datetime.fromtimestamp(float(epoch), datetime.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "{:03d}Z".format(
        int((float(epoch) % 1) * 1000))


def _key(team: str, member: str) -> str:
    return "{}/{}".format(team, member)


def read(session: SessionPaths) -> Dict[str, Dict[str, Any]]:
    """Every grant on record, expired ones included."""
    doc = store.read_json(session.operators_json, default=None)
    if not isinstance(doc, dict):
        return {}
    grants = doc.get("grants")
    if not isinstance(grants, dict):
        return {}
    return {k: v for k, v in grants.items() if isinstance(k, str) and isinstance(v, dict)}


def _write(session: SessionPaths, grants: Dict[str, Dict[str, Any]]) -> None:
    store.write_json(session.operators_json, {"v": SCHEMA, "grants": grants})


def expires_at(grant: Dict[str, Any]) -> Optional[float]:
    """Epoch seconds this grant lapses, or None when it does not."""
    raw = grant.get("expires_at")
    if not isinstance(raw, str) or not raw:
        return None
    from herdr_team.roster import parse_iso

    return parse_iso(raw)


def is_live(grant: Dict[str, Any], now: Optional[float] = None) -> bool:
    lapses = expires_at(grant)
    if lapses is None:
        return True
    return (time.time() if now is None else now) < lapses


def active(session: SessionPaths, team: Optional[str], member: Optional[str], now: Optional[float] = None) -> Optional[Dict[str, Any]]:
    """The live grant for one member of one team, or None."""
    if not team or not member:
        return None
    grant = read(session).get(_key(team, member))
    if not isinstance(grant, dict) or not is_live(grant, now):
        return None
    return dict(grant)


def active_all(session: SessionPaths, now: Optional[float] = None) -> List[Dict[str, Any]]:
    """Every live grant, newest first, each carrying its ``team`` and ``member``."""
    out: List[Dict[str, Any]] = []
    for key, grant in read(session).items():
        if not is_live(grant, now):
            continue
        team, _, member = key.partition("/")
        if not team or not member:
            continue
        out.append(dict(grant, team=team, member=member))
    out.sort(key=lambda g: str(g.get("granted_at") or ""), reverse=True)
    return out


def grant(session: SessionPaths, team: str, member: str, ttl_s: Optional[int] = None, note: Optional[str] = None, granted_by: str = "human") -> Dict[str, Any]:
    """Record one delegation. ``ttl_s`` of 0 or None means it does not expire."""
    seconds = DEFAULT_TTL_S if ttl_s is None else int(ttl_s)
    entry: Dict[str, Any] = {
        "team": team,
        "member": member,
        "granted_at": store.now_iso(),
        "granted_by": granted_by,
        "expires_at": _iso_at(time.time() + seconds) if seconds > 0 else None,
        "note": (note or "").strip()[:200] or None,
    }
    grants = read(session)
    grants[_key(team, member)] = entry
    _write(session, grants)
    return dict(entry)


def revoke(session: SessionPaths, team: str, member: str) -> bool:
    """Drop a delegation. True when one was there."""
    grants = read(session)
    if grants.pop(_key(team, member), None) is None:
        return False
    _write(session, grants)
    return True


def refuse_without_grant(team: Optional[str], author: Any, action: str) -> HerdrTeamError:
    """The refusal a member gets when it tries an operator-only command."""
    return HerdrTeamError(
        "author_mismatch",
        "{} is the operator's; {!r} holds no delegation. The operator grants one with: "
        "herdr-synapse operator grant {} --team {}".format(action, getattr(author, "name", "?"), getattr(author, "name", "<member>"), team or "<team>"),
        EXIT_REFUSED,
        {"action": action, "author": getattr(author, "name", None), "via": getattr(author, "via", None), "needs": "operator_grant"},
    )
