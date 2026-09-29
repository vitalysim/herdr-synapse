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

Synapse-managed launches are unrestricted by default: Claude gets
``--dangerously-skip-permissions``, Codex gets
``--dangerously-bypass-approvals-and-sandbox``, and OpenCode gets ``--auto``;
every other kind with a known switch gets its own (``permissions.YOLO_ARGS``).
The same flags are rebuilt on exact-session resume, controlled model restart,
and OpenCode clear/restart instead of depending on a previous process argv.
An explicit native permission policy omits these flags.

While a team's canvas is on (0.21), a member Synapse starts also gets the
canvas MCP server at launch: ``features.mcp_launch_args`` appends Claude
Code's ``--mcp-config <file>`` or Codex's two ``-c
mcp_servers.synapse_canvas.*`` overrides **last**, so every launch path builds
the same flags (Pi has no such flag and uses the CLI). A controlled restart never recomputes them; it carries the
live ones through ``preserved_launch_args``, which keeps a value only when
``features.is_preserved_mcp_value`` recognises it as Synapse's own.

Pi has no built-in tool permission prompts; ``--approve`` trusts project
resources for this run without changing global trust decisions.

The vocabulary is the harness's own, passed through untranslated: ``medium``
means whatever the harness means by it. A kind not listed here cannot carry a
model at all (``model_unsupported``), which is honest about what was verified.
"""
from __future__ import annotations

import os
from typing import Any, Dict, List, Optional, Tuple

from herdr_team import features as _features
from herdr_team import permissions as _permissions
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError, UsageError

KINDS: Tuple[str, ...] = ("claude", "codex", "opencode", "pi")

#: Effort words each harness understands. ``None`` means provider-specific:
#: any token is accepted and passed through.
EFFORTS: Dict[str, Optional[Tuple[str, ...]]] = {
    "claude": ("low", "medium", "high", "xhigh", "max"),
    # The union across Codex models; each model accepts a subset, which
    # ``harnesses.check_model`` reads from Codex's own model cache.
    "codex": ("minimal", "low", "medium", "high", "xhigh", "max", "ultra"),
    "opencode": None,
    "pi": ("off", "minimal", "low", "medium", "high", "xhigh", "max"),
}

#: Full-auto execution is the plugin default for every kind with a known switch,
#: a superset of ``KINDS``. Keep the spelling aligned with the installed harness
#: CLIs; these are argv elements passed directly to Herdr, never shell fragments.
UNRESTRICTED_ARGS = _permissions.YOLO_ARGS

#: The flag that selects a harness's named setup, its *profile* in Synapse's
#: words: an OpenCode or Claude Code agent, or a Codex config profile
#: (``$CODEX_HOME/<name>.config.toml``). Every flag takes the name as its one
#: value and combines with the kind's resume form. ``harnesses`` lists the names.
#: Kimi 0.29 has ``--agent`` too, but refuses it outside ``kimi -p`` with an
#: experimental switch (live, 2026-09-26), so a member cannot use it.
PROFILE_FLAGS: Dict[str, str] = {"opencode": "--agent", "claude": "--agent", "codex": "-p"}
#: Spellings of the same selector a live argv may carry; dropped from a
#: restart's preserved flags when the roster names the profile itself.
_PROFILE_SPELLINGS: Dict[str, Tuple[str, ...]] = {"opencode": ("--agent",), "claude": ("--agent",), "codex": ("-p", "--profile")}
_PROFILE_OK = frozenset("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-:")

#: Flags a Synapse-managed launch must add so the harness runs its own tools
#: inside the pane's process tree, per kind.
#:
#: Codex attaches to a machine-wide ``app-server`` daemon unless told not to.
#: Everything it then spawns is a child of that daemon rather than of the pane,
#: and Synapse's whole authority model is the pane's process tree: without
#: ``--no-daemon`` ``identity.confirm_pane_ancestry`` cannot recognise the
#: caller, so every ``herdr-synapse`` command a codex member runs fails
#: ``pane_mismatch`` ("HERDR_PANE_ID names pane w1:p4 but ...") and the TUI
#: refuses to show the conversation twice ("this conversation is open in
#: another app"). Both were live on 2026-09-28 and both went away on a relaunch
#: with the flag, which is why this is a launch requirement and not a taste.
#:
#: Applied inside ``launch_args``, so the create path, ``fresh_argv``,
#: ``restart_argv`` and ``resume_argv`` inherit it from one place. Deliberately
#: *not* in ``roster.RESUME_COMMANDS``: that table is the bare "reopen this
#: conversation" spelling Herdr itself uses, and it is also how ``cmd_restore``
#: names the binary to look for on ``PATH``.
PANE_LOCAL_ARGS: Dict[str, Tuple[str, ...]] = {"codex": ("--no-daemon",)}

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


def validate_profile(kind: Any, profile: Any) -> Optional[str]:
    """A profile name the kind can take, or None; refuses a kind with no selector and an unsafe name."""
    if profile is None:
        return None
    text = str(profile).strip()
    if not text:
        return None
    key = str(kind or "").strip()
    if key not in PROFILE_FLAGS:
        raise HerdrTeamError(
            "profile_unsupported",
            "a {} agent has no profiles Synapse can select; profiles exist for {}".format(key or "?", ", ".join(sorted(PROFILE_FLAGS))),
            EXIT_REFUSED, {"kind": key, "supported": sorted(PROFILE_FLAGS)},
        )
    if len(text) > MAX_TOKEN_CHARS or text.startswith("-") or any(ch not in _PROFILE_OK for ch in text):
        raise UsageError("profile {!r} is not a profile name (letters, digits, . _ - : only)".format(text))
    return text


def profile_args(kind: Any, profile: Optional[str]) -> List[str]:
    name = validate_profile(kind, profile)
    return [PROFILE_FLAGS[str(kind).strip()], name] if name else []


def launch_args(kind: Any, model: Optional[str], effort: Optional[str], permissions: str = "yolo", profile: Optional[str] = None,
                mcp: Optional[Dict[str, Any]] = None) -> List[str]:
    """Profile, model flags, the pane-local flags, the saved permission policy (YOLO by default), then the canvas MCP flags.

    ``PANE_LOCAL_ARGS`` sits in the middle because every launch path funnels
    through here: that is what makes a codex member's tools run inside its own
    pane instead of a machine-wide daemon on all four of them.

    ``mcp`` is ``features.mcp_spec(...)`` for the member's team, None while
    its canvas is off; its flags go last so ``session_names.prepare`` can
    still append ``--name`` (which also ends Claude's variadic ``--mcp-config``).
    """
    validate(kind, model, effort)
    key = str(kind or "").strip()
    out: List[str] = profile_args(key, profile)
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
    out += list(PANE_LOCAL_ARGS.get(key, ()))
    out += _permissions.launch_args(key, permissions)
    out += _features.mcp_launch_args(key, mcp)
    return out


def resume_argv(kind: Any, session: Any, model: Optional[str], effort: Optional[str], permissions: str = "yolo", profile: Optional[str] = None,
                mcp: Optional[Dict[str, Any]] = None) -> List[str]:
    """``roster.resume_argv(session)`` with the setting's flags (and the canvas MCP flags, when given) appended."""
    from herdr_team import roster as _roster

    if kind == "pi" and (not isinstance(session, dict) or session.get("kind") != "path"
                         or not os.path.isabs(str(session.get("value") or ""))):
        raise HerdrTeamError("resume_unsupported", "Pi requires its recorded absolute session path", EXIT_REFUSED)
    return list(_roster.resume_argv(session)) + launch_args(kind, model, effort, permissions, profile, mcp=mcp)


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


def preserved_launch_args(kind: Any, current_argv: Any, permissions: str = "yolo", profile: Optional[str] = None) -> List[str]:
    """Allowlisted runtime-policy flags from a live harness argv.

    Session, model and effort selectors are intentionally absent from the
    allowlist because the restart builds those from the roster. Unknown flags
    are dropped instead of being copied into a durable board record. A live
    profile selector is kept only while the roster names none: once it does,
    the roster's profile is the one the restart passes. ``PANE_LOCAL_ARGS`` is
    excluded for the same reason: ``launch_args`` adds it unconditionally, so
    preserving it as well would duplicate the flag.
    """
    key = str(kind or "").strip()
    mode = _permissions.validate(permissions)
    specs = _PRESERVED_FLAGS.get(key)
    if specs is None or not isinstance(current_argv, (list, tuple)):
        return []
    if mode == "native":
        specs = {flag: arity for flag, arity in specs.items() if flag != "--dangerously-bypass-hook-trust"}
    if profile:
        specs = {flag: arity for flag, arity in specs.items() if flag not in _PROFILE_SPELLINGS.get(key, ())}
    pane_local = PANE_LOCAL_ARGS.get(key, ())
    if pane_local:
        # ``launch_args`` emits these on every path, so copying them from the
        # live argv too would put the flag in the command line twice.
        specs = {flag: arity for flag, arity in specs.items() if flag not in pane_local}
    argv = [str(arg) for arg in current_argv]
    mcp_flags = _features.PRESERVED_MCP_FLAGS.get(key, ())
    out: List[str] = []
    index = 1 if argv else 0
    while index < len(argv):
        token = argv[index]
        # Synapse's own canvas injection (0.21): kept only when the value is
        # recognisably ours, so a user's inline --mcp-config JSON (which may
        # hold secrets) or an unrelated -c override is dropped like any
        # unknown flag. Checked first because Codex's -c has no other entry.
        mcp = _mcp_pair(key, mcp_flags, argv, index)
        if mcp is not None:
            kept, index = mcp
            out.extend(kept)
            continue
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


def _mcp_pair(kind: str, flags: Tuple[str, ...], argv: List[str], index: int) -> Optional[Tuple[List[str], int]]:
    """``(kept tokens, next index)`` when ``argv[index]`` is one of the kind's MCP flags, else None.

    Both spellings are read: ``flag value`` (arity 1) and ``flag=value``.
    A value ``features.is_preserved_mcp_value`` does not accept is consumed
    and dropped.
    """
    token = argv[index]
    for flag in flags:
        if token == flag:
            if index + 1 >= len(argv) or argv[index + 1].startswith("-"):
                return [], index + 1  # no value: the next flag is examined on its own
            value = argv[index + 1]
            return ([token, value] if _features.is_preserved_mcp_value(kind, flag, value) else []), index + 2
        if token.startswith(flag + "="):
            value = token[len(flag) + 1:]
            return ([token] if _features.is_preserved_mcp_value(kind, flag, value) else []), index + 1
    return None


def _without_mcp(kind: str, preserved: List[str]) -> List[str]:
    """``preserved`` minus Synapse's canvas flags, for a launch that injects its own."""
    flags = _features.PRESERVED_MCP_FLAGS.get(kind, ())
    out: List[str] = []
    index = 0
    while index < len(preserved):
        pair = _mcp_pair(kind, flags, preserved, index)
        if pair is not None:
            index = pair[1]
            continue
        out.append(preserved[index])
        index += 1
    return out


def _has_run(tokens: List[str], wanted: Tuple[str, ...]) -> bool:
    """Does ``wanted`` appear in ``tokens`` as a contiguous run?

    A run, not a set: ``devin``'s policy flag is ``--permission-mode dangerous``
    and a live argv carrying ``--permission-mode ask`` must not read as if the
    bypass were already there.
    """
    if not wanted:
        return True
    span = len(wanted)
    return any(tuple(tokens[i:i + span]) == wanted for i in range(len(tokens) - span + 1))


def _mcp_settings_present(kind: str, tokens: List[str]) -> set:
    """Which canvas MCP settings a live argv already carries, by name.

    Recognition goes through ``features.is_preserved_mcp_value``, never a raw
    comparison with what ``mcp_launch_args`` would emit: the config file's path
    is stable but Codex's two ``-c`` values are JSON, and a member started
    before a ``team.json`` move would look unflagged over a cosmetic
    difference. Claude Code contributes the single name ``config_file``; Codex
    contributes one name per ``mcp_servers.synapse_canvas.<key>`` override it
    sets.
    """
    flags = _features.PRESERVED_MCP_FLAGS.get(kind, ())
    found: set = set()
    index = 1 if tokens else 0  # argv[0] is the binary
    while index < len(tokens):
        pair = _mcp_pair(kind, flags, tokens, index)
        if pair is None:
            index += 1
            continue
        kept, index = pair
        if not kept:
            continue
        value = kept[1] if len(kept) > 1 else kept[0].split("=", 1)[1]
        if kind == "claude":
            found.add("config_file")
        else:
            found.add(value[len(_features.CODEX_OVERRIDE_PREFIX):].split("=", 1)[0])
    return found


def missing_launch_flags(kind: Any, current_argv: Any, permissions: str = "yolo", mcp: Optional[Dict[str, Any]] = None) -> List[str]:
    """Behaviour-changing launch flags a live harness argv does not carry.

    Herdr's own session restore rebuilds a canned ``codex resume <id>`` or
    ``claude --resume <id>`` from the session reference alone. Everything
    ``launch_args`` adds is lost: the approval-bypass flag, the canvas MCP
    server, the pane-local flag. The member comes back looking healthy and then
    asks permission for every canvas command, or has no canvas door at all.
    Synapse cannot change Herdr, so it detects instead, and this is the pure
    predicate the detection is built on.

    Only flags whose absence *changes what the harness does* are counted, so a
    notice is never raised over a cosmetic difference: the kind's policy flag
    while the saved policy is not ``native``, the canvas MCP settings while the
    team's canvas is on, and ``PANE_LOCAL_ARGS``. Model, effort and profile are
    excluded on purpose -- they are the member's preference, they are visible in
    ``who``, and a wrong one is not a broken member.

    ``[]`` when there is no evidence either way (no argv, or a kind Synapse
    does not launch), because "unknown" must not read as "missing".
    """
    key = str(kind or "").strip()
    if not isinstance(current_argv, (list, tuple)) or not current_argv:
        return []
    tokens = [str(arg) for arg in current_argv]
    missing: List[str] = []
    for flag in PANE_LOCAL_ARGS.get(key, ()):
        if flag not in tokens:
            missing.append(flag)
    policy = _permissions.validate(permissions)
    wanted = tuple(_permissions.launch_args(key, policy))
    if wanted and not _has_run(tokens, wanted):
        missing.append(" ".join(wanted))
    if mcp and key in _features.MCP_KINDS:
        present = _mcp_settings_present(key, tokens)
        if key == "claude":
            if "config_file" not in present:
                missing.append(_features.PRESERVED_MCP_FLAGS["claude"][0])
        else:
            for override in _features.CODEX_OVERRIDE_KEYS:
                if override not in present:
                    missing.append("-c {}{}".format(_features.CODEX_OVERRIDE_PREFIX, override))
    return missing


def restart_argv(kind: Any, session: Any, model: Optional[str], effort: Optional[str], current_argv: Any = None, permissions: str = "yolo",
                 profile: Optional[str] = None, mcp: Optional[Dict[str, Any]] = None) -> List[str]:
    """Exact controlled-resume argv plus safe live policy flags.

    Codex's version chooser is a legitimate startup blocker, but a notifier
    cannot decide whether to upgrade on the user's behalf. Disable that check
    for this one automated restart; ordinary launches and ``resume`` commands
    retain the user's normal update behavior.

    ``mcp`` is for the one restart that exists *because* the live argv is
    wrong: ``restore --refresh-flags`` repairing a member Herdr brought back
    without Synapse's flags. Without it the canvas flags can only be carried
    over from the live argv, which is exactly what is missing there. With it
    the spec is injected and the preserved half is stripped of MCP flags, so
    they appear once -- the same rule ``fresh_argv`` follows.
    """
    key = str(kind or "").strip()
    out = resume_argv(key, session, model, effort, permissions, profile, mcp=mcp)
    if key == "codex":
        out += ["-c", "check_for_update_on_startup=false"]
    preserved = preserved_launch_args(key, current_argv, permissions, profile)
    if mcp:
        preserved = _without_mcp(key, preserved)
    return out + preserved


def fresh_argv(kind: Any, model: Optional[str], effort: Optional[str], current_argv: Any = None, permissions: str = "yolo",
               profile: Optional[str] = None, mcp: Optional[Dict[str, Any]] = None) -> List[str]:
    """A fresh harness argv with its profile, setting and safe live policy retained.

    With ``mcp`` the canvas flags come from that spec, never also from the
    live argv, so they appear once.
    """
    key = str(kind or "").strip()
    validate(key, model, effort)
    preserved = preserved_launch_args(key, current_argv, permissions, profile)
    if mcp:
        preserved = _without_mcp(key, preserved)
    return [key] + launch_args(key, model, effort, permissions, profile, mcp=mcp) + preserved


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
