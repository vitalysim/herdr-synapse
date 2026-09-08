"""Which model, and how much thinking, each member gets: the per-harness table.

Pure and I/O-free. Everything is argv, never a shell string, because Herdr's
``agent.start`` hands ``args`` to the binary verbatim and ``resume`` execs.

What the three supported harnesses accept (verified on the installed
binaries, 2026-09-08):

- **Claude Code** ``--model <alias|name>`` and ``--effort low|medium|high|xhigh|max``
  at launch and on ``--resume``; ``/model <m>`` and ``/effort <e>`` typed live.
- **Codex** ``-m <model>`` and ``-c model_reasoning_effort="<e>"`` (the value is
  TOML, hence the quotes -- its own help shows ``-c model="o3"``), on ``codex``
  and on ``codex resume <id>``; ``/model`` is a picker, so no live keystroke.
- **OpenCode** ``-m provider/model`` and ``--variant <e>`` (provider-specific
  reasoning effort); on ``--session <id>`` too; ``/models`` is a picker.

The vocabulary is the harness's own, passed through untranslated: ``medium``
means whatever the harness means by it. A kind not listed here cannot carry a
model at all (``model_unsupported``), which is honest about what was verified.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from herdr_team.errors import EXIT_REFUSED, HerdrTeamError, UsageError

KINDS: Tuple[str, ...] = ("claude", "codex", "opencode")

#: Effort words each harness understands. ``None`` means provider-specific:
#: any token is accepted and passed through.
EFFORTS: Dict[str, Optional[Tuple[str, ...]]] = {
    "claude": ("low", "medium", "high", "xhigh", "max"),
    "codex": ("minimal", "low", "medium", "high", "xhigh"),
    "opencode": None,
}

#: What ends the harness cleanly, so the notifier can resume it with new flags.
EXIT_KEYSTROKE: Dict[str, str] = {"claude": "/exit", "codex": "/quit", "opencode": "/exit"}

#: Claude's short aliases, for matching a request against the transcript's full name.
CLAUDE_ALIASES: Tuple[str, ...] = ("opus", "sonnet", "haiku", "fable")

MAX_TOKEN_CHARS = 80
_TOKEN_OK = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-/:")


def _token(value: Any, what: str) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if len(text) > MAX_TOKEN_CHARS or any(ch not in _TOKEN_OK for ch in text):
        raise UsageError("{} {!r} is not a model or effort token (letters, digits, . _ - / : only)".format(what, text))
    return text


def parse_setting(text: Any) -> Tuple[Optional[str], Optional[str]]:
    """``"opus@medium"`` -> ``("opus", "medium")``; either half may be absent (``"opus"``, ``"@high"``)."""
    raw = str(text or "").strip()
    if not raw:
        raise UsageError("a setting looks like <model>[@<effort>], for example opus@medium or @high")
    if raw.count("@") > 1:
        raise UsageError("a setting has at most one @: <model>[@<effort>]")
    model_part, _, effort_part = raw.partition("@")
    model = _token(model_part, "model")
    effort = _token(effort_part, "effort")
    if model is None and effort is None:
        raise UsageError("a setting looks like <model>[@<effort>], for example opus@medium or @high")
    return model, effort


def label(model: Optional[str], effort: Optional[str]) -> Optional[str]:
    """The one-token form users type: ``opus@medium``, ``opus``, ``@high``, or None."""
    if model and effort:
        return "{}@{}".format(model, effort)
    if model:
        return model
    if effort:
        return "@{}".format(effort)
    return None


def validate(kind: Any, model: Optional[str], effort: Optional[str]) -> None:
    """Refuse what the harness would not understand, naming what it would."""
    if model is None and effort is None:
        return
    key = str(kind or "").strip()
    if key not in KINDS:
        raise HerdrTeamError(
            "model_unsupported",
            "no verified model or effort flags for a {} agent; supported: {}".format(key or "?", ", ".join(KINDS)),
            EXIT_REFUSED, {"kind": key, "supported": list(KINDS)},
        )
    words = EFFORTS.get(key)
    if effort is not None and words is not None and effort not in words:
        raise HerdrTeamError(
            "effort_unknown",
            "{} does not know the effort {!r}; it takes {}".format(key, effort, ", ".join(words)),
            EXIT_REFUSED, {"kind": key, "effort": effort, "efforts": list(words)},
        )


def launch_args(kind: Any, model: Optional[str], effort: Optional[str]) -> List[str]:
    """The flags to append to the harness's own argv for this setting (empty when nothing is set)."""
    validate(kind, model, effort)
    key = str(kind or "").strip()
    out: List[str] = []
    if key == "claude":
        if model:
            out += ["--model", model]
        if effort:
            out += ["--effort", effort]
    elif key == "codex":
        if model:
            out += ["-m", model]
        if effort:
            out += ["-c", 'model_reasoning_effort="{}"'.format(effort)]
    elif key == "opencode":
        if model:
            out += ["-m", model]
        if effort:
            out += ["--variant", effort]
    return out


