"""``watch`` and ``unwatch``: flag any agent and see what it is doing (0.21).

``watch TARGET`` flags an agent (a pane id such as ``w3:p2``, a Herdr agent
name, a member name, or a terminal id), in a team or not; ``watch show
[TARGET]`` prints activity cards (``herdr_team.activity``); ``watch list``
names the flagged agents; ``unwatch TARGET`` drops a flag and clears the
agent's ``team_doing`` sidebar token. ``watch`` takes positional words, so a
first word ``show`` or ``list`` is the sub-action and anything else is a
target: to watch an agent literally named ``show``, use its pane id.

Authority (``.local/prd/canvas-contracts.md`` sections 3.1 and 10.3):
flagging and unflagging are the operator's, in person; a card shows what an
agent ran and edited, read from its own transcript, so ``show`` is the
operator's too, and a team's manager (or a member the operator delegated to)
may see its own team's members. ``list`` only reads the flags. Everything but
``list`` refuses ``whiteboard_off`` while the session's whiteboard layer is
off; the flags themselves are kept.
"""
from __future__ import annotations

import argparse
from datetime import datetime
from typing import Any, Dict, List, Mapping, Optional, Tuple

from herdr_team import activity as _activity
from herdr_team import features as _features
from herdr_team import roster as _roster
from herdr_team import store
from herdr_team.cli import Command, api_for, emit, layout_for
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError, UsageError
from herdr_team.identity import audit, authority_refusal
from herdr_team.paths import team_name_from_arg

STEP_MARKS = {"completed": "✓", "in_progress": "▶", "pending": "·", "cancelled": "✗"}


def _add_watch_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("words", nargs="*", metavar="TARGET | show [TARGET] | list",
                        help="an agent to watch (pane id, agent name, member name), or: show [TARGET], list")


def _add_unwatch_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("target", help="the watched agent: pane id, agent name, member name, or terminal id")


# --------------------------------------------------------------------------
# who is asking, and about whom


def _open(args: argparse.Namespace) -> Tuple[Any, Any, Any, Optional[str]]:
    """Layout, api, author, and the team hint (None when the session has several and none is implied)."""
    from herdr_team.cmd_board import resolve_author, resolve_team

    layout = layout_for(args)
    api = api_for(args, layout)
    try:
        hint = resolve_team(args, layout, None, required=False)
    except HerdrTeamError:
        hint = None  # several teams and nothing to pick one: watch is not about a team
    author = resolve_author(args, layout, api, team=hint, require_server=False)
    return layout, api, author, hint


def _require_operator(layout: Any, author: Any, action: str, team: Optional[str]) -> None:
    if author.trusted_human:
        return
    audit(layout, author.team or team, "author_mismatch", author, {"action": action})
    if author.is_human:
        message = authority_refusal(action, author)
    else:
        message = "{} is for the operator in person; {} is an agent, so ask the operator on the board".format(action, author.name)
    raise HerdrTeamError("author_mismatch", message, EXIT_REFUSED, {"action": action, "author": author.name, "via": author.via})


def _manager_of(layout: Any, team: str) -> Optional[str]:
    doc = store.read_json(layout.team(team).team_json, default=None)
    for member in (doc.get("members") if isinstance(doc, dict) else None) or []:
        if isinstance(member, dict) and member.get("manager") and member.get("status") != "left" and member.get("kind") != "human":
            return str(member.get("name"))
    return None


def show_scope(layout: Any, author: Any) -> Tuple[str, Optional[str]]:
    """``(authority, team)``: the operator sees any agent (team None); a manager or delegate its own team's members."""
    if author.trusted_human:
        return "operator", None
    if author.is_member and author.verified and author.team:
        if getattr(author, "operator", False):
            return "delegate", str(author.team)
        if _manager_of(layout, str(author.team)) == author.name:
            return "manager", str(author.team)
    audit(layout, author.team, "author_mismatch", author, {"action": "watch show"})
    message = authority_refusal("watch show", author) if author.is_human else (
        "watch show reads what agents ran and edited; only the operator, or a team's manager for its own members, may run it "
        "(this is {!r}, {})".format(author.name, author.via))
    raise HerdrTeamError("author_mismatch", message, EXIT_REFUSED, {"action": "watch show", "author": author.name, "via": author.via})


def _live(api: Any) -> Dict[str, Dict[str, Any]]:
    """``agent.list`` by terminal id; raises when Herdr cannot be reached (flagging needs the live agent)."""
    result = api.request("agent.list", {}, timeout=5.0)
    agents = result.get("agents") if isinstance(result, dict) else None
    return {str(a["terminal_id"]): a for a in agents or [] if isinstance(a, dict) and isinstance(a.get("terminal_id"), str)}


