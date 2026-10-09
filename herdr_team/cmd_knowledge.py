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
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import difflib
import os as _os
import sys
import tempfile

from herdr_team import charter as _charter
from herdr_team import instructions_doc as _doc
from herdr_team import paths as _paths
from herdr_team import roster as _roster
from herdr_team import store
from herdr_team import workdir as _workdir
from herdr_team.cli import Command, api_for, emit, layout_for
from herdr_team.cmd_board import check_write_session, load_doc, resolve_team
from herdr_team.cmd_roster import _author, _editor_text, _human_only
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError, UsageError


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
    parser.add_argument("action", nargs="?", choices=("set", "clear", "render", "sync", "confirm"),
                        help="set <path>, clear, render, sync auto|manual, or confirm <id>. Neither mode adopts an edit "
                             "by itself: auto posts each settled edit of the Rules or a member's instructions with its "
                             "exact text, and confirm <id> (the operator) adopts it; manual only holds the file and "
                             "lists it here")
    parser.add_argument("path", nargs="?", help="the project directory (with set), auto|manual (with sync), or the "
                                                "pending change's id (with confirm)")
    parser.add_argument("--force", action="store_true",
                        help="write this team's version over replaceable held durable records (the operator): each is first copied "
                             "into <team>/inherited/ and the copy read back; a file that cannot be copied and checked "
                             "(over 1 MiB, inherited/ not a writable directory, a different file at the copy's name, "
                             "or a file that changed meanwhile) is left exactly as it is and the reason is printed; "
                             "conflicting named canvas assets stay untouched and must be moved aside")


def _run_project(args: argparse.Namespace) -> int:
    layout, author, team_name = _team_and_author(args)
    team_paths = layout.team(team_name)

    if args.action is None:
        doc = load_doc(team_paths)
        project = _workdir.project_dir_of(doc)
        from . import document_sync
        sync_state = {path: {key: value for key, value in entry.items() if key in ("digest", "pending", "since", "error", "hold")} for path, entry in document_sync.entries(document_sync.load(team_paths))}
        payload: Dict[str, Any] = {"team": team_name, "project_dir": project, "sync": "auto" if document_sync.enabled(doc) else "manual", "sync_state": sync_state,
                                   "pending": document_sync.pending_changes(team_paths)}
        if project:
            targets = _workdir.paths_for(project, team_name)
            payload["folder"] = os.fspath(targets["root"])
            payload["exists"] = targets["root"].is_dir()
            # A hold is the quiet half of the never-overwrite rule: the file stays as it is and this team's version of
            # it is not written until the operator resolves it, so this is where it must always be visible.
            payload["held"] = document_sync.held_records(team_paths, targets["root"])
            payload["copies"] = document_sync.copies(team_paths, targets["root"])
        return emit(args, payload, lambda: (
            "project: {}\nfolder:  {}{}\nsync: {}{}{}".format(project, payload.get("folder"), "" if payload.get("exists") else "  (not created yet)",
                                                              payload["sync"], _held_text(payload.get("held") or []),
                                                              _copies_text(payload.get("copies") or []))
            if project else
            "project: none. Set one with: herdr-synapse project set <path>"
        ))

    check_write_session(args, layout, team_name)

    if args.action == "render":
        if args.force:
            _human_only(layout, team_name, author, "project render --force")
        result = _workdir.render(layout, team_name, force=args.force)
        _merge_canvas_snapshot(result, _workdir.render_canvas_snapshot(layout, team_name, force=args.force))
        _merge_board_snapshot(layout, team_name, team_paths, result, args.force)
        _spend_inherited_reports(team_paths, result)
        return emit(args, dict(result, team=team_name), lambda: _render_result_text(result))

    _human_only(layout, team_name, author, "project {}".format(args.action))

    if args.action == "confirm":
        return _confirm_change(args, layout, team_name, author, team_paths)

    if args.action == "sync":
        if args.path not in ("auto", "manual"):
            raise UsageError("project sync needs auto or manual; neither adopts an edit by itself: auto posts each "
                             "settled edit with its exact text for  herdr-synapse project confirm <id>, manual only "
                             "holds it")
        from . import document_sync
        with document_sync.document_lock(team_paths):
            def set_sync(doc: _roster.Team) -> None:
                doc.config["document_sync"] = args.path
            _roster.update_team(team_paths, set_sync)
            _charter.audit(layout, team_name, "project_sync", author, {"mode": args.path})
        synced = _workdir.render(layout, team_name)
        _spend_inherited_reports(team_paths, synced)
        return emit(args, {"team": team_name, "sync": args.path, "inherited": synced.get("inherited") or []},
                    lambda: "\n".join(["document sync: {}{}".format(
                        args.path, " (a settled edit of the Rules or of a member's instructions is posted to you with "
                                   "its exact text and adopted only when you run the  herdr-synapse project confirm  "
                                   "it names; nothing is adopted by itself)"
                        if args.path == "auto" else " (an edit is held and listed by  herdr-synapse project ; adopt "
                                                    "one with  instructions <name> --adopt  or  knowledge import)")]
                        + _inherited_lines(synced.get("inherited") or [])))

    if args.action == "clear":
        def clear(doc: _roster.Team) -> None:
            doc.config.pop("project_dir", None)

        from .document_sync import document_lock
        with document_lock(team_paths):
            _roster.update_team(team_paths, clear)
        return emit(args, {"team": team_name, "project_dir": None}, "project cleared. Files already written were left in place.")

    if not args.path:
        raise UsageError("project set needs a directory: herdr-synapse project set <path>")
    resolved = _workdir.resolve_project_dir(args.path, state_root=layout.state_root.path)
    previous = _workdir.project_dir_of(load_doc(team_paths))

    def apply(doc: _roster.Team) -> None:
        doc.config["project_dir"] = os.fspath(resolved)

    from . import document_sync as _sync
    with _sync.document_lock(team_paths):
        _roster.update_team(team_paths, apply)
    if previous and os.fspath(previous) != os.fspath(resolved):
        # The folder was moved (``mv``) and this points the team at it: what this team recorded about the files it
        # wrote follows them, file by file, where their bytes are still the recorded ones.
        _sync.follow_move(team_paths, _workdir.team_root(previous, team_name),
                          _workdir.team_root(os.fspath(resolved), team_name))
    result = _workdir.render(layout, team_name, force=args.force)
    # A team that has already drawn gets its board mirrored by the same act that creates the folder.
    _merge_canvas_snapshot(result, _workdir.render_canvas_snapshot(layout, team_name, force=args.force))
    # Writing files is not telling anyone. Members that joined before the folder
    # existed had no way to learn about it: nothing announced it and `me` is the
    # only command that prints the path.
    targets = _workdir.paths_for(os.fspath(resolved), team_name)
    _roster.append_system_record(
        team_paths, "project_set",
        "team folder: {} (your own instructions: {}; team rules and findings: {})".format(
            targets["root"], targets["members"] / "<your name>.md", targets["knowledge"]),
        to=["all"], extra={"folder": os.fspath(targets["root"])}, socket=os.fspath(layout.socket),
    )
    payload = dict(result, team=team_name, project_dir=os.fspath(resolved))
    _spend_inherited_reports(team_paths, result)
    return emit(args, payload, lambda: "project: {}\n{}".format(resolved, _render_result_text(result)))


