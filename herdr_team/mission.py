"""Mission control: every team in the session on one screen, grouped by what it needs (0.19).

Five lanes, each a list of cards, newest concern first:

* **Needs you**: what waits on a human. Asks, agents stuck in an approval
  dialog, work waiting for the human's review or decision, disputes the
  contradiction mode sends to the human, and operator grants about to expire.
* **Blocked**: work items their owner reported blocked, and work whose owner
  has gone missing.
* **Working**: agents in a turn, with the work item they hold.
* **Done**: work settled or approved in the last day, and agents that finished
  a turn nobody has looked at yet.
* **Idle**: agents with nothing in progress.

Every card carries the literal command that deals with it (``argv``), the same
shape ``work next`` uses, so the popup can show it and a manager agent can run
it. Nothing here writes: it reads the files the notifier and the CLI already
keep (``team.json``, ``who.json``, the board, ``work.jsonl``, ``facts.jsonl``,
the operator grants), so it adds no server state.
"""

from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

from herdr_team import asks as _asks
from herdr_team import facts as _facts
from herdr_team import store
from herdr_team import work as _work
from herdr_team.errors import HerdrTeamError

LANES = ("needs_you", "blocked", "working", "done", "idle")
LANE_TITLES = {"needs_you": "Needs you", "blocked": "Blocked", "working": "Working", "done": "Done", "idle": "Idle"}
RECENT_DONE_S = 24 * 3600
GRANT_EXPIRY_WARN_S = 3600
CLI = "herdr-synapse"


def _card(lane: str, team: str, kind: str, title: str, detail: str = "", who: Optional[str] = None, pane_id: Optional[str] = None,
          argv: Optional[List[str]] = None, since: Optional[str] = None) -> Dict[str, Any]:
    return {"lane": lane, "team": team, "kind": kind, "title": title, "detail": detail, "who": who,
            "pane_id": pane_id, "argv": argv, "since": since}


def _live_members(who: Dict[str, Any], team: str) -> Dict[str, Dict[str, Any]]:
    teams = who.get("teams") if isinstance(who.get("teams"), dict) else {}
    row = teams.get(team) if isinstance(teams.get(team), dict) else {}
    return {str(m.get("name")): m for m in row.get("members") or [] if isinstance(m, dict)}


