"""``fact``, ``facts``, ``contradictions``: the team's findings as facts with time, provenance and disputes (0.19).

The model is ``herdr_team.facts``. This module adds who may do what, the board
records that tell the team, and the contradiction policy switch:

* any member (or the operator) records, supports and retires its own facts;
* superseding or retiring a peer's fact, and resolving a dispute, is for the
  manager or the operator; a manager who is a party to a dispute cannot settle
  it, the operator decides;
* ``contradictions <mode>`` is the operator's switch; it never changes what
  agents may say to each other, only who is told about a clash.
"""
from __future__ import annotations

import argparse
import os
from typing import Any, Dict, List, Optional

from herdr_team import facts as F
from herdr_team import roster as _roster
from herdr_team.cli import Command, add_global_arguments, emit
from herdr_team.cmd_board import (
    _open_team,
    agent_members,
    board_get,
    check_write_session,
    env_of,
    member_generation,
    parse_seq,
    prepare_text,
    update_doc,
    validate_refs,
    warn,
)
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError, UsageError
from herdr_team.identity import Author, audit

CLI = "herdr-synapse"


def _manager(doc: Dict[str, Any]) -> Optional[str]:
    for member in agent_members(doc):
        if member.get("manager"):
            return str(member.get("name"))
    return None


def _may_override(author: Author, doc: Dict[str, Any], state: Optional[F.State] = None, fact_id: Optional[str] = None) -> bool:
    """The operator, a delegate, or the manager; but never a manager overriding the other side of its own dispute."""
    if author.trusted_human or getattr(author, "operator", False):
        return True
    if not (author.is_member and author.verified and _manager(doc) == author.name):
        return False
    if state is not None and fact_id is not None and fact_id in state.facts:
        for dispute_id in state.facts[fact_id].disputes:
            dispute = state.disputes.get(dispute_id)
            if dispute is not None and dispute.open and any(state.facts[f].author == author.name for f in dispute.facts if f in state.facts):
                return False
    return True


def _writer(author: Author) -> None:
    if author.name == "system":
        raise HerdrTeamError("author_mismatch", "hooks and startup processes cannot record facts", EXIT_REFUSED)
    if author.is_member and not author.verified:
        raise HerdrTeamError("author_unverified", "recording a fact needs a verified member pane ({})".format(author.reason or "identity unverified"), EXIT_REFUSED)
    if not author.is_member and not author.trusted_human:
        raise HerdrTeamError("author_mismatch", "facts are recorded by members or the operator; {} is neither".format(author.name), EXIT_REFUSED)


def _one_line(raw: Optional[str], limit: int, what: str) -> Optional[str]:
    if raw is None or not str(raw).strip():
        return None
    text, _t, _b = prepare_text(" ".join(str(raw).split()), False, False)
    if len(text) > limit:
        raise HerdrTeamError("text_too_long", "{} is {} characters; keep it under {}".format(what, len(text), limit), EXIT_REFUSED, {"field": what})
    return text


def _sources(args: argparse.Namespace, layout: Any, team_name: str, team: Any, doc: Dict[str, Any]) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for raw in getattr(args, "source", None) or []:
        out.append(F.url_source(raw))
    for raw in getattr(args, "post", None) or []:
        seq = parse_seq(raw, "--post")
        if board_get(team, seq) is None:
            raise HerdrTeamError("reply_to_unknown", "no post #{} on this board".format(seq), EXIT_REFUSED, {"post": seq})
        out.append({"kind": "post", "seq": seq})
    for path in validate_refs(layout, team_name, list(getattr(args, "ref", None) or []), doc, env=env_of(args)):
        out.append({"kind": "file", "path": path})
    if len(out) > F.MAX_SOURCES:
        raise UsageError("at most {} sources per fact".format(F.MAX_SOURCES))
    return out


def _announce(layout: Any, team: Any, event: str, text: str, to: List[Optional[str]], extra: Dict[str, Any]) -> Optional[int]:
    targets = []
    for name in to:
        if name and name not in targets:
            targets.append(name)
    try:
        return _roster.append_system_record(team, event, text, to=targets or ["all"], extra=extra, socket=os.fspath(layout.socket))
    except HerdrTeamError:
        return None


