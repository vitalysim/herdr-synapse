"""Daemon-private delivery ledger ``notifier/ledger.jsonl`` (plan 8.3).

Append-only JSON lines, one per phase, every line fsynced through
``store.append_line``:

* ``intent``  written before ``agent.prompt`` (carries the attempt metrics),
* ``result``  written after the call returned or failed,
* ``outcome`` written when the recipient's cursor moves past the nudged seqs,
* ``wrong_target`` written when the daemon caught itself about to prompt a
  terminal that is not in any roster (the count must stay zero).

On restart an intent without a result counts as sent (``open_intents``): the
daemon must not re-send it immediately. Human-visible outcomes also become
board ``system`` records; the daemon does that, not this module.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import asdict, dataclass, field
from typing import Any, Dict, Iterator, List, Optional

from herdr_team import store
from herdr_team.paths import TeamPaths

PHASE_INTENT = "intent"
PHASE_RESULT = "result"
PHASE_OUTCOME = "outcome"
PHASE_WRONG_TARGET = "wrong_target"

RESULT_LANDED_WORKING = "landed_working"
RESULT_LANDED_IN_TURN = "landed_in_turn"
RESULT_NOT_SUBMITTED = "not_submitted"
RESULT_WRONG_OCCUPANT = "wrong_occupant"
RESULT_WRONG_TARGET = "wrong_target"
RESULT_HUNG = "hung"
RESULT_REFUSED = "refused"
RESULT_TRANSIENT = "transient"
RESULT_DRY = "dry"

#: Every result the daemon can record; ``counts`` always lists them.
RESULTS = (
    RESULT_LANDED_WORKING,
    RESULT_LANDED_IN_TURN,
    RESULT_NOT_SUBMITTED,
    RESULT_WRONG_OCCUPANT,
    RESULT_WRONG_TARGET,
    RESULT_HUNG,
    RESULT_REFUSED,
    RESULT_TRANSIENT,
    RESULT_DRY,
)

#: A clean landing: ``landed_working`` and the cursor advanced within 5 min on the first attempt.
CLEAN_CURSOR_LATENCY_MS = 300000.0


def now_iso() -> str:
    t = time.time()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + ".{:03d}Z".format(int((t - int(t)) * 1000))


@dataclass
class Attempt:
    """Metrics per nudge (plan 8.3 last paragraph)."""

    id: str
    member: str
    kind: str
    seqs: List[int]
    hook_authority: bool
    weak_idle: bool
    focused: bool
    prompt_line_empty: bool
    gate_ms: float
    queue_ms: float
    attempts: int
    manifest_source: Optional[str] = None
    result: Optional[str] = None
    result_ms: Optional[float] = None
    cursor_advanced: Optional[bool] = None
    cursor_latency_ms: Optional[float] = None
    extra: Dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> Dict[str, Any]:
        obj = asdict(self)
        extra = obj.pop("extra", None) or {}
        for key, value in extra.items():
            obj.setdefault(key, value)
        return obj


class Ledger:
    def __init__(self, team: TeamPaths) -> None:
        self.team = team
        self.path = team.ledger

    # -- writes ---------------------------------------------------------------

    def _append(self, obj: Dict[str, Any]) -> None:
        line = json.dumps(obj, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        store.append_line(self.path, line, fsync=True)

    def record_intent(self, attempt: Attempt) -> None:
        obj: Dict[str, Any] = {"phase": PHASE_INTENT, "ts": now_iso()}
        obj.update(attempt.to_json())
        self._append(obj)

    def record_result(self, attempt_id: str, result: str, details: Optional[Dict[str, Any]] = None) -> None:
        obj: Dict[str, Any] = {"phase": PHASE_RESULT, "ts": now_iso(), "id": attempt_id, "result": result}
        if details:
            for key, value in details.items():
                obj.setdefault(key, value)
        self._append(obj)

    def record_outcome(self, attempt_id: str, cursor_advanced: bool, latency_ms: Optional[float]) -> None:
        self._append({
            "phase": PHASE_OUTCOME,
            "ts": now_iso(),
            "id": attempt_id,
            "cursor_advanced": bool(cursor_advanced),
            "cursor_latency_ms": latency_ms,
        })

    def record_wrong_target(self, member: str, terminal_id: Optional[str], pane_id: Optional[str], reason: str) -> None:
        """The hard rule tripped: the daemon refused to prompt a non-roster terminal."""
        self._append({
            "phase": PHASE_WRONG_TARGET,
            "ts": now_iso(),
            "result": RESULT_WRONG_TARGET,
            "member": member,
            "terminal_id": terminal_id,
            "pane_id": pane_id,
            "reason": reason,
        })

    # -- reads ----------------------------------------------------------------

    def replay(self) -> Iterator[Dict[str, Any]]:
        """Every parseable record, oldest first; torn or garbage lines are skipped."""
        raw = store.read_bytes(self.path)
        if not raw:
            return
        for line in raw.split(b"\n"):
            if not line.strip():
                continue
            try:
                obj = json.loads(line.decode("utf-8", "replace"))
            except ValueError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("phase"), str):
                yield obj

    def attempts(self) -> Dict[str, Dict[str, Any]]:
        """Intents merged with their result and outcome, keyed by attempt id, in intent order."""
        merged: Dict[str, Dict[str, Any]] = {}
        for obj in self.replay():
            phase = obj.get("phase")
            attempt_id = obj.get("id")
            if not isinstance(attempt_id, str):
                continue
            if phase == PHASE_INTENT:
                entry = dict(obj)
                entry.pop("phase", None)
                entry.setdefault("result", None)
                entry.setdefault("cursor_advanced", None)
                entry.setdefault("cursor_latency_ms", None)
                merged[attempt_id] = entry
            elif phase == PHASE_RESULT:
                entry = merged.setdefault(attempt_id, {"id": attempt_id})
                entry["result"] = obj.get("result")
                for key, value in obj.items():
                    if key not in ("phase", "ts", "id", "result"):
                        entry.setdefault(key, value)
                entry["result_ts"] = obj.get("ts")
            elif phase == PHASE_OUTCOME:
                entry = merged.setdefault(attempt_id, {"id": attempt_id})
                entry["cursor_advanced"] = obj.get("cursor_advanced")
                entry["cursor_latency_ms"] = obj.get("cursor_latency_ms")
        return merged

    def open_intents(self) -> List[Dict[str, Any]]:
        """Intents with no result: treated as sent after a restart."""
        return [entry for entry in self.attempts().values() if entry.get("result") is None and "member" in entry]

    def counts(self) -> Dict[str, int]:
        """Per-result totals including ``wrong_target``; every known result key is present."""
        totals: Dict[str, int] = {name: 0 for name in RESULTS}
        totals["intents"] = 0
        totals["open_intents"] = 0
        totals["outcomes"] = 0
        totals["cursor_advanced"] = 0
        for obj in self.replay():
            phase = obj.get("phase")
            if phase == PHASE_INTENT:
                totals["intents"] += 1
            elif phase == PHASE_RESULT:
                result = obj.get("result")
                if isinstance(result, str):
                    totals[result] = totals.get(result, 0) + 1
            elif phase == PHASE_OUTCOME:
                totals["outcomes"] += 1
                if obj.get("cursor_advanced"):
                    totals["cursor_advanced"] += 1
            elif phase == PHASE_WRONG_TARGET:
                totals[RESULT_WRONG_TARGET] += 1
        totals["open_intents"] = len(self.open_intents())
        return totals

    @staticmethod
    def is_clean(entry: Dict[str, Any]) -> bool:
        if entry.get("result") != RESULT_LANDED_WORKING:
            return False
        if not entry.get("cursor_advanced"):
            return False
        latency = entry.get("cursor_latency_ms")
        if latency is not None and float(latency) > CLEAN_CURSOR_LATENCY_MS:
            return False
        return int(entry.get("attempts") or 1) == 1

    def clean_rate(self, kind: str, window: int = 20) -> Optional[float]:
        """Share of the last ``window`` completed round trips for ``kind`` that were clean; None below the window."""
        completed = [
            entry for entry in self.attempts().values()
            if entry.get("kind") == kind and entry.get("result") is not None and entry.get("result") != RESULT_DRY
        ]
        if len(completed) < window:
            return None
        recent = completed[-window:]
        clean = sum(1 for entry in recent if self.is_clean(entry))
        return clean / float(window)

    def stats(self) -> Dict[str, Any]:
        """``counts`` plus per-kind clean rates, for ``daemon status`` and ``notifier stats``."""
        counts = self.counts()
        kinds: Dict[str, Dict[str, Any]] = {}
        for entry in self.attempts().values():
            kind = entry.get("kind")
            if not isinstance(kind, str) or entry.get("result") is None:
                continue
            bucket = kinds.setdefault(kind, {"round_trips": 0, "clean": 0})
            bucket["round_trips"] += 1
            if self.is_clean(entry):
                bucket["clean"] += 1
        for kind, bucket in kinds.items():
            bucket["clean_rate"] = self.clean_rate(kind)
            bucket["verified"] = bool(bucket["clean_rate"] is not None and bucket["clean_rate"] >= 0.9)
        return {"counts": counts, "kinds": kinds, "path": os.fspath(self.path)}
