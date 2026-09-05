"""Command group: ``skill install [--check] [--force]`` and ``skill check`` (plan 9.3).

Reproduces the layout ``npx skills add … -g`` leaves behind, offline:

* canonical copy ``~/.agents/skills/herdr-team/SKILL.md``
* a real copy in ``~/.claude/skills/herdr-team/SKILL.md``
* symlinks ``~/.codex/skills/herdr-team``, ``~/.copilot/skills/herdr-team``,
  ``~/.gemini/skills/herdr-team`` -> the canonical directory, created only
  when the agent's home dir (``~/.codex`` etc.) exists; ``~/.codex/skills/.system``
  is never touched.

A directory or symlink that is not ours (no version marker, or a link to
somewhere else) is left alone with ``reason: foreign`` unless ``--force``.
``skill check`` compares SHA-256 hashes of every installed copy and link
target against the bundled skill and lists the stale ones.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import re
import shutil
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from herdr_team import SKILL_VERSION, paths
from herdr_team import cli as _cli
from herdr_team.cli import emit, read_skill_text
from herdr_team.errors import EXIT_REFUSED, HerdrTeamError

SKILL_NAME = "herdr-team"
SKILL_FILE = "SKILL.md"
MARKER_RE = re.compile(r"<!--\s*herdr-team skill v(\d+)")
CANONICAL_ROOT = ".agents"
COPY_ROOTS = (".claude",)
SYMLINK_ROOTS = (".codex", ".copilot", ".gemini")
SKIP_ENTRIES = (".system",)



def skill_version_of(text: str) -> Optional[int]:
    """The ``<!-- herdr-team skill vN … -->`` marker's N, or None when absent."""
    match = MARKER_RE.search(text)
    return int(match.group(1)) if match else None


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _read(path: Path) -> Optional[str]:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def _is_ours_dir(directory: Path) -> bool:
    """A skill directory we may replace: holds a SKILL.md carrying our marker."""
    text = _read(directory / SKILL_FILE)
    return text is not None and skill_version_of(text) is not None


def _home(args: argparse.Namespace) -> Path:
    override = getattr(args, "home", None)
    if override:
        return Path(os.path.expanduser(override))
    return paths.home_dir(args.env)


def _skill_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("action", choices=("install", "check"))
    parser.add_argument("--check", action="store_true", help="install: only report what install would do")
    parser.add_argument("--force", action="store_true", help="replace foreign directories or symlinks")
    parser.add_argument("--home", metavar="PATH", help="root holding .agents/.claude/.codex/... (default $HOME)")