def dispute_notice(layout: Any, team: Any, doc: Dict[str, Any], state: F.State, dispute: F.Dispute, timeout_ms: int) -> Optional[int]:
    """Tell whoever the dispute's mode says should hear about it; ``observe`` tells only the human."""
    parties = [state.facts[fid] for fid in dispute.facts if fid in state.facts]
    authors = []
    for fact in parties:
        if fact.author not in authors:
            authors.append(fact.author)
    claims = "; ".join("{} ({}) says {!r}".format(f.id, f.author, F.clip(f.statement, 120)) for f in parties)
    subject = "{}{}".format(dispute.about or "?", " · " + dispute.attribute if dispute.attribute else "")
    extra = {"dispute": dispute.id, "facts": list(dispute.facts), "mode": dispute.mode}
    if dispute.mode == F.MODE_OBSERVE:
        return _announce(layout, team, "fact_disputed", "{}: members disagree about {}: {}. Resolve with: {} fact resolve {} --keep <fact>".format(dispute.id, subject, claims, CLI, dispute.id), ["human"], extra)
    manager = _manager(doc)
    decider = manager if manager and manager not in authors else "human"
    if dispute.mode == F.MODE_DEBATE:
        text = ("{}: you disagree about {}: {}. Settle it between you on the board: concede with {} fact retire <your fact>, "
                "or refine yours with {} fact add \"...\" --about \"{}\" --attribute \"{}\" --supersedes <your fact>. "
                "Unresolved in {} min, it goes to {}.").format(dispute.id, subject, claims, CLI, CLI, dispute.about or "", dispute.attribute or "", int(timeout_ms / 60000), decider)
        return _announce(layout, team, "fact_conflict", text, [a for a in authors if a != "human"], extra)
    return _announce(layout, team, "fact_conflict", "{}: members disagree about {}: {}. Decide with: {} fact resolve {} --keep <fact> (or --keep-both / --retire-all)".format(
        dispute.id, subject, claims, CLI, dispute.id), [decider], dict(extra, escalated=True))


# --------------------------------------------------------------------------
# fact <action>


def _add(args: argparse.Namespace) -> int:
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False, write=True)
    _writer(author)
    statement = _one_line(args.statement, F.MAX_STATEMENT_CHARS, "statement")
    if not statement:
        raise UsageError("a fact needs a statement")
    about = _one_line(args.about, F.MAX_SUBJECT_CHARS, "--about")
    attribute = _one_line(args.attribute, F.MAX_SUBJECT_CHARS, "--attribute")
    if attribute and not about:
        raise UsageError("--attribute needs --about: the subject it is an attribute of")
    kind_label = _one_line(args.type, 60, "--type")
    vocab = F.vocabulary(doc)
    if kind_label and vocab and kind_label not in vocab:
        raise HerdrTeamError("type_unknown", "{!r} is not in this team's vocabulary ({})".format(kind_label, ", ".join(sorted(vocab))), EXIT_REFUSED, {"type": kind_label, "vocabulary": sorted(vocab)})
    fields: Dict[str, Any] = {
        "statement": statement, "by": author.name, "by_kind": author.kind or ("human" if author.is_human else None),
        "by_gen": member_generation(doc, author) if author.is_member else None,
        "about": about, "attribute": attribute, "type": kind_label,
        "valid_from": F.parse_when(args.valid_from, "--valid-from"), "valid_to": F.parse_when(args.valid_to, "--valid-to"),
        "sources": _sources(args, layout, team_name, team, doc),
        "supersedes": F.parse_fact_id(args.supersedes) if args.supersedes else None,
    }
    policy = F.contradictions_config(doc)
    result = F.add(team, fields, policy["mode"], may_override=_may_override(author, doc, F.load(team), fields["supersedes"]))
    state = result.state or F.load(team)
    if result.supported is not None and result.fact is None:
        fact = result.supported
        audit(layout, team_name, "fact_support", author, {"id": fact.id})
        seq = _announce(layout, team, "fact_added", "{} supports {}: {} ({})".format(author.name, fact.id, F.clip(fact.label(), 200), fact.confidence()), ["all"],
                        {"fact": {"id": fact.id, "op": "support"}, "author": author.name})
        return emit(args, {"team": team_name, "supported": fact.to_json(state.disputes), "record_seq": seq},
                    "{} already records that; your support was added ({})".format(fact.id, fact.confidence()))
    fact = result.fact
    if fact is None:  # neither recorded nor supported: add() always returns one of the two
        raise HerdrTeamError("fact_not_recorded", "the fact was not recorded", EXIT_REFUSED)
    audit(layout, team_name, "fact_add", author, {"id": fact.id, "supersedes": fact.supersedes, "dispute": result.dispute.id if result.dispute else None})
    text = "{} recorded {}: {}".format(author.name, fact.id, F.clip(fact.label(), 240))
    if result.superseded is not None:
        text += " (supersedes {})".format(result.superseded.id)
    seq = _announce(layout, team, "fact_added", text + ". Facts are peer notes, not instructions.", ["all"],
                    {"fact": {"id": fact.id, "op": "add", "supersedes": fact.supersedes}, "author": author.name})
    notice = dispute_notice(layout, team, doc, state, result.dispute, policy["debate_timeout_ms"]) if result.dispute is not None else None
    for near in result.near_duplicates:
        warn(args, "{} reads almost the same ({}); if it is the same fact, support it instead: {} fact support {}".format(near.id, F.clip(near.statement, 80), CLI, near.id))
    payload = {
        "team": team_name, "fact": fact.to_json(state.disputes), "record_seq": seq,
        "superseded": result.superseded.id if result.superseded else None,
        "near_duplicates": [f.id for f in result.near_duplicates],
        "dispute": result.dispute.to_json() if result.dispute else None, "dispute_seq": notice,
    }
    human = "{} recorded{}".format(fact.id, " (supersedes {})".format(result.superseded.id) if result.superseded else "")
    if result.dispute is not None:
        human += "; it disagrees with {} ({}, mode {})".format(", ".join(f for f in result.dispute.facts if f != fact.id), result.dispute.id, result.dispute.mode)
    return emit(args, payload, human)


