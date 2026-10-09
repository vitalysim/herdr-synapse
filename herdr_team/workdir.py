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
   file found in the folder is held and never imported by itself: only a
   human command (``instructions --adopt``, ``knowledge import``) reads it
   back. In ``auto`` mode, a settled edit of a document this team wrote is
   proposed with its complete text for a verified operator's ``project confirm``.
   ``manual`` holds edits for explicit import or adoption. Neither mode lets a
   file edit become the operator's instruction without that confirmation.
2. **Consent is explicit.** ``config.project_dir`` is empty until a human runs
   ``project set``. The directory is never inferred from member cwds, so the
   plugin cannot pick the wrong repository or write into two of them.
3. **Never overwrite.** A record the mirror writes (``MIRRORED_RECORDS``
   classifies them as durable) is written or deleted only when its path is
   absent or it holds exactly the bytes this team last wrote there;
   everything else is held, untouched, and reported
   (``document_sync.FolderGuard``, the one door for durable records).
   ``board.md`` and ``canvas.md`` are disposable views: a marker-bearing view
   is regenerated, and a file without the marker stays untouched. Durable
   bytes this team did not write are replaced only on an explicit operator act,
   and only after a verified copy into
   ``<team>/inherited/``; otherwise nothing changes. Removing or renaming a
   member rewrites its file as a tombstone, and ``project clear`` leaves
   every file in place.
4. **Foreign files are left alone.** Every generated file carries the marker
   below on its first line; in the shared ``.herdr-synapse/`` folder a
   ``README.md`` or ``.gitignore`` without it is somebody else's and is never
   overwritten without ``--force``, the same rule ``cmd_skill`` uses.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import store
from .errors import EXIT_REFUSED, HerdrTeamError
from . import instructions_doc as _doc
from . import paths as _paths

#: Bumped when a generated file's layout changes; the renderer rewrites older ones.
#: Bumped whenever a generated file's shape changes. ``edited`` uses it to tell
#: an upgrade from an operator's edit: a file whose marker names an older version
#: is not announced as an edit waiting for ``--adopt``. A marker is not
#: authorship, though: the guard still writes over such a file only when it holds
#: the bytes this team last wrote there, and holds it otherwise.
WORKDIR_VERSION = 2

#: What every generated file starts with. Its absence means the file is not ours.
MARKER_TEXT = "herdr-synapse:workdir v{} managed file; check project sync mode before editing".format(WORKDIR_VERSION)
#: Markdown files get an HTML comment so the marker does not render.
MARKER = "<!-- {} -->".format(MARKER_TEXT)
#: ``.gitignore`` has no HTML comments: an ``<!-- ... -->`` line there is a *pattern*.
MARKER_HASH = "# {}".format(MARKER_TEXT)
#: JSON has no comment syntax at all, so a mirrored payload carries the marker as a
#: reserved top-level key on its own first line. The file stays valid JSON from byte
#: one, the existing first-line ``is_ours`` check still recognises it, and every
#: reader can ignore one extra key. An HTML comment would make the file unparseable
#: and a ``#`` line would too, which is why neither of the two above is reused.
#: The reserved key's own name, so a reader (or a test) can tell it from a value.
MARKER_KEY_NAME = "//"
MARKER_JSON = '{{"{}": "{}",'.format(MARKER_KEY_NAME, MARKER_TEXT)
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
#: still rules. An untouched file this team wrote is regenerated under the new
#: name; an edited one is held for adoption exactly as before.
MARKER_PREFIXES = ("<!-- herdr-synapse:workdir ", "# herdr-synapse:workdir ",
                   "<!-- herdr-team:workdir ", "# herdr-team:workdir ",
                   '{"//": "herdr-synapse:workdir ', '{"//": "herdr-team:workdir ')


def marker_for(path: Path, instance: Optional[str] = None) -> str:
    """The comment syntax the file at ``path`` actually understands.

    ``instance`` (``instance_id``) is written into a Markdown marker only: the two documents document sync reads,
    ``knowledge.md`` and ``members/<name>.md``, carry the id of the team instance that rendered them, so that
    another instance's complete render of the same path -- a team of the same name in another session -- is told
    apart from an edit of this team's document. It is attribution, not authentication: the line is plain text an
    editor keeps or copies, so it decides what a file is *reported* as, never whether anything is adopted.
    """
    name = Path(path).name
    if name in (".gitignore", ".gitattributes"):
        return MARKER_HASH
    if name.endswith(".json"):
        return MARKER_JSON
    if instance:
        return "<!-- {}; {}{} -->".format(MARKER_TEXT, INSTANCE_WORD, instance)
    return MARKER


#: How a Markdown marker names the team instance that wrote it (``marker_for``).
INSTANCE_WORD = "written by team instance "
_INSTANCE_RE = re.compile(r"; " + re.escape(INSTANCE_WORD) + r"([0-9a-f]{12}) -->\s*$")


def instance_id(team_paths: Any) -> str:
    """A provenance label derived from this team's state directory and creation time, not proof of an editor's identity.

    Same-named teams in different sessions have different state directories; a different creation timestamp
    distinguishes a recreation. Identical timestamps can collide, so this label never authorizes adoption: the
    recorded byte lineage and operator confirmation are still required.
    """
    try:
        doc = store.read_json(team_paths.team_json, default={})
    except (HerdrTeamError, OSError, ValueError):
        doc = {}
    created = doc.get("created_at") if isinstance(doc, dict) else None
    root = os.path.realpath(os.fspath(team_paths.root))
    return hashlib.sha256("{}\0{}".format(root, created or "").encode("utf-8")).hexdigest()[:12]


def marker_instance(data: Any) -> Optional[str]:
    """The team instance a generated Markdown file's first line names, or None (no marker, or an older one)."""
    if isinstance(data, bytes):
        first = data.split(b"\n", 1)[0].decode("utf-8", "replace")
    else:
        first = str(data or "").split("\n", 1)[0]
    if not first.startswith(MARKER_PREFIXES):
        return None
    match = _INSTANCE_RE.search(first)
    return match.group(1) if match else None

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
#: ``facts.md`` carries one line per current fact with its id, subject, attribute,
#: sources and dispute state, 200-300 bytes each, and ``findings_view`` shows up to
#: 200 facts: right on the 64 KiB default, so it gets its own room rather than
#: silently losing the tail of a long-lived team's record.
MAX_FACTS_BYTES = 256 * 1024
#: ``canvas.json`` is the import payload, so a truncated one is worse than none: it
#: looks importable and is not. Past this cap the pointer form is written instead
#: (``render_canvas_snapshot``). A realistic board measures ~1,280 bytes an element
#: all-in, which puts this at roughly 800 elements -- an order of magnitude under
#: ``MAX_BOARD_BYTES`` and well past ``canvas.MAX_ELEMENTS``.
MAX_CANVAS_JSON_BYTES = 1024 * 1024
#: One mirrored canvas asset, and the whole mirrored set. Chart specs are small;
#: a pasted image is not, and it is already in the session's own ``assets/``.
MAX_CANVAS_ASSET_BYTES = 1024 * 1024
MAX_CANVAS_ASSETS_BYTES = 8 * 1024 * 1024


#: Where an operator-ordered overwrite (``project render --force``, or an adopt or
#: import from this folder) puts its verified copy of the bytes it replaces. Inside
#: the knowledge path, so the copy outlives the session; git-ignored (owner
#: decision D1), so it stays on this disk and does not travel with a clone.
INHERITED_DIR_NAME = "inherited"


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
        # The durable record a new team can inherit from (0.22.1). ``facts.md``
        # (with ``facts-2.md``... past its cap) is what ``knowledge import`` reads
        # back, and ``canvas.json`` plus ``canvas-assets/`` are what ``canvas
        # import`` reads back, which is why they are committed. ``canvas.md`` is
        # the readable listing of ``canvas.json``, regenerated with it and
        # git-ignored, like ``board.md`` (owner decision D1).
        "facts": root / "facts.md",
        "canvas_md": root / "canvas.md",
        "canvas_json": root / "canvas.json",
        "canvas_assets": root / "canvas-assets",
        "inherited": root / INHERITED_DIR_NAME,
    }
# --------------------------------------------------------------------------
# what the mirror writes, and which of it a previous team could lose through


#: A record the mirror writes only where the path is absent or holds exactly the
#: bytes this team last wrote there (``document_sync.FolderGuard``): anything else
#: is held, because a previous team, a pull or a person may have put it there.
DURABLE = "durable"
#: Ours by definition, or a regenerated view nothing reads back. In the shared
#: ``.herdr-synapse/`` folder (``README.md``, ``.gitignore``) ``write_disposable``
#: writes one, refusing a file without our marker unless ``--force``. Inside a
#: team's folder (``board.md``, ``canvas.md``, ``VIEWS``) ``write_view`` writes
#: one over any file carrying the marker and never over one without it, with or
#: without ``--force``: the guard never holds a view (owner decision, 2026-10-09).
DISPOSABLE = "disposable"


