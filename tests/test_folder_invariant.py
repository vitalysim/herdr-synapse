"""The oracle for the team folder: a model-based property test that *is* the inheritance invariant.

STATUS ON THE TREE THIS WAS WRITTEN AGAINST (feat/canvas-v2, d13e58c5 plus the uncommitted
inheritance diff, 2026-10-08). ``FolderInvariantOracleTests`` and ``KnownFindingScriptTests``
FAIL there, on purpose and not as ``expectedFailure``: they are the baseline the fix stage turns
green, and an ``expectedFailure`` would turn the first fix into an "unexpected success" error, which
reads as a regression. The four HIGH findings of round 4 each fail them:

* DATA-1 / COMP-1 -- the ``inherited/`` bound treats ``canvas-assets/`` as one record, so a
  previous team's ninth and later pictures are destroyed and ``assets_preserved`` names them;
* COMP-2 -- ``FolderGuard.write_bytes`` overwrites with no copy (reached through the asset mirror's
  check-then-write window);
* DATA-2 -- a ``hold_kind == "adopt"`` record is never looked at again, so ``project render
  --force`` destroys the document that replaced it;
* TRUS-1 -- ``instructions <name> --adopt`` names a snapshot that holds other bytes than the ones it
  destroys.

The property test reports more than those four on that tree, and every one is real (each was
reproduced by hand before this note was written):

* known from round 4: DATA-3 (a write refused because the record moved still says "exactly as you
  found it"), COMP-4/DATA-6 (the canvas-asset line prints ``<that folder>/canvas.json``), and COMP-1's
  second half (a re-emitted record names a copy that is no longer on disk);
* new: ``instructions <name> --adopt`` drops the lines outside the ``##`` sections when its copy
  cannot be made; in ``auto`` mode the notifier's own adoption of a replaced member document drops
  them too, and the next render writes over them with no copy; the payload copy of a merge-conflicted
  or undecodable ``canvas.json`` is offered to ``canvas import --dry-run``, which exits 1; and
  ``instructions <name> --adopt`` raises ``UnicodeDecodeError`` on a document with one invalid byte.

``OracleSelfTests`` and every test in ``tests/test_trust_boundary_shapes.py`` pass on that tree.

SECOND VOCABULARY (the oracle extension, later the same day, against the fix stage's tree). The fix stage turned
the classes above green. ``ExtendedWorld`` adds the events nobody had generated -- two teams on one folder (in turn,
and one rendering in the middle of the other's write), the notifier's own tick, symlinks inside the folder, case
variants on a case-folding filesystem, renames into a stranger's name / back / to 32 characters, ``facts.md`` in
parts, imports over imports, ``project set`` elsewhere and back, a moved folder, ``dissolve`` in the same session,
a board naming pictures that never arrived and ``herdr-synapse project``'s own text -- and two checks
(OUTSIDE_DESTROYED, PAYLOAD_ASSET_MISSING). ``FolderInvariantExtendedOracleTests`` and
``ExtensionFindingScriptTests`` FAIL on that tree, on purpose, for the same reason as above: a symlinked
``canvas-assets/`` makes the prune delete the files it points at (outside the team folder); the notifier's
foreign-member line prints ``instructions <name> --adopt`` for a name the team does not use; the notifier's
``_announce_edits`` raises ``UnicodeDecodeError`` on a member document with one invalid byte; the inherited-canvas
line prints ``canvas settings human_edits live``, which exits 2; ``knowledge import`` offers ``--adopt`` for a member
document ``--adopt`` refuses; ``mirror.json`` keys a member document's authorship by file name, so a same-named
document in another folder holding bytes this team once wrote is written over with no copy; a held first contact
baselines the document this team *would* write, so another writer's identical bytes are written over with no copy
and the held record is not refreshed; a held member line for a member who left prints an ``--adopt`` that refuses;
the "imported already" refusal prints a ``canvas undo`` that exits 1 once the marks are gone; and a record left in
place is re-copied under a new stamp by every new generation until its own copies evict its distinct history.
THIRD VOCABULARY (the second oracle extension, against the second fix pass's tree). ``ThirdWorld`` adds what people
do to the folder besides pulling into it -- editing or truncating a copy under ``inherited/``, deleting a record, a
clone without git-lfs or with CRLF conversion, a directory where a file belongs and the reverse, copies restored with
other mtimes, a record past the copy cap -- the commands that render quietly, ``canvas import`` / ``canvas undo`` of
that import / ``whiteboard clear``, a hazard armed on every step the first two never armed one on, and a checkout path
with a space or an apostrophe, plus one check (REPAIR_UNPARSEABLE). ``FolderInvariantThirdVocabularyTests`` and
``ThirdVocabularyFindingScriptTests`` FAIL on that tree, on purpose: a printed repair naming a path is unquoted, so it
exits 2 in a path with a space; ``_recorded_copy`` trusts a recorded copy that is still a file whatever it now holds,
so after an edit of the only copy the held line's ``--adopt`` and ``--force`` destroy the live document and name the
edited copy; ``--force`` over a record past the copy cap reports "drifted ... your edit" or nothing at all; ``knowledge
import`` offers ``--adopt`` for a member document past the instructions limit; a ``moved`` record is regenerated by the
next render without a word; and a held record whose document was deleted is re-reported "left exactly as it is".
If a fix makes an oracle seed pass, the seed is fixed; if it makes the oracle *quieter* any other
way -- an allowance added, an event removed, a check skipped -- the fix is wrong.

NEVER OVERWRITE (the owner's decision of 2026-10-08, after the three vocabularies above). The mirror now writes or
deletes a file only where the path is empty or the file holds the bytes this team last wrote there; nothing is copied
or adopted by itself, and the one replacement of foreign bytes is an operator act through a verified copy. Every
finding listed above came from the copy-then-overwrite design that decision removed, so each scripted test now pins
the new contract on the same shape. The oracle got stricter, not looser: its three allowances are gone (nothing has a
bound, nothing is overwritten without a copy, and an adoption no longer excuses anything), held records must leave
their file's bytes untouched, and NO AUTOMATIC COPY is checked after every step.

**The invariant**, checked after every step against an independent model of every byte-version
that has existed in the folder:

* NO LOSS: every byte-version that ever existed in the team's folder at a durable record's path and
  was not written by the team now pointed at it is still on disk at that path, or byte-identical
  somewhere under ``<team>/inherited/``. No allowance excuses a version.
* NO FALSE REPORT: every snapshot a result, board line or CLI message names holds exactly the bytes
  it claims; "pruned", "held", "regenerated" and "left exactly as it is" are each true of the disk
  at the moment they are said (a held record's file is byte-for-byte what it was before the step);
  every repair command printed exits 0 when run through the real dispatcher, and running it keeps
  NO LOSS.
* NO AUTOMATIC COPY: a file that appears under ``inherited/`` on a step that was not forced holds
  bytes an operator act read (an adopt, an import, a discard or a verified confirmation of that
  exact path), which the model records independently of what the product says. An unsolicited
  notifier authority change is a violation, never consent for a copy.

**Independence.** The model is built from what this test placed in the folder and what it found
there afterwards, by reading files. It never trusts a result dict to say what was copied: result
dicts are what it *checks*. A byte-identical file is not authorship. Its existing version gains a
writer only on an exact operator-read path (or explicit force), after a completed real atomic
write was observed and the final file readback matches those bytes. Merely matching a template,
being claimed, or appearing in a result never grants authorship.

**What counts as durable** is the owner's decision D1 of 2026-10-08, not ``MIRRORED_RECORDS``:
``knowledge.md``, ``facts.md``, ``members/<name>.md``, ``canvas.json`` and each ``canvas-assets/``
file. ``canvas.md`` is a regenerated listing nothing reads back (DISPOSABLE under D1), so the oracle
does not owe it a copy. Reading the classification out of the code under test would let a change
that reclassifies a record disposable silence the oracle.

Speed: the default run (``DEFAULT_SEEDS`` x ``DEFAULT_STEPS``) is part of the normal suite. Widen
it for a verification round with ``HERDR_SYNAPSE_ORACLE_SEEDS`` (``200``, ``40-80`` or ``3,17,29``)
and ``HERDR_SYNAPSE_ORACLE_STEPS``.
"""

from __future__ import annotations

import contextlib
import copy
import errno
import fnmatch
import hashlib
import io
import json
import os
import random
import re
import shlex
import shutil
import stat
import tempfile
import traceback
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Optional, Sequence, Set, Tuple
from unittest import mock

from herdr_team import canvas as C
from herdr_team import charter, cli, roster, store, workdir
from herdr_team import document_sync as DS
from herdr_team import facts as F
from herdr_team.identity import Author
from support import FakeApi, TempState, whiteboard_on

DEFAULT_SEEDS = 40
DEFAULT_STEPS = 30
TEAM = "alpha"
#: A member document for a name this team does not use: a previous team's, left alone.
STRANGER_MEMBER = "alpha-oldhand"
#: The one placeholder a printed line may carry: the session-archive route, offered conditionally
#: ("if the team that wrote it was dissolved in this session") and never as the repair.
CONDITIONAL_PLACEHOLDER = "<that team's name>"


def _seeds() -> List[int]:
    raw = os.environ.get("HERDR_SYNAPSE_ORACLE_SEEDS", "").strip()
    if not raw:
        return list(range(DEFAULT_SEEDS))
    if "," in raw:
        return [int(part) for part in raw.split(",") if part.strip()]
    if "-" in raw:
        low, high = raw.split("-", 1)
        return list(range(int(low), int(high)))
    return list(range(int(raw)))


def _steps() -> int:
    raw = os.environ.get("HERDR_SYNAPSE_ORACLE_STEPS", "").strip()
    return int(raw) if raw else DEFAULT_STEPS


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


#: ``facts-2.md`` and later: the numbered parts of ``facts.md`` (RELE-5). They are the same record split under its
#: cap, so D1's "facts.md" covers them; the oracle's first vocabulary never produced one, so it never needed this.
_FACTS_PART = re.compile(r"^facts-([2-9]|[1-9][0-9]+)\.md$")


def durable(rel: str) -> bool:
    """Is ``rel`` (relative to the team root) a durable record under the owner's D1 classification?"""
    if rel in ("knowledge.md", "facts.md", "canvas.json") or _FACTS_PART.match(rel):
        return True
    parts = rel.split("/")
    if len(parts) == 2 and parts[0] == "members" and parts[1].endswith(".md"):
        return True
    return len(parts) == 2 and parts[0] == "canvas-assets" and not parts[1].startswith(".")


def _probe_case_insensitive() -> bool:
    """Does the temporary directory's filesystem fold case (APFS and HFS+ by default, NTFS)?"""
    probe = Path(tempfile.mkdtemp(prefix="orc-case-"))
    try:
        (probe / "a").write_bytes(b"")
        return (probe / "A").exists()
    finally:
        shutil.rmtree(probe, ignore_errors=True)


CASE_INSENSITIVE = _probe_case_insensitive()


def canon(rel: str) -> str:
    """The one name a durable record has on this filesystem.

    On a case-folding filesystem ``Knowledge.md`` *is* ``knowledge.md``: a pull that writes one writes the other, and
    the directory entry keeps whichever spelling created it. The model keys versions by the spelling the mirror uses,
    so a record is one record whatever its entry is called. On a case-sensitive filesystem this is the identity.
    """
    if CASE_INSENSITIVE:
        low = rel.lower()
        if low != rel and durable(low):
            return low
    return rel


def _force_writable(path: Path) -> None:
    for root, dirs, files in os.walk(path):
        for name in dirs + files:
            full = os.path.join(root, name)
            try:
                if not os.path.islink(full):
                    os.chmod(full, os.stat(full).st_mode | stat.S_IWUSR | stat.S_IRUSR | (stat.S_IXUSR if name in dirs else 0))
            except OSError:
                pass
    try:
        os.chmod(path, os.stat(path).st_mode | stat.S_IRWXU)
    except OSError:
        pass


def _rmtree(path: Path) -> None:
    if not os.path.lexists(path):
        return
    if path.is_file() or path.is_symlink():
        path.unlink()
        return
    _force_writable(path)
    shutil.rmtree(path, ignore_errors=True)


# --------------------------------------------------------------------------
# the model


class Version(object):
    """One byte-version that has existed at one durable path of one folder."""

    __slots__ = ("rel", "data", "digest", "writers", "released", "origin")

    def __init__(self, rel: str, data: bytes, origin: str) -> None:
        self.rel, self.data, self.digest = rel, data, sha(data)
        #: Who put these bytes there: an int is a team generation, ``"world"`` is this test.
        self.writers: Set[Any] = set()
        #: The world itself destroyed it while no copy existed (a pull over it, ``rm -rf inherited``):
        #: git still has it, and the mirror is not to blame.
        self.released = False
        self.origin = origin

    def __repr__(self) -> str:  # pragma: no cover - diagnostics only
        return "Version({} {} by {} from {})".format(self.rel, self.digest[:10], sorted(map(str, self.writers)), self.origin)


class Model(object):
    """Every byte-version that has existed in one folder, and who wrote each."""

    def __init__(self) -> None:
        self.versions: Dict[Tuple[str, str], Version] = {}
        #: ``(rel, sha)`` of every byte-version an operator act read, which the product may then copy and replace.
        self.reads: Set[Tuple[str, str]] = set()

    def version(self, rel: str, data: bytes, origin: str) -> Version:
        key = (rel, sha(data))
        found = self.versions.get(key)
        if found is None:
            found = self.versions[key] = Version(rel, data, origin)
        return found

    def world_placed(self, rel: str, data: bytes, origin: str) -> None:
        found = self.version(rel, data, origin)
        found.writers.add("world")
        found.released = False

    def code_wrote(self, rel: str, data: bytes, gen: int, origin: str) -> None:
        self.version(rel, data, origin).writers.add(gen)

    def obligations(self, gen: int) -> List[Version]:
        return [v for v in self.versions.values()
                if not v.released and gen not in v.writers]

    def release_unrecoverable(self, disk: "Disk") -> None:
        """After a world event: whatever the world itself destroyed is the world's loss, not the mirror's."""
        for version in self.versions.values():
            if not version.released and not disk.recoverable(version):
                version.released = True