def _confirm_change(args: argparse.Namespace, layout: Any, team_name: str, author: Any, team_paths: Any) -> int:
    """``project confirm <id>``: adopt one pending change, printing the exact text as it is adopted."""
    from . import document_sync

    if not args.path:
        raise UsageError("project confirm needs the id of a pending change; herdr-synapse project lists them")
    adopted = document_sync.confirm(layout, team_name, author, args.path)
    path = adopted["path"]
    render = _render_quietly(layout, team_name)
    _spend_inherited_reports(team_paths, {"inherited": [record for record in render.get("inherited") or []
                                                        if str(record.get("path")) == path]})
    raw = adopted["digest"]  # the digest of the bytes confirmed: the copy the render took of them, if it took one
    payload = dict(adopted, mirror=render.get("written") or [], **_replaced(render, path, raw))

    def text() -> str:
        subject = "{}'s instructions".format(adopted["member"]) if adopted.get("member") else "this team's Rules"
        lines = ["adopted as {} (operator authority), exactly:".format(subject)]
        lines.extend(_quoted_block(adopted.get("text")) if adopted.get("text") else ["           | (empty: cleared)"])
        origin = ("the file's marker names team instance {}, this team".format(adopted["instance"])
                  if adopted.get("instance") else "the file's marker names no team instance")
        lines.append("from {} ({})".format(path, origin))
        if (adopted.get("was") or "") != (adopted.get("proposed_from") or ""):
            lines.append("this replaces text stored after the edit was proposed, which read:")
            lines.extend(_quoted_block(adopted.get("was")) if adopted.get("was") else ["           | (empty)"])
        replaced = _replaced_text(render, path, raw)
        if replaced:
            lines.append(replaced.strip("\n"))
        if adopted.get("warning"):
            lines.append("warning: " + adopted["warning"])
        return "\n".join(lines)
    return emit(args, payload, text)


def _held_text(held: List[Dict[str, Any]]) -> str:
    """Every standing hold, with its reason and the commands that resolve it."""
    if not held:
        return ""
    from . import document_sync as _sync

    lines = ["", "held, left exactly as it is ({}):".format(len(held))]
    for record in held:
        if record.get("stale"):
            lines.append("  {}: it has changed since the last render looked at it;  herdr-synapse project render{}  "
                         "looks again".format(record["path"], _sync.team_flag(record.get("team"))))
            continue
        lines.append("  " + _sync.describe_inherited(record, style="list"))
    return "\n".join(lines)


def _copies_text(copies: List[Dict[str, Any]]) -> str:
    """The copies taken before an operator-ordered overwrite, each re-read: where a replaced file's bytes are now."""
    if not copies:
        return ""
    lines = ["", "replaced after a checked copy ({}):".format(len(copies))]
    for row in copies:
        state = "" if row.get("intact") else "  (that copy no longer holds those bytes: it was changed or removed)"
        lines.append("  {}  ->  {}{}".format(row["path"], row["snapshot"], state))
    return "\n".join(lines)


def _inherited_lines(records: List[Dict[str, Any]]) -> List[str]:
    """One line per hold or copy, for every caller that shows them: one formatter, ``describe_inherited``."""
    from . import document_sync as _sync

    return ["  " + _sync.describe_inherited(record, style="list") for record in records]


def _spend_inherited_reports(team_paths: Any, result: Dict[str, Any]) -> None:
    """Consume ordinary reports shown by this CLI. Pending proposals are owed an operator board notice.

    Plain render is agent-allowed, so showing its caller the edit cannot spend the operator's confirmation notice.
    Only the daemon's successful human-addressed append spends that proposal token.
    """
    from . import document_sync

    document_sync.mark_reported(team_paths, [record for record in result.get("inherited") or []
                                           if record.get("reason") != "proposed"])


def _render_result_text(result: Dict[str, Any]) -> str:
    if result.get("reason") and not (result.get("written") or result.get("holds") or result.get("inherited")):
        return result["reason"]
    lines: List[str] = []
    written = result.get("written") or []
    holds = result.get("holds") or []
    if result.get("reason"):
        lines.append(result["reason"])
    if written:
        lines.append("wrote {} file{}:".format(len(written), "" if len(written) == 1 else "s"))
        lines.extend("  " + path for path in written)
    elif holds:
        lines.append("nothing written; {} file{} held:".format(len(holds), "" if len(holds) == 1 else "s"))
    else:
        lines.append("already up to date")
    held = {str(record.get("path")) for record in (result.get("holds") or [])}
    for path in result.get("skipped") or []:
        if path not in held:
            lines.append("  skipped, not ours (pass --force to overwrite): " + path)
    for path in result.get("sync_pending") or []:
        lines.append("  preserved for document sync (see --json project for status): " + path)
    # Every hold, every time: a held file is not written until the operator resolves it, so a render that names it
    # only once would let the folder freeze silently.
    lines.extend(_inherited_lines([record for record in (result.get("holds") or [])
                                   if str(record.get("path")) not in (result.get("sync_pending") or [])]))
    shown = {str(record.get("path")) for record in (result.get("holds") or [])}
    lines.extend(_inherited_lines([record for record in (result.get("inherited") or [])
                                   if str(record.get("path")) not in shown]))
    for path in result.get("foreign_members") or []:
        # A members/<name>.md for a name this team does not use: the previous team's, almost always.
        lines.append("  a member document for a name this team does not use (not adopted, never written over): " + path)
    if result.get("canvas_reason"):
        lines.append("  canvas mirror not written: " + result["canvas_reason"])
    if result.get("board_reason"):
        lines.append("  board.md not written: " + result["board_reason"])
    return "\n".join(lines)


def _merge_board_snapshot(layout: Any, team_name: str, team_paths: Any, result: Dict[str, Any], force: bool) -> None:
    """``project render --force`` also rewrites ``board.md``, and says so when a file without the marker stops it.

    ``board.md`` is a disposable view (``workdir.write_view``): never held, written over any file carrying the marker
    and never over one without it, so ``--force`` cannot write it over such a file either -- the line it prints is
    the ``mv`` that moves that file aside. A plain render leaves the board to the notifier, which keeps it current.
    """
    if not force or not result.get("project_dir"):
        return
    snapshot = _workdir.render_board_snapshot(layout, team_name)
    if snapshot.get("written") and snapshot.get("path"):
        result.setdefault("written", []).append(snapshot["path"])
    if snapshot.get("foreign"):
        result["board_reason"] = snapshot.get("reason")


def _merge_canvas_snapshot(result: Dict[str, Any], snapshot: Dict[str, Any]) -> Dict[str, Any]:
    """Fold ``render_canvas_snapshot``'s outcome into ``project render``'s own result, holds and reports included."""
    for path in snapshot.get("written") or []:
        result.setdefault("written", []).append(path)
    if snapshot.get("reason"):
        result["canvas_reason"] = snapshot["reason"]
    for key in ("holds", "inherited", "errors"):
        for item in snapshot.get(key) or []:
            result.setdefault(key, []).append(item)
    result["canvas"] = {key: snapshot.get(key) for key in
                        ("elements", "version", "assets", "path", "reason", "too_large",
                         "assets_pruned", "assets_held")}
    return result


# --------------------------------------------------------------------------
# instructions


