"""Which model, and how much thinking, each member gets: the per-harness table.

Pure and I/O-free. Everything is argv, never a shell string, because Herdr's
``agent.start`` hands ``args`` to the binary verbatim and ``resume`` execs.

What the supported harnesses accept (verified on the installed
binaries, most recently 2026-09-16):

- **Claude Code** ``--model <alias|name>`` and ``--effort low|medium|high|xhigh|max``
  at launch and on ``--resume``; ``/model <m>`` and ``/effort <e>`` typed live.
- **Codex** ``-m <model>`` and ``-c model_reasoning_effort="<e>"`` (the value is
  TOML, hence the quotes -- its own help shows ``-c model="o3"``), on ``codex``
  and on ``codex resume <id>``; ``/model`` is a picker, so no live keystroke.
- **OpenCode** ``-m provider/model`` on both a fresh TUI and ``--session <id>``.
  The full TUI does *not* accept ``--variant`` (that flag belongs to
  ``opencode run``); select provider-specific effort through its native
  ``/variants`` dialog by typing the exact variant and pressing Enter.
- **Pi** ``--model provider/id`` and ``--thinking <level>`` on fresh starts
  and exact-path resumes. Model changes use the controlled restart path.

Synapse-managed Claude Code, Codex and OpenCode launches are unrestricted by default:
Claude gets ``--dangerously-skip-permissions``, Codex gets
``--dangerously-bypass-approvals-and-sandbox``, and OpenCode gets ``--auto``.
The same flags are rebuilt on exact-session resume, controlled model restart,
and OpenCode clear/restart instead of depending on a previous process argv.
An explicit native permission policy omits these flags.

Pi has no built-in tool permission prompts; ``--approve`` trusts project
resources for this run without changing global trust decisions.

The vocabulary is the harness's own, passed through untranslated: ``medium``
means whatever the harness means by it. A kind not listed here cannot carry a
model at all (``model_unsupported``), which is honest about what was verified.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from herdr_team import permissions as _permissions
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError, UsageError

KINDS: Tuple[str, ...] = ("claude", "codex", "opencode", "pi")

#: Effort words each harness understands. ``None`` means provider-specific:
#: any token is accepted and passed through.
EFFORTS: Dict[str, Optional[Tuple[str, ...]]] = {
    "claude": ("low", "medium", "high", "xhigh", "max"),
    "codex": ("minimal", "low", "medium", "high", "xhigh"),
    "opencode": None,
    "pi": ("off", "minimal", "low", "medium", "high", "xhigh", "max"),
}

#: Full-auto execution is the plugin default for every fully supported kind.
#: Keep the spelling aligned with the installed harness CLIs; these are argv
#: elements passed directly to Herdr, never shell fragments.
UNRESTRICTED_ARGS = _permissions.YOLO_ARGS

#: What ends the harness cleanly, so the notifier can resume it with new flags.
EXIT_KEYSTROKE: Dict[str, str] = {"claude": "/exit", "codex": "/quit", "opencode": "/exit", "pi": "/quit"}

#: Claude's short aliases, for matching a request against the transcript's full name.
CLAUDE_ALIASES: Tuple[str, ...] = ("opus", "sonnet", "haiku", "fable")

MAX_TOKEN_CHARS = 80
_TOKEN_OK = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-/:")

# Non-permission runtime switches worth carrying across a model-change restart.
# Permission, approval and sandbox selectors are deliberately absent: every
# supported kind is rebuilt from its saved permission policy. This remains an allowlist
# because copying an arbitrary process argv into the board could persist
# secrets supplied through unrelated CLI options.
_PRESERVED_FLAGS: Dict[str, Dict[str, int]] = {
    "pi": {
        "--session-dir": 1, "--extension": 1, "-e": 1, "--no-extensions": 0,
        "--skill": 1, "--no-skills": 0, "--tools": 1, "--exclude-tools": 1,
        "--no-tools": 0, "--no-builtin-tools": 0, "--tui-mode": 1,
        "--offline": 0, "--no-context-files": 0,
    },
    "claude": {
        "--settings": 1,
    },
    "codex": {
        "--dangerously-bypass-hook-trust": 0,
        "--strict-config": 0,
        "--oss": 0,
        "--local-provider": 1,
        "-p": 1,
        "--profile": 1,
        "-C": 1,
        "--cd": 1,
        "--add-dir": 1,
        "--search": 0,
        "--no-alt-screen": 0,
    },
    "opencode": {
        "--pure": 0,
        "--agent": 1,
        "--mini": 0,
        "--no-replay": 0,
        "--replay-limit": 1,
    },
}


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


def launch_args(kind: Any, model: Optional[str], effort: Optional[str], permissions: str = "yolo") -> List[str]:
    """Model flags and the saved permission policy (YOLO by default)."""
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
    elif key == "pi":
        if model:
            out += ["--model", model]
        if effort:
            out += ["--thinking", effort]
    out += _permissions.launch_args(key, permissions)
    return out


def resume_argv(kind: Any, session: Any, model: Optional[str], effort: Optional[str], permissions: str = "yolo") -> List[str]:
    """``roster.resume_argv(session)`` with the setting's flags appended."""
    from herdr_team import roster as _roster

    if kind == "pi" and (not isinstance(session, dict) or session.get("kind") != "path"
                         or not os.path.isabs(str(session.get("value") or ""))):
        raise HerdrTeamError("resume_unsupported", "Pi requires its recorded absolute session path", EXIT_REFUSED)
    return list(_roster.resume_argv(session)) + launch_args(kind, model, effort, permissions)