def resolve(text: str, rows: Optional[Mapping[str, Mapping[str, Any]]], members: Mapping[str, Tuple[str, Mapping[str, Any]]],
            entries: List[Dict[str, Any]], team_hint: Optional[str] = None) -> Dict[str, Any]:
    """``{"terminal_id", "target", "row", "entry"}`` for a pane id, member name, agent name, or terminal id.

    Tried in that order, the hinted team's members before other teams'; a
    watched agent that is no longer running is still found by the name,
    pane or terminal its flag recorded. ``agent_not_found`` otherwise.
    """
    text = (text or "").strip()
    live = rows or {}
    terminal: Optional[str] = None
    for tid, row in live.items():
        if row.get("pane_id") == text:
            terminal = tid
            break
    if terminal is None:
        ranked = sorted(members.items(), key=lambda item: (item[1][0] != team_hint, item[1][0]))
        terminal = next((tid for tid, (_team, member) in ranked if member.get("name") == text), None)
    if terminal is None:
        terminal = next((tid for tid, row in live.items() if row.get("name") == text and row.get("agent")), None)
    if terminal is None and text in live:
        terminal = text
    if terminal is None:
        for field in ("terminal_id", "name", "pane_id"):
            terminal = next((e["terminal_id"] for e in entries if e.get(field) == text), None)
            if terminal is not None:
                break
    if terminal is None:
        raise HerdrTeamError("agent_not_found", "no agent {!r} in this Herdr session: pass a pane id (w3:p2), an agent name, or a member name".format(text),
                             EXIT_REFUSED, {"target": text})
    row = live.get(terminal)
    entry = next((e for e in entries if e["terminal_id"] == terminal), None)
    base: Dict[str, Any] = dict(entry) if entry is not None else {"terminal_id": terminal}
    if isinstance(row, Mapping):
        base.update(pane_id=row.get("pane_id") or base.get("pane_id"), kind=row.get("agent") or base.get("kind"),
                    name=base.get("name") or row.get("name"))
    target = _activity.target_for(base, members.get(terminal))
    if not target.get("name"):
        target["name"] = target.get("pane_id")
    return {"terminal_id": terminal, "target": target, "row": row, "entry": entry}


def _stamp(api: Any, card: Dict[str, Any]) -> bool:
    """Stamp the card's ``team_doing`` token now rather than at the notifier's next refresh; best effort."""
    results = _roster.execute_token_commands(api, _activity.doing_token_commands([card]))
    return bool(results) and all(r.get("ok") for r in results)


def _ensure_daemon(layout: Any, env: Mapping[str, str]) -> None:
    """The notifier keeps the token fresh; start it when it is not running (``HERDR_TEAM_NO_DAEMON=1`` skips)."""
    from herdr_team.cmd_roster import _ensure_daemon as ensure

    try:
        ensure(layout, dict(env))
    except Exception:  # noqa: BLE001 - the flag is stored; a notifier that will not start is doctor's business
        pass


# --------------------------------------------------------------------------
# the commands


def _run_watch(args: argparse.Namespace) -> int:
    words = [str(w) for w in args.words or []]
    if not words or words[0] == "list":
        if len(words) > 1:
            raise UsageError("watch list takes no target")
        return _run_list(args)
    if words[0] == "show":
        if len(words) > 2:
            raise UsageError("watch show takes at most one target")
        return _run_show(args, words[1] if len(words) == 2 else None)
    if len(words) != 1:
        raise UsageError("watch takes one target: a pane id (w3:p2), an agent name, or a member name")
    return _run_add(args, words[0])


