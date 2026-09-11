"""The team's working directory inside the project: ``.herdr-synapse/<team>/``.

Agents in one folder all read the same ``CLAUDE.md`` or ``AGENTS.md``, so
nothing on disk tells them apart. This module is the agent-facing half of the
team: a folder people and agents can both reach with plain relative paths,
holding a rendered mirror of the knowledge base and of each member's
instructions, plus an ``artifacts/`` directory agents own outright.

Four rules make it safe to put in a repository agents can write to:

1. **The mirror is never truth.** The authoritative copies live in the team's
   state dir, where only the CLI writes them and authorship is stamped from
   the pane. Every read that feeds an agent's context comes from there. A
   hand-edited mirror is reported as drifted and overwritten on the next
   render, never imported. Without this an agent could edit a file and have
   it read back to its teammates as the operator's instruction.
2. **Consent is explicit.** ``config.project_dir`` is empty until a human runs
   ``project set``. The directory is never inferred from member cwds, so the
   plugin cannot pick the wrong repository or write into two of them.
3. **Nothing here is ever deleted.** Removing or renaming a member rewrites
   its file as a tombstone. ``project clear`` stops the plugin writing and
   leaves every file in place.
4. **Foreign files are left alone.** Every generated file carries the marker
   below on its first line; a file without it is somebody else's and is never
   overwritten without ``--force``, the same rule ``cmd_skill`` uses.
"""

from __future__ import annotations

import hashlib
import os
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import store
from .errors import EXIT_REFUSED, HerdrTeamError
from . import instructions_doc as _doc
from . import paths as _paths

#: Bumped when a generated file's layout changes; the renderer rewrites older ones.
#: Bumped whenever a generated file's shape changes. It is also how an upgrade
#: is told apart from an operator's edit: a file whose marker names an older
#: version was written by an older plugin, not by a human, so it is regenerated
#: rather than held for adoption. Without this an old notifier still running
#: during an upgrade leaves every member file looking edited, forever.
WORKDIR_VERSION = 2

#: What every generated file starts with. Its absence means the file is not ours.
MARKER_TEXT = "herdr-synapse:workdir v{} generated file, edits are overwritten".format(WORKDIR_VERSION)
#: Markdown files get an HTML comment so the marker does not render.
MARKER = "<!-- {} -->".format(MARKER_TEXT)
#: ``.gitignore`` has no HTML comments: an ``<!-- ... -->`` line there is a *pattern*.
MARKER_HASH = "# {}".format(MARKER_TEXT)
#: Any of these identifies a file as ours: two comment syntaxes, and the name
#: the plugin wrote before 0.9. The old prefix has to stay recognised, or a file
#: carrying it would read as somebody else's and the renderer would refuse to
#: touch it for good.
#:
#: The rename deliberately does *not* bump ``WORKDIR_VERSION``. Only the name in
#: the marker changed, so an old file and a new one are the same shape, and a
#: bump would make ``edited`` answer "an older plugin wrote this, not a human"
#: for every one of them: an operator's unadopted edit, in flight when the
#: upgrade landed, would have been silently overwritten. Left at 2, the digest
#: still rules. An untouched file drifts from the new marker and is regenerated
#: under the new name; an edited one is held for adoption exactly as before.
MARKER_PREFIXES = ("<!-- herdr-synapse:workdir ", "# herdr-synapse:workdir ",
                   "<!-- herdr-team:workdir ", "# herdr-team:workdir ")


def marker_for(path: Path) -> str:
    """The comment syntax the file at ``path`` actually understands."""
    return MARKER_HASH if Path(path).name in (".gitignore", ".gitattributes") else MARKER

#: The one directory name the plugin claims inside a project.
DIR_NAME = ".herdr-synapse"
#: Names it claimed before. A checkout still holding one is renamed on the next
#: render (``migrate_legacy_dir``), and a path under one still resolves, so a
#: ``--ref`` an agent wrote from memory does not break the moment it moves.
LEGACY_DIR_NAMES = (".herdr-team",)

#: Cap on one rendered mirror, so a pathological instructions file cannot fill a repo.
MAX_RENDER_BYTES = 64 * 1024
#: Mirrors the operator may edit in place; a drifted one is kept, never overwritten.
EDITABLE = "member"
#: The board snapshot is an archive of everything that was said, so it gets far
#: more room than a document mirror; at 64 KB a real team's board was truncated.
MAX_BOARD_BYTES = 8 * 1024 * 1024


