"""Generated team views: the work graph, fact map, topology, timeline and lanes as JSON (0.21).

What the page's Team tab draws, computed from files already on disk and
nothing else: ``team.json``, the board, ``work.jsonl``, ``facts.jsonl``, the
link registry, ``who.json`` and the canvas scene. Nothing here writes, and
nothing here asks Herdr anything unless ``who.json`` has no row for the team
(the notifier is down), when one ``agent.list`` fills in the live states.

Every part degrades to empty on unreadable input and never raises, because
one corrupt file must not blank the whole tab. The contract is
``.local/prd/canvas-contracts.md`` section 15.
"""
from __future__ import annotations

import time
from datetime import datetime, timezone
from typing import Any, Callable, Dict, List, Optional, TypeVar

from herdr_team import store
from herdr_team.paths import TeamPaths

TIMELINE_LIMIT = 200
TIMELINE_TEXT_CHARS = 160
#: The fact map keeps the newest this many facts; the page is a map, not an archive.
FACTS_LIMIT = 1000
#: The work graph keeps the newest this many items.
WORK_LIMIT = 1000
#: How a fact's status reads on the map; a disputed fact is ``current`` with its disputes listed.
FACT_STATUSES = ("current", "retired", "superseded")
_T = TypeVar("_T")


def _safe(fn: Callable[[], _T], default: _T) -> _T:
    try:
        return fn()
    except Exception:  # noqa: BLE001 - a view is decoration over stores that have their own readers; one bad file blanks one part only
        return default