class _Plan:
    def __init__(self, home: Path, source_text: str) -> None:
        self.home = home
        self.source_text = source_text
        self.source_hash = sha256(source_text)
        self.canonical_dir = home / CANONICAL_ROOT / "skills" / SKILL_NAME
        self.installed: List[Dict[str, Any]] = []
        self.skipped: List[Dict[str, Any]] = []
        self.stale: List[Dict[str, Any]] = []
        self.errors: List[str] = []

    # -- inspection -------------------------------------------------------------

    def copy_state(self, directory: Path) -> str:
        """``missing | ours | current | foreign_symlink | foreign``."""
        if directory.is_symlink():
            return "foreign_symlink"
        if not directory.exists():
            return "missing"
        if not directory.is_dir():
            return "foreign"
        if not _is_ours_dir(directory):
            return "foreign"
        text = _read(directory / SKILL_FILE) or ""
        return "current" if sha256(text) == self.source_hash else "ours"

    def link_state(self, link: Path) -> str:
        """``missing | current | ours | foreign_symlink | foreign``."""
        if link.is_symlink():
            try:
                target = Path(os.path.realpath(link))
            except OSError:
                return "foreign_symlink"
            if target == Path(os.path.realpath(self.canonical_dir)):
                return "current"
            return "foreign_symlink"
        if not link.exists():
            return "missing"
        if link.is_dir() and _is_ours_dir(link):
            return "ours"  # an older layout wrote a copy here
        return "foreign"

    # -- actions ----------------------------------------------------------------

    def write_copy(self, directory: Path, dry_run: bool, force: bool, kind: str = "copy") -> None:
        state = self.copy_state(directory)
        entry = {"path": os.fspath(directory / SKILL_FILE), "kind": kind}
        if state in ("foreign", "foreign_symlink") and not force:
            self.skipped.append({"path": os.fspath(directory), "reason": state})
            return
        if state == "current":
            self.installed.append(dict(entry, state="unchanged"))
            return
        if dry_run:
            self.installed.append(dict(entry, state="would_write"))
            if state == "ours":
                self.stale.append({"path": entry["path"], "reason": "hash_mismatch"})
            return
        try:
            if state == "foreign_symlink":
                os.unlink(directory)
            elif state == "foreign" and directory.exists():
                if directory.is_dir():
                    shutil.rmtree(directory)
                else:
                    os.unlink(directory)
            paths.ensure_dir(directory, 0o755)
            target = directory / SKILL_FILE
            tmp = directory / (".tmp-" + SKILL_FILE)
            tmp.write_text(self.source_text, encoding="utf-8")
            os.chmod(tmp, 0o644)
            os.replace(tmp, target)
        except OSError as err:
            self.errors.append("{}: {}".format(directory, err))
            self.skipped.append({"path": os.fspath(directory), "reason": "os_error: {}".format(err)})
            return
        self.installed.append(dict(entry, state="written"))

    def write_link(self, link: Path, dry_run: bool, force: bool) -> None:
        state = self.link_state(link)
        entry = {"path": os.fspath(link), "kind": "symlink", "target": os.fspath(self.canonical_dir)}
        if state in ("foreign", "foreign_symlink") and not force:
            self.skipped.append({"path": os.fspath(link), "reason": state})
            return
        if state == "current":
            self.installed.append(dict(entry, state="unchanged"))
            return
        if dry_run:
            self.installed.append(dict(entry, state="would_link"))
            return
        try:
            if link.is_symlink():
                os.unlink(link)
            elif link.is_dir():
                shutil.rmtree(link)
            elif link.exists():
                os.unlink(link)
            paths.ensure_dir(link.parent, 0o755)
            os.symlink(os.fspath(self.canonical_dir), os.fspath(link))
        except OSError as err:
            self.errors.append("{}: {}".format(link, err))
            self.skipped.append({"path": os.fspath(link), "reason": "os_error: {}".format(err)})
            return
        self.installed.append(dict(entry, state="linked"))

    # -- check ------------------------------------------------------------------

    def check_copy(self, directory: Path, kind: str = "copy") -> None:
        state = self.copy_state(directory)
        entry = {"path": os.fspath(directory / SKILL_FILE), "kind": kind}
        if state == "missing":
            self.skipped.append({"path": os.fspath(directory), "reason": "missing"})
        elif state == "current":
            self.installed.append(dict(entry, state="current"))
        elif state == "ours":
            text = _read(directory / SKILL_FILE) or ""
            self.installed.append(dict(entry, state="stale"))
            self.stale.append({"path": entry["path"], "reason": "hash_mismatch", "version": skill_version_of(text)})
        else:
            self.skipped.append({"path": os.fspath(directory), "reason": state})

    def check_link(self, link: Path) -> None:
        state = self.link_state(link)
        entry = {"path": os.fspath(link), "kind": "symlink", "target": os.fspath(self.canonical_dir)}
        if state == "missing":
            self.skipped.append({"path": os.fspath(link), "reason": "missing"})
        elif state == "current":
            self.installed.append(dict(entry, state="current"))
        elif state == "ours":
            self.installed.append(dict(entry, state="stale", kind="copy"))
            self.stale.append({"path": os.fspath(link), "reason": "copy_instead_of_symlink"})
        else:
            self.skipped.append({"path": os.fspath(link), "reason": state})