class ForeignFileError(HerdrTeamError):
    """A path we would generate exists and was not written by this plugin."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            "workdir_foreign_file",
            "{} exists and was not written by herdr-synapse; move it aside or pass --force".format(path),
            EXIT_REFUSED,
            {"path": os.fspath(path)},
        )


# --------------------------------------------------------------------------
# where the project is


def project_dir_of(doc: Dict[str, Any]) -> Optional[str]:
    """``config.project_dir`` of a ``team.json`` document, when it holds a usable path."""
    if not isinstance(doc, dict):
        return None
    config = doc.get("config")
    if not isinstance(config, dict):
        return None
    value = config.get("project_dir")
    if not isinstance(value, str) or not value.strip():
        return None
    return value


def resolve_project_dir(raw: str, state_root: Optional[Path] = None) -> Path:
    """Validate a human-supplied project directory.

    Refuses anything that is not an existing directory, plus the handful of
    paths that would make the folder useless or dangerous: the filesystem
    root, ``$HOME`` itself, and anywhere inside the plugin's own state dir.
    The path is resolved first, so a symlinked component cannot smuggle the
    folder somewhere else.
    """
    text = str(raw or "").strip()
    if not text:
        raise HerdrTeamError("path_invalid", "a project directory is required", EXIT_REFUSED, {"path": raw})
    candidate = Path(os.path.expanduser(text))
    try:
        resolved = candidate.resolve()
    except OSError as err:
        raise HerdrTeamError("path_invalid", "cannot resolve {}: {}".format(text, err), EXIT_REFUSED, {"path": text}) from err
    if not resolved.is_dir():
        raise HerdrTeamError("path_invalid", "{} is not an existing directory".format(resolved), EXIT_REFUSED, {"path": os.fspath(resolved)})
    if resolved.parent == resolved:
        raise HerdrTeamError("path_invalid", "refusing to use the filesystem root as a project directory", EXIT_REFUSED, {"path": os.fspath(resolved)})
    home = Path(os.path.expanduser("~")).resolve()
    if resolved == home:
        raise HerdrTeamError("path_invalid", "refusing to use your home directory as a project directory", EXIT_REFUSED, {"path": os.fspath(resolved)})
    if state_root is not None:
        try:
            state = Path(state_root).resolve()
        except OSError:
            state = Path(state_root)
        if resolved == state or state in resolved.parents:
            raise HerdrTeamError("path_invalid", "refusing a project directory inside herdr-synapse's own state dir", EXIT_REFUSED, {"path": os.fspath(resolved)})
    return resolved


def legacy_dirs(project_dir: str) -> List[Path]:
    """Folders under a previous name that still exist in this project."""
    return [p for p in (Path(project_dir) / name for name in LEGACY_DIR_NAMES) if p.is_dir()]


def migrate_legacy_dir(project_dir: str) -> Optional[Tuple[Path, Path]]:
    """Rename a folder left under a previous name; returns ``(from, to)`` when one moved.

    Only ever a rename, and only into a name nothing occupies: the folder holds
    the team's artifacts, so merging two of them or writing over one is not a
    thing to attempt automatically. A symlink is refused rather than followed,
    because the destination is a directory this plugin then writes into.
    """
    target = Path(project_dir) / DIR_NAME
    for source in legacy_dirs(project_dir):
        if target.exists() or target.is_symlink() or source.is_symlink():
            continue
        try:
            source.rename(target)
        except OSError:
            continue
        return (source, target)
    return None


def team_root(project_dir: str, team_name: str) -> Path:
    """``<project>/.herdr-synapse/<team>``. Namespaced so two teams can share one project."""
    return Path(project_dir) / DIR_NAME / _paths.validate_team_name(team_name)


def paths_for(project_dir: str, team_name: str) -> Dict[str, Path]:
    """Every path this module generates, for one team in one project."""
    shared = Path(project_dir) / DIR_NAME
    root = team_root(project_dir, team_name)
    return {
        "shared": shared,
        "readme": shared / "README.md",
        "gitignore": shared / ".gitignore",
        "root": root,
        "knowledge": root / "knowledge.md",
        "members": root / "members",
        "artifacts": root / "artifacts",
        "exports": root / "exports",
        "board": root / "board.md",
    }


def is_inside(candidate: Path, project_dir: Optional[str]) -> bool:
    """True when ``candidate`` resolves inside the current or legacy team folder.

    Both sides are resolved, because ``paths.check_not_symlink`` lstats only
    the final component: an intermediate symlink would otherwise walk a
    ``--ref`` out of the project while still looking like a plain relative
    path.
    """
    if not project_dir:
        return False
    try:
        target = Path(candidate).resolve()
        roots = [(Path(project_dir) / name).resolve() for name in (DIR_NAME,) + LEGACY_DIR_NAMES]
    except OSError:
        return False
    # A previous name counts too: an agent holding the old path in its context
    # would otherwise have every ``--ref`` refused the moment the folder moved.
    return any(target == root or root in target.parents for root in roots)


# --------------------------------------------------------------------------
# writing


def _is_ours(path: Path) -> bool:
    """True when ``path`` is absent or carries our marker on its first line."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            first = handle.readline()
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return first.startswith(MARKER_PREFIXES)