class MirrorRecord(object):
    """One row of ``MIRRORED_RECORDS``: a record the mirror writes into a team's folder.

    Data rather than a code path, beside the ``paths_for`` that names the records, so the answer to "is this
    guarded?" is visible, and a path no row claims is a refusal rather than an unguarded write.
    """

    __slots__ = ("key", "durability", "kind", "why")

    def __init__(self, key: str, durability: str, kind: str, why: str) -> None:
        self.key, self.durability, self.kind, self.why = key, durability, kind, why

    @property
    def durable(self) -> bool:
        return self.durability == DURABLE

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "MirrorRecord({!r}, {!r})".format(self.key, self.durability)


#: Every record ``paths_for`` names, classified. Keyed by that function's own key
#: so the two cannot drift apart: a key in one and not the other fails
#: ``tests/test_workdir.py::MirrorRecordTableTests``.
MIRRORED_RECORDS: Dict[str, MirrorRecord] = {
    "shared": MirrorRecord("shared", DISPOSABLE, "shared",
                           "the directory the per-team subdirectories live in; a container"),
    "root": MirrorRecord("root", DISPOSABLE, "root", "the team's own directory; a container"),
    "readme": MirrorRecord("readme", DISPOSABLE, "readme", "the folder's explanation of itself: ours by definition"),
    "gitignore": MirrorRecord("gitignore", DISPOSABLE, "gitignore", "our ignore rules: ours by definition"),
    "knowledge": MirrorRecord("knowledge", DURABLE, "knowledge",
                              "the operator's Rules and the attributed Findings: the headline inheritance channel"),
    "facts": MirrorRecord("facts", DURABLE, "facts",
                          "current facts with provenance, read back by knowledge import; facts-2.md, ... too"),
    "members": MirrorRecord("members", DURABLE, "member",
                            "one member's marching orders per file, read back by instructions --adopt"),
    "board": MirrorRecord("board", DISPOSABLE, "board",
                          "the rolling board snapshot: regenerated, git-ignored, never read back (write_view)"),
    "canvas_md": MirrorRecord("canvas_md", DISPOSABLE, "canvas",
                              "the readable listing of the board, regenerated from the scene, git-ignored and read "
                              "back by nothing (write_view)"),
    "canvas_json": MirrorRecord("canvas_json", DURABLE, "canvas-payload", "the import payload canvas import reads back"),
    "canvas_assets": MirrorRecord("canvas_assets", DURABLE, "canvas-asset",
                                  "the pictures and chart data the marks name, one record per file"),
    "inherited": MirrorRecord("inherited", DURABLE, "inherited",
                              "the copies an operator-ordered overwrite took; never written by the mirror itself"),
    "artifacts": MirrorRecord("artifacts", DURABLE, "artifacts",
                              "the agents' own work products: the mirror creates the directory and writes nothing in it"),
    "exports": MirrorRecord("exports", DURABLE, "exports",
                            "where board export writes: the mirror creates the directory and writes nothing in it"),
}


#: The regenerated views that live inside a team's folder (owner decision, 2026-10-09: "board.md and canvas.md are
#: disposable views nothing reads back"). The generated ``.gitignore`` leaves them out, nothing reads them back and
#: each is a pure function of this team's board or scene, so ``document_sync.FolderGuard`` never meets one and can
#: never hold one: ``write_view`` writes them. The one thing it will not do is write over a file without this
#: plugin's marker -- that is not a view of ours but somebody's own file at the name -- and that is reported.
VIEWS = frozenset({"board", "canvas_md"})


class UnclassifiedMirrorPath(HerdrTeamError):
    """A write into a team's folder at a path no row of ``MIRRORED_RECORDS`` claims. Refused, never written."""

    def __init__(self, path: Path) -> None:
        super().__init__(
            "workdir_unclassified_record",
            "{} is not a classified mirror record; add it to workdir.MIRRORED_RECORDS "
            "as durable or disposable before writing it".format(path),
            EXIT_REFUSED,
            {"path": os.fspath(path)},
        )


class UnguardedMirrorWrite(HerdrTeamError):
    """A write into a team's folder that no classification stood behind. Refused.

    The raw writers refuse any path inside a ``.herdr-synapse`` folder unless the caller classified it, so a record
    added to the mirror later cannot be written around ``document_sync.FolderGuard`` by calling the function the
    disposable views use.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(
            "workdir_unguarded_write",
            "{} is inside a team folder; write it through document_sync.FolderGuard, or through "
            "workdir.write_disposable for a record the table classifies as disposable".format(path),
            EXIT_REFUSED,
            {"path": os.fspath(path)},
        )


def record_for(path: Path, targets: Dict[str, Path]) -> MirrorRecord:
    """The ``MIRRORED_RECORDS`` row that claims ``path``. Raises when none does.

    By exact path for the files and containers; a member document and a canvas asset by their directory; a
    numbered ``facts`` part by name; anything under ``inherited/`` by that row. Everything else is refused.
    """
    path = Path(path)
    for key, record in MIRRORED_RECORDS.items():
        if targets.get(key) == path:
            return record
    parent = path.parent
    if targets.get("root") == parent and _FACTS_PART_NAME_RE.match(path.name):
        return MIRRORED_RECORDS["facts"]
    for key in ("members", "canvas_assets"):
        if targets.get(key) == parent:
            return MIRRORED_RECORDS[key]
    inherited = targets.get("inherited")
    if inherited is not None and (parent == inherited or inherited in parent.parents):
        return MIRRORED_RECORDS["inherited"]
    raise UnclassifiedMirrorPath(path)


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


def _not_regular(path: Path) -> bool:
    """Is something other than a regular file (a FIFO, a socket, a device, a directory) sitting at ``path``?

    Asked before any read of a record, because opening a FIFO for reading blocks until something writes to it: one
    ``mkfifo knowledge.md`` in the checkout froze ``render`` -- and with it the notifier's tick -- for good.
    """
    try:
        return os.path.lexists(os.fspath(path)) and not path.is_symlink() and not path.is_file()
    except OSError:
        return True


def is_ours(path: Path) -> bool:
    """True when ``path`` is absent or carries our marker on its first line."""
    if _not_regular(path):
        return False
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


def digest_bytes(data: bytes) -> str:
    """``digest`` over bytes, for a file the guard must not have to decode.

    Identical to ``digest`` for anything that is valid UTF-8, so a digest
    recorded through either function compares against one recorded through the
    other. A document with one invalid byte, a merge conflict or a binary canvas
    asset still has to be recognised as "the bytes this team wrote", or not.
    """
    return hashlib.sha256(data).hexdigest()[:16]


def generated_text(path: Path, body: str, max_bytes: int = MAX_RENDER_BYTES, instance: Optional[str] = None) -> str:
    """Exactly what ``write_generated`` would put at ``path``: the marker, then ``body``.

    Factored out so the guard can ask "is what is on disk already the thing we
    are about to write?" with the same bytes the write will use, truncation
    included. Asking with the untruncated body made an over-cap record look like
    a stranger on every render of a folder nothing had touched. ``instance`` is
    ``marker_for``'s.
    """
    text = marker_for(path, instance) + "\n" + body
    if len(text.encode("utf-8")) > max_bytes:
        suffix = "\n…\n"
        keep = max_bytes - len(suffix.encode("utf-8"))
        text = text.encode("utf-8")[:keep].decode("utf-8", "ignore") + suffix
    return text


def in_mirror_folder(path: Path) -> bool:
    """Is ``path`` inside a ``.herdr-synapse`` folder -- the mirror's own territory?

    Cheap and name-based on purpose: it has to answer for a raw writer that was
    handed a path and no ``targets``, which is exactly the caller the
    classification must not depend on remembering anything. A current or legacy
    folder name anywhere above the file is enough, so nothing outside a team's
    folder is affected.
    """
    parts = Path(path).parts[:-1]
    return any(name in parts for name in (DIR_NAME,) + LEGACY_DIR_NAMES)


def still_holds(path: Path, observed: bytes) -> bool:
    """Does ``path`` still hold exactly ``observed``, the bytes the guard decided on?

    Asked immediately before a replace or an unlink, because ``os.replace`` cannot be made conditional and the
    checkout is writable by the agents: a file that changed since the look is refused rather than destroyed.
    Reading one byte past ``observed`` catches growth; anything the comparison cannot settle is a mismatch.
    """
    try:
        if path.is_symlink() or not path.is_file():
            return False
        with path.open("rb") as handle:
            data = handle.read(len(observed) + 1)
    except OSError:
        return False
    return data == observed


def write_generated(path: Path, body: str, force: bool = False, max_bytes: int = MAX_RENDER_BYTES,
                    classified: bool = False) -> bool:
    """Write one generated file. Returns True when the file changed.

    Refuses a file that exists without our marker unless ``force``. Never
    deletes: callers that need a file to stop meaning something write a
    tombstone over it instead.

    A path inside a ``.herdr-synapse`` folder is written only when the caller
    classified it (``classified=True``), which only ``write_disposable`` does:
    a durable record goes through ``document_sync.FolderGuard``, so this cannot
    be the way around the never-overwrite rule (``UnguardedMirrorWrite``).
    """
    text = generated_text(path, body, max_bytes)
    if not classified and in_mirror_folder(path):
        raise UnguardedMirrorWrite(path)
    if not force and not is_ours(path):
        raise ForeignFileError(path)
    try:
        current = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, ValueError):
        # ``ValueError`` is ``UnicodeDecodeError``: a file we cannot decode is
        # certainly not byte-identical to the text we are about to write.
        current = None
    if current == text:
        return False
    store.atomic_write(path, text.encode("utf-8"), fsync=False)
    return True


class PayloadTooLargeError(HerdrTeamError):
    """A generated JSON payload is over its cap, so it was not written at all."""

    def __init__(self, path: Path, size: int, cap: int) -> None:
        super().__init__(
            "workdir_payload_too_large",
            "{} would be {} bytes, over the {} byte cap; it was not written".format(path, size, cap),
            EXIT_REFUSED,
            {"path": os.fspath(path), "bytes": size, "cap": cap},
        )


def generated_json_text(path: Path, payload: Dict[str, Any]) -> str:
    """Exactly what ``write_generated_json`` would put at ``path``. No cap check.

    The guard's counterpart to ``generated_text``: it needs the bytes to compare
    against the file on disk, and the cap is the writer's refusal to make, not
    the guard's.
    """
    body = {k: v for k, v in payload.items() if k != "//"}
    encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":"))
    # ``MARKER_JSON`` is the opening brace plus the reserved key and its comma, so
    # the payload's own opening brace is dropped and the rest follows verbatim.
    return (MARKER_JSON[:-1] + "}\n") if encoded == "{}" else (MARKER_JSON + "\n" + encoded[1:] + "\n")


def write_generated_json(path: Path, payload: Dict[str, Any], max_bytes: int = MAX_CANVAS_JSON_BYTES) -> bool:
    """Write one generated JSON file outside a team folder. Returns True when the file changed.

    The Markdown sibling above truncates an over-cap body, which is right for prose a human reads and wrong for a
    payload a command reads back: half a scene is still valid JSON and still looks importable. So this **refuses**
    (``PayloadTooLargeError``). Inside a team folder the payload is a durable record and goes through
    ``document_sync.FolderGuard.write_json`` instead, which this refuses to stand in for.
    """
    text = generated_json_text(path, payload)
    size = len(text.encode("utf-8"))
    if size > max_bytes:
        raise PayloadTooLargeError(path, size, max_bytes)
    if in_mirror_folder(path):
        raise UnguardedMirrorWrite(path)
    if not is_ours(path):
        raise ForeignFileError(path)
    try:
        current = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError, ValueError):
        current = None
    if current == text:
        return False
    store.atomic_write(path, text.encode("utf-8"), fsync=False)
    return True


#: The reserved key this plugin's never-overwrite code writes into ``mirror.json`` (and, as ``document_sync.SCHEMA_KEY``,
#: into ``document-sync.json``). 0.22.1 and older never wrote it, so a state file *without* it is positive evidence that
#: an older plugin wrote the folder: what ``document_sync.FolderGuard`` decides an upgrade from. Its value is a dict, so
#: ``mirror_state`` (which keeps only string digests) never mistakes it for a file name.
STATE_MARK_KEY = "//"
STATE_SCHEMA = 2


def mirror_state(team_paths: Any) -> Dict[str, str]:
    """``{file name: digest}`` of what the plugin last wrote to each editable mirror."""
    doc = store.read_json(team_paths.mirror_json, default=None)
    if not isinstance(doc, dict):
        return {}
    return {k: v for k, v in doc.items() if isinstance(k, str) and isinstance(v, str)}


def mirror_mark(team_paths: Any) -> Optional[Dict[str, Any]]:
    """``mirror.json``'s state mark: None when the file is absent or unreadable, ``{}`` when an older plugin wrote it."""
    try:
        doc = store.read_json(team_paths.mirror_json, default=None)
    except (HerdrTeamError, OSError, ValueError):
        return None
    if not isinstance(doc, dict):
        return None
    mark = doc.get(STATE_MARK_KEY)
    return dict(mark) if isinstance(mark, dict) else {}