class Disk(object):
    """One reading of a team folder: every regular file, by path relative to the team root."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.files: Dict[str, bytes] = {}
        self.inherited_is_dir = (root / workdir.INHERITED_DIR_NAME).is_dir()
        if root.is_dir():
            for dirpath, dirnames, filenames in os.walk(root):
                for name in filenames:
                    full = Path(dirpath) / name
                    if name.startswith(".tmp-") or full.is_symlink() or not full.is_file():
                        continue
                    try:
                        self.files[canon(full.relative_to(root).as_posix())] = full.read_bytes()
                    except OSError:
                        continue
            # A record directory that is a symlink is read through, one level: what the mirror would prune or
            # write over there is at that record's path as far as the mirror can tell. (``inherited/`` is never
            # followed: a copy that only exists beyond a link is not in this folder.)
            for name in ("canvas-assets", "members"):
                linked = root / name
                if not (linked.is_symlink() and linked.is_dir()):
                    continue
                try:
                    entries = sorted(linked.iterdir())
                except OSError:
                    continue
                for entry in entries:
                    if entry.name.startswith(".tmp-") or entry.is_symlink() or not entry.is_file():
                        continue
                    try:
                        self.files[canon(name + "/" + entry.name)] = entry.read_bytes()
                    except OSError:
                        continue
        self.inherited: Dict[str, bytes] = {rel: data for rel, data in self.files.items()
                                            if rel.startswith(workdir.INHERITED_DIR_NAME + "/")}
        self.inherited_digests = {sha(data) for data in self.inherited.values()}

    def get(self, rel: str) -> Optional[bytes]:
        return self.files.get(rel)

    def recoverable(self, version: Version) -> bool:
        return self.files.get(version.rel) == version.data or version.digest in self.inherited_digests

    def fingerprint(self) -> str:
        return sha(json.dumps(sorted((rel, sha(data)) for rel, data in self.files.items())).encode())


# --------------------------------------------------------------------------
# what the oracle saw a step produce


class Violation(AssertionError):
    def __init__(self, check: str, detail: str) -> None:
        super().__init__("{}: {}".format(check, detail))
        self.check, self.detail = check, detail


class StepOutput(object):
    """Everything one step claimed, gathered so the claims can be checked against the disk."""

    def __init__(self) -> None:
        self.records: List[Dict[str, Any]] = []
        self.renders: List[Dict[str, Any]] = []
        self.canvases: List[Dict[str, Any]] = []
        self.adopts: List[Tuple[str, Dict[str, Any]]] = []
        self.texts: List[str] = []

    def add_render(self, result: Optional[Dict[str, Any]]) -> None:
        if not isinstance(result, dict):
            return
        self.renders.append(result)
        self.records.extend(r for r in result.get("inherited") or [] if isinstance(r, dict))
        if isinstance(result.get("canvas"), dict):
            self.canvases.append(result["canvas"])

    def add_canvas(self, result: Optional[Dict[str, Any]]) -> None:
        if not isinstance(result, dict):
            return
        self.canvases.append(result)
        self.records.extend(r for r in result.get("inherited") or [] if isinstance(r, dict))


#: Where a command ends inside a sentence. The lines put two spaces around a command and a space before
#: its comma, but a few run it into prose ("...; herdr-synapse project render --force overrules the
#: hold"), so the first plain English word after it ends it too.
_COMMAND_END = re.compile(r"  | [,.;](?=\s|$)|\n| \(| \d+ older cop"
                          r"| (?:overrules|which|lists|copies|to|and|or|if|then|before|after|is|was)\b|$")


def extract_commands(text: str) -> List[str]:
    """Every ``herdr-synapse ...`` command a line hands the operator, as it would be typed."""
    out: List[str] = []
    # A ``|``-quoted line is a file's own text shown back (``knowledge import``'s Rules), not a command this hands out.
    text = "\n".join(line for line in (text or "").split("\n") if not line.lstrip().startswith("| "))
    for match in re.finditer(r"herdr-synapse ", text or ""):
        rest = text[match.start():]
        end = _COMMAND_END.search(rest, len("herdr-synapse "))
        command = rest[:end.start() if end else len(rest)].strip().rstrip(".,;")
        if command and command != "herdr-synapse" and command not in out:
            out.append(command)
    return out


# --------------------------------------------------------------------------
# the world


class Gen(object):
    """One team generation: a state root (a session, a machine) pointed at a folder."""

    def __init__(self, index: int, project: Path, mode: Optional[str], rules: Optional[str],
                 ts: Optional[TempState] = None) -> None:
        self.index = index
        if ts is None:
            self.ts = TempState()
        else:
            # The same session after ``dissolve`` renamed the team directory into its archive: a new team of the
            # same name starts from a fresh roster in the same state root.
            from herdr_team import paths as _paths

            self.ts = ts
            _paths.ensure_team_dirs(ts.team)
            ts.write_team_json()
        whiteboard_on(self.ts.session, self.ts.team, via="cli")
        self.project = project
        self.mode = mode

        def configure(doc: roster.Team) -> None:
            if mode is None:
                doc.config.pop("document_sync", None)
            else:
                doc.config["document_sync"] = mode

        roster.update_team(self.ts.team, configure)
        if rules:
            charter.set_rules(self.ts.layout, TEAM, human(), rules)
        self.last_batch: Optional[str] = None
        self.images = 0

    @property
    def layout(self) -> Any:
        return self.ts.layout

    @property
    def paths(self) -> Any:
        return self.ts.layout.team(TEAM)

    def members(self) -> List[str]:
        doc = roster.load_team(self.paths)
        return [m.name for m in doc.members if not m.is_human]

    def active_members(self) -> List[str]:
        doc = roster.load_team(self.paths)
        return [m.name for m in doc.members if not m.is_human and m.status != "left"]


def human(name: str = "human") -> Author:
    return Author(name=name, kind="human", via="console", verified=True)


def agent(name: str = "alpha-worker") -> Author:
    return Author(name=name, kind="claude", via="pane", verified=True, pane_id="w1:p1", terminal_id="t1")


def drawer(gen: "Gen") -> C.CanvasAuthor:
    """A member drawing from the CLI, as the canvas tests draw: the operator needs a verified origin."""
    members = gen.active_members() or gen.members()
    name = "alpha-worker" if "alpha-worker" in members else members[-1]
    return C.CanvasAuthor(name, "member", "cli", True, agent="claude")


class World(object):
    """Teams sharing one project folder, driven by one seeded generator, checked after every step."""

    def __init__(self, seed: int, collect: bool = False) -> None:
        self.seed = seed
        self.rng = random.Random(seed)
        self.collect = collect
        self.violations: List[Dict[str, Any]] = []
        self.trace: List[str] = []
        self.tmp = Path(tempfile.mkdtemp(prefix="orc-")).resolve()
        self.project = self.tmp / "p0"
        self.project.mkdir()
        self.projects = 1
        self.model = Model()
        self.gens: List[Gen] = []
        self.counter = 0
        self.clock = 1000.0
        self.branches: Dict[str, List[bytes]] = {}
        self.branch_at: Dict[str, int] = {}
        self.folder_ro = False
        self.inherited_ro = False
        self.checked_commands: Set[Tuple[str, str]] = set()
        self.step_label = ""
        self.commands_run = 0
        #: The generation acting right now, when it is not the newest one (a second team on the same folder).
        self.acting: Optional[Gen] = None
        self.peer: Optional[Gen] = None
        self.peers: List[Gen] = []
        self.next_index = 0
        #: Files outside the team folder that a symlink inside it reaches: no step may change or delete them.
        self.outside: Dict[str, bytes] = {}
        #: Directories outside the project that hold such files (restored after a printed repair runs).
        self.outside_roots: List[Path] = []
        #: One model per project folder this world has used, so a team that moves back finds its own history.
        self.models: Dict[Path, Model] = {}

    # -- plumbing ----------------------------------------------------------

    @property
    def gen(self) -> Gen:
        return self.acting if self.acting is not None else self.gens[-1]

    @contextlib.contextmanager
    def as_gen(self, gen: "Gen") -> Iterator[None]:
        """Run the steps inside as ``gen``: a second team pointed at the same folder."""
        previous, self.acting = self.acting, gen
        try:
            yield
        finally:
            self.acting = previous

    def take_index(self) -> int:
        index, self.next_index = self.next_index, self.next_index + 1
        return index

    @property
    def root(self) -> Path:
        return workdir.team_root(os.fspath(self.project), TEAM)

    def files(self) -> Dict[str, Path]:
        return workdir.paths_for(os.fspath(self.project), TEAM)

    def token(self) -> str:
        self.counter += 1
        return "s{}n{}".format(self.seed, self.counter)

    def disk(self) -> Disk:
        return Disk(self.root)

    def close(self) -> None:
        done: Set[int] = set()
        for gen in self.gens + self.peers:
            if id(gen.ts) in done:
                continue
            done.add(id(gen.ts))
            _force_writable(gen.ts.tmp)
            gen.ts.cleanup()
        _rmtree(self.tmp)

    def run_cli(self, *argv: str) -> Tuple[int, Any, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "build_parser", _parser):
            code = cli.main(["--json"] + list(argv) + ["--team", TEAM], env=self.gen.ts.env,
                            stdout=out, stderr=err, api=FakeApi())
        text = out.getvalue().strip()
        try:
            payload = json.loads(text) if text else None
        except ValueError:
            payload = text
        return code, payload, err.getvalue()

    def fail(self, check: str, detail: str) -> None:
        violation = {"seed": self.seed, "step": len(self.trace), "event": self.step_label,
                     "check": check, "detail": detail}
        self.violations.append(violation)
        if not self.collect:
            raise Violation(check, detail)

    def report(self) -> str:
        first = self.violations[0]["step"] if self.violations else len(self.trace)
        lines = ["seed {}: {} violation(s) in {} steps, the first at step {}; the events:".format(
            self.seed, len(self.violations), len(self.trace), first)]
        lines.extend("  {:>2}. {}".format(n + 1, label) for n, label in enumerate(self.trace))
        return "\n".join(lines)

    # -- the world's own writes ------------------------------------------

    def world_write(self, rel: str, data: bytes, origin: str) -> None:
        """A pull, a checkout, a colleague's rsync: the world puts bytes at a path.

        ``rel`` may be spelled differently from the record (``Knowledge.md``); the model keys it by ``canon``. A
        symlink on the way is replaced, as git replaces one: the world never writes *through* a link.
        """
        key = canon(rel)
        path = self.root / rel
        before = self.disk()
        current = before.get(key)
        if current is not None and durable(key):
            old = self.model.versions.get((key, sha(current)))
            if old is not None and sha(current) not in before.inherited_digests:
                old.released = True  # the world wrote over the only live copy; git still has it
        cursor = self.root
        unlinked: List[str] = []
        for part in Path(rel).parts:
            cursor = cursor / part
            if cursor.is_symlink():
                cursor.unlink()
                unlinked.append(cursor.relative_to(self.root).as_posix())
        if unlinked:
            # What was reachable only through the link left with it: the world's doing, not the mirror's.
            after = self.disk()
            for version in self.model.versions.values():
                if any(version.rel == u or version.rel.startswith(u + "/") for u in unlinked) \
                        and not after.recoverable(version):
                    version.released = True
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.is_dir():
            _rmtree(path)
        path.write_bytes(data)
        if durable(key):
            self.model.world_placed(key, data, origin)

    def place_outside(self, path: Path, data: bytes) -> None:
        """A file outside the team folder (the user's own, or the repository's source) that a symlink will reach."""
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        self.outside[os.fspath(path)] = data

    def _check_outside(self) -> None:
        """Nothing the mirror does through its folder may change or delete a file outside it."""
        for name, data in list(self.outside.items()):
            path = Path(name)
            try:
                now = path.read_bytes() if path.is_file() and not path.is_symlink() else None
            except OSError:
                now = None
            if now == data:
                continue
            self.fail("OUTSIDE_DESTROYED", "{} is outside the team folder, reached only through a symlink in it, and this "
                      "step {} it ({} bytes before)".format(name, "deleted" if now is None else "changed", len(data)))
            if now is None:
                del self.outside[name]
            else:
                self.outside[name] = now

    def world_remove(self, rel: str) -> None:
        _rmtree(self.root / rel)
        self.model.release_unrecoverable(self.disk())

    # -- document shapes --------------------------------------------------

    def document(self, rel: str, flavor: str, token: str, outside: bool = True) -> bytes:
        """A version of one durable document as a previous team, a branch or a stranger would leave it."""
        path = self.root / rel
        marker = workdir.marker_for(path)
        if rel == "knowledge.md":
            body = "\n".join([marker, "# alpha knowledge", "", DS.RULES_HEADING, "",
                              "rules from {}: ask before deploying".format(token), "", DS.FINDINGS_HEADING, "",
                              "- **beta-worker** (2026-01-01): finding {}".format(token), ""])
            if flavor == "unadoptable":
                body = body.replace(DS.FINDINGS_HEADING, DS.RULES_HEADING + "\n\n" + DS.FINDINGS_HEADING, 1)
        elif rel == "facts.md":
            body = "\n".join([marker, "# alpha facts", "", "## F-1  the cache hit rate is {}".format(token),
                              "", "- by: beta-worker (claude)", ""])
        elif rel.startswith("members/"):
            name = Path(rel).stem
            lines = [marker, "# {}".format(name), ""]
            if outside:
                lines += ["A note above the first heading, only here: {}".format(token), ""]
            lines += ["## Mission", "", "mission {}".format(token), "", "## Scope", "", "scope {}".format(token), ""]
            body = "\n".join(lines)
        elif rel == "canvas.json":
            payload = {"payload": 1, "team": "beta", "scene_version": 9,
                       "elements": [{"id": "E-1", "type": "shape", "text": "board {}".format(token)}]}
            encoded = json.dumps(payload, separators=(",", ":"))
            body = workdir.MARKER_JSON + "\n" + encoded[1:] + "\n"
            if flavor == "markerless":
                return (encoded + "\n").encode("utf-8")
        else:
            raise ValueError(rel)
        if flavor == "markerless":
            return "# hand written, no marker\n\n{}\n".format(token).encode("utf-8")
        if flavor == "foreign-marker":
            return "<!-- another-tool v3 managed file -->\n# notes\n\n{}\n".format(token).encode("utf-8")
        if flavor == "conflict":
            return ("<<<<<<< HEAD\n" + body + "=======\n" + body.replace(token, token + "-theirs")
                    + ">>>>>>> branch\n").encode("utf-8")
        if flavor == "invalid-utf8":
            return body.encode("utf-8") + b"\xff\xfe not utf-8 " + token.encode("utf-8") + b"\n"
        return body.encode("utf-8")

    def asset(self, token: str, wrong_name: bool = False) -> Tuple[str, bytes]:
        data = b"\x89PNG\r\n\x1a\n a previous team's picture " + token.encode("utf-8")
        digest = sha(data)[:32] if not wrong_name else sha(b"other" + data)[:32]
        return digest + ".png", data

    # -- one code step -----------------------------------------------------

    def code_step(self, label: str, action: Callable[[StepOutput], None], forced: bool = False,
                  hazard: Optional[Callable[["World"], Any]] = None,
                  adoption: Sequence[str] = (), reads: Sequence[str] = ()) -> None:
        """Run product code once, then hold every claim it made against the disk."""
        gen = self.gen.index
        pre = self.disk()
        written: Dict[str, bytes] = {}
        raced: Dict[str, List[bytes]] = {}
        self._raced = raced
        output = StepOutput()
        real_write = store.atomic_write

        def observing(path: Any, data: bytes, *args: Any, **kwargs: Any) -> None:
            real_write(path, data, *args, **kwargs)
            written[os.fspath(path)] = bytes(data)

        rules_before = self._stored()
        with mock.patch.object(store, "atomic_write", observing):
            context = hazard(self) if hazard is not None else None
            if context is not None:
                context.__enter__()
            try:
                action(output)
            except Violation:
                raise
            except Exception:  # noqa: BLE001 - a mirror is best effort and must never raise
                self.fail("CRASH", traceback.format_exc(limit=6))
            finally:
                if context is not None:
                    context.__exit__(None, None, None)
        self._check_outside()
        post = self.disk()
        self._absorb(pre, post, raced, gen, label, written, reads, forced)
        self._release(pre, adoption, reads, rules_before, raced)
        self._check_no_loss(post, gen, output)
        self._check_no_automatic_copy(pre, post, forced)
        self._check_claims(output, pre, post, raced, written, gen)
        # Whatever is unrecoverable now passed the check: it was this team's own output. Neither a later
        # team nor a later command owes anything for bytes that were gone before it ran.
        self.model.release_unrecoverable(post)
        for command in self._printed(output):
            self._check_command(command)

    def _absorb(self, pre: Disk, post: Disk, raced: Dict[str, List[bytes]], gen: int, label: str,
                written: Dict[str, bytes], reads: Sequence[str], forced: bool) -> None:
        for rel, data in post.files.items():
            if not durable(rel) or pre.get(rel) == data or data in raced.get(rel, ()):
                continue
            self.model.code_wrote(rel, data, gen, label)
        # Explicit replacement may write the same canonical bytes. Only a completed real
        # write at its independently bound read path, with matching readback, proves authorship.
        for path, data in written.items():
            rel = self._rel(path)
            if rel and durable(rel) and (forced or rel in reads) and pre.get(rel) == data \
                    and post.get(rel) == data and data not in raced.get(rel, ()):
                self.model.code_wrote(rel, data, gen, label + " (observed operator write)")

    def _stored(self) -> Dict[str, str]:
        """The team's stored rules and instructions, by the path each is mirrored at: what an adoption changes."""
        layout = self.gen.layout
        out = {"knowledge.md": charter.get_rules(layout, TEAM) or ""}
        for name in self.gen.members():
            out["members/{}.md".format(name)] = charter.get_instructions(layout, TEAM, name) or ""
        return out

    def _release(self, pre: Disk, adopted: Sequence[str], reads: Sequence[str], before: Dict[str, str],
                 raced: Optional[Dict[str, List[bytes]]] = None) -> None:
        """An operator act read these files: the bytes it read may now be copied and replaced.

        ``reads`` is what the step's own command reads (``--adopt`` its member document, ``knowledge import`` the
        folder's knowledge and facts, ``canvas import`` the payload, ``--discard`` the member document, or a verified
        confirmation's independently bound document). The legacy ``adopted`` paths now assert that a notifier did
        not change stored authority without such an operator read. They never grant a read allowance. A version a
        race put at an explicit read path during the step was there for that operator command to read as well
        (two imports in one step read two versions).
        """
        after = self._stored()
        for rel in adopted:
            if rel not in reads and before.get(rel) != after.get(rel):
                self.fail("UNCONFIRMED_AUTHORITY", "{} changed stored authority during an autonomous step, without "
                          "an explicitly bound operator read; that change authorizes no copy".format(rel))
        for rel in reads:
            for live in [rel] + ([r for r in pre.files if _FACTS_PART.match(r)] if rel == "facts.md" else []):
                for data in [pre.get(live)] + list((raced or {}).get(live, ())):
                    if data is not None:
                        self.model.reads.add((live, sha(data)))

    # -- NO AUTOMATIC COPY -------------------------------------------------

    def _check_no_automatic_copy(self, pre: Disk, post: Disk, forced: bool) -> None:
        """A copy under ``inherited/`` exists only because an operator ordered a replacement.

        The owner's rule (2026-10-08): nothing is copied by itself. Every file that appears under ``inherited/`` is
        either taken on a forced step or holds bytes an operator act (an adopt, an import, a discard) read.
        """
        if forced:
            return
        for rel, data in post.inherited.items():
            if pre.inherited.get(rel) == data:
                continue
            live = _live_of_copy(rel)
            if live is not None and (live, sha(data)) in self.model.reads:
                continue
            self.fail("NO_AUTOMATIC_COPY", "{} appeared under inherited/ ({} bytes) on a step no operator ordered a "
                      "replacement on, and holds no bytes an operator act released".format(rel, len(data)))

    # -- NO LOSS -----------------------------------------------------------

    def _check_no_loss(self, post: Disk, gen: int, output: StepOutput) -> None:
        """No allowances: under the never-overwrite rule every version this team did not write survives at its path
        or, after an operator-ordered replacement, byte-identical under ``inherited/``."""
        for version in self.model.obligations(gen):
            if post.recoverable(version):
                continue
            said = [{k: r.get(k) for k in ("kind", "held", "reason", "regenerated", "snapshot")}
                    for r in output.records if self._rel(r.get("path")) == version.rel]
            self.fail("NO_LOSS", "{} ({} bytes, sha {}, from {}) is not on disk at its path and not under "
                      "inherited/; written by {}; this step's records for it: {}".format(
                          version.rel, len(version.data), version.digest[:12], version.origin,
                          sorted(map(str, version.writers)), said or "none"))
            version.released = True  # collect mode: count each loss once

    # -- NO FALSE REPORT ---------------------------------------------------

    def _rel(self, path: Any) -> Optional[str]:
        if not path:
            return None
        try:
            return Path(str(path)).relative_to(self.root).as_posix()
        except ValueError:
            return None

    def _check_claims(self, output: StepOutput, pre: Disk, post: Disk, raced: Dict[str, List[bytes]],
                      written: Dict[str, bytes], gen: int) -> None:
        for record in output.records:
            self._check_record(record, pre, post, raced, written, gen)
        self._check_fresh_claims(output, pre, post, raced, gen)
        for result in output.canvases:
            self._check_canvas(result, pre, post, raced)
        for record in output.records:
            rel = self._rel(record.get("path"))
            if record.get("held") and rel is not None and rel in pre.files and durable(rel):
                if post.get(rel) != pre.get(rel) and post.get(rel) not in raced.get(rel, ()):
                    self.fail("HELD_BUT_WRITTEN", "{} is reported held ({}) but its bytes changed".format(
                        rel, record.get("reason")))
        for result in output.renders:
            for key in ("skipped", "sync_pending"):
                for path in result.get(key) or []:
                    rel = self._rel(path)
                    if rel is None or not durable(rel) or rel not in pre.files:
                        continue
                    if post.get(rel) != pre.get(rel) and post.get(rel) not in raced.get(rel, ()):
                        self.fail("HELD_BUT_WRITTEN", "{} is reported {} but its bytes changed".format(rel, key))
        for member, payload in output.adopts:
            snapshot = payload.get("snapshot") if isinstance(payload, dict) else None
            if not snapshot:
                continue
            rel = "members/{}.md".format(member)
            if not Path(snapshot).is_file():
                self.fail("ADOPT_SNAPSHOT_MISSING", "--adopt says it copied {} to {}, which is not a file".format(rel, snapshot))
            elif Path(snapshot).read_bytes() != pre.get(rel):
                self.fail("ADOPT_SNAPSHOT_WRONG", "--adopt says the lines it drops from {} were copied verbatim to {}, "
                          "but that file holds other bytes than the document it adopted".format(rel, snapshot))

    def _check_record(self, record: Dict[str, Any], pre: Disk, post: Disk, raced: Dict[str, List[bytes]],
                      written: Dict[str, bytes], gen: int) -> None:
        rel = self._rel(record.get("path"))
        snapshot = record.get("snapshot")
        what = "{} record for {}".format(record.get("kind"), rel or record.get("path"))
        if snapshot:
            snap = Path(str(snapshot))
            if not snap.is_file():
                self.fail("SNAPSHOT_MISSING", "{} names {}, which is not a file".format(what, snapshot))
                return
            if self._rel(snap) is None or not self._rel(snap).startswith(workdir.INHERITED_DIR_NAME + "/"):
                self.fail("SNAPSHOT_MISPLACED", "{} names a copy outside inherited/: {}".format(what, snapshot))
            copied = snap.read_bytes()
            if rel is not None:
                replaced = bool(record.get("regenerated"))
                if record.get("held") or not replaced:
                    if post.get(rel) is None and (self.root / rel).is_symlink():
                        pass  # a symlink is held as itself; the oracle's reading of the folder never follows one
                    elif post.get(rel) is None:
                        self.fail("HELD_RECORD_GONE", "{} says the record was left exactly as it is, and it is gone".format(what))
                    elif post.get(rel) != copied:
                        self.fail("SNAPSHOT_NOT_CURRENT", "{} says {} holds the record that is still in place, and the "
                                  "bytes differ".format(what, snapshot))
                elif durable(rel) and (rel, sha(copied)) not in self.model.versions:
                    # A record is re-emitted until a caller shows it, so it may describe an earlier
                    # step's replacement; what it may never do is name bytes that never stood there.
                    self.fail("SNAPSHOT_UNKNOWN_BYTES", "{} names {}, which holds no version that ever stood at {}".format(
                        what, snapshot, rel))

    def _check_fresh_claims(self, output: StepOutput, pre: Disk, post: Disk,
                            raced: Dict[str, List[bytes]], gen: int) -> None:
        """A step that wrote over a version this team did not write, and says so, names *those* bytes.

        The per-record check above has to tolerate a re-emitted report about an earlier step. This
        one does not: when the code replaced a foreign version in this very step and this step's
        records claim the path was replaced, one of them must name a copy of exactly what was
        destroyed -- otherwise the operator is handed the copy of a different document (DATA-2).
        """
        for rel in sorted(set(pre.files) | set(post.files)):
            if not durable(rel) or pre.get(rel) is None or pre.get(rel) == post.get(rel):
                continue
            before = self.model.versions.get((rel, sha(pre.get(rel))))
            if before is None or gen in before.writers or before.data in raced.get(rel, ()):
                continue
            claims = [r for r in output.records if self._rel(r.get("path")) == rel
                      and r.get("regenerated") and r.get("snapshot")]
            if not claims:
                continue
            named = [Path(str(r["snapshot"])).read_bytes() for r in claims if Path(str(r["snapshot"])).is_file()]
            if not named:
                continue  # SNAPSHOT_MISSING has already said the copy is not there
            if pre.get(rel) not in named and not any(data in named for data in raced.get(rel, ())):
                self.fail("SNAPSHOT_WRONG_BYTES", "{} was written over in this step and its record names {} as the copy, "
                          "which does not hold the bytes destroyed".format(rel, ", ".join(str(r["snapshot"]) for r in claims)))

    def _check_canvas(self, result: Dict[str, Any], pre: Disk, post: Disk, raced: Dict[str, List[bytes]]) -> None:
        for name in result.get("assets_pruned") or []:
            if post.get("canvas-assets/" + name) is not None:
                self.fail("ASSET_PRUNED_FALSE", "assets_pruned names {}, which is still there".format(name))
        for name in result.get("assets_held") or []:
            rel = "canvas-assets/" + name
            if pre.get(rel) is not None and post.get(rel) != pre.get(rel):
                self.fail("ASSET_HELD_FALSE", "assets_held names {}, which is no longer as it was".format(name))

    # -- every printed repair runs ----------------------------------------

    def _printed(self, output: StepOutput) -> List[str]:
        commands: List[str] = []
        texts = list(output.texts)
        for record in output.records:
            texts.append(DS.describe_inherited(record, style="board"))
            texts.append(DS.describe_inherited(record, style="list"))
            if record.get("command"):
                texts.append(str(record["command"]))
        for text in texts:
            for command in extract_commands(text):
                if command not in commands:
                    commands.append(command)
        return commands

    def _fingerprint(self) -> str:
        paths = self.gen.paths
        parts = [self.disk().fingerprint()]
        for path in (DS.state_path(paths), paths.mirror_json, paths.team_json):
            try:
                parts.append(sha(Path(path).read_bytes()))
            except OSError:
                parts.append("-")
        parts.append(str(C.current_version(paths)))
        return "|".join(parts)

    def _check_command(self, command: str) -> None:
        """Run one printed repair through the real dispatcher in a copy of the world, then restore it."""
        if CONDITIONAL_PLACEHOLDER in command:
            return
        if "<" in command or ">" in command:
            self.fail("REPAIR_PLACEHOLDER", "a printed repair cannot be run as printed: {}".format(command))
            return
        key = (command, self._fingerprint())
        if key in self.checked_commands:
            return
        self.checked_commands.add(key)
        self.commands_run += 1
        words = shlex.split(command)
        backup = self.tmp / "sandbox"
        _rmtree(backup)
        sources = {"state": self.gen.ts.tmp, "project": self.project}
        for n, root in enumerate(self.outside_roots):
            if root.is_dir():
                sources["outside-{}".format(n)] = root
        for name, source in sources.items():
            shutil.copytree(os.fspath(source), os.fspath(backup / name), symlinks=True)
        model = copy.deepcopy(self.model)
        outside = dict(self.outside)
        label, self.step_label = self.step_label, "{} -> printed: {}".format(self.step_label, command)
        try:
            forced = "--force" in words
            output = StepOutput()

            def run(out: StepOutput) -> None:
                try:
                    code, payload, err = self.run_cli(*words[1:])
                except Exception:  # noqa: BLE001 - a printed repair that raises is the finding
                    self.fail("REPAIR_CRASH", "printed repair `{}` raised:\n{}".format(
                        command, traceback.format_exc(limit=4)))
                    return
                if code != 0:
                    self.fail("REPAIR_EXIT", "printed repair `{}` exited {}: {}".format(command, code, err.strip()[:300]))
                if words[1:3] == ["project", "render"] and isinstance(payload, dict):
                    out.add_render(payload)
                if words[1:2] == ["instructions"] and "--adopt" in words and isinstance(payload, dict):
                    out.adopts.append((words[2], payload))

            reads = ["members/{}.md".format(words[2])] if words[1:2] == ["instructions"] and "--adopt" in words else []
            if words[1:3] == ["project", "confirm"] and len(words) > 3:
                # Resolve consent before executing it, independently of the
                # command's report. Only this advertised proposal's exact file
                # can be released; scans and renders still release nothing.
                proposals = store.read_json(DS.state_path(self.gen.paths), default={})
                for path, entry in proposals.items() if isinstance(proposals, dict) else []:
                    proposal = entry.get("proposal") if isinstance(entry, dict) else None
                    if isinstance(proposal, dict) and proposal.get("id") == words[3]:
                        rel = self._rel(path)
                        if rel and durable(rel):
                            reads.append(rel)
            self._nested_step(run, forced, output, reads)
        finally:
            self.step_label = label
            self.model = model
            self.outside = outside
            for name, source in sources.items():
                _rmtree(source)
                shutil.copytree(os.fspath(backup / name), os.fspath(source), symlinks=True)
            _rmtree(backup)

    def _nested_step(self, action: Callable[[StepOutput], None], forced: bool, output: StepOutput,
                     reads: Sequence[str] = ()) -> None:
        """A printed repair's consequences, held to NO LOSS. Its own printed lines are not followed again."""
        gen = self.gen.index
        pre = self.disk()
        rules_before = self._stored()
        written: Dict[str, bytes] = {}
        real_write = store.atomic_write

        def observing(path: Any, data: bytes, *args: Any, **kwargs: Any) -> None:
            real_write(path, data, *args, **kwargs)
            written[os.fspath(path)] = bytes(data)

        with mock.patch.object(store, "atomic_write", observing):
            action(output)
        self._check_outside()
        post = self.disk()
        self._absorb(pre, post, {}, gen, self.step_label, written, reads, forced)
        self._release(pre, (), reads, rules_before)
        self._check_no_loss(post, gen, output)
        self._check_no_automatic_copy(pre, post, forced)
        self._check_claims(output, pre, post, {}, written, gen)

    # -- events ------------------------------------------------------------

    def new_gen(self, project: Path, mode: Any = "random", rules: Any = "random", ts: Optional[TempState] = None) -> None:
        """A new team generation pointed at ``project``: dissolve-and-recreate, a restart, another machine.

        ``ts`` reuses a state root: a team recreated under the same name in the same session after ``dissolve``.
        """
        index = self.take_index()
        if mode == "random":
            mode = self.rng.choice(["auto", "auto", "auto", "manual", None])
        if rules == "random":
            rules = None if self.rng.random() < 0.6 else "rules this team's operator typed: {}".format(self.token())
        self.project = project
        self.gens.append(Gen(index, project, mode, rules, ts=ts))
        self.step_label = "create gen {} ({}, {}) --project {}".format(index, mode or "default", "rules" if rules else "no rules", project.name)

        def create(out: StepOutput) -> None:
            code, payload, err = self.run_cli("project", "set", os.fspath(project))
            if code != 0:
                self.fail("CREATE_EXIT", err.strip()[:300])
            out.add_render(payload if isinstance(payload, dict) else None)
            self._spend(self.gen.paths, out.records)

        self.trace.append(self.step_label)
        self.code_step(self.step_label, create)

    def ev_render(self) -> None:
        def run(out: StepOutput) -> None:
            result = workdir.render(self.gen.layout, TEAM)
            out.add_render(result)
            out.texts.extend(DS.describe_inherited(r) for r in result.get("inherited") or [])
            self._spend(self.gen.paths, result.get("inherited") or [])
        self.code_step("render", run)

    def ev_cli_render(self, force: bool = False) -> None:
        def run(out: StepOutput) -> None:
            from herdr_team import cmd_knowledge

            argv = ["project", "render"] + (["--force"] if force else [])
            code, payload, err = self.run_cli(*argv)
            if code != 0:
                self.fail("RENDER_EXIT", "project render{} exited {}: {}".format(" --force" if force else "", code, err[:300]))
            if isinstance(payload, dict):
                out.add_render(payload)
                out.texts.append(cmd_knowledge._render_result_text(payload))
        self.code_step("project render" + (" --force" if force else ""), run, forced=force)

    def _spend(self, paths: Any, records: Any) -> None:
        """What the notifier does once it has boarded the records, and as it does it: never fatally
        (``daemon._refresh_workdir`` catches everything, so the daemon outlives one bad write)."""
        try:
            DS.mark_reported(paths, records)
        except Exception:  # noqa: BLE001 - mirrors the daemon's own guard
            pass

    def mirror_canvas(self, out: StepOutput) -> None:
        """The notifier's canvas tick: the mirror follows the board within seconds of a draw."""
        result = workdir.render_canvas_snapshot(self.gen.layout, TEAM)
        out.add_canvas(result)
        out.texts.extend(DS.describe_inherited(r) for r in result.get("inherited") or [])
        self._spend(self.gen.paths, result.get("inherited") or [])

    def ev_draw(self, images: int = 0) -> None:
        gen = self.gen

        def run(out: StepOutput) -> None:
            ops: List[Dict[str, Any]] = [{"op": "shape", "text": "mark {}".format(self.token()),
                                          "at": "c{}r{}".format(self.rng.randrange(0, 40), self.rng.randrange(0, 20)),
                                          "intent": "the oracle's board"}]
            renders = C._dir(gen.paths) / C.RENDERS_DIR
            renders.mkdir(parents=True, exist_ok=True)
            for _ in range(images):
                gen.images += 1
                picture = renders / "pic-{}.png".format(gen.images)
                picture.write_bytes(_png(10 + gen.images, 7 + gen.index))
                ops.append({"op": "image", "path": os.fspath(picture),
                            "at": "c{}r{}".format(self.rng.randrange(0, 40), 30 + gen.images), "intent": "a picture"})
            result = C.apply_ops(gen.layout, gen.paths, ops, drawer(gen))
            if result.get("refused"):
                return
            gen.last_batch = result.get("batch") or gen.last_batch
            self.mirror_canvas(out)
        self.code_step("draw" + (" {} picture(s)".format(images) if images else ""), run)

    def ev_undo(self) -> None:
        gen = self.gen

        def run(out: StepOutput) -> None:
            if not gen.last_batch:
                return
            C.apply_ops(gen.layout, gen.paths, [{"op": "undo", "batch": gen.last_batch, "intent": "take it back"}], drawer(gen))
            gen.last_batch = None
            self.mirror_canvas(out)
        self.code_step("canvas undo", run)

    def ev_canvas_import(self) -> None:
        source = self.rng.choice(["folder", "inherited"])

        def run(out: StepOutput) -> None:
            target = self.root
            if source == "inherited":
                copies = sorted((self.root / workdir.INHERITED_DIR_NAME).glob("canvas-*.json")) \
                    if (self.root / workdir.INHERITED_DIR_NAME).is_dir() else []
                if not copies:
                    return
                target = copies[-1]
            self.run_cli("canvas", "import", "--from", os.fspath(target))
            self.mirror_canvas(out)
        self.code_step("canvas import --from {} (+ mirror)".format(source), run, reads=["canvas.json"])

    def ev_adopt(self, name: Optional[str] = None) -> None:
        members = self.gen.members()
        name = name or self.rng.choice(members)
        rel = "members/{}.md".format(name)

        def run(out: StepOutput) -> None:
            code, payload, err = self.run_cli("instructions", name, "--adopt", "--yes")
            if code == 0 and isinstance(payload, dict):
                out.adopts.append((name, payload))
                if payload.get("adopted"):
                    # The adoption's own render: its records are what it printed.
                    pass
        self.code_step("instructions {} --adopt".format(name), run, reads=[rel])

    def ev_knowledge_import(self) -> None:
        def run(out: StepOutput) -> None:
            self.run_cli("knowledge", "import", "--from", os.fspath(self.root), "--yes")
        self.code_step("knowledge import --from <this folder>", run, reads=["knowledge.md", "facts.md", "canvas.json"])

    def ev_settle(self) -> None:
        self.clock += 10.0

        def run(out: StepOutput) -> None:
            for tick in (self.clock, self.clock + DS.SETTLE_SECONDS + 1):
                result = DS.scan(self.gen.layout, TEAM, now=tick)
                out.records.extend(result.get("inherited") or [])
        rels = ["knowledge.md"] + ["members/{}.md".format(m) for m in self.gen.members()]
        self.code_step("notifier settle (two scans)", run, adoption=rels)

    def ev_activity(self) -> None:
        layout, paths = self.gen.layout, self.gen.paths
        kind = self.rng.choice(["finding", "fact", "rules", "instructions"])
        token = self.token()
        if kind == "finding":
            charter.add_finding(layout, TEAM, agent(), "finding {}".format(token))
        elif kind == "fact":
            F.add(paths, {"statement": "fact {}".format(token), "about": "oracle", "attribute": token,
                          "by": "alpha-worker", "by_kind": "claude"}, mode="add")
        elif kind == "rules":
            charter.set_rules(layout, TEAM, human(), "rules {}".format(token))
        else:
            charter.set_instructions(layout, TEAM, human(), self.rng.choice(self.gen.active_members() or self.gen.members()),
                                     "mission {}".format(token), None)
        self.trace[-1] = "team activity: {}".format(kind)

    def ev_rename(self) -> None:
        members = self.gen.members()
        name = self.rng.choice(members)
        self.counter += 1
        new = "{}-r{}".format(re.sub(r"-r\d+$", "", name), self.counter)

        def rename(doc: roster.Team) -> None:
            member = doc.find(name)
            if member is not None:
                roster.apply_adoption(member, new)

        roster.update_team(self.gen.paths, rename)
        self.trace[-1] = "rename member {} -> {}".format(name, new)

    def ev_sync_toggle(self) -> None:
        mode = self.rng.choice(["auto", "manual"])

        def run(out: StepOutput) -> None:
            code, payload, err = self.run_cli("project", "sync", mode)
            if isinstance(payload, dict):
                out.records.extend(payload.get("inherited") or [])
        self.code_step("project sync {}".format(mode), run)

    def ev_pull(self) -> None:
        rel = self.rng.choice(["knowledge.md", "facts.md", "members", "members", "members", "canvas.json",
                               "stranger-member"])
        if rel == "members":
            rel = "members/{}.md".format(self.rng.choice(self.gen.members()))
        elif rel == "stranger-member":
            rel = "members/{}.md".format(STRANGER_MEMBER)
        flavor = self.rng.choice(["plausible", "plausible", "plausible", "markerless", "foreign-marker",
                                  "conflict", "invalid-utf8"] + (["unadoptable"] if rel == "knowledge.md" else []))
        data = self.document(rel, flavor, self.token(), outside=self.rng.random() < 0.6)
        self.world_write(rel, data, "pull {} {}".format(flavor, rel))
        self.trace[-1] = "git pull delivers {} ({})".format(rel, flavor)

    def ev_branch_switch(self) -> None:
        rel = self.rng.choice(["knowledge.md", "facts.md", "members/{}.md".format(self.gen.members()[0]), "canvas.json"])
        pair = self.branches.get(rel)
        if pair is None:
            pair = self.branches[rel] = [self.document(rel, "plausible", self.token() + "-branchA"),
                                         self.document(rel, "plausible", self.token() + "-branchB")]
        side = self.branch_at.get(rel, 1) ^ 1
        self.branch_at[rel] = side
        self.world_write(rel, pair[side], "branch {} of {}".format("AB"[side], rel))
        self.trace[-1] = "git checkout branch {}: {}".format("AB"[side], rel)

    def previous_board(self, count: int) -> None:
        names = []
        for _ in range(count):
            name, data = self.asset(self.token())
            self.world_write("canvas-assets/" + name, data, "previous board picture")
            names.append(name)
        payload = {"payload": 1, "team": "beta", "scene_version": 9,
                   "elements": [{"id": "E-{}".format(n + 1), "type": "image", "asset": name}
                                for n, name in enumerate(names)]}
        encoded = json.dumps(payload, separators=(",", ":"))
        self.world_write("canvas.json", (workdir.MARKER_JSON + "\n" + encoded[1:] + "\n").encode("utf-8"),
                         "previous board payload")

    def ev_previous_board(self) -> None:
        count = self.rng.choice([1, 2, 3, 9, 10, 12, 14])
        self.previous_board(count)
        self.trace[-1] = "a previous team's board arrives with {} picture(s)".format(count)

    def ev_previous_folder(self) -> None:
        token = self.token()
        self.world_write("knowledge.md", self.document("knowledge.md", "plausible", token), "previous folder")
        self.world_write("facts.md", self.document("facts.md", "plausible", token), "previous folder")
        for name in self.gen.members():
            self.world_write("members/{}.md".format(name), self.document("members/{}.md".format(name), "plausible",
                                                                          token, outside=self.rng.random() < 0.6),
                             "previous folder")
        count = self.rng.choice([2, 9, 11])
        self.previous_board(count)
        self.trace[-1] = "a previous team's whole folder arrives ({} pictures)".format(count)

    def ev_stray_asset(self) -> None:
        wrong = self.rng.random() < 0.3
        name, data = self.asset(self.token(), wrong_name=wrong)
        self.world_write("canvas-assets/" + name, data, "stray asset")
        self.trace[-1] = "a stray picture arrives{}".format(" under a name its bytes do not hash to" if wrong else "")

    def set_folder_ro(self, on: bool) -> None:
        root = self.root
        dirs = [root, root / "members", root / "canvas-assets", root / "artifacts"]
        inherited = root / workdir.INHERITED_DIR_NAME
        if inherited.is_dir():
            dirs += [inherited] + [d for d in inherited.iterdir() if d.is_dir()]
        for d in dirs:
            if d.is_dir() and not d.is_symlink():
                os.chmod(d, 0o555 if on else 0o755)
        self.folder_ro = on
        if not on and self.inherited_ro:
            self.set_inherited_ro(True)

    def set_inherited_ro(self, on: bool) -> None:
        inherited = self.root / workdir.INHERITED_DIR_NAME
        if on:
            if not inherited.exists():
                inherited.mkdir(parents=True)
            if not inherited.is_dir():
                return
            for d in [inherited] + [d for d in inherited.iterdir() if d.is_dir()]:
                os.chmod(d, 0o555)
        elif inherited.is_dir():
            for d in [inherited] + [d for d in inherited.iterdir() if d.is_dir()]:
                os.chmod(d, 0o755)
        self.inherited_ro = on

    def ev_inherited_missing(self) -> None:
        self.set_inherited_ro(False)
        self.world_remove(workdir.INHERITED_DIR_NAME)
        self.trace[-1] = "someone deletes inherited/"

    def ev_inherited_file(self) -> None:
        self.set_inherited_ro(False)
        self.world_remove(workdir.INHERITED_DIR_NAME)
        (self.root / workdir.INHERITED_DIR_NAME).write_bytes(b"")
        self.trace[-1] = "an agent runs: touch <team>/inherited"

    def ev_inherited_file_removed(self) -> None:
        path = self.root / workdir.INHERITED_DIR_NAME
        if path.is_file():
            path.unlink()
        self.trace[-1] = "the inherited file is removed"

    def ev_fresh_clone(self) -> None:
        """A colleague's checkout: only what git carries, into a new folder, for a new state root."""
        self.projects += 1
        clone = self.tmp / "p{}".format(self.projects - 1)
        shared = self.project / workdir.DIR_NAME
        ignored = _gitignore_patterns(shared / ".gitignore")
        for dirpath, dirnames, filenames in os.walk(shared):
            for name in filenames:
                full = Path(dirpath) / name
                rel = full.relative_to(shared).as_posix()
                if name.startswith(".tmp-") or _ignored(rel, ignored) or full.is_symlink():
                    continue
                target = clone / workdir.DIR_NAME / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(os.fspath(full), os.fspath(target))
        if self.folder_ro:
            self.folder_ro = False
        self.inherited_ro = False
        (clone).mkdir(parents=True, exist_ok=True)
        self.models[self.project] = self.model
        self.peer = None
        self.model = Model()
        self.branches, self.branch_at = {}, {}
        self.project = clone
        for rel, data in self.disk().files.items():
            if durable(rel):
                self.model.world_placed(rel, data, "fresh clone")
        self.trace[-1] = "a fresh clone of the repository (new folder {})".format(clone.name)
        self.new_gen(clone)

    def ev_recreate(self) -> None:
        self.trace.pop()
        self.new_gen(self.project)

    # -- hazards: each arms one code step ----------------------------------

    def hazard_step(self, kind: str) -> None:
        steps = {
            "disk-full-inherited": ["render", "force", "draw-images", "cli-render"],
            "disk-full-folder": ["render", "force", "draw-images"],
            "race-document": ["render", "force", "cli-render"],
            "race-asset": ["draw-images"],
            "state-write-fails": ["render", "cli-render", "draw-images"],
        }[kind]
        step = self.rng.choice(steps)
        hazard = _HAZARDS[kind]
        self.trace[-1] = "{} during {}".format(kind, step)
        self.step_label = self.trace[-1]
        self.run_named(step, hazard)

    def run_named(self, step: str, hazard: Optional[Callable[["World"], Any]] = None) -> None:
        if step == "render":
            def run(out: StepOutput) -> None:
                result = workdir.render(self.gen.layout, TEAM)
                out.add_render(result)
                self._spend(self.gen.paths, result.get("inherited") or [])
            self.code_step(self.step_label, run, hazard=hazard)
        elif step in ("force", "cli-render"):
            force = step == "force"

            def run(out: StepOutput) -> None:
                from herdr_team import cmd_knowledge

                code, payload, err = self.run_cli(*(["project", "render"] + (["--force"] if force else [])))
                if isinstance(payload, dict):
                    out.add_render(payload)
                    out.texts.append(cmd_knowledge._render_result_text(payload))
            self.code_step(self.step_label, run, forced=force, hazard=hazard)
        elif step == "draw-images":
            gen = self.gen

            def run(out: StepOutput) -> None:
                renders = C._dir(gen.paths) / C.RENDERS_DIR
                renders.mkdir(parents=True, exist_ok=True)
                gen.images += 1
                picture = renders / "pic-{}.png".format(gen.images)
                picture.write_bytes(_png(10 + gen.images, 7 + gen.index))
                result = C.apply_ops(gen.layout, gen.paths, [{"op": "image", "path": os.fspath(picture),
                                                              "at": "c0r{}".format(30 + gen.images), "intent": "p"}], drawer(gen))
                if not result.get("refused"):
                    gen.last_batch = result.get("batch") or gen.last_batch
                    self.mirror_canvas(out)
            self.code_step(self.step_label, run, hazard=hazard)

    # -- the generator -----------------------------------------------------

    EVENTS: Tuple[Tuple[str, int], ...] = (
        ("render", 9), ("cli_render", 4), ("cli_render_force", 4),
        ("recreate", 4), ("fresh_clone", 1),
        ("pull", 12), ("branch_switch", 4), ("previous_board", 3), ("previous_folder", 2), ("stray_asset", 2),
        ("draw", 6), ("draw_images", 3), ("undo", 2), ("canvas_import", 2),
        ("adopt", 6), ("knowledge_import", 1), ("settle", 4), ("activity", 5), ("rename", 1), ("sync_toggle", 1),
        ("folder_ro", 1), ("inherited_ro", 1), ("inherited_missing", 1), ("inherited_file", 1),
        ("disk_full", 2), ("race_document", 2), ("race_asset", 2), ("state_write_fails", 1),
    )

    def choose(self) -> str:
        events = [(name, weight) for name, weight in self.EVENTS if self.allowed(name)]
        if self.folder_ro:
            events.append(("folder_rw", 8))
        if self.inherited_ro:
            events.append(("inherited_rw", 5))
        if (self.root / workdir.INHERITED_DIR_NAME).is_file() and not self.folder_ro:
            events.append(("inherited_file_removed", 6))
        total = sum(weight for _, weight in events)
        pick = self.rng.uniform(0, total)
        for name, weight in events:
            pick -= weight
            if pick <= 0:
                return name
        return events[-1][0]

    def allowed(self, name: str) -> bool:
        world_writes = ("pull", "branch_switch", "previous_board", "previous_folder", "stray_asset",
                        "inherited_missing", "inherited_file", "fresh_clone", "recreate", "inherited_ro")
        if self.folder_ro and name in world_writes:
            return False  # a read-only checkout receives no pull
        if name in ("folder_ro", "inherited_ro", "disk_full") and os.getuid() == 0:
            return False
        if name == "folder_ro" and self.folder_ro:
            return False
        if name == "inherited_ro" and self.inherited_ro:
            return False
        return True

    def step(self) -> str:
        name = self.choose()
        self.trace.append(name)
        self.step_label = name
        self.dispatch(name)
        return name

    def dispatch(self, name: str) -> None:
        if name == "render":
            self.ev_render()
        elif name == "cli_render":
            self.ev_cli_render()
        elif name == "cli_render_force":
            self.ev_cli_render(force=True)
        elif name == "recreate":
            self.ev_recreate()
        elif name == "fresh_clone":
            self.ev_fresh_clone()
        elif name == "pull":
            self.ev_pull()
        elif name == "branch_switch":
            self.ev_branch_switch()
        elif name == "previous_board":
            self.ev_previous_board()
        elif name == "previous_folder":
            self.ev_previous_folder()
        elif name == "stray_asset":
            self.ev_stray_asset()
        elif name == "draw":
            self.ev_draw()
        elif name == "draw_images":
            self.ev_draw(images=self.rng.choice([1, 2, 3]))
        elif name == "undo":
            self.ev_undo()
        elif name == "canvas_import":
            self.ev_canvas_import()
        elif name == "adopt":
            self.ev_adopt()
        elif name == "knowledge_import":
            self.ev_knowledge_import()
        elif name == "settle":
            self.ev_settle()
        elif name == "activity":
            self.ev_activity()
        elif name == "rename":
            self.ev_rename()
        elif name == "sync_toggle":
            self.ev_sync_toggle()
        elif name == "folder_ro":
            self.set_folder_ro(True)
            self.trace[-1] = "the checkout becomes read-only"
        elif name == "folder_rw":
            self.set_folder_ro(False)
            self.trace[-1] = "the checkout is writable again"
        elif name == "inherited_ro":
            self.set_inherited_ro(True)
            self.trace[-1] = "inherited/ becomes read-only"
        elif name == "inherited_rw":
            self.set_inherited_ro(False)
            self.trace[-1] = "inherited/ is writable again"
        elif name == "inherited_missing":
            self.ev_inherited_missing()
        elif name == "inherited_file":
            self.ev_inherited_file()
        elif name == "inherited_file_removed":
            self.ev_inherited_file_removed()
        elif name == "disk_full":
            self.hazard_step(self.rng.choice(["disk-full-inherited", "disk-full-folder"]))
        elif name == "race_document":
            self.hazard_step("race-document")
        elif name == "race_asset":
            self.hazard_step("race-asset")
        elif name == "state_write_fails":
            self.hazard_step("state-write-fails")
        else:  # pragma: no cover - the table and this dispatch must agree
            raise AssertionError(name)

    def start(self) -> None:
        """A seed opens either on an empty folder or on one a previous team left behind."""
        if self.rng.random() < 0.5:
            self.trace.append("start: a previous team's folder is already there")
            self.root.mkdir(parents=True, exist_ok=True)
            # The member names are the fixture's until the first team exists.
            token = self.token()
            self.world_write("knowledge.md", self.document("knowledge.md", "plausible", token), "previous folder")
            self.world_write("facts.md", self.document("facts.md", "plausible", token), "previous folder")
            for name in ("alpha-reviewer", "alpha-worker"):
                self.world_write("members/{}.md".format(name), self.document(
                    "members/{}.md".format(name), "plausible", token, outside=self.rng.random() < 0.6), "previous folder")
            self.previous_board(self.rng.choice([2, 9, 12]))
        self.new_gen(self.project)

    #: Events that are the world acting, not the mirror: after one of these, whatever the world itself
    #: destroyed while no copy existed is released (git still has it), never blamed on the code.
    WORLD_EVENTS = frozenset({"pull", "branch_switch", "previous_board", "previous_folder", "stray_asset",
                              "inherited_missing", "inherited_file", "inherited_file_removed"})

    def run(self, steps: int) -> None:
        self.start()
        while len(self.trace) < steps:
            name = self.step()
            if name in self.WORLD_EVENTS:
                self.model.release_unrecoverable(self.disk())