def digest(text: str) -> str:
    """Short content hash, used to report a mirror the human edited by hand."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def write_generated(path: Path, body: str, force: bool = False, max_bytes: int = MAX_RENDER_BYTES) -> bool:
    """Write one generated file. Returns True when the file changed.

    Refuses a file that exists without our marker unless ``force``. Never
    deletes: callers that need a file to stop meaning something write a
    tombstone over it instead.
    """
    text = marker_for(path) + "\n" + body
    if len(text.encode("utf-8")) > max_bytes:
        suffix = "\n…\n"
        keep = max_bytes - len(suffix.encode("utf-8"))
        text = text.encode("utf-8")[:keep].decode("utf-8", "ignore") + suffix
    if not force and not _is_ours(path):
        raise ForeignFileError(path)
    try:
        current = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        current = None
    if current == text:
        return False
    store.atomic_write(path, text.encode("utf-8"), fsync=False)
    return True


def mirror_state(team_paths: Any) -> Dict[str, str]:
    """``{file name: digest}`` of what the plugin last wrote to each editable mirror."""
    doc = store.read_json(team_paths.mirror_json, default=None)
    if not isinstance(doc, dict):
        return {}
    return {k: v for k, v in doc.items() if isinstance(k, str) and isinstance(v, str)}


def save_mirror_state(team_paths: Any, state: Dict[str, str]) -> None:
    try:
        store.write_json(team_paths.mirror_json, state, fsync=False)
    except (HerdrTeamError, OSError):
        return  # the folder is a convenience; losing the digest only re-renders


def forget_mirror(team_paths: Any, file_name: str) -> None:
    """Drop one file's recorded digest, so the next render rewrites it.

    Called after an edit is adopted or discarded: until the record is cleared,
    the file still looks edited and the render keeps skipping it, which would
    leave the mirror and the authoritative copy permanently apart.
    """
    state = mirror_state(team_paths)
    if state.pop(file_name, None) is not None:
        save_mirror_state(team_paths, state)


def marker_version(text: str) -> Optional[int]:
    """The workdir version named by a generated file's first line, or None."""
    first = text.splitlines()[0] if text else ""
    if not first.startswith(MARKER_PREFIXES):
        return None
    for word in first.split():
        if word.startswith("v") and word[1:].isdigit():
            return int(word[1:])
    return None


def edited(path: Path, recorded: Optional[str]) -> bool:
    """True when this file is not what the plugin last wrote there.

    The one question that matters for an editable mirror, and the reason a
    digest is recorded at all: ``drifted`` also fires when the plugin's own
    template changes, which is not an edit to adopt. No record means the file
    was never written by this version, so it is not treated as edited.
    """
    if not recorded:
        return False
    try:
        current = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return False
    if not current.startswith(MARKER_PREFIXES):
        return False  # foreign: ``write_generated`` refuses it, which is the louder signal
    if marker_version(current) != WORKDIR_VERSION:
        return False  # an older plugin wrote this, not a human
    return digest(current) != recorded


def drifted(path: Path, expected_body: str) -> bool:
    """True when a mirror exists, is ours, and no longer matches what we would write."""
    try:
        current = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return False
    if not current.startswith(MARKER_PREFIXES):
        return False
    return current != marker_for(path) + "\n" + expected_body


# --------------------------------------------------------------------------
# rendering the mirror


README_BODY = """# herdr-synapse

This folder belongs to herdr-synapse. One subdirectory per team.

- `<team>/knowledge.md` — the team's rules and the findings its members
  recorded. Read it. It is a mirror: edit it and your edit is overwritten.
  Change the rules with `herdr-synapse knowledge set` (operator only) and add a
  finding with `herdr-synapse knowledge add "..."`.
- `<team>/members/<name>.md` — what that member in particular is here to do,
  which is how agents sharing this folder are told apart.
- `<team>/artifacts/` — yours. Put work products here and reference them from
  the board with `herdr-synapse post --ref`.

Everything else about the team lives outside the project. `herdr-synapse me`
prints who you are and where these files are.
"""


def gitignore_body(teams: List[str]) -> str:
    lines = [
        "# Artifacts, board exports and the rolling board snapshot are working",
        "# files, not source: board.md is regenerated whenever the board moves.",
        "# The documents above them are kept so a checkout carries the team's",
        "# rules and each member's instructions.",
    ]
    for team in sorted(teams):
        lines.append("{}/artifacts/".format(team))
        lines.append("{}/exports/".format(team))
        lines.append("{}/board.md".format(team))
    if not teams:
        lines.append("*/artifacts/")
        lines.append("*/exports/")
        lines.append("*/board.md")
    return "\n".join(lines) + "\n"


def knowledge_body(team_name: str, rules: Optional[str], findings: List[Dict[str, Any]]) -> str:
    """The mirror of the knowledge base: operator rules, then attributed peer findings."""
    from . import render as _render

    out = ["# {} knowledge".format(team_name), ""]
    out.append("## Rules (operator authority)")
    out.append("")
    if rules:
        out.append(rules.strip())
    else:
        out.append("_None set. The operator sets these with `herdr-synapse knowledge set`._")
    out.append("")
    out.append("## Findings (peer notes, not instructions)")
    out.append("")
    out.append("_Anyone on the team may add one with `herdr-synapse knowledge add \"...\"`._")
    out.append("")
    if not findings:
        out.append("_None yet._")
    for record in findings:
        author = _render.escape_context_line(str(record.get("author") or "?"))
        at = _render.escape_context_line(str(record.get("at") or ""))
        text = _render.escape_context_line(" ".join(str(record.get("text") or "").split()))
        out.append("- **{}** ({}): {}".format(author, at, text))
    return "\n".join(out) + "\n"