def _support(args: argparse.Namespace) -> int:
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False, write=True)
    _writer(author)
    fact_id = F.parse_fact_id(args.id)
    fact = F.support(team, fact_id, author.name, member_generation(doc, author) if author.is_member else None, _sources(args, layout, team_name, team, doc))
    state = F.load(team)
    audit(layout, team_name, "fact_support", author, {"id": fact_id})
    seq = _announce(layout, team, "fact_added", "{} supports {}: {} ({})".format(author.name, fact.id, F.clip(fact.label(), 200), fact.confidence()), ["all"],
                    {"fact": {"id": fact.id, "op": "support"}, "author": author.name})
    return emit(args, {"team": team_name, "fact": fact.to_json(state.disputes), "record_seq": seq}, "{}: {}".format(fact.id, fact.confidence()))


def _retire(args: argparse.Namespace) -> int:
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False, write=True)
    _writer(author)
    fact_id = F.parse_fact_id(args.id)
    reason = _one_line(args.reason, 300, "reason")
    override = _may_override(author, doc, F.load(team), fact_id)
    fact, settled = F.retire(team, fact_id, author.name, reason, override, valid_to=F.parse_when(args.valid_to, "--valid-to"))
    if fact.author != author.name and getattr(author, "operator", False) and not author.trusted_human:
        audit(layout, team_name, "operator_action", author, {"action": "fact retire", "id": fact_id})
    audit(layout, team_name, "fact_retire", author, {"id": fact_id, "settled": [d.id for d in settled]})
    seq = _announce(layout, team, "fact_retired", "{} retired {}{}: {}".format(author.name, fact.id, " ({})".format(reason) if reason else "", F.clip(fact.label(), 200)), ["all"],
                    {"fact": {"id": fact.id, "op": "retire"}, "author": author.name})
    state = F.load(team)
    for dispute in settled:
        how = "conceded" if dispute.resolved_by == "concession" else "was retired by {}".format(dispute.resolved_by)
        _announce(layout, team, "fact_resolved", "{} settled: {} {}; {} stands".format(dispute.id, fact.id, how, ", ".join(dispute.keep) or "nothing"),
                  [state.facts[f].author for f in dispute.facts if f in state.facts] + [_manager(doc)], {"dispute": dispute.id, "keep": dispute.keep})
    return emit(args, {"team": team_name, "fact": fact.to_json(state.disputes), "record_seq": seq, "settled": [d.id for d in settled]}, "{} retired".format(fact.id))


