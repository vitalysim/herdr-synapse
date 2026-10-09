"""Guard durable mirrors and require explicit consent before adopting document edits.

**The one rule** (owner decision, 2026-10-08). The mirror writes or deletes a durable file in a team's folder only
when the path is absent, or when the file holds exactly the bytes this team last wrote there (its recorded
digest). In every other case it leaves the file untouched, records a *hold* with the reason, and reports it
once to a consumer that shows it. Nothing found in place is copied or adopted by itself.

Why a rule rather than a careful copy. The design this replaces copied a file this team had not written into
``<team>/inherited/`` and then overwrote it. Six review rounds found every one of its data losses in that
step -- a copy that failed, collided, was too large, landed in session state, or was edited afterwards, each
turning a report of "copied" into a false statement over a destroyed file -- and every laundering route (an
agent's forged ``knowledge.md`` adopted as the operator's Rules) in its automatic first-contact adoption.
Not copying and not adopting removes both mechanisms instead of hardening them a seventh time. The cost is
that a folder can stay held until the operator resolves it, which the owner accepted.

**Replacing bytes this team did not write** happens on an explicit operator act -- ``project render
--force``, or bytes a confirmation, adoption, import or discard *released* (``release``).
``FolderGuard`` then reads the file once, writes a copy named by its content digest
under ``inherited/``, reads the copy back and compares it, and replaces the file only when the copy verified
and the file still holds the bytes it read. Anything else -- too large, unwritable, a different file at the
copy's name, the file changed meanwhile -- refuses and changes nothing.

**Trust.** Neither sync mode adopts edits automatically. In ``auto`` mode, ``scan`` proposes a settled edit of
a document this team wrote, showing the complete text that would be stored. Only a verified operator's
``project confirm <id>`` adopts it. The id binds the bytes, project, team instance, stored text and revision,
and member identity. A changed context requires a new proposal. ``manual`` holds edits for explicit import
or adoption instead. The checkout cannot tell who edited a file, so its marker is attribution, not authority.

**A claim is not authorship.** A file found holding exactly the bytes this team would write is left as it is
and its digest recorded as ``claimed``: replacing claimed bytes is ``--force``'s job, never a quiet rewrite,
and ``scan`` never adopts from them. Otherwise a second session's team of the same name, finding this team's
file identical to its own, could rewrite it with its operator's Rules. A claim
is this team's own only when it follows bytes this team wrote at that path, when an operator act released
those bytes, or on the first encounter after an upgrade positively recorded from older unmarked state.
Upgrade grace covers only knowledge and member documents, which the older plugin actually wrote.
``board.md`` and ``canvas.md`` are disposable views handled separately by ``workdir.write_view``.

``document-sync.json`` is keyed by absolute path, so a digest recorded in one folder is never evidence about
a file in another; ``follow_move`` re-keys the entries of a folder that was moved, file by file, only where
the moved file still holds the recorded bytes. One entry::

    {"digest": <sha256[:16] of the bytes this team last wrote, or found identical, here>,
     "claimed": <true when that digest was found, not written>,
     "source": <the stored rules/instructions those bytes were rendered from>, "findings": <knowledge.md only>,
     "pending", "since", "error": <scan's settle bookkeeping>,
     "settled": <digest of an edit scan examined and found nothing in to adopt>,
     "proposal": <context-bound id, file digest, complete canonical text and reviewed baseline>,
     "release": <digest of bytes an operator act allowed this team to replace, after a verified copy>,
     "hold": {"reason", "digest", "stamp", "kind", "why", "at"}, "copy": {"digest", "snapshot", "at"},
     "reported": <token of the last record a consumer showed>}

The document lock serializes rendering, CLI writers and imports, independently of the roster lock.
"""
from __future__ import annotations

import os
import json
import re
import shlex
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import charter, instructions_doc, paths as _paths, roster, store, workdir
from .errors import EXIT_REFUSED, HerdrTeamError
from .identity import Author

#: What ``scan`` is willing to parse and propose.
MAX_BYTES = 128 * 1024
#: The largest file the guard reads in order to copy it before an operator-ordered overwrite. Every record the
#: mirror writes is under it (``canvas.json`` is capped at 1 MiB, a ``facts.md`` part at 256 KiB), so a file
#: past it was not written by this team, and since it cannot be copied it is never replaced.
MAX_COPY_BYTES = 1024 * 1024
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


#: The reserved key of ``document-sync.json`` that this plugin's never-overwrite code always writes (``load`` adds it
#: and every writer saves what ``load`` returned). Every other key is an absolute path.
SCHEMA_KEY = workdir.STATE_MARK_KEY


def _older_mirror(paths: Any) -> bool:
    """Did an older plugin write this team's ``mirror.json``, or has an upgrade already been recorded there?"""
    mark = workdir.mirror_mark(paths)
    return mark is not None and ("schema" not in mark or bool(mark.get("upgraded")))


def load(paths: Any) -> Dict[str, Any]:
    """``document-sync.json``, always carrying its schema mark (``SCHEMA_KEY``).

    The mark records whether this team's folder was written by an older plugin -- 0.22.1 and before, which kept no
    record of the files it wrote -- and it is decided from positive evidence only: a ``document-sync.json`` or a
    ``mirror.json`` that holds records but no mark of this code's. A missing file is no evidence of anything (a first
    state write that failed looks exactly like that), so it never makes a team an upgrade.
    """
    try:
        value = store.read_json(state_path(paths), default=None)
    except (HerdrTeamError, OSError, ValueError):
        value = None
    raw = value if isinstance(value, dict) else {}
    state = dict(raw)
    mark = state.get(SCHEMA_KEY)
    if not (isinstance(mark, dict) and mark.get("schema")):
        older = any(key != SCHEMA_KEY for key in raw) or _older_mirror(paths)
        state[SCHEMA_KEY] = {"schema": workdir.STATE_SCHEMA, "upgraded": bool(older)}
    return state


def upgraded(state: Dict[str, Any]) -> bool:
    mark = state.get(SCHEMA_KEY)
    return isinstance(mark, dict) and bool(mark.get("upgraded"))


def entries(state: Dict[str, Any]):
    """The per-path entries of a loaded state, without its schema mark."""
    return [(key, entry) for key, entry in state.items() if key != SCHEMA_KEY and isinstance(entry, dict)]


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


def digest(data: bytes) -> str:
    return workdir.digest_bytes(data)


def quote(path: Any) -> str:
    """A path as a shell word, so a printed command runs as printed in a checkout path with a space or an apostrophe."""
    return shlex.quote(os.fspath(path))


# --------------------------------------------------------------------------
# one look at one file


class Look(object):
    """What is at one path: its bytes, nothing, or why its bytes cannot be read.

    One look per decision, and the write proves the file still holds what was looked at (``workdir.still_holds``)
    immediately before replacing anything. Changes seen by that check are refused, but the final check cannot
    make ``os.replace`` conditional. Nothing that is not a regular file is ever opened: opening a FIFO blocks.
    """

    __slots__ = ("data", "missing", "why", "too_large")

    def __init__(self, data: Optional[bytes] = None, missing: bool = False, why: str = "",
                 too_large: bool = False) -> None:
        self.data, self.missing, self.why, self.too_large = data, missing, why, too_large


TOO_LARGE_TO_COPY = "it is over 1 MiB, too large to copy"