def adopt_command(name: str) -> str:
    """The command that imports an edit to this member's file."""
    return "herdr-synapse instructions {} --adopt".format(name)


def member_body(team_name: str, name: str, role: str, brief: Optional[str], instructions: Optional[str], left_for: Optional[str] = None, left: bool = False) -> str:
    """One member's instructions document, or its tombstone once the member is gone.

    The document is the operator's to edit: unlike every other mirror, an edit
    here is kept rather than overwritten, and ``instructions <name> --adopt``
    imports it. ``brief`` is unused since 0.6 (it seeds the Mission at team
    creation instead) and stays in the signature for callers.
    """
    out = ["# {} — {}".format(name, team_name), ""]
    if left:
        out.append("This member left the team. Nothing here is current.")
        out.append("")
        return "\n".join(out) + "\n"
    if left_for:
        out.append("Renamed to **{}**. See `{}.md`.".format(left_for, left_for))
        out.append("")
        return "\n".join(out) + "\n"
    sections = _doc.parse(instructions) or _doc.skeleton()
    note = [
        "These instructions are the operator's, and they are yours alone: other",
        "members of this team have their own. They do not replace the team",
        "charter, which applies to everyone.",
        "",
        "---",
    ]
    return _doc.document(name, team_name, role, sections, adopt_command=adopt_command(name), note=note)


def repair_creation_scaffolds(layout: Any, team_name: str) -> Optional[List[str]]:
    """Complete only the exact Mission-only documents produced by the creation bug.

    Revision 1 plus byte-for-byte generated storage plus a Mission equal to the
    stored brief is the migration signature. Anything edited, custom, later,
    missing, or belonging to an inactive member is left alone. The semantic
    instructions do not change, so this intentionally does not bump a revision
    or post to the board. ``None`` means the lock was busy and a daemon caller
    should retry on its next scan.
    """
    from . import roster as _roster

    team_paths = layout.team(team_name)
    repaired: List[str] = []
    doc = _roster.load_team(team_paths)

    def candidate(member: Any) -> bool:
        return bool(not member.is_human and member.status == "active" and member.instructions_seq == 1 and str(member.brief or "").strip())

    def has_exact_bug_signature(member: Any) -> bool:
        if not candidate(member):
            return False
        brief = str(member.brief or "").strip()
        try:
            raw = team_paths.instructions(member.name).read_text(encoding="utf-8")
        except (FileNotFoundError, OSError):
            return False
        sections = _doc.parse(raw)
        return bool(
            len(sections) == 1
            and sections[0][0] == "Mission"
            and _doc.mission_paragraph(sections)
            and raw == _doc.to_text(sections)
            and "\n".join(sections[0][1]).strip() == brief
        )

    # Most teams already have complete documents. Check their exact bytes
    # without taking the team lock so a daemon scan does not contend with
    # ordinary board and roster writes merely to discover there is no repair.
    if not any(has_exact_bug_signature(member) for member in doc.members):
        return repaired
    try:
        lock = store.team_lock(team_paths)
        lock.acquire()
    except HerdrTeamError as err:
        if err.code == "board_locked":
            return None
        raise
    try:
        doc = _roster.load_team(team_paths)
        for member in doc.members:
            if not has_exact_bug_signature(member):
                continue
            target = team_paths.instructions(member.name)
            store.atomic_write(target, _doc.to_text(_doc.skeleton(str(member.brief or "").strip())).encode("utf-8"))
            repaired.append(member.name)
    finally:
        lock.release()
    return repaired


