"""Inspect launch permissions and save operator-controlled overrides."""
from __future__ import annotations

import argparse
import os

from herdr_team import charter, identity, permissions, roster, store, swap
from herdr_team.cli import Command, emit
from herdr_team.cmd_board import _open_team, env_of
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError, UsageError


def _arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("member", nargs="?", help="member to inspect or configure (omit to list all)")
    parser.add_argument("mode", nargs="?", choices=permissions.MODES + ("inherit",), help="saved next-launch setting; inherit removes the member override")
    parser.add_argument("--default", dest="default_mode", choices=permissions.MODES, help="set the team default (initially yolo)")


def _run(args: argparse.Namespace) -> int:
    writing = args.mode is not None or args.default_mode is not None
    if args.default_mode is not None and args.member is not None:
        raise UsageError("use --default MODE alone, or MEMBER MODE for one member")
    layout, api, author, team_name, paths, doc = _open_team(args, require_server=False, write=writing)
    if writing:
        charter.require_human(layout, team_name, author, "permissions")
    if args.member and not any(m.get("name") == args.member and m.get("kind") != "human" and m.get("status") != "left" for m in doc.get("members", [])):
        raise HerdrTeamError("member_not_found", "no agent member named {}".format(args.member), EXIT_REFUSED)
    if writing:
        # Serialize with restore and swap, including a pending operation's retries.
        with store.FileLock(paths.root / "restore.lock", timeout=0, code="permissions_busy"):
            permissions.ensure_current_daemon(layout, env_of(args))

            def mutate(team: roster.Team) -> None:
                affected = list(team.agents()) if args.default_mode else [team.find(args.member)]
                for member in affected:
                    if member is None or member.status == "left":
                        raise HerdrTeamError("member_not_found", "member changed; refresh and retry", EXIT_REFUSED)
                    swap.require_available(member)
                if args.default_mode:
                    team.config["permissions"] = args.default_mode
                else:
                    affected[0].permissions = None if args.mode == "inherit" else args.mode

            doc = roster.update_team(paths, mutate).to_json()
            identity.audit(layout, team_name, "permissions_changed", author,
                           {"member": args.member, "mode": args.default_mode or args.mode})
            roster.append_system_record(paths, "permissions_changed",
                "{} permissions: {}; applies at next launch; running agents unchanged".format(args.member or team_name, args.default_mode or args.mode),
                to=["all"], socket=os.fspath(layout.socket))
    rows = [dict(name=m["name"], kind=m["kind"], **permissions.view(doc.get("config"), m))
            for m in doc.get("members", []) if m.get("kind") != "human" and m.get("status") != "left" and (not args.member or m.get("name") == args.member)]
    payload = {"team": team_name, "default": permissions.effective(doc.get("config"), {}), "members": rows}
    lines = ["{} default: {}; saved next-launch settings (running mode unverified)".format(team_name, payload["default"])]
    lines.extend("{} [{}]: {} ({}) — {}{}".format(r["name"], r["kind"], r["mode"], r["source"], r["effect"],
                 "; " + " ".join(r["flags"]) if r["flags"] else "") for r in rows)
    return emit(args, payload, "\n".join(lines))


COMMANDS = [Command("permissions", "show saved launch permissions; operator sets MEMBER yolo|native|inherit or --default MODE", _arguments, _run)]
