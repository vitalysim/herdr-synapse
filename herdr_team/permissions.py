"""Saved launch policy, independent of model choice and runtime observations.

Native means that Synapse adds no permission bypass. The harness's own config
still applies; it is not a promise of sandboxing or interactive tool approval.
"""
from __future__ import annotations

from typing import Any, Dict, List

from herdr_team.errors import EXIT_REFUSED, HerdrTeamError, UsageError

MODES = ("yolo", "native")
#: The harness's own flag that starts its interactive TUI without tool-approval
#: prompts. Every kind listed gets it on every Synapse-managed launch unless its
#: policy is ``native``; a kind missing here has no known switch and launches
#: with its own settings. Each flag must combine with the kind's resume form in
#: ``roster.RESUME_COMMANDS``, because ``resume`` and ``restore`` append it there.
YOLO_ARGS = {
    "claude": ("--dangerously-skip-permissions",),
    "codex": ("--dangerously-bypass-approvals-and-sandbox",),
    "opencode": ("--auto",),
    "pi": ("--approve",),
    "gemini": ("--yolo",),
    "cursor": ("--force",),
    # ``--yolo`` auto-approves tools but still lets the agent ask; ``--auto`` would
    # stop it asking the operator anything, which the ask flow depends on.
    "kimi": ("--yolo",),
    "copilot": ("--yolo",),  # = --allow-all: tools, paths and URLs (Copilot CLI 0.0.381+)
    "qwen": ("--yolo",),
    "hermes": ("--yolo",),
    "kilo": ("--auto",),  # the TUI's own flag; explicit denies still apply
    "devin": ("--permission-mode", "dangerous"),
    "qodercli": ("--yolo",),  # = --permission-mode bypass_permissions
    "letta": ("--yolo",),  # = --permission-mode unrestricted
    "omp": ("--yolo",),
    "maki": ("--yolo",),  # deny rules still apply
    "muse": ("--yolo",),  # no approvals and no sandbox for the run
}
#: How each flag is known. ``live``: the whole Synapse launch was exercised in a
#: real TUI. ``help``: read from the installed binary's own ``--help``.
#: ``docs``: the vendor's documentation or source only; not run here (2026-09-24).
#: Deliberately absent: droid and grok (their references do not say the flag
#: applies to the interactive TUI), kiro (the flag belongs to ``kiro-cli chat``
#: and asks for a confirmation at startup), cline (``--yolo`` exits after one
#: turn), agy (unconfirmed; the local ``agy`` was the editor launcher), and amp
#: and mastracode (no approval prompts by default, so there is nothing to add).
YOLO_EVIDENCE = {
    "claude": "live", "codex": "live", "opencode": "live", "pi": "live",
    # Kimi's flag was seen live in a Synapse launch (its footer read "yolo", 2026-09-26).
    "kimi": "live", "gemini": "help", "cursor": "help",
    "copilot": "docs", "qwen": "docs", "hermes": "docs", "kilo": "docs", "devin": "docs",
    "qodercli": "docs", "letta": "docs", "omp": "docs", "maki": "docs", "muse": "docs",
}


def validate(mode: Any) -> str:
    if mode not in MODES:
        raise UsageError("permissions must be yolo or native")
    return str(mode)


def effective(config: Any, member: Any) -> str:
    override = member.get("permissions") if isinstance(member, dict) else getattr(member, "permissions", None)
    default = config.get("permissions") if isinstance(config, dict) else None
    return validate(override if override is not None else default if default is not None else "yolo")


def launch_args(kind: Any, mode: str) -> List[str]:
    return list(YOLO_ARGS.get(kind, ())) if validate(mode) == "yolo" else []


def view(config: Any, member: Any) -> Dict[str, Any]:
    get = member.get if isinstance(member, dict) else lambda key: getattr(member, key, None)
    mode = effective(config, member)
    kind = get("kind")
    source = "member" if get("permissions") is not None else "team" if isinstance(config, dict) and config.get("permissions") is not None else "default"
    if kind == "pi":
        effect = "project resources trusted for this run; Pi has no built-in tool approval prompts" if mode == "yolo" else "native project trust; Pi has no built-in tool approval prompts"
    elif kind not in YOLO_ARGS:
        effect = "no known YOLO switch for this agent; uses agent settings"
    elif mode == "native":
        effect = "uses agent permission settings; no Synapse bypass flags"
    else:
        effect = "auto-approval" if kind in ("opencode", "gemini", "kimi", "kilo") else "permission bypass"
        if YOLO_EVIDENCE.get(kind) != "live":
            effect += "; flag from the agent's own {}, not yet live-verified by Synapse".format(
                "--help" if YOLO_EVIDENCE.get(kind) == "help" else "documentation")
    return {"mode": mode, "source": source, "flags": launch_args(kind, mode),
            "evidence": YOLO_EVIDENCE.get(kind), "effect": effect, "applies": "next launch", "running_mode": "unknown"}


def ensure_current_daemon(layout: Any, env: Dict[str, str]) -> None:
    """An old notifier would rebuild YOLO argv regardless of the saved policy."""
    if env.get("HERDR_TEAM_NO_DAEMON") == "1":
        return
    from herdr_team import VERSION, daemon
    info = daemon.read_daemon_info(layout.session)
    if daemon.info_alive(info) and info.version != VERSION:
        daemon.detach_and_run(layout, env, replace=True)
    if not daemon.ensure_daemon(layout, env):
        raise HerdrTeamError("daemon_unavailable", "start the current Synapse daemon before changing permissions", EXIT_REFUSED)
    info = daemon.read_daemon_info(layout.session)
    if info is None or info.version != VERSION:
        raise HerdrTeamError("daemon_outdated", "run herdr-synapse daemon start --replace, then retry", EXIT_REFUSED)