def render(layout: Any, team_name: str, force: bool = False) -> Dict[str, Any]:
    """Regenerate every mirrored file for one team. Returns what changed.

    Best effort by design: this runs from commands whose real job is something
    else, so a read-only checkout or a foreign file is reported, never fatal.
    Nothing here deletes; a member that left gets a tombstone written over its
    file so the folder keeps a record instead of a hole.
    """
    from . import charter as _charter
    from . import roster as _roster

    team_paths = layout.team(team_name)
    repaired = repair_creation_scaffolds(layout, team_name) or []
    doc = _roster.load_team(team_paths)
    project = project_dir_of(doc.to_json())
    result: Dict[str, Any] = {"project_dir": project, "written": [], "skipped": [], "drifted": [], "awaiting_adopt": [], "repaired": repaired}
    if not project:
        result["reason"] = "no project directory; set one with herdr-synapse project set <path>"
        return result

    moved = migrate_legacy_dir(project)
    if moved is not None:
        result["moved"] = {"from": os.fspath(moved[0]), "to": os.fspath(moved[1])}

    targets = paths_for(project, team_name)
    rules = _charter.get_rules(layout, team_name)
    findings = _charter.read_findings(layout, team_name)

    plan: List[Tuple[Path, str, str]] = [
        (targets["readme"], README_BODY, "generated"),
        (targets["knowledge"], knowledge_body(team_name, rules, findings), "generated"),
    ]
    for member in doc.members:
        if member.is_human:
            continue
        instructions = _charter.get_instructions(layout, team_name, member.name)
        plan.append((
            targets["members"] / (_paths._safe_stem(member.name, "name") + ".md"),
            member_body(team_name, member.name, member.role or "", member.brief, instructions, left=member.status == "left"),
            EDITABLE,
        ))
        # A rename leaves a file behind under the old name. It is never deleted:
        # it is rewritten to point at the new one, so a teammate holding the old
        # path finds a forwarding note rather than stale instructions.
        for previous in member.previous_names or []:
            old_name = previous.get("name") if isinstance(previous, dict) else None
            if not isinstance(old_name, str) or old_name == member.name:
                continue
            try:
                stem = _paths._safe_stem(old_name, "name")
            except HerdrTeamError:
                continue
            plan.append((
                targets["members"] / (stem + ".md"),
                member_body(team_name, old_name, "", None, None, left_for=member.name),
                "generated",
            ))

    try:
        _paths.ensure_dir(targets["members"])
        _paths.ensure_dir(targets["artifacts"])
        existing_teams = _existing_team_dirs(targets["shared"], team_name)
        plan.append((targets["gitignore"], gitignore_body(existing_teams), "generated"))
    except (HerdrTeamError, OSError) as err:
        result["reason"] = "cannot create {}: {}".format(targets["root"], err)
        return result

    state = mirror_state(team_paths)
    state_changed = False
    for path, body, kind in plan:
        if kind == EDITABLE and not force and edited(path, state.get(path.name)):
            # The operator's own document. Overwriting it here is what made the
            # file uneditable; it waits for ``instructions --adopt`` instead,
            # which is also the step that confers authority on the edit, since
            # the checkout is writable by the agents themselves.
            result["awaiting_adopt"].append(os.fspath(path))
            continue
        if drifted(path, body):
            result["drifted"].append(os.fspath(path))
        try:
            wrote = write_generated(path, body, force=force)
            if wrote:
                result["written"].append(os.fspath(path))
            if kind == EDITABLE and (wrote or path.name not in state):
                try:
                    state[path.name] = digest(path.read_text(encoding="utf-8"))
                    state_changed = True
                except OSError:
                    pass
        except ForeignFileError:
            result["skipped"].append(os.fspath(path))
        except (HerdrTeamError, OSError) as err:
            result["skipped"].append(os.fspath(path))
            result.setdefault("errors", []).append("{}: {}".format(path, err))
    if state_changed:
        save_mirror_state(team_paths, state)
    return result


def _existing_team_dirs(shared: Path, current: str) -> List[str]:
    """Team subdirectories share ``.herdr-synapse/``, so one .gitignore covers them all."""
    names = {current}
    try:
        for entry in shared.iterdir():
            if entry.is_dir() and not entry.name.startswith("."):
                names.add(entry.name)
    except OSError:
        pass
    return sorted(names)


# --------------------------------------------------------------------------
# noticing what changed in the folder


#: Files walked per scan. A bigger drop is reported as "and N more", never walked twice.
MAX_WATCHED_FILES = 500
#: Hard budget for one record's text. The skill asks *agents* for under 500; a
#: machine-generated record gets well under half of that, and this is close to
#: one record's fair share of the 4096-byte context block every member shares.
#: Listing 8 deep paths per category produced 1003-character records.
MAX_RECORD_CHARS = 220
#: Longest path printed verbatim before the middle is elided.
MAX_PATH_CHARS = 56
#: Places named in one record; the rest are counted.
MAX_GROUPS_IN_RECORD = 3


#: Below this depth a directory is fingerprinted as one aggregate entry.
MAX_WATCHED_DEPTH = 5
#: A directory holding more than this many files is data, not deliverables.
MAX_DIR_FILES = 32
#: Directory names never walked; generated trees, not work products.
IGNORED_DIR_NAMES = frozenset({
    ".git", "node_modules", "__pycache__", ".venv", "venv", "site-packages", "dist", "build",
    "target", ".cache", "cache", ".tox", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "coverage", ".next", ".gradle", ".terraform",
})