def _resolve(args: argparse.Namespace) -> int:
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False, write=True)
    dispute_id = F.parse_dispute_id(args.id)
    state = F.load(team)
    dispute = state.disputes.get(dispute_id)
    if dispute is None:
        raise HerdrTeamError("dispute_not_found", "no dispute {}".format(dispute_id), EXIT_REFUSED, {"id": dispute_id})
    parties = {state.facts[f].author for f in dispute.facts if f in state.facts}
    manager = _manager(doc)
    allowed = author.trusted_human or getattr(author, "operator", False) or (author.is_member and author.verified and author.name == manager and author.name not in parties)
    if not allowed:
        audit(layout, team_name, "author_mismatch", author, {"action": "fact resolve", "id": dispute_id})
        raise HerdrTeamError("author_mismatch", "a dispute is settled by the manager (when it is not a party) or the operator; a party concedes with: {} fact retire <its fact>".format(CLI),
                             EXIT_REFUSED, {"id": dispute_id, "manager": manager, "parties": sorted(parties)})
    if args.keep_both:
        keep = list(dispute.facts)
    elif args.retire_all:
        keep = []
    else:
        keep = [F.parse_fact_id(k) for raw in args.keep for k in raw.split(",") if k.strip()]
    reason = _one_line(args.reason, 300, "reason")
    dispute = F.resolve(team, dispute_id, author.name, keep, reason)
    audit(layout, team_name, "fact_resolve", author, {"id": dispute_id, "keep": keep})
    seq = _announce(layout, team, "fact_resolved", "{} resolved by {}: {} {}{}".format(
        dispute_id, author.name, "kept " + ", ".join(keep) if keep else "retired every claim", "" if not dispute.facts else "(of {})".format(", ".join(dispute.facts)), "; " + reason if reason else ""),
        sorted(parties) + [manager], {"dispute": dispute_id, "keep": keep})
    return emit(args, {"team": team_name, "dispute": dispute.to_json(), "record_seq": seq}, "{} resolved: {}".format(dispute_id, "kept " + ", ".join(keep) if keep else "nothing kept"))


def _show(args: argparse.Namespace) -> int:
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False)
    state = F.load(team)
    fact_id = F.parse_fact_id(args.id)
    fact = state.facts.get(fact_id)
    if fact is None:
        raise HerdrTeamError("fact_not_found", "no fact {}".format(fact_id), EXIT_REFUSED, {"id": fact_id})
    row = fact.to_json(state.disputes)
    row["dispute_details"] = [state.disputes[d].to_json() for d in fact.disputes if d in state.disputes]

    def human() -> str:
        lines = ["{} [{}] {}".format(fact.id, row["status"], fact.label()),
                 "by {}{}; {}".format(fact.author, " (generation {})".format(fact.author_gen) if fact.author_gen else "", fact.confidence()),
                 "recorded {}{}".format(fact.recorded_at, "; retired {} by {}{}".format(fact.retired_at, fact.retired_by, " ({})".format(fact.retire_reason) if fact.retire_reason else "") if fact.retired_at else "")]
        if fact.valid_from or fact.valid_to:
            lines.append("true from {} to {}".format(fact.valid_from or "?", fact.valid_to or "now"))
        if fact.supersedes:
            lines.append("supersedes {}".format(fact.supersedes))
        if fact.superseded_by:
            lines.append("superseded by {}".format(fact.superseded_by))
        for source in fact.sources:
            if source.get("kind") == "url":
                lines.append("source: {} (read {})".format(source.get("url"), str(source.get("retrieved_at") or "")[:10]))
            elif source.get("kind") == "post":
                lines.append("source: board post #{}".format(source.get("seq")))
            else:
                lines.append("source: {}".format(source.get("path")))
        for d in row["dispute_details"]:
            lines.append("dispute {} ({}{}): {}".format(d["id"], d["mode"], ", open" if d["open"] else ", resolved by {}".format(d["resolved_by"]), ", ".join(d["facts"])))
        return "\n".join(lines)

    return emit(args, {"team": team_name, "fact": row}, human)


