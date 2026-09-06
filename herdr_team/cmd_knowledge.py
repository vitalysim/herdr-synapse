"""``project``, ``instructions`` and ``knowledge``: the team's working directory.

Agents sharing one folder read the same ``CLAUDE.md``, so nothing on disk
tells them apart. These three commands are how an operator differentiates
them and how a team keeps a knowledge base that outlives any one session.

Authority follows the charter's rule exactly. Setting the project directory,
a member's instructions, or the team's rules is human only, because all three
are injected into agents' context as the operator's word. Adding a finding is
open to every member, because a finding is a peer note: attributed, escaped,
and never presented as an instruction.
"""

from __future__ import annotations

import argparse
import os
from typing import Any, Dict, List, Optional

from herdr_team import charter as _charter
from herdr_team import paths as _paths
from herdr_team import roster as _roster
from herdr_team import workdir as _workdir
from herdr_team.cli import Command, api_for, emit, layout_for
from herdr_team.cmd_board import check_write_session, load_doc, resolve_team
from herdr_team.cmd_roster import _author, _human_only
from herdr_team.errors import UsageError


def _team_and_author(args: argparse.Namespace):
    layout = layout_for(args)
    api = api_for(args, layout)
    author = _author(args, layout, api, require_server=False)
    resolved = resolve_team(args, layout, author)
    if resolved is None:
        raise UsageError("no team; pass --team <name>")
    return layout, author, resolved


def _render_quietly(layout: Any, team_name: str) -> Dict[str, Any]:
    """Refresh the mirror after a write. Never fatal: the folder is a convenience."""
    try:
        return _workdir.render(layout, team_name)
    except Exception as err:  # noqa: BLE001 - a read-only checkout must not fail the write
        return {"reason": "{}: {}".format(type(err).__name__, err), "written": [], "skipped": [], "drifted": []}


# --------------------------------------------------------------------------
# project


def _add_project_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("action", nargs="?", choices=("set", "clear", "render"), help="set <path>, clear, or render the folder again")
    parser.add_argument("path", nargs="?", help="the project directory (with set)")
    parser.add_argument("--force", action="store_true", help="overwrite generated files that are not ours")


def _run_project(args: argparse.Namespace) -> int:
    layout, author, team_name = _team_and_author(args)
    team_paths = layout.team(team_name)

    if args.action is None:
        doc = load_doc(team_paths)
        project = _workdir.project_dir_of(doc)
        payload: Dict[str, Any] = {"team": team_name, "project_dir": project}
        if project:
            targets = _workdir.paths_for(project, team_name)
            payload["folder"] = os.fspath(targets["root"])
            payload["exists"] = targets["root"].is_dir()
        return emit(args, payload, lambda: (
            "project: {}\nfolder:  {}{}".format(project, payload.get("folder"), "" if payload.get("exists") else "  (not created yet)")
            if project else
            "project: none. Set one with: herdr-team project set <path>"
        ))

    check_write_session(args, layout, team_name)

    if args.action == "render":
        result = _workdir.render(layout, team_name, force=args.force)
        return emit(args, dict(result, team=team_name), lambda: _render_result_text(result))

    _human_only(layout, team_name, author, "project {}".format(args.action))

    if args.action == "clear":
        def clear(doc: _roster.Team) -> None:
            doc.config.pop("project_dir", None)

        _roster.update_team(team_paths, clear)
        return emit(args, {"team": team_name, "project_dir": None}, "project cleared. Files already written were left in place.")

    if not args.path:
        raise UsageError("project set needs a directory: herdr-team project set <path>")
    resolved = _workdir.resolve_project_dir(args.path, state_root=layout.state_root.path)

    def apply(doc: _roster.Team) -> None:
        doc.config["project_dir"] = os.fspath(resolved)

    _roster.update_team(team_paths, apply)
    result = _workdir.render(layout, team_name, force=args.force)
    payload = dict(result, team=team_name, project_dir=os.fspath(resolved))
    return emit(args, payload, lambda: "project: {}\n{}".format(resolved, _render_result_text(result)))