def _add_instructions_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("member", nargs="?", help="member name (default: you)")
    parser.add_argument("--set", dest="text", metavar="TEXT", help="set this member's instructions (human only)")
    parser.add_argument("--file", metavar="PATH", help="set them from a file (human only)")
    parser.add_argument("--edit", action="store_true", help="open the document in $EDITOR (human only)")
    parser.add_argument("--adopt", action="store_true", help="import the edit made to the file in the project folder (human only)")
    parser.add_argument("--discard", action="store_true", help="restore that file from the authoritative copy (human only)")
    parser.add_argument("--yes", action="store_true", help="do not ask before adopting or discarding")
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
        raise UsageError("which member? herdr-synapse instructions <name>")

    modes = [name for name, on in (("--set", args.text is not None), ("--file", args.file is not None),
                                   ("--edit", args.edit), ("--adopt", args.adopt),
                                   ("--discard", args.discard), ("--clear", args.clear)) if on]
    if len(modes) > 1:
        raise UsageError("pass one of {}, not {}".format(", ".join(modes[:-1]) or "--set", modes[-1]))

    if not modes:
        text = _charter.get_instructions(layout, team_name, member_name)
        doc = load_doc(team_paths_of(layout, team_name))
        brief = None
        for candidate in doc.get("members") or []:
            if isinstance(candidate, dict) and candidate.get("name") == member_name:
                brief = candidate.get("brief")
                break
        payload = {"team": team_name, "member": member_name, "brief": brief, "instructions": text}
        path = _mirror_path(layout, team_name, member_name)
        if path is not None:
            payload["path"] = _os.fspath(path)
        return emit(args, payload, lambda: text or "no instructions set for {}".format(member_name))

    check_write_session(args, layout, team_name)
    if args.discard:
        return _discard_instructions(args, layout, team_name, author, member_name)
    if args.adopt:
        return _adopt_instructions(args, layout, team_name, author, member_name)

    text, file_path = args.text, args.file
    if args.clear:
        text, file_path = None, None
    if args.edit:
        _human_only(layout, team_name, author, "instructions --edit")
        current = _charter.get_instructions(layout, team_name, member_name) or ""
        sections = _doc.parse(current) or _doc.skeleton()
        edited = _editor_text(_doc.document(member_name, team_name, _role_of(layout, team_name, member_name), sections), env_of_args(args))
        text = _doc.to_text(_doc.parse(edited))

    result = _charter.set_instructions(layout, team_name, author, member_name, text, file_path, urgent=args.urgent)
    render = _render_quietly(layout, team_name)
    payload = dict(result, mirror=render.get("written") or [])
    return emit(args, payload, lambda: _written_text(result))


def env_of_args(args: argparse.Namespace) -> Dict[str, str]:
    from herdr_team.cmd_board import env_of

    return env_of(args)


def _written_text(result: Dict[str, Any]) -> str:
    return "instructions for {}: {} chars (revision {})".format(result["member"], result["chars"], result.get("instructions_seq"))


def _role_of(layout: Any, team_name: str, member_name: str) -> str:
    for candidate in load_doc(team_paths_of(layout, team_name)).get("members") or []:
        if isinstance(candidate, dict) and candidate.get("name") == member_name:
            return str(candidate.get("role") or "")
    return ""


def _mirror_path(layout: Any, team_name: str, member_name: str):
    """The member's file in the project folder, or None when the team has no folder."""
    project = _workdir.project_dir_of(load_doc(team_paths_of(layout, team_name)))
    if not project:
        return None
    return _workdir.paths_for(project, team_name)["members"] / (member_name + ".md")


def _read_mirror_bytes(path) -> bytes:
    try:
        return path.read_bytes()
    except (FileNotFoundError, OSError) as err:
        raise HerdrTeamError("path_invalid", "cannot read {}: {}".format(path, err), EXIT_REFUSED, {"path": _os.fspath(path)})


def _confirm(args: argparse.Namespace, question: str) -> bool:
    """Ask before importing or throwing away an edit; ``--yes`` and ``--json`` skip."""
    if args.yes or getattr(args, "json", False):
        return True
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise HerdrTeamError("confirmation_required", "{} (pass --yes)".format(question), EXIT_REFUSED, {"question": question})
    args.stdout.write("{} [y/N] ".format(question))
    args.stdout.flush()
    return sys.stdin.readline().strip().lower() in ("y", "yes")


def _adopt_instructions(args: argparse.Namespace, layout: Any, team_name: str, author: Any, member_name: str) -> int:
    """Import the document in the project folder as this member's instructions, in front of a diff.

    This command is what makes a document the operator's word. The folder is inside a checkout the agents can write
    to, and the plugin cannot tell whose editor saved the file, so nothing there is authoritative until a human runs
    this and sees the diff. It reads the file in place, once; the bytes it read are then *released*, so the render
    after it may write this team's version over them -- after copying them into ``inherited/members/`` and checking
    the copy, or not at all. That copy is what keeps any lines outside the ``##`` sections, which adopting drops.
    """
    from . import document_sync as _sync

    _human_only(layout, team_name, author, "instructions --adopt")
    path = _mirror_path(layout, team_name, member_name)
    if path is None:
        raise HerdrTeamError("no_project_dir", "team {} has no project folder; set one with herdr-synapse project set <path>".format(team_name), EXIT_REFUSED, {"team": team_name})
    if not _workdir.is_ours(path):
        raise HerdrTeamError("workdir_foreign_file", "{} was not written by herdr-synapse; move it aside first".format(path), EXIT_REFUSED, {"path": _os.fspath(path)})
    raw = _read_mirror_bytes(path)
    try:
        body = raw.decode("utf-8")
    except UnicodeDecodeError as err:
        raise HerdrTeamError("workdir_unreadable", "{} is not valid UTF-8 (from byte {}), so it cannot be adopted; fix "
                             "the file, or write this team's version over it with  herdr-synapse project render "
                             "--force --team {}  which copies it into inherited/ first".format(
                                 path, err.start, _sync.quote(team_name)), EXIT_REFUSED,
                             {"path": _os.fspath(path)})
    sections = _doc.parse(body)
    incoming = _doc.to_text(sections) if not _doc.is_empty(sections) else ""
    current = _charter.get_instructions(layout, team_name, member_name) or ""
    team_paths = team_paths_of(layout, team_name)
    if incoming.strip() == current.strip():
        # The substance is already this member's, so there is nothing to adopt -- but the operator has looked at the
        # file, so its bytes are released: a held document no longer waits for an adoption that would change nothing.
        _sync.release(team_paths, path, raw)
        render = _render_quietly(layout, team_name)
        payload = {"team": team_name, "member": member_name, "adopted": False, "reason": "unchanged",
                   "path": _os.fspath(path), "mirror": render.get("written") or [], **_replaced(render, path, raw)}
        return emit(args, payload, "no change in {}{}".format(path, _replaced_text(render, path, raw)))
    diff = list(difflib.unified_diff(current.splitlines(), incoming.splitlines(), "authoritative", "the document",
                                     lineterm="", n=1))
    if not getattr(args, "json", False):
        args.stdout.write("\n".join(diff) + "\n")
    if not _confirm(args, "adopt this document as {}'s instructions?".format(member_name)):
        payload = {"team": team_name, "member": member_name, "adopted": False, "reason": "declined", "path": _os.fspath(path)}
        return emit(args, payload, "not adopted; {} is unchanged".format(path))
    result = _charter.set_instructions(layout, team_name, author, member_name, incoming, None, urgent=args.urgent)
    _workdir.forget_mirror(team_paths, path.name)
    _sync.release(team_paths, path, raw)
    render = _render_quietly(layout, team_name)
    _spend_inherited_reports(team_paths, {"inherited": [record for record in render.get("inherited") or []
                                                        if str(record.get("path")) == _os.fspath(path)]})
    payload = dict(result, adopted=True, diff=diff, mirror=render.get("written") or [], **_replaced(render, path, raw))
    return emit(args, payload, lambda: "adopted. " + _written_text(result) + _replaced_text(render, path, raw))


def _replaced_record(render: Dict[str, Any], path: Any, raw: Any) -> Optional[Dict[str, Any]]:
    """The render's record for the document the command read: its hold, or the checked copy of exactly ``raw``
    (the bytes read, or their digest).

    A copy record is re-emitted until a consumer shows it, so the render can carry an earlier replacement's copy;
    naming that one as the copy of the bytes this command read would be false, so its digest has to match.
    """
    for record in (render.get("holds") or []) + (render.get("inherited") or []):
        if str(record.get("path")) != _os.fspath(path):
            continue
        if record.get("held"):
            return record
        found = raw if isinstance(raw, str) else (_workdir.digest_bytes(raw) if raw is not None else None)
        if found is not None and record.get("token") == "copy:{}".format(found):
            return record
    return None


def _replaced(render: Dict[str, Any], path: Any, raw: Any = None) -> Dict[str, Any]:
    """What the render after an adoption did to the document: the checked copy it took, or the hold that kept it."""
    record = _replaced_record(render, path, raw)
    if record is None:
        return {}
    return {"held": record} if record.get("held") else {"snapshot": record.get("snapshot")}