def fingerprint_artifacts(project_dir: Optional[str], team_name: str) -> Dict[str, Tuple[int, int, int]]:
    """``{relative path: (newest mtime_ns, total size, file count)}`` for ``artifacts/``.

    Prunes rather than truncates. The old version stopped mid-walk once it had
    ``MAX_WATCHED_FILES`` entries, so dumping 600 files into ``aaa-data/``
    pushed ``zzz-report.md`` out of the fingerprint and the next diff announced
    it as *removed* although nothing was deleted. Collapsing a deep or wide
    subtree into one aggregate entry keeps the entry count bounded by the shape
    of the tree instead of by where the walk happened to stop.
    """
    out: Dict[str, Tuple[int, int, int]] = {}
    if not project_dir:
        return out
    root = paths_for(project_dir, team_name)["artifacts"]
    root_str = os.fspath(root)
    try:
        walker = os.walk(root_str, topdown=True)
    except OSError:
        return out
    for dirpath, dirnames, filenames in walker:
        dirnames.sort()
        try:
            rel_dir = os.path.relpath(dirpath, root_str)
        except ValueError:
            dirnames[:] = []
            continue
        rel_dir = "" if rel_dir == "." else rel_dir
        depth = len(rel_dir.split(os.sep)) if rel_dir else 0
        names = [f for f in sorted(filenames) if not f.startswith(".")]
        dirnames[:] = [d for d in dirnames if not d.startswith(".") and d not in IGNORED_DIR_NAMES]

        too_deep = depth >= MAX_WATCHED_DEPTH
        too_wide = len(names) > MAX_DIR_FILES
        if rel_dir and (too_deep or too_wide):
            # One entry for the whole subtree; do not descend into it.
            aggregate = _aggregate_dir(dirpath)
            if aggregate is not None:
                out[rel_dir.replace(os.sep, "/") + "/"] = aggregate
            dirnames[:] = []
            continue
        for filename in names:
            full = os.path.join(dirpath, filename)
            try:
                st = os.stat(full)
            except OSError:
                continue
            key = os.path.join(rel_dir, filename) if rel_dir else filename
            out[key.replace(os.sep, "/")] = (int(st.st_mtime_ns), int(st.st_size), 1)
            if len(out) >= MAX_WATCHED_FILES:
                return out
    return out


def _aggregate_dir(dirpath: str) -> Optional[Tuple[int, int, int]]:
    """One fingerprint for a whole subtree: newest mtime, total size, file count."""
    newest = 0
    total = 0
    files = 0
    try:
        for sub_dir, sub_dirs, sub_files in os.walk(dirpath):
            sub_dirs[:] = [d for d in sub_dirs if not d.startswith(".") and d not in IGNORED_DIR_NAMES]
            for name in sub_files:
                if name.startswith("."):
                    continue
                try:
                    st = os.stat(os.path.join(sub_dir, name))
                except OSError:
                    continue
                newest = max(newest, int(st.st_mtime_ns))
                total += int(st.st_size)
                files += 1
    except OSError:
        return None
    return (newest, total, max(1, files))


def diff_artifacts(before: Dict[str, Tuple[int, int]], after: Dict[str, Tuple[int, int]]) -> Dict[str, List[str]]:
    """What changed between two fingerprints, each list sorted."""
    added = sorted(set(after) - set(before))
    removed = sorted(set(before) - set(after))
    changed = sorted(name for name in set(before) & set(after) if before[name] != after[name])
    # How many real files each entry stands for; a collapsed subtree stands for many.
    counts: Dict[str, int] = {}
    for name in added + changed:
        counts[name] = _entry_files(after.get(name))
    for name in removed:
        counts[name] = _entry_files(before.get(name))
    return {"added": added, "changed": changed, "removed": removed, "counts": counts}


def _entry_files(value: Any) -> int:
    """How many files a fingerprint entry represents (1 for a plain file)."""
    if isinstance(value, tuple) and len(value) >= 3:
        try:
            return max(1, int(value[2]))
        except (TypeError, ValueError):
            return 1
    return 1


def shorten_path(rel: str, limit: int = MAX_PATH_CHARS) -> str:
    """Elide the middle of a long path, keeping the first and last segments.

    The first segment is usually the owning member, so it is the one part that
    must survive: a record has to say *whose* work changed.
    """
    text = str(rel or "")
    if len(text) <= limit:
        return text
    trailing = "/" if text.endswith("/") else ""
    parts = [p for p in text.strip("/").split("/") if p]
    if len(parts) <= 1:
        return text[: max(1, limit - 1)].rstrip("/") + "\u2026" + trailing
    first, last = parts[0], parts[-1]
    candidate = "{}/\u2026/{}{}".format(first, last, trailing)
    if len(candidate) <= limit:
        return candidate
    candidate = "{}/\u2026{}".format(first, trailing)
    if len(candidate) <= limit:
        return candidate
    return first[: max(1, limit - 2)] + "\u2026"


def group_paths(names: List[str], counts: Dict[str, int], max_groups: int) -> Tuple[List[Tuple[str, int, bool]], int]:
    """Roll paths up into at most ``max_groups`` (directory, files, sole) buckets.

    Returns the buckets, biggest first, plus how many were left over. Grouping
    by place rather than listing leaves is what bounds a record's length by the
    number of directories that changed instead of the number of files.
    """
    buckets: Dict[str, int] = {}
    sole: Dict[str, str] = {}
    for name in names:
        weight = int(counts.get(name, 1) or 1)
        if name.endswith("/"):
            key = name
        else:
            head, _, _tail = name.rpartition("/")
            key = (head + "/") if head else ""
        buckets[key] = buckets.get(key, 0) + weight
        if buckets[key] == weight:
            sole[key] = name
        else:
            sole.pop(key, None)

    # Collapse the deepest buckets into their parents until few enough remain.
    while len(buckets) > max_groups:
        deepest = max(buckets, key=lambda k: (k.count("/"), k))
        if deepest.count("/") <= 1:
            break
        parent = deepest.rstrip("/").rpartition("/")[0]
        parent = (parent + "/") if parent else ""
        buckets[parent] = buckets.get(parent, 0) + buckets.pop(deepest)
        sole.pop(parent, None)
        sole.pop(deepest, None)

    # Fold a bucket that lives under another so siblings do not repeat a prefix.
    for key in sorted(buckets, key=lambda k: -k.count("/")):
        if key not in buckets:
            continue
        for other in list(buckets):
            if other != key and other and key.startswith(other):
                buckets[other] += buckets.pop(key)
                sole.pop(other, None)
                break

    ordered = sorted(buckets.items(), key=lambda kv: (-kv[1], kv[0]))
    kept = ordered[:max_groups]
    remainder = sum(count for _key, count in ordered[max_groups:])
    return [(key, count, key in sole and count == 1) for key, count in kept], remainder