def resume_argv(kind: Any, session: Any, model: Optional[str], effort: Optional[str]) -> List[str]:
    """``roster.resume_argv(session)`` with the setting's flags appended."""
    from herdr_team import roster as _roster

    return list(_roster.resume_argv(session)) + launch_args(kind, model, effort)


def live_keystrokes(kind: Any, model: Optional[str], effort: Optional[str]) -> Optional[List[str]]:
    """The lines a notifier types to change a running agent, or None when the kind has no typeable command."""
    validate(kind, model, effort)
    if str(kind or "").strip() != "claude":
        return None
    lines: List[str] = []
    if model:
        lines.append("/model {}".format(model))
    if effort:
        lines.append("/effort {}".format(effort))
    return lines


def exit_keystroke(kind: Any) -> str:
    key = str(kind or "").strip()
    try:
        return EXIT_KEYSTROKE[key]
    except KeyError:
        raise HerdrTeamError("model_unsupported", "no verified exit command for a {} agent; supported: {}".format(key or "?", ", ".join(KINDS)),
                             EXIT_REFUSED, {"kind": key, "supported": list(KINDS)})


def observed_matches(kind: Any, requested: Optional[str], observed: Optional[str]) -> bool:
    """Does the model the harness reports satisfy the one that was requested?

    Claude reports the full name (``claude-opus-5``) for an alias request
    (``opus``); OpenCode reports the bare id for a ``provider/model`` request.
    """
    if not requested or not observed:
        return False
    want = requested.strip().lower()
    have = observed.strip().lower()
    if want == have:
        return True
    key = str(kind or "").strip()
    if key == "claude":
        if want in CLAUDE_ALIASES:
            return have.startswith("claude-" + want)
        return have.startswith(want)
    if key == "opencode":
        return want.rsplit("/", 1)[-1] == have.rsplit("/", 1)[-1]
    return False


def effective_setting(config: Any, member: Any) -> Tuple[Optional[str], Optional[str]]:
    """Member override, else the team default for its kind, else nothing.

    ``config`` is ``team.json`` ``config`` (``{"models": {"<kind>": {"model", "effort"}}}``);
    ``member`` is a roster row (dict or ``Member``). Each half resolves on its own,
    so a member with only an effort override still takes the team's model.
    """
    get = (lambda k: member.get(k)) if isinstance(member, dict) else (lambda k: getattr(member, k, None))
    kind = str(get("kind") or "")
    defaults = ((config or {}).get("models") or {}).get(kind) if isinstance(config, dict) else None
    defaults = defaults if isinstance(defaults, dict) else {}
    model = get("model") or defaults.get("model") or None
    effort = get("effort") or defaults.get("effort") or None
    return (str(model) if model else None, str(effort) if effort else None)


def source_of(config: Any, member: Any) -> Tuple[str, str]:
    """Where each half of the effective setting comes from: ``member``, ``default``, or ``harness``."""
    get = (lambda k: member.get(k)) if isinstance(member, dict) else (lambda k: getattr(member, k, None))
    kind = str(get("kind") or "")
    defaults = ((config or {}).get("models") or {}).get(kind) if isinstance(config, dict) else None
    defaults = defaults if isinstance(defaults, dict) else {}

    def one(key: str) -> str:
        if get(key):
            return "member"
        if defaults.get(key):
            return "default"
        return "harness"

    return one("model"), one("effort")