def _list(args: argparse.Namespace) -> int:
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False)
    state = F.load(team)
    facts = state.ordered()
    as_of = F.parse_when(getattr(args, "as_of", None), "--as-of")
    if as_of:
        when = F.epoch(as_of) or 0.0
        facts = [f for f in facts if f.believed_at(when)]
    elif getattr(args, "history", False) or getattr(args, "all", False):
        pass
    else:
        facts = [f for f in facts if f.current]
    about = getattr(args, "about", None)
    if about:
        facts = [f for f in facts if F.normalize(f.about) == F.normalize(about) or F.normalize(about) in F.normalize(f.statement)]
    if getattr(args, "by", None):
        facts = [f for f in facts if args.by in f.members]
    if getattr(args, "disputed", False):
        facts = [f for f in facts if f.status(state.disputes) == "disputed"]
    if getattr(args, "history", False):
        facts = sorted(facts, key=lambda f: (F.normalize(f.about), F.normalize(f.attribute), f.valid_from or f.recorded_at))
    limit = getattr(args, "limit", None)
    if limit:
        facts = facts[-int(limit):]
    rows = [f.to_json(state.disputes) for f in facts]
    payload = {"team": team_name, "facts": rows, "as_of": as_of, "counts": F.counts(team),
               "contradictions": F.contradictions_config(doc)}

    def human() -> str:
        if not rows:
            return "no facts{}; record one with: {} fact add \"<statement>\" --about \"<subject>\" --attribute \"<what>\" --source <url>".format(" match" if about or as_of else " yet", CLI)
        out = []
        for fact in facts:
            status = fact.status(state.disputes)
            span = ""
            if getattr(args, "history", False):
                span = " ({} – {})".format((fact.valid_from or fact.recorded_at)[:10], (fact.valid_to or fact.retired_at or "now")[:10])
            out.append("{:<6} {:<10} {}{}  · {}, {}".format(fact.id, status, F.clip(fact.label(), 100), span, fact.author, fact.confidence()))
        return "\n".join(out)

    return emit(args, payload, human)


def _disputes(args: argparse.Namespace) -> int:
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False)
    state = F.load(team)
    rows = [d for d in sorted(state.disputes.values(), key=lambda d: F._num(d.id)) if args.all or d.open]

    def human() -> str:
        if not rows:
            return "no open disputes (mode: {})".format(F.contradictions_config(doc)["mode"])
        out = []
        for d in rows:
            claims = "; ".join("{} {} ({})".format(f, F.clip(state.facts[f].statement, 60), state.facts[f].author) for f in d.facts if f in state.facts)
            out.append("{} [{}{}{}] {}{}: {}".format(d.id, d.mode, ", escalated to " + str(d.escalated_to) if d.escalated_at else "", ", resolved by " + str(d.resolved_by) if d.resolved_at else "",
                                                    d.about or "?", " · " + d.attribute if d.attribute else "", claims))
        return "\n".join(out)

    return emit(args, {"team": team_name, "disputes": [d.to_json() for d in rows]}, human)


# --------------------------------------------------------------------------
# contradictions


def _run_contradictions(args: argparse.Namespace) -> int:
    layout, _api, author, team_name, team, doc = _open_team(args, require_server=False, write=bool(args.mode or args.timeout))
    current = F.contradictions_config(doc)
    if not args.mode and not args.timeout:
        return emit(args, {"team": team_name, "contradictions": current},
                    "contradictions: {} (debate escalates after {} min). Modes: off, observe, debate, escalate".format(current["mode"], int(current["debate_timeout_ms"] / 60000)))
    from herdr_team.cmd_roster import _human_only

    _human_only(layout, team_name, author, "contradictions")
    check_write_session(args, layout, team_name)
    timeout_ms = current["debate_timeout_ms"]
    if args.timeout:
        from herdr_team.cmd_misc import parse_duration_s

        timeout_ms = parse_duration_s(args.timeout) * 1000
    mode = args.mode or current["mode"]

    def mutate(d: Dict[str, Any]) -> None:
        config = d.setdefault("config", {})
        config["contradictions"] = {"mode": mode, "debate_timeout_ms": timeout_ms}

    update_doc(team, mutate)
    audit(layout, team_name, "contradictions_changed", author, {"mode": mode, "debate_timeout_ms": timeout_ms})
    _announce(layout, team, "contradictions_changed", "contradiction handling is now {} (was {}); agents post as freely as before".format(mode, current["mode"]), ["all"],
              {"mode": mode, "previous": current["mode"]})
    return emit(args, {"team": team_name, "contradictions": {"mode": mode, "debate_timeout_ms": timeout_ms}, "previous": current},
                "contradictions: {} (was {})".format(mode, current["mode"]))


# --------------------------------------------------------------------------
# wiring


def _list_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--about", metavar="SUBJECT", help="facts about this subject (or mentioning it)")
    parser.add_argument("--by", metavar="MEMBER", help="facts this member recorded or supports")
    parser.add_argument("--all", action="store_true", help="include retired and superseded facts")
    parser.add_argument("--history", action="store_true", help="every version, oldest first, with when each was true")
    parser.add_argument("--as-of", dest="as_of", metavar="DATE", help="what the team believed was true on that date")
    parser.add_argument("--disputed", action="store_true", help="only facts in an open dispute")
    parser.add_argument("--limit", type=int, metavar="N")