def save_mirror_state(team_paths: Any, state: Dict[str, str], upgraded: Optional[bool] = None) -> None:
    """Persist ``mirror.json`` with this plugin's state mark, carrying an upgrade already recorded there forward."""
    payload: Dict[str, Any] = {k: v for k, v in state.items() if isinstance(v, str)}
    existing = mirror_mark(team_paths)
    mark = dict(existing or {})
    if (existing is not None and "schema" not in existing) or upgraded:
        # Rewriting an older plugin's file is where its evidence would be lost, so the upgrade is recorded here.
        mark["upgraded"] = True
    mark["schema"] = STATE_SCHEMA
    payload[STATE_MARK_KEY] = mark
    try:
        store.write_json(team_paths.mirror_json, payload, fsync=False)
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
    if not recorded or _not_regular(path):
        return False
    try:
        current = path.read_text(encoding="utf-8")
    except (FileNotFoundError, OSError):
        return False
    except ValueError:
        return True  # one invalid byte is not what we wrote; see ``drifted``
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

class DurableRecordError(HerdrTeamError):
    """A record in a team's folder was about to be written outside ``document_sync.FolderGuard``. Refused.

    ``refresh_gitignore`` and the shared ``README.md`` go through ``write_disposable``, and ``board.md`` / ``canvas.md``
    through ``write_view``; each raises this for a path its table row does not give it, so the day somebody routes a
    durable record through either, that write fails loudly instead of quietly bypassing the guard.
    """

    def __init__(self, path: Path) -> None:
        super().__init__(
            "workdir_durable_record",
            "{} is written under the never-overwrite rule; write it through document_sync.FolderGuard".format(path),
            EXIT_REFUSED,
            {"path": os.fspath(path)},
        )


class ForeignViewError(ForeignFileError):
    """``board.md`` or ``canvas.md`` is a file without this plugin's marker: somebody's own file, left alone.

    No ``--force`` in the message: ``write_view`` never writes over such a file, forced or not, so moving it aside is
    the one repair, and it is the one printed.
    """

    def __init__(self, path: Path, why: str = "it does not carry herdr-synapse's marker") -> None:
        HerdrTeamError.__init__(
            self,
            "workdir_foreign_file",
            "{} was not written by this plugin ({}), so this team's regenerated {} is not written there; move "
            "the file aside with:\n  {}\nThe next snapshot writes the view.".format(
                path, why, Path(path).name, aside_command(path)),
            EXIT_REFUSED,
            {"path": os.fspath(path), "view": True},
        )


def aside_command(path: Any) -> str:
    """The ``mv`` that moves a file aside, to a name nothing has yet: ``mv`` silently replaces a file at its
    destination, and an earlier ``.aside`` is bytes the operator moved there on an earlier line's say-so."""
    import shlex

    base = os.fspath(path) + ".aside"
    target, number = base, 2
    while os.path.lexists(target):
        target, number = "{}-{}".format(base, number), number + 1
    return "mv {} {}".format(shlex.quote(os.fspath(path)), shlex.quote(target))


def write_disposable(path: Path, body: str, targets: Dict[str, Path], force: bool = False,
                     max_bytes: int = MAX_RENDER_BYTES) -> bool:
    """Write the shared folder's ``README.md`` or ``.gitignore``. True when it changed.

    Classifies the path exactly as the guard does -- an unclassified one is refused -- and refuses a durable record
    or a team-folder view (``VIEWS``, which ``write_view`` writes) outright, so there is no path into a team's folder
    that skips the never-overwrite rule by accident.
    """
    record = record_for(path, targets)
    if record.durable or record.key in VIEWS:
        raise DurableRecordError(path)
    return write_generated(path, body, force=force, max_bytes=max_bytes, classified=True)


#: The most ``write_view`` reads of what is at a view's path: past it the file is not a view this plugin wrote.
MAX_VIEW_READ_BYTES = 2 * MAX_BOARD_BYTES


def write_view(path: Path, body: str, targets: Dict[str, Path], max_bytes: int = MAX_RENDER_BYTES) -> bool:
    """Write ``board.md`` or ``canvas.md`` (``VIEWS``). True when it changed; ``ForeignViewError`` when left alone.

    A view is regenerated from this team's own board or scene and nothing reads it back, so a file at its path that
    carries this plugin's marker -- this team's earlier view, or a previous team's, git-ignored and on this disk only
    -- is written over, and nothing is ever held. Left untouched: a file without the marker, a symlink, a
    non-regular file, or one larger than any view; those are somebody's own and stay exactly as they are, with
    ``--force`` too. The file is looked at once and proved unchanged immediately before the replace, so a file that
    changes before that check is refused; an absent path is created exclusively for the same
    reason. A final check cannot make ``os.replace`` conditional: a writer landing after that check may still have
    its newer bytes replaced. This view is regenerated, not a durable record or an inheritance source.
    """
    record = record_for(path, targets)
    if record.key not in VIEWS:
        raise DurableRecordError(path)
    text = generated_text(path, body, max_bytes).encode("utf-8")
    path = Path(path)
    if path.is_symlink():
        raise ForeignViewError(path, "it is a symlink, which is never followed")
    if _not_regular(path):
        raise ForeignViewError(path, "it is not a regular file")
    try:
        with path.open("rb") as handle:
            current: Optional[bytes] = handle.read(MAX_VIEW_READ_BYTES + 1)
    except FileNotFoundError:
        current = None
    except OSError as err:
        raise ForeignViewError(path, "it cannot be read: {}".format(err.strerror or err))
    if current is None:
        from .document_sync import _create_exclusive

        _paths.ensure_dir(path.parent)
        if not _create_exclusive(path, text):
            raise ForeignViewError(path, "a file appeared there while this view was being written")
        return True
    if current == text:
        return False
    if len(current) > MAX_VIEW_READ_BYTES:
        raise ForeignViewError(path, "it is larger than any view this plugin writes")
    if not current.decode("utf-8", "replace").startswith(MARKER_PREFIXES):
        raise ForeignViewError(path)
    if not still_holds(path, current):
        raise ForeignViewError(path, "it changed while this view was being written")
    store.atomic_write(path, text, fsync=False)
    return True