_PARSER: List[Any] = []
_BUILD_PARSER = cli.build_parser


def _parser() -> Any:
    """``cli.build_parser``, built once. It is pure construction and argparse parsers are reusable;
    rebuilding it is ~90 ms of gettext lookups per call, which is most of this suite's runtime."""
    if not _PARSER:
        _PARSER.append(_BUILD_PARSER())
    return _PARSER[0]


def _png(width: int, height: int) -> bytes:
    """PNG bytes as far as the canvas reads them (magic and IHDR size); never decoded."""
    magic = b"\x89PNG\r\n\x1a\n"
    return magic + (13).to_bytes(4, "big") + b"IHDR" + width.to_bytes(4, "big") + height.to_bytes(4, "big") \
        + b"\x08\x02\x00\x00\x00" + b"\x00" * 16


def _gitignore_patterns(path: Path) -> List[str]:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return []
    return [line.strip() for line in text.splitlines() if line.strip() and not line.startswith("#")]


def _ignored(rel: str, patterns: Sequence[str]) -> bool:
    for pattern in patterns:
        directory = pattern.endswith("/")
        stem = pattern.rstrip("/")
        parts = rel.split("/")
        width = len(stem.split("/"))
        if directory:
            if any(fnmatch.fnmatch("/".join(parts[:n]), stem) for n in range(1, len(parts))):
                return True
        elif fnmatch.fnmatch("/".join(parts[:width]), stem) and len(parts) == width:
            return True
    return False


