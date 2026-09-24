"""``search``: find what members said and did in their own harness conversations.

The reader is ``herdr_team.transcripts``; this module decides who may read
whose conversation, which conversations that covers, and how the answer is
shown.

Authority follows the rule the skill already teaches, that an agent never
reads a teammate's pane: a transcript is the same thing, kept longer. The
operator from a trusted origin, a member the operator delegated to, and the
team's manager may search any member, since coordinating the team is their
job. Any other member searches only itself, and asking for anyone else is
refused (``author_mismatch``) and audited. Every search is audited as
``transcript_search`` with the size of the query, never its text: the audit
log is read by more people than the transcripts are.
"""
from __future__ import annotations

import argparse
import time
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team import roster as _roster
from herdr_team import transcripts as _transcripts
from herdr_team.cli import Command, api_for, emit, layout_for
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError, UsageError
from herdr_team.identity import audit, authority_refusal

INTEGRATION_HINT = "no conversation recorded; Herdr reports it once the kind's integration is installed (herdr integration install {kind})"


def _add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("query", nargs="+", help='words that must all appear in one message (any case); "a phrase" in double quotes matches exactly')
    parser.add_argument("--member", action="append", metavar="NAME", help="search this member (repeatable); default every agent member, or only yourself when you are a plain member")
    parser.add_argument("--since", metavar="3d|12h|ISO", help="only messages at or after this time (30m, 12h, 3d, 2w, or an ISO date/time)")
    parser.add_argument("--limit", type=int, default=_transcripts.DEFAULT_LIMIT, help="most hits to show, newest first (default {})".format(_transcripts.DEFAULT_LIMIT))
    parser.add_argument("--history", action="store_true", help="also search the member's earlier conversations (before a restart, a clear, or a swap)")
    parser.add_argument("--role", choices=_transcripts.ROLES, help="only what the user said, what the agent said, or its tool calls and results")
    parser.add_argument("--context", type=int, default=_transcripts.DEFAULT_CONTEXT, metavar="CHARS", help="excerpt length around the match (default {})".format(_transcripts.DEFAULT_CONTEXT))


def _manager_name(members: Sequence[Mapping[str, Any]]) -> Optional[str]:
    for member in members:
        if member.get("manager") and member.get("status") != "left" and member.get("kind") != "human":
            return str(member.get("name"))
    return None


def _agents(members: Sequence[Mapping[str, Any]]) -> Dict[str, Mapping[str, Any]]:
    return {str(m.get("name")): m for m in members if m.get("name") and m.get("kind") != "human"}


def scope(layout: Any, team_name: str, members: Sequence[Mapping[str, Any]], author: Any,
          requested: Optional[List[str]]) -> Tuple[List[str], str]:
    """``(member names to search, the authority that allowed it)``, or refuse.

    Authority is decided before the names are checked, so a refusal says the
    same thing whether or not the names exist.
    """
    agents = _agents(members)
    wanted = list(dict.fromkeys(requested or []))
    own = (author.is_member and author.verified and author.team == team_name and author.name in agents)
    if author.trusted_human:
        authority = "operator"
    elif own and getattr(author, "operator", False):
        authority = "delegate"
    elif own and author.name == _manager_name(members):
        authority = "manager"
    elif own:
        authority = "self"
        others = [name for name in wanted if name != author.name]
        if others:
            manager = _manager_name(members)
            audit(layout, team_name, "author_mismatch", author, {"action": "search", "members": others})
            raise HerdrTeamError(
                "author_mismatch",
                "a member searches only its own conversations; {} may search {}. Ask on the board instead.".format(
                    "the operator or the manager ({})".format(manager) if manager else "the operator", ", ".join(others)),
                EXIT_REFUSED, {"action": "search", "author": author.name, "members": others, "manager": manager})
        wanted = [author.name]
    else:
        audit(layout, team_name, "author_mismatch", author, {"action": "search", "members": wanted or ["all"]})
        message = authority_refusal("search", author) if author.is_human else (
            "search reads members' own conversations; only the operator, a delegate, the team's manager, "
            "or the member itself may run it (this is {!r}, {})".format(author.name, author.via))
        raise HerdrTeamError("author_mismatch", message, EXIT_REFUSED, {"action": "search", "author": author.name, "via": author.via})
    if not wanted:
        wanted = [name for name, m in agents.items() if m.get("status") != "left"]
    unknown = [name for name in wanted if name not in agents]
    if unknown:
        raise HerdrTeamError("member_not_found", "{} is not an agent member of team {}".format(", ".join(unknown), team_name), EXIT_REFUSED,
                             {"names": unknown, "team": team_name, "members": sorted(agents)})
    return wanted, authority