# --------------------------------------------------------------------------
# rendering the mirror


README_BODY = """# herdr-synapse

This folder belongs to herdr-synapse. One subdirectory per team.

herdr-synapse writes a durable record here only when the path is empty or the file still
holds exactly what this team last wrote there. Anything else — a previous
team's file, a pull, your edit, an agent's — is left exactly as it is, and
`herdr-synapse project` lists it as held, with the commands that resolve it.
Nothing found here is copied or adopted by itself: `knowledge import --from
<this folder>/<team>`, `instructions <name> --adopt` and `canvas import` take
it in, in front of you. `project render --force` writes the team's version
over a held file, after copying it into `<team>/inherited/` and checking the
copy; if no checked copy can be made it changes nothing and says why.
`board.md` and `canvas.md` are disposable views: marker-bearing versions are
regenerated, and a file without the marker stays untouched until you move it.

- `<team>/knowledge.md` — team rules and attributed findings. With automatic
  document sync, edit only the Rules section; Findings stay generated.
  `herdr-synapse project` shows the mode; `project sync auto|manual` changes it.
  Auto mode proposes a settled edit of a document this team wrote with its full
  text; only the operator's `project confirm <id>` adopts it. Neither mode
  adopts an edit by itself. In manual mode,
  change rules with `knowledge set` and adopt member edits with
  `instructions --adopt`. Add a finding with `herdr-synapse knowledge add "..."`.
- `<team>/members/<name>.md` — what that member in particular is here to do,
  which is how agents sharing this folder are told apart.
- `<team>/facts.md` — what the team currently holds to be true, with each
  fact's id, author, sources and dispute state. A long record continues in
  `facts-2.md`, `facts-3.md`, ...; `facts.md` says how many parts there are.
- `<team>/canvas.json` plus `<team>/canvas-assets/` — the whiteboard in the
  form `canvas import` reads back, and the pictures and chart data its marks
  name. `<team>/canvas.md` is the same board as `canvas look` prints it.
- `<team>/inherited/` — copies `project render --force` (or a confirm, adopt or import
  from this folder) took before replacing a file
  this team did not write.
- `<team>/artifacts/` — yours. Put work products here and reference them from
  the board with `herdr-synapse post --ref`.
- `<team>/exports/` — where `herdr-synapse board export` writes.

What a clone carries: everything above except `inherited/`, `canvas.md`,
`board.md`, `artifacts/` and `exports/`, which the generated `.gitignore`
leaves out.

What never travels is what belonged to a conversation rather than to the
record: board posts, work items, open proposals, claims, locks, checkpoints and
the recall index. Those stay in the session archive, readable, and are not
restored. `herdr-synapse me` prints who you are and where these files are.
"""


def gitignore_body(teams: List[str]) -> str:
    """The generated ``.herdr-synapse/.gitignore``: what a clone does not need to *use* the team's record.

    Owner decision D1 (2026-10-08): commit what a new team needs to use the record -- ``knowledge.md``, ``facts.md``
    and its numbered parts, ``members/``, ``canvas.json`` and the ``canvas-assets/`` files it names -- and nothing
    else. ``inherited/`` holds the copies an operator-ordered overwrite took: they stay on this disk and do not travel.
    ``canvas.md`` is a readable listing nothing reads back, regenerated from the live scene.
    """
    lines = [
        "# Artifacts, board exports and the rolling board snapshot are working",
        "# files, not source: board.md is regenerated whenever the board moves.",
        "# canvas.md is a readable listing regenerated from the live scene.",
        "# inherited/ holds the copies taken before a file this team did not write",
        "# was replaced; they stay on this disk and do not travel with a clone.",
        "#",
        "# Kept, so a checkout carries what a new team needs to use the record:",
        "# knowledge.md, facts.md (and facts-2.md ... for a long record),",
        "# members/, canvas.json and canvas-assets/ -- the pictures and chart",
        "# data the canvas marks name, pruned when no mark names one.",
    ]
    for team in sorted(teams):
        for name in ("artifacts/", "exports/", "board.md", "canvas.md", "inherited/"):
            lines.append("{}/{}".format(team, name))
    if not teams:
        for name in ("artifacts/", "exports/", "board.md", "canvas.md", "inherited/"):
            lines.append("*/{}".format(name))
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


def _escape_line(text: str) -> str:
    """``render.escape_context_line``, imported once rather than once per field.

    ``facts.md`` is rendered on every refresh of the mirror, and a function-level
    import inside the per-field helpers below ran the import machinery about
    twelve times per fact -- measured as a fifth of a 3000-fact render.
    """
    if not _ESCAPE:
        from . import render as _render

        _ESCAPE.append(_render.escape_context_line)
    return _ESCAPE[0](text)


_ESCAPE: List[Any] = []


def _fact_field(row: Dict[str, Any], *names: str) -> str:
    """The first of ``names`` this row carries as a non-empty scalar, cleaned for one line."""
    for key in names:
        value = row.get(key)
        if value is None or isinstance(value, (list, tuple, dict, bool)):
            continue
        text = " ".join(str(value).split())
        if text:
            return _escape_line(text)
    return ""


def _fact_list(row: Dict[str, Any], *names: str) -> List[str]:
    """One row's list-valued field, flattened to printable strings."""
    for key in names:
        value = row.get(key)
        if not isinstance(value, (list, tuple)) or not value:
            continue
        out = []
        for item in value:
            if isinstance(item, dict):
                item = item.get("url") or item.get("ref") or item.get("name") or item.get("kind") or ""
            text = " ".join(str(item).split())
            if text:
                out.append(_escape_line(text))
        if out:
            return out
    return []


def _facts_intro(team_name: str, part: int = 1, parts: int = 1, total: int = 0) -> List[str]:
    """The heading and prose above the rows of one part of ``facts.md``.

    A single-part record has exactly the heading it always had, so a team whose
    facts fit in one file sees no change at all. A split record says, in part 1,
    how many parts there are and how many facts they hold in all, in the fixed
    form ``facts_parts_declared`` reads back -- which is what lets ``knowledge
    import`` tell a missing part from a record that was simply short.
    """
    if parts <= 1:
        out = ["# {} facts".format(team_name), ""]
    else:
        out = ["# {} facts (part {} of {})".format(team_name, part, parts), ""]
    if part == 1:
        out.append("What this team currently holds to be true, newest last. Add one with")
        out.append("`herdr-synapse fact add \"...\"`; support one with `fact support <id>`.")
        out.append("A fact a previous team recorded is attributed to it and is not this")
        out.append("team's own until somebody here supports it.")
        if parts > 1:
            out.append("")
            out.append(FACTS_PARTS_LINE.format(parts=parts, names=_facts_part_names(parts), total=total))
    else:
        out.append("Part {} of {} of this team's facts; `facts.md` is part 1 and says how many there are.".format(
            part, parts))
    out.append("")
    return out


#: Part 1's statement of how the record is split. Read back by ``facts_parts_declared``, so its wording is a
#: contract: the part count and the fact count are the two numbers an import checks what it read against.
FACTS_PARTS_LINE = ("This record is in {parts} parts ({names}) holding {total} facts in all; "
                    "`herdr-synapse knowledge import` reads every part.")
_FACTS_PARTS_RE = re.compile(r"^This record is in (\d+) parts \(.*\) holding (\d+) facts in all;")
#: ``facts-2.md``, ``facts-3.md``, ...: the parts after the first, beside ``facts.md``.
_FACTS_PART_NAME_RE = re.compile(r"^facts-([2-9]|[1-9][0-9]+)\.md$")


def facts_part_name(number: int) -> str:
    """The file name of part ``number`` of ``facts.md`` (part 1 is ``facts.md`` itself)."""
    return "facts.md" if number <= 1 else "facts-{}.md".format(int(number))


def facts_parts_declared(text: Any) -> Optional[Tuple[int, int]]:
    """``(parts, facts)`` as part 1 of a split ``facts.md`` states them, or None for a single-part record."""
    for line in str(text or "").splitlines()[:16]:
        match = _FACTS_PARTS_RE.match(line.strip())
        if match:
            return int(match.group(1)), int(match.group(2))
    return None


