"""One-command refresh of the Synapse checkout, CLI, skill, and daemon.

GitHub-managed plugins are reinstalled through Herdr, which owns their
checkout lifecycle.  A locally linked checkout is never pulled or rewritten:
it is already the operator's source of truth, so only its installed surfaces
are refreshed.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from herdr_team import PLUGIN_ID, VERSION
from herdr_team import api as _api
from herdr_team import cli as _cli
from herdr_team.cli import api_for, emit, layout_for
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError

HERDR_UPDATE_TIMEOUT_S = 180.0
SYNAPSE_STEP_TIMEOUT_S = 30.0


def _update_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--ref", metavar="REF", help="GitHub source only: replace the recorded branch/tag for this update")
    parser.add_argument("--force-skill", action="store_true", help="replace foreign Synapse skill directories or links")


def _plugin_entry(api: Any) -> Dict[str, Any]:
    result = api.request("plugin.list", {})
    plugins = result.get("plugins") if isinstance(result, dict) else result
    for entry in plugins or []:
        if isinstance(entry, dict) and entry.get("plugin_id") == PLUGIN_ID:
            return dict(entry)
    raise HerdrTeamError(
        "plugin_not_installed",
        "{} is not registered with this Herdr server; install or link it first".format(PLUGIN_ID),
        EXIT_REFUSED,
    )


def _github_install(entry: Dict[str, Any], override_ref: Optional[str]) -> Tuple[str, Optional[str]]:
    source = entry.get("source") if isinstance(entry.get("source"), dict) else {}
    owner = source.get("owner")
    repo = source.get("repo")
    subdir = source.get("subdir")
    if not isinstance(owner, str) or not owner or not isinstance(repo, str) or not repo:
        raise HerdrTeamError("plugin_source_invalid", "the registered GitHub source has no owner/repository", EXIT_REFUSED)
    spec = "{}/{}".format(owner, repo)
    if isinstance(subdir, str) and subdir:
        spec += "/" + subdir
    requested_ref = override_ref if override_ref is not None else source.get("requested_ref")
    return spec, requested_ref if isinstance(requested_ref, str) and requested_ref else None


def _failure_message(stderr: str, stdout: str) -> str:
    for line in (stderr or "").splitlines():
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            error = obj.get("error") if isinstance(obj.get("error"), dict) else obj
            if isinstance(error, dict) and error.get("message"):
                return str(error["message"])
    return (stderr or stdout or "command failed").strip().split("\n", 1)[0][:500]


def _run_herdr_refresh(api: Any, command: Sequence[str], label: str) -> None:
    result = api.run(command, timeout=HERDR_UPDATE_TIMEOUT_S)
    if not result.ok:
        raise HerdrTeamError(
            "plugin_update_failed",
            "Herdr could not refresh {}: {}".format(label, _failure_message(result.stderr, result.stdout)),
            EXIT_REFUSED,
            {"source": label, "returncode": result.returncode},
        )


def _run_synapse_step(
    executable: Path,
    args: Sequence[str],
    env: Mapping[str, str],
    timeout: float = SYNAPSE_STEP_TIMEOUT_S,
) -> Dict[str, Any]:
    argv = [os.fspath(executable), "--json"] + [str(value) for value in args]
    try:
        completed = subprocess.run(
            argv,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=dict(env),
            check=False,
        )
    except subprocess.TimeoutExpired:
        raise HerdrTeamError("update_step_timeout", "{} did not finish within {:g}s".format(" ".join(args), timeout), EXIT_REFUSED)
    except OSError as err:
        raise HerdrTeamError("update_step_failed", "cannot run {}: {}".format(executable, err), EXIT_REFUSED)
    stdout = completed.stdout.decode("utf-8", "replace")
    stderr = completed.stderr.decode("utf-8", "replace")
    if completed.returncode != 0:
        raise HerdrTeamError(
            "update_step_failed",
            "{}: {}".format(" ".join(args), _failure_message(stderr, stdout)),
            EXIT_REFUSED,
            {"step": list(args), "returncode": completed.returncode},
        )
    try:
        payload = json.loads(stdout)
    except ValueError:
        raise HerdrTeamError("update_step_failed", "{} printed no valid JSON".format(" ".join(args)), EXIT_REFUSED)
    if not isinstance(payload, dict):
        raise HerdrTeamError("update_step_failed", "{} printed a non-object result".format(" ".join(args)), EXIT_REFUSED)
    if stderr.strip():
        payload = dict(payload)
        payload["warnings"] = stderr.strip().splitlines()
    return payload


def _plugin_root(entry: Dict[str, Any]) -> Path:
    value = entry.get("plugin_root")
    if not isinstance(value, str) or not value:
        manifest = entry.get("manifest_path")
        value = os.fspath(Path(manifest).parent) if isinstance(manifest, str) and manifest else ""
    if not value:
        raise HerdrTeamError("plugin_source_invalid", "the registered plugin has no checkout path", EXIT_REFUSED)
    root = Path(value).resolve()
    executable = root / "bin" / "herdr-synapse"
    if not executable.is_file():
        raise HerdrTeamError("plugin_source_invalid", "updated checkout has no {}".format(executable), EXIT_REFUSED)
    return root


def _human(payload: Dict[str, Any]) -> str:
    checkout = payload["checkout"]
    lines: List[str] = ["Synapse update complete: {} -> {}".format(payload["before"], payload["after"])]
    if checkout["action"] == "reinstalled":
        lines.append("  checkout: refreshed {}{}".format(checkout["source"], " @ " + checkout["ref"] if checkout.get("ref") else ""))
    else:
        lines.append("  checkout: local link refreshed; files untouched ({})".format(checkout["path"]))
    lines.append("  CLI: {}".format(payload["cli"].get("path") or "refreshed"))
    lines.append("  skill: v{} {}".format(payload["skill"].get("version", "?"), "current" if payload["skill"].get("ok") else "needs attention"))
    daemon = payload["daemon"].get("daemon") if isinstance(payload["daemon"].get("daemon"), dict) else {}
    lines.append("  notifier: pid {} (Herdr {}; safe ! {})".format(
        daemon.get("pid") or "?",
        daemon.get("herdr_version") or "?",
        "ready" if ((daemon.get("capabilities") or {}).get("atomic_idle_prompt") is True) else "unavailable" if ((daemon.get("capabilities") or {}).get("atomic_idle_prompt") is False) else "unknown",
    ))
    return "\n".join(lines)


def run_update(args: argparse.Namespace) -> int:
    layout = layout_for(args)
    api = api_for(args, layout)
    before = _plugin_entry(api)
    source = before.get("source") if isinstance(before.get("source"), dict) else {}
    source_kind = str(source.get("kind") or "unknown")
    checkout: Dict[str, Any]
    if source_kind == "github":
        spec, requested_ref = _github_install(before, args.ref)
        command = ["plugin", "install", spec]
        if requested_ref:
            command += ["--ref", requested_ref]
        command.append("--yes")
        _run_herdr_refresh(api, command, spec)
        checkout = {"action": "reinstalled", "source": spec, "ref": requested_ref}
        current = _plugin_entry(api)
    elif source_kind == "local":
        if args.ref:
            raise HerdrTeamError("plugin_source_local", "--ref cannot update a locally linked checkout; update that checkout yourself", EXIT_REFUSED)
        linked_root = _plugin_root(before)
        link_command = ["plugin", "link", os.fspath(linked_root), "--enabled" if before.get("enabled") is not False else "--disabled"]
        _run_herdr_refresh(api, link_command, os.fspath(linked_root))
        current = _plugin_entry(api)
        checkout = {"action": "relinked", "path": os.fspath(linked_root)}
    else:
        raise HerdrTeamError(
            "plugin_source_unsupported",
            "cannot update plugin source kind {!r}; reinstall or link Synapse explicitly".format(source_kind),
            EXIT_REFUSED,
            {"source_kind": source_kind},
        )

    root = _plugin_root(current)
    executable = root / "bin" / "herdr-synapse"
    child_env = _api.child_env(args.env, layout.socket)
    cli_result = _run_synapse_step(executable, ["install-cli", "--yes"], child_env)
    skill_args = ["skill", "install"] + (["--force"] if args.force_skill else [])
    skill_result = _run_synapse_step(executable, skill_args, child_env)
    daemon_result = _run_synapse_step(executable, ["daemon", "start", "--replace"], child_env)
    payload = {
        "before": str(before.get("version") or VERSION),
        "after": str(current.get("version") or VERSION),
        "source_kind": source_kind,
        "checkout": checkout,
        "plugin_root": os.fspath(root),
        "cli": cli_result,
        "skill": skill_result,
        "daemon": daemon_result,
    }
    if skill_result.get("ok") is False:
        raise HerdrTeamError(
            "skill_update_refused",
            "the checkout, CLI, and notifier were refreshed, but one or more skill targets need attention; inspect `herdr-synapse skill check` or rerun update with --force-skill",
            EXIT_REFUSED,
            {"update": payload},
        )
    return emit(args, payload, lambda: _human(payload))


COMMANDS = [
    _cli.Command(
        name="update",
        help="refresh the managed checkout, CLI, skill, and notifier",
        add_arguments=_update_args,
        run=run_update,
        description="One safe update path. GitHub installs are refreshed through Herdr; local links are kept as-is, then the CLI, skill copies, and notifier are refreshed.",
    )
]
