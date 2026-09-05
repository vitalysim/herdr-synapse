"""``herdr-team`` argparse root and command registry.

Every ``herdr_team/cmd_*.py`` module exposes ``COMMANDS: List[Command]``.
``load_commands`` imports the fixed module list, checks for duplicate names,
and ``build_parser`` registers each as a subcommand. Commands receive the
parsed ``argparse.Namespace`` and return an exit code; they raise
``HerdrTeamError`` for anything that must end as a JSON error on stderr.

Global flags available to every command: ``--json``, ``--team``,
``--session``, ``--socket``. ``args.env`` carries the environment mapping
(``os.environ`` unless a test injected one); use ``layout_for(args)`` and
``api_for(args)`` rather than touching ``os.environ`` or paths directly.

Exit-code and output contract: ``docs/cli.md``.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys
from dataclasses import dataclass, field
from typing import IO, Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple, Union

from herdr_team import SKILL_VERSION, VERSION
from herdr_team.errors import EXIT_OK, EXIT_USAGE, HerdrTeamError, UsageError, emit_error
from herdr_team import paths as _paths

PROG = "herdr-team"

COMMAND_MODULES: Tuple[str, ...] = (
    "herdr_team.cmd_roster",
    "herdr_team.cmd_board",
    "herdr_team.cmd_misc",
    "herdr_team.cmd_hooks",
    "herdr_team.cmd_skill",
    "herdr_team.cmd_daemon",
    "herdr_team.cmd_ui",
)


@dataclass
class Command:
    """One subcommand.

    ``add_arguments(parser)`` adds command-specific arguments;
    ``run(args) -> int`` executes and returns the exit code. ``aliases`` are
    extra names; ``hidden`` keeps internal commands (``hook-event``) out of
    ``--help``.
    """

    name: str
    help: str
    add_arguments: Callable[[argparse.ArgumentParser], None]
    run: Callable[[argparse.Namespace], int]
    aliases: Tuple[str, ...] = ()
    hidden: bool = False
    description: Optional[str] = None
    module: str = field(default="", compare=False)


class _Parser(argparse.ArgumentParser):
    """argparse that raises ``UsageError`` instead of printing and exiting."""

    def error(self, message: str) -> None:  # type: ignore[override]
        raise UsageError(message, {"usage": self.format_usage().strip()})


def load_commands(modules: Sequence[str] = COMMAND_MODULES) -> List[Command]:
    """Import every command module and collect ``COMMANDS``; duplicate names are a bug."""
    commands: List[Command] = []
    seen: Dict[str, str] = {}
    for module_name in modules:
        module = importlib.import_module(module_name)
        exported = getattr(module, "COMMANDS", None)
        if exported is None:
            raise RuntimeError("{} defines no COMMANDS".format(module_name))
        for command in exported:
            if not isinstance(command, Command):
                raise RuntimeError("{} exports a non-Command entry: {!r}".format(module_name, command))
            for name in (command.name,) + tuple(command.aliases):
                if name in seen:
                    raise RuntimeError("command {!r} registered by both {} and {}".format(name, seen[name], module_name))
                seen[name] = module_name
            command.module = module_name
            commands.append(command)
    return commands


def add_global_arguments(parser: argparse.ArgumentParser, nested: bool = False) -> None:
    """Global flags; on a subparser their defaults are suppressed so the root's values survive."""
    kw: Dict[str, Any] = {"default": argparse.SUPPRESS} if nested else {}
    parser.add_argument("--json", action="store_true", help="print one JSON object on stdout", **kw)
    parser.add_argument("--team", metavar="NAME|PATH", help="team name, or a team directory path when outside Herdr", **kw)
    parser.add_argument("--session", metavar="NAME", help="Herdr named session (mirrors herdr --session)", **kw)
    parser.add_argument("--socket", metavar="PATH", help="Herdr socket path override (wins over --session and env)", **kw)
    parser.add_argument("--session-mismatch-ok", dest="session_mismatch_ok", action="store_true", help="write to a team whose team.json socket differs from the resolved socket (plan 12)", **kw)