def _replaced_text(render: Dict[str, Any], path: Any, raw: Any = None) -> str:
    from . import document_sync as _sync

    record = _replaced_record(render, path, raw)
    return "\n" + _sync.describe_inherited(record, style="list") if record is not None else ""


def _discard_instructions(args: argparse.Namespace, layout: Any, team_name: str, author: Any, member_name: str) -> int:
    """Write this team's version of the member document over what is in the folder, after a checked copy of it."""
    from . import document_sync as _sync

    _human_only(layout, team_name, author, "instructions --discard")
    path = _mirror_path(layout, team_name, member_name)
    if path is None:
        raise HerdrTeamError("no_project_dir", "team {} has no project folder".format(team_name), EXIT_REFUSED, {"team": team_name})
    if not _confirm(args, "discard the edit in {}?".format(path)):
        return emit(args, {"team": team_name, "member": member_name, "discarded": False}, "kept")
    team_paths = team_paths_of(layout, team_name)
    _workdir.forget_mirror(team_paths, path.name)
    seen = _sync.look(path)
    if seen.data is not None:
        _sync.release(team_paths, path, seen.data)
    render = _workdir.render(layout, team_name)
    _spend_inherited_reports(team_paths, render)
    replaced = _replaced(render, path, seen.data)
    payload = {"team": team_name, "member": member_name, "discarded": "held" not in replaced, "path": _os.fspath(path),
               "mirror": render.get("written") or [], **replaced}
    return emit(args, payload, "{} {}{}".format("kept" if "held" in replaced else "restored", path,
                                               _replaced_text(render, path, seen.data)))


def team_paths_of(layout: Any, team_name: str):
    return layout.team(team_name)


# --------------------------------------------------------------------------
# knowledge


def _add_knowledge_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("action", nargs="?", choices=("set", "add", "clear", "import"),
                        help="set the rules (human only), add a finding, clear the rules, or import another team's record")
    parser.add_argument("text", nargs="?", help="the rules, the finding, or (with import) the team or folder to inherit from")
    parser.add_argument("--file", metavar="PATH", help="read the rules from a file (with set)")
    parser.add_argument("--limit", type=int, default=_charter.MAX_FINDINGS_SHOWN, help="how many findings to show")
    parser.add_argument("--urgent", action="store_true", help="nudge every member instead of waiting for their next board read")
    add_inherit_arguments(parser)


def add_inherit_arguments(parser: argparse.ArgumentParser, own_source: bool = False) -> None:
    """The flags ``knowledge import`` and ``create --inherit`` share, so the two commands take the same words.

    ``own_source`` is for a command that names the source its own way (``create --inherit``) and already owns
    ``--yes``: it gets the step switches and nothing else, so there is one spelling per idea per command.
    """
    if not own_source:
        parser.add_argument("--from", dest="source", metavar="TEAM|PATH",
                            help="a team dissolved in this session, a team's folder in a project directory, or an archived team directory")
    parser.add_argument("--no-rules", dest="no_rules", action="store_true", help="do not adopt the rules")
    parser.add_argument("--no-facts", dest="no_facts", action="store_true", help="do not adopt the facts")
    parser.add_argument("--no-canvas", dest="no_canvas", action="store_true", help="do not import the canvas")
    parser.add_argument("--keep-authors", dest="keep_authors", action="store_true",
                        help="keep the canvas marks' original author names (the operator in person; they can edit marks or undo the import batch)")
    parser.add_argument("--team-can-edit", dest="team_can_edit", action="store_true",
                        help="let the team work on the inherited board directly (human_edits live); without it their changes to it arrive as proposals")
    parser.add_argument("--skip-unknown", dest="skip_unknown", action="store_true", help="leave out canvas marks this build does not know")
    parser.add_argument("--skip-missing", dest="skip_missing", action="store_true", help="leave out canvas marks whose data file or picture is not here")
    if own_source:
        return
    parser.add_argument("--dry-run", dest="dry_run", action="store_true", help="say what it would adopt and what would refuse it; writes nothing")
    parser.add_argument("--yes", action="store_true",
                        help="adopt the rules without asking (they are printed either way), and replace rules this "
                             "team already has")


def _run_knowledge(args: argparse.Namespace) -> int:
    layout, author, team_name = _team_and_author(args)

    if args.action == "import":
        return _run_knowledge_import(args, layout, author, team_name)

    if args.action is None:
        rules = _charter.get_rules(layout, team_name)
        findings = _charter.read_findings(layout, team_name, limit=max(1, int(args.limit)))
        payload = {"team": team_name, "rules": rules, "findings": findings}
        return emit(args, payload, lambda: _knowledge_text(team_name, rules, findings))

    check_write_session(args, layout, team_name)

    if args.action == "add":
        if not args.text:
            raise UsageError('knowledge add needs text: herdr-synapse knowledge add "<what you learned>"')
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


# --------------------------------------------------------------------------
# knowledge import: the one explicit act that makes a previous team's record this team's


def _resolve_source(layout: Any, raw: Any) -> Dict[str, Any]:
    """``--from``: a dissolved team's name (``name`` or ``name@stamp``) first, then a path. The first match wins.

    A name is tried before a path because that is what the dissolve message prints, and because a path that happens to
    look like a team name is not a thing that exists.
    """
    from herdr_team import canvas_import as IM

    text = str(raw or "").strip()
    if not text:
        raise UsageError("knowledge import needs --from <team|path>")
    name = text.partition("@")[0].strip()
    try:
        valid_name = _paths.validate_team_name(name) == name
    except HerdrTeamError:
        valid_name = False
    if valid_name and IM.dissolved(layout.session, name):
        entry = IM.resolve_archive(layout.session, text)
        return {"kind": "archive", "team": name, "stamp": entry["stamp"], "dir": _os.fspath(entry["path"]),
                "paths": IM.archive_paths(entry, name)}
    path = Path(_os.path.expanduser(text))
    if path.is_file() and path.name in ("knowledge.md", "canvas.json", "facts.md", "team.json"):
        # One of the folder's own files, named instead of the folder. Reports and messages print paths to these, and
        # the operator pasting one back is right about where they want to inherit from.
        path = path.parent
    if path.is_dir():
        if (path / "team.json").is_file():
            team_name = path.name.rsplit("-", 1)[0] if _re_stamp(path.name) else path.name
            return {"kind": "team_dir", "team": team_name, "stamp": _re_stamp(path.name), "dir": _os.fspath(path),
                    "paths": _paths.TeamPaths(path, team_name)}
        if (path / "knowledge.md").is_file() or (path / "canvas.json").is_file() or (path / "members").is_dir():
            return {"kind": "knowledge_path", "team": path.name, "stamp": None, "dir": _os.fspath(path), "paths": None}
    raise HerdrTeamError("path_invalid", "cannot inherit from {!r}: --from takes a team dissolved in this session (see "
                                        "herdr-synapse canvas import --from-archive <team> --list), a team's folder in a "
                                        "project directory (the one holding knowledge.md and canvas.json), or an archived "
                                        "team directory (the one holding team.json)".format(text),
                         EXIT_REFUSED, {"from": text})


def _re_stamp(name: str) -> Optional[str]:
    """The archive stamp a directory name ends in (``beta-20261001T120000Z``), or None for a plain team folder."""
    from herdr_team import canvas_import as IM

    tail = name.rsplit("-", 1)[-1]
    return tail if IM.STAMP_RE.match(tail) else None


def _own_root(team_paths: Any, team_name: Optional[str]) -> Optional[Path]:
    """This team's folder as the guard names it (``paths_for`` of the stored project directory), or None."""
    if team_paths is None or not team_name:
        return None
    try:
        project = _workdir.project_dir_of(load_doc(team_paths))
        return _workdir.paths_for(project, team_name)["root"] if project else None
    except (HerdrTeamError, OSError, ValueError, AttributeError, TypeError):
        return None


