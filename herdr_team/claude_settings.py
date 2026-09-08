"""Install and remove the team hooks in ``~/.claude/settings.json`` (plan 9.4).

Three separate entry objects: ``SessionStart`` (matcher ``*``, timeout 10),
``UserPromptSubmit`` (timeout 5), ``Stop`` (timeout 10), seconds. Edited
under ``store.claude_settings_lock``. Strict parse (duplicate keys,
comments, trailing commas rejected), ``json.dumps(indent=2)`` in existing
key order, self-check with the same duplicate-key rule, a backup, manual
instructions on parse failure, duplicate scan across all four Claude
settings files. Removal matches the exact command string of our own hook
path and drops an entry object only when its hooks array empties.

The entry shape mirrors Herdr's own installer
(``src/integration/claude_settings.rs``)::

    {"matcher": "*", "hooks": [{"type": "command", "command": "bash '<path>' <action>", "timeout": 10}]}

so the two installers can share one ``hooks`` object: each matches removals
by its own exact command string and never touches the other's entries.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from herdr_team import PLUGIN_ID, paths, store
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError

#: Claude Code hook event -> timeout in seconds.
HOOK_EVENTS = {"SessionStart": 10, "UserPromptSubmit": 5, "Stop": 10}
#: Claude Code hook event -> the action word the shim receives as ``$1``.
HOOK_ACTIONS = {"SessionStart": "session-start", "UserPromptSubmit": "prompt-submit", "Stop": "stop"}
#: Events that carry a ``matcher`` (Herdr uses ``*`` on SessionStart; the other two take none).
HOOK_MATCHERS = {"SessionStart": "*"}
HOOK_FILE_NAME = "herdr-synapse-hook.sh"
SETTINGS_FILES = ("settings.json", "settings.local.json")
BACKUP_SUFFIX = ".herdr-team.bak"
SHIM_MARKER = "# HERDR_TEAM_HOOK_VERSION="
SHIM_VERSION = 2
CLI_PLACEHOLDER = "@@HERDR_TEAM_CLI@@"
#: The shim logs to the plugin's own state directory, whose last path segment is
#: the plugin id. Baking the id in here rather than writing it in the shim keeps
#: the two from drifting: the 0.8.0 rename left the shim's fallback naming the
#: previous id, so a session whose state had moved logged nowhere.
PLUGIN_ID_PLACEHOLDER = "@@HERDR_TEAM_PLUGIN_ID@@"


class StrictJsonError(ValueError):
    """Duplicate keys, comments, trailing commas, or the wrong structure."""


# --------------------------------------------------------------------------
# strict JSON


def _pairs_hook(pairs: List[Tuple[str, Any]]) -> Dict[str, Any]:
    obj: Dict[str, Any] = {}
    for key, value in pairs:
        if key in obj:
            raise StrictJsonError('duplicate key "{}"'.format(key))
        obj[key] = value
    return obj


def parse_strict(text: str, what: str = "claude settings") -> Dict[str, Any]:
    """Parse ``text`` as strict JSON; the top level must be an object.

    Python's ``json`` already rejects comments, trailing commas, single
    quotes, and hexadecimal numbers; the pairs hook adds the duplicate-key
    rule Herdr's installer applies.
    """
    if not text.strip():
        return {}
    try:
        value = json.loads(text, object_pairs_hook=_pairs_hook)
    except StrictJsonError:
        raise
    except ValueError as err:
        raise StrictJsonError("{}: {}".format(what, err))
    if not isinstance(value, dict):
        raise StrictJsonError("{} must be a JSON object at the top level".format(what))
    hooks = value.get("hooks")
    if hooks is not None:
        if not isinstance(hooks, dict):
            raise StrictJsonError("{} hooks must be a JSON object".format(what))
        for event, entries in hooks.items():
            if not isinstance(entries, list):
                raise StrictJsonError("hook entries for {} must be an array".format(event))
    return value


def load_strict(path: Path) -> Dict[str, Any]:
    """Read and strictly parse ``path``; a missing or empty file is ``{}``."""
    try:
        text = Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except UnicodeDecodeError as err:
        raise StrictJsonError("{} is not UTF-8: {}".format(path, err))
    return parse_strict(text, os.fspath(path))


def dump(obj: Dict[str, Any]) -> str:
    """``json.dumps(indent=2)`` preserving key order, trailing newline."""
    return json.dumps(obj, indent=2, ensure_ascii=False) + "\n"


# --------------------------------------------------------------------------
# commands and entries


def shell_single_quote(value: str) -> str:
    """Same quoting Herdr's installer uses: ``'…'`` with ``'"'"'`` for embedded quotes."""
    return "'" + value.replace("'", "'\"'\"'") + "'"