def gather(layout: Any, team_filter: Optional[str] = None, now: Optional[float] = None) -> Dict[str, Any]:
    now = time.time() if now is None else now
    who = store.read_json(layout.session.who_json, default={}) or {}
    if not isinstance(who, dict):
        who = {}
    lanes: Dict[str, List[Dict[str, Any]]] = {lane: [] for lane in LANES}
    try:
        names = sorted(layout.session.list_teams())
    except OSError:
        names = []
    for team_name in names:
        if team_filter and team_name != team_filter:
            continue
        team = layout.session.team(team_name)
        doc = store.read_json(team.team_json, default={}) or {}
        if not isinstance(doc, dict):
            continue
        members = [m for m in doc.get("members") or [] if isinstance(m, dict) and m.get("kind") != "human" and m.get("status") != "left"]
        manager = next((str(m.get("name")) for m in members if m.get("manager")), None)
        live = _live_members(who, team_name)
        items = _safe(lambda: _work.load(team), {})
        state = _safe(lambda: _facts.load(team), None)

        # asks: the notifier opens a popup for these; they stay here until answered
        for record in _safe(lambda: _asks.pending(team, _asks.dismissed(team)), []):
            if isinstance(record.get("work"), dict):
                continue  # a work item's own card (review, decision, blocked) says it better
            lanes["needs_you"].append(_card("needs_you", team_name, "ask", "{} asks: {}".format(record.get("from"), _work.clip(record.get("text"), 120)),
                                            "#{} {}".format(record.get("seq"), record.get("kind")), record.get("from"), None,
                                            [CLI, "--team", team_name, "asks"], record.get("ts")))

        # agents: the lane follows what Herdr reports about each one
        holding: Dict[str, List[str]] = {}
        for item in items.values():
            if item.status in _work.ACTIVE and item.owner:
                holding.setdefault(item.owner, []).append(item.id)
        for member in members:
            name = str(member.get("name"))
            row = live.get(name, {})
            status = row.get("agent_status")
            pane = member.get("pane_id")
            headline = row.get("last_headline") or ""
            work_ids = holding.get(name, [])
            detail = ", ".join(work_ids) + (" · " if work_ids and headline else "") + str(headline or "")
            if member.get("status") in ("missing", "unbound"):
                if work_ids:
                    lanes["blocked"].append(_card("blocked", team_name, "member", "{} is {} and holds {}".format(name, member.get("status"), ", ".join(work_ids)),
                                                  "", name, pane, [CLI, "--team", team_name, "resume", name]))
                continue
            if status == "blocked":
                lanes["needs_you"].append(_card("needs_you", team_name, "dialog", "{} is waiting on a dialog in its pane".format(name), detail, name, pane,
                                                [CLI, "--team", team_name, "focus", name]))
            elif status == "working":
                lanes["working"].append(_card("working", team_name, "member", name, detail, name, pane))
            elif status == "done":
                lanes["done"].append(_card("done", team_name, "member", "{} finished a turn".format(name), detail, name, pane))
            elif not work_ids:
                lanes["idle"].append(_card("idle", team_name, "member", name, str(headline or ""), name, pane))
            else:
                lanes["working"].append(_card("working", team_name, "member", name, detail + " (idle between turns)", name, pane))

        # work items
        for item in _work.sorted_items(items):
            if item.status == _work.STATUS_BLOCKED:
                lanes["blocked"].append(_card("blocked", team_name, "work", "{} {}".format(item.id, _work.clip(item.title, 80)), item.block_reason or "",
                                              item.owner, None, [CLI, "--team", team_name, "work", "show", item.id], item.updated_at))
            elif item.status == _work.STATUS_IN_REVIEW and "human" in item.review_by:
                lanes["needs_you"].append(_card("needs_you", team_name, "review", "review {}: {}".format(item.id, _work.clip(item.title, 80)),
                                                _work.clip(item.summary, 120), item.owner, None,
                                                [CLI, "--team", team_name, "work", "review", item.id, "--approve"], item.updated_at))
            elif item.status in _work.SETTLED_NEEDS_DECISION and (manager is None or item.requester == "human"):
                lanes["needs_you"].append(_card("needs_you", team_name, "decision", "{} ended {}: {}".format(item.id, item.status, _work.clip(item.title, 70)),
                                                _work.clip(item.summary, 120), item.owner, None,
                                                [CLI, "--team", team_name, "work", "reopen", item.id], item.updated_at))
            elif item.status == _work.STATUS_DONE:
                updated = _facts.epoch(item.updated_at)
                if updated is not None and now - updated <= RECENT_DONE_S:
                    lanes["done"].append(_card("done", team_name, "work", "{} done: {}".format(item.id, _work.clip(item.title, 80)),
                                               _work.clip(item.summary, 120), item.owner, None, [CLI, "--team", team_name, "work", "show", item.id], item.updated_at))

        # disputes the contradiction mode sends to the human
        if state is not None:
            for dispute in state.open_disputes():
                to_human = dispute.mode == _facts.MODE_OBSERVE or dispute.escalated_to == "human" or (dispute.mode == _facts.MODE_ESCALATE and manager is None)
                if not to_human:
                    continue
                parties = ", ".join("{} ({})".format(f, state.facts[f].author) for f in dispute.facts if f in state.facts)
                lanes["needs_you"].append(_card("needs_you", team_name, "dispute", "{}: disagreement about {}{}".format(dispute.id, dispute.about or "?", " · " + dispute.attribute if dispute.attribute else ""),
                                                parties, None, None, [CLI, "--team", team_name, "fact", "resolve", dispute.id, "--keep", "<fact>"], dispute.opened_at))

    # grants about to lapse: authority nobody remembers lending is the failure worth naming
    from herdr_team import operator as _operator

    for grant in _safe(lambda: _operator.active_all(layout.session, now), []):
        if team_filter and grant.get("team") != team_filter:
            continue
        expires = _facts.epoch(grant.get("expires_at")) if grant.get("expires_at") else None
        if expires is not None and expires - now <= GRANT_EXPIRY_WARN_S:
            lanes["needs_you"].append(_card("needs_you", str(grant.get("team")), "grant", "{} acts as operator until {}".format(grant.get("member"), grant.get("expires_at")),
                                            "extend it or let it lapse", grant.get("member"), None,
                                            [CLI, "--team", str(grant.get("team")), "operator", "grant", str(grant.get("member"))]))
    return {"lanes": lanes, "counts": {lane: len(cards) for lane, cards in lanes.items()},
            "daemon_beat_at": who.get("daemon_beat_at"), "generated_at": store.now_iso()}


def _safe(fn: Any, default: Any) -> Any:
    """One unreadable team or file must never hide the rest of the session."""
    try:
        return fn()
    except Exception:  # noqa: BLE001 - a hand-edited line of any shape must not blank the screen
        return default


def format_lines(report: Dict[str, Any], width: int = 100, ascii_only: bool = False) -> List[str]:
    """The report as lines, shared by the command and the popup."""
    lines: List[str] = []
    bullet = "-" if ascii_only else "•"
    for lane in LANES:
        cards = report["lanes"].get(lane) or []
        lines.append("{} ({})".format(LANE_TITLES[lane], len(cards)))
        for card in cards:
            head = "  {} [{}] {}".format(bullet, card["team"], card["title"])
            lines.append(head[: max(20, width)])
            if card.get("detail"):
                lines.append(("      " + card["detail"])[: max(20, width)])
            if card.get("argv") and lane in ("needs_you", "blocked"):
                lines.append(("      $ " + " ".join(card["argv"]))[: max(20, width)])
        lines.append("")
    while lines and not lines[-1]:
        lines.pop()
    return lines
