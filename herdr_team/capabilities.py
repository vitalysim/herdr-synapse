"""Runtime capability discovery for optional Herdr API methods.

Herdr release numbers are not capability identifiers: a fork and the
canonical binary may report the same version while exposing different API
methods.  Probes in this module are deliberately side-effect free and inspect
the live server response rather than the installed CLI's static schema.
"""

from __future__ import annotations

from typing import Any, Dict, Optional

from herdr_team.errors import HerdrTeamError

ATOMIC_IDLE_PROMPT = "atomic_idle_prompt"
ATOMIC_IDLE_PROMPT_METHOD = "agent.prompt_if_idle"

# The method rejects an empty prompt before resolving the target.  A server
# that knows the method therefore returns ``empty_agent_prompt``; an older
# server fails while decoding the method name.  No terminal bytes can be
# queued by either path.
_PROBE_PARAMS: Dict[str, Any] = {
    "target": "__herdr_synapse_capability_probe__",
    "text": "",
    "expected_terminal_id": "__probe__",
    "expected_state_change_seq": 0,
}
_METHOD_MISSING_CODES = frozenset({"invalid_request", "unknown_method", "unsupported_method", "method_not_found"})
_UNREACHABLE_CODES = frozenset({"server_not_running", "herdr_timeout", "herdr_not_found"})


def probe_atomic_idle_prompt(api: Any, timeout: float = 2.0) -> Optional[bool]:
    """Return whether the *running server* implements atomic idle-only prompts.

    ``None`` means the probe itself could not obtain a trustworthy answer.
    ``request_raw`` matters here: older Herdr versions answer an unknown method
    with an empty response id, which the ordinary client correctly reports as
    a protocol mismatch before exposing the useful ``invalid_request`` code.
    """

    try:
        response = api.request_raw(ATOMIC_IDLE_PROMPT_METHOD, dict(_PROBE_PARAMS), timeout=timeout)
    except HerdrTeamError as err:
        if err.code in _UNREACHABLE_CODES or err.code == "herdr_protocol":
            return None
        return False if err.code in _METHOD_MISSING_CODES else True
    if not isinstance(response, dict):
        return None
    error = response.get("error")
    if isinstance(error, dict):
        code = str(error.get("code") or "")
        if code in _UNREACHABLE_CODES or code == "herdr_protocol":
            return None
        return False if code in _METHOD_MISSING_CODES else True
    return True if isinstance(response.get("result"), dict) else None


def capability_map(atomic_idle_prompt: Optional[bool]) -> Dict[str, Optional[bool]]:
    """The stable ``daemon.json`` capability object."""

    return {ATOMIC_IDLE_PROMPT: atomic_idle_prompt}


def atomic_idle_prompt_of(doc: Any) -> Optional[bool]:
    """Read the optional capability from a daemon/status JSON object."""

    if not isinstance(doc, dict):
        return None
    capabilities = doc.get("capabilities")
    if not isinstance(capabilities, dict):
        return None
    value = capabilities.get(ATOMIC_IDLE_PROMPT)
    return value if isinstance(value, bool) else None


def unavailable_detail(member: Optional[str] = None, error: Optional[str] = None) -> str:
    """Actionable refusal text for safe ``!`` on a server without the method."""

    target = member or "name"
    detail = (
        "the running Herdr server does not provide atomic idle-only delivery; "
        "post with @{0} text for safe board delivery, or use !!{0} text only when forced terminal input is intentional".format(target)
    )
    return "{} ({})".format(detail, error) if error else detail