def hook_command(hook_path: Path, event: str) -> str:
    """Single-quoted absolute path (spaces in HOME) plus the event's action word."""
    action = HOOK_ACTIONS.get(event, event)
    return "bash {} {}".format(shell_single_quote(os.fspath(hook_path)), action)


def own_commands(hook_path: Path) -> Dict[str, str]:
    return {event: hook_command(hook_path, event) for event in HOOK_EVENTS}


def entry_for(hook_path: Path, event: str) -> Dict[str, Any]:
    """The entry object we append for ``event``: matcher first (when any), then one command hook."""
    entry: Dict[str, Any] = {}
    matcher = HOOK_MATCHERS.get(event)
    if matcher is not None:
        entry["matcher"] = matcher
    entry["hooks"] = [{"type": "command", "command": hook_command(hook_path, event), "timeout": HOOK_EVENTS[event]}]
    return entry


def manual_entries(hook_path: Path) -> str:
    """The JSON a human pastes into ``settings.json`` under ``"hooks"`` when we refuse to edit."""
    return json.dumps({event: [entry_for(hook_path, event)] for event in HOOK_EVENTS}, indent=2)


def _is_command_hook(hook: Any, command: str) -> bool:
    return isinstance(hook, dict) and hook.get("type") == "command" and hook.get("command") == command


def _event_has_command(entries: List[Any], command: str) -> bool:
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        hooks = entry.get("hooks")
        if isinstance(hooks, list) and any(_is_command_hook(h, command) for h in hooks):
            return True
    return False


def _remove_commands(hooks: Dict[str, Any], commands: List[str]) -> List[str]:
    """Remove every command hook whose string is in ``commands``; returns the events touched.

    An entry object is dropped only when its ``hooks`` array empties; an
    event key is dropped only when its entries array empties.
    """
    touched: List[str] = []
    for event in list(hooks.keys()):
        entries = hooks.get(event)
        if not isinstance(entries, list):
            continue
        changed = False
        kept_entries: List[Any] = []
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                kept_entries.append(entry)
                continue
            before = len(entry["hooks"])
            entry["hooks"] = [h for h in entry["hooks"] if not any(_is_command_hook(h, c) for c in commands)]
            if len(entry["hooks"]) != before:
                changed = True
            if entry["hooks"]:
                kept_entries.append(entry)
        if changed:
            touched.append(event)
            if kept_entries:
                hooks[event] = kept_entries
            else:
                del hooks[event]
    return touched


# --------------------------------------------------------------------------
# file plumbing


def _real_target(settings_path: Path) -> Path:
    """Write through a symlinked ``settings.json`` (dotfile managers) rather than replacing the link."""
    p = Path(settings_path)
    if p.is_symlink():
        return Path(os.path.realpath(p))
    return p


def _read_text(path: Path) -> Optional[str]:
    try:
        return Path(path).read_text(encoding="utf-8")
    except FileNotFoundError:
        return None


def backup_path(settings_path: Path) -> Path:
    return Path(os.fspath(settings_path) + BACKUP_SUFFIX)


def _write_backup(settings_path: Path, original: Optional[str]) -> Optional[str]:
    if original is None:
        return None
    target = backup_path(settings_path)
    store.atomic_write(target, original.encode("utf-8"), mode=0o600)
    return os.fspath(target)


def _write_settings(settings_path: Path, doc: Dict[str, Any]) -> None:
    target = _real_target(settings_path)
    text = dump(doc)
    # Keep whatever mode the file had (Claude creates it 0644); 0600 for a new file.
    mode = 0o600
    try:
        mode = os.stat(target).st_mode & 0o777
    except FileNotFoundError:
        pass
    paths.ensure_dir(target.parent, 0o700)
    store.atomic_write(target, text.encode("utf-8"), mode=mode)


def _parse_or_refuse(settings_path: Path, hook_path: Path, action: str) -> Dict[str, Any]:
    try:
        return load_strict(settings_path)
    except StrictJsonError as err:
        raise HerdrTeamError(
            "settings_unparseable",
            "refusing to {} hooks: {} cannot be parsed strictly ({}); add or remove these entries by hand under \"hooks\"".format(
                action, settings_path, err
            ),
            EXIT_REFUSED,
            {"settings": os.fspath(settings_path), "manual": manual_entries(hook_path), "commands": own_commands(hook_path)},
        )


def _self_check(settings_path: Path, hook_path: Path, expect_present: bool) -> None:
    doc = load_strict(settings_path)  # raises StrictJsonError on a bad write
    hooks = doc.get("hooks") if isinstance(doc.get("hooks"), dict) else {}
    for event, command in own_commands(hook_path).items():
        present = _event_has_command(hooks.get(event, []) if isinstance(hooks, dict) else [], command)
        if present != expect_present:
            raise StrictJsonError("self-check: {} hook {} after write".format(event, "missing" if expect_present else "still present"))


