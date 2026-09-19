"""Saved launch policy, independent of model choice and runtime observations.

Native means that Synapse adds no permission bypass. The harness's own config
still applies; it is not a promise of sandboxing or interactive tool approval.
"""
from __future__ import annotations

from typing import Any, Dict, List

from herdr_team.errors import EXIT_REFUSED, HerdrTeamError, UsageError

MODES = ("yolo", "native")
YOLO_ARGS = {
    "claude": ("--dangerously-skip-permissions",),
    "codex": ("--dangerously-bypass-approvals-and-sandbox",),
    "opencode": ("--auto",),
    "pi": ("--approve",),
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
        effect = "no verified Synapse YOLO switch; uses agent settings"
    elif mode == "native":
        effect = "uses agent permission settings; no Synapse bypass flags"
    else:
        effect = "auto-approval" if kind == "opencode" else "permission bypass"
    return {"mode": mode, "source": source, "flags": launch_args(kind, mode),
            "effect": effect, "applies": "next launch", "running_mode": "unknown"}


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