def _targets(plan: _Plan) -> Dict[str, List[Path]]:
    """Copy and symlink targets for the agent dirs that exist; absent agents are reported, not created."""
    home = plan.home
    copies: List[Path] = []
    links: List[Path] = []
    for root in COPY_ROOTS + SYMLINK_ROOTS:
        if not (home / root).is_dir():
            plan.skipped.append({"path": os.fspath(home / root / "skills" / SKILL_NAME), "reason": "agent_dir_missing"})
            continue
        (copies if root in COPY_ROOTS else links).append(home / root / "skills" / SKILL_NAME)
    return {"copies": copies, "links": links}


def _payload(plan: _Plan, ok: bool) -> Dict[str, Any]:
    return {
        "version": SKILL_VERSION,
        "ok": ok,
        "source": os.fspath(paths.skill_file()),
        "hash": plan.source_hash,
        "canonical": os.fspath(plan.canonical_dir / SKILL_FILE),
        "installed": plan.installed,
        "skipped": plan.skipped,
        "stale": plan.stale,
        "errors": plan.errors,
    }


def _human(payload: Dict[str, Any], action: str) -> str:
    lines = ["skill {} (v{}): {}".format(action, payload["version"], "ok" if payload["ok"] else "attention needed")]
    for entry in payload["installed"]:
        lines.append("  {:<10} {:<8} {}".format(entry.get("state", ""), entry["kind"], entry["path"]))
    for entry in payload["skipped"]:
        lines.append("  skipped    {:<8} {} ({})".format("", entry["path"], entry["reason"]))
    for entry in payload["stale"]:
        lines.append("  stale      {:<8} {} ({})".format("", entry["path"], entry["reason"]))
    for err in payload["errors"]:
        lines.append("  error: " + err)
    if any(s["reason"] in ("foreign", "foreign_symlink") for s in payload["skipped"]):
        lines.append("  foreign entries are left alone; rerun with --force to replace them")
    return "\n".join(lines) + "\n"


def run_skill(args: argparse.Namespace) -> int:
    source_text = read_skill_text()
    if skill_version_of(source_text) != SKILL_VERSION:
        raise HerdrTeamError("skill_version_mismatch", "bundled SKILL.md marker does not say v{}".format(SKILL_VERSION), EXIT_REFUSED)
    home = _home(args)
    plan = _Plan(home, source_text)
    targets = _targets(plan)
    if args.action == "check":
        plan.check_copy(plan.canonical_dir, kind="canonical")
        for directory in targets["copies"]:
            plan.check_copy(directory)
        for link in targets["links"]:
            plan.check_link(link)
        ok = plan.copy_state(plan.canonical_dir) == "current" and not plan.stale and not any(
            s["reason"] in ("foreign", "foreign_symlink") for s in plan.skipped
        )
        payload = _payload(plan, ok)
        return emit(args, payload, lambda: _human(payload, "check"))
    dry_run = bool(args.check)
    force = bool(args.force)
    plan.write_copy(plan.canonical_dir, dry_run, force, kind="canonical")
    for directory in targets["copies"]:
        plan.write_copy(directory, dry_run, force)
    for link in targets["links"]:
        plan.write_link(link, dry_run, force)
    ok = not plan.errors and not any(s["reason"] in ("foreign", "foreign_symlink") for s in plan.skipped)
    payload = _payload(plan, ok)
    payload["dry_run"] = dry_run
    return emit(args, payload, lambda: _human(payload, "install --check" if dry_run else "install"))


Command = _cli.Command

COMMANDS: List[Command] = [
    Command(
        name="skill",
        help="install the herdr-team skill into the agents' skill dirs, or check it",
        add_arguments=_skill_args,
        run=run_skill,
        description="skill install [--check] [--force] | skill check. Canonical ~/.agents/skills/herdr-team, copy in ~/.claude/skills, symlinks from ~/.codex, ~/.copilot, ~/.gemini when those dirs exist.",
    ),
]