def _iso(now: Optional[float]) -> str:
    when = time.time() if now is None else float(now)
    return datetime.fromtimestamp(when, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.") + "{:03d}Z".format(int((when % 1) * 1000))


def _clip(text: Any, limit: int) -> str:
    """One line of printable text, secrets redacted, at most ``limit`` characters."""
    from herdr_team import sanitize as _sanitize
    from herdr_team.cmd_board import SECRET_PATTERNS

    out = str(text or "")
    # Redaction runs before the cut, so a secret split by it cannot slip past a pattern that needs all of it.
    for label, pattern in SECRET_PATTERNS:
        out = pattern.sub("[redacted:{}]".format(label), out)
    out = " ".join(_sanitize.replace_format_chars(_sanitize.strip_controls(out), "").split())
    return out if len(out) <= limit else out[: max(0, limit - 1)].rstrip() + "…"


# --------------------------------------------------------------------------
# work


def work_graph(team: TeamPaths) -> Dict[str, Any]:
    """Work items as nodes and dependencies as edges (dependency -> dependant)."""
    from herdr_team import work as _work

    items = _safe(lambda: _work.load(team), {})
    ordered = _work.sorted_items(items)[-WORK_LIMIT:] if items else []
    shown = {item.id for item in ordered}
    nodes: List[Dict[str, Any]] = []
    edges: List[Dict[str, str]] = []
    for item in ordered:
        nodes.append({
            "id": item.id, "title": _clip(item.title, 200), "status": item.status, "owner": item.owner,
            "attempt": item.attempts[-1].n if item.attempts else 0,
            "ready": _work.is_ready(item, items), "waiting_on": _work.waiting_on(item, items),
            "review": bool(item.review_by),
        })
        for dep in item.deps:
            if dep in shown:
                edges.append({"from": dep, "to": item.id})
    return {"nodes": nodes, "edges": edges}


# --------------------------------------------------------------------------
# facts


def _fact_status(fact: Any) -> str:
    if fact.superseded_by:
        return "superseded"
    if fact.retired_at:
        return "retired"
    return "current"


def fact_map(team: TeamPaths) -> Dict[str, Any]:
    """Facts grouped by subject, with support, supersession and open disputes."""
    from herdr_team import facts as _facts

    state = _safe(lambda: _facts.load(team), None)
    if state is None:
        return {"subjects": [], "disputes": []}
    ordered = state.ordered()[-FACTS_LIMIT:]
    groups: Dict[Optional[str], List[Dict[str, Any]]] = {}
    for fact in ordered:
        about = _clip(fact.about, 120) or None
        groups.setdefault(about, []).append({
            "id": fact.id, "attribute": fact.attribute, "statement": _clip(fact.statement, 400), "author": fact.author,
            "support": len(fact.members), "status": _fact_status(fact), "superseded_by": fact.superseded_by,
            "disputes": [d for d in fact.disputes if state.disputes.get(d) is not None and state.disputes[d].open],
        })
    # Named subjects alphabetically, then the facts recorded without one.
    subjects = [{"about": about, "facts": groups[about]} for about in sorted(k for k in groups if k is not None)]
    if None in groups:
        subjects.append({"about": None, "facts": groups[None]})
    disputes = [{"id": d.id, "facts": list(d.facts), "mode": d.mode, "open": d.open, "about": d.about, "attribute": d.attribute}
                for d in sorted(state.disputes.values(), key=lambda d: (not d.open, int(d.id.split("-", 1)[1])))]
    return {"subjects": subjects, "disputes": disputes}


# --------------------------------------------------------------------------
# topology


def _live_rows(who: Dict[str, Any], team_name: str) -> Optional[Dict[str, Dict[str, Any]]]:
    teams = who.get("teams") if isinstance(who, dict) and isinstance(who.get("teams"), dict) else {}
    row = teams.get(team_name)
    if not isinstance(row, dict):
        return None
    return {str(m.get("name")): m for m in row.get("members") or [] if isinstance(m, dict)}


def _author_colors(team: Optional[TeamPaths]) -> Dict[str, str]:
    """Each member's canvas colour (``scene.authors``), so the Team tab and the canvas agree."""
    if team is None:
        return {}
    from herdr_team import features as _features

    scene = store.read_json(_features.whiteboard_dir(team) / "scene.json", default=None)
    authors = scene.get("authors") if isinstance(scene, dict) and isinstance(scene.get("authors"), dict) else {}
    return {str(name): str(row["color"]) for name, row in authors.items() if isinstance(row, dict) and isinstance(row.get("color"), str)}


def topology(layout: Any, team_name: str, doc: Dict[str, Any], who: Dict[str, Any]) -> Dict[str, Any]:
    """The team, its manager, members with live status, and linked teams."""
    from herdr_team import links as _links

    live = _live_rows(who, team_name) or {}
    team = _safe(lambda: layout.team(team_name), None)
    colors = _safe(lambda: _author_colors(team), {})
    members: List[Dict[str, Any]] = []
    manager = None
    for member in doc.get("members") or [] if isinstance(doc, dict) else []:
        if not isinstance(member, dict) or member.get("kind") == "human" or member.get("status") == "left":
            continue
        name = str(member.get("name") or "")
        if member.get("manager"):
            manager = name
        row = live.get(name) or {}
        members.append({
            "name": name, "role": member.get("role"), "kind": member.get("kind"), "profile": member.get("profile"),
            "status": member.get("status"), "agent_status": row.get("agent_status"),
            "pane_id": row.get("pane_id") or member.get("pane_id"), "color": colors.get(name),
        })
    linked = _safe(lambda: _links.summary(layout.session, team_name), [])
    links = [{"team": row.get("team"), "state": row.get("state"), "manager": row.get("manager")} for row in linked if isinstance(row, dict)]
    return {"team": team_name, "manager": manager, "members": members, "links": links}


def _who_with_live_states(layout: Any, team_name: str, doc: Dict[str, Any], api: Any) -> Dict[str, Any]:
    """``who.json``, or, when it has no row for this team and an ``api`` was given, the same shape from one ``agent.list``."""
    who = store.read_json(layout.session.who_json, default={}) or {}
    who = who if isinstance(who, dict) else {}
    if api is None or _live_rows(who, team_name) is not None:
        return who
    result = _safe(lambda: api.request("agent.list", {}), None)
    agents = result.get("agents") if isinstance(result, dict) and isinstance(result.get("agents"), list) else []
    by_terminal = {str(a.get("terminal_id")): a for a in agents if isinstance(a, dict) and a.get("terminal_id")}
    rows = []
    for member in doc.get("members") or []:
        if not isinstance(member, dict):
            continue
        agent = by_terminal.get(str(member.get("terminal_id") or ""))
        if agent is not None:
            rows.append({"name": member.get("name"), "agent_status": agent.get("agent_status"), "pane_id": agent.get("pane_id")})
    return {"teams": {team_name: {"members": rows}}}


# --------------------------------------------------------------------------
# timeline


def timeline(team: TeamPaths, limit: int = TIMELINE_LIMIT) -> List[Dict[str, Any]]:
    """The newest board records, redacted and clipped, oldest first."""
    records = _safe(lambda: store.BoardStore(team).read(last=max(1, int(limit)), include_retracted=False), [])
    out: List[Dict[str, Any]] = []
    for record in records[-max(1, int(limit)):]:
        if not isinstance(record, dict):
            continue
        to = record.get("to")
        out.append({
            "seq": record.get("seq"), "ts": record.get("ts"), "kind": record.get("kind"), "event": record.get("event"),
            "from": record.get("from"), "to": [str(t) for t in to] if isinstance(to, list) else [],
            "text": _clip(record.get("text"), TIMELINE_TEXT_CHARS),
        })
    return out


# --------------------------------------------------------------------------
# all of it


def _lanes(layout: Any, team_name: str, now: Optional[float]) -> Dict[str, List[Dict[str, Any]]]:
    from herdr_team import mission as _mission

    lanes = _mission.gather(layout, team_filter=team_name, now=now).get("lanes")
    return {lane: list(cards) for lane, cards in lanes.items()} if isinstance(lanes, dict) else {}


def _canvas(layout: Any, team: TeamPaths, doc: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    from herdr_team import canvas as _canvas_mod
    from herdr_team import features as _features

    if not _features.team_switch(layout.session, team, doc).on:
        return None
    summary = _canvas_mod.summary(team)
    return dict(summary) if isinstance(summary, dict) else None


def team_views(layout: Any, team_name: str, api: Any = None, now: Optional[float] = None) -> Dict[str, Any]:
    """Every view of one team: ``work``, ``facts``, ``topology``, ``timeline``, ``lanes``, ``canvas``."""
    team = layout.team(team_name)
    doc = _safe(lambda: store.RosterStore(team).load(), {})
    doc = doc if isinstance(doc, dict) else {}
    who = _safe(lambda: _who_with_live_states(layout, team_name, doc, api), {})
    return {
        "team": team_name,
        "generated_at": _iso(now),
        "work": _safe(lambda: work_graph(team), {"nodes": [], "edges": []}),
        "facts": _safe(lambda: fact_map(team), {"subjects": [], "disputes": []}),
        "topology": _safe(lambda: topology(layout, team_name, doc, who),
                          {"team": team_name, "manager": None, "members": [], "links": []}),
        "timeline": _safe(lambda: timeline(team), []),
        "lanes": _safe(lambda: _lanes(layout, team_name, now), {}),
        "canvas": _safe(lambda: _canvas(layout, team, doc), None),
    }