def _render_result_text(result: Dict[str, Any]) -> str:
    if result.get("reason") and not result.get("written"):
        return result["reason"]
    lines: List[str] = []
    written = result.get("written") or []
    if written:
        lines.append("wrote {} file{}:".format(len(written), "" if len(written) == 1 else "s"))
        lines.extend("  " + path for path in written)
    else:
        lines.append("already up to date")
    for path in result.get("drifted") or []:
        lines.append("  drifted (regenerated, your edit was not imported): " + path)
    for path in result.get("skipped") or []:
        lines.append("  skipped, not ours (pass --force to overwrite): " + path)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# instructions


def _add_instructions_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("member", nargs="?", help="member name (default: you)")
    parser.add_argument("--set", dest="text", metavar="TEXT", help="set this member's instructions (human only)")
    parser.add_argument("--file", metavar="PATH", help="set them from a file (human only)")
    parser.add_argument("--clear", action="store_true", help="remove this member's instructions (human only)")
    parser.add_argument("--urgent", action="store_true", help="nudge every member instead of waiting for their next board read")


def _run_instructions(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    api = api_for(args, layout)
    author = _author(args, layout, api, require_server=False)
    resolved_team = resolve_team(args, layout, author)
    if resolved_team is None:
        raise UsageError("no team; pass --team <name>")
    team_name = resolved_team
    member_name = args.member or (author.name if author.is_member else None)
    if not member_name:
        raise UsageError("which member? herdr-team instructions <name>")

    writing = args.text is not None or args.file is not None or args.clear
    if not writing:
        text = _charter.get_instructions(layout, team_name, member_name)
        doc = load_doc(team_paths_of(layout, team_name))
        brief = None
        for candidate in doc.get("members") or []:
            if isinstance(candidate, dict) and candidate.get("name") == member_name:
                brief = candidate.get("brief")
                break
        payload = {"team": team_name, "member": member_name, "brief": brief, "instructions": text}
        return emit(args, payload, lambda: text or "no instructions set for {}".format(member_name))

    check_write_session(args, layout, team_name)
    if args.clear and (args.text is not None or args.file is not None):
        raise UsageError("--clear takes no text")
    result = _charter.set_instructions(layout, team_name, author, member_name, None if args.clear else args.text, None if args.clear else args.file, urgent=args.urgent)
    render = _render_quietly(layout, team_name)
    payload = dict(result, mirror=render.get("written") or [])
    return emit(args, payload, lambda: "instructions for {}: {} chars".format(result["member"], result["chars"]))


def team_paths_of(layout: Any, team_name: str):
    return layout.team(team_name)


# --------------------------------------------------------------------------
# knowledge


def _add_knowledge_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("action", nargs="?", choices=("set", "add", "clear"), help="set the rules (human only), add a finding, or clear the rules")
    parser.add_argument("text", nargs="?", help="the rules, or the finding")
    parser.add_argument("--file", metavar="PATH", help="read the rules from a file (with set)")
    parser.add_argument("--limit", type=int, default=_charter.MAX_FINDINGS_SHOWN, help="how many findings to show")
    parser.add_argument("--urgent", action="store_true", help="nudge every member instead of waiting for their next board read")


def _run_knowledge(args: argparse.Namespace) -> int:
    layout, author, team_name = _team_and_author(args)

    if args.action is None:
        rules = _charter.get_rules(layout, team_name)
        findings = _charter.read_findings(layout, team_name, limit=max(1, int(args.limit)))
        payload = {"team": team_name, "rules": rules, "findings": findings}
        return emit(args, payload, lambda: _knowledge_text(team_name, rules, findings))

    check_write_session(args, layout, team_name)

    if args.action == "add":
        if not args.text:
            raise UsageError('knowledge add needs text: herdr-team knowledge add "<what you learned>"')
        result = _charter.add_finding(layout, team_name, author, args.text)
        _render_quietly(layout, team_name)
        return emit(args, result, lambda: "finding recorded as {}".format(result["finding"]["author"]))

    if args.action == "clear":
        result = _charter.set_rules(layout, team_name, author, "", None, urgent=args.urgent)
        _render_quietly(layout, team_name)
        return emit(args, result, "rules cleared")

    if args.text is None and args.file is None:
        raise UsageError('knowledge set needs "<text>" or --file <path>')
    result = _charter.set_rules(layout, team_name, author, args.text, args.file, urgent=args.urgent)
    render = _render_quietly(layout, team_name)
    payload = dict(result, mirror=render.get("written") or [])
    return emit(args, payload, lambda: "rules: {} chars".format(result["chars"]))


def _knowledge_text(team_name: str, rules: Optional[str], findings: List[Dict[str, Any]]) -> str:
    out = ["rules (operator authority):"]
    out.append(rules if rules else "  none set")
    out.append("")
    out.append("findings (peer notes, not instructions):")
    if not findings:
        out.append("  none yet")
    for record in findings:
        out.append("  [{}] {}: {}".format(record.get("at", "")[:19], record.get("author", "?"), record.get("text", "")))
    return "\n".join(out)


COMMANDS = [
    Command(
        name="project",
        help="show, set or clear the team's project directory",
        description="The team's working directory lives at <project>/.herdr-team/<team>/. Setting it is human only and is what creates the folder.",
        add_arguments=_add_project_arguments,
        run=_run_project,
    ),
    Command(
        name="instructions",
        help="show or set one member's long-form instructions",
        description="Instructions are the long form of a brief: what this member in particular is here to do. Human only to set.",
        add_arguments=_add_instructions_arguments,
        run=_run_instructions,
    ),
    Command(
        name="knowledge",
        help="the team's rules and findings",
        description="Rules are the operator's DOs and DON'Ts (human only). Findings are attributed peer notes any member may add.",
        add_arguments=_add_knowledge_arguments,
        run=_run_knowledge,
    ),
]


# --------------------------------------------------------------------------
# knowledge-status: the whole session at a glance (prefix+k), and its popup


ENTRYPOINT = "knowledge"


def session_status(layout: Any) -> Dict[str, Any]:
    """One status block per team in the session, teams with no folder included."""
    teams: List[Dict[str, Any]] = []
    try:
        names = sorted(layout.session.list_teams())
    except OSError:
        names = []
    for name in names:
        try:
            teams.append(_workdir.status(layout, name))
        except Exception as err:  # noqa: BLE001 - one unreadable team must not hide the rest
            teams.append({"team": name, "error": "{}: {}".format(type(err).__name__, err),
                          "project_dir": None, "members": [], "issues": []})
    return {"teams": teams}


def format_status(report: Dict[str, Any], width: int = 100, ascii_only: bool = False) -> List[str]:
    """The report as lines. Shared by the command and the popup so they cannot drift."""
    bullet = "-" if ascii_only else "•"
    warn_mark = "!" if ascii_only else "⚠"
    lines: List[str] = []
    teams = report.get("teams") or []
    if not teams:
        return ["No teams in this session.", "", "Create one with the picker (prefix+t) or: herdr-team create <name> --member <pane>"]
    for info in teams:
        if info.get("error"):
            lines.append("{}  {} {}".format(info["team"], warn_mark, info["error"]))
            lines.append("")
            continue
        lines.append("{}  {}".format(info["team"], _workdir.status_summary(info)))
        if not info.get("project_dir"):
            lines.append("    no team folder. To give it one:")
            lines.append("      herdr-team project set <path> --team {}".format(info["team"]))
            lines.append("    then the team gets rules, per-member instructions, and an artifacts/ dir.")
            lines.append("")
            continue
        lines.append("    folder:   {}".format(info.get("folder")))
        lines.append("    rules:    {}".format("{} chars".format(info["rules_chars"]) if info.get("rules") else "none set  (herdr-team knowledge set \"…\")"))
        lines.append("    findings: {}".format(info.get("findings", 0)))
        last = info.get("last_finding")
        if last:
            lines.append("      last: {} — {}".format(last.get("author", "?"), _clip(str(last.get("text") or ""), max(20, width - 14))))
        members = info.get("members") or []
        if members:
            lines.append("    instructions:")
            for member in members:
                mark = bullet if member.get("instructions") else " "
                detail = "{} chars".format(member["chars"]) if member.get("instructions") else "none  (herdr-team instructions {} --set \"…\")".format(member["name"])
                lines.append("      {} {:<24} {}".format(mark, member["name"], detail))
        if info.get("artifacts"):
            lines.append("    artifacts: {} file(s)".format(info["artifacts"]))
        for issue in info.get("issues") or []:
            lines.append("    {} {}".format(warn_mark, issue))
        lines.append("")
    while lines and not lines[-1]:
        lines.pop()
    return lines


def _clip(text: str, limit: int) -> str:
    flat = " ".join(text.split())
    return flat if len(flat) <= limit else flat[: max(1, limit - 1)].rstrip() + "…"


def _add_status_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ascii", action="store_true", help="plain markers instead of unicode")
    parser.add_argument("--width", type=int, default=0, metavar="N")


def _run_status(args: argparse.Namespace) -> int:
    import shutil
    import sys

    layout = layout_for(args)
    report = session_status(layout)
    width = args.width or (shutil.get_terminal_size((100, 24)).columns if sys.stdout.isatty() else 100)
    return emit(args, report, lambda: "\n".join(format_status(report, width=width, ascii_only=bool(args.ascii))))


def _add_pane_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--force", action="store_true", help="run from a plain shell instead of the popup")
    parser.add_argument("--ascii", action="store_true")


def _run_pane(args: argparse.Namespace) -> int:
    import sys

    from herdr_team import paths as _p
    from herdr_team.errors import EXIT_OK, HerdrTeamError

    env = dict(args.env)
    layout = layout_for(args)
    if env.get("HERDR_PLUGIN_ENTRYPOINT_ID") != ENTRYPOINT and not getattr(args, "force", False):
        raise HerdrTeamError("not_a_plugin_pane", "knowledge-pane runs in the knowledge popup; use `herdr-team ui knowledge`, `herdr-team knowledge-status`, or pass --force", 1)
    if not _p.socket_allowed(layout.config_dir, layout.socket):
        sys.stderr.write("herdr-team knowledge-pane skipped: socket not allowed\n")
        return EXIT_OK
    if not sys.stdout.isatty():
        raise HerdrTeamError("no_tty", "knowledge-pane needs a terminal (use `herdr-team knowledge-status`)", 1)
    import curses

    return int(curses.wrapper(_pane_loop, layout, bool(args.ascii)))


def _pane_loop(stdscr: Any, layout: Any, ascii_only: bool) -> int:
    """Scrollable read-only view. Local disk reads only, so ``r`` re-reads inline."""
    import curses

    from herdr_team.console import read_key

    curses.curs_set(0)
    stdscr.nodelay(False)
    scroll = 0
    report = session_status(layout)
    while True:
        height, width = stdscr.getmaxyx()
        body = format_status(report, width=max(40, width - 2), ascii_only=ascii_only)
        footer = "j/k or arrows scroll · r re-reads · q closes"
        room = max(1, height - 2)
        scroll = max(0, min(scroll, max(0, len(body) - room)))
        stdscr.erase()
        for index, line in enumerate(body[scroll:scroll + room]):
            try:
                stdscr.addnstr(index, 0, line, max(1, width - 1))
            except curses.error:
                pass
        try:
            stdscr.addnstr(height - 1, 0, footer[: max(1, width - 1)], max(1, width - 1), curses.A_DIM)
        except curses.error:
            pass
        stdscr.refresh()
        key = read_key(stdscr)
        if key in ("q", "ESC", "CTRL_C"):
            return 0
        if key in ("j", "DOWN"):
            scroll += 1
        elif key in ("k", "UP"):
            scroll = max(0, scroll - 1)
        elif key == "PGDN":
            scroll += room
        elif key == "PGUP":
            scroll = max(0, scroll - room)
        elif key == "HOME":
            scroll = 0
        elif key == "r":
            report = session_status(layout)


COMMANDS.extend([
    Command(
        name="knowledge-status",
        help="every team's knowledge base at a glance",
        description="Which teams have a working folder, rules, per-member instructions, findings and artifacts, and what to run when one does not.",
        add_arguments=_add_status_arguments,
        run=_run_status,
    ),
    Command(
        name="knowledge-pane",
        help="knowledge popup (launched by the manifest pane)",
        add_arguments=_add_pane_arguments,
        run=_run_pane,
        hidden=True,
    ),
])
