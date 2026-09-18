"""Polled restore CLI process; temporary files keep pipe buffers from blocking startup."""
from __future__ import annotations

import json
import os
import subprocess
import tempfile
from typing import Any, Dict, Optional

from herdr_team.console import cli_path


class RestoreProcess:
    result_key = "counts"

    def __init__(self, team: str, workspace: Optional[str], env: Dict[str, str], command: Optional[list] = None) -> None:
        self.team = team
        self.output = tempfile.TemporaryFile()
        self.errors = tempfile.TemporaryFile()
        argv = [os.fspath(cli_path()), "--json", "restore", team]
        if workspace:
            argv += ["--workspace", workspace]
        if command is not None:
            argv = [os.fspath(cli_path()), "--json"] + command
        try:
            self.proc = subprocess.Popen(argv, env=dict(env), stdin=subprocess.DEVNULL,
                                         stdout=self.output, stderr=self.errors, start_new_session=True)
        except OSError:
            self.close()
            raise

    def progress(self) -> str:
        size = os.fstat(self.errors.fileno()).st_size
        tail = os.pread(self.errors.fileno(), min(size, 4096), max(0, size - 4096)).decode("utf-8", "replace").strip()
        return tail.splitlines()[-1] if tail else "restoring {}…".format(self.team)

    def result(self) -> Optional[Dict[str, Any]]:
        code = self.proc.poll()
        if code is None:
            return None
        self.output.seek(0)
        try:
            out = json.load(self.output)
        except (ValueError, UnicodeError):
            out = None
        if not isinstance(out, dict) or self.result_key not in out:
            out = {"error": self.progress(), "counts": {"failed": 1}, "members": []}
        out["exit_code"] = code
        return out

    def close(self) -> None:
        self.output.close()
        self.errors.close()