def _run(args: argparse.Namespace) -> int:
    from herdr_team.cmd_board import load_doc, members_of, resolve_team
    from herdr_team.cmd_roster import _author

    layout = layout_for(args)
    api = api_for(args, layout)
    author = _author(args, layout, api, require_server=False)
    team_name = resolve_team(args, layout, author)
    if team_name is None:
        raise UsageError("no team; pass --team <name>")
    try:
        query = _transcripts.Query.parse(" ".join(args.query))
    except ValueError as err:
        raise UsageError(str(err))
    since: Optional[float] = None
    if args.since:
        try:
            since = _transcripts.parse_since(args.since, time.time())
        except ValueError as err:
            raise UsageError(str(err))
    if not 1 <= args.limit <= _transcripts.MAX_LIMIT:
        raise UsageError("--limit takes 1 to {}".format(_transcripts.MAX_LIMIT))
    width = max(_transcripts.MIN_CONTEXT, min(_transcripts.MAX_CONTEXT, int(args.context)))

    members = members_of(load_doc(layout.team(team_name)))
    names, authority = scope(layout, team_name, members, author, args.member)
    agents = _agents(members)
    targets = []
    for name in names:
        member = agents[name]
        record = _roster.read_pane_record(layout.session, member.get("terminal_id"))
        refs = _transcripts.session_refs(member, record, bool(args.history))
        targets.append((name, refs, INTEGRATION_HINT.format(kind=member.get("kind") or "<kind>")))

    from herdr_team.context import home_dir

    search = _transcripts.Search(query, since=since, role=args.role, limit=args.limit, width=width,
                                 env=dict(args.env), home=home_dir(dict(args.env)))
    scanned = _transcripts.run(search, targets)
    hits = search.hits()
    audit(layout, team_name, "transcript_search", author, {
        "authority": authority, "query_chars": len(query.text), "terms": len(query.terms), "members": names,
        "history": bool(args.history), "role": args.role, "since": _transcripts.iso_of(since),
        "hits": search.total, "shown": len(hits),
    })
    payload = {
        "team": team_name, "query": query.text, "terms": query.terms, "members": names,
        "since": _transcripts.iso_of(since), "role": args.role, "history": bool(args.history),
        "total": search.total, "hits": hits,
        "scanned": {name: scan.to_json() for name, scan in scanned.items()},
    }
    return emit(args, payload, lambda: render(payload))


def _size(count: int) -> str:
    if count >= 1024 * 1024:
        return "{:.1f} MiB".format(count / (1024.0 * 1024.0))
    if count >= 1024:
        return "{:.0f} KiB".format(count / 1024.0)
    return "{} B".format(count)


def render(payload: Mapping[str, Any], width: int = 0) -> str:
    """Hits newest first, two or three lines each, then what was scanned and what was not."""
    hits = payload.get("hits") or []
    total = int(payload.get("total") or 0)
    query = payload.get("query")
    lines: List[str] = []
    if not hits:
        lines.append("no messages match {} in {}".format(query, ", ".join(payload.get("members") or []) or "team {}".format(payload.get("team"))))
    else:
        more = " (showing the newest {})".format(len(hits)) if total > len(hits) else ""
        lines.append("{} match{} for {} in team {}, newest first{}".format(total, "" if total == 1 else "es", query, payload.get("team"), more))
    for hit in hits:
        epoch = _roster.parse_iso(hit.get("ts"))
        # Local time, the clock ``--since 2026-09-22`` is read in; JSON keeps UTC.
        stamp = datetime.fromtimestamp(epoch).strftime("%Y-%m-%d %H:%M:%S") if epoch is not None else "time unknown"
        earlier = "  (earlier conversation)" if hit.get("history") else ""
        lines.append("")
        lines.append("{}  {}  {}  {}{}".format(hit.get("member"), hit.get("kind"), stamp, hit.get("role"), earlier))
        text = _transcripts.marked(hit)
        if width > 8 and len(text) > width - 2:
            text = text[: width - 3] + "…"
        lines.append("  " + text)
        where = str(hit.get("source_path") or "")
        session = str(hit.get("session") or "")
        lines.append("  session {}{}".format(session, "" if where == session or not where else "  " + where))
    scanned = payload.get("scanned") or {}
    if scanned:
        lines.append("")
        parts = []
        for name, scan in scanned.items():
            count = int(scan.get("sessions") or 0)
            parts.append("{} {} conversation{} ({})".format(name, count, "" if count == 1 else "s", _size(int(scan.get("bytes") or 0))))
        lines.append("scanned: " + "; ".join(parts))
        for name, scan in scanned.items():
            for reason in scan.get("skipped") or []:
                lines.append("  {}: {}".format(name, reason))
    return "\n".join(lines)


COMMANDS: List[Command] = [
    Command("search", "search what members said and did in their own conversations (the manager or operator: anyone; a member: itself)",
            _add_arguments, _run),
]
