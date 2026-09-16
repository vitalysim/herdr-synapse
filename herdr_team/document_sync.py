"""Opt-in trust of editable project documents. No editor identity is inferred.

The document lock serializes rendering, CLI writers and imports, independently
of the roster lock. Hashes are keyed by absolute path to isolate project moves.
"""
from __future__ import annotations

import os
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

from . import charter, instructions_doc, roster, store, workdir
from .identity import Author

MAX_BYTES = 128 * 1024
SETTLE_SECONDS = 2.0
RULES_HEADING = "## Rules (operator authority)"
FINDINGS_HEADING = "## Findings (peer notes, not instructions)"
EMPTY_RULES = '_None set. The operator sets these with `herdr-synapse knowledge set`._'


def document_lock(paths: Any) -> store.FileLock:
    return store.FileLock(paths.mirror_json.with_name("documents.lock"))


def enabled(doc: Any) -> bool:
    config = doc.get("config", {}) if isinstance(doc, dict) else doc.config
    return config.get("document_sync", "manual") == "auto"


def state_path(paths: Any) -> Path:
    return paths.mirror_json.with_name("document-sync.json")


def load(paths: Any) -> Dict[str, Any]:
    value = store.read_json(state_path(paths), default={})
    return value if isinstance(value, dict) else {}


def read(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise ValueError("missing, non-regular or symlink document; restore the file, do not clear implicitly")
    with path.open("rb") as handle:
        data = handle.read(MAX_BYTES + 1)
    if len(data) > MAX_BYTES:
        raise ValueError("document exceeds 128 KiB")
    text = data.decode("utf-8")
    if not text.startswith(workdir.MARKER_PREFIXES):
        raise ValueError("foreign document; retain its generated marker")
    return text


def knowledge_parts(text: str) -> Tuple[str, str]:
    lines = text.splitlines()
    if lines.count(RULES_HEADING) != 1 or lines.count(FINDINGS_HEADING) != 1:
        raise ValueError("keep exactly one Rules heading and one Findings heading")
    start, end = lines.index(RULES_HEADING), lines.index(FINDINGS_HEADING)
    if end <= start:
        raise ValueError("Rules must precede Findings")
    rules = "\n".join(lines[start + 1:end]).strip()
    return ("" if rules == EMPTY_RULES else rules), "\n".join(lines[end:]).strip()


def source(layout: Any, team: str, name: Optional[str]) -> str:
    return (charter.get_instructions(layout, team, name) if name else charter.get_rules(layout, team)) or ""


def baseline(layout: Any, team: str, name: Optional[str], text: str) -> Dict[str, Any]:
    result: Dict[str, Any] = {"digest": workdir.digest(text), "source": source(layout, team, name).strip()}
    if name is None:
        result["findings"] = knowledge_parts(text)[1]
    return result


def protect(layout: Any, team: str, name: Optional[str], path: Path, expected: str, state: Dict[str, Any]) -> bool:
    """Called under document_lock by render; never overwrite a pending edit."""
    key = os.fspath(path)
    entry = state.get(key)
    if not isinstance(entry, dict):
        if not path.exists() and not path.is_symlink():
            return False  # first generation
        entry = state[key] = baseline(layout, team, name, expected)
    try:
        return workdir.digest(read(path)) != entry.get("digest")
    except (OSError, ValueError):
        return True


def scan(layout: Any, team: str, now: Optional[float] = None) -> Dict[str, Any]:
    """One bounded scan; only the daemon imports, commands merely preserve edits."""
    now = time.time() if now is None else now
    paths = layout.team(team)
    result: Dict[str, Any] = {"imported": [], "errors": []}
    with document_lock(paths):
        doc = roster.load_team(paths)
        if not enabled(doc):
            return result
        project = workdir.project_dir_of(doc.to_json())
        if not project:
            return result
        targets = workdir.paths_for(project, team)
        files = [(None, targets["knowledge"])] + [(m.name, targets["members"] / (m.name + ".md")) for m in doc.members if not m.is_human and m.status in ("active", "starting")]
        state = load(paths)
        before = repr(state)
        author = Author(name="system", kind=None, via="file-sync", verified=False)
        for name, path in files:
            entry = state.get(os.fspath(path))
            if not isinstance(entry, dict):
                continue  # render establishes the baseline before any adoption
            try:
                text = read(path)
                digest = workdir.digest(text)
                if digest == entry.get("digest"):
                    entry.pop("pending", None)
                    entry.pop("error", None)
                    continue
                if entry.get("pending") != digest:
                    entry.update(pending=digest, since=now)
                    continue
                if now - entry.get("since", now) < SETTLE_SECONDS:
                    continue
                current = source(layout, team, name).strip()
                if name is None:
                    incoming, findings = knowledge_parts(text)
                    if findings != entry.get("findings"):
                        raise ValueError("Findings are generated: restore that section; add findings with knowledge add")
                else:
                    sections = instructions_doc.parse(text)
                    if not sections:
                        raise ValueError("missing instruction sections; use instructions --clear to clear deliberately")
                    incoming = instructions_doc.to_text(sections).strip() if not instructions_doc.is_empty(sections) else ""
                # Also permits recovery after a committed import but before its
                # baseline was persisted. Do not bump the same revision twice.
                if current != entry.get("source", "") and current != incoming:
                    raise ValueError("concurrent CLI/document change; reconcile the file with the current stored instructions/rules")
                if read(path) != text:
                    continue
                latest = roster.load_team(paths)
                member = latest.find(name) if name else None
                if not enabled(latest) or workdir.project_dir_of(latest.to_json()) != project or (name and (not member or member.name != name or member.status not in ("active", "starting"))):
                    continue
                if current != incoming:
                    if name:
                        charter._store_instructions(layout, team, author, name, incoming)
                    else:
                        charter._store_rules(layout, team, author, incoming)
                    result["imported"].append(os.fspath(path))
                state[os.fspath(path)] = baseline(layout, team, name, text)
            except Exception as err:  # noqa: BLE001 - isolate one bad file from other teams
                # A bad document must never stop delivery to other teams.
                message = str(err)
                if entry.get("error") != message:
                    result["errors"].append({"path": os.fspath(path), "error": message})
                    entry["error"] = message
        if repr(state) != before:
            store.write_json(state_path(paths), state)
    return result