def _run_add(args: argparse.Namespace, text: str) -> int:
    layout, api, author, hint = _open(args)
    _require_operator(layout, author, "watch", hint)
    _features.require_layer(layout.session)
    rows = _live(api)
    entries = _activity.watched(layout.session)
    found = resolve(text, rows, _activity.memberships(layout), entries, hint)
    row = found["row"]
    if not isinstance(row, dict) or not row.get("agent"):
        raise HerdrTeamError("agent_not_found", "{} is not a running agent; watch it while its agent runs".format(text), EXIT_REFUSED,
                             {"target": text, "terminal_id": found["terminal_id"]})
    target = found["target"]
    stored = _activity.add_watch(layout.session, {
        "terminal_id": found["terminal_id"], "pane_id": row.get("pane_id"), "name": target.get("name"),
        "kind": row.get("agent"), "team": target.get("team"),
    }, by=author.name)
    card = _activity.card(layout, target, live=row, env=args.env, watched_ids={found["terminal_id"]})
    stamped = _stamp(api, card)
    _ensure_daemon(layout, args.env)
    count = len(_activity.watched(layout.session))
    payload = {"watching": stored, "added": found["entry"] is None, "card": card, "watched": count,
               "max": _activity.MAX_WATCHED, "token": {"key": _activity.DOING_TOKEN, "value": card.get("doing"), "stamped": stamped}}
    label = "{} ({} · {})".format(stored.get("name") or stored.get("pane_id"), stored.get("kind") or "?", stored.get("pane_id") or "-")

    def human() -> str:
        lead = "already watching {}".format(label) if found["entry"] is not None else "watching {}".format(label)
        doing = card.get("doing")
        lines = [lead + ("; its sidebar row shows: {}".format(doing) if doing else "; its sidebar row shows what it is doing ($team_doing)"),
                 "{}/{} watched. See it: herdr-synapse watch show {}".format(count, _activity.MAX_WATCHED, stored.get("pane_id") or text)]
        return "\n".join(lines)

    return emit(args, payload, human)


def _run_unwatch(args: argparse.Namespace) -> int:
    layout, api, author, hint = _open(args)
    _require_operator(layout, author, "unwatch", hint)
    _features.require_layer(layout.session)
    rows = _activity.live_rows(api)
    entries = _activity.watched(layout.session)
    found = resolve(args.target, rows, _activity.memberships(layout), entries, hint)
    removed = _activity.remove_watch(layout.session, found["terminal_id"])
    cleared: List[str] = []
    if removed is not None:
        panes = [removed.get("pane_id"), (found["row"] or {}).get("pane_id")]
        for pane in dict.fromkeys(p for p in panes if isinstance(p, str) and p):
            if _activity.clear_token(api, pane):
                cleared.append(pane)
    payload = {"removed": removed, "terminal_id": found["terminal_id"], "cleared": cleared,
               "watched": len(_activity.watched(layout.session))}
    name = (removed or found["target"]).get("name") or args.target
    if removed is None:
        return emit(args, payload, "{} was not watched".format(name))
    return emit(args, payload, "stopped watching {}{}".format(name, "; its sidebar token was cleared" if cleared else ""))