def _own_folder(source: Dict[str, Any], team_paths: Any, team_name: Optional[str]) -> bool:
    """Is the source this team's own folder in its project directory? Then what a step adopts from it is released."""
    if source.get("kind") != "knowledge_path":
        return False
    own = _own_root(team_paths, team_name)
    return own is not None and _os.path.realpath(_os.fspath(own)) == _os.path.realpath(str(source["dir"]))


def _read_source_file(source: Dict[str, Any], name: str) -> str:
    """One file of a knowledge-path source, read once; the digest of the bytes read is kept for ``release``."""
    path = Path(source["dir"]) / name
    data = path.read_bytes()
    source.setdefault("read", {})[_os.fspath(path)] = _workdir.digest_bytes(data)
    return data.decode("utf-8")


def _source_rules(source: Dict[str, Any]) -> Optional[str]:
    """The source's rules: an archived team's ``rules.md``, or the Rules section of a knowledge path's ``knowledge.md``."""
    if "rules_text" in source:
        # Read once: the text the operator is shown before confirming is exactly the text that is adopted.
        return source["rules_text"]
    source["rules_text"] = _read_source_rules(source)
    return source["rules_text"]


def _read_source_rules(source: Dict[str, Any]) -> Optional[str]:
    from . import document_sync

    paths = source.get("paths")
    if paths is not None:
        for candidate in (paths.rules_md, paths.knowledge_md):
            try:
                text = candidate.read_text(encoding="utf-8").strip()
            except (OSError, ValueError):
                continue
            if text:
                return text
        return None
    try:
        body = _read_source_file(source, "knowledge.md")
    except (OSError, ValueError):
        return None
    try:
        rules, _findings = document_sync.knowledge_parts(body)
    except ValueError:
        return None
    return rules.strip() or None


def _source_facts(source: Dict[str, Any], team_paths: Any = None) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """``(rows, why there are none)``. Current facts only: a retired or superseded fact is not what the team believed.

    An archived team's own record (``facts.jsonl``) when there is one, else the ``facts.md`` the project mirror wrote,
    read back through ``facts.parse_mirror``. Reading the mirror is legal here and only here: the mirror's first rule
    says a mirrored file is never truth until a human adopts it, and this command *is* that human act.
    """
    from herdr_team import facts as _facts

    paths = source.get("paths")
    if paths is not None:
        try:
            state = _facts.load(paths)
        except (HerdrTeamError, OSError, ValueError, TypeError):
            return [], "its facts could not be read"
        return [fact.to_json(state.disputes) for fact in state.current()], None
    try:
        body = _read_source_file(source, "facts.md")
    except (OSError, ValueError):
        return [], ("that folder carries no readable facts.md; if the team that wrote it was dissolved in this "
                    "session, its facts are in the session archive:  herdr-synapse knowledge import --from <that "
                    "team's name>")
    rows = _facts.parse_mirror(body)
    source.pop("facts_missing", None)
    declared = _workdir.facts_parts_declared(body)
    if declared is None:
        return rows, None
    # A record past ``MAX_FACTS_BYTES`` is split into numbered parts, and part 1 says how many there are and how many
    # facts they hold. Every part is read; one that is missing or unreadable is named, with the true count, rather than
    # the short count being reported as the whole record.
    parts, total = declared
    missing: List[str] = []
    for number in range(2, parts + 1):
        name = _workdir.facts_part_name(number)
        try:
            rows.extend(_facts.parse_mirror(_read_source_file(source, name)))
        except (OSError, ValueError):
            missing.append(name)
    if missing or len(rows) < total:
        source["facts_missing"] = {"parts": missing, "declared": total, "read": len(rows)}
    return rows, None


def _release_read(team_paths: Any, source: Dict[str, Any], names: Any, own_root: Optional[Path]) -> None:
    """Release the source files a step adopted from, when the source is this team's own folder: exactly the bytes read.

    Under the guard's own name for each file (``own_root``, from the stored project directory), not the path as the
    operator typed it: ``--from .herdr-synapse/alpha`` or a path through a symlink names the same file by another
    string, and a release under that string would never match, leaving the file held by the command that resolves it.
    """
    from . import document_sync

    if own_root is None:
        return
    for path, found in sorted((source.get("read") or {}).items()):
        name = Path(path).name
        if name in names or (name.startswith("facts-") and "facts.md" in names):
            document_sync.release(team_paths, Path(own_root) / name, found)


def _source_project_dir(source: Dict[str, Any]) -> Optional[str]:
    """The project directory the source team used, for its member documents."""
    if source["kind"] == "knowledge_path":
        return None
    paths = source.get("paths")
    try:
        return _workdir.project_dir_of(store.read_json(paths.team_json, default={}) or {})
    except (OSError, ValueError, AttributeError):
        return None


def _source_members(source: Dict[str, Any], active: List[str], team_paths: Any = None,
                    team_name: Optional[str] = None) -> List[Dict[str, Any]]:
    """The member documents in the source that belong to a name this team uses: offered, never adopted (see §4.5).

    Adopting one automatically is the bug this round closes, not a feature: a file in a checkout the agents can write
    to would become this team's operator authority with no human in front of a diff.

    ``regenerated`` marks a document whose bytes are the ones *this* team last wrote there (its recorded digest, under
    the guard's own key for the file however ``--from`` spelled the folder, and not a mere claim), so the operator is
    not handed their own regenerated document as "the previous team's" and told to review it.
    """
    if source["kind"] == "knowledge_path":
        folder = Path(source["dir"]) / "members"
    else:
        project = _source_project_dir(source)
        if not project:
            return []
        folder = _workdir.paths_for(project, source["team"])["members"]
    out: List[Dict[str, Any]] = []
    try:
        names = sorted(entry.name for entry in _os.scandir(folder) if entry.is_file() and entry.name.endswith(".md"))
    except OSError:
        return []
    from . import document_sync as _sync

    written = _sync.load(team_paths) if team_paths is not None else {}
    own = None
    if team_paths is not None and team_name:
        try:
            own_project = _workdir.project_dir_of(load_doc(team_paths))
            own = _workdir.paths_for(own_project, team_name)["members"] if own_project else None
        except (HerdrTeamError, OSError, ValueError, AttributeError, TypeError):
            own = None
    same = own is not None and _os.path.realpath(_os.fspath(folder)) == _os.path.realpath(_os.fspath(own))
    for name in names:
        stem = name[:-3]
        row = {"name": stem, "path": _os.fspath(folder / name), "mine": stem in active}
        # Offered with ``--adopt`` only when ``--adopt`` can take it as it stands. ``--adopt`` reads this team's own
        # ``members/<name>.md``, so a document in another folder is not what it would adopt; and it refuses a
        # markerless or undecodable one, so offering it then printed a repair that exits 1 (X5).
        row["adoptable"] = bool(same and _sync._adoptable_now(folder / name))
        # The guard's own key for the file, however the operator spelled the folder.
        entry = written.get(_os.fspath((own if same else folder) / name)) or {}
        recorded = entry.get("digest") if not entry.get("claimed") else None
        if recorded:
            seen = _sync.look(folder / name)
            row["regenerated"] = seen.data is not None and _workdir.digest_bytes(seen.data) == recorded
        out.append(row)
    return out