def _too_large(limit: int) -> str:
    if limit <= MAX_COPY_BYTES:
        return TOO_LARGE_TO_COPY
    return "it is over {} MiB, larger than this team ever writes there".format(limit // (1024 * 1024))


def look(path: Path, limit: int = MAX_COPY_BYTES) -> Look:
    """One look at ``path``, reading at most ``limit`` bytes (``board.md`` may be larger than the copy budget)."""
    path = Path(path)
    try:
        if not os.path.lexists(os.fspath(path)):
            return Look(missing=True)
        if path.is_symlink():
            return Look(why="it is a symlink, which is never followed")
        if not path.is_file():
            return Look(why="it is not a regular file")
        if path.stat().st_size > limit:
            return Look(why=_too_large(limit), too_large=True)
        with path.open("rb") as handle:
            data = handle.read(limit + 1)
    except OSError as err:
        return Look(why="it cannot be read ({})".format(err.strerror or err))
    if len(data) > limit:
        return Look(why=_too_large(limit), too_large=True)
    return Look(data=data)


def _stamp(path: Path) -> str:
    """Size and mtime of what is at ``path``: tells one digest-less hold (too large, unreadable) from the next."""
    try:
        st = os.lstat(os.fspath(path))
    except OSError:
        return ""
    return "{}-{}".format(st.st_size, st.st_mtime_ns)


def _holds(path: Any, data: bytes) -> bool:
    """Does the regular file at ``path`` hold exactly ``data``? Never follows a symlink."""
    seen = look(Path(path), limit=max(len(data), 1))
    return seen.data is not None and seen.data == data


def _create_exclusive(target: Path, raw: bytes) -> bool:
    """Create ``target`` holding ``raw`` only if nothing is there. True when created, False when the name was taken.

    The absence check and the write are one operation: the bytes go to a temporary sibling and ``os.link`` then
    publishes them under the real name, which the filesystem refuses with ``EEXIST`` if anything is there by then.
    A filesystem with no hard links falls back to a last-moment absence check, which narrows the window rather than
    closing it, and is the best that filesystem allows.
    """
    target = Path(target)
    temporary = target.with_name(".tmp-new-{}-{}".format(os.getpid(), target.name))
    store.atomic_write(temporary, raw, fsync=False)
    try:
        try:
            os.link(os.fspath(temporary), os.fspath(target))
            return True
        except FileExistsError:
            return False
        except OSError:
            if os.path.lexists(os.fspath(target)):
                return False
            os.replace(os.fspath(temporary), os.fspath(target))
            return True
    finally:
        try:
            os.unlink(os.fspath(temporary))
        except OSError:
            pass


# --------------------------------------------------------------------------
# operator acts: releasing bytes to be replaced


def release(paths: Any, path: Any, data: Any) -> bool:
    """An operator act (an adoption, an import from this folder, a discard) allows these exact bytes to be replaced.

    ``data`` is the bytes the act read, or their digest. Only those bytes: if the file has changed by the time the
    next render looks, the release does not match and the file is held like any other. The render that acts on it
    copies the file into ``inherited/`` and verifies the copy first. Takes the document lock.
    """
    found = data if isinstance(data, str) else digest(data if isinstance(data, bytes) else str(data).encode("utf-8"))
    try:
        with document_lock(paths):
            state = load(paths)
            entry = state.setdefault(os.fspath(path), {})
            if not isinstance(entry, dict):
                entry = state[os.fspath(path)] = {}
            entry["release"] = found
            entry.pop("pending", None)
            entry.pop("error", None)
            store.write_json(state_path(paths), state)
    except (HerdrTeamError, OSError, ValueError):
        return False
    return True


def release_import(paths: Any, targets: Dict[str, Path], source_path: Any, full_digest: Any) -> bool:
    """``canvas import`` from this team's own ``canvas.json``: release exactly the bytes the import read."""
    if not source_path or not full_digest:
        return False
    candidate = Path(os.path.expanduser(str(source_path)))
    if candidate.is_dir():
        candidate = candidate / "canvas.json"
    try:
        same = os.path.realpath(os.fspath(candidate)) == os.path.realpath(os.fspath(targets["canvas_json"]))
    except (KeyError, OSError, ValueError):
        return False
    return same and release(paths, targets["canvas_json"], str(full_digest)[:16])


def follow_move(paths: Any, old_root: Any, new_root: Any) -> int:
    """``project set`` to a folder the team's old one was moved to: re-key what this team recorded, file by file.

    Entries are keyed by absolute path, so after ``mv`` every file this team wrote would be "a file this team has no
    record of writing" at its new path, and a picture this team mirrored could never be pruned. Only when the old
    folder is gone (it was moved, not copied), and only for a file whose bytes at the new path are exactly the
    recorded ones -- or for a ``deleted`` hold whose file is absent at the new path too, so that a document the
    operator deleted in auto mode is not recreated because its folder changed name. Every other entry of the old
    folder describes a path that no longer exists and is dropped. The number of entries carried over is returned.
    Takes the document lock.
    """
    old = os.fspath(old_root).rstrip(os.sep) + os.sep
    if not old_root or os.path.lexists(os.fspath(old_root)) or not os.path.isdir(os.fspath(new_root)):
        return 0
    moved = 0
    try:
        with document_lock(paths):
            state = load(paths)
            for key in [k for k in state if k.startswith(old)]:
                entry = state.pop(key)
                if not isinstance(entry, dict) or not entry.get("digest"):
                    continue
                target = Path(new_root) / key[len(old):]
                seen = look(target)
                hold = entry.get("hold") if isinstance(entry.get("hold"), dict) else {}
                if hold.get("reason") == "deleted" and seen.missing:
                    # Still deleted, still this team's to recreate only on the operator's word.
                    for field in ("pending", "since", "error", "release", "settled", "reported", "proposal"):
                        entry.pop(field, None)
                    state[os.fspath(target)] = entry
                    moved += 1
                    continue
                if seen.data is None or digest(seen.data) != entry.get("digest"):
                    continue
                for field in ("hold", "pending", "since", "error", "release", "settled", "reported", "proposal"):
                    entry.pop(field, None)
                copy = entry.get("copy")
                if isinstance(copy, dict) and str(copy.get("snapshot") or "").startswith(old):
                    copy["snapshot"] = os.fspath(Path(new_root) / str(copy["snapshot"])[len(old):])
                state[os.fspath(target)] = entry
                moved += 1
            store.write_json(state_path(paths), state)
    except (HerdrTeamError, OSError, ValueError):
        return 0
    return moved


# --------------------------------------------------------------------------
# the guard


#: Why a file is held. Each is a statement about the disk that is true when it is made.
HOLD_REASONS = {
    "found": "this team has no record of writing what is there now",
    "changed": "it changed after this team last wrote it",
    "deleted": ("it was deleted after this team last wrote it, and automatic document sync never recreates a "
                "deleted document by itself"),
    "unreadable": "",          # the look's own words
    "too_large": "",           # the look's own words
    "copy_failed": "it was to be replaced, but its copy into inherited/ could not be made and checked",
    "moved": "it changed on disk while this render was writing it",
    "unnamed": "no mark on this team's board names it, and this team has no record of writing it",
    "conflict": "its bytes do not match its name, so this team's picture of that name was not written",
    "symlinked": "it is reached through a symlinked directory, which is never followed",
    "proposed": ("it holds an edit of a document this team wrote, waiting for the operator to confirm it: automatic "
                 "document sync adopts nothing by itself, and it cannot tell who made the edit"),
    "replaced": "another team instance wrote its own version over the one this team last wrote there",
    "unwritable": "",          # the error's own words
}

#: The kinds of record a plugin older than the never-overwrite rule wrote with no record of having written them, and
#: which the first encounter after an upgrade may therefore take as this team's own (``FolderGuard._upgrade_own``).
UPGRADE_KINDS = ("knowledge", "member")
#: The documents document sync reads, whose generated marker names the team instance that rendered them.
SYNCED_KINDS = ("knowledge", "member")


class FolderGuard(object):
    """The mirror's durable-record writer and remover. See the module docstring.

    Durable records go through ``write`` (or ``create`` for a canvas asset, ``remove`` for the prune).
    Disposable ``board.md`` and ``canvas.md`` go through ``workdir.write_view``; the shared folder's ``README.md``
    and ``.gitignore`` use ``workdir.write_disposable``. ``workdir``'s raw writers refuse a path inside a
    ``.herdr-synapse`` folder that no caller classified, so these are the only doors.

    ``force`` is ``project render --force``: the operator ordering replaceable held durable paths replaced,
    each one through ``_replace_foreign``'s verified copy. The caller holds ``document_lock``.
    """

    def __init__(self, layout: Any, team_name: str, targets: Dict[str, Path], force: bool = False) -> None:
        self.layout = layout
        self.team_name = team_name
        self.targets = targets
        self.force = force
        self.team_paths = layout.team(team_name)
        self.state = load(self.team_paths)
        # A state file without the schema mark is saved by the first guard that meets it, so the upgrade decision it
        # carries is recorded while its evidence (an older plugin's unmarked state files) is still on disk.
        self._before = repr(self.state) if self._marked_on_disk() else None
        # A render can follow a mode, project, roster or source change without a scan. Do not advertise consent
        # that confirm would now reject; keep the foreign bytes held with ordinary explicit repair commands.
        for key, entry in entries(self.state):
            proposal = entry.get("proposal")
            if not isinstance(proposal, dict):
                continue
            try:
                context = _proposal_context(layout, team_name, proposal.get("member"), Path(key))
                current = look(Path(key), limit=MAX_BYTES)
                valid = (current.data is not None and digest(current.data) == proposal.get("digest")
                         and context == proposal.get("context")
                         and _proposal_id(proposal["digest"], context) == proposal.get("id"))
            except (HerdrTeamError, OSError, ValueError):
                valid = False
            if not valid:
                for field in ("proposal", "pending", "since", "error", "settled"):
                    entry.pop(field, None)
                if (entry.get("hold") or {}).get("reason") == "proposed":
                    entry["hold"].update(reason="changed", why=HOLD_REASONS["changed"])
        #: The folder was written by a plugin that kept no record of what it wrote (``load``'s schema mark): a file at
        #: a path this team has no entry for, holding exactly this team's version, is taken as this team's own on its
        #: first encounter (see "A claim is not authorship"). Decided once, from positive evidence, for every guard.
        self.upgrade = upgraded(self.state)
        #: This team instance, as the markers of its synced documents name it (``workdir.marker_for``).
        self.instance = workdir.instance_id(self.team_paths)
        #: Every record this guard met, by path: the order reports are made in.
        self.met: List[str] = []
        #: Paths whose hold is not a board report: an edit document sync is about to propose, or one the daemon
        #: announces as waiting for ``instructions --adopt``. The caller removes a path it cannot announce.
        self.quiet: set = set()
        self.errors: List[str] = []
        self.now = store.now_iso()

    def _marked_on_disk(self) -> bool:
        try:
            value = store.read_json(state_path(self.team_paths), default=None)
        except (HerdrTeamError, OSError, ValueError):
            return False
        return isinstance(value, dict) and isinstance(value.get(SCHEMA_KEY), dict)

    # -- bookkeeping -------------------------------------------------------

    def entry(self, path: Path) -> Dict[str, Any]:
        key = os.fspath(path)
        entry = self.state.get(key)
        if not isinstance(entry, dict):
            entry = self.state[key] = {}
        return entry

    def _meet(self, path: Path) -> None:
        key = os.fspath(path)
        if key not in self.met:
            self.met.append(key)

    def _wrote(self, path: Path, data: bytes, kind: str) -> None:
        """Record that this guard has just put ``data`` at ``path``: they are this team's bytes now.

        The copy record and the report token go with the bytes they described, so a copy taken before an earlier
        replacement is not reported again as if this write had taken it (``_replace_foreign`` sets its own after).
        """
        entry = self.entry(path)
        self._record(path, entry, data, kind)
        for field in ("claimed", "copy", "reported"):
            entry.pop(field, None)

    def _record(self, path: Path, entry: Dict[str, Any], data: bytes, kind: str) -> None:
        found = digest(data)
        if entry.get("digest") != found:
            entry["digest"] = found
            entry.update(self._trust_baseline(path, data, kind))
        # The file holds this team's bytes again, so no edit is waiting in it any more.
        for field in ("hold", "release", "settled", "proposal"):
            entry.pop(field, None)

    def _found_identical(self, path: Path, data: bytes, kind: str, upgrade_own: bool = False) -> None:
        """The file already holds exactly this team's version. Its digest is recorded; whether as this team's own
        bytes or only ``claimed`` is the module docstring's "A claim is not authorship"."""
        entry = self.entry(path)
        found = digest(data)
        # Reading or releasing identical bytes is not writing them. Promoting a claim here lets a later rename
        # destroy a foreign document without a checked copy, even though this render did nothing to its bytes.
        own = entry.get("digest") == found and not entry.get("claimed")
        self._record(path, entry, data, kind)
        if own or upgrade_own:
            entry.pop("claimed", None)
        else:
            entry["claimed"] = True

    def _upgrade_own(self, path: Path, entry: Dict[str, Any], kind: str, data: bytes,
                     expected: bytes, legacy: Optional[bytes]) -> bool:
        """Upgrade grace, per path: are these bytes this team's own, written by the older plugin it upgraded from?

        Only on a team ``load`` marked as upgraded, only at a path this team has no entry for at all (its first
        encounter since the upgrade), only for a record kind the older plugin wrote, and only for bytes that are
        exactly this team's version -- as this plugin writes it, or as the older one did (``legacy``: the marker
        without an instance id) -- or, for a member document, exactly the digest the older plugin's ``mirror.json``
        recorded writing there. Anything else is held like any other file.
        """
        if not self.upgrade or kind not in UPGRADE_KINDS or any(k in entry for k in ("digest", "hold", "claimed")):
            return False
        if data == expected or (legacy is not None and data == legacy):
            return True
        if kind == "member":
            recorded = workdir.mirror_state(self.team_paths).get(Path(path).name)
            return bool(recorded) and recorded == digest(data)
        return False

    def _trust_baseline(self, path: Path, data: bytes, kind: str) -> Dict[str, Any]:
        """What ``scan`` needs to tell an edit of this document from the document this team wrote."""
        if kind not in ("knowledge", "member"):
            return {}
        name = None if kind == "knowledge" else Path(path).stem
        try:
            text = data.decode("utf-8")
            out: Dict[str, Any] = {"source": source(self.layout, self.team_name, name).strip()}
            if name is None:
                out["findings"] = knowledge_parts(text)[1]
            return out
        except (HerdrTeamError, OSError, ValueError):
            return {}

    def hold(self, path: Path, kind: str, reason: str, found: Optional[str] = None, why: str = "",
             quiet: bool = False) -> str:
        """Record a hold. A hold with no digest carries the file's size and mtime instead, so that a *different*
        file held later for the same reason at the same path is a new record, reported again."""
        entry = self.entry(path)
        held = {"reason": reason, "digest": found, "kind": kind, "why": why or HOLD_REASONS.get(reason, ""),
                "at": self.now}
        if not found:
            held["stamp"] = _stamp(path)
        entry["hold"] = held
        self._meet(path)
        if quiet:
            self.quiet.add(os.fspath(path))
        return "held"

    def clear_hold(self, path: Path) -> None:
        """Drop a hold whose reason no longer applies (a picture a mark now names, with the bytes its name says)."""
        entry = self.state.get(os.fspath(path))
        if isinstance(entry, dict) and entry.pop("hold", None) is not None and not entry:
            self.state.pop(os.fspath(path), None)

    def forget_gone(self, directory: Path) -> None:
        """Drop the holds on files in ``directory`` that are no longer there: nothing remains to say about them."""
        prefix = os.fspath(directory) + os.sep
        for key in list(self.state):
            entry = self.state.get(key)
            if not key.startswith(prefix) or not isinstance(entry, dict) or "hold" not in entry:
                continue
            if not os.path.lexists(key):
                entry.pop("hold", None)
                if not entry.get("digest"):
                    self.state.pop(key, None)

    def through_symlink(self, path: Path) -> bool:
        """Is any directory between the team folder and ``path`` a symlink, or not a directory at all?

        A name says nothing about where the bytes are: ``ln -s ../../src canvas-assets`` would make a prune list and
        delete a repository's source. So nothing is written, listed or removed through a symlinked directory.
        """
        root = self.targets.get("root")
        if root is None:
            return True
        try:
            relative = Path(path).parent.relative_to(Path(root))
        except ValueError:
            return True
        here = Path(root)
        for part in relative.parts:
            here = here / part
            if here.is_symlink() or (os.path.lexists(os.fspath(here)) and not here.is_dir()):
                return True
        return False

    # -- the writers -------------------------------------------------------

    def write(self, path: Path, expected: bytes, kind: Optional[str] = None, quiet: bool = False,
              keep_missing: bool = False, limit: int = MAX_COPY_BYTES, legacy: Optional[bytes] = None) -> str:
        """Write ``expected`` at ``path`` under the one rule. ``written``, ``unchanged`` or ``held``.

        ``quiet`` makes a ``changed`` hold no board report: the caller announces that edit its own way (document
        sync proposing it, or the notifier's ``--adopt`` line), and un-quiets it when it cannot. ``keep_missing`` is
        document sync's rule that deleting a synced document never regenerates it by itself (a deletion may be an
        editor's save half done); ``--force`` is the operator saying to write it. ``limit`` is the most this
        decision reads of the file. ``legacy`` is this team's version as the plugin it may have upgraded from wrote
        it (``_upgrade_own``).
        """
        record = workdir.record_for(path, self.targets)
        kind = kind or record.kind
        # Met whatever the outcome, so a copy record a silent caller left unreported reaches the next consumer.
        self._meet(path)
        if self.through_symlink(path):
            return self.hold(path, kind, "symlinked")
        entry = self.entry(path)
        own = bool(entry.get("digest")) and not entry.get("claimed")
        seen = look(path, limit=max(limit, MAX_COPY_BYTES))
        if seen.missing:
            if keep_missing and own and not self.force:
                return self.hold(path, kind, "deleted")
            _paths.ensure_dir(path.parent)
            if not _create_exclusive(path, expected):
                return self.hold(path, kind, "moved")
            self._wrote(path, expected, kind)
            return "written"
        if seen.data is None:
            return self.hold(path, kind, "too_large" if seen.too_large else "unreadable", why=seen.why)
        found = digest(seen.data)
        upgrade_own = self._upgrade_own(path, entry, kind, seen.data, expected, legacy)
        if seen.data == expected:
            if found == entry.get("release") and not (own and found == entry.get("digest")):
                # Explicit adoption/confirmation may leave already-canonical bytes. Establish authorship by a
                # real checked-copy replacement, not by relabelling a read as a write; ordinary claims stay claims.
                return self._replace_foreign(path, seen.data, expected, kind)
            self._found_identical(path, expected, kind, upgrade_own=upgrade_own)
            return "unchanged"
        if (found == entry.get("digest") and own) or upgrade_own:
            return self._replace(path, seen.data, expected, kind)
        if self.force or found == entry.get("release"):
            return self._replace_foreign(path, seen.data, expected, kind)
        if own:
            other = workdir.marker_instance(seen.data) if kind in SYNCED_KINDS else None
            if other and other != self.instance:
                return self.hold(path, kind, "replaced", found, why="{} (its marker names team instance {}, this "
                                 "team is {}); it is not an edit of this team's document, so nothing in it is "
                                 "proposed".format(HOLD_REASONS["replaced"], other, self.instance))
            proposal = entry.get("proposal")
            if isinstance(proposal, dict) and proposal.get("digest") == found:
                return self.hold(path, kind, "proposed", found)
            return self.hold(path, kind, "changed", found, quiet=quiet)
        return self.hold(path, kind, "found", found)

    def write_text(self, path: Path, body: str, max_bytes: int = workdir.MAX_RENDER_BYTES,
                   kind: Optional[str] = None, quiet: bool = False, keep_missing: bool = False) -> str:
        """``write`` of a generated Markdown record; the synced documents carry this instance's id in their marker."""
        record_kind = kind or workdir.record_for(path, self.targets).kind
        instance = self.instance if record_kind in SYNCED_KINDS else None
        expected = workdir.generated_text(path, body, max_bytes, instance=instance).encode("utf-8")
        legacy = workdir.generated_text(path, body, max_bytes).encode("utf-8") if instance else None
        return self.write(path, expected, kind=kind, quiet=quiet, keep_missing=keep_missing, limit=max_bytes,
                          legacy=legacy)

    def write_json(self, path: Path, payload: Dict[str, Any], max_bytes: int = workdir.MAX_CANVAS_JSON_BYTES,
                   kind: Optional[str] = None) -> str:
        """The payload's cap is checked before anything: a payload this render would not write decides nothing."""
        text = workdir.generated_json_text(path, payload)
        size = len(text.encode("utf-8"))
        if size > max_bytes:
            raise workdir.PayloadTooLargeError(path, size, max_bytes)
        return self.write(path, text.encode("utf-8"), kind=kind)

    def _unwritable(self, path: Path, kind: str, found: str, err: OSError) -> str:
        why = "it could not be replaced: {} ({}); make {} writable, and the next render writes it".format(
            err.strerror or type(err).__name__, os.fspath(path), os.fspath(Path(path).parent))
        return self.hold(path, kind, "unwritable", found, why=why)

    def _replace(self, path: Path, observed: bytes, expected: bytes, kind: str) -> str:
        """Replace bytes this team wrote, proving immediately before that they are still the bytes looked at."""
        if not workdir.still_holds(path, observed):
            return self.hold(path, kind, "moved")
        try:
            store.atomic_write(path, expected, fsync=False)
        except OSError as err:
            return self._unwritable(path, kind, digest(observed), err)
        self._wrote(path, expected, kind)
        return "written"

    def _replace_foreign(self, path: Path, observed: bytes, expected: bytes, kind: str) -> str:
        """The operator-ordered overwrite: a verified copy first, or nothing at all.

        Two windows remain, and both are as narrow as a replace that cannot be conditional allows. Between the last
        ``still_holds`` and ``os.replace`` a writer can change the file, and its change is replaced (the copy holds the
        bytes looked at, not the writer's); between the copy's read-back and the replace a writer can change the copy,
        so the copy is read back once more after the replace and, if it no longer holds the replaced bytes, they are
        written again, from memory, under a second exclusive name before anything is reported.
        """
        found = digest(observed)
        if len(observed) > MAX_COPY_BYTES:
            return self.hold(path, kind, "too_large", why=TOO_LARGE_TO_COPY)
        snapshot, why, created = self._copy(path, observed)
        if snapshot is None:
            return self.hold(path, kind, "copy_failed", found, why="{}: {}".format(HOLD_REASONS["copy_failed"], why))
        if not workdir.still_holds(path, observed):
            # The copy is of bytes that did stand here a moment ago, so it is kept; the file is not touched.
            return self.hold(path, kind, "moved", found)
        try:
            store.atomic_write(path, expected, fsync=False)
        except OSError as err:
            if created:
                # The file is untouched, so the copy this call made of it is the bytes still in place: not a record.
                try:
                    os.unlink(os.fspath(snapshot))
                except OSError:
                    pass
            return self._unwritable(path, kind, found, err)
        if not _holds(snapshot, observed):
            snapshot = self._copy_again(path, observed) or snapshot
        self._wrote(path, expected, kind)
        self.entry(path)["copy"] = {"digest": found, "snapshot": os.fspath(snapshot), "kind": kind, "at": self.now}
        self._meet(path)
        return "written"

    def _copy(self, path: Path, data: bytes) -> Tuple[Optional[Path], str, bool]:
        """``copy_verified``, and whether this call created the copy (False when an identical one was already there)."""
        try:
            target = self.copy_target(path, data)
            existed = os.path.lexists(os.fspath(target))
        except (KeyError, ValueError):
            existed = False
        snapshot, why = self.copy_verified(path, data)
        return snapshot, why, snapshot is not None and not existed

    def _copy_again(self, path: Path, data: bytes) -> Optional[Path]:
        """The replaced bytes, from memory, under the first free ``-2``, ``-3``... name beside the copy; read back."""
        try:
            first = self.copy_target(path, data)
        except (KeyError, ValueError):
            return None
        for number in range(2, 10):
            target = first.with_name("{}-{}{}".format(first.stem, number, first.suffix))
            try:
                if not os.path.lexists(os.fspath(target)):
                    _create_exclusive(target, data)
            except (HerdrTeamError, OSError):
                return None
            if _holds(target, data):
                return target
        return None

    def copy_target(self, path: Path, data: bytes) -> Path:
        """``inherited/<dir>/<stem>-<digest16><suffix>``: a name that is a function of the bytes."""
        relative = Path(path).relative_to(self.targets["root"])
        name = "{}-{}{}".format(relative.stem, digest(data), relative.suffix)
        return Path(self.targets["inherited"]) / relative.parent / name

    def copy_verified(self, path: Path, data: bytes) -> Tuple[Optional[Path], str]:
        """Copy ``data`` (the bytes read from ``path``) under ``inherited/`` and read it back. ``(copy, "")`` or ``(None, why)``.

        A copy is trusted only after its bytes have been read back and compared, never because a path exists: an
        existing file at the copy's name counts only if it holds exactly these bytes.
        """
        try:
            target = self.copy_target(path, data)
        except (KeyError, ValueError):
            return None, "{} is not inside the team folder".format(path)
        root = Path(self.targets["root"])
        here = root
        for part in target.parent.relative_to(root).parts:
            here = here / part
            if here.is_symlink() or (os.path.lexists(os.fspath(here)) and not here.is_dir()):
                return None, "{} is not a directory; remove or rename it".format(here)
        try:
            _paths.ensure_dir(target.parent)
            if not os.path.lexists(os.fspath(target)):
                _create_exclusive(target, data)
            if target.is_symlink() or not target.is_file():
                return None, "{} is not a regular file".format(target)
            with target.open("rb") as handle:
                copied = handle.read(len(data) + 1)
        except (HerdrTeamError, OSError) as err:
            return None, "{} could not be written ({})".format(target, getattr(err, "strerror", None) or err)
        if copied != data:
            return None, "a different file already has the copy's name, {}".format(target)
        return target, ""

    def create(self, path: Path, data: bytes) -> bool:
        """One canvas asset, written only where nothing is. True when this team wrote it.

        An asset already there is never claimed, even when its bytes are the ones this team would write: a
        previous team's board may still name that file, so it is only ever deleted if this team created it.
        """
        workdir.record_for(path, self.targets)
        if self.through_symlink(path):
            return False
        _paths.ensure_dir(path.parent)
        _paths.check_not_symlink(path)
        if os.path.lexists(os.fspath(path)) or not _create_exclusive(path, data):
            return False
        entry = self.entry(path)
        entry["digest"] = digest(data)
        entry.pop("hold", None)
        return True

    def remove(self, path: Path, kind: str = "canvas-asset") -> str:
        """Delete one asset no mark names: ``removed`` when this team wrote these bytes, else ``held``."""
        workdir.record_for(path, self.targets)
        if self.through_symlink(path):
            return self.hold(path, kind, "symlinked")
        seen = look(path)
        if seen.missing:
            self.state.pop(os.fspath(path), None)
            return "gone"
        if seen.data is None:
            return self.hold(path, kind, "too_large" if seen.too_large else "unreadable", why=seen.why)
        found = digest(seen.data)
        if found != self.entry(path).get("digest"):
            return self.hold(path, kind, "unnamed", found)
        if not workdir.still_holds(path, seen.data):
            return self.hold(path, kind, "moved", found)
        try:
            path.unlink()
        except OSError as err:
            self.errors.append("{}: {}".format(path, err))
            return "held"
        self.state.pop(os.fspath(path), None)
        return "removed"

    # -- what the caller owes the operator ---------------------------------

    def records(self) -> List[Dict[str, Any]]:
        """Every hold and every verified copy this guard met, true of the disk now."""
        active = active_members(self.team_paths)
        out = []
        for key in self.met:
            entry = self.state.get(key)
            if not isinstance(entry, dict):
                continue
            made = record_of(Path(key), entry, active, self.team_name)
            if made is not None:
                made["quiet"] = key in self.quiet
                out.append(made)
        return out

    def reports(self) -> List[Dict[str, Any]]:
        """The records a consumer has not shown yet: each hold and each copy is reported once."""
        return [r for r in self.records() if not r.get("reported") and not r.get("quiet")]

    def save(self) -> None:
        """Persist ``document-sync.json``, never raising, and report a failure.

        A failure loses the record of what this render wrote, and the next render reads whatever record was saved
        before it, or none. Either way it errs towards holding: a file whose recorded digest is older than its bytes
        is held (``changed``) and one with no record at all is only claimed if it holds this team's version, held
        otherwise -- never written over. A lost first save never makes a team an upgrade: that is decided from an older
        plugin's unmarked state files (``load``), and this team's ``mirror.json`` carries this code's mark.
        """
        if repr(self.state) == self._before:
            return
        try:
            store.write_json(state_path(self.team_paths), self.state)
            self._before = repr(self.state)
        except (HerdrTeamError, OSError) as err:
            self.errors.append("{}: {}".format(state_path(self.team_paths), err))


# --------------------------------------------------------------------------
# records: what is said about a hold or a copy


def active_members(paths: Any) -> Optional[set]:
    """The team's active agent members, or None when the roster cannot be read (``--adopt`` refuses any other name)."""
    try:
        doc = roster.load_team(paths)
    except (HerdrTeamError, OSError, ValueError, AttributeError, TypeError):
        return None
    return {m.name for m in doc.members if not m.is_human and m.status in ("active", "starting")}


def _adoptable_now(path: Path) -> bool:
    """Would ``instructions <name> --adopt`` accept this member document as it stands right now?

    The same three refusals the command makes: no marker, not UTF-8, and instructions past the stored limit.
    """
    seen = look(Path(path))
    if seen.data is None:
        return False
    try:
        text = seen.data.decode("utf-8")
    except ValueError:
        return False
    if not text.startswith(workdir.MARKER_PREFIXES):
        return False
    sections = instructions_doc.parse(text)
    incoming = instructions_doc.to_text(sections) if not instructions_doc.is_empty(sections) else ""
    return len(incoming.strip()) <= charter.MAX_INSTRUCTIONS_CHARS


def _importable(path: Any) -> bool:
    """Does ``canvas import --from <path> --dry-run`` accept this file as a scene?"""
    try:
        from . import canvas_import as _canvas_import

        _canvas_import.read_document(os.fspath(path))
        return True
    except Exception:  # noqa: BLE001 - any refusal at all means the command would not run
        return False


def _token(entry: Dict[str, Any]) -> Optional[str]:
    hold = entry.get("hold")
    if isinstance(hold, dict):
        proposal = entry.get("proposal")
        if hold.get("reason") == "proposed" and isinstance(proposal, dict):
            return "proposal:{}".format(proposal.get("id") or "")
        return "hold:{}:{}".format(hold.get("reason"), hold.get("digest") or hold.get("stamp") or "")
    copy = entry.get("copy")
    if isinstance(copy, dict):
        return "copy:{}".format(copy.get("digest"))
    return None


def record_of(path: Path, entry: Dict[str, Any], active: Optional[set] = None,
              team: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """One record for one path's hold, or for the copy taken before its last operator-ordered overwrite.

    ``snapshot`` is named only after the copy has been read back and still holds the digest it was taken of; a copy
    edited or removed since is said to be gone rather than named. ``team`` goes into every printed command, so that
    in a session with several teams the command acts on the team that holds the file.
    """
    token = _token(entry)
    if token is None:
        return None
    hold = entry.get("hold") if isinstance(entry.get("hold"), dict) else None
    info = hold or entry.get("copy") or {}
    kind = str(info.get("kind") or "knowledge")
    member = Path(path).stem if kind == "member" else None
    record: Dict[str, Any] = {
        "path": os.fspath(path), "kind": kind, "member": member, "held": hold is not None,
        "reason": hold.get("reason") if hold else None, "why": (hold.get("why") if hold else "") or "",
        "snapshot": None, "regenerated": False, "copy_gone": None, "token": token, "team": team,
        "reported": entry.get("reported") == token,
    }
    proposal = entry.get("proposal")
    if hold is not None and hold.get("reason") == "proposed" and isinstance(proposal, dict):
        record["proposal"] = {key: proposal.get(key) for key in ("id", "digest", "kind", "member", "text", "was", "instance")}
    if hold is None:
        snapshot = Path(str(info.get("snapshot") or ""))
        seen = look(snapshot) if info.get("snapshot") else Look(missing=True)
        if seen.data is not None and digest(seen.data) == info.get("digest"):
            record["snapshot"] = os.fspath(snapshot)
        else:
            record["copy_gone"] = os.fspath(snapshot)
        record["regenerated"] = True
    record["commands"] = _commands(record, active)
    record["command"] = record["commands"][0] if record["commands"] else None
    return record


def team_flag(team: Optional[str]) -> str:
    """`` --team <name>`` for a printed command, or nothing when the caller has no team to name."""
    return " --team {}".format(quote(team)) if team else ""


def _commands(record: Dict[str, Any], active: Optional[set]) -> List[str]:
    """The ``herdr-synapse`` commands that resolve one record, each runnable exactly as printed: paths shell-quoted,
    and the team named, so the command acts on the team holding the file and not on a default or space team."""
    path = Path(record["path"])
    kind = record["kind"]
    reason = record.get("reason")
    team = team_flag(record.get("team"))
    force = "herdr-synapse project render --force" + team
    if not record["held"]:
        if kind == "canvas-payload" and record.get("snapshot") and _importable(record["snapshot"]):
            return ["herdr-synapse canvas import --from {} --dry-run{}".format(quote(record["snapshot"]), team)]
        return []
    if reason == "deleted":
        return [force, "herdr-synapse project sync manual" + team]
    if reason == "proposed":
        proposal = record.get("proposal") or {}
        name = proposal.get("member")
        if proposal.get("id") and (not name or active is None or name in active):
            return ["herdr-synapse project confirm {}{}".format(quote(proposal["id"]), team), force]
        return [force]
    if reason == "unwritable":
        return []
    if reason in ("found", "changed", "replaced"):
        if kind == "member":
            name = record.get("member") or path.stem
            if (active is None or name in active) and _adoptable_now(path):
                return ["herdr-synapse instructions {} --adopt{}".format(quote(name), team), force]
            return [force]
        if kind == "canvas-payload":
            return (["herdr-synapse canvas import --from {} --dry-run{}".format(quote(path), team), force]
                    if _importable(path) else [force])
        if kind in ("knowledge", "facts"):
            return ["herdr-synapse knowledge import --from {} --dry-run{}".format(quote(path.parent), team), force]
        return [] if kind == "canvas-asset" else [force]
    if reason == "moved":
        return ["herdr-synapse project render" + team]
    if reason == "copy_failed":
        return [force]
    return []


def _aside(path: Any) -> str:
    """``workdir.aside_command``: a destination nothing has yet, both words shell-quoted."""
    return workdir.aside_command(path)


#: Holds no ``herdr-synapse`` command can resolve while the file is what it is: moving it aside is the repair.
_MOVE_ASIDE = ("unreadable", "too_large", "copy_failed", "symlinked")


def describe_inherited(record: Dict[str, Any], style: str = "board") -> str:
    """One sentence about one record: ``board`` ends it with a full stop, ``list`` (one line under a heading) does not.

    Says only what is true on disk: a held file "is left exactly as it is" because it is, a deleted one "is gone",
    and a copy is named only after it was read back and matched. A copy line says the bytes were "other than the
    ones this team last wrote there", which is all the guard knows: only the last digest is recorded, so an older
    version this team wrote that a checkout restored is not known to be this team's. Every command is shell-quoted,
    names the team, and runs as printed.
    """
    path = str(record.get("path") or "")
    commands = [_command_clause(c, record) for c in record.get("commands") or []]
    if record.get("held"):
        reason = str(record.get("reason") or "")
        if reason == "unnamed":
            state = ("is a picture or data file that no mark on this team's board names and that this team has no "
                     "record of writing, so it stays where it is")
        elif reason == "conflict":
            state = ("holds bytes that do not match its name, so this team's picture of that name was not written "
                     "and the file stays where it is")
        elif reason == "deleted":
            state = "is gone: {}".format(HOLD_REASONS["deleted"])
        elif reason == "proposed":
            proposal = record.get("proposal") or {}
            state = ("is left exactly as it is, and {} are not changed: the file holds an edit of them that is "
                     "waiting for the operator; automatic document sync adopts nothing by itself, and it cannot tell "
                     "who made an edit".format(_subject(proposal)))
        else:
            state = "is left exactly as it is: {}".format(str(record.get("why") or HOLD_REASONS.get(reason, "")).strip())
        if reason in _MOVE_ASIDE and os.path.lexists(path):
            commands.append("to let this team write it, move it aside yourself, for example  {}  and render "
                            "again".format(_aside(path)))
    elif record.get("snapshot"):
        state = ("held bytes other than the ones this team last wrote there; they were copied to {} (read back and "
                 "checked) and this team's version was written over the file".format(record["snapshot"]))
    else:
        state = ("held bytes other than the ones this team last wrote there; this team's version was written over the "
                 "file after they were copied to {}, but that copy no longer holds them (it was changed or removed "
                 "since)".format(record.get("copy_gone")))
    line = "{} {}{}".format(path, state, "".join("; " + clause for clause in commands)).rstrip()
    line = line if style == "list" else line + "."
    if record.get("held") and record.get("reason") == "proposed" and isinstance(record.get("proposal"), dict):
        line += "\n" + "\n".join(proposal_block(record["proposal"], "    " if style == "list" else ""))
    return line


def _subject(proposal: Dict[str, Any]) -> str:
    member = proposal.get("member")
    return "{}'s instructions".format(member) if member else "the team's Rules"


def proposal_block(proposal: Dict[str, Any], indent: str = "") -> List[str]:
    """What a pending change would do, as ``|`` lines: who wrote the file, then the change against what is stored.

    Shown wherever the change is (the board line, ``project``, ``project render``) so that the operator reads the
    exact text before the one command that adopts it, and the confirm prints the same text again as it adopts it.
    """
    instance = proposal.get("instance")
    origin = ("the file's marker names team instance {}, this team".format(instance) if instance
              else "the file's marker names no team instance")
    lines = ["{}| written by: {}; who edited it is not known (anyone who can write the checkout can, an agent "
             "included)".format(indent, origin)]
    was = str(proposal.get("was") or "")
    text = str(proposal.get("text") or "")
    lines.append("{}| change to {} (- stored when proposed, + proposed):".format(indent, _subject(proposal)))
    import difflib

    diff = list(difflib.unified_diff(was.splitlines(), text.splitlines(), lineterm="", n=2))[2:]
    if not diff:
        diff = ["+" + line for line in text.splitlines()] or ["(empty)"]
    lines.extend("{}| {}".format(indent, line) for line in diff)
    lines.append("{}| complete text to adopt:".format(indent))
    lines.extend("{}| {}".format(indent, line) for line in text.splitlines())
    if not text:
        lines.append("{}| (empty: cleared)".format(indent))
    return lines


_TEAM_SUFFIX = re.compile(r" --team \S+$")


def _command_clause(command: str, record: Optional[Dict[str, Any]] = None) -> str:
    """One clause naming one command, which ends at two spaces so that the command can be read back out of the line."""
    verb = _TEAM_SUFFIX.sub("", command)
    if verb.endswith("--dry-run"):
        if " knowledge import " in verb:
            return "see its Rules, and what an import would take of its facts and canvas, with  {}  ".format(command)
        return "see what it holds with  {}  ".format(command)
    if verb.endswith("--adopt"):
        return "review and adopt it with  {}  ".format(command)
    if " project confirm " in verb:
        return "adopt exactly the text it holds, shown below, with  {}  ".format(command)
    if verb.endswith("--force"):
        if record is not None and record.get("reason") == "deleted":
            return "write this team's version again with  {}  ".format(command)
        return "write this team's version, copying the file into inherited/ first, with  {}  ".format(command)
    if verb.endswith("project sync manual"):
        return ("or turn automatic document sync off, and the render that command runs writes this team's version, "
                "with  {}  ".format(command))
    return "look again with  {}  ".format(command)


def held_records(paths: Any, folder: Any) -> List[Dict[str, Any]]:
    """Every standing hold under ``folder``, re-checked against the disk: ``project``'s list.

    A hold describes the file as the last render saw it. A file that has changed since says so (``stale``) rather
    than being listed as "left exactly as it is" with a reason that no longer applies, and a held file that is no
    longer there at all is not listed: there is nothing left to resolve (``deleted`` is the one hold about a
    missing file, and it goes stale when the file is back).
    """
    out: List[Dict[str, Any]] = []
    root = os.path.realpath(os.fspath(folder)) + os.sep if folder else None
    state = load(paths)
    active = active_members(paths)
    team = getattr(paths, "name", None)
    for key, entry in sorted(state.items()):
        if not isinstance(entry, dict) or not isinstance(entry.get("hold"), dict):
            continue
        if root is not None and not os.path.realpath(key).startswith(root):
            continue
        reason = entry["hold"].get("reason")
        present = os.path.lexists(key)
        if not present and reason != "deleted":
            continue
        made = record_of(Path(key), entry, active, team)
        if made is None:
            continue
        held_digest = entry["hold"].get("digest")
        if reason == "deleted":
            made["stale"] = present
        elif held_digest:
            seen = look(Path(key))
            if seen.data is None or digest(seen.data) != held_digest:
                made["stale"] = True
        out.append(made)
    return out


def copies(paths: Any, folder: Any) -> List[Dict[str, Any]]:
    """Every copy this team took before an operator-ordered overwrite under ``folder``, each re-read and checked."""
    out: List[Dict[str, Any]] = []
    root = os.path.realpath(os.fspath(folder)) + os.sep if folder else None
    for key, entry in sorted(load(paths).items()):
        if not isinstance(entry, dict) or not isinstance(entry.get("copy"), dict):
            continue
        if root is not None and not os.path.realpath(key).startswith(root):
            continue
        copy = entry["copy"]
        seen = look(Path(str(copy.get("snapshot") or "")))
        out.append({"path": key, "kind": copy.get("kind"), "snapshot": copy.get("snapshot"), "at": copy.get("at"),
                    "intact": seen.data is not None and digest(seen.data) == copy.get("digest")})
    return out


def mark_reported(paths: Any, records: Any) -> None:
    """Record that these records reached the operator, so each is said once. Never raises.

    The consumer that displays or boards a record spends it; a silent caller leaves it for the next one. A state
    file that cannot be written means the record is shown again, which is the safe direction.
    """
    wanted = {str(r.get("path")): r.get("token") for r in (records or [])
              if isinstance(r, dict) and r.get("path") and r.get("token")}
    if not wanted:
        return
    try:
        with document_lock(paths):
            state = load(paths)
            changed = False
            for key, token in wanted.items():
                entry = state.get(key)
                if isinstance(entry, dict) and _token(entry) == token and entry.get("reported") != token:
                    entry["reported"] = token
                    changed = True
            if changed:
                store.write_json(state_path(paths), state)
    except (HerdrTeamError, OSError, ValueError):
        return


# --------------------------------------------------------------------------
# document sync: proposing a settled edit of a document this team wrote, never adopting it


def _incoming(name: Optional[str], text: str, team: str) -> str:
    """The Rules or instructions a synced document holds, refusing what ``scan`` must never read as a change.

    The findings test is for ``knowledge.md``: only the Rules section is the operator's to edit there, so a document
    whose Findings are not the ones this team generated is reported, never proposed.
    """
    if name is None:
        incoming, _findings = knowledge_parts(text)
        return charter.sanitize(incoming, charter.MAX_RULES_CHARS, code="rules_too_long")
    # ``instructions_doc.parse`` turns a document with no ``##`` heading into a Mission, which is how a tombstone or a
    # rename note would become operator authority. The human verb remains for it.
    if not any(line.lstrip().startswith(instructions_doc.HEADING) for line in text.splitlines()):
        raise ValueError("not an instructions document (no sections); adopt a pre-0.6 or generated placeholder "
                         "deliberately with  herdr-synapse instructions {} --adopt{}".format(quote(name), team_flag(team)))
    sections = instructions_doc.parse(text)
    if not sections:
        raise ValueError("missing instruction sections; nothing was proposed from it")
    incoming = instructions_doc.to_text(sections) if not instructions_doc.is_empty(sections) else ""
    cleaned = charter.sanitize(incoming, charter.MAX_INSTRUCTIONS_CHARS, code="instructions_too_long")
    sections = instructions_doc.parse(cleaned)
    return instructions_doc.to_text(sections).strip() if not instructions_doc.is_empty(sections) else ""


def _proposal_context(layout: Any, team: str, name: Optional[str], path: Path,
                      doc: Any = None) -> Dict[str, Any]:
    """Bind consent to the current destination and authority, independently of the edited file's bytes."""
    paths = layout.team(team)
    doc = roster.load_team(paths) if doc is None else doc
    project = workdir.project_dir_of(doc.to_json())
    member = doc.find(name) if name else None
    identity = None
    if name:
        if not member or member.name != name or member.is_human or member.status not in ("active", "starting"):
            raise HerdrTeamError("member_not_found", "{} is no longer an active member under that exact name, so "
                                 "the proposed instructions were not adopted".format(name), EXIT_REFUSED,
                                 {"name": name, "team": team})
        identity = {"name": name, "generation": member.generation, "joined_at": member.joined_at,
                    "terminal": member.terminal_id, "pane": member.pane_id}
    if not project or not enabled(doc):
        raise ValueError("the team needs its current project and automatic document sync for this confirmation")
    targets = workdir.paths_for(project, team)
    expected = targets["members"] / (name + ".md") if name else targets["knowledge"]
    if os.path.abspath(os.fspath(path)) != os.path.abspath(os.fspath(expected)):
        raise ValueError("the document is no longer in this team's configured project")
    here = expected.parent
    while here != targets["root"].parent:
        if here.is_symlink():
            raise ValueError("a document directory is symlinked; nothing is confirmed through it")
        here = here.parent
    return {"project": os.path.realpath(project), "path": os.path.realpath(os.fspath(expected)),
            "instance": workdir.instance_id(paths), "member": identity,
            "revision": charter.instructions_seq(member) if name else charter.rules_seq(doc),
            "source": source(layout, team, name).strip()}


def _proposal_id(found: str, context: Dict[str, Any]) -> str:
    return digest(json.dumps({"digest": found, "context": context}, sort_keys=True,
                             ensure_ascii=True).encode("utf-8"))


def scan(layout: Any, team: str, now: Optional[float] = None) -> Dict[str, Any]:
    """One bounded scan in auto mode: turn each settled edit of a document this team wrote into a pending change.

    Owner decision D-AUTO (2026-10-09): automatic document sync never adopts an edit by itself. The code cannot tell
    the operator's edit from an agent's -- the checkout is writable by the agents -- so an edit of ``knowledge.md``'s
    Rules or a member's instructions is recorded as a *proposal* (the exact text, what is stored now, and the team
    instance the file's marker names), the render holds the file with reason ``proposed`` and reports it once, and
    only ``confirm`` (``herdr-synapse project confirm <id>``, the operator's verified command) stores the text.
    Until then the stored Rules and instructions are unchanged.

    Only a document with a recorded ``digest`` that is not merely ``claimed`` -- bytes this team wrote -- is ever
    considered, so a file found in place is never proposed; nor is a complete render by another team instance (its
    marker names that instance), which the render holds as ``replaced``. An edit with nothing to adopt in it (a note
    outside the sections, whitespace) is *settled*: it stays held and is reported with the commands that resolve it.
    """
    now = time.time() if now is None else now
    paths = layout.team(team)
    result: Dict[str, Any] = {"imported": [], "proposed": [], "errors": []}
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
        instance = workdir.instance_id(paths)
        for name, path in files:
            entry = state.get(os.fspath(path))
            if not isinstance(entry, dict) or not entry.get("digest") or entry.get("claimed"):
                continue  # this team never wrote it: never proposed, only held
            try:
                text = read(path)
                found = workdir.digest(text)
                proposal = entry.get("proposal") if isinstance(entry.get("proposal"), dict) else {}
                other = workdir.marker_instance(text)
                if found in (entry.get("digest"), entry.get("release")) or (other and other != instance):
                    entry.pop("proposal", None)
                    entry.pop("pending", None)
                    entry.pop("error", None)
                    continue
                context = _proposal_context(layout, team, name, path)
                ident = _proposal_id(found, context)
                if proposal.get("digest") == found and proposal.get("id") == ident:
                    entry.pop("pending", None)
                    entry.pop("error", None)
                    continue
                if found == entry.get("settled") and context["source"] == entry.get("source"):
                    continue
                repropose = proposal.get("digest") == found
                if not repropose and entry.get("pending") != found:
                    entry.pop("proposal", None)
                    entry.update(pending=found, since=now)
                    continue
                if not repropose and now - entry.get("since", now) < SETTLE_SECONDS:
                    continue
                current = context["source"]
                if name is None and knowledge_parts(text)[1] != entry.get("findings"):
                    raise ValueError(
                        "its Findings section is not the one this team generated, so nothing in it was proposed "
                        "and the file is left as it is; add findings with knowledge add, and to let this team "
                        "write the file again run  herdr-synapse project render --force{}  (it copies the file "
                        "into inherited/ first)".format(team_flag(team)))
                incoming = _incoming(name, text, team)
                if read(path) != text:
                    continue
                latest = roster.load_team(paths)
                if _proposal_context(layout, team, name, path, latest) != context:
                    continue
                if current != incoming:
                    entry["proposal"] = {"id": ident, "digest": found, "context": context,
                                         "kind": "instructions" if name else "rules", "member": name,
                                         "text": incoming, "was": current, "instance": other,
                                         "at": store.now_iso()}
                    result["proposed"].append(os.fspath(path))
                else:
                    entry.pop("proposal", None)
                    entry.update(settled=found, source=incoming)
                entry.pop("pending", None)
                entry.pop("error", None)
            except Exception as err:  # noqa: BLE001 - isolate one bad file from other teams
                # A bad document must never stop delivery to other teams.
                message = str(err)
                if entry.get("error") != message:
                    result["errors"].append({"path": os.fspath(path), "error": message})
                    entry["error"] = message
        if repr(state) != before:
            store.write_json(state_path(paths), state)
    return result


def pending_changes(paths: Any) -> List[Dict[str, Any]]:
    """Saved proposals: path, consent id, file digest, complete text and the baseline reviewed when proposed."""
    out = []
    for key, entry in sorted(entries(load(paths))):
        proposal = entry.get("proposal")
        if isinstance(proposal, dict) and proposal.get("digest"):
            out.append(dict(proposal, path=key))
    return out


def confirm(layout: Any, team: str, author: Author, ident: str) -> Dict[str, Any]:
    """Adopt one pending change, exactly as proposed: the operator's command, with ``knowledge set``'s authority.

    ``ident`` binds to the file bytes and the reviewed destination and authority: if the bytes, project, source
    revision or member identity changed, nothing is adopted. The canonical text is re-read and must equal the full
    text shown. The proposal is consumed durably before storage, so a failed initial state save adopts nothing and
    a retry cannot apply the same proposal twice. A later bookkeeping failure is reported after verifying storage.
    Afterwards the file's bytes are
    released, so the render that follows takes a checked copy and writes this team's version over it. Even when
    the edited file already equals that version, a real replacement establishes authorship; a read cannot do so.
    """
    charter.require_human(layout, team, author, "project confirm")
    paths = layout.team(team)
    wanted = str(ident or "").strip().lower()
    with document_lock(paths):
        state = load(paths)
        matches = [(key, entry) for key, entry in entries(state) if isinstance(entry.get("proposal"), dict)
                   and wanted and str(entry["proposal"].get("id") or "") == wanted]
        if not matches:
            waiting = ", ".join(str(e["proposal"].get("id")) for _k, e in entries(state)
                                if isinstance(e.get("proposal"), dict)) or "none"
            raise HerdrTeamError("no_pending_change", "no pending change {!r} for team {} (waiting: {}); "
                                 "herdr-synapse project{} lists them".format(ident, team, waiting, team_flag(team)),
                                 EXIT_REFUSED, {"id": ident, "team": team})
        key, entry = matches[0]
        proposal = entry["proposal"]
        path = Path(key)
        seen = look(path, limit=MAX_BYTES)
        found = proposal["digest"]
        if seen.data is None or digest(seen.data) != found:
            raise HerdrTeamError("document_changed", "{} no longer holds the edit {} that was proposed, so nothing was "
                                 "adopted; herdr-synapse project{} shows what it holds now".format(
                                     path, wanted, team_flag(team)), EXIT_REFUSED, {"path": key})
        name = proposal.get("member")
        try:
            context = _proposal_context(layout, team, name, path)
            if context != proposal.get("context") or _proposal_id(found, context) != wanted:
                raise ValueError("the project, stored text/revision or member identity changed since this proposal")
            incoming = _incoming(name, seen.data.decode("utf-8"), team)
        except ValueError as err:
            raise HerdrTeamError("document_changed", "{}: {}".format(path, err), EXIT_REFUSED, {"path": key})
        if incoming != str(proposal.get("text") or ""):
            raise HerdrTeamError("document_changed", "{} reads differently from the change that was proposed; nothing "
                                 "was adopted".format(path), EXIT_REFUSED, {"path": key})
        current = context["source"]
        # Consume consent before touching authority. A crash now leaves the edited file held; the next settled scan
        # can propose it again, while a crash after storage finds the text already stored and cannot bump it twice.
        for field in ("proposal", "pending", "since", "error", "settled"):
            entry.pop(field, None)
        try:
            store.write_json(state_path(paths), state)
        except (HerdrTeamError, OSError, ValueError) as err:
            raise HerdrTeamError("document_sync_failed", "could not record confirmation: {}; nothing was "
                                 "adopted".format(err), EXIT_REFUSED, {"path": key})
        warnings = []

        def verify_writer(doc: Any) -> None:
            # Roster mutations do not take document_lock. The writer calls this under team.lock and publishes
            # before releasing it, so member replacement or a project change cannot redirect old consent.
            try:
                latest = _proposal_context(layout, team, name, path, doc=doc)
                if latest != context:
                    raise ValueError("the proposal's authority context changed during confirmation")
            except (HerdrTeamError, ValueError) as err:
                raise HerdrTeamError("document_changed", str(err), EXIT_REFUSED, {"path": key})

        try:
            stored = (charter._store_instructions(layout, team, author, name, incoming, verify=verify_writer) if name
                      else charter._store_rules(layout, team, author, incoming, verify=verify_writer))
        except (HerdrTeamError, OSError, ValueError) as err:
            # The setter writes the text before its revision, board and audit work. A later error is partial
            # completion, not a truthful promise that nothing was saved.
            if (isinstance(err, HerdrTeamError) and err.code == "document_changed") or source(layout, team, name).strip() != incoming:
                raise
            stored = {"partial": True}
            warnings.append("the confirmed text was saved, but revision or notification work was incomplete: {}".format(err))
        entry.update(release=found, source=incoming)
        try:
            store.write_json(state_path(paths), state)
        except (HerdrTeamError, OSError, ValueError) as err:
            warnings.append("the confirmed text was saved, but mirror state could not be saved; the edited file may "
                            "remain held until you render with --force: {}".format(err))
    result = {"team": team, "path": key, "kind": proposal.get("kind"), "member": name, "text": incoming,
              "was": current, "proposed_from": proposal.get("was"), "instance": proposal.get("instance"),
              "id": wanted, "digest": found, "stored": stored}
    if warnings:
        result["warning"] = "; ".join(warnings)
    return result
