"""Operator-created fresh replacements, with a read-only plan and durable retries."""
from __future__ import annotations

import argparse
import os
from pathlib import Path

from herdr_team import cmd_roster as commands
from herdr_team import roster, store, swap
from herdr_team.cli import Command, api_for, emit, layout_for
from herdr_team.cmd_board import audit, check_write_session, env_of
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError, UsageError


def _arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("member")
    parser.add_argument("--to", choices=swap.KINDS, help="type of fresh agent to create")
    parser.add_argument("--model", help="destination model[@effort] (default: saved or team settings)")
    parser.add_argument("--handoff-file", metavar="PATH", help="optional operator handoff note, up to 8 KiB")
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="preview without creating or stopping anything")
    mode.add_argument("--status", action="store_true", help="show the last swap and recovery information")
    mode.add_argument("--retry", action="store_true", help="continue the unfinished swap in its reserved pane")
    mode.add_argument("--cancel", action="store_true", help="cancel before takeover; retain the source conversation for resume")


def _run(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    api = api_for(args, layout)
    env = env_of(args)
    team_name = commands._team_arg(args, layout, None)
    check_write_session(args, layout, team_name)
    author = commands._author(args, layout, api, team=team_name, require_server=not args.status)
    commands._human_only(layout, team_name, author, "swap")
    book = roster.Roster(layout, team_name)
    member = book.load().find(args.member)
    if member is None or member.is_human or member.status == "left":
        raise HerdrTeamError("member_not_found", "no agent member named {}".format(args.member), EXIT_REFUSED)
    if (args.status or args.retry or args.cancel) and (args.to or args.model or args.handoff_file):
        raise UsageError("--status/--retry/--cancel use the saved swap; omit --to, --model and --handoff-file")
    if args.status:
        op = member.swap or {}
        return emit(args, {"team": team_name, "member": member.name, "swap": op, "agent_history": member.agent_history},
                    "{}: {}{}".format(member.name, op.get("phase", "no swap"), "; " + op["error"] if op.get("error") else ""))
    if not args.retry and not args.cancel and not args.to:
        raise UsageError("choose --to {}, or use --status/--retry/--cancel".format("|".join(swap.KINDS)))
    if member.pane_id and member.pane_id in (author.pane_id, env.get("HERDR_PANE_ID")):
        raise HerdrTeamError("swap_own_pane", "run swap from the team picker, console, or another pane", EXIT_REFUSED)
    note = ""
    if args.handoff_file:
        try:
            with Path(args.handoff_file).open("rb") as stream:
                data = stream.read(8193)
            if len(data) > 8192:
                raise UsageError("handoff note exceeds 8 KiB")
            note = data.decode("utf-8")
        except (OSError, UnicodeError) as err:
            raise UsageError("cannot read handoff note: {}".format(err))
    if args.dry_run:
        spec = swap.plan(book.load(), member.name, args.to, args.model, env)
        if not roster._kind_verified(layout, args.to):
            raise HerdrTeamError("kind_untrusted", "trust or probe {} before swapping to it".format(args.to), EXIT_REFUSED)
        swap.source_pane(api, spec["source"])
        spec.pop("source")
        return emit(args, dict(spec, team=team_name, dry_run=True),
                    "create fresh {} for {}; keep its configuration; close its outgoing pane".format(args.to, member.name))
    # Shared with restore: no restoration can race allocation/launch/takeover.
    with store.FileLock(book.paths.root / "restore.lock", timeout=0, code="swap_busy"):
        # Old daemons do not understand the durable replacement reservation.
        # Replace only a verified running plugin daemon, through its normal startup path.
        if env.get("HERDR_TEAM_NO_DAEMON") != "1":
            from herdr_team import VERSION, daemon
            info = daemon.read_daemon_info(layout.session)
            if daemon.info_alive(info) and info.version != VERSION:
                daemon.detach_and_run(layout, env, replace=True)
            if not daemon.ensure_daemon(layout, env):
                raise HerdrTeamError("daemon_unavailable", "start the current Synapse daemon before swapping", EXIT_REFUSED)
            info = daemon.read_daemon_info(layout.session)
            if info is None or info.version != VERSION:
                raise HerdrTeamError("daemon_outdated", "run herdr-synapse daemon start --replace, then retry", EXIT_REFUSED)
        member = book.load().find(args.member)
        if args.retry or args.cancel:
            if member is None or not swap.active(member):
                raise HerdrTeamError("swap_not_pending", "no unfinished swap to retry", EXIT_REFUSED)
            op = member.swap
            if args.cancel:
                op = swap.cancel(book, api, args.member, op)
                audit(layout, team_name, "swap_cancelled", author, {"member": args.member, "swap_id": op["id"]})
                return emit(args, {"team": team_name, "member": args.member, "swap": op},
                            "swap cancelled; source configuration retained. If its pane was closed, use herdr-synapse resume {}".format(args.member))
        else:
            spec = swap.plan(book.load(), args.member, args.to, args.model, env)
            if not roster._kind_verified(layout, args.to):
                raise HerdrTeamError("kind_untrusted", "trust or probe {} before swapping to it".format(args.to), EXIT_REFUSED)
            swap.source_pane(api, spec["source"])
            commands._ensure_daemon(layout, env)
            op = swap.prepare(book, spec, author, note)
        def progress(message: str) -> None:
            args.stderr.write("swap {}: {}\n".format(args.member, message))
            args.stderr.flush()
        audit(layout, team_name, "swap_requested", author, {"member": args.member, "swap_id": op["id"], "retry": args.retry})
        try:
            op = swap.execute(book, api, args.member, op, author, progress)
        except (HerdrTeamError, OSError) as err:
            op = swap.current(book.paths, args.member).get("swap") or op
            emit(args, {"team": team_name, "member": args.member, "swap": op, "error": str(err)},
                 "swap failed: {}; inspect the reserved pane, then run herdr-synapse --team {} swap {} --retry".format(err, team_name, args.member))
            return EXIT_REFUSED
        return emit(args, {"team": team_name, "member": args.member, "swap": op},
                    "{} now has a fresh {} in {}; handoff queued".format(args.member, op["destination"]["kind"], op["pane"]["pane_id"]))


COMMANDS = [Command("swap", "create a fresh agent to replace a member's current agent (operator only)", _arguments, _run)]