def _fact_row_lines(row: Dict[str, Any]) -> List[str]:
    """The lines one fact prints: its heading bullet, then its meta and sources lines when it has them."""
    out: List[str] = []
    fact_id = _fact_field(row, "id")
    statement = _fact_field(row, "statement", "text", "label")
    subject = _fact_field(row, "subject", "about")
    attribute = _fact_field(row, "attribute")
    head = "- " + ("**{}** ".format(fact_id) if fact_id else "")
    topic = " / ".join([part for part in (subject, attribute) if part])
    out.append(head + (topic + " — " if topic else "") + (statement or "_no statement_"))
    meta = []
    author = _fact_field(row, "author")
    kind = _fact_field(row, "author_kind", "kind")
    at = _fact_field(row, "at", "recorded_at")
    if author:
        meta.append("{}{}".format(author, " ({})".format(kind) if kind else ""))
    if at:
        meta.append(at)
    # ``confidence`` already reads as a phrase ("1 member, 2 sources"), and
    # ``status`` is "current" for almost every row, so it earns a field only
    # when it is not. The separators are the ones ``facts.parse_mirror``
    # reads this file back with: this line is a contract, not a layout.
    for label, keys in (("confidence", ("confidence",)), ("from", ("source_team", "inherited_from"))):
        value = _fact_field(row, *keys)
        if value:
            meta.append("{} {}".format(label, value))
    status = _fact_field(row, "status")
    if status and status != "current":
        meta.append("status {}".format(status))
    supporters = _fact_list(row, "supporters", "supported_by")
    if supporters:
        meta.append("supported by " + ", ".join(supporters))
    valid_from, valid_to = _fact_field(row, "valid_from"), _fact_field(row, "valid_to")
    if valid_from or valid_to:
        meta.append("valid {} → {}".format(valid_from or "—", valid_to or "—"))
    disputes = _fact_list(row, "disputes", "open_disputes")
    if disputes:
        meta.append("disputed " + ", ".join(disputes))
    if meta:
        out.append("  " + " · ".join(meta))
    sources = _fact_list(row, "sources")
    if sources:
        out.append("  sources: " + ", ".join(sources))
    return out


def facts_body(team_name: str, rows: List[Dict[str, Any]]) -> str:
    """The mirror of the team's current facts, with their provenance, as one document.

    ``knowledge.md``'s Findings line is a lossy version of this one: it keeps the
    statement and loses the fact id, the subject and attribute, the sources, the
    confidence, the supporters, the validity window and the dispute state. This
    is not a new channel, it is a better one, and it is what makes a team's facts
    inheritable rather than merely readable.

    A pure function of ``rows``, deliberately: no "now" anywhere, or the file
    rewrites itself on every pass and shows up as a change when nothing was
    learned. Every field is optional, so a richer row shape prints more without
    this having to change. ``facts_parts`` is what the mirror writes: this body
    whenever it fits under ``MAX_FACTS_BYTES``, numbered parts when it does not.
    """
    return _facts_whole(team_name, [_fact_block(row) for row in rows if isinstance(row, dict)])


def _fact_block(row: Dict[str, Any]) -> str:
    """One fact's lines, as the text block every part of ``facts.md`` is built from."""
    return "\n".join(_fact_row_lines(row)) + "\n"


def _facts_whole(team_name: str, blocks: List[str]) -> str:
    """The single-part ``facts.md`` body from already rendered row blocks."""
    head = "\n".join(_facts_intro(team_name)) + "\n"
    return head + ("".join(blocks) if blocks else "_None yet._\n")


def facts_parts(team_name: str, rows: List[Dict[str, Any]], cap: int = MAX_FACTS_BYTES) -> List[str]:
    """``facts.md`` as the bodies of its parts: one body when the record fits under ``cap``, more when it does not.

    The file used to be truncated at the cap with an ellipsis, so a team past
    roughly 1,100 facts committed a record silently short of its newest facts,
    and ``knowledge import --from <folder>`` reported the short count as a
    success. Nothing is cut now: the rows are dealt into parts in order, every
    part fits under ``cap`` with its marker, and part 1 states the part count and
    the fact count so an import can say exactly what it did not find.
    """
    # Every row is rendered once: the single-part body and the split are built from the same blocks.
    good = [row for row in rows if isinstance(row, dict)]
    blocks = [_fact_block(row) for row in good]
    whole = _facts_whole(team_name, blocks)
    marker = len((MARKER + "\n").encode("utf-8"))
    if marker + len(whole.encode("utf-8")) <= cap:
        return [whole]
    # Part 1's heading is the longest any part carries, and it names the parts in a bounded form
    # (``_facts_part_names``), so a fixed reserve covers it whatever the count.
    budget = cap - marker - FACTS_HEADING_RESERVE
    chunks: List[List[str]] = [[]]
    used = 0
    for block in blocks:
        size = len(block.encode("utf-8"))
        if chunks[-1] and used + size > budget:
            chunks.append([])
            used = 0
        chunks[-1].append(block)
        used += size
    parts = len(chunks)
    return ["\n".join(_facts_intro(team_name, number, parts, len(good))) + "\n" + "".join(chunk)
            for number, chunk in enumerate(chunks, 1)]


#: Bytes kept free in every part of a split ``facts.md`` for its heading and prose. Part 1's is the longest, at well
#: under 1 KiB however many parts there are, because the part names are written in a bounded form.
FACTS_HEADING_RESERVE = 2048


def _facts_part_names(parts: int) -> str:
    """``facts.md, facts-2.md, facts-3.md``, or ``facts.md, facts-2.md … facts-12.md`` past three parts."""
    if parts <= 3:
        return ", ".join(facts_part_name(n) for n in range(1, parts + 1))
    return "facts.md, facts-2.md … {}".format(facts_part_name(parts))


#: The fact views the mirror renders, per team, keyed by what the fact log is on disk. ``render`` runs for every team
#: on every roster refresh -- roughly every two seconds in the notifier -- and the log only changes when a fact does,
#: so replaying it and rendering ``facts.md`` again each time was the per-tick cost a long-lived team paid for
#: nothing. Both views are pure functions of the log (no clock is read), so a log whose size, mtime and inode are
#: unchanged gives the same Findings and the same ``facts.md`` parts. Bounded, and a stat that fails is a miss.
_FACT_VIEWS: Dict[str, Tuple[Any, List[Dict[str, Any]], List[str]]] = {}
_FACT_VIEWS_MAX = 64


def _fact_log_key(team_paths: Any) -> Optional[Tuple[Any, ...]]:
    from . import facts as _facts

    key: List[Any] = []
    for path in (_facts.facts_jsonl(team_paths), team_paths.knowledge_jsonl):
        try:
            st = os.stat(os.fspath(path))
        except FileNotFoundError:
            key.append(None)
            continue
        except OSError:
            return None
        key.append((st.st_size, st.st_mtime_ns, st.st_ino))
    return tuple(key)


def _fact_views(team_paths: Any, team_name: str, limit: int) -> Tuple[List[Dict[str, Any]], List[str]]:
    """``(findings for knowledge.md, the bodies of facts.md's parts)``, from one replay of the fact log.

    The two used to replay the log once each, per render (RELE-1). A log that cannot be read gives no findings and
    an empty record, never an exception.
    """
    from . import facts as _facts

    key = _fact_log_key(team_paths)
    slot = "{}\0{}\0{}".format(os.fspath(_facts.facts_jsonl(team_paths)), team_name, limit)
    cached = _FACT_VIEWS.get(slot)
    if key is not None and cached is not None and cached[0] == key:
        return cached[1], cached[2]
    try:
        state: Any = _facts.load(team_paths)
    except (HerdrTeamError, OSError, ValueError, TypeError):
        state = None
    try:
        findings = _facts.findings_view(team_paths, limit=limit, state=state)
    except (HerdrTeamError, OSError, ValueError, TypeError):
        findings = []
    bodies = facts_parts(team_name, facts_rows(team_paths, state=state))
    if key is not None and state is not None:
        if len(_FACT_VIEWS) >= _FACT_VIEWS_MAX:
            _FACT_VIEWS.clear()
        _FACT_VIEWS[slot] = (key, findings, bodies)
    return findings, bodies


def facts_part_plan(targets: Dict[str, Path], team_name: str, rows: Optional[List[Dict[str, Any]]] = None,
                    bodies: Optional[List[str]] = None) -> List[Tuple[Path, str]]:
    """Every part of ``facts.md`` this render writes, as ``(path, body)``, including the parts it has to empty.

    A part the record no longer needs is never deleted -- the mirror deletes no
    document -- so a ``facts-3.md`` left by a longer record is rewritten to say
    it is empty, and only when it is already there: a record that fits in one
    file creates no extra files.
    """
    if bodies is None:
        bodies = facts_parts(team_name, rows or [])
    out = [(targets["facts"] if number == 1 else targets["root"] / facts_part_name(number), body)
           for number, body in enumerate(bodies, 1)]
    try:
        stale = sorted(int(m.group(1)) for m in (_FACTS_PART_NAME_RE.match(entry.name)
                                                  for entry in targets["root"].iterdir()) if m)
    except OSError:
        stale = []
    for number in stale:
        if number > len(bodies):
            out.append((targets["root"] / facts_part_name(number),
                        "# {} facts (empty part)\n\nThis part is empty: every current fact is in the parts "
                        "`facts.md` names.\n".format(team_name)))
    return out