# --------------------------------------------------------------------------
# hazards


class _Hazard(object):
    def __init__(self, world: World) -> None:
        self.world = world
        self.patches: List[Any] = []

    def __enter__(self) -> "_Hazard":
        for patcher in self.patches:
            patcher.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        for patcher in reversed(self.patches):
            patcher.stop()


class DiskFull(_Hazard):
    """``ENOSPC`` for every write under ``scope``: the checkout's volume is full, the state root's is not."""

    def __init__(self, world: World, scope: Path) -> None:
        super().__init__(world)
        inner = store.atomic_write

        def full(path: Any, data: bytes, *args: Any, **kwargs: Any) -> None:
            target = Path(path)
            if target == scope or scope in target.parents:
                raise OSError(errno.ENOSPC, "No space left on device", os.fspath(target))
            inner(path, data, *args, **kwargs)

        self.patches.append(mock.patch.object(store, "atomic_write", full))


class StateWriteFails(_Hazard):
    """The final ``document-sync.json`` write fails: the copies are on disk and their records are not."""

    def __init__(self, world: World) -> None:
        super().__init__(world)
        inner = store.atomic_write

        def failing(path: Any, data: bytes, *args: Any, **kwargs: Any) -> None:
            if Path(path).name == "document-sync.json":
                raise OSError(errno.EIO, "Input/output error", os.fspath(path))
            inner(path, data, *args, **kwargs)

        self.patches.append(mock.patch.object(store, "atomic_write", failing))


class RaceDocument(_Hazard):
    """An agent writes the live document the instant a copy of it starts landing in ``inherited/``."""

    def __init__(self, world: World) -> None:
        super().__init__(world)
        inner = store.atomic_write
        done = {"fired": False}

        def racing(path: Any, data: bytes, *args: Any, **kwargs: Any) -> None:
            inner(path, data, *args, **kwargs)
            if done["fired"]:
                return
            rel = world._rel(path)
            if rel is None or not rel.startswith(workdir.INHERITED_DIR_NAME + "/"):
                return
            live = _live_of_copy(rel)
            if live is None or not (world.root / live).is_file():
                return
            done["fired"] = True
            new = world.document(live, "plausible", world.token() + "-raced")
            world.world_write(live, new, "raced over {}".format(live))
            world._raced.setdefault(live, []).append(new)

        self.patches.append(mock.patch.object(store, "atomic_write", racing))


class RaceAsset(_Hazard):
    """A colleague's file lands at an asset's name between the mirror's look and its write."""

    def __init__(self, world: World) -> None:
        super().__init__(world)
        inner = store.read_bytes
        source_dir = C._dir(world.gen.paths) / C.ASSETS_DIR
        done = {"fired": False}

        def racing(path: Any, *args: Any, **kwargs: Any) -> Any:
            found = inner(path, *args, **kwargs)
            target = Path(path)
            if not done["fired"] and target.parent == source_dir:
                rel = "canvas-assets/" + target.name
                if not (world.root / rel).exists():
                    done["fired"] = True
                    data = b"a colleague's file under the same name " + world.token().encode("utf-8")
                    world.world_write(rel, data, "a colleague's file in the asset window")
                    world._raced.setdefault(rel, []).append(data)
            return found

        self.patches.append(mock.patch.object(store, "read_bytes", racing))


def _live_of_copy(rel: str) -> Optional[str]:
    """The live record a copy under ``inherited/`` was taken of, read off its name (``<stem>-<digest16><suffix>``).

    A temporary file the copy is staged in (``.tmp-new-<pid>-<name>``) names the same record, so a race armed on
    the copy's own write fires while the copy is still being made.
    """
    parts = rel.split("/")[1:]
    if not parts:
        return None
    name = re.sub(r"^\.tmp-new-\d+-", "", parts[-1])
    stem, _dot, suffix = name.rpartition("-")[0], "", Path(name).suffix
    if len(parts) == 2 and parts[0] == "members":
        return "members/{}.md".format(stem) if stem else None
    if len(parts) != 1 or not stem:
        return None
    if stem in ("knowledge", "facts") or _FACTS_PART.match(stem + ".md"):
        return stem + ".md"
    return "canvas.json" if stem == "canvas" and suffix == ".json" else None


_HAZARDS: Dict[str, Callable[[World], _Hazard]] = {
    "disk-full-inherited": lambda world: DiskFull(world, world.root / workdir.INHERITED_DIR_NAME),
    "disk-full-folder": lambda world: DiskFull(world, world.project),
    "race-document": RaceDocument,
    "race-asset": RaceAsset,
    "state-write-fails": StateWriteFails,
}


# --------------------------------------------------------------------------
# the tests


def _distinct(violations: Sequence[Dict[str, Any]], limit: int = 12) -> List[str]:
    """One line per distinct failure shape: the check, the kind of path, and the event that did it."""
    seen: Set[Tuple[str, str, str]] = set()
    lines: List[str] = []
    for violation in violations:
        path = re.sub(r"[0-9a-f]{12,}", "<hash>", violation["detail"].split(" ", 1)[0])
        event = re.sub(r"/\S+", "<path>", violation["event"])
        key = (violation["check"], re.sub(r"members/\S+", "members/<name>", path), event.split(" -> ")[0][:40])
        if key in seen:
            continue
        seen.add(key)
        lines.append("step {:>2} [{}] {}: {}".format(violation["step"], violation["event"][:90], violation["check"],
                                                     violation["detail"][:600]))
        if len(lines) >= limit:
            break
    return lines


class FolderInvariantOracleTests(unittest.TestCase):
    """The property itself, over a fixed seed list. Red on the round-4 tree; see the module docstring.

    Each seed runs to the end in collect mode rather than stopping at its first failure, so one
    run names every distinct failure class a seed reaches: a fix that clears the first one shows
    what is behind it instead of hiding it until the next run.
    """

    def test_no_sequence_of_folder_events_loses_or_misreports_a_record(self):
        steps = _steps()
        for seed in _seeds():
            with self.subTest(seed=seed):
                world = World(seed, collect=True)
                try:
                    world.run(steps)
                finally:
                    world.close()
                if world.violations:
                    self.fail("{}\n\ndistinct failures:\n  {}".format(world.report(), "\n  ".join(_distinct(world.violations))))


class ScriptedWorld(World):
    """A world driven by an explicit script instead of the generator, for the known findings."""

    def __init__(self) -> None:
        super().__init__(seed=0, collect=True)

    def label(self, text: str) -> None:
        self.trace.append(text)
        self.step_label = text

    def checks(self) -> List[str]:
        return sorted({v["check"] for v in self.violations})