def _clause(label: str, names: List[str], counts: Dict[str, int], max_groups: int, path_limit: int) -> str:
    groups, remainder = group_paths(names, counts, max_groups)
    pieces: List[str] = []
    for key, count, is_sole in groups:
        if is_sole:
            pieces.append(shorten_path(_sole_name(names, key), path_limit))
        elif count == 1 and key:
            pieces.append("1 file under " + shorten_path(key, path_limit))
        else:
            pieces.append("{} files under {}".format(count, shorten_path(key or "artifacts/", path_limit)))
    text = "{} {}".format(label, ", ".join(pieces))
    if remainder > 0:
        text += " +{} more".format(remainder)
    return text


def _sole_name(names: List[str], key: str) -> str:
    for name in names:
        head, _, _tail = name.rpartition("/")
        if ((head + "/") if head else "") == key or name == key:
            return name
    return key


def describe_change(team_name: str, diff: Dict[str, List[str]], limit: int = MAX_RECORD_CHARS) -> Optional[str]:
    """One short board-record line naming what changed, or None when nothing did.

    Says *where* work appeared, never what is in it: the record is an awareness
    signal, and reading the file is the member's own decision. Summarised by
    directory rather than listed leaf by leaf, because a member generating a
    data tree otherwise produced a record longer than the whole context block
    its teammates share.
    """
    categories = [
        ("new", list(diff.get("added") or [])),
        ("updated", list(diff.get("changed") or [])),
        ("removed", list(diff.get("removed") or [])),
    ]
    categories = [(label, names) for label, names in categories if names]
    if not categories:
        return None
    counts = dict(diff.get("counts") or {})

    for max_groups in (MAX_GROUPS_IN_RECORD, 2, 1):
        for path_limit in (MAX_PATH_CHARS, 36, 24):
            text = "artifacts: " + "; ".join(_clause(label, names, counts, max_groups, path_limit) for label, names in categories)
            if len(text) <= limit:
                return text
    # Counts only: structurally short whatever the input looks like.
    totals = ", ".join("{} {}".format(sum(int(counts.get(n, 1) or 1) for n in names), label) for label, names in categories)
    text = "artifacts: {} under artifacts/".format(totals)
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "\u2026"


# --------------------------------------------------------------------------
# status


def status(layout: Any, team_name: str) -> Dict[str, Any]:
    """What the team's knowledge base looks like right now.

    One function behind the ``prefix+t`` tree, the ``prefix+k`` view and
    ``doctor``, so the three can never disagree about whether a team has a
    folder. Reads only; safe to call from a render loop.
    """
    from . import charter as _charter
    from . import roster as _roster

    team_paths = layout.team(team_name)
    doc = _roster.load_team(team_paths)
    project = project_dir_of(doc.to_json())
    rules = _charter.get_rules(layout, team_name) or ""
    findings = _charter.read_findings(layout, team_name)
    members: List[Dict[str, Any]] = []
    state = mirror_state(team_paths) if project else {}
    for member in doc.members:
        if member.is_human or member.status == "left":
            continue
        text = _charter.get_instructions(layout, team_name, member.name) or ""
        mission = bool(_doc.mission_paragraph(_doc.parse(text)))
        members.append({"name": member.name, "role": member.role or "", "kind": member.kind or "",
                        "instructions": bool(text), "mission": mission, "chars": len(text),
                        "stale": _charter.instructions_stale(member),
                        "edited": _member_file_edited(team_paths, project, team_name, member.name, state) if project else False})
    out: Dict[str, Any] = {
        "team": team_name,
        "project_dir": project,
        "folder": os.fspath(team_root(project, team_name)) if project else None,
        "exists": bool(project) and team_root(project, team_name).is_dir(),
        "rules": bool(rules),
        "rules_chars": len(rules),
        "findings": len(findings),
        "last_finding": findings[-1] if findings else None,
        "members": members,
        "with_instructions": sum(1 for m in members if m["instructions"]),
        "with_missions": sum(1 for m in members if m["mission"]),
        "missing_missions": [m["name"] for m in members if not m["mission"]],
        "artifacts": 0,
        "issues": [],
        "awaiting_adopt": [],
    }
    if not project:
        return out
    path = Path(project)
    if not path.is_dir():
        out["issues"].append("project directory {} is gone".format(project))
        return out
    if not os.access(project, os.W_OK):
        out["issues"].append("project directory {} is not writable".format(project))
    out["artifacts"] = sum(_entry_files(v) for v in fingerprint_artifacts(project, team_name).values())
    targets = paths_for(project, team_name)
    for label, path_ in (("README.md", targets["readme"]), ("knowledge.md", targets["knowledge"])):
        if path_.exists() and not _is_ours(path_):
            out["issues"].append("{} was not written by herdr-synapse".format(label))
    # A member's own document is the one mirror the operator edits, so both a
    # foreign file and a pending edit matter here; neither used to be checked.
    for entry in out["members"]:
        member_file = targets["members"] / (str(entry["name"]) + ".md")
        if not member_file.exists():
            continue
        if not _is_ours(member_file):
            out["issues"].append("members/{}.md was not written by herdr-synapse".format(entry["name"]))
        elif entry.get("edited"):
            out["awaiting_adopt"].append(entry["name"])
    return out


