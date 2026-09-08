"""Exit-code contract and the one exception type every module raises.

Contract (plan section 5.2, ``docs/cli.md``):

    0  ok
    1  refused or validation failure
    2  usage error
    3  not a member, or Herdr unreachable
    4  echo rejected (text carries a ``[herdr-team`` marker or ``[n<digits>]``)
    5  daemon down, or lock timeout

Every error leaves the process as one JSON object on stderr::

    {"code": "<snake_case_code>", "message": "<human text>", ...details}

``code`` is stable and machine-readable; ``message`` is for humans. Extra
keys come from ``HerdrTeamError.details`` and must be JSON-serialisable.
"""

from __future__ import annotations

import json
import sys
from typing import IO, Any, Dict, Optional

EXIT_OK = 0
EXIT_REFUSED = 1
EXIT_USAGE = 2
EXIT_UNREACHABLE = 3
EXIT_ECHO_REJECTED = 4
EXIT_DAEMON_DOWN = 5
#: A ``post --wait`` gave up: the operator never answered. Distinct from
#: ``EXIT_REFUSED`` on purpose, so an agent can tell "nobody answered" from
#: "the post was rejected" without reading prose. 7 is the Claude hook shim's
#: internal block code and is not a contract code, so 6 is the next free one.
EXIT_NO_ANSWER = 6

#: Default exit code per well-known error code. Anything not listed exits 1.
EXIT_CODE_FOR: Dict[str, int] = {
    "usage": EXIT_USAGE,
    "unknown_command": EXIT_USAGE,
    "not_a_member": EXIT_UNREACHABLE,
    "herdr_unreachable": EXIT_UNREACHABLE,
    "server_not_running": EXIT_UNREACHABLE,
    "herdr_timeout": EXIT_UNREACHABLE,
    "herdr_not_found": EXIT_UNREACHABLE,
    "team_required": EXIT_UNREACHABLE,
    "echo_rejected": EXIT_ECHO_REJECTED,
    "daemon_down": EXIT_DAEMON_DOWN,
    "lock_timeout": EXIT_DAEMON_DOWN,
    "board_locked": EXIT_DAEMON_DOWN,
    "wait_no_answer": EXIT_NO_ANSWER,
}


def exit_code_for(code: str, default: int = EXIT_REFUSED) -> int:
    """Return the contract exit code for ``code``."""
    return EXIT_CODE_FOR.get(code, default)


class HerdrTeamError(Exception):
    """Any refusal, validation failure, or environment problem.

    ``code`` is the stable machine-readable identifier, ``message`` the human
    text, ``exit_code`` the process exit status (derived from ``code`` when
    omitted), ``details`` extra JSON fields merged into the error object.
    """

    def __init__(
        self,
        code: str,
        message: str,
        exit_code: Optional[int] = None,
        details: Optional[Dict[str, Any]] = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.exit_code = exit_code if exit_code is not None else exit_code_for(code)
        self.details: Dict[str, Any] = dict(details or {})

    def to_json(self) -> Dict[str, Any]:
        """The JSON object printed on stderr for this error."""
        obj: Dict[str, Any] = {"code": self.code, "message": self.message}
        for key, value in self.details.items():
            if key not in obj:
                obj[key] = value
        return obj

    def __str__(self) -> str:
        return "{}: {}".format(self.code, self.message)


class UsageError(HerdrTeamError):
    """Bad arguments: exit 2."""

    def __init__(self, message: str, details: Optional[Dict[str, Any]] = None) -> None:
        super().__init__("usage", message, EXIT_USAGE, details)


class LockTimeout(HerdrTeamError):
    """A bounded lock wait expired: exit 5. ``details['lock']`` names the file."""

    def __init__(self, lock_path: str, timeout: float, code: str = "lock_timeout") -> None:
        super().__init__(
            code,
            "could not acquire {} within {:g}s".format(lock_path, timeout),
            EXIT_DAEMON_DOWN,
            {"lock": lock_path, "timeout_s": timeout},
        )


def error_json(err: BaseException) -> Dict[str, Any]:
    """JSON object for any exception; non-HerdrTeamError becomes ``internal``."""
    if isinstance(err, HerdrTeamError):
        return err.to_json()
    return {"code": "internal", "message": "{}: {}".format(type(err).__name__, err)}


def emit_error(err: BaseException, stream: Optional[IO[str]] = None) -> int:
    """Write the error JSON line to ``stream`` (stderr) and return its exit code."""
    out = stream if stream is not None else sys.stderr
    out.write(json.dumps(error_json(err), ensure_ascii=False, sort_keys=False) + "\n")
    out.flush()
    if isinstance(err, HerdrTeamError):
        return err.exit_code
    return EXIT_REFUSED