def _source_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--source", action="append", metavar="URL[@YYYY-MM-DD]", help="an external source and the day it was read (repeatable)")
    parser.add_argument("--post", action="append", metavar="SEQ", help="a board post that supports it (repeatable)")
    parser.add_argument("--ref", action="append", metavar="PATH", help="a file under the team dir or a member cwd (repeatable)")


def _add_fact_arguments(parser: argparse.ArgumentParser) -> None:
    sub = parser.add_subparsers(dest="fact_action", metavar="<action>")
    specs = [
        ("add", "record a fact, optionally about a subject and attribute, with its sources"),
        ("support", "stand behind a fact another member recorded, optionally with more sources"),
        ("retire", "a fact of yours is no longer true (the manager or operator may retire any)"),
        ("show", "one fact: provenance, confidence, validity and disputes"),
        ("list", "current facts (see facts --help for the filters)"),
        ("disputes", "open disputes between members' facts (--all for resolved ones)"),
        ("resolve", "settle a dispute: keep one fact, both, or none (manager or operator)"),
    ]
    parsers: Dict[str, argparse.ArgumentParser] = {}
    for name, help_text in specs:
        p = sub.add_parser(name, help=help_text, description=help_text, allow_abbrev=False)
        add_global_arguments(p, nested=True)
        parsers[name] = p
    p = parsers["add"]
    p.add_argument("statement", help="one sentence, <= {} characters".format(F.MAX_STATEMENT_CHARS))
    p.add_argument("--about", metavar="SUBJECT", help="what it is about: a competitor, a source, a component, a person")
    p.add_argument("--attribute", metavar="WHAT", help="which property of the subject (price, launch date, owner, ...); two members giving different values is a dispute")
    p.add_argument("--type", metavar="TYPE", help="the subject's type, from the team vocabulary when it has one")
    p.add_argument("--valid-from", dest="valid_from", metavar="DATE", help="when it became true (default: now)")
    p.add_argument("--valid-to", dest="valid_to", metavar="DATE", help="when it stopped being true, if known")
    p.add_argument("--supersedes", metavar="F-N", help="this replaces an earlier fact (yours, or any for the manager/operator)")
    _source_arguments(p)
    p = parsers["support"]
    p.add_argument("id", metavar="F-N")
    _source_arguments(p)
    p = parsers["retire"]
    p.add_argument("id", metavar="F-N")
    p.add_argument("reason", nargs="?")
    p.add_argument("--valid-to", dest="valid_to", metavar="DATE", help="when it stopped being true (default: unknown)")
    parsers["show"].add_argument("id", metavar="F-N")
    _list_arguments(parsers["list"])
    parsers["disputes"].add_argument("--all", action="store_true")
    p = parsers["resolve"]
    p.add_argument("id", metavar="D-N")
    choice = p.add_mutually_exclusive_group(required=True)
    choice.add_argument("--keep", action="append", metavar="F-N", help="the fact(s) that stand; the others are retired")
    choice.add_argument("--keep-both", dest="keep_both", action="store_true", help="both stand (they did not really conflict)")
    choice.add_argument("--retire-all", dest="retire_all", action="store_true", help="none stands")
    p.add_argument("--reason", metavar="TEXT")


_ACTIONS = {"add": _add, "support": _support, "retire": _retire, "show": _show, "list": _list, "disputes": _disputes, "resolve": _resolve}


def _run_fact(args: argparse.Namespace) -> int:
    action = getattr(args, "fact_action", None)
    if action is None:
        raise UsageError("fact needs an action: add, support, retire, show, list, disputes, resolve")
    return _ACTIONS[action](args)


def _add_contradictions_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("mode", nargs="?", choices=F.MODES, help="off | observe (default) | debate | escalate")
    parser.add_argument("--timeout", metavar="DURATION", help="debate: escalate after this long unresolved (default 30m)")


COMMANDS: List[Command] = [
    Command("fact", "team facts with time, provenance and disputes: add, support, retire, show, list, disputes, resolve", _add_fact_arguments, _run_fact),
    Command("facts", "the team's current facts (--history, --as-of DATE, --about, --disputed)", _list_arguments, _list),
    Command("contradictions", "how clashing facts are handled: off, observe, debate, escalate (operator sets it)", _add_contradictions_arguments, _run_contradictions),
]