def _member_file_edited(team_paths: Any, project: str, team_name: str, name: str, state: Optional[Dict[str, str]] = None) -> bool:
    """True when this member's mirror holds an edit nobody has adopted yet."""
    path = paths_for(project, team_name)["members"] / (str(name) + ".md")
    recorded = (mirror_state(team_paths) if state is None else state).get(path.name)
    return edited(path, recorded)


def status_summary(info: Dict[str, Any]) -> str:
    """One short line for a list: what this team's knowledge base amounts to."""
    total = len(info.get("members") or [])
    mission_status = "{}/{} with Missions".format(info.get("with_missions", 0), total)
    if not info.get("project_dir"):
        return "no folder, " + mission_status
    if info.get("issues"):
        return info["issues"][0]
    if not info.get("exists"):
        return "folder not created yet"
    parts = ["rules" if info.get("rules") else "no rules"]
    parts.append(mission_status)
    parts.append("{} finding{}".format(info.get("findings", 0), "" if info.get("findings") == 1 else "s"))
    if info.get("artifacts"):
        parts.append("{} artifact{}".format(info["artifacts"], "" if info["artifacts"] == 1 else "s"))
    waiting = info.get("awaiting_adopt") or []
    if waiting:
        parts.append("{} edit{} to adopt".format(len(waiting), "" if len(waiting) == 1 else "s"))
    return ", ".join(parts)


# --------------------------------------------------------------------------
# the rolling board snapshot


def refresh_gitignore(project_dir: str, team_name: str, force: bool = False) -> bool:
    """Rewrite ``.herdr-synapse/.gitignore`` so it covers every team dir present."""
    targets = paths_for(project_dir, team_name)
    try:
        teams = _existing_team_dirs(targets["shared"], team_name)
        return write_generated(targets["gitignore"], gitignore_body(teams), force=force)
    except (ForeignFileError, HerdrTeamError, OSError):
        return False


def render_board_snapshot(layout: Any, team_name: str, force: bool = False) -> Dict[str, Any]:
    """Write the team's whole board to ``<team>/board.md``, archive included.

    The board is the team's record of what happened, and it lived only in the
    plugin's state dir where nobody looks. This keeps a readable copy beside
    the team's rules and per-member instructions, regenerated whenever the
    board moves. It is a mirror like the others: marker-protected, never read
    back, and listed in the generated ``.gitignore`` because it is rewritten
    constantly and would otherwise dominate every diff.

    Best effort: the checkout may be read-only or gone, and this runs from the
    notifier tick whose real job is delivery.
    """
    from . import roster as _roster
    from . import store as _store

    team_paths = layout.team(team_name)
    doc = _roster.load_team(team_paths)
    project = project_dir_of(doc.to_json())
    out: Dict[str, Any] = {"team": team_name, "path": None, "records": 0, "written": False}
    if not project:
        out["reason"] = "no project directory"
        return out
    target = paths_for(project, team_name)["board"]
    try:
        records = _store.BoardStore(team_paths).read(include_archive=True, include_retracted=True)
    except (HerdrTeamError, OSError) as err:
        out["reason"] = "cannot read the board: {}".format(err)
        return out
    out["records"] = len(records)
    out["path"] = os.fspath(target)

    from . import render as _render

    # The newest post's timestamp, not "now": the snapshot's content must be a
    # pure function of the board, or it rewrites itself on every pass and shows
    # up as a change when nothing was said.
    newest = records[-1].get("ts") if records else None
    body = _render.render_export_markdown(
        records, team_name,
        charter=doc.charter,
        members=[m.to_json() for m in doc.members],
        exported_at=newest if isinstance(newest, str) else None,
        exported_by=None,
    )
    try:
        _paths.ensure_dir(target.parent)
        # The ignore rule must exist before the file does, or a snapshot written
        # by an older folder layout is committable until the next roster change.
        refresh_gitignore(project, team_name)
        out["written"] = write_generated(target, body, force=force, max_bytes=MAX_BOARD_BYTES)
    except ForeignFileError:
        out["reason"] = "{} was not written by herdr-synapse".format(target)
    except (HerdrTeamError, OSError) as err:
        out["reason"] = "{}: {}".format(type(err).__name__, err)
    return out