def inherit(args: argparse.Namespace, layout: Any, team_name: str, author: Any, source: Dict[str, Any],
            options: Dict[str, Any]) -> Dict[str, Any]:
    """Adopt a previous team's rules, facts and canvas into ``team_name``. Each step is independent.

    Cheapest first, and a step's failure never undoes an earlier one (the same per-file isolation
    ``workdir._render_locked`` uses), so a refusal late leaves the most value behind. Nothing in the source is changed
    or deleted: every read is a read.
    """
    from herdr_team import canvas as C
    from herdr_team import canvas_import as IM
    from herdr_team import facts as _facts

    team_paths = layout.team(team_name)
    doc = load_doc(team_paths)
    result: Dict[str, Any] = {"team": team_name, "from": {k: v for k, v in source.items() if k != "paths"},
                              "rules": None, "facts": None, "canvas": None, "members": []}
    reference = "{}@{}".format(source["team"], source["stamp"]) if source.get("stamp") else source["dir"]
    # Inheriting from this team's own folder adopts files the mirror is holding: exactly the bytes each step read are
    # released, so the next render may write this team's version over them -- after a checked copy, or not at all.
    own = _own_folder(source, team_paths, team_name) and not options.get("dry_run")
    own_root = _own_root(team_paths, team_name) if own else None
    result["own_folder"] = bool(own)

    # 1. the rules: the operator's own words, in an operator-authority section.
    if options.get("rules"):
        incoming = _source_rules(source)
        current = _charter.get_rules(layout, team_name) or ""
        if not incoming:
            result["rules"] = {"adopted": False, "reason": "that folder carries no rules"}
        elif options.get("dry_run"):
            blocked = bool(current.strip()) and not options.get("yes")
            result["rules"] = {"adopted": False, "dry_run": True, "would_adopt": not blocked, "chars": len(incoming),
                               "text": incoming, "current": current.strip() or None,
                               "reason": "team {} already has rules; --yes would replace them".format(team_name) if blocked else None}
        elif current.strip() and not options.get("yes"):
            result["rules"] = {"adopted": False, "code": "rules_present", "text": incoming,
                              "reason": "team {} already has rules ({}...); pass --yes to replace them, or set them by "
                                        "hand with  herdr-synapse knowledge set. The rules in {} begin: {}...".format(
                                            team_name, _one_line(current, 60), reference, _one_line(incoming, 60))}
        else:
            try:
                written = _charter.set_rules(layout, team_name, author, incoming, None)
                result["rules"] = {"adopted": True, "chars": written["chars"], "text": incoming}
                if own:
                    _release_read(team_paths, source, ("knowledge.md",), own_root)
            except HerdrTeamError as err:
                result["rules"] = {"adopted": False, "code": err.code, "reason": err.message}

    # 2. the facts: what the previous team believed, attributed to it. A live member's ``fact support`` is what makes
    #    one this team's own, which is why they do not arrive as this team's verified facts.
    if options.get("facts"):
        rows, why = _source_facts(source, team_paths)
        if why:
            result["facts"] = {"added": [], "reason": why}
        elif not rows:
            result["facts"] = {"added": [], "reason": "that team recorded no current facts"}
        elif options.get("dry_run"):
            # The same filter the real run applies (``facts.plan_inherited``), so the count is the one it would add.
            plan = _facts.plan_inherited(team_paths, rows)
            result["facts"] = {"added": [], "dry_run": True, "would_add": plan["new"], "already_held": plan["held"],
                               "unusable": plan["unusable"], "of": len(rows), "by": source["team"]}
        else:
            try:
                added = _facts.add_inherited(team_paths, rows, source["team"], reference)
                result["facts"] = {"added": list(added), "of": len(rows), "by": source["team"]}
                if not added:
                    result["facts"]["reason"] = ("every current fact there is one this team already holds (the same "
                                                 "statement about the same subject and attribute)")
                if own:
                    _release_read(team_paths, source, ("facts.md",), own_root)
            except HerdrTeamError as err:
                result["facts"] = {"added": [], "code": err.code, "reason": err.message}
        missing = source.get("facts_missing")
        if isinstance(missing, dict) and isinstance(result.get("facts"), dict):
            # The record says how many facts it holds, so a short read is reported as short, with what is missing.
            result["facts"]["declared"] = missing.get("declared")
            result["facts"]["missing"] = (
                "the folder's facts.md says the record holds {} facts in numbered parts and {} could be read{}".format(
                    missing.get("declared"), missing.get("read"),
                    "; missing or unreadable: " + ", ".join(missing.get("parts") or []) if missing.get("parts") else ""))

    # 3. the canvas: the ``import`` core op, so it lands in the log, folds into the scene, reaches the page and is undoable.
    if options.get("canvas"):
        result["artifacts"] = _inherit_artifacts(layout, team_paths, doc, source, dry_run=bool(options.get("dry_run")))
        op: Dict[str, Any] = {"op": "import", "from": source["dir"], "intent": "inherit the canvas from {}".format(reference)}
        if source.get("stamp"):
            op["stamp"] = source["stamp"]
        for key in ("keep_authors", "skip_unknown", "skip_missing", "team_can_edit"):
            if options.get(key):
                op[key] = True
        try:
            if options.get("dry_run"):
                from herdr_team import features as _features
                from herdr_team.cmd_canvas import dry_run

                # The real run refuses a team whose whiteboard is off before anything else; so does the dry run, or
                # the two would name different refusals for one canvas.
                _features.require_on(layout.session, team_paths, doc)
                report = dry_run(layout, team_paths, doc, options["canvas_author"], op)
                result["canvas"] = dict(report.to_json(), dry_run=True)
            else:
                applied = C.apply_ops(layout, team_paths, [op], options["canvas_author"], doc=doc)
                refused = (applied.get("refused") or [None])[0]
                if refused:
                    result["canvas"] = {"imported": False, "code": refused["code"], "reason": refused["message"]}
                else:
                    entry = applied["applied"][0]
                    result["canvas"] = dict(entry.get("import") or {}, imported=True, version=applied["version"])
                    if own:
                        from herdr_team import document_sync as _sync

                        _sync.release_import(team_paths, _workdir.paths_for(_workdir.project_dir_of(doc), team_name),
                                             source["dir"], result["canvas"].get("digest"))
        except HerdrTeamError as err:
            hint = ""
            if err.code == "whiteboard_off":
                hint = "; then run knowledge import again for the canvas"
            result["canvas"] = {"imported": False, "code": err.code, "reason": err.message + hint}

    # 4. the member documents: listed, with the one command that adopts one in front of a diff.
    active = [m.get("name") for m in doc.get("members") or [] if isinstance(m, dict) and m.get("status") != "left" and m.get("name")]
    result["members"] = _source_members(source, active, team_paths, team_name)

    if not options.get("dry_run"):
        _record_inheritance(layout, team_paths, team_name, source, reference, result)
    return result


def _inherit_artifacts(layout: Any, team_paths: Any, doc: Dict[str, Any], source: Dict[str, Any],
                       dry_run: bool = False) -> Dict[str, Any]:
    """Bring the data files the source board reads into this team's ``artifacts/``, before the canvas is imported.

    This is the step whose absence made inheritance work for a team of the same name and fail for every other one. A
    chart's ``data_path`` is resolved against the *importing* team's artifacts folder, so ``alpha``'s board imported
    into ``beta`` was refused (e) for a CSV sitting in ``alpha/artifacts/`` next door, and the whole canvas step came
    back with zero marks. A team recreated under the same name shares that folder with the team that died, which is
    the only reason the owner's own round trip passed.

    Best effort and independent of the canvas step: what it cannot bring is named, and refusal (e) still has the last
    word on whether the board can be imported.
    """
    from herdr_team import canvas as C
    from herdr_team import canvas_import as IM

    try:
        document = IM.read_document(source["dir"], stamp=source.get("stamp"))
    except HerdrTeamError as err:
        return {"copied": [], "omitted": [], "reason": err.message}
    try:
        target = C.artifacts_dir(layout, team_paths, doc)
    except (HerdrTeamError, OSError, ValueError):
        target = None
    if dry_run:
        wanted = sorted({rel for el in document.scene.get("elements") or [] if isinstance(el, dict)
                         for _field, rel in IM.artifact_refs(el)})
        return {"copied": [], "omitted": [], "dry_run": True, "would_copy": wanted,
                "from": _os.fspath(document.artifacts_source) if document.artifacts_source else None,
                "dir": _os.fspath(target) if target else None}
    return IM.copy_artifacts(document, target)