def build_parser(commands: Optional[List[Command]] = None) -> argparse.ArgumentParser:
    parser = _Parser(
        prog=PROG,
        description="Teams of coding agents in one Herdr session.",
        epilog="Exit codes: 0 ok, 1 refused, 2 usage, 3 not a member or Herdr unreachable, 4 echo rejected, 5 daemon down or lock timeout. See docs/cli.md.",
        allow_abbrev=False,
    )
    parser.add_argument("--version", action="store_true", help="print the plugin version and exit")
    parser.add_argument("--skill", action="store_true", help="print skills/herdr-team/SKILL.md and exit")
    add_global_arguments(parser)
    subparsers = parser.add_subparsers(dest="command", metavar="<command>", parser_class=_Parser)
    for command in commands if commands is not None else load_commands():
        kwargs: Dict[str, Any] = {
            "aliases": list(command.aliases),
            "description": command.description or command.help,
            "allow_abbrev": False,
        }
        if not command.hidden:
            kwargs["help"] = command.help
        sub = subparsers.add_parser(command.name, **kwargs)
        add_global_arguments(sub, nested=True)
        command.add_arguments(sub)
        sub.set_defaults(_command=command)
    return parser


# --------------------------------------------------------------------------
# helpers for command modules


def layout_for(args: argparse.Namespace) -> _paths.Layout:
    """Resolve socket, state root, and session dir from the global flags and ``args.env``."""
    return _paths.resolve_layout(
        args.env,
        session=getattr(args, "session", None),
        socket=getattr(args, "socket", None),
        team=getattr(args, "team", None),
    )


def api_for(args: argparse.Namespace, layout: Optional[_paths.Layout] = None) -> Any:
    """A ``HerdrApi`` bound to the resolved socket (tests may pre-set ``args.api``)."""
    preset = getattr(args, "api", None)
    if preset is not None:
        return preset
    from herdr_team.api import HerdrApi

    resolved = layout if layout is not None else layout_for(args)
    return HerdrApi(resolved.socket, env=args.env)


def emit(args: argparse.Namespace, payload: Dict[str, Any], human: Union[None, str, Callable[[], str]] = None) -> int:
    """Print ``payload`` as one JSON object in ``--json`` mode, else the human text."""
    out: IO[str] = getattr(args, "stdout", None) or sys.stdout
    if getattr(args, "json", False):
        out.write(json.dumps(payload, ensure_ascii=False) + "\n")
    else:
        if callable(human):
            human = human()
        if human is None:
            human = json.dumps(payload, ensure_ascii=False, indent=2)
        out.write(human if human.endswith("\n") else human + "\n")
    out.flush()
    return EXIT_OK


def read_skill_text() -> str:
    path = _paths.skill_file()
    try:
        return path.read_text(encoding="utf-8")
    except OSError as err:
        raise HerdrTeamError("skill_missing", "cannot read {}: {}".format(path, err))


# --------------------------------------------------------------------------
# entry point


def main(
    argv: Optional[Sequence[str]] = None,
    env: Optional[Mapping[str, str]] = None,
    stdout: Optional[IO[str]] = None,
    stderr: Optional[IO[str]] = None,
) -> int:
    """Parse, dispatch, and turn every ``HerdrTeamError`` into a JSON stderr line."""
    args_list = list(sys.argv[1:] if argv is None else argv)
    out = stdout if stdout is not None else sys.stdout
    err = stderr if stderr is not None else sys.stderr
    environment: Mapping[str, str] = env if env is not None else os.environ
    json_mode = "--json" in args_list
    try:
        parser = build_parser()
        args = parser.parse_args(args_list)
        args.env = environment
        args.stdout = out
        args.stderr = err
        if args.version:
            return emit(args, {"version": VERSION, "skill_version": SKILL_VERSION, "plugin_id": "herdr-team"}, "{} {}".format(PROG, VERSION))
        if args.skill:
            text = read_skill_text()
            if args.json:
                return emit(args, {"skill": text, "skill_version": SKILL_VERSION})
            out.write(text if text.endswith("\n") else text + "\n")
            out.flush()
            return EXIT_OK
        command = getattr(args, "_command", None)
        if command is None:
            raise UsageError("a command is required", {"usage": parser.format_usage().strip()})
        return int(command.run(args))
    except SystemExit as exc:  # argparse --help
        code = exc.code
        return int(code) if isinstance(code, int) else (0 if code is None else 1)
    except HerdrTeamError as exc:
        # Always one JSON object first; in human mode a usage error also gets the usage text after it.
        code = emit_error(exc, err)
        if not json_mode and isinstance(exc, UsageError):
            usage = exc.details.get("usage")
            if usage:
                err.write(usage + "\n")
                err.flush()
        return code
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        return EXIT_OK


if __name__ == "__main__":
    # ``python -m herdr_team.cli`` executes this file as ``__main__`` while the
    # command modules import ``herdr_team.cli``; dispatch through the imported
    # module so there is exactly one ``Command`` class in the registry.
    from herdr_team.cli import main as _main

    sys.exit(_main())