class KnownFindingScriptTests(unittest.TestCase):
    """The four HIGH findings of round 4, each as the shortest script the oracle fails on.

    Red on the round-4 tree, by design: each asserts the oracle passes, and today it does not. They
    are not separate regression tests with their own idea of the fix -- the same checks judge them as
    judge every random seed, so a fix that satisfies one of these and loses bytes another way still
    fails the property test above.
    """

    def run_script(self, script: Callable[[ScriptedWorld], None]) -> ScriptedWorld:
        world = ScriptedWorld()
        self.addCleanup(world.close)
        script(world)
        return world

    def assertOraclePasses(self, world: ScriptedWorld) -> None:
        if world.violations:
            self.fail("{}\n\ndistinct failures:\n  {}".format(world.report(), "\n  ".join(_distinct(world.violations))))

    def test_data_1_a_fresh_teams_first_mark_keeps_every_picture_a_previous_board_left(self):
        """DATA-1 / COMP-1: ten pictures, one mark, and the bound counts the ten as one record's history."""
        def script(w: ScriptedWorld) -> None:
            w.root.mkdir(parents=True, exist_ok=True)
            w.label("a previous team's board arrives with 10 pictures")
            w.previous_board(10)
            w.new_gen(w.project, mode="manual", rules=None)
            w.label("the new team draws one mark")
            w.ev_draw()
        self.assertOraclePasses(self.run_script(script))

    def test_comp_2_a_file_landing_at_an_asset_name_is_not_overwritten(self):
        """COMP-2: the asset mirror checks absence, then ``FolderGuard.write_bytes`` writes with no look of its own."""
        def script(w: ScriptedWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            w.label("a colleague's file lands at the asset name while the mirror writes it")
            w.run_named("draw-images", _HAZARDS["race-asset"])
        self.assertOraclePasses(self.run_script(script))

    def held_member_then_replaced(self, w: ScriptedWorld) -> str:
        """auto mode: a previous team's member document is held for --adopt, then a pull replaces it."""
        rel = "members/alpha-worker.md"
        w.root.mkdir(parents=True, exist_ok=True)
        w.label("a previous team's members/alpha-worker.md is in the folder")
        w.world_write(rel, w.document(rel, "plausible", "VERSION-ONE"), "previous folder")
        w.new_gen(w.project, mode="auto", rules=None)
        w.label("a pull replaces it with a second version")
        w.world_write(rel, w.document(rel, "plausible", "VERSION-TWO", outside=True), "pull")
        w.model.release_unrecoverable(w.disk())
        w.label("render")
        w.ev_render()
        return rel

    def test_data_2_the_force_a_held_member_line_prints_keeps_what_is_there_now(self):
        """DATA-2: ``hold_kind == "adopt"`` is never looked at again, so ``--force`` destroys the replacement."""
        def script(w: ScriptedWorld) -> None:
            self.held_member_then_replaced(w)
            w.label("project render --force")
            w.ev_cli_render(force=True)
        self.assertOraclePasses(self.run_script(script))

    def test_trus_1_adopt_names_a_copy_of_the_bytes_it_drops(self):
        """TRUS-1: ``instructions <name> --adopt`` names the first version's copy while dropping the second's lines."""
        def script(w: ScriptedWorld) -> None:
            self.held_member_then_replaced(w)
            w.label("instructions alpha-worker --adopt")
            w.ev_adopt("alpha-worker")
        self.assertOraclePasses(self.run_script(script))


class OracleSelfTests(unittest.TestCase):
    """The oracle is not vacuous, and it does not blame the mirror for the world. Green on every tree."""

    def world(self) -> ScriptedWorld:
        world = ScriptedWorld()
        self.addCleanup(world.close)
        world.new_gen(world.project, mode="manual", rules=None)
        return world

    def identical_foreign_facts(self) -> Tuple[ScriptedWorld, bytes]:
        """Two generations share a template, but the second did not write the first one's file."""
        w = self.world()
        data = w.disk().get("facts.md")
        self.assertIsNotNone(data)
        first = w.gen.index
        w.new_gen(w.project, mode="manual", rules=None)
        self.assertEqual(w.disk().get("facts.md"), data)
        self.assertEqual(w.model.version("facts.md", data, "self-test").writers, {first})
        return w, data

    def test_matching_the_new_generations_template_does_not_grant_authorship(self):
        w, data = self.identical_foreign_facts()
        w.code_step("matching template without a write", lambda out: None)
        self.assertNotIn(w.gen.index, w.model.version("facts.md", data, "self-test").writers)
        self.assertEqual(w.violations, [])

    def test_verified_same_byte_operator_write_grants_authorship(self):
        """A real completed write, not a claimed one, records the explicit replacement's author."""
        w, data = self.identical_foreign_facts()

        def copy_then_write(out: StepOutput) -> None:
            copy = w.root / "inherited" / ("facts-" + sha(data)[:16] + ".md")
            copy.parent.mkdir(parents=True, exist_ok=True)
            store.atomic_write(copy, data)
            self.assertEqual(copy.read_bytes(), data)
            store.atomic_write(w.root / "facts.md", data)

        w.code_step("operator reads and replaces facts.md", copy_then_write, reads=["facts.md"])
        self.assertIn(w.gen.index, w.model.version("facts.md", data, "self-test").writers)
        self.assertEqual(w.violations, [])

    def test_an_explicit_read_of_another_path_does_not_attribute_a_same_byte_write(self):
        w, data = self.identical_foreign_facts()
        w.code_step("reads knowledge, rewrites foreign facts", lambda out: store.atomic_write(w.root / "facts.md", data),
                    reads=["knowledge.md"])
        self.assertNotIn(w.gen.index, w.model.version("facts.md", data, "self-test").writers)
        self.assertEqual(w.violations, [])

    def test_a_same_byte_write_needs_matching_final_readback_to_gain_authorship(self):
        w, data = self.identical_foreign_facts()

        def write_then_change(out: StepOutput) -> None:
            copy = w.root / "inherited" / ("facts-" + sha(data)[:16] + ".md")
            copy.parent.mkdir(parents=True, exist_ok=True)
            store.atomic_write(copy, data)
            store.atomic_write(w.root / "facts.md", data)
            (w.root / "facts.md").write_bytes(b"different final bytes\n")

        w.code_step("operator write changed afterward", write_then_change, reads=["facts.md"])
        self.assertNotIn(w.gen.index, w.model.version("facts.md", data, "self-test").writers)
        self.assertEqual(w.violations, [])

    def test_unconfirmed_same_byte_rewrite_still_fails_foreign_rewritten(self):
        """Observing a write must not excuse an autonomous rewrite of an identical foreign file."""
        import test_folder_invariant_never_overwrite as X4

        w = X4.FourthWorld(seed=0, collect=True)
        self.addCleanup(w.close)
        w.new_gen(w.project, mode="manual", rules=None)
        data = w.disk().get("facts.md")
        self.assertIsNotNone(data)
        w.new_gen(w.project, mode="manual", rules=None)
        w.code_step("unconfirmed same-byte rewrite", lambda out: store.atomic_write(w.root / "facts.md", data))
        self.assertNotIn(w.gen.index, w.model.version("facts.md", data, "self-test").writers)
        self.assertIn("FOREIGN_REWRITTEN", {v["check"] for v in w.violations}, w.violations)

    def test_a_write_over_a_record_nobody_copied_is_a_loss(self):
        w = self.world()
        w.label("a pull delivers a foreign facts.md")
        w.world_write("facts.md", w.document("facts.md", "plausible", "ONLY-HERE"), "pull")

        def destroy(out: StepOutput) -> None:
            (w.root / "facts.md").write_bytes(b"overwritten with no copy\n")

        w.label("a writer that takes no copy")
        w.code_step("destroy", destroy)
        self.assertEqual(w.checks(), ["NO_LOSS"], w.violations)

    def test_a_copy_anywhere_under_inherited_satisfies_it(self):
        """On a step the operator ordered (``--force``), a copy anywhere under ``inherited/`` keeps the record."""
        w = self.world()
        data = w.document("facts.md", "plausible", "COPIED")
        w.world_write("facts.md", data, "pull")

        def copy_then_write(out: StepOutput) -> None:
            (w.root / "inherited" / "deep").mkdir(parents=True, exist_ok=True)
            (w.root / "inherited" / "deep" / "x.md").write_bytes(data)
            (w.root / "facts.md").write_bytes(b"ours\n")

        w.code_step("copy, then write (forced)", copy_then_write, forced=True)
        self.assertEqual(w.violations, [])

    def test_the_world_destroying_a_record_is_not_blamed_on_the_mirror(self):
        w = self.world()
        w.label("pull one")
        w.world_write("facts.md", w.document("facts.md", "plausible", "FIRST"), "pull")
        w.label("pull two, over the first")
        w.world_write("facts.md", w.document("facts.md", "plausible", "SECOND"), "pull")
        w.code_step("nothing", lambda out: None)
        self.assertEqual(w.violations, [])

    def test_a_report_naming_other_bytes_than_the_record_in_place_is_a_false_report(self):
        w = self.world()
        w.world_write("facts.md", w.document("facts.md", "plausible", "IN-PLACE"), "pull")
        bogus = w.root / "inherited" / "facts-x-y.md"
        bogus.parent.mkdir(parents=True, exist_ok=True)
        bogus.write_bytes(b"some other document\n")

        def claim(out: StepOutput) -> None:
            out.records.append({"path": os.fspath(w.root / "facts.md"), "kind": "facts", "snapshot": os.fspath(bogus),
                                "where": "project", "held": True, "regenerated": False, "removed": False})

        w.code_step("a held record naming a wrong copy", claim)
        self.assertIn("SNAPSHOT_NOT_CURRENT", w.checks())

    def test_a_printed_repair_that_does_not_run_is_caught(self):
        w = self.world()

        def claim(out: StepOutput) -> None:
            out.texts.append("Repair it with  herdr-synapse project render --no-such-flag , or not.")
            out.texts.append("Read the board with  herdr-synapse canvas import --from <that folder>/canvas.json .")

        w.code_step("two bad repairs", claim)
        self.assertEqual(w.checks(), ["REPAIR_EXIT", "REPAIR_PLACEHOLDER"], w.violations)

    def test_a_printed_repair_runs_in_a_copy_of_the_world_and_leaves_it_untouched(self):
        w = self.world()
        before = w.disk().fingerprint()

        def claim(out: StepOutput) -> None:
            out.texts.append("regenerate it with  herdr-synapse project render --force")

        w.code_step("prints --force", claim)
        self.assertEqual(w.violations, [])
        self.assertEqual(w.commands_run, 1)
        self.assertEqual(w.disk().fingerprint(), before)

    def test_a_copy_nobody_ordered_is_caught(self):
        """NO_AUTOMATIC_COPY: a file appearing under ``inherited/`` on an ordinary step is a violation."""
        world = ScriptedWorld()
        self.addCleanup(world.close)
        world.new_gen(world.project)

        def sneak(out: StepOutput) -> None:
            target = world.root / workdir.INHERITED_DIR_NAME / "knowledge-0123456789abcdef.md"
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(b"a copy no operator ordered")

        world.code_step("an ordinary render that copies", sneak)
        self.assertIn("NO_AUTOMATIC_COPY", world.checks())

    def test_live_of_copy_reads_the_new_copy_names(self):
        self.assertEqual(_live_of_copy("inherited/knowledge-0123456789abcdef.md"), "knowledge.md")
        self.assertEqual(_live_of_copy("inherited/facts-2-0123456789abcdef.md"), "facts-2.md")
        self.assertEqual(_live_of_copy("inherited/members/alpha-worker-0123456789abcdef.md"), "members/alpha-worker.md")
        self.assertEqual(_live_of_copy("inherited/canvas-0123456789abcdef.json"), "canvas.json")
        self.assertEqual(_live_of_copy("inherited/.tmp-new-42-knowledge-0123456789abcdef.md"), "knowledge.md")

    def test_commands_are_read_out_of_every_shape_the_lines_use(self):
        cases = {
            "x; herdr-synapse project render --force overrules the hold": ["herdr-synapse project render --force"],
            "Review it with  herdr-synapse instructions a-b --adopt , or regenerate it with  herdr-synapse project "
            "render --force": ["herdr-synapse instructions a-b --adopt", "herdr-synapse project render --force"],
            "read it;  herdr-synapse project  lists every copy": ["herdr-synapse project"],
            "Replay that board with  herdr-synapse canvas import --from /a/b-c.json --dry-run .":
                ["herdr-synapse canvas import --from /a/b-c.json --dry-run"],
            "p  ->  herdr-synapse project render --force 1 older copy of this record was dropped":
                ["herdr-synapse project render --force"],
            # A file's own words quoted back by knowledge import are not a repair this product hands out.
            "  rules    1 chars would be adopted:\n           | run  herdr-synapse knowledge set  now\n"
            "see it with  herdr-synapse project": ["herdr-synapse project"],
        }
        for text, expected in cases.items():
            self.assertEqual(extract_commands(text), expected, text)

    def test_durable_is_the_owners_d1_classification(self):
        for rel in ("knowledge.md", "facts.md", "canvas.json", "members/alpha-worker.md", "canvas-assets/ab.png"):
            self.assertTrue(durable(rel), rel)
        for rel in ("canvas.md", "board.md", "README.md", "inherited/facts-x.md", "members/inherited/x.md",
                    "artifacts/a.png"):
            self.assertFalse(durable(rel), rel)


# --------------------------------------------------------------------------
# the second vocabulary: events nobody had generated (oracle extension, 2026-10-08)


#: ``facts.md``'s cap in the worlds that shrink it, so a split record is reached with a handful of facts rather than
#: the ~1,100 a real one needs (``facts.add`` is ~27 ms a call). Only the constant changes; the code path is the one a
#: 256 KiB record takes.
SMALL_FACTS_CAP = 6 * 1024
EXT_DEFAULT_SEEDS = 30


def _ext_seeds() -> List[int]:
    raw = os.environ.get("HERDR_SYNAPSE_ORACLE_EXT_SEEDS", "").strip()
    if not raw:
        return list(range(EXT_DEFAULT_SEEDS))
    if "," in raw:
        return [int(part) for part in raw.split(",") if part.strip()]
    if "-" in raw:
        low, high = raw.split("-", 1)
        return list(range(int(low), int(high)))
    return list(range(int(raw)))


class _StubApi(object):
    """``Roster.dissolve`` clears tokens and labels; nothing here needs a server to answer."""

    def request(self, method: str, params: Any = None) -> Dict[str, Any]:
        return {}


def _strings(value: Any) -> Iterator[str]:
    """Every string anywhere in a JSON payload: a printed command can sit in any field.

    Except a file's own words quoted back: ``knowledge import`` puts the Rules it would adopt in ``rules.text`` (and
    the team's current ones in ``rules.current``) so the operator reads them before they become operator authority.
    That text is whatever the folder held -- a placeholder naming ``herdr-synapse knowledge set``, or bait -- and not
    a repair this product hands the operator.
    """
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        quoted = ("text", "current") if "chars" in value else ()
        for key, item in value.items():
            if key in quoted:
                continue
            yield from _strings(item)
    elif isinstance(value, list):
        for item in value:
            yield from _strings(item)


#: A stamped copy's stem (``facts-20261008T103414Z-06b64ccf``): a copy under ``inherited/``, never a member.
_STAMPED_COPY = r"-[0-9a-f]{16}$"
#: File stems a ``<name>`` placeholder is never filled with: they are records, not members.
_NOT_MEMBERS = {"knowledge", "facts", "canvas", "board", "README", "readme"}


class PeerRenders(_Hazard):
    """A second team on the same folder renders in the middle of this team's write.

    Two state roots (two sessions, two machines on one shared checkout, a restarted notifier next to the old one)
    each hold their own ``document_lock``, so nothing serializes them. The peer's whole render and canvas mirror run
    the instant this team's first write into the folder lands -- which is usually the verbatim copy, i.e. between the
    copy and the write it protects. The peer's own NO LOSS is checked on the spot; what it wrote is attributed to it.
    """

    def __init__(self, world: "ExtendedWorld") -> None:
        super().__init__(world)
        inner = store.atomic_write
        state = {"fired": False, "inside": False}
        peer = world.peer

        def racing(path: Any, data: bytes, *args: Any, **kwargs: Any) -> None:
            inner(path, data, *args, **kwargs)
            if state["fired"] or state["inside"] or peer is None or world._rel(path) is None:
                return
            state["fired"], state["inside"] = True, True
            before = world.disk()
            try:
                for call in (workdir.render, workdir.render_canvas_snapshot):
                    try:
                        result = call(peer.layout, TEAM)
                        DS.mark_reported(peer.paths, result.get("inherited") or [])
                    except Exception:  # noqa: BLE001 - the peer is the world here; its crash is its own
                        world.fail("CRASH", "the peer's mid-write render raised:\n" + traceback.format_exc(limit=4))
            finally:
                state["inside"] = False
            after = world.disk()
            for version in world.model.obligations(peer.index):
                if before.recoverable(version) and not after.recoverable(version):
                    world.fail("NO_LOSS", "{} ({} bytes, sha {}, from {}) was destroyed with no copy by the second team "
                               "rendering in the middle of this team's write".format(
                                   version.rel, len(version.data), version.digest[:12], version.origin))
                    version.released = True
            for rel, data in after.files.items():
                if durable(rel) and before.get(rel) != data:
                    world.model.code_wrote(rel, data, peer.index, "peer mid-write")
                    world._raced.setdefault(rel, []).append(data)

        self.patches.append(mock.patch.object(store, "atomic_write", racing))


class ExtendedWorld(World):
    """``World`` plus the events the first vocabulary never generated.

    Two teams rendering into one folder (in turn, and one in the middle of the other's write); the notifier's own
    tick (``Daemon._refresh_workdir`` and ``snapshot_canvas``) instead of direct render calls, with every line it
    boards checked like any other printed repair; symlinks inside the folder that reach files outside it; a pull
    under a case variant of a record's name on a case-folding filesystem; renames into a stranger's name, back to an
    old name and to a 32-character name; a member leaving; ``facts.md`` split into parts and parts left by a longer
    record; imports over imports and from another folder; ``project set`` to another folder and back, and a folder
    moved; ``dissolve`` and recreate in the same session, any number of times; a previous board whose
    ``canvas.json`` names pictures that never arrived; and ``herdr-synapse project``'s own text.

    Two checks the first vocabulary had no use for: OUTSIDE_DESTROYED (a file outside the team folder, reached
    through a symlink in it, changed or deleted by a step) and PAYLOAD_ASSET_MISSING (a ``canvas.json`` this team
    just wrote names a picture that is not in ``canvas-assets/`` and that the payload does not list as omitted).
    A printed ``<name>`` is filled with each member-document name the same line mentions before it is run, so a
    line is judged on whether any reading of it works rather than on its template.
    """

    EXT_EVENTS: Tuple[Tuple[str, int], ...] = (
        ("daemon_tick", 6), ("peer_join", 2), ("peer_step", 7), ("peer_race", 3),
        ("symlink", 3), ("unsymlink", 2), ("pull_case", 3), ("rename_ext", 2), ("member_leaves", 1),
        ("pull_part", 3), ("previous_split_facts", 1), ("facts_burst", 2),
        ("knowledge_import_other", 1), ("knowledge_import_text", 2), ("import_twice", 1),
        ("project_set_other", 2), ("move_folder", 1), ("dissolve", 2),
        ("previous_board_missing", 2), ("project_status", 3),
    )
    EVENTS = World.EVENTS + EXT_EVENTS
    WORLD_EVENTS = World.WORLD_EVENTS | frozenset({
        "symlink", "unsymlink", "pull_case", "pull_part", "previous_split_facts", "previous_board_missing"})

    def __init__(self, seed: int, collect: bool = False) -> None:
        super().__init__(seed, collect)
        self.rng = random.Random(seed * 7919 + 17)
        self.daemons: Dict[int, Tuple[Any, Any]] = {}
        self.small_facts = self.rng.random() < 0.5
        self.dissolves = 0
        self.patchers: List[Any] = []
        if self.small_facts:
            real = workdir.facts_parts

            def small(team_name: str, rows: List[Dict[str, Any]], cap: int = SMALL_FACTS_CAP) -> List[str]:
                return real(team_name, rows, min(cap, SMALL_FACTS_CAP))

            self.patchers = [mock.patch.object(workdir, "facts_parts", small),
                             mock.patch.object(workdir, "MAX_FACTS_BYTES", SMALL_FACTS_CAP)]
            for patcher in self.patchers:
                patcher.start()
            workdir._FACT_VIEWS.clear()

    def close(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.patchers = []
        workdir._FACT_VIEWS.clear()
        super().close()

    # -- documents ---------------------------------------------------------

    def document(self, rel: str, flavor: str, token: str, outside: bool = True) -> bytes:
        if _FACTS_PART.match(rel):
            marker = workdir.marker_for(self.root / rel)
            body = "\n".join([marker, "# alpha facts, part {}".format(rel[6:-3]), "",
                              "## F-9  the queue depth is {}".format(token), "", "- by: beta-worker (claude)", ""])
            if flavor == "markerless":
                return "# hand written, no marker\n\n{}\n".format(token).encode("utf-8")
            if flavor == "conflict":
                return ("<<<<<<< HEAD\n" + body + "=======\n" + body.replace(token, token + "-theirs")
                        + ">>>>>>> branch\n").encode("utf-8")
            if flavor == "invalid-utf8":
                return body.encode("utf-8") + b"\xff\xfe " + token.encode("utf-8") + b"\n"
            return body.encode("utf-8")
        return super().document(rel, flavor, token, outside)

    # -- what a step printed -----------------------------------------------

    def _printed(self, output: StepOutput) -> List[str]:
        texts = list(output.texts)
        for record in output.records:
            texts.append(DS.describe_inherited(record, style="board"))
            texts.append(DS.describe_inherited(record, style="list"))
            if record.get("command"):
                texts.append(str(record["command"]))
        commands: List[str] = []
        for text in texts:
            for command in extract_commands(text):
                # Markdown-quoted commands (`herdr-synapse whiteboard clear`) end at the closing backtick.
                command = command.split("`", 1)[0].strip()
                # ... and a command named inside a sentence ("herdr-synapse project says why") ends at its verb.
                command = re.split(r" (?:says|tells|names|prints|shows|reports|holds|keeps)\b", command, maxsplit=1)[0].strip()
                if not command or command == "herdr-synapse":
                    continue
                found = [command]
                if "<name>" in command:
                    named = re.findall(r"members/([A-Za-z][A-Za-z0-9_-]*)\.md\b", text) + \
                        re.findall(r"(?<![\w/.-])([A-Za-z][A-Za-z0-9_-]*)\.md\b", text)
                    stems = [s for s in dict.fromkeys(named) if s not in _NOT_MEMBERS
                             and not re.search(_STAMPED_COPY, s) and not _FACTS_PART.match(s + ".md")]
                    if stems:
                        found = [command.replace("<name>", stem) for stem in stems]
                for one in found:
                    if one not in commands:
                        commands.append(one)
        return commands

    def mirror_canvas(self, out: StepOutput) -> None:
        result = workdir.render_canvas_snapshot(self.gen.layout, TEAM)
        out.add_canvas(result)
        out.texts.extend(DS.describe_inherited(r) for r in result.get("inherited") or [])
        if result.get("reason"):
            out.texts.append(str(result["reason"]))
        self._spend(self.gen.paths, result.get("inherited") or [])

    def run_cli_text(self, *argv: str) -> Tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "build_parser", _parser):
            code = cli.main(list(argv) + ["--team", TEAM], env=self.gen.ts.env, stdout=out, stderr=err, api=FakeApi())
        return code, out.getvalue(), err.getvalue()

    # -- the extra claim -----------------------------------------------------

    def _check_claims(self, output: StepOutput, pre: Disk, post: Disk, raced: Dict[str, List[bytes]],
                      written: Dict[str, bytes], gen: int) -> None:
        super()._check_claims(output, pre, post, raced, written, gen)
        data = post.get("canvas.json")
        if data is None or data == pre.get("canvas.json"):
            return
        version = self.model.versions.get(("canvas.json", sha(data)))
        if version is None or gen not in version.writers:
            return
        try:
            payload = json.loads(data.decode("utf-8"))
        except ValueError:
            return
        if not isinstance(payload, dict):
            return
        from herdr_team import canvas_import

        excused = {str(item.get("name")) for key in ("assets_omitted", "assets_conflict")
                   for item in payload.get(key) or [] if isinstance(item, dict)}
        for name in canvas_import.asset_names(payload):
            if name in excused:
                continue
            there = post.get("canvas-assets/" + name)
            if there is None:
                # Reached through a symlinked ``canvas-assets/`` is still reachable; OUTSIDE_DESTROYED judges the link.
                through = self.root / "canvas-assets" / name
                there = through.read_bytes() if through.is_file() else None
            if there is None or sha(there)[:32] != name.split(".", 1)[0]:
                self.fail("PAYLOAD_ASSET_MISSING", "the canvas.json this step wrote names {} and does not list it as "
                          "omitted, and canvas-assets/{} is {}".format(name, name, "missing" if there is None else "other bytes"))

    # -- the notifier's own tick --------------------------------------------

    def _daemon(self, gen: Gen) -> Tuple[Any, Any]:
        found = self.daemons.get(gen.index)
        if found is None:
            from test_daemon import FakeClock, make_daemon

            clock = FakeClock(self.clock)
            daemon, _api, _clock = make_daemon(gen.ts, clock=clock)
            daemon.scan_teams(force=True)
            found = self.daemons[gen.index] = (daemon, clock)
        return found

    def ev_daemon_tick(self) -> None:
        gen = self.gen

        def run(out: StepOutput) -> None:
            real_render, real_canvas, real_scan = workdir.render, workdir.render_canvas_snapshot, DS.scan

            def render(layout: Any, team_name: str, force: bool = False) -> Dict[str, Any]:
                result = real_render(layout, team_name, force)
                out.add_render(result)
                return result

            def canvas(layout: Any, team_name: str, force: bool = False) -> Dict[str, Any]:
                result = real_canvas(layout, team_name, force)
                out.add_canvas(result)
                return result

            def scan(layout: Any, team_name: str, now: Optional[float] = None) -> Dict[str, Any]:
                result = real_scan(layout, team_name, now)
                out.records.extend(r for r in result.get("inherited") or [] if isinstance(r, dict))
                return result

            board = store.BoardStore(gen.ts.team)
            seen = len(board.read())
            with mock.patch.object(workdir, "render", render), \
                    mock.patch.object(workdir, "render_canvas_snapshot", canvas), mock.patch.object(DS, "scan", scan):
                daemon, clock = self._daemon(gen)
                team = daemon.teams.get(TEAM)
                if team is None:
                    daemon.scan_teams(force=True)
                    team = daemon.teams.get(TEAM)
                if team is not None:
                    clock.advance(3.0)
                    team.document_scan_at = None
                    daemon._reload_roster(team)
                    team.canvas_snapshot_ms = None
                    daemon.snapshot_canvas(team, daemon.now_ms())
            for record in board.read()[seen:]:
                out.texts.append(str(record.get("text") or ""))
        rels = ["knowledge.md"] + ["members/{}.md".format(m) for m in gen.members()]
        self.code_step("notifier tick (Daemon._refresh_workdir + snapshot_canvas)", run, adoption=rels)

    # -- a second team on the same folder --------------------------------------

    def ev_peer_join(self) -> None:
        if self.peer is not None:
            self.trace[-1] = "peer_join: a second team is already there; it renders"
            with self.as_gen(self.peer):
                self.ev_render()
            return
        index = self.take_index()
        mode = self.rng.choice(["auto", "auto", "manual", None])
        rules = None if self.rng.random() < 0.5 else "the second team's rules: {}".format(self.token())
        peer = Gen(index, self.project, mode, rules)
        self.peer = peer
        self.peers.append(peer)
        label = "a second team (gen {}, {}, {}) is pointed at the same folder".format(
            index, mode or "default", "rules" if rules else "no rules")
        self.trace[-1] = label

        def create(out: StepOutput) -> None:
            code, payload, err = self.run_cli("project", "set", os.fspath(self.project))
            if code != 0:
                self.fail("CREATE_EXIT", err.strip()[:300])
            out.add_render(payload if isinstance(payload, dict) else None)
            self._spend(self.gen.paths, out.records)

        with self.as_gen(peer):
            self.code_step(label, create)

    def ev_peer_step(self) -> None:
        if self.peer is None:
            self.ev_peer_join()
            return
        what = self.rng.choice(["render", "cli_render", "cli_render_force", "draw", "draw_images", "daemon_tick",
                                "adopt", "settle", "activity", "undo"])
        self.trace[-1] = "the second team: {}".format(what)
        with self.as_gen(self.peer):
            if what == "render":
                self.ev_render()
            elif what == "cli_render":
                self.ev_cli_render()
            elif what == "cli_render_force":
                self.ev_cli_render(force=True)
            elif what == "draw":
                self.ev_draw()
            elif what == "draw_images":
                self.ev_draw(images=self.rng.choice([1, 2]))
            elif what == "daemon_tick":
                self.ev_daemon_tick()
            elif what == "adopt":
                self.ev_adopt()
            elif what == "settle":
                self.ev_settle()
            elif what == "undo":
                self.ev_undo()
            else:
                self.ev_activity()
                self.trace[-1] = "the second team: " + self.trace[-1]

    def ev_peer_race(self) -> None:
        if self.peer is None:
            self.ev_peer_join()
            return
        step = self.rng.choice(["render", "cli-render", "force", "draw-images"])
        self.trace[-1] = "the second team renders in the middle of this team's {}".format(step)
        self.step_label = self.trace[-1]
        self.run_named(step, PeerRenders)

    # -- symlinks --------------------------------------------------------------

    def ev_symlink(self) -> None:
        kind = self.rng.choice(["assets-outside", "assets-repo", "member", "knowledge", "canvas-json", "inherited",
                                "facts"])
        if kind.startswith("assets"):
            self.symlink_assets(beyond_repository=kind == "assets-outside")
        elif kind == "inherited":
            self.symlink_inherited()
        else:
            rel = {"knowledge": "knowledge.md", "canvas-json": "canvas.json", "facts": "facts.md"}.get(kind) or \
                "members/{}.md".format(self.rng.choice(self.gen.members()))
            self.symlink_record(rel)

    def symlink_assets(self, beyond_repository: bool) -> None:
        """``mv canvas-assets <elsewhere> && ln -s <elsewhere> canvas-assets``, elsewhere holding the user's own files."""
        n = self.token()
        base = (self.tmp / "outside-{}".format(n)) if beyond_repository else (self.project / "src-{}".format(n))
        if beyond_repository:
            self.outside_roots.append(base)
        self.place_outside(base / "holiday-{}.jpg".format(n), b"\xff\xd8 the user's own photo " + n.encode())
        self.place_outside(base / "main-{}.py".format(n), b"print('the repository's own source')  # " + n.encode())
        assets = self.root / "canvas-assets"
        if assets.is_symlink():
            assets.unlink()
        elif assets.is_dir():
            for entry in sorted(assets.iterdir()):
                if entry.is_file() and not entry.is_symlink():
                    self.place_outside(base / entry.name, entry.read_bytes())
            _rmtree(assets)
        assets.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(os.fspath(base), os.fspath(assets))
        for entry in sorted(base.iterdir()):
            if entry.is_file():
                self.model.world_placed("canvas-assets/" + entry.name, entry.read_bytes(), "reached through the symlink")
        self.trace[-1] = "canvas-assets/ becomes a symlink to a directory {} the team folder".format(
            "outside the repository, beyond" if beyond_repository else "in the repository, outside")

    def symlink_inherited(self) -> None:
        n = self.token()
        base = self.tmp / "outside-{}".format(n)
        self.outside_roots.append(base)
        self.place_outside(base / "keep-{}.txt".format(n), b"a file the user keeps here " + n.encode())
        self.set_inherited_ro(False)
        self.world_remove(workdir.INHERITED_DIR_NAME)
        inherited = self.root / workdir.INHERITED_DIR_NAME
        inherited.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(os.fspath(base), os.fspath(inherited))
        self.trace[-1] = "inherited/ becomes a symlink to a directory outside the repository"

    def symlink_record(self, rel: str) -> None:
        n = self.token()
        target = self.project / "NOTES-{}.md".format(n)
        self.place_outside(target, "the operator's own notes, not a record: {}\n".format(n).encode())
        path = self.root / rel
        if os.path.lexists(os.fspath(path)):
            self.world_remove(rel)
        path.parent.mkdir(parents=True, exist_ok=True)
        os.symlink(os.fspath(target), os.fspath(path))
        self.trace[-1] = "{} becomes a symlink to a file in the repository outside the team folder".format(rel)

    def ev_unsymlink(self) -> None:
        removed = []
        for dirpath, dirnames, filenames in os.walk(self.root):
            for name in dirnames + filenames:
                full = Path(dirpath) / name
                if full.is_symlink():
                    full.unlink()
                    removed.append(full.relative_to(self.root).as_posix())
        self.trace[-1] = "git checkout replaces the symlinks ({})".format(", ".join(removed) or "none")

    # -- case-folding names ----------------------------------------------------

    def ev_pull_case(self) -> None:
        member = self.rng.choice(self.gen.members())
        rel, variant = self.rng.choice([
            ("knowledge.md", "Knowledge.md"), ("facts.md", "FACTS.md"), ("canvas.json", "Canvas.json"),
            ("members/{}.md".format(member), "members/{}.md".format(member.title())),
            ("members/{}.md".format(member), "Members/{}.md".format(member)),
        ])
        flavor = self.rng.choice(["plausible", "plausible", "markerless", "conflict"])
        self.world_write(variant, self.document(rel, flavor, self.token()), "pull {} as {}".format(flavor, variant))
        self.trace[-1] = "git pull delivers {} ({}) on a case-folding filesystem".format(variant, flavor)

    # -- members -------------------------------------------------------------------

    def ev_rename_ext(self) -> None:
        members = self.gen.members()
        name = self.rng.choice(members)
        doc = roster.load_team(self.gen.paths)
        member = doc.find(name)
        previous = [p.get("name") for p in (member.previous_names if member is not None else []) or []
                    if isinstance(p, dict) and isinstance(p.get("name"), str) and p.get("name") not in members]
        self.counter += 1
        long_name = ("alpha-l{}-".format(self.counter) + "q" * 32)[:paths_max_member()]
        options = [long_name] + ([STRANGER_MEMBER] if STRANGER_MEMBER not in members else []) + previous[-1:]
        new = self.rng.choice(options)

        def rename(team: roster.Team) -> None:
            found = team.find(name)
            if found is not None:
                roster.apply_adoption(found, new)

        roster.update_team(self.gen.paths, rename)
        self.trace[-1] = "rename member {} -> {}{}".format(
            name, new, " (back to an old name)" if new in previous else " (a stranger's name)" if new == STRANGER_MEMBER
            else " (32 characters)")

    def ev_member_leaves(self) -> None:
        members = self.gen.active_members()
        if len(members) < 2:
            self.trace[-1] = "member_leaves: only one member, nobody leaves"
            return
        name = self.rng.choice(members)

        def leave(team: roster.Team) -> None:
            found = team.find(name)
            if found is not None:
                found.status = "left"

        roster.update_team(self.gen.paths, leave)
        self.trace[-1] = "member {} leaves".format(name)

    # -- facts.md in parts -----------------------------------------------------------

    def ev_pull_part(self) -> None:
        rel = self.rng.choice(["facts-2.md", "facts-3.md"])
        flavor = self.rng.choice(["plausible", "plausible", "markerless", "conflict", "invalid-utf8"])
        self.world_write(rel, self.document(rel, flavor, self.token()), "pull {} {}".format(flavor, rel))
        self.trace[-1] = "git pull delivers {} ({}), a part of a longer record".format(rel, flavor)

    def ev_previous_split_facts(self) -> None:
        token = self.token()
        self.world_write("facts.md", self.document("facts.md", "plausible", token), "previous split record")
        for rel in ("facts-2.md", "facts-3.md"):
            self.world_write(rel, self.document(rel, "plausible", token), "previous split record")
        self.trace[-1] = "a previous team's facts.md arrives in three parts"

    def ev_facts_burst(self) -> None:
        count = self.rng.randrange(3, 9)
        for _ in range(count):
            token = self.token()
            F.add(self.gen.paths, {"statement": "fact {} ".format(token) + "w" * 360, "about": "oracle",
                                   "attribute": token, "by": "alpha-worker", "by_kind": "claude"}, mode="add")
        self.trace[-1] = "the team records {} long facts{}".format(count, " (past the shrunk cap)" if self.small_facts else "")

    # -- imports ----------------------------------------------------------------------

    def _other_folders(self) -> List[Path]:
        out = []
        for entry in sorted(self.tmp.iterdir()):
            folder = entry / workdir.DIR_NAME / TEAM
            if entry != self.project and folder.is_dir() and not entry.name.startswith("sandbox"):
                out.append(folder)
        return out

    def ev_knowledge_import_other(self) -> None:
        others = self._other_folders()
        if not others:
            self.trace[-1] = "knowledge import --from <another folder>: there is none yet"
            return
        source = self.rng.choice(others)

        def run(out: StepOutput) -> None:
            code, payload, err = self.run_cli("knowledge", "import", "--from", os.fspath(source), "--yes")
            out.texts.extend(_strings(payload))
        self.code_step("knowledge import --from <another folder> --yes", run)

    def ev_knowledge_import_text(self) -> None:
        def run(out: StepOutput) -> None:
            code, text, err = self.run_cli_text("knowledge", "import", "--from", os.fspath(self.root), "--yes")
            out.texts.extend([text, err])
        self.code_step("knowledge import --from <this folder> (text)", run,
                       reads=["knowledge.md", "facts.md", "canvas.json"])

    def ev_import_twice(self) -> None:
        def run(out: StepOutput) -> None:
            for _ in range(2):
                code, payload, err = self.run_cli("knowledge", "import", "--from", os.fspath(self.root), "--yes")
                out.texts.extend(_strings(payload))
            for _ in range(2):
                code, payload, err = self.run_cli("canvas", "import", "--from", os.fspath(self.root))
                out.texts.extend(_strings(payload))
            self.mirror_canvas(out)
        self.code_step("knowledge import twice, canvas import twice (+ mirror)", run,
                       reads=["knowledge.md", "facts.md", "canvas.json"])

    # -- folders that move ------------------------------------------------------------

    def _switch_folder(self, target: Path) -> None:
        self.models[self.project] = self.model
        self.peer = None
        self.branches, self.branch_at = {}, {}
        self.project = target
        found = self.models.get(target)
        if found is None:
            found = Model()
            self.model = found
            for rel, data in self.disk().files.items():
                if durable(rel):
                    found.world_placed(rel, data, "found in the folder moved to")
        self.model = found

    def ev_project_set_other(self) -> None:
        others = [entry for entry in sorted(self.tmp.iterdir())
                  if entry.is_dir() and entry != self.project and re.match(r"^[pqm][0-9]", entry.name)]
        self.counter += 1
        fresh = self.tmp / "q{}".format(self.counter)
        target = self.rng.choice(others + [fresh])
        target.mkdir(parents=True, exist_ok=True)
        self.trace[-1] = "project set to {} folder ({})".format("a new" if target == fresh else "another existing",
                                                                 target.name)
        self.step_label = self.trace[-1]
        self._switch_folder(target)

        def run(out: StepOutput) -> None:
            code, payload, err = self.run_cli("project", "set", os.fspath(target))
            if code != 0:
                self.fail("CREATE_EXIT", err.strip()[:300])
            out.add_render(payload if isinstance(payload, dict) else None)
            self._spend(self.gen.paths, out.records)
        self.code_step(self.step_label, run)

    def ev_move_folder(self) -> None:
        self.counter += 1
        old, new = self.project, self.tmp / "m{}".format(self.counter)
        os.rename(os.fspath(old), os.fspath(new))
        prefix = os.fspath(old) + os.sep
        self.outside = {(os.fspath(new) + os.sep + k[len(prefix):]) if k.startswith(prefix) else k: v
                        for k, v in self.outside.items()}
        model = self.model
        self.models.pop(old, None)
        self.peer = None
        self.project = new
        self.models[new] = model
        # An absolute symlink into the old path dangles now: what it reached left with the move.
        model.release_unrecoverable(self.disk())
        self.trace[-1] = "mv {} {} && herdr-synapse project set {}".format(old.name, new.name, new.name)
        self.step_label = self.trace[-1]

        def run(out: StepOutput) -> None:
            code, payload, err = self.run_cli("project", "set", os.fspath(new))
            if code != 0:
                self.fail("CREATE_EXIT", err.strip()[:300])
            out.add_render(payload if isinstance(payload, dict) else None)
            self._spend(self.gen.paths, out.records)
        self.code_step(self.step_label, run)

    def ev_dissolve(self) -> None:
        gen = self.gens[-1]
        self.dissolves += 1
        stamp = "20261008T{:02d}{:02d}{:02d}Z".format(10 + self.dissolves // 3600, (self.dissolves // 60) % 60,
                                                         self.dissolves % 60)
        self.trace[-1] = "dissolve #{} in this session, then create the team again".format(self.dissolves)
        self.step_label = self.trace[-1]

        before = self.disk().fingerprint()
        outside = dict(self.outside)
        try:
            roster.Roster(gen.layout, TEAM).dissolve(_StubApi(), timestamp=stamp)
        except Exception:  # noqa: BLE001 - a dissolve that raises is the finding
            self.fail("CRASH", "dissolve raised:\n" + traceback.format_exc(limit=4))
        # Dissolving moves the team's state directory into the session archive and never touches the folder.
        if self.disk().fingerprint() != before:
            self.fail("DISSOLVE_TOUCHED_FOLDER", "dissolve changed the team folder")
        self._check_outside()
        self.outside = outside if not self.violations else self.outside
        self.daemons.pop(gen.index, None)
        self.new_gen(self.project, ts=gen.ts)

    # -- boards that are not whole ------------------------------------------------------

    def ev_previous_board_missing(self) -> None:
        count = self.rng.choice([1, 2, 3, 9])
        names = []
        delivered = 0
        for n in range(count):
            name, data = self.asset(self.token())
            if n == 0 or self.rng.random() < 0.5:
                names.append(name)  # named by the board, never delivered
                continue
            self.world_write("canvas-assets/" + name, data, "previous board picture")
            names.append(name)
            delivered += 1
        payload = {"payload": 1, "team": "beta", "scene_version": 9,
                   "elements": [{"id": "E-{}".format(n + 1), "type": "image", "asset": name}
                                for n, name in enumerate(names)]}
        encoded = json.dumps(payload, separators=(",", ":"))
        self.world_write("canvas.json", (workdir.MARKER_JSON + "\n" + encoded[1:] + "\n").encode("utf-8"),
                         "previous board payload, pictures missing")
        self.trace[-1] = "a previous team's board arrives naming {} picture(s), {} of them delivered".format(count, delivered)

    def ev_project_status(self) -> None:
        def run(out: StepOutput) -> None:
            code, text, err = self.run_cli_text("project")
            if code != 0:
                self.fail("STATUS_EXIT", "herdr-synapse project exited {}: {}".format(code, err.strip()[:300]))
            out.texts.append(text)
            code, payload, err = self.run_cli("project")
            out.texts.extend(_strings(payload))
        self.code_step("herdr-synapse project (text and --json)", run)

    # -- the generator -------------------------------------------------------------------

    def allowed(self, name: str) -> bool:
        if not super().allowed(name):
            return False
        moving = ("symlink", "unsymlink", "pull_case", "pull_part", "previous_split_facts", "previous_board_missing",
                  "project_set_other", "move_folder", "dissolve", "peer_join")
        if self.folder_ro and name in moving:
            return False
        if name in ("project_set_other", "move_folder") and self.inherited_ro:
            return False
        if name == "pull_case" and not CASE_INSENSITIVE:
            return False
        if name == "inherited_ro" and (self.root / workdir.INHERITED_DIR_NAME).is_symlink():
            return False
        if name == "facts_burst" and not self.small_facts:
            return self.rng.random() < 0.2
        return True

    def dispatch(self, name: str) -> None:
        handler = getattr(self, "ev_" + name, None) if name in {n for n, _ in self.EXT_EVENTS} else None
        if handler is None:
            super().dispatch(name)
        else:
            handler()


def paths_max_member() -> int:
    from herdr_team import paths as _paths

    return _paths.MAX_MEMBER_CHARS


class FolderInvariantExtendedOracleTests(unittest.TestCase):
    """The same invariant over the second vocabulary (``ExtendedWorld``). Widen with ``HERDR_SYNAPSE_ORACLE_EXT_SEEDS``."""

    def test_no_sequence_of_the_new_folder_events_loses_or_misreports_a_record(self):
        steps = _steps()
        for seed in _ext_seeds():
            with self.subTest(seed=seed):
                world = ExtendedWorld(seed, collect=True)
                try:
                    world.run(steps)
                finally:
                    world.close()
                if world.violations:
                    self.fail("{}\n\ndistinct failures:\n  {}".format(world.report(), "\n  ".join(_distinct(world.violations))))


class ScriptedExtendedWorld(ExtendedWorld):
    """An ``ExtendedWorld`` driven by an explicit script, at the real ``facts.md`` cap."""

    def __init__(self) -> None:
        super().__init__(seed=0, collect=True)
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.patchers, self.small_facts = [], False

    def label(self, text: str) -> None:
        self.trace.append(text)
        self.step_label = text

    def checks(self) -> List[str]:
        return sorted({v["check"] for v in self.violations})


class ExtensionFindingScriptTests(unittest.TestCase):
    """What the second vocabulary found, each as the shortest script the oracle fails on.

    Red on the tree they were found against (feat/canvas-v2, d13e58c5 plus the inheritance diff after the
    fix stage, 2026-10-08), by design and not as ``expectedFailure``, for the reason the module docstring
    gives. The property test above is what judges a fix; these name the shapes.
    """

    def run_script(self, script: Callable[[ScriptedExtendedWorld], None]) -> ScriptedExtendedWorld:
        world = ScriptedExtendedWorld()
        self.addCleanup(world.close)
        script(world)
        return world

    def assertOraclePasses(self, world: ScriptedExtendedWorld) -> None:
        if world.violations:
            self.fail("{}\n\ndistinct failures:\n  {}".format(world.report(), "\n  ".join(_distinct(world.violations))))

    def test_x1_a_symlinked_canvas_assets_directory_never_deletes_the_files_it_points_at(self):
        """The prune lists ``canvas-assets/`` through the link and unlinks every unnamed file in the target."""
        def script(w: ScriptedExtendedWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            w.label("canvas-assets/ becomes a symlink")
            w.symlink_assets(beyond_repository=False)
            w.label("draw")
            w.ev_draw()
        self.assertOraclePasses(self.run_script(script))

    def test_x2_the_notifiers_foreign_member_line_prints_a_repair_that_runs(self):
        """``instructions <name> --adopt`` for a name this team does not use exits 1 whatever name is filled in."""
        def script(w: ScriptedExtendedWorld) -> None:
            w.new_gen(w.project, mode="auto", rules=None)
            rel = "members/{}.md".format(STRANGER_MEMBER)
            w.label("git pull delivers a previous team's member document for a name this team does not use")
            w.world_write(rel, w.document(rel, "plausible", "STRANGER"), "pull")
            w.label("notifier tick")
            w.ev_daemon_tick()
        self.assertOraclePasses(self.run_script(script))

    def test_x3_the_notifier_survives_a_member_document_with_one_invalid_byte(self):
        """``Daemon._announce_edits`` reads ``awaiting_adopt`` with ``read_text`` and catches only ``OSError``."""
        def script(w: ScriptedExtendedWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            rel = "members/alpha-worker.md"
            w.label("git pull delivers members/alpha-worker.md with one invalid byte")
            w.world_write(rel, w.document(rel, "invalid-utf8", "BYTE"), "pull")
            w.label("notifier tick")
            w.ev_daemon_tick()
        self.assertOraclePasses(self.run_script(script))

    def test_x4_the_inherited_canvas_line_prints_a_settings_command_that_runs(self):
        """``canvas settings human_edits live`` exits 2; the flag is ``--human-edits live``."""
        def script(w: ScriptedExtendedWorld) -> None:
            w.root.mkdir(parents=True, exist_ok=True)
            w.label("a previous team's board arrives with 2 pictures")
            w.previous_board(2)
            w.new_gen(w.project, mode="manual", rules=None)
            w.label("knowledge import --from <this folder> --yes")
            w.ev_knowledge_import_text()
        self.assertOraclePasses(self.run_script(script))

    def test_x5_knowledge_import_offers_adopt_only_for_a_member_document_adopt_can_read(self):
        """The members line offers ``instructions <name> --adopt`` for a markerless document, which refuses it."""
        def script(w: ScriptedExtendedWorld) -> None:
            w.root.mkdir(parents=True, exist_ok=True)
            rel = "members/alpha-reviewer.md"
            w.label("a previous team's hand-written members/alpha-reviewer.md, no marker")
            w.world_write(rel, w.document(rel, "markerless", "HAND"), "previous folder")
            w.new_gen(w.project, mode="manual", rules=None)
            w.label("knowledge import --from <this folder> --yes")
            w.ev_knowledge_import_text()
        self.assertOraclePasses(self.run_script(script))

    def test_x6_authorship_of_a_member_document_is_of_this_file_not_of_its_name(self):
        """``mirror.json`` keys a member document's digest by file name, so a same-named document in another folder
        holding bytes this team once wrote elsewhere is taken as ours and written over with no copy."""
        def script(w: ScriptedExtendedWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            mine = (w.root / "members" / "alpha-worker.md").read_bytes()
            w.label("rename member alpha-worker -> alpha-worker-r1")
            roster.update_team(w.gen.paths, lambda doc: roster.apply_adoption(doc.find("alpha-worker"), "alpha-worker-r1"))
            w.label("render")
            w.ev_render()
            other = w.tmp / "q-colleague"
            colleague = other / workdir.DIR_NAME / TEAM / "members" / "alpha-worker.md"
            colleague.parent.mkdir(parents=True)
            colleague.write_bytes(mine)  # a colleague's checkout of a team configured the same way
            w.label("project set to a colleague's checkout whose members/alpha-worker.md is committed")
            w._switch_folder(other)
            w.step_label = w.trace[-1]

            def run(out: StepOutput) -> None:
                code, payload, err = w.run_cli("project", "set", os.fspath(other))
                out.add_render(payload if isinstance(payload, dict) else None)
            w.code_step(w.step_label, run)
        self.assertOraclePasses(self.run_script(script))

    def _peer(self, w: ScriptedExtendedWorld, mode: Optional[str], label: str) -> Gen:
        """A second team (its own state root, the same team name) pointed at ``w``'s folder by ``project set``."""
        peer = Gen(w.take_index(), w.project, mode, None)
        w.peer = peer
        w.peers.append(peer)
        w.label(label)

        def create(out: StepOutput) -> None:
            code, payload, err = w.run_cli("project", "set", os.fspath(w.project))
            out.add_render(payload if isinstance(payload, dict) else None)
        with w.as_gen(peer):
            w.code_step(label, create)
        return peer

    def test_x7_a_baseline_of_a_document_never_written_is_not_authorship(self):
        """The old guard baselined a found document as the one this team *would* write. Another writer then put
        exactly those bytes there (a second team with no rules and no findings writes the same predictable
        ``knowledge.md``), and the next render treated them as "the record we last wrote" and wrote over them with no
        copy. Only bytes this team wrote, or would write byte for byte, are its own now."""
        def script(w: ScriptedExtendedWorld) -> None:
            w.root.mkdir(parents=True, exist_ok=True)
            w.label("a previous team's knowledge.md is in the folder")
            w.world_write("knowledge.md", w.document("knowledge.md", "plausible", "PREV"), "previous folder")
            w.new_gen(w.project, mode="auto", rules=None)
            self._peer(w, "manual", "a second team with no rules is pointed at the same folder")
            w.label("the operator sets rules")
            charter.set_rules(w.gen.layout, TEAM, human(), "the operator's rules")
            w.label("project render")
            w.ev_cli_render()
        self.assertOraclePasses(self.run_script(script))

    def test_x7_b_a_held_record_is_made_true_of_the_disk_before_it_is_shown(self):
        """The same shape, other half: a held record whose file has since changed used to be shown as it was (the
        old guard answered "ours" before it looked at the hold), naming a copy of bytes no longer in place. A hold
        is now recomputed from the file on every render that meets it."""
        def script(w: ScriptedExtendedWorld) -> None:
            w.root.mkdir(parents=True, exist_ok=True)
            w.world_write("knowledge.md", w.document("knowledge.md", "plausible", "PREV"), "previous folder")
            w.label("inherited/ is read-only")
            w.set_inherited_ro(True)
            w.new_gen(w.project, mode="auto", rules=None)
            w.label("a pull replaces knowledge.md")
            w.world_write("knowledge.md", w.document("knowledge.md", "plausible", "PULLED"), "pull")
            w.label("the notifier renders and has not boarded the record yet")
            w.code_step(w.trace[-1], lambda out: out.add_render(workdir.render(w.gen.layout, TEAM)))
            w.label("inherited/ is writable again")
            w.set_inherited_ro(False)
            self._peer(w, "manual", "a second team with no rules is pointed at the same folder")
            w.label("notifier tick")
            w.ev_daemon_tick()
        self.assertOraclePasses(self.run_script(script))

    def test_x8_a_held_member_line_for_a_member_who_left_prints_a_repair_that_runs(self):
        """A previous team's member document is held for ``--adopt``; once that member has left, the line used to
        print ``instructions <name> --adopt``, which refuses a member that is not active (``member_not_found``)."""
        def script(w: ScriptedExtendedWorld) -> None:
            w.root.mkdir(parents=True, exist_ok=True)
            rel = "members/alpha-worker.md"
            w.label("a previous team's members/alpha-worker.md is in the folder")
            w.world_write(rel, w.document(rel, "plausible", "PREV"), "previous folder")
            w.new_gen(w.project, mode="auto", rules=None)
            w.label("member alpha-worker leaves")
            roster.update_team(w.gen.paths, lambda doc: setattr(doc.find("alpha-worker"), "status", "left"))
            w.label("project render")
            w.ev_cli_render()
        self.assertOraclePasses(self.run_script(script))

    def test_x9_the_already_imported_refusal_prints_an_undo_that_runs(self):
        """After an imported board's marks are all deleted, a second import is refused with "that board was imported
        already as B-1 ... `canvas undo B-1` takes it back", and that undo exits 1: there is nothing left to take back."""
        def script(w: ScriptedExtendedWorld) -> None:
            w.root.mkdir(parents=True, exist_ok=True)
            w.label("a previous team's board arrives with 2 pictures")
            w.previous_board(2)
            w.new_gen(w.project, mode="manual", rules=None)
            w.label("canvas import --from <this folder>")
            w.code_step(w.trace[-1], lambda out: out.texts.extend(w.run_cli("canvas", "import", "--from", os.fspath(w.root))[2:]))
            w.label("the operator deletes every imported mark")
            lead = C.CanvasAuthor("human", "human", "cli", True, operator=True)
            for element in list(C.load_scene(w.gen.paths)["elements"]):
                C.apply_ops(w.gen.layout, w.gen.paths, [{"op": "delete", "id": element["id"], "intent": "tidy"}], lead)

            def again(out: StepOutput) -> None:
                code, payload, err = w.run_cli("canvas", "import", "--from", os.fspath(w.root))
                out.texts.append(err)
            w.label("canvas import --from <this folder> again")
            w.code_step(w.trace[-1], again)
        self.assertOraclePasses(self.run_script(script))

    def test_x10_a_record_left_in_place_does_not_fill_its_own_history_with_copies_of_itself(self):
        """A foreign ``knowledge.md`` met by generation after generation (dissolve and recreate, the owner's own
        question) used to be copied again by each one until its own copies evicted its history. Under the
        never-overwrite rule it is held by each generation, and nothing is copied at all."""
        def script(w: ScriptedExtendedWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            for n in range(5):
                w.label("git pull delivers knowledge.md version {}".format(n))
                w.world_write("knowledge.md", w.document("knowledge.md", "plausible", "V{}".format(n)), "pull")
                w.label("render")
                w.ev_render()
            w.label("git pull delivers a knowledge.md another tool manages")
            w.world_write("knowledge.md", w.document("knowledge.md", "foreign-marker", "OTHER"), "pull")
            for _ in range(8):
                w.new_gen(w.project, mode="manual", rules=None)
            self.assertFalse((w.root / workdir.INHERITED_DIR_NAME).exists(), "nothing is copied by itself")
        self.assertOraclePasses(self.run_script(script))


# --------------------------------------------------------------------------
# the third vocabulary
#
# Two extensions in a row finding nothing is the run's stopping criterion, so this one goes where neither earlier
# vocabulary went: what people do to the folder *besides* pulling into it (editing or truncating a copy under
# ``inherited/``, deleting a record, a clone without git-lfs or with CRLF conversion, a directory where a file belongs
# and the reverse, copies restored with old mtimes, a record too large to copy), the commands that render quietly
# (``knowledge set|add|clear``, ``instructions --set|--discard``), the canvas commands whose prune follows the board
# (``canvas import`` through the CLI, then ``canvas undo`` of that import, ``whiteboard clear``), every hazard armed on
# the steps the first two vocabularies never armed one on (``--adopt``, the settle, ``knowledge import``, the
# notifier tick, ``project set``...), and a checkout whose path holds a space or an apostrophe.


X3_DEFAULT_SEEDS = 20


def _x3_seeds() -> List[int]:
    raw = os.environ.get("HERDR_SYNAPSE_ORACLE_X3_SEEDS", "").strip()
    if not raw:
        return list(range(X3_DEFAULT_SEEDS))
    if "," in raw:
        return [int(part) for part in raw.split(",") if part.strip()]
    if "-" in raw:
        low, high = raw.split("-", 1)
        return list(range(int(low), int(high)))
    return list(range(int(raw)))


def _lfs_pointer(data: bytes) -> bytes:
    """What a clone made without git-lfs installed leaves where an LFS-tracked picture was."""
    return "version https://git-lfs.github.com/spec/v1\noid sha256:{}\nsize {}\n".format(sha(data), len(data)).encode()


#: Where a checkout can live. A space is ``~/Library/Mobile Documents``, ``Google Drive/My Drive`` or any folder a
#: person named; an apostrophe is a person's name. Every printed repair that names a path has to survive both.
_PATH_WORDS = {"space": "my project", "apostrophe": "O'Brien's work"}


def _err_strings(err: str) -> List[str]:
    """The strings of an error the CLI wrote to stderr: JSON under ``--json``, so read as JSON, never as escaped text."""
    try:
        return list(_strings(json.loads(err))) if err.strip() else []
    except ValueError:
        return [err]


class ThirdWorld(ExtendedWorld):
    """``ExtendedWorld`` plus the third vocabulary (see the section comment above)."""

    X3_EVENTS: Tuple[Tuple[str, int], ...] = (
        ("edit_copy", 3), ("delete_record", 3), ("lfs_pointers", 1), ("crlf_clone", 1), ("dir_as_file", 1),
        ("record_is_dir", 1), ("mtime_skew", 2), ("pull_oversized", 1),
        ("cli_activity", 5), ("discard", 2), ("canvas_import_cli", 3), ("undo_import", 3), ("whiteboard_clear", 1),
        ("hazard_ext", 7),
    )
    EVENTS = ExtendedWorld.EVENTS + X3_EVENTS
    X3_WORLD = frozenset({"edit_copy", "delete_record", "lfs_pointers", "dir_as_file", "record_is_dir", "mtime_skew",
                          "pull_oversized"})
    WORLD_EVENTS = ExtendedWorld.WORLD_EVENTS | X3_WORLD
    #: The steps a hazard is armed on here: none of them had one in the first two vocabularies.
    HAZARD_STEPS = ("adopt", "settle", "knowledge_import", "daemon_tick", "canvas_import_cli", "sync_toggle", "discard",
                    "cli_activity", "recreate", "undo_import", "whiteboard_clear", "project_status", "import_twice")

    def __init__(self, seed: int, collect: bool = False) -> None:
        super().__init__(seed, collect)
        self.rng = random.Random(seed * 104729 + 31)
        self.path_style = self.rng.choice(["plain", "space", "apostrophe"])
        if self.path_style != "plain":
            _rmtree(self.tmp)
            self.tmp = Path(tempfile.mkdtemp(prefix="orc-x3 {} ".format(_PATH_WORDS[self.path_style]))).resolve()
            self.project = self.tmp / "p0"
            self.project.mkdir()
        self._armed: Optional[Callable[[World], Any]] = None

    # -- plumbing ------------------------------------------------------------------

    def _clear_file_parents(self, rel: str, last: bool = False) -> None:
        """A regular file where ``rel`` needs a directory is replaced, as git replaces it (``dir_as_file`` leaves one)."""
        cursor = self.root
        if cursor.is_file() and not cursor.is_symlink():
            cursor.unlink()
        parts = Path(rel).parts
        for part in parts if last else parts[:-1]:
            cursor = cursor / part
            if cursor.is_file() and not cursor.is_symlink():
                cursor.unlink()

    def world_write(self, rel: str, data: bytes, origin: str) -> None:
        self._clear_file_parents(rel)
        super().world_write(rel, data, origin)

    def symlink_record(self, rel: str) -> None:
        self._clear_file_parents(rel)
        super().symlink_record(rel)

    def symlink_assets(self, beyond_repository: bool) -> None:
        self._clear_file_parents("canvas-assets", last=True)
        super().symlink_assets(beyond_repository)

    def code_step(self, label: str, action: Callable[[StepOutput], None], forced: bool = False,
                  hazard: Optional[Callable[[World], Any]] = None, adoption: Sequence[str] = (),
                  reads: Sequence[str] = ()) -> None:
        if hazard is None and self._armed is not None:
            hazard, self._armed = self._armed, None
        super().code_step(label, action, forced=forced, hazard=hazard, adoption=adoption, reads=reads)

    def _check_command(self, command: str) -> None:
        """A printed repair has to be one shell command as printed: an unbalanced quote is not one."""
        if CONDITIONAL_PLACEHOLDER not in command:
            try:
                shlex.split(command)
            except ValueError as err:
                self.fail("REPAIR_UNPARSEABLE", "a printed repair is not one shell command as printed ({}): {}".format(
                    err, command))
                return
        super()._check_command(command)

    def _copies(self) -> List[Path]:
        inherited = self.root / workdir.INHERITED_DIR_NAME
        if inherited.is_symlink() or not inherited.is_dir():
            return []
        return sorted(p for p in inherited.rglob("*") if p.is_file() and not p.is_symlink())

    def ev_settle(self) -> None:
        """The base settle, guarded as ``Daemon._refresh_workdir`` guards ``scan``: here a hazard can be armed on it,
        and a scan whose state write fails raises, which the notifier logs and outlives. What it did before raising is
        still held to the invariant."""
        self.clock += 10.0

        def run(out: StepOutput) -> None:
            for tick in (self.clock, self.clock + DS.SETTLE_SECONDS + 1):
                try:
                    result = DS.scan(self.gen.layout, TEAM, now=tick)
                except Exception:  # noqa: BLE001 - mirrors the daemon's own guard
                    return
                out.records.extend(result.get("inherited") or [])
        rels = ["knowledge.md"] + ["members/{}.md".format(m) for m in self.gen.members()]
        self.code_step("notifier settle (two scans)", run, adoption=rels)

    # -- what people do to the folder besides pulling ------------------------------------

    def ev_edit_copy(self) -> None:
        copies = self._copies()
        if not copies:
            self.trace[-1] = "edit_copy: there is no copy under inherited/ yet"
            return
        target = self.rng.choice(copies)
        how = self.rng.choice(["append", "truncate", "replace"])
        data = target.read_bytes()
        new = {"append": data + b"\nmerged by hand into the live file\n", "truncate": data[:len(data) // 2],
               "replace": b"resolved; see the live file\n"}[how]
        try:
            target.write_bytes(new)
        except OSError:
            self.trace[-1] = "edit_copy: {} cannot be written".format(target.name)
            return
        self.trace[-1] = "someone edits a copy under inherited/ ({}: {})".format(how, target.relative_to(self.root).as_posix())

    def ev_delete_record(self) -> None:
        members = self.gen.members()
        assets = sorted(p.name for p in (self.root / "canvas-assets").iterdir()) \
            if (self.root / "canvas-assets").is_dir() and not (self.root / "canvas-assets").is_symlink() else []
        options = ["knowledge.md", "facts.md", "canvas.json", "members/{}.md".format(self.rng.choice(members)),
                   "members", "canvas-assets"] + (["canvas-assets/" + self.rng.choice(assets)] if assets else [])
        rel = self.rng.choice(options)
        self.world_remove(rel)
        self.trace[-1] = "git checkout of a branch without {} (it is deleted)".format(rel)

    def ev_lfs_pointers(self) -> None:
        assets = self.root / "canvas-assets"
        if assets.is_symlink() or not assets.is_dir():
            self.trace[-1] = "lfs_pointers: no canvas-assets/ directory"
            return
        names = []
        for entry in sorted(assets.iterdir()):
            if entry.is_file() and not entry.is_symlink():
                self.world_write("canvas-assets/" + entry.name, _lfs_pointer(entry.read_bytes()), "an LFS pointer")
                names.append(entry.name)
        self.trace[-1] = "a checkout without git-lfs turns {} picture(s) into LFS pointers".format(len(names))

    def ev_crlf_clone(self) -> None:
        """A colleague's checkout with ``core.autocrlf=true``: every text record arrives with CRLF line endings."""
        self.projects += 1
        clone = self.tmp / "p{}".format(self.projects - 1)
        shared = self.project / workdir.DIR_NAME
        ignored = _gitignore_patterns(shared / ".gitignore")
        for dirpath, dirnames, filenames in os.walk(shared):
            for name in filenames:
                full = Path(dirpath) / name
                rel = full.relative_to(shared).as_posix()
                if name.startswith(".tmp-") or _ignored(rel, ignored) or full.is_symlink() or not full.is_file():
                    continue
                target = clone / workdir.DIR_NAME / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                data = full.read_bytes()
                if name.endswith((".md", ".json", ".gitignore")) or name == ".gitignore":
                    data = data.replace(b"\r\n", b"\n").replace(b"\n", b"\r\n")
                target.write_bytes(data)
        self.folder_ro = False
        self.inherited_ro = False
        clone.mkdir(parents=True, exist_ok=True)
        self.models[self.project] = self.model
        self.peer = None
        self.model = Model()
        self.branches, self.branch_at = {}, {}
        self.project = clone
        for rel, data in self.disk().files.items():
            if durable(rel):
                self.model.world_placed(rel, data, "CRLF clone")
        self.trace[-1] = "a fresh clone with core.autocrlf=true (new folder {})".format(clone.name)
        self.new_gen(clone)

    def ev_dir_as_file(self) -> None:
        name = self.rng.choice(["members", "canvas-assets"])
        self.world_remove(name)
        self.root.mkdir(parents=True, exist_ok=True)
        (self.root / name).write_bytes(b"")
        self.trace[-1] = "an agent runs: rm -rf <team>/{0} && touch <team>/{0}".format(name)

    def ev_record_is_dir(self) -> None:
        rel = self.rng.choice(["knowledge.md", "facts.md", "canvas.json",
                               "members/{}.md".format(self.rng.choice(self.gen.members()))])
        self.world_remove(rel)
        path = self.root / rel
        if path.parent.is_file():
            path.parent.unlink()
        path.mkdir(parents=True, exist_ok=True)
        (path / "README").write_bytes(b"a directory where a record belongs\n")
        self.trace[-1] = "mkdir <team>/{} (a directory where the record belongs)".format(rel)

    def ev_mtime_skew(self) -> None:
        copies = self._copies()
        touched = 0
        for entry in copies:
            if self.rng.random() < 0.5:
                stamp = self.rng.uniform(1.0e8, 2.5e9)
                try:
                    os.utime(os.fspath(entry), (stamp, stamp))
                    touched += 1
                except OSError:
                    pass
        self.trace[-1] = "copies under inherited/ restored from a backup with other mtimes ({} touched)".format(touched)

    def ev_pull_oversized(self) -> None:
        # Past the copy cap (no copy can be made, the documented ``--force`` case), or past ``knowledge.md``'s own
        # read cap and under the copy cap (a copy can be made, so nothing excuses a loss).
        pad = self.rng.choice([DS.MAX_COPY_BYTES + 4096, DS.MAX_BYTES + 4096])
        kind = self.rng.choice(["knowledge.md", "member", "canvas.json", "asset"])
        token = self.token()
        if kind == "asset":
            data = b"\x89PNG\r\n\x1a\n a very large picture " + token.encode() + b"\x00" * pad
            rel = "canvas-assets/" + sha(data)[:32] + ".png"
        else:
            rel = kind if kind != "member" else "members/{}.md".format(self.rng.choice(self.gen.members()))
            base = self.document(rel, "plausible", token)
            data = base + (b" " * pad if rel.endswith(".json") else b"\n" + b"x" * pad + b"\n")
        self.world_write(rel, data, "an oversized {}".format(rel))
        self.trace[-1] = "git pull delivers an oversized {} ({} bytes)".format(rel, len(data))

    # -- the commands that render quietly ---------------------------------------------

    def ev_cli_activity(self) -> None:
        token = self.token()
        member = self.rng.choice(self.gen.active_members() or self.gen.members())
        argv = self.rng.choice([
            ["knowledge", "set", "rules typed at the CLI {}".format(token), "--yes"],
            ["knowledge", "add", "a finding typed at the CLI {}".format(token)],
            ["knowledge", "clear"],
            ["instructions", member, "--set", "## Mission\n\nmission typed at the CLI {}\n".format(token)],
        ])
        label = "cli: herdr-synapse {}".format(" ".join(argv[:2]))

        def run(out: StepOutput) -> None:
            code, payload, err = self.run_cli(*argv)
            out.texts.extend(_strings(payload))
            out.texts.extend(_err_strings(err))
        self.trace[-1] = label
        self.code_step(label, run)

    def ev_discard(self) -> None:
        name = self.rng.choice(self.gen.members())

        def run(out: StepOutput) -> None:
            code, payload, err = self.run_cli("instructions", name, "--discard", "--yes")
            out.texts.extend(_strings(payload))
            out.texts.extend(_err_strings(err))
        self.trace[-1] = "instructions {} --discard --yes".format(name)
        self.code_step(self.trace[-1], run, reads=["members/{}.md".format(name)])

    # -- canvas commands whose prune follows the board ------------------------------------

    def ev_canvas_import_cli(self) -> None:
        gen = self.gen
        choices = ["folder", "inherited"] + (["other"] if self._other_folders() else [])
        source = self.rng.choice(choices)
        target: Optional[Path] = self.root
        if source == "inherited":
            copies = [p for p in self._copies() if p.parent.name == workdir.INHERITED_DIR_NAME
                      and p.name.startswith("canvas-") and p.name.endswith(".json")]
            target = copies[-1] if copies else None
        elif source == "other":
            target = self.rng.choice(self._other_folders())
        clear_first = self.rng.random() < 0.5
        self.trace[-1] = "canvas import --from {}{} (CLI, + mirror)".format(source, " after whiteboard clear" if clear_first else "")

        def run(out: StepOutput) -> None:
            if target is None:
                return
            if clear_first:
                C.clear(gen.layout, gen.paths, "human")
            code, payload, err = self.run_cli("canvas", "import", "--from", os.fspath(target))
            out.texts.extend(_strings(payload))
            out.texts.extend(_err_strings(err))
            batches = [s for s in _strings(payload) if re.match(r"^B-[0-9]+$", s)]
            if code == 0 and batches:
                gen.import_batch = batches[0]  # type: ignore[attr-defined]
            self.mirror_canvas(out)
        self.code_step(self.trace[-1], run, reads=["canvas.json"])

    def ev_undo_import(self) -> None:
        gen = self.gen
        batch = getattr(gen, "import_batch", None)
        self.trace[-1] = "canvas undo {} (the import's own batch, + mirror)".format(batch)

        def run(out: StepOutput) -> None:
            if not batch:
                return
            code, payload, err = self.run_cli("canvas", "undo", batch)
            out.texts.extend(_strings(payload))
            gen.import_batch = None  # type: ignore[attr-defined]
            self.mirror_canvas(out)
        self.code_step(self.trace[-1], run)

    def ev_whiteboard_clear(self) -> None:
        gen = self.gen

        def run(out: StepOutput) -> None:
            C.clear(gen.layout, gen.paths, "human")
            self.mirror_canvas(out)
        self.trace[-1] = "whiteboard clear (+ mirror)"
        self.code_step(self.trace[-1], run)

    # -- hazards on the steps nobody armed one on -------------------------------------------

    def ev_hazard_ext(self) -> None:
        kinds = ["disk-full-inherited", "disk-full-folder", "race-document", "state-write-fails"] + \
            (["peer"] if self.peer is not None else [])
        kind = self.rng.choice(kinds)
        step = self.rng.choice(self.HAZARD_STEPS)
        if kind.startswith("disk-full") and os.getuid() == 0:
            kind = "race-document"
        self._armed = PeerRenders if kind == "peer" else _HAZARDS[kind]
        self.trace[-1] = "{} during {}".format(kind, step)
        label = self.trace[-1]
        try:
            self.dispatch(step)
        finally:
            self._armed = None
        if not self.trace[-1].startswith(kind):
            self.trace[-1] = "{} during {}".format(kind, self.trace[-1])
        del label

    # -- the generator -------------------------------------------------------------------------

    def allowed(self, name: str) -> bool:
        if name in self.X3_WORLD or name == "crlf_clone":
            if self.folder_ro:
                return False
        if name == "hazard_ext" and self.folder_ro:
            return self.rng.random() < 0.3
        return super().allowed(name)

    def dispatch(self, name: str) -> None:
        if name in {n for n, _ in self.X3_EVENTS}:
            getattr(self, "ev_" + name)()
        else:
            super().dispatch(name)


class FolderInvariantThirdVocabularyTests(unittest.TestCase):
    """The same invariant over the third vocabulary (``ThirdWorld``). Widen with ``HERDR_SYNAPSE_ORACLE_X3_SEEDS``."""

    def test_no_sequence_of_the_third_vocabulary_loses_or_misreports_a_record(self):
        steps = _steps()
        for seed in _x3_seeds():
            with self.subTest(seed=seed):
                world = ThirdWorld(seed, collect=True)
                try:
                    world.run(steps)
                finally:
                    world.close()
                if world.violations:
                    self.fail("{}\n\ndistinct failures:\n  {}".format(world.report(), "\n  ".join(_distinct(world.violations))))


class ScriptedThirdWorld(ThirdWorld):
    """A ``ThirdWorld`` driven by an explicit script, at the real ``facts.md`` cap, in a plain path unless asked."""

    def __init__(self, path_words: Optional[str] = None) -> None:
        super().__init__(seed=0, collect=True)
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.patchers, self.small_facts = [], False
        _rmtree(self.tmp)
        prefix = "orc-x3 {} ".format(path_words) if path_words else "orc-x3-"
        self.tmp = Path(tempfile.mkdtemp(prefix=prefix)).resolve()
        self.project = self.tmp / "p0"
        self.project.mkdir()

    def label(self, text: str) -> None:
        self.trace.append(text)
        self.step_label = text

    def checks(self) -> List[str]:
        return sorted({v["check"] for v in self.violations})


class ThirdVocabularyFindingScriptTests(unittest.TestCase):
    """What the third vocabulary found, each as the shortest script the oracle fails on.

    Red on the tree they were found against (feat/canvas-v2, d13e58c5 plus the inheritance diff after the second fix
    pass, 2026-10-08), by design and not as ``expectedFailure``, for the reason the module docstring gives.
    """

    def run_script(self, script: Callable[[ScriptedThirdWorld], None], path_words: Optional[str] = None) -> ScriptedThirdWorld:
        world = ScriptedThirdWorld(path_words)
        self.addCleanup(world.close)
        script(world)
        return world

    def assertOraclePasses(self, world: ScriptedThirdWorld) -> None:
        if world.violations:
            self.fail("{}\n\ndistinct failures:\n  {}".format(world.report(), "\n  ".join(_distinct(world.violations))))

    def held_member(self, w: ScriptedThirdWorld) -> str:
        """auto mode: a previous team's member document, with a note above its first heading, held for --adopt."""
        rel = "members/alpha-worker.md"
        w.root.mkdir(parents=True, exist_ok=True)
        w.label("a previous team's members/alpha-worker.md is in the folder")
        w.world_write(rel, w.document(rel, "plausible", "PREVIOUS", outside=True), "previous folder")
        w.new_gen(w.project, mode="auto", rules=None)
        return rel

    def edit_the_copy(self, w: ScriptedThirdWorld, rel: str) -> bytes:
        """``--adopt`` takes the held document in, so its checked copy lands in ``inherited/``; the operator then edits
        that copy, and a pull brings the original bytes back to the live path. Returns those bytes."""
        original = (w.root / rel).read_bytes()
        w.label("instructions alpha-worker --adopt (the held line's repair)")
        w.ev_adopt("alpha-worker")
        copies = [p for p in w._copies() if p.read_bytes() == original]
        self.assertTrue(copies, "the adopted document was copied before it was replaced")
        w.label("the operator edits that copy under inherited/ while merging it")
        copies[0].write_bytes(original + b"\nmy merge notes\n")
        w.model.release_unrecoverable(w.disk())
        w.label("git pull brings the original document back")
        w.world_write(rel, original, "pull")
        return original

    def test_x3_1_a_printed_repair_naming_a_path_survives_a_space_in_the_checkout_path(self):
        """``knowledge import --from <folder>`` and ``canvas import --from <copy> --dry-run`` are printed unquoted, so
        in a checkout under ``My Drive`` (or any folder with a space) they exit 2 with ``unrecognized arguments``."""
        def script(w: ScriptedThirdWorld) -> None:
            w.root.mkdir(parents=True, exist_ok=True)
            w.label("a previous team's knowledge.md is in the folder")
            w.world_write("knowledge.md", w.document("knowledge.md", "plausible", "PREVIOUS"), "previous folder")
            w.new_gen(w.project, mode="manual", rules="the operator's rules")
            w.label("draw")
            w.ev_draw()
            w.label("git pull delivers a previous board's canvas.json")
            w.world_write("canvas.json", w.document("canvas.json", "plausible", "BOARD"), "pull")
            w.label("draw")
            w.ev_draw()
        self.assertOraclePasses(self.run_script(script, path_words="My Drive"))

    def test_x3_2_an_edited_copy_is_never_taken_for_the_record_it_was_taken_of(self):
        """A copy is trusted only after its bytes are read back: an edited copy at the name the original's copy would
        take is a collision, so a second ``--adopt`` of the original refuses to replace it rather than name the edit."""
        def script(w: ScriptedThirdWorld) -> None:
            rel = self.held_member(w)
            original = self.edit_the_copy(w, rel)
            w.label("instructions alpha-worker --adopt again")
            w.ev_adopt("alpha-worker")
            self.assertEqual((w.root / rel).read_bytes(), original, "left as it is: its copy could not be checked")
        self.assertOraclePasses(self.run_script(script))

    def test_x3_2_b_force_over_a_held_record_whose_copy_was_edited_refuses(self):
        def script(w: ScriptedThirdWorld) -> None:
            rel = self.held_member(w)
            original = self.edit_the_copy(w, rel)
            w.label("project render --force")
            w.ev_cli_render(force=True)
            self.assertEqual((w.root / rel).read_bytes(), original, "left as it is: its copy could not be checked")
        self.assertOraclePasses(self.run_script(script))

    def test_x3_3_force_over_a_record_too_large_to_copy_says_there_is_no_copy(self):
        """Allowance 2 needs the report to say so. Over a ``knowledge.md`` past the copy cap whose hold was boarded,
        ``project render --force`` reports "drifted (regenerated, your edit was not imported)" and no record; over a
        ``canvas.json`` past the cap it reports nothing but "wrote 1 file"."""
        def script(w: ScriptedThirdWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            pad = DS.MAX_COPY_BYTES + 4096
            w.label("git pull delivers a knowledge.md past the copy cap")
            w.world_write("knowledge.md", w.document("knowledge.md", "plausible", "BIG") + b"\n" + b"x" * pad + b"\n",
                          "oversized knowledge.md")
            w.label("render (the hold is boarded)")
            w.ev_render()
            w.label("project render --force")
            w.ev_cli_render(force=True)
        self.assertOraclePasses(self.run_script(script))

    def test_x3_3_b_force_over_a_canvas_json_too_large_to_copy_says_there_is_no_copy(self):
        def script(w: ScriptedThirdWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            w.label("draw")
            w.ev_draw()
            w.label("git pull delivers a canvas.json past the copy cap")
            w.world_write("canvas.json", w.document("canvas.json", "plausible", "BIG") + b" " * (DS.MAX_COPY_BYTES + 4096),
                          "oversized canvas.json")
            w.label("draw (the mirror holds it)")
            w.ev_draw()
            w.label("project render --force")
            w.ev_cli_render(force=True)
        self.assertOraclePasses(self.run_script(script))

    def test_x3_4_knowledge_import_offers_adopt_only_for_a_member_document_adopt_accepts(self):
        """The members line offers ``instructions <name> --adopt`` for a 135 KB member document, which exits 1 with
        ``instructions_too_long`` (the limit is 4000 characters)."""
        def script(w: ScriptedThirdWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            rel = "members/alpha-worker.md"
            w.label("git pull delivers a 135 KB members/alpha-worker.md")
            w.world_write(rel, w.document(rel, "plausible", "LONG") + b"\n" + b"x" * (DS.MAX_BYTES + 4096) + b"\n", "pull")
            w.label("knowledge import --from <this folder> --yes (text)")
            w.ev_knowledge_import_text()
        self.assertOraclePasses(self.run_script(script))

    def test_x3_5_a_moved_record_is_not_regenerated_without_a_word(self):
        """A write refused because the record changed after its copy (``moved``) says "nothing was written over it";
        the next render regenerates that new version (its copy exists) and reports nothing, so the operator's last
        word about the file is false. Seen in one step here because the notifier's first tick renders twice."""
        def script(w: ScriptedThirdWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            w.label("git pull delivers a previous team's knowledge.md")
            w.world_write("knowledge.md", w.document("knowledge.md", "plausible", "PULLED"), "pull")
            w._armed = _HAZARDS["race-document"]
            w.label("race-document during the notifier's first tick")
            w.ev_daemon_tick()
        self.assertOraclePasses(self.run_script(script))

    def test_x3_6_a_held_record_whose_document_is_gone_is_not_reported_left_as_it_is(self):
        """A held member document deleted by a checkout (or replaced by a directory) is re-reported as "left exactly as
        it is" on the next render."""
        def script(w: ScriptedThirdWorld) -> None:
            rel = self.held_member(w)
            w.label("git checkout of a branch without members/alpha-worker.md")
            w.world_remove(rel)
            w.label("render")
            w.ev_render()
        self.assertOraclePasses(self.run_script(script))

    def test_x3_3_c_a_held_member_replaced_by_one_too_large_to_copy_is_not_said_to_be_at_its_old_copy(self):
        """DATA-2's root surviving for an uncopyable replacement: a plain render re-reports the ``adopt`` hold as copied
        to the *first* version's copy while a version past the copy cap stands in its place, and the ``--force`` that
        line prints destroys it saying "drifted ... your edit"."""
        def script(w: ScriptedThirdWorld) -> None:
            rel = self.held_member(w)
            w.label("git pull replaces it with a version past the copy cap")
            w.world_write(rel, w.document(rel, "plausible", "TWO") + b"\n" + b"x" * (DS.MAX_COPY_BYTES + 4096) + b"\n",
                          "oversized member document")
            w.model.release_unrecoverable(w.disk())
            w.label("render")
            w.ev_render()
        self.assertOraclePasses(self.run_script(script))

    def test_x3_7_the_notifier_does_not_board_a_failed_write_its_own_render_then_makes(self):
        """auto mode: a write that failed on a full disk (under ``knowledge add``'s quiet render) is re-reported by the
        notifier's scan as "the record is still exactly as you found it ... once the folder is writable, project render
        writes it" in the same tick whose render writes it."""
        def script(w: ScriptedThirdWorld) -> None:
            w.new_gen(w.project, mode="auto", rules=None)
            w.label("git pull delivers a previous team's knowledge.md")
            w.world_write("knowledge.md", w.document("knowledge.md", "plausible", "PULLED"), "pull")
            w.model.release_unrecoverable(w.disk())
            w._armed = _HAZARDS["disk-full-folder"]
            w.label("knowledge add on a full disk")
            w.code_step(w.step_label, lambda out: out.texts.extend(_err_strings(w.run_cli("knowledge", "add", "a finding")[2])))
            w.label("notifier tick, the disk has room again")
            w.ev_daemon_tick()
        self.assertOraclePasses(self.run_script(script))
