"""Nonblocking swap process shared by the member picker and board console."""
from __future__ import annotations

from typing import Any, Dict, List

from herdr_team.restore_ui import RestoreProcess


def command(args: Dict[str, Any]) -> List[str]:
    argv = ["--team", str(args["team"]), "swap", str(args["member"])]
    if args.get("retry"):
        return argv + ["--retry"]
    argv += ["--to", str(args["kind"])]
    if args.get("setting"):
        argv += ["--model", str(args["setting"])]
    return argv


class SwapProcess(RestoreProcess):
    result_key = "swap"

    def __init__(self, args: Dict[str, Any], env: Dict[str, str]) -> None:
        self.member = str(args["member"])
        super().__init__(str(args["team"]), None, env, command=command(args))

    def progress(self) -> str:
        text = super().progress()
        return "creating replacement for {}…".format(self.member) if text.startswith("restoring ") else text


def result_text(result: Dict[str, Any]) -> str:
    op = result.get("swap") or {}
    if result.get("exit_code") or op.get("phase") != "complete":
        return "swap failed: {}; inspect its pane, then retry with /swap {} --retry".format(result.get("error") or op.get("error") or "see swap --status", result.get("member", "<name>"))
    return "{}: fresh {} in {}; handoff queued".format(result.get("member"), op["destination"]["kind"], op["pane"]["pane_id"])