def facts_rows(team_paths: Any, limit: int = 0, state: Any = None) -> List[Dict[str, Any]]:
    """The rows ``facts.md`` renders (seam S5): ``facts.mirror_rows``.

    The richer view: subject, attribute, sources, confidence, supporters,
    validity window and dispute state. ``state`` is a fact log the caller has
    already replayed, so ``_render_locked`` reads the log once for both this and
    the Findings section of ``knowledge.md`` instead of twice per render. A log
    that cannot be read gives no rows, never an exception.
    """
    from . import facts as _facts

    try:
        rows = _facts.mirror_rows(team_paths, limit=limit, state=state)
    except (HerdrTeamError, OSError, ValueError, TypeError):
        return []
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def canvas_body(team_name: str, listing: str, scene_version: int = 0, elements: int = 0,
                too_large: Optional[Dict[str, Any]] = None) -> str:
    """The readable mirror of the canvas: what ``canvas look`` prints, plus how to read it back.

    A pure function of its arguments. The human's copy of the board; the machine's
    is ``canvas.json`` beside it, and the one sentence that matters here is the one
    naming the command that replays it.
    """
    out = ["# {} canvas".format(team_name), ""]
    out.append("The team's whiteboard as `herdr-synapse canvas look` prints it: {} element{} at version {}.".format(
        elements, "" if elements == 1 else "s", scene_version))
    if too_large:
        out.append("")
        out.append("The scene itself was too large to mirror ({} bytes, cap {}). Export it from the".format(
            too_large.get("bytes"), too_large.get("cap")))
        out.append("live session and import that file instead:")
        out.append("")
        out.append("    {}".format(too_large.get("export")))
        out.append("    {}".format(too_large.get("import")))
    else:
        out.append("The importable scene is `canvas.json`; replay it into another team's canvas with")
        out.append("")
        out.append("    herdr-synapse canvas import --from <this folder>/canvas.json")
    out.append("")
    body = (listing or "").rstrip("\n")
    out.append(body if body else "_The canvas is empty._")
    return "\n".join(out) + "\n"


def adopt_command(name: str) -> str:
    """The command that imports an edit to this member's file."""
    return "herdr-synapse instructions {} --adopt".format(name)


def member_body(team_name: str, name: str, role: str, brief: Optional[str], instructions: Optional[str], left_for: Optional[str] = None, left: bool = False, auto_sync: bool = False) -> str:
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
    if auto_sync:
        note.insert(0, "<!-- Saved edits are proposed to the operator, who confirms them; nothing is adopted by itself. Private Notes are not sent to agents. -->")
    return _doc.document(name, team_name, role, sections, adopt_command=None if auto_sync else adopt_command(name), note=note)


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
    from .document_sync import document_lock
    with document_lock(layout.team(team_name)):
        return _render_locked(layout, team_name, force)