def _record_inheritance(layout: Any, team_paths: Any, team_name: str, source: Dict[str, Any], reference: str,
                        result: Dict[str, Any]) -> None:
    """Two board records, so the team learns what it is looking at is not its own work.

    Provenance is the whole mitigation for adopting another team's rules at all: without a record on the board, the
    fix would trade a loud failure (the rules never arrive) for a quiet one (they arrive as if this team wrote them).
    """
    # ``system_record`` flattens ``extra`` onto the record, so these keys are namespaced: a bare ``from`` would
    # overwrite the record's own author and the board would refuse the whole record.
    canvas = result.get("canvas") or {}
    provenance = {"inherited_from": reference, "inherited_team": source["team"], "inherited_kind": source["kind"]}
    try:
        if canvas.get("imported"):
            _roster.append_system_record(
                team_paths, "canvas_imported",
                "the canvas was inherited from {}: {} marks as {} (they are not this team's own work; "
                "herdr-synapse canvas undo {} takes it back)".format(reference, canvas.get("elements"), canvas.get("batch"),
                                                                     canvas.get("batch")),
                to=["all"], extra=dict(provenance, inherited_batch=canvas.get("batch")), socket=_os.fspath(layout.socket))
        _roster.append_system_record(
            team_paths, "knowledge_imported", _inherit_text(result, short=True), to=["human"],
            extra=dict(provenance), socket=_os.fspath(layout.socket))
    except (HerdrTeamError, OSError, ValueError) as err:
        # The board record is the provenance, so a failure to write it is reported rather than swallowed.
        result["records_error"] = "{}: {}".format(getattr(err, "code", type(err).__name__), getattr(err, "message", err))


def _sync_quote(word: Any) -> str:
    from . import document_sync as _sync

    return _sync.quote(str(word))


def _team_flag(team: Any) -> str:
    from . import document_sync as _sync

    return _sync.team_flag(str(team) if team else None)


def _quoted_block(text: Any) -> List[str]:
    """``text`` as an indented block, one ``|`` line per line, so it cannot be mistaken for this command's own output."""
    return ["           | " + line for line in str(text or "").splitlines()] or ["           |"]


def _one_line(text: Any, limit: int) -> str:
    body = " ".join(str(text or "").split())
    return body[:limit]


def _inherit_text(result: Dict[str, Any], short: bool = False) -> str:
    """What the operator reads: one line per step, a refusal's code and message in place of its line."""
    source = result.get("from") or {}
    where = "{} (dissolved {})".format(source.get("team"), source.get("stamp")) if source.get("stamp") else source.get("team")
    lines = ["inherited from {}, {}:".format(where, source.get("dir"))]
    rules = result.get("rules")
    if isinstance(rules, dict):
        shown = not short and bool(rules.get("text")) and bool(rules.get("dry_run") or rules.get("adopted"))
        colon = ":" if shown else ""
        if rules.get("dry_run"):
            lines.append("  rules    " + ("{} chars would be adopted as this team's operator Rules{}".format(
                rules.get("chars"), colon) if rules.get("would_adopt")
                else "would not be adopted ({}){}".format(rules.get("reason"), "; they read:" if shown else "")))
        else:
            lines.append("  rules    " + ("adopted as this team's operator Rules, {} chars{}".format(rules.get("chars"), colon)
                                          if rules.get("adopted")
                                          else "not adopted ({}{})".format(rules.get("code") + ": " if rules.get("code") else "", rules.get("reason"))))
        if shown:
            # The text itself, every time: this command is the one door through which a file found in the folder
            # becomes operator authority, and nobody should walk through it without reading what comes in.
            lines.extend(_quoted_block(rules["text"]))
    facts = result.get("facts")
    if isinstance(facts, dict):
        if facts.get("dry_run"):
            held = facts.get("already_held") or 0
            lines.append("  facts    {} of {} current facts would be attributed to {}{}".format(
                facts.get("would_add"), facts.get("of"), facts.get("by"),
                "; {} duplicate or already-held rows would not be added".format(held) if held else ""))
        elif facts.get("added"):
            lines.append("  facts    {} of {} current facts, attributed to {}; to make one yours, "
                         "support it:  herdr-synapse fact support {}".format(len(facts["added"]), facts.get("of"), facts.get("by"),
                                                                 facts["added"][0]))
        else:
            lines.append("  facts    none added ({}{})".format(facts.get("code") + ": " if facts.get("code") else "",
                                                          facts.get("reason") or "nothing there could be added"))
        if facts.get("missing"):
            lines.append("           short: {}".format(facts["missing"]))
    canvas = result.get("canvas")
    if isinstance(canvas, dict):
        if canvas.get("imported"):
            # The skipped ids belong here and were missing: --skip-missing / --skip-unknown made this line read as a
            # plain success while marks were dropped, and ``canvas import``'s own text did report them. Two surfaces,
            # one import, one answer.
            left = "".join("; {} left out ({})".format(", ".join(ids), reason)
                           for reason, ids in sorted((canvas.get("skipped") or {}).items()) if ids)
            lines.append("  canvas   {} marks, {} comments, {} pictures, as {} (undo {} to take it back){}".format(
                canvas.get("elements"), canvas.get("comments"), canvas.get("assets"), canvas.get("batch"),
                canvas.get("batch"), left))
            if canvas.get("authors") != "keep":
                from herdr_team import canvas_import as IM

                lines.append("           " + IM.team_edit_line(bool(canvas.get("team_can_edit"))))
        elif canvas.get("dry_run"):
            lines.append("  canvas   {} marks would come in; {} would refuse it".format(
                canvas.get("elements"), len(canvas.get("refusals") or []) or "nothing"))
        else:
            lines.append("  canvas   not imported ({}: {})".format(canvas.get("code"), canvas.get("reason")))
    artifacts = result.get("artifacts")
    if isinstance(artifacts, dict) and (artifacts.get("copied") or artifacts.get("omitted") or artifacts.get("would_copy")):
        if artifacts.get("dry_run"):
            lines.append("  data     {} would be brought into this team's artifacts/".format(", ".join(artifacts["would_copy"])))
        else:
            parts = []
            if artifacts.get("copied"):
                parts.append("{} copied into this team's artifacts/".format(", ".join(artifacts["copied"])))
            for row in artifacts.get("omitted") or []:
                parts.append("{} not copied ({})".format(row.get("path"), row.get("why")))
            lines.append("  data     " + "; ".join(parts))
    # A document whose bytes are the ones this team last wrote there is this team's own regenerated placeholder, not
    # the previous team's record, so offering it with ``--adopt`` sends the operator to a diff with nothing in it.
    candidates = [row for row in result.get("members") or [] if row.get("mine") and not row.get("regenerated")]
    members = [row for row in candidates if row.get("adoptable", True)]
    if members:
        # The exact command per member, never ``<name>``: a placeholder is a repair nobody can run as printed.
        lines.append("  members  {} {} in that folder but {} not adopted: review {} with  {}".format(
            ", ".join(row["name"] + ".md" for row in members), "is" if len(members) == 1 else "are",
            "was" if len(members) == 1 else "were", "it" if len(members) == 1 else "each",
            " ,  ".join("herdr-synapse instructions {} --adopt{}".format(
                _sync_quote(row["name"]), _team_flag(result.get("team"))) for row in members)))
    unreadable = [row for row in candidates if not row.get("adoptable", True)]
    if unreadable:
        # ``--adopt`` cannot take these: it reads only this team's own folder's document, and refuses one without this
        # plugin's marker or not in UTF-8. So it is not offered; the file is the record, and it is read where it is.
        lines.append("  members  {} in that folder {} not adopted, and instructions --adopt cannot take {} (it reads "
                     "only this team's own folder, and only a document with this plugin's marker, in UTF-8 and within the "
                     "instructions limit): read {} "
                     "where {}".format(", ".join(row["name"] + ".md" for row in unreadable),
                                       "was" if len(unreadable) == 1 else "were",
                                       "it" if len(unreadable) == 1 else "them",
                                       "it" if len(unreadable) == 1 else "them",
                                       "it is" if len(unreadable) == 1 else "they are"))
    regenerated = [row for row in result.get("members") or [] if row.get("mine") and row.get("regenerated")]
    if regenerated:
        lines.append("  members  {} in that folder {} exactly what this team last wrote there, so there is "
                     "nothing of another team's in it to adopt".format(
                         ", ".join(row["name"] + ".md" for row in regenerated),
                         "is" if len(regenerated) == 1 else "are"))
    if result.get("own_folder"):
        # The source is this team's own folder, so the next render may replace what this import read and adopted --
        # after a checked copy into inherited/ -- and saying "nothing was changed" there would be false a line later.
        lines.append("the files this import adopted from are this team's now: the render after it replaces each one "
                     "only after a checked copy into inherited/, and only if it still holds what was read; nothing "
                     "was deleted.")
    else:
        lines.append("nothing in {} was changed or deleted.".format(source.get("team") or "the source"))
    if short:
        return " ".join(line.strip() for line in lines)
    return "\n".join(lines)