def foreground_argv(kind: Any, processes: Any) -> Optional[List[str]]:
    """The live harness argv in ``pane.process_info.foreground_processes``."""
    key = str(kind or "").strip().lower()
    if key not in KINDS or not isinstance(processes, list):
        return None
    for process in processes:
        if not isinstance(process, dict):
            continue
        argv = process.get("argv")
        if not isinstance(argv, list) or not argv or not all(isinstance(arg, str) for arg in argv):
            continue
        head = os.path.basename(argv[0]).lower()
        if head.endswith(".exe"):
            head = head[:-4]
        name = str(process.get("name") or "").lower()
        if head == key or name == key:
            return list(argv)
        if key == "pi" and head in ("node", "bun") and len(argv) > 1 and "pi-coding-agent/" in argv[1]:
            return ["pi"] + list(argv[2:])
    return None


def preserved_launch_args(kind: Any, current_argv: Any, permissions: str = "yolo") -> List[str]:
    """Allowlisted runtime-policy flags from a live harness argv.

    Session, model and effort selectors are intentionally absent from the
    allowlist because the restart builds those from the roster. Unknown flags
    are dropped instead of being copied into a durable board record.
    """
    key = str(kind or "").strip()
    mode = _permissions.validate(permissions)
    specs = _PRESERVED_FLAGS.get(key)
    if specs is None or not isinstance(current_argv, (list, tuple)):
        return []
    if mode == "native":
        specs = {flag: arity for flag, arity in specs.items() if flag != "--dangerously-bypass-hook-trust"}
    argv = [str(arg) for arg in current_argv]
    out: List[str] = []
    index = 1 if argv else 0
    while index < len(argv):
        token = argv[index]
        matched = False
        for flag, arity in specs.items():
            if token == flag:
                if arity == 0:
                    out.append(token)
                    index += 1
                elif index + 1 < len(argv):
                    value = argv[index + 1]
                    if len(value) <= 4096 and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
                        out.extend([token, value])
                    index += 2
                else:
                    index += 1
                matched = True
                break
            if arity == 1 and token.startswith(flag + "="):
                value = token[len(flag) + 1:]
                if value and len(value) <= 4096 and not any(ord(ch) < 32 or ord(ch) == 127 for ch in value):
                    out.append(token)
                index += 1
                matched = True
                break
        if not matched:
            index += 1
    return out


def restart_argv(kind: Any, session: Any, model: Optional[str], effort: Optional[str], current_argv: Any = None, permissions: str = "yolo") -> List[str]:
    """Exact controlled-resume argv plus safe live policy flags.

    Codex's version chooser is a legitimate startup blocker, but a notifier
    cannot decide whether to upgrade on the user's behalf. Disable that check
    for this one automated restart; ordinary launches and ``resume`` commands
    retain the user's normal update behavior.
    """
    key = str(kind or "").strip()
    out = resume_argv(key, session, model, effort, permissions)
    if key == "codex":
        out += ["-c", "check_for_update_on_startup=false"]
    return out + preserved_launch_args(key, current_argv, permissions)


def fresh_argv(kind: Any, model: Optional[str], effort: Optional[str], current_argv: Any = None, permissions: str = "yolo") -> List[str]:
    """A fresh harness argv with its setting and safe live policy retained."""
    key = str(kind or "").strip()
    validate(key, model, effort)
    return [key] + launch_args(key, model, effort, permissions) + preserved_launch_args(key, current_argv, permissions)


def live_keystrokes(kind: Any, model: Optional[str], effort: Optional[str]) -> Optional[List[str]]:
    """The lines a notifier types to change a running agent, or None when the kind has no typeable command."""
    validate(kind, model, effort)
    key = str(kind or "").strip()
    if key == "claude":
        lines: List[str] = []
        if model:
            lines.append("/model {}".format(model))
        if effort:
            lines.append("/effort {}".format(effort))
        return lines
    if key == "opencode" and model is None and effort:
        # /variants opens a searchable native picker. The second line filters
        # it to the exact provider-defined variant and Enter selects it.
        return ["/variants", effort]
    return None


def post_start_keystrokes(kind: Any, effort: Optional[str]) -> List[str]:
    """Native UI steps still required after argv starts or resumes a harness."""
    validate(kind, None, effort)
    if str(kind or "").strip() == "opencode" and effort:
        return ["/variants", effort]
    return []


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
    if key == "pi":
        return "/" not in want and want == have.split("/", 1)[-1]
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