def _render_locked(layout: Any, team_name: str, force: bool = False) -> Dict[str, Any]:
    """Regenerate every mirrored file for one team. Returns what changed.

    Best effort by design: this runs from commands whose real job is something
    else, so a read-only checkout or a foreign file is reported, never fatal.
    Nothing here deletes; a member that left gets a tombstone written over its
    file so the folder keeps a record instead of a hole.

    Every durable record goes through ``document_sync.FolderGuard``, which
    writes only an empty path or bytes this team last wrote, and holds anything
    else. ``result["holds"]`` is every hold this render met, for a caller that
    lists them; ``result["inherited"]`` is the holds and checked copies no
    consumer has shown yet, re-emitted until one calls
    ``document_sync.mark_reported``, so a silent caller costs the operator only
    the delay to the daemon's next tick.
    """
    from . import charter as _charter
    from . import roster as _roster
    from . import document_sync as sync

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
    # The fact log is replayed once per render and both views are built from that
    # one state. They each used to call ``facts.load``, which made the notifier's
    # per-tick refresh roughly four times slower for a long-lived team -- including
    # one that never touched inheritance -- because ``render`` runs for every team
    # on every roster refresh.
    findings, fact_parts = _fact_views(team_paths, team_name, _charter.MAX_FINDINGS_SHOWN)

    plan: List[Tuple[Path, str, str]] = [
        (targets["readme"], README_BODY, "generated"),
        (targets["knowledge"], knowledge_body(team_name, rules, findings), "generated"),
    ]
    # ``facts.md`` is split into numbered parts rather than truncated: a truncated
    # record is a lost one, and ``knowledge import`` used to report the short count
    # as a success. Each part is under its own cap.
    caps: Dict[Path, int] = {}
    for part_path, part_body in facts_part_plan(targets, team_name, bodies=fact_parts):
        plan.append((part_path, part_body, "generated"))
        caps[part_path] = MAX_FACTS_BYTES
    for member in doc.members:
        if member.is_human:
            continue
        instructions = _charter.get_instructions(layout, team_name, member.name)
        plan.append((
            targets["members"] / (_paths._safe_stem(member.name, "name") + ".md"),
            member_body(team_name, member.name, member.role or "", member.brief, instructions, left=member.status == "left", auto_sync=sync.enabled(doc)),
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
        _paths.ensure_dir(targets["root"])
        existing_teams = _existing_team_dirs(targets["shared"], team_name)
        plan.append((targets["gitignore"], gitignore_body(existing_teams), "generated"))
    except (HerdrTeamError, OSError) as err:
        result["reason"] = "cannot create {}: {}".format(targets["root"], err)
        return result
    # ``members/`` and ``artifacts/`` each on their own: a ``members`` that is a regular file stops only the member
    # documents, and the message names the path that failed rather than the team folder, which exists.
    members_ok = True
    for key in ("members", "artifacts"):
        try:
            _paths.ensure_dir(targets[key])
        except (HerdrTeamError, OSError) as err:
            message = "cannot create {}: {}".format(targets[key], err)
            result.setdefault("errors", []).append(message)
            if key == "members":
                members_ok = False
                result["reason"] = message + "; the member documents were not written"

    guard = sync.FolderGuard(layout, team_name, targets, force=force)
    state = mirror_state(team_paths)
    state_changed = False
    auto_sync = sync.enabled(doc)
    sync_names = {m.name for m in doc.members if not m.is_human and m.status in ("active", "starting")}
    for path, body, kind in plan:
        if not members_ok and path.parent == targets["members"]:
            result.setdefault("blocked", []).append(os.fspath(path))
            continue
        cap = caps.get(path, MAX_RENDER_BYTES)
        sync_document = auto_sync and (path == targets["knowledge"] or (kind == EDITABLE and path.stem in sync_names))
        if kind == EDITABLE and not sync_document and not force and edited(path, state.get(path.name)):
            # The operator's own document. Overwriting it here is what made the
            # file uneditable; it waits for ``instructions --adopt`` instead,
            # which is also the step that confers authority on the edit, since
            # the checkout is writable by the agents themselves.
            result["awaiting_adopt"].append(os.fspath(path))
            continue
        try:
            if not record_for(path, targets).durable:
                if write_disposable(path, body, targets, force=force, max_bytes=cap):
                    result["written"].append(os.fspath(path))
                continue
            # A "changed" hold on a synced document is the edit document sync is about to propose, and on a member
            # document in manual mode the edit the notifier announces for ``--adopt``: each is announced that way,
            # so it is quiet here -- unless, below, it turns out nothing will announce it.
            outcome = guard.write_text(path, body, max_bytes=cap, quiet=sync_document or kind == EDITABLE,
                                       keep_missing=sync_document)
        except ForeignFileError:
            result["skipped"].append(os.fspath(path))
            continue
        except (HerdrTeamError, OSError, ValueError) as err:
            result["skipped"].append(os.fspath(path))
            result.setdefault("errors", []).append("{}: {}".format(path, err))
            continue
        if outcome == "written":
            result["written"].append(os.fspath(path))
        elif outcome == "held":
            entry = guard.entry(path)
            hold = entry.get("hold") or {}
            reason = hold.get("reason")
            if sync_document and reason == "changed" and entry.get("settled") != hold.get("digest"):
                # Document sync has not finished with this edit: it reports what it adopts, or the error.
                result.setdefault("sync_pending", []).append(os.fspath(path))
            elif kind == EDITABLE and not sync_document and reason == "changed" and is_ours(path):
                result["awaiting_adopt"].append(os.fspath(path))
            else:
                # Nothing else will say this one -- an edit document sync settled with nothing to adopt, a member
                # document without the marker ``--adopt`` needs, a merge conflict -- so it is reported like any hold.
                guard.quiet.discard(os.fspath(path))
                result["skipped"].append(os.fspath(path))
            continue
        if kind == EDITABLE and (outcome == "written" or path.name not in state):
            try:
                state[path.name] = digest(path.read_text(encoding="utf-8"))
                state_changed = True
            except (OSError, ValueError):
                pass
    if state_changed:
        save_mirror_state(team_paths, state, upgraded=guard.upgrade)
    result["holds"] = [record for record in guard.records() if record["held"]]
    result["inherited"] = guard.reports()
    guard.save()
    for message in guard.errors:
        result.setdefault("errors", []).append(message)
    result["foreign_members"] = _foreign_members(targets["members"], {path for path, _, _ in plan})
    return result


def _foreign_members(members_dir: Path, planned: Any) -> List[str]:
    """Member documents in the folder for names this team does not use.

    A project dir outlives the roster that filled it, so a team whose only
    member is ``beta-worker`` can be rendering beside ``alpha-worker.md`` and its
    ghost mission. Nothing is adopted from these and nothing is deleted (rule 3),
    which is why this is reporting rather than repair -- but reporting it is the
    difference between an operator who knows the folder has a stranger in it and
    one who finds out when an agent quotes it.
    """
    out: List[str] = []
    try:
        entries = sorted(members_dir.iterdir())
    except OSError:
        return out
    for entry in entries:
        if entry.suffix != ".md" or entry in planned or entry.is_dir():
            continue
        out.append(os.fspath(entry))
    return out


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
        if path_.exists() and not is_ours(path_):
            out["issues"].append("{} was not written by herdr-synapse".format(label))
    # A member's own document is the one mirror the operator edits, so both a
    # foreign file and a pending edit matter here; neither used to be checked.
    for entry in out["members"]:
        member_file = targets["members"] / (str(entry["name"]) + ".md")
        if not member_file.exists():
            continue
        if not is_ours(member_file):
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
        return write_disposable(targets["gitignore"], gitignore_body(teams), targets, force=force)
    except (ForeignFileError, HerdrTeamError, OSError):
        return False


def render_board_snapshot(layout: Any, team_name: str, force: bool = False) -> Dict[str, Any]:
    """Write the team's whole board to ``<team>/board.md``, archive included.

    The board is the team's record of what happened, and it lived only in the
    plugin's state dir where nobody looks. This keeps a readable copy beside
    the team's rules and per-member instructions, regenerated whenever the
    board moves. It is a disposable view (``write_view``): never read back,
    listed in the generated ``.gitignore`` because it is rewritten constantly
    and would otherwise dominate every diff, written over any file carrying the
    marker and never over one without it. ``force`` changes nothing for a view
    and is accepted for the callers that pass their own ``--force`` through.

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
        # A disposable view (owner decision, 2026-10-09): written over any file
        # carrying the marker, never over one without it, never held.
        out["written"] = write_view(target, body, paths_for(project, team_name), max_bytes=MAX_BOARD_BYTES)
    except ForeignViewError as err:
        out["reason"] = err.message
        out["foreign"] = os.fspath(target)
    except (HerdrTeamError, OSError, ValueError) as err:
        out["reason"] = "{}: {}".format(type(err).__name__, err)
    return out


# --------------------------------------------------------------------------
# the canvas snapshot


def canvas_listing(team_paths: Any, scene: Dict[str, Any]) -> Optional[str]:
    """``canvas.snapshot_listing`` (seam S1): the readable listing, with no side effects.

    Deliberately not ``canvas.look``. A read must never regenerate a mirror, and
    ``look`` writes presence and a cursor: mirroring through it would make the
    snapshot tick look like a reader at the board and would put the renderer's
    own footprints in the file it is writing. ``None`` when the seam is not
    available, which skips the canvas mirror rather than failing the render.
    """
    try:
        from . import canvas as _canvas
    except ImportError:
        return None
    listing = getattr(_canvas, "snapshot_listing", None)
    if listing is None:
        return None
    text = listing(team_paths, scene)
    return text if isinstance(text, str) else None


def canvas_payload(scene: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """``canvas_import.payload`` (seam S2): what ``canvas.json`` carries.

    The producer and the consumer of the payload are one role on purpose, so the
    mirror asks for it rather than deciding what travels. ``None`` when the
    import module is not available.
    """
    try:
        from . import canvas_import as _canvas_import
    except ImportError:
        return None
    build = getattr(_canvas_import, "payload", None)
    if build is None:
        return None
    payload = build(scene)
    return payload if isinstance(payload, dict) else None


def canvas_asset_names(payload: Dict[str, Any]) -> List[str]:
    """``canvas_import.asset_names`` (seam S2): the asset names the carried elements reference."""
    try:
        from . import canvas_import as _canvas_import
    except ImportError:
        return []
    names = getattr(_canvas_import, "asset_names", None)
    if names is None:
        return []
    found = names(payload)
    return sorted({str(name) for name in found}) if isinstance(found, (list, tuple, set)) else []


def _asset_hash(name: str) -> str:
    """The content hash a canvas asset's name is built from (``<sha256[:32]>.<ext>``)."""
    return str(name).split(".", 1)[0]


def mirror_canvas_assets(source_dir: Path, target_dir: Path, names: List[str],
                         guard: Any) -> Dict[str, Any]:
    """Copy the assets the carried elements name. ``{copied, omitted, conflict}``.

    The one unmarked mirror: these files are opaque content and a marker would corrupt them, so the **name is the
    integrity check** instead. Written only where nothing is (``guard.create``, which proves the absence in the same
    operation as the write), never overwritten. A name already there whose bytes do not hash to it is left exactly
    alone, held and reported, because one of the two is wrong and this is not the code that gets to decide which.

    Only the named assets, never the whole ``assets/`` directory: that one is append-only and holds orphans a
    ``clear`` left behind that no live element references.
    """
    out: Dict[str, Any] = {"copied": [], "omitted": [], "conflict": []}
    if not names:
        return out
    total = 0
    for name in names:
        if not name or "/" in name or "\\" in name or name.startswith("."):
            out["omitted"].append({"name": str(name), "bytes": 0, "why": "not an asset name"})
            continue
        source = source_dir / name
        target = target_dir / name
        try:
            if target.is_symlink() or source.is_symlink():
                out["omitted"].append({"name": name, "bytes": 0, "why": "symlink"})
                continue
            if target.is_file():
                data = store.read_bytes(target)
                if data is not None and hashlib.sha256(data).hexdigest()[:32] != _asset_hash(name):
                    out["conflict"].append({"name": name})
                    guard.hold(target, "canvas-asset", "conflict", digest_bytes(data))
                elif data is not None:
                    # A mark names it and its bytes are the ones its name says: whatever it was held for (no mark
                    # named it, its bytes did not match) is no longer true, so the hold goes. It is still not claimed.
                    guard.clear_hold(target)
                continue
            if not source.is_file():
                out["omitted"].append({"name": name, "bytes": 0, "why": "missing from the session"})
                continue
            size = source.stat().st_size
            if size > MAX_CANVAS_ASSET_BYTES:
                out["omitted"].append({"name": name, "bytes": int(size), "why": "over the per-asset cap"})
                continue
            if total + size > MAX_CANVAS_ASSETS_BYTES:
                out["omitted"].append({"name": name, "bytes": int(size), "why": "over the total asset budget"})
                continue
            data = store.read_bytes(source)
            if data is None:
                out["omitted"].append({"name": name, "bytes": int(size), "why": "unreadable"})
                continue
            if not guard.create(target, data):
                # Something took the name between the look above and the write: left exactly as it is.
                out["conflict"].append({"name": name})
                continue
            total += len(data)
            out["copied"].append(name)
        except (HerdrTeamError, OSError) as err:
            # A fixed word, never ``str(err)``: an OSError's text carries the path and the temp file's random suffix,
            # and this string reaches the committed ``canvas.json``.
            out["omitted"].append({"name": name, "bytes": 0,
                                   "why": "unwritable" if isinstance(err, OSError) else "refused",
                                   "error": type(err).__name__})
    return out


def _under_state_root(layout: Any, path: Path) -> str:
    """``path`` written relative to the state root, or its last three segments when it is not under one.

    Only for strings that go into a generated file somebody commits. Everything the result dict carries, which the
    daemon logs and no checkout sees, keeps the real path.
    """
    tail = os.fspath(Path(*path.parts[-4:])) if len(path.parts) > 4 else path.name
    try:
        relative = os.path.relpath(os.fspath(path), os.fspath(layout.state_root.path))
    except (AttributeError, OSError, TypeError, ValueError):
        return tail
    return tail if relative.startswith("..") or os.path.isabs(relative) else relative


def prune_canvas_assets(guard: Any, target_dir: Path, names: List[str]) -> Dict[str, Any]:
    """Remove the mirrored assets no carried element names. ``{pruned, held}``.

    The one place the mirror deletes a file, and it deletes only a file whose bytes this team wrote there
    (``guard.remove`` compares them with the recorded digest on one look and proves them again before the
    ``unlink``). Any other file -- a previous team's picture, a stray, one a pull replaced -- stays where it is and is
    reported once. A symlinked ``canvas-assets/`` is never listed: it could point at a repository's ``src/``.
    """
    keep = set(names)
    out: Dict[str, Any] = {"pruned": [], "held": []}
    if guard.through_symlink(target_dir / "x"):
        out["refused"] = "{} is a symlink, which is never followed; replace it with a directory".format(target_dir)
        return out
    # A held picture somebody removed by hand -- the docs tell them to -- is no longer anything to report.
    guard.forget_gone(target_dir)
    try:
        entries = sorted(target_dir.iterdir())
    except OSError:
        return out
    for entry in entries:
        if entry.name in keep or entry.name.startswith(".") or entry.is_dir():
            continue
        try:
            outcome = guard.remove(entry)
        except (HerdrTeamError, OSError):
            outcome = "held"
        if outcome == "removed":
            out["pruned"].append(entry.name)
        elif outcome == "held":
            out["held"].append(entry.name)
    return out


def render_canvas_snapshot(layout: Any, team_name: str, force: bool = False) -> Dict[str, Any]:
    """Mirror the team's canvas into the knowledge path: ``canvas.md`` + ``canvas.json``.

    The canvas was the largest thing a team made that died with its session:
    ``dissolve`` archived it and nothing ever read it back, and a new team on the
    same knowledge path started at zero elements. This is the half that makes it
    durable; ``canvas import`` is the half that reads it back.

    Two files because they have two readers. ``canvas.md`` is for the human: it
    is the listing, readable in the checkout, regenerated from the scene and
    git-ignored. ``canvas.json`` is the payload, which is why it refuses to be
    truncated and degrades to a pointer at the live scene instead of shipping
    half a board that looks importable.

    The payload and the directory of pictures are ``DURABLE`` in
    ``MIRRORED_RECORDS``, so they go through the guard exactly as ``render``
    does; the listing is disposable (owner decision D1) and goes through
    ``write_view``. The empty-canvas early return
    below is *not* the guard: it only means a team that has drawn nothing never
    reaches the folder at all. Once this team's scene version has moved once,
    the guard is what stands between a previous team's committed board and the
    first mark anyone draws -- including after ``canvas undo B-1``, the
    documented way to take an inherited board back off, which returns the team
    to zero elements at a non-zero version and used to write ``elements: []``
    straight over the payload.

    Takes the document lock, because the guard's records live in
    ``document-sync.json`` beside the auto-sync path's: it must not be called
    while that lock is held.

    Best effort, like every other mirror: a read-only checkout, a full disk or a
    foreign file is reported in the result and never raised. "Cannot write the
    mirror" must never surface as "cannot draw."
    """
    from . import roster as _roster
    from .document_sync import document_lock

    team_paths = layout.team(team_name)
    doc = _roster.load_team(team_paths)
    project = project_dir_of(doc.to_json())
    out: Dict[str, Any] = {"team": team_name, "path": None, "elements": 0, "version": 0,
                           "written": [], "assets": 0}
    if not project:
        out["reason"] = "no project directory"
        return out
    try:
        from . import canvas as _canvas
        scene = _canvas.load_scene(team_paths)
    except (ImportError, HerdrTeamError, OSError, ValueError) as err:
        out["reason"] = "cannot read the canvas: {}".format(err)
        return out
    elements = scene.get("elements") if isinstance(scene, dict) else None
    elements = elements if isinstance(elements, list) else []
    version = int(scene.get("version") or 0) if isinstance(scene, dict) else 0
    out["elements"], out["version"] = len(elements), version
    if not elements and not version:
        out["reason"] = "the canvas has not been drawn on"
        return out

    targets = paths_for(project, team_name)
    out["path"] = os.fspath(targets["canvas_md"])
    listing = canvas_listing(team_paths, scene)
    payload = canvas_payload(scene)
    if listing is None or payload is None:
        out["reason"] = "the canvas mirror needs canvas.snapshot_listing and canvas_import.payload"
        return out

    try:
        _paths.ensure_dir(targets["root"])
    except (HerdrTeamError, OSError) as err:
        out["reason"] = "cannot create {}: {}".format(targets["root"], err)
        return out

    with document_lock(team_paths):
        return _canvas_snapshot_locked(layout, team_name, targets, team_paths, _canvas,
                                       scene, elements, version, listing, payload, out, force)


def _canvas_snapshot_locked(layout: Any, team_name: str, targets: Dict[str, Path], team_paths: Any,
                            _canvas: Any, scene: Dict[str, Any], elements: List[Any], version: int,
                            listing: str, payload: Dict[str, Any], out: Dict[str, Any],
                            force: bool) -> Dict[str, Any]:
    """The writing half of ``render_canvas_snapshot``, under the document lock."""
    from . import document_sync as sync

    guard = sync.FolderGuard(layout, team_name, targets, force=force)
    assets = mirror_canvas_assets(
        _canvas._dir(team_paths) / _canvas.ASSETS_DIR, targets["canvas_assets"],
        canvas_asset_names(payload), guard)
    if assets["omitted"]:
        payload["assets_omitted"] = assets["omitted"]
    if assets["conflict"]:
        payload["assets_conflict"] = assets["conflict"]
    out["assets"] = len(assets["copied"])
    out["assets_omitted"] = assets["omitted"]
    out["assets_conflict"] = assets["conflict"]

    too_large: Optional[Dict[str, Any]] = None
    outcome = None
    try:
        outcome = guard.write_json(targets["canvas_json"], payload, max_bytes=MAX_CANVAS_JSON_BYTES)
    except PayloadTooLargeError as err:
        details = err.details if isinstance(getattr(err, "details", None), dict) else {}
        too_large = {
            "bytes": details.get("bytes"), "cap": details.get("cap"),
            # Relative to the state root, because this file is committed: the absolute path is
            # /Users/<name>/.local/state/herdr/... on a real machine, and the operator's home directory is not
            # something a shared repository should learn. The two commands below are the usable pointer anyway.
            "scene": _under_state_root(layout, _canvas._dir(team_paths) / _canvas.SCENE_FILE),
            "export": "herdr-synapse canvas export --team {} --format json --out {}-canvas.json".format(team_name, team_name),
            "import": "herdr-synapse canvas import --from {}-canvas.json".format(team_name),
        }
        pointer = {k: v for k, v in payload.items() if k in ("payload", "v", "team", "scene_version", "updated_at")}
        pointer["elements"] = None
        pointer["element_count"] = len(elements)
        pointer["too_large"] = too_large
        try:
            # The pointer gets a cap of its own: it is a fixed handful of keys plus two commands.
            outcome = guard.write_json(targets["canvas_json"], pointer, max_bytes=MAX_RENDER_BYTES)
            out["too_large"] = too_large
        except (HerdrTeamError, OSError) as inner:
            out["reason"] = "{}: {}".format(targets["canvas_json"], inner)
    except (HerdrTeamError, OSError) as err:
        out["reason"] = "{}: {}".format(targets["canvas_json"], err)
    if outcome == "written":
        out["written"].append(os.fspath(targets["canvas_json"]))
    elif outcome == "held":
        out["reason"] = "{} is held: this team's board is not written there until the hold is resolved".format(
            targets["canvas_json"])
        # A hold is reported once through ``inherited``; the notifier need not rebuild the scene every ten seconds
        # to learn again that the file is still held.
        out["held_only"] = True
    payload_written = outcome in ("written", "unchanged")

    # Only after this team's payload is on disk: a held ``canvas.json`` still names its pictures, and a picture this
    # team wrote may be one that payload names too.
    pruned: Dict[str, Any] = {"pruned": [], "held": []}
    if payload_written and too_large is None:
        # A second team on the same folder (another session on one checkout) is not serialized with this one, so a
        # picture this payload names may have gone since it was mirrored above; mirror it again.
        gone = [name for name in canvas_asset_names(payload)
                if name and not os.path.lexists(os.fspath(targets["canvas_assets"] / name))]
        if gone:
            again = mirror_canvas_assets(_canvas._dir(team_paths) / _canvas.ASSETS_DIR, targets["canvas_assets"],
                                         gone, guard)
            if again["copied"]:
                out["assets_remirrored"] = again["copied"]
    if payload_written:
        pruned = prune_canvas_assets(guard, targets["canvas_assets"], canvas_asset_names(payload))
    out["assets_pruned"] = pruned["pruned"]
    out["assets_held"] = pruned["held"]
    if pruned.get("refused"):
        out.setdefault("errors", []).append(pruned["refused"])

    body = canvas_body(team_name, listing, scene_version=version, elements=len(elements), too_large=too_large)
    try:
        # A disposable view (owner decision, 2026-10-09): git-ignored, read back by nothing, a pure function of the
        # scene, so it is written over any file carrying the marker, never over one without it, and never held.
        if write_view(targets["canvas_md"], body, targets):
            out["written"].append(os.fspath(targets["canvas_md"]))
    except ForeignViewError as err:
        out["reason"] = err.message
        out["foreign"] = os.fspath(targets["canvas_md"])
        out.pop("held_only", None)
    except (HerdrTeamError, OSError, ValueError) as err:
        out["reason"] = "{}: {}".format(targets["canvas_md"], err)
        out.pop("held_only", None)

    out["holds"] = [record for record in guard.records() if record["held"]]
    out["inherited"] = guard.reports()
    guard.save()
    for message in guard.errors:
        out.setdefault("errors", []).append(message)
    return out
