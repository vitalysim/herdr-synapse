"""Which posts are still waiting on the operator.

An agent addressing the operator is the one thing on the board that needs a
person, and nothing in the schema says so: ``kind: "answer"`` exists in the
enum and no code branches on it, and there is no ``answered`` or ``resolved``
field anywhere. So "is this still waiting" is derived, and this module is the
one place that derives it.

An ask is a post from a member whose recipients include ``human``. It stops
being pending when a **later record from the operator** replies to it, when it
is retracted, or when the operator dismisses it. A reply from a *peer* does not
clear it: that is exactly what went wrong on the team this was written for,
where six of ten asks were never answered by the operator and the four that
were came from other agents, one of them overriding a genuine halt.
"""
from __future__ import annotations

import json
import os
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set

from herdr_team import store
from herdr_team.errors import HerdrTeamError
from herdr_team.paths import TeamPaths

#: Kinds that mean "I am waiting on you" rather than "for your records". Only
#: these block by default; the popup shows every ask whatever its kind.
ASKING_KINDS = ("question", "blocked", "request")

#: How much of the board to look back over. An ask older than this is not worth
#: a modal, and reading the whole archive to find one would make the daemon's
#: tick cost grow with the life of the team.
LOOKBACK = 400


def is_ask(record: Any) -> bool:
    """True when ``record`` is a member post addressed to the operator."""
    if not isinstance(record, dict):
        return False
    if record.get("from") in (None, "human", "system"):
        return False
    if record.get("kind") not in ("note", "request", "handoff", "done", "blocked", "question", "answer"):
        return False
    return "human" in [t for t in (record.get("to") or []) if isinstance(t, str)]


def answered_by(record: Any) -> Optional[int]:
    """The seq this record answers, when the operator wrote it, else None.

    Only the operator closes an ask. A teammate replying to a question is a
    peer note on the same thread; it may be useful, it is not an answer.
    """
    if not isinstance(record, dict) or record.get("from") != "human":
        return None
    reply_to = record.get("reply_to")
    return reply_to if isinstance(reply_to, int) and not isinstance(reply_to, bool) else None


def pending(team: TeamPaths, dismissed: Optional[Iterable[int]] = None, lookback: int = LOOKBACK) -> List[Dict[str, Any]]:
    """Asks still waiting on the operator, newest first.

    Cheap enough for a daemon tick: one bounded read, no archive.
    """
    try:
        records = store.BoardStore(team).read(last=max(1, int(lookback)), include_retracted=False)
    except HerdrTeamError:
        return []
    skip: Set[int] = {int(s) for s in (dismissed or []) if isinstance(s, int) and not isinstance(s, bool)}
    for record in records:
        seq = answered_by(record)
        if seq is not None:
            skip.add(seq)
        retracts = record.get("retracts")
        if isinstance(retracts, int) and not isinstance(retracts, bool):
            skip.add(retracts)
    out = [r for r in records if is_ask(r) and r.get("seq") not in skip]
    out.sort(key=lambda r: int(r.get("seq") or 0), reverse=True)
    return out


def blocks(kind: Any, policy: Dict[str, Any]) -> bool:
    """True when a post of this kind should wait for the operator."""
    if not policy.get("block"):
        return False
    return str(kind or "") in [str(k) for k in (policy.get("block_kinds") or ())]


# --------------------------------------------------------------------------
# what the operator has waved away for now


def _dismissed_path(team: TeamPaths):
    return team.root / "notifier" / "dismissed-asks.json"


def dismissed(team: TeamPaths) -> List[int]:
    """Seqs the operator closed the popup on. "Not now", not "answered"."""
    doc = store.read_json(_dismissed_path(team), default=None)
    if not isinstance(doc, dict):
        return []
    seqs = doc.get("seqs")
    return [int(s) for s in seqs if isinstance(s, int) and not isinstance(s, bool)] if isinstance(seqs, list) else []


def dismiss(team: TeamPaths, seqs: Sequence[int], keep: int = 200) -> List[int]:
    """Remember that the operator waved these away, so the popup stops reopening."""
    current = dismissed(team)
    for seq in seqs:
        if isinstance(seq, int) and not isinstance(seq, bool) and seq not in current:
            current.append(seq)
    current = sorted(current)[-max(1, int(keep)):]
    store.write_json(_dismissed_path(team), {"v": 1, "seqs": current})
    return current
