"""The compose popup, plan 7.1 path (e).

One line, console syntax, default recipient from
``HERDR_PLUGIN_CONTEXT_JSON.focused_pane_id`` looked up in the roster
(``focused_pane_agent`` is only a kind label). Author ``human`` via
``popup``, unverified, with a launch nonce. Esc and Ctrl-C exit; 10 min
idle watchdog.

Decisions live in ``tui_model`` (``ComposeModel``, ``compose_apply_key``,
``parse_post_directives``); this module reads the roster, runs the curses
loop, and executes the post through the ``herdr-synapse`` CLI so author
resolution, validation, and locking happen in exactly one code path. The
popup exits after a successful post; a refused post keeps the line so the
human can fix it.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sys
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from herdr_team import paths as _paths
from herdr_team import tui_model
from herdr_team.errors import EXIT_OK, EXIT_REFUSED, HerdrTeamError, emit_error
from herdr_team.paths import Layout
from herdr_team.tui_model import ComposeModel, Intent

IDLE_WATCHDOG_S = 600.0
TICK_S = 0.5
ENTRYPOINT = "compose"
NONCE_KEY = "launch_nonce"


@dataclass
class ComposeIntent:
    text: str
    to: List[str] = field(default_factory=list)
    kind: str = "note"
    reply_to: Optional[int] = None
    urgent: bool = False
    refs: List[str] = field(default_factory=list)

    def to_args(self) -> Dict[str, Any]:
        return {
            "text": self.text,
            "to": list(self.to),
            "kind": self.kind,
            "reply_to": self.reply_to,
            "urgent": self.urgent,
            "refs": list(self.refs),
            "spill": len(self.text) > tui_model.MAX_TEXT_CHARS,
        }


def default_recipient(context: Dict[str, Any], roster_members: List[Dict[str, Any]]) -> Optional[str]:
    """The roster member whose pane is ``focused_pane_id``; None when the focus is not a member."""
    return tui_model.compose_default_recipient(context, roster_members)


def parse_compose_line(line: str, default_to: Optional[str]) -> ComposeIntent:
    """Console syntax on one line; raises ``compose_invalid`` (exit 1) for an empty or malformed line."""
    spec, err = tui_model.parse_post_directives(line, default_to)
    if err or spec is None:
        raise HerdrTeamError("compose_invalid", err or "empty post", EXIT_REFUSED)
    to = list(spec.to) if spec.to else ["all"]
    return ComposeIntent(text=spec.text, to=to, kind=spec.kind, reply_to=spec.reply_to, urgent=spec.urgent, refs=list(spec.refs))


# --------------------------------------------------------------------------
# runtime helpers (no curses)


def load_context(env: Dict[str, str]) -> Dict[str, Any]:
    from herdr_team.picker import load_context as _load

    return _load(env)


def new_nonce() -> str:
    return secrets.token_hex(8)


def env_with_nonce(env: Dict[str, str], nonce: str) -> Dict[str, str]:
    """Copy ``env`` with ``launch_nonce`` added to ``HERDR_PLUGIN_CONTEXT_JSON`` (identity records it in ``origin``)."""
    out = dict(env)
    raw = out.get("HERDR_PLUGIN_CONTEXT_JSON") or ""
    try:
        doc = json.loads(raw) if raw else {}
    except ValueError:
        doc = {}
    doc = doc if isinstance(doc, dict) else {}
    doc.setdefault(NONCE_KEY, nonce)
    out["HERDR_PLUGIN_CONTEXT_JSON"] = json.dumps(doc, ensure_ascii=False, separators=(",", ":"))
    return out


def build_model(layout: Layout, team: str, context: Dict[str, Any]) -> ComposeModel:
    from herdr_team.console import roster_members

    members = roster_members(layout, team)
    names = [str(m.get("name")) for m in members if m.get("name") and m.get("status", "active") != "left"]
    return ComposeModel(default_to=default_recipient(context, members), team=team, roster_names=names, roster_members=list(members), file_roots=tui_model.member_file_roots(members))


def post_argv(intent: Intent, team: str) -> List[str]:
    from herdr_team.console import post_args

    return post_args(intent, team)


def execute_post(intent: Intent, team: str, env: Dict[str, str]) -> Dict[str, Any]:
    """Run ``herdr-synapse --json post ...``; returns ``{"ok", "seq", "to", "notifier"}`` or ``{"ok": False, "error": {...}}``."""
    from herdr_team.console import run_cli

    rc, out, err = run_cli(post_argv(intent, team), env)
    if err:
        return {"ok": False, "error": err, "rc": rc}
    if not isinstance(out, dict):
        return {"ok": False, "error": {"code": "cli_failed", "message": "post exited {}".format(rc)}, "rc": rc}
    return {"ok": True, "seq": out.get("seq"), "to": out.get("to") or [], "notifier": out.get("notifier"), "rc": rc}


# --------------------------------------------------------------------------
# curses loop


def _loop(stdscr: Any, model: ComposeModel, team: str, env: Dict[str, str]) -> int:
    import curses

    from herdr_team.console import disable_bracketed_paste, draw_lines, enable_bracketed_paste, read_key

    curses.raw()
    curses.noecho()
    stdscr.keypad(True)
    enable_bracketed_paste()
    last_key = time.monotonic()
    try:
        while True:
            height, width = stdscr.getmaxyx()
            lines = tui_model.compose_lines(model, width)
            x = tui_model.display_width(tui_model.INPUT_PROMPT) + tui_model.display_width(model.input[: model.cursor])
            draw_lines(stdscr, lines[:height], (min(1, height - 1), min(x, max(0, width - 1))))
            # Re-armed every pass: ``_read_escape`` used to clear this, which left
            # the loop blocking for ever with no tick and no idle watchdog.
            stdscr.timeout(int(TICK_S * 1000))
            key = read_key(stdscr, int(TICK_S * 1000))
            if key is None:
                if time.monotonic() - last_key > IDLE_WATCHDOG_S:
                    return EXIT_OK
                continue
            last_key = time.monotonic()
            intent = tui_model.compose_apply_key(model, key)
            if intent is None:
                continue
            if intent.kind == "quit":
                return EXIT_OK
            if intent.kind == "error":
                continue
            if intent.kind == "post":
                result = execute_post(intent, team, env)
                if result["ok"]:
                    return EXIT_OK
                error = result["error"]
                model.status = "post failed: {}: {}".format(error.get("code"), error.get("message"))
    finally:
        disable_bracketed_paste()


def run(layout: Layout, api: Any, env: Dict[str, str]) -> int:
    """Pick the team, build the model, run the popup; returns the exit code."""
    import curses

    from herdr_team.console import pick_team

    context = load_context(env)
    team = pick_team(layout, None, env)
    model = build_model(layout, team, context)
    nonce = str(context.get(NONCE_KEY) or context.get("nonce") or new_nonce())
    child_env = env_with_nonce(env, nonce)
    return int(curses.wrapper(_loop, model, team, child_env))


# --------------------------------------------------------------------------
# entrypoint (registered by cmd_misc as ``compose``)


def add_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--force", action="store_true", help="run from a plain shell instead of the popup")


def run_args(args: argparse.Namespace) -> int:
    from herdr_team import cli as _cli

    env = dict(args.env)
    layout = _cli.layout_for(args)
    if env.get("HERDR_PLUGIN_ENTRYPOINT_ID") != ENTRYPOINT and not getattr(args, "force", False):
        raise HerdrTeamError("not_a_plugin_pane", "compose runs in the post popup; use `herdr-synapse ui compose` or pass --force", EXIT_REFUSED)
    if not _paths.socket_allowed(layout.config_dir, layout.socket):
        # Plan 4.1 / PK-07: an unlisted socket makes the pane a no-op before any socket call.
        sys.stderr.write("herdr-synapse compose skipped: socket not allowed\n")
        return EXIT_OK
    if not sys.stdout.isatty():
        raise HerdrTeamError("no_tty", "compose needs a terminal", EXIT_REFUSED)
    team = getattr(args, "team", None)
    if team:
        env["HERDR_TEAM"] = str(team)
    api = _cli.api_for(args, layout)
    return run(layout, api, env)


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="herdr-synapse compose", allow_abbrev=False)
    parser.add_argument("--team")
    parser.add_argument("--session")
    parser.add_argument("--socket")
    parser.add_argument("--json", action="store_true")
    add_arguments(parser)
    args = parser.parse_args(list(sys.argv[1:] if argv is None else argv))
    args.env = dict(os.environ)
    try:
        return run_args(args)
    except HerdrTeamError as err:
        return emit_error(err)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
