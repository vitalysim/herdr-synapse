"""The only discovery-related command that may spend inference quota, after consent."""
from __future__ import annotations

import argparse
import os
from typing import List

from herdr_team import model_checks, model_inventory as I, models, paths, store
from herdr_team.cli import Command, api_for, emit, layout_for
from herdr_team.cmd_board import resolve_author
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError, UsageError


def _arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("harness", choices=I.KINDS)
    parser.add_argument("--model", required=True)
    parser.add_argument("--profile")
    parser.add_argument("--effort")
    parser.add_argument("--cwd", required=True)
    parser.add_argument("--yes", action="store_true", help="consent to one request that may consume quota or incur charges")


def _run(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    hint = paths.team_name_from_arg(getattr(args, "team", None), args.env)
    author = resolve_author(args, layout, api_for(args, layout), team=hint, require_server=False)
    if not author.trusted_human:
        raise HerdrTeamError("author_mismatch", "model-check is the operator's alone", EXIT_REFUSED)
    if not args.yes:
        raise HerdrTeamError("confirmation_required", "one request may consume quota or incur charges; confirm with --yes", EXIT_REFUSED)
    cwd = os.path.abspath(os.path.expanduser(args.cwd))
    if not os.path.isdir(cwd):
        raise UsageError("--cwd must be an existing directory")
    models.validate(args.harness, args.model, args.effort)
    if args.profile and not I.token(args.profile):
        raise UsageError("invalid profile")
    with store.FileLock(layout.session.root / "model-check-run.lock", timeout=0, code="model_check_busy"):
        inventory = I.discover(args.harness, cwd, dict(args.env), args.profile, refresh=True)
        evidence = model_checks.check(inventory, args.model, args.effort, dict(args.env))
        model_checks.save(layout.session, evidence)
    return emit(args, evidence, "{}: {} (requested {}; observed {})".format(args.harness, evidence["status"], args.model, evidence.get("observed_model") or "unconfirmed"))


COMMANDS: List[Command] = [Command("model-check", "explicit operator-consented, isolated single-model access check", _arguments, _run)]