def inherit_text(result: Dict[str, Any]) -> str:
    """``knowledge import``'s human text (also printed under ``create --inherit``'s summary)."""
    return _inherit_text(result)


def inherit_options(args: argparse.Namespace, canvas_author: Any) -> Dict[str, Any]:
    """The flags ``knowledge import`` and ``create --inherit`` share."""
    return {"rules": not getattr(args, "no_rules", False), "facts": not getattr(args, "no_facts", False),
            "canvas": not getattr(args, "no_canvas", False), "keep_authors": bool(getattr(args, "keep_authors", False)),
            "skip_unknown": bool(getattr(args, "skip_unknown", False)), "skip_missing": bool(getattr(args, "skip_missing", False)),
            "team_can_edit": bool(getattr(args, "team_can_edit", False)),
            "dry_run": bool(getattr(args, "dry_run", False)), "yes": bool(getattr(args, "yes", False)),
            "canvas_author": canvas_author}


def _run_knowledge_import(args: argparse.Namespace, layout: Any, author: Any, team_name: str) -> int:
    from herdr_team import canvas as C

    _human_only(layout, team_name, author, "knowledge import")
    check_write_session(args, layout, team_name)
    team_paths = layout.team(team_name)
    doc = load_doc(team_paths)
    source = _resolve_source(layout, getattr(args, "source", None) or args.text)
    canvas_author = C.author_from_identity(author, doc, via="cli")
    options = inherit_options(args, canvas_author)
    if not _confirm_rules(args, layout, team_name, source, options):
        return emit(args, {"team": team_name, "imported": False, "reason": "declined"},
                    "not imported; nothing was changed")
    result = inherit(args, layout, team_name, author, source, options)
    render = _render_quietly(layout, team_name)
    # The render after an import from this team's own folder may replace the files the import read, after a checked
    # copy; what it did to each is said here, where the operator is looking.
    result["folder"] = render.get("inherited") or []
    _spend_inherited_reports(team_paths, render)
    steps = [result.get("rules"), result.get("facts"), result.get("canvas")]
    refused = [step for step in steps if isinstance(step, dict) and step.get("code")]
    asked = [step for step in steps if isinstance(step, dict)]
    code = emit(args, result, lambda: "\n".join([inherit_text(result)] + _inherited_lines(result["folder"])))
    if asked and len(refused) == len(asked):
        return EXIT_REFUSED
    return code


def _confirm_rules(args: argparse.Namespace, layout: Any, team_name: str, source: Dict[str, Any],
                   options: Dict[str, Any]) -> bool:
    """Show the Rules an import would adopt, and ask, before anything is adopted. False when the operator declines.

    The same footing ``instructions --adopt`` gives a member document, for text with more authority: the folder is
    writable by the agents, so what its ``knowledge.md`` says becomes the operator's word only in front of the
    operator. ``--yes`` and ``--json`` skip the question (the text is in the output either way); without a terminal
    and without ``--yes`` the command refuses (``confirmation_required``) and changes nothing. Asked only when the
    rules would actually be adopted: a team that already has rules refuses them without ``--yes`` anyway.
    """
    if not options.get("rules") or options.get("dry_run") or options.get("yes") or getattr(args, "json", False):
        return True
    incoming = _source_rules(source)
    if not incoming or (_charter.get_rules(layout, team_name) or "").strip():
        return True
    args.stdout.write("the rules in {} read:\n{}\n".format(source.get("dir"), "\n".join(_quoted_block(incoming))))
    return _confirm(args, "adopt these as team {}'s operator Rules?".format(team_name))


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
        description="The team's working directory lives at <project>/.herdr-synapse/<team>/. Setting it is human only and is what creates the folder.",
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
        description="Rules are the operator's DOs and DON'Ts (human only). Findings are attributed peer notes any member may add. "
                    "knowledge import --from <team|folder> adopts a previous team's rules, facts and canvas into this team (the operator).",
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
        return ["No teams in this session.", "", "Create one with the picker (prefix+t) or: herdr-synapse create <name> --member <pane>"]
    for info in teams:
        if info.get("error"):
            lines.append("{}  {} {}".format(info["team"], warn_mark, info["error"]))
            lines.append("")
            continue
        lines.append("{}  {}".format(info["team"], _workdir.status_summary(info)))
        missing_missions = info.get("missing_missions") or []
        if missing_missions:
            lines.append("    {} Mission missing: {}".format(warn_mark, ", ".join(str(name) for name in missing_missions)))
        if not info.get("project_dir"):
            lines.append("    no team folder. To give it one:")
            lines.append("      herdr-synapse project set <path> --team {}".format(info["team"]))
            lines.append("    then the team gets rules, per-member instructions, and an artifacts/ dir.")
            lines.append("")
            continue
        lines.append("    folder:   {}".format(info.get("folder")))
        lines.append("    rules:    {}".format("{} chars".format(info["rules_chars"]) if info.get("rules") else "none set  (herdr-synapse knowledge set \"…\")"))
        lines.append("    findings: {}".format(info.get("findings", 0)))
        last = info.get("last_finding")
        if last:
            lines.append("      last: {} — {}".format(last.get("author", "?"), _clip(str(last.get("text") or ""), max(20, width - 14))))
        members = info.get("members") or []
        if members:
            lines.append("    member documents:")
            for member in members:
                mark = bullet if member.get("mission") else warn_mark
                if member.get("mission"):
                    detail = "{} chars, Mission set".format(member["chars"])
                elif member.get("instructions"):
                    detail = "{} chars, Mission missing  (herdr-synapse instructions {} --edit)".format(member["chars"], member["name"])
                else:
                    detail = "none, Mission missing  (herdr-synapse instructions {} --set \"## Mission …\")".format(member["name"])
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
        raise HerdrTeamError("not_a_plugin_pane", "knowledge-pane runs in the knowledge popup; use `herdr-synapse ui knowledge`, `herdr-synapse knowledge-status`, or pass --force", 1)
    if not _p.socket_allowed(layout.config_dir, layout.socket):
        sys.stderr.write("herdr-synapse knowledge-pane skipped: socket not allowed\n")
        return EXIT_OK
    if not sys.stdout.isatty():
        raise HerdrTeamError("no_tty", "knowledge-pane needs a terminal (use `herdr-synapse knowledge-status`)", 1)
    import curses

    return int(curses.wrapper(_pane_loop, layout, bool(args.ascii)))


def _pane_loop(stdscr: Any, layout: Any, ascii_only: bool) -> int:
    """Scrollable read-only view. Local disk reads only, so ``r`` re-reads inline."""
    import curses

    from herdr_team.console import read_key

    curses.curs_set(0)
    stdscr.nodelay(False)  # blocking by design: this viewer has nothing to poll
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