def _restore(settings_path: Path, original: Optional[str]) -> None:
    target = _real_target(settings_path)
    if original is None:
        try:
            os.unlink(target)
        except FileNotFoundError:
            pass
        return
    store.atomic_write(target, original.encode("utf-8"), mode=0o600)


# --------------------------------------------------------------------------
# public operations


def install(settings_path: Path, hook_path: Path) -> Dict[str, Any]:
    """Idempotent; returns ``{"added": [...], "already": [...], "backup": path, "changed": bool}``."""
    settings_path = Path(settings_path)
    hook_path = Path(hook_path)
    with store.claude_settings_lock(settings_path):
        doc = _parse_or_refuse(settings_path, hook_path, "install")
        original = _read_text(_real_target(settings_path))
        hooks = doc.get("hooks")
        if hooks is None:
            hooks = {}
            doc["hooks"] = hooks
        added: List[str] = []
        already: List[str] = []
        for event, command in own_commands(hook_path).items():
            entries = hooks.get(event)
            if entries is None:
                entries = []
                hooks[event] = entries
            if _event_has_command(entries, command):
                already.append(event)
                continue
            entries.append(entry_for(hook_path, event))
            added.append(event)
        result: Dict[str, Any] = {
            "settings": os.fspath(settings_path),
            "hook": os.fspath(hook_path),
            "added": added,
            "already": already,
            "backup": None,
            "changed": bool(added),
        }
        if not added:
            return result
        result["backup"] = _write_backup(settings_path, original)
        _write_settings(settings_path, doc)
        try:
            _self_check(settings_path, hook_path, expect_present=True)
        except StrictJsonError as err:
            _restore(settings_path, original)
            raise HerdrTeamError("settings_selfcheck_failed", "restored {}: {}".format(settings_path, err), EXIT_REFUSED, {"settings": os.fspath(settings_path)})
        return result


def uninstall(settings_path: Path, hook_path: Path) -> Dict[str, Any]:
    """Remove only our three command hooks; ``{"removed": [...], "backup": path, "changed": bool}``."""
    settings_path = Path(settings_path)
    hook_path = Path(hook_path)
    with store.claude_settings_lock(settings_path):
        doc = _parse_or_refuse(settings_path, hook_path, "uninstall")
        original = _read_text(_real_target(settings_path))
        hooks = doc.get("hooks")
        result: Dict[str, Any] = {
            "settings": os.fspath(settings_path),
            "hook": os.fspath(hook_path),
            "removed": [],
            "backup": None,
            "changed": False,
        }
        if not isinstance(hooks, dict):
            return result
        removed = _remove_commands(hooks, list(own_commands(hook_path).values()))
        if not removed:
            return result
        result["removed"] = removed
        result["changed"] = True
        result["backup"] = _write_backup(settings_path, original)
        _write_settings(settings_path, doc)
        try:
            _self_check(settings_path, hook_path, expect_present=False)
        except StrictJsonError as err:
            _restore(settings_path, original)
            raise HerdrTeamError("settings_selfcheck_failed", "restored {}: {}".format(settings_path, err), EXIT_REFUSED, {"settings": os.fspath(settings_path)})
        return result


def check(settings_path: Path, hook_path: Path) -> Dict[str, Any]:
    """``{"installed": bool, "events": {...}, "duplicates": [...], "parse_error": str|None}`` without writing."""
    settings_path = Path(settings_path)
    hook_path = Path(hook_path)
    events: Dict[str, bool] = {event: False for event in HOOK_EVENTS}
    parse_error: Optional[str] = None
    try:
        doc = load_strict(settings_path)
    except StrictJsonError as err:
        doc = {}
        parse_error = str(err)
    hooks = doc.get("hooks") if isinstance(doc.get("hooks"), dict) else {}
    for event, command in own_commands(hook_path).items():
        events[event] = _event_has_command(hooks.get(event, []), command)
    duplicates = _duplicates_in([settings_path], own_commands(hook_path))
    return {
        "installed": all(events.values()),
        "events": events,
        "duplicates": duplicates,
        "parse_error": parse_error,
        "settings": os.fspath(settings_path),
        "hook": os.fspath(hook_path),
        "hook_exists": hook_path.is_file(),
    }