def _run_list(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    api = api_for(args, layout)
    entries = _activity.watched(layout.session)
    rows = _activity.live_rows(api) if entries else {}
    agents = []
    for entry in entries:
        row = rows.get(entry["terminal_id"]) if rows is not None else None
        running = None if rows is None else bool(isinstance(row, dict) and row.get("agent"))
        agents.append(dict(entry, running=running, live_pane_id=(row or {}).get("pane_id")))
    layer = _features.layer_enabled(layout.session)
    payload = {"layer": layer, "agents": agents, "max": _activity.MAX_WATCHED}

    def human() -> str:
        lines: List[str] = []
        if not layer:
            lines.append("the whiteboard layer is off, so nothing is refreshed; the operator turns it on with: herdr-synapse whiteboard enable")
        if not agents:
            lines.append("no agents are watched; watch one with: herdr-synapse watch <pane|name>")
            return "\n".join(lines)
        lines.append("{} of {} watched:".format(len(agents), _activity.MAX_WATCHED))
        for agent in agents:
            state = {None: "", True: "", False: "  (not running)"}[agent["running"]]
            lines.append("  ◉ {:<24} {:<9} {:<8} {}{}".format(
                agent.get("name") or "-", agent.get("kind") or "?", agent.get("live_pane_id") or agent.get("pane_id") or "-",
                "team " + agent["team"] if agent.get("team") else "no team", state))
        return "\n".join(lines)

    return emit(args, payload, human)


def _run_show(args: argparse.Namespace, text: Optional[str]) -> int:
    layout, api, author, hint = _open(args)
    _features.require_layer(layout.session)
    authority, scope_team = show_scope(layout, author)
    explicit = team_name_from_arg(getattr(args, "team", None), dict(args.env))
    if text is not None:
        rows = _activity.live_rows(api)
        entries = _activity.watched(layout.session)
        found = resolve(text, rows, _activity.memberships(layout), entries, scope_team or hint)
        target = found["target"]
        if scope_team is not None and target.get("team") != scope_team:
            audit(layout, scope_team, "author_mismatch", author, {"action": "watch show", "target": text})
            raise HerdrTeamError(
                "author_mismatch", "{} may watch only its own team's members; {} is not in team {}".format(author.name, text, scope_team),
                EXIT_REFUSED, {"action": "watch show", "author": author.name, "team": scope_team, "target": text})
        live: Any = rows.get(found["terminal_id"]) if rows is not None else None
        kwargs: Dict[str, Any] = {"live": live} if rows is not None else {}
        result = [_activity.card(layout, target, env=args.env, watched_ids={e["terminal_id"] for e in entries}, **kwargs)]
    else:
        team = scope_team or explicit
        result = _activity.cards(layout, api, team=team, env=args.env, include_watched=scope_team is None)
    audit(layout, scope_team or explicit or (result[0].get("team") if len(result) == 1 else None), "watch_show", author,
          {"authority": authority, "cards": [c.get("name") or c.get("key") for c in result]})
    payload = {"authority": authority, "team": scope_team or explicit, "cards": result}
    return emit(args, payload, lambda: render(result))


# --------------------------------------------------------------------------
# text


def _clock(value: Any) -> str:
    epoch = _roster.parse_iso(value) if isinstance(value, str) else None
    return datetime.fromtimestamp(epoch).strftime("%H:%M") if epoch is not None else ""


def render_card(card: Mapping[str, Any]) -> List[str]:
    """One card as text: identity and state, headline, plan, recent actions, files, context, last prompt."""
    kind = str(card.get("kind") or "?") + ("/" + str(card["profile"]) if card.get("profile") else "")
    team = "team {}{}".format(card["team"], "/" + str(card["role"]) if card.get("role") else "") if card.get("team") else "no team"
    marker = "◉ " if card.get("watched") else ""
    lines = [marker + " · ".join(str(p) for p in (card.get("name") or card.get("pane_id") or card.get("key"), kind, card.get("pane_id") or "-", team,
                                                        str(card.get("state") or "unknown").replace("_", " ")))]
    if card.get("headline"):
        lines.append("  " + str(card["headline"]))
    if card.get("doing"):
        lines.append("  doing: " + str(card["doing"]))
    plan = card.get("plan")
    if isinstance(plan, dict) and plan.get("steps"):
        where = "{}/{}".format(plan["current"], plan.get("total")) if plan.get("current") else "{} steps".format(plan.get("total"))
        lines.append("  plan {} ({}):".format(where, plan.get("source")))
        for step in plan["steps"]:
            lines.append("    {} {}".format(STEP_MARKS.get(str(step.get("status")), "·"), step.get("text")))
    actions = card.get("actions") or []
    if actions:
        lines.append("  recent (newest first):")
        for action in actions:
            lines.append("    {:<6} {}{}".format(action.get("icon"), action.get("text"),
                                               "  " + _clock(action.get("at")) if _clock(action.get("at")) else ""))
    if card.get("files"):
        lines.append("  files: " + ", ".join(str(f) for f in card["files"]))
    context = card.get("context")
    if isinstance(context, dict) and context.get("percent") is not None:
        lines.append("  context {:.0f}%".format(float(context["percent"])))
    if card.get("last_prompt"):
        lines.append("  last prompt: " + str(card["last_prompt"]))
    if card.get("reader") is None:
        lines.append("  activity: no reader for {}; title and state only".format(card.get("kind") or "this kind"))
    elif card.get("reader_error"):
        note = card.get("reader_note")
        lines.append("  activity: unknown{}".format(" ({})".format(note) if note else ""))
    return lines


def render(cards: List[Mapping[str, Any]]) -> str:
    if not cards:
        return "no agents are watched; watch one with: herdr-synapse watch <pane|name>"
    out: List[str] = []
    for index, card in enumerate(cards):
        if index:
            out.append("")
        out.extend(render_card(card))
    return "\n".join(out)


COMMANDS: List[Command] = [
    Command("watch", "flag any agent and see what it is doing: watch TARGET | watch show [TARGET] | watch list (operator; show: also a manager for its team)",
            _add_watch_arguments, _run_watch,
            description="watch TARGET flags an agent (pane id, agent name, or member name) so its sidebar row shows what it is doing; "
                        "watch show [TARGET] prints activity cards; watch list names the flagged agents. Flags are the operator's, "
                        "in person; everything but list needs the whiteboard layer on (herdr-synapse whiteboard enable)."),
    Command("unwatch", "drop an agent's watch flag and clear its sidebar token (operator)", _add_unwatch_arguments, _run_unwatch),
]