def _command_hooks(path: Path) -> List[Tuple[str, str]]:
    """``(event, command)`` for every command hook in ``path``; unparseable files contribute nothing."""
    found: List[Tuple[str, str]] = []
    try:
        doc = load_strict(path)
    except StrictJsonError:
        return found
    hooks = doc.get("hooks") if isinstance(doc.get("hooks"), dict) else {}
    for event, entries in hooks.items():
        if not isinstance(entries, list):
            continue
        for entry in entries:
            if not isinstance(entry, dict) or not isinstance(entry.get("hooks"), list):
                continue
            for hook in entry["hooks"]:
                if isinstance(hook, dict) and hook.get("type") == "command" and isinstance(hook.get("command"), str):
                    found.append((event, hook["command"]))
    return found


def _duplicates_in(files: List[Path], ours: Optional[Dict[str, str]] = None) -> List[Dict[str, Any]]:
    seen: Dict[Tuple[str, str], List[str]] = {}
    for path in files:
        if not Path(path).is_file():
            continue
        for event, command in _command_hooks(Path(path)):
            seen.setdefault((event, command), []).append(os.fspath(path))
    own_values = set((ours or {}).values())
    out: List[Dict[str, Any]] = []
    for (event, command), where in seen.items():
        if len(where) > 1:
            out.append({"event": event, "command": command, "files": where, "count": len(where), "ours": command in own_values})
    out.sort(key=lambda d: (d["event"], d["command"]))
    return out


def settings_files(claude_dir: Path, project_dirs: Optional[List[Path]] = None) -> List[Path]:
    """The four Claude settings files: user ``settings.json``/``settings.local.json`` plus the project pair."""
    files = [Path(claude_dir) / name for name in SETTINGS_FILES]
    for project in project_dirs or []:
        for name in SETTINGS_FILES:
            files.append(Path(project) / ".claude" / name)
    return files


def scan_duplicates(claude_dir: Path, project_dirs: Optional[List[Path]] = None, hook_path: Optional[Path] = None) -> List[Dict[str, Any]]:
    """Duplicate hook commands across user and project settings files (a hook run twice per event)."""
    ours = own_commands(Path(hook_path)) if hook_path is not None else None
    return _duplicates_in(settings_files(claude_dir, project_dirs), ours)


# --------------------------------------------------------------------------
# the shim file


def shim_source() -> Path:
    return paths.plugin_root() / "hooks" / "claude" / HOOK_FILE_NAME


def sh_single_quoted_body(value: str) -> str:
    """``value`` escaped for the inside of a single-quoted sh literal (``'`` becomes ``'\\''``)."""
    return value.replace("'", "'\\''")


def render_shim(cli_path: Path) -> str:
    """The shim text with the absolute CLI path baked in (sh-quoted; a ``'`` in the path stays literal)."""
    text = shim_source().read_text(encoding="utf-8")
    if CLI_PLACEHOLDER not in text:
        raise HerdrTeamError("shim_source_invalid", "{} lacks the {} placeholder".format(shim_source(), CLI_PLACEHOLDER), EXIT_REFUSED)
    path = os.fspath(cli_path)
    if "\n" in path or "\r" in path:
        raise HerdrTeamError("cli_path_invalid", "the CLI path may not contain a newline", EXIT_REFUSED, {"path": path})
    if PLUGIN_ID_PLACEHOLDER not in text:
        raise HerdrTeamError("shim_source_invalid", "{} lacks the {} placeholder".format(shim_source(), PLUGIN_ID_PLACEHOLDER), EXIT_REFUSED)
    text = text.replace(PLUGIN_ID_PLACEHOLDER, sh_single_quoted_body(PLUGIN_ID))
    return text.replace(CLI_PLACEHOLDER, sh_single_quoted_body(path))


def shim_is_ours(path: Path) -> bool:
    try:
        head = Path(path).read_text(encoding="utf-8", errors="replace")[:4096]
    except OSError:
        return False
    return SHIM_MARKER in head


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_shim(hooks_dir: Path, cli_path: Path) -> Path:
    """Write ``<hooks_dir>/herdr-synapse-hook.sh`` with the absolute CLI path baked in, 0700."""
    hooks_dir = Path(hooks_dir)
    target = hooks_dir / HOOK_FILE_NAME
    if target.exists() and not target.is_symlink() and not shim_is_ours(target):
        raise HerdrTeamError("hook_file_foreign", "{} exists and is not a herdr-synapse hook; remove it first".format(target), EXIT_REFUSED, {"path": os.fspath(target)})
    paths.ensure_dir(hooks_dir, 0o700)
    store.atomic_write(target, render_shim(Path(cli_path)).encode("utf-8"), mode=0o700)
    return target


def remove_shim(hooks_dir: Path) -> bool:
    """Delete our shim (never a foreign file); True when something was removed."""
    target = Path(hooks_dir) / HOOK_FILE_NAME
    if not target.is_file() or not shim_is_ours(target):
        return False
    os.unlink(target)
    return True
