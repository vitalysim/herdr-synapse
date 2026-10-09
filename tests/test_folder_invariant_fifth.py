"""The fifth oracle vocabulary: what the fourth left out of the never-overwrite rule (verify round 2, 2026-10-09).

The fourth vocabulary (``tests/test_folder_invariant_never_overwrite.py``) attacks the two facts the rule turns on --
"the bytes this team last wrote there" and "an operator ordered this replacement" -- with one shape each. This one
adds the shapes of the same nine situations it never generated, aimed at what the fix pass after it added (claims,
the upgrade carve-out, ``follow_move``, guarded ``board.md`` / ``canvas.md``, the ``deleted`` hold):

* this team's bytes, but not the file it left: a hard link to a file outside the checkout holding the same bytes
  (``ln -f``, a backup tool), and a picture this team wrote and pruned that ``git checkout`` restores;
* the operator edits this team's own document and reverts it to the exact bytes this team wrote; the operator edits
  it and runs ``project render --force`` before document sync has settled;
* ``--force`` with a file already at the copy's name that is a hard link to the record itself; ``--force`` over a
  record of exactly the copy budget and one byte past it; ``--force`` with ``inherited/`` (or ``inherited/members``)
  a symlink to a directory *inside* the team folder (``members/``, ``canvas-assets/``, the folder itself);
* a previous team's ``board.md`` / ``canvas.md``, now that both are written under the one rule, below the copy
  budget, between it and ``board.md``'s 8 MiB read cap, and past that;
* a second team of the same name in another session that resolves its own hold the way its line says
  (``knowledge set`` then ``project render --force``), and one created while this team's first
  ``document-sync.json`` write fails;
* a folder moved while a ``deleted`` hold is open, into a path with a space and an apostrophe while holds are open,
  and moved away and back.

Five checks, each a sentence the contract or the module docstrings state:

* CROSS_SESSION_AUTHORITY -- "nothing a file in the folder says becomes this team's rules ... until you run an import
  or an adopt yourself"; the fix pass treated another session's rules reaching this team's Rules (x4_10) as a trust
  finding. A step that is not one of this team's own imports or adopts must not make this team's stored Rules or a
  member's instructions equal to text another session's operator stored.
* VIEW_LOSS -- disposable ``board.md`` and ``canvas.md`` may regenerate marker-bearing views within their read
  cap. Markerless or oversized files at those names survive; a view is never deleted or copied automatically.
* STRAY_WRITE -- a step creates no file in the team folder outside ``inherited/`` that is not one of the records
  ``paths_for`` names (a copy must never land in ``members/`` or ``canvas-assets/``).
* DELETED_RECREATED -- "automatic document sync never recreates a deleted document by itself": while a team is in
  ``auto`` mode, a synced document held ``deleted`` is recreated only by ``--force`` or ``project sync manual``.
* REVERT_STILL_HELD -- a file holding exactly the bytes this team last wrote is writable: once the operator reverts
  an edit, the next render leaves no hold on it.

``force_copy_edit`` (a writer editing the verified copy before the replace) is the same residual window as
``PreReplaceRace``, widened to the copy, and is armed only with ``HERDR_SYNAPSE_ORACLE_KNOWN_RACE=1``.

STATUS ON THE TREE THIS WAS WRITTEN AGAINST (feat/canvas-v2, d13e58c5 plus the uncommitted never-overwrite diff and
its first fix pass, 2026-10-09). ``FolderInvariantFifthVocabularyTests`` and five of ``FifthVocabularyFindingScriptTests``
FAIL there, on purpose and not as ``expectedFailure``, for the reason ``tests/test_folder_invariant.py`` gives. No
NO_LOSS was found in about 1,100 widened seeds (30, 45 and 60 steps) without the known race; what fails is trust and
promises: another session's operator's Rules or a member's Mission becoming this team's (x5_1, x5_1_b, x5_2), a deleted
synced document recreated because the folder moved (x5_3), and a ``--force`` whose replace fails reprinting itself
without saying why (x5_4_b). ``x5_5`` to ``x5_9`` are positive controls and pass. Two of the oracle's own fixes are
part of this vocabulary: ``ev_settle`` follows the real clock (``FifthWorld.ev_settle``), and FOREIGN_REWRITTEN no longer
judges files reached through a symlinked record directory (``FifthWorld._stats``).

Widen with ``HERDR_SYNAPSE_ORACLE_X5_SEEDS`` (``200``, ``40-80`` or ``3,17,29``) and ``HERDR_SYNAPSE_ORACLE_STEPS``.
"""

from __future__ import annotations

import os
import random
import re
import time
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple
from unittest import mock

import test_folder_invariant as T
import test_folder_invariant_never_overwrite as X4
from herdr_team import charter, roster, store, workdir
from herdr_team import document_sync as DS

X5_DEFAULT_SEEDS = 12


def _x5_seeds() -> List[int]:
    raw = os.environ.get("HERDR_SYNAPSE_ORACLE_X5_SEEDS", "").strip()
    if not raw:
        return list(range(X5_DEFAULT_SEEDS))
    if "," in raw:
        return [int(part) for part in raw.split(",") if part.strip()]
    if "-" in raw:
        low, high = raw.split("-", 1)
        return list(range(int(low), int(high)))
    return list(range(int(raw)))


VIEWS = ("board.md", "canvas.md")
#: What a step may create in the team folder outside ``inherited/``, besides ``members/<a roster name>.md``.
_RECORD = re.compile(r"^(knowledge\.md|facts(-\d+)?\.md|canvas\.json|canvas\.md|board\.md|README\.md|\.gitignore"
                     r"|canvas-assets/[^/]+)$")
#: A step whose own command is an operator import or adoption: it may legitimately take another session's text.
_EXPLICIT = re.compile(r"import|--adopt|inherit|knowledge set|instructions .* --set|\bproject confirm [0-9a-f]{16}\b")


class CopyEdit(T._Hazard):
    """A writer edits the verified copy under ``inherited/`` after it was read back, before the record is replaced."""

    def __init__(self, world: "FifthWorld") -> None:
        super().__init__(world)
        inner = workdir.still_holds
        done = {"fired": False}

        def racing(path: Any, observed: bytes) -> bool:
            ok = inner(path, observed)
            if ok and not done["fired"]:
                inherited = world.root / workdir.INHERITED_DIR_NAME
                for copy in sorted(inherited.rglob("*")) if inherited.is_dir() else []:
                    if copy.is_file() and not copy.is_symlink() and copy.read_bytes() == observed:
                        done["fired"] = True
                        copy.write_bytes(observed + b"\nan agent's edit of the copy\n")
                        break
            return ok

        self.patches.append(mock.patch.object(workdir, "still_holds", racing))


class FifthWorld(X4.FourthWorld):
    """``FourthWorld`` plus the fifth vocabulary and its five checks (see the module docstring)."""

    X5_EVENTS: Tuple[Tuple[str, int], ...] = (
        ("hardlink_ours", 2), ("restore_pruned_asset", 2), ("operator_revert", 3), ("edit_then_force", 2),
        ("force_copy_hardlink", 2), ("force_budget_edge", 3), ("inherited_points_inside", 2),
        ("foreign_view", 3), ("twin_force", 3), ("twin_state_loss", 1),
        ("move_deleted_hold", 2), ("move_to_quoted", 2), ("move_back", 1),
    ) + ((("force_copy_edit", 2),) if X4.KNOWN_RACE else ())
    EVENTS = X4.FourthWorld.EVENTS + X5_EVENTS
    X5_WORLD = frozenset({"hardlink_ours", "restore_pruned_asset"})
    WORLD_EVENTS = X4.FourthWorld.WORLD_EVENTS | X5_WORLD

    def __init__(self, seed: int, collect: bool = False) -> None:
        super().__init__(seed, collect)
        self.rng = random.Random(seed * 104729 + 71)
        #: Bytes the world (a previous team, a pull) placed at ``board.md`` / ``canvas.md``.
        self.view_foreign: Dict[str, Set[bytes]] = {rel: set() for rel in VIEWS}
        #: Synced documents held ``deleted`` in auto mode, by the generation that holds them.
        self.deleted_holds: Dict[str, int] = {}
        self.twin: Optional[T.Gen] = None
        self.hardlinks = 0

    # -- the world never writes through a hard link (git unlinks and creates) ----------------

    def world_write(self, rel: str, data: bytes, origin: str) -> None:
        path = self.root / rel
        try:
            if path.is_file() and not path.is_symlink() and os.stat(os.fspath(path)).st_nlink > 1:
                path.unlink()
        except OSError:
            pass
        super().world_write(rel, data, origin)
        if rel in VIEWS:
            self.view_foreign[rel].add(data)
        self.deleted_holds.pop(rel, None)

    # -- the five checks -------------------------------------------------------------------

    def _authority(self, gen: T.Gen) -> Dict[str, str]:
        out = {"rules": charter.get_rules(gen.layout, T.TEAM) or ""}
        try:
            names = gen.members()
        except Exception:  # noqa: BLE001 - a dissolved roster has no members to compare
            names = []
        for name in names:
            out[name] = charter.get_instructions(gen.layout, T.TEAM, name) or ""
        return out

    def _foreign_authority(self, gen: T.Gen) -> Set[str]:
        out: Set[str] = set()
        seen: Set[int] = {id(gen.ts)}
        for other in self.gens + self.peers:
            if id(other.ts) in seen:
                continue
            seen.add(id(other.ts))
            try:
                out.update(v.strip() for v in self._authority(other).values() if v.strip())
            except Exception:  # noqa: BLE001 - an archived state root has nothing stored any more
                continue
        return out

    def _allowed_members(self) -> Set[str]:
        names: Set[str] = {T.STRANGER_MEMBER}
        for gen in self.gens + self.peers:
            try:
                doc = roster.load_team(gen.paths)
            except Exception:  # noqa: BLE001
                continue
            for member in doc.members:
                names.add(member.name)
                for previous in member.previous_names or []:
                    if isinstance(previous, dict) and isinstance(previous.get("name"), str):
                        names.add(previous["name"])
        return names

    def code_step(self, label: str, action: Callable[[T.StepOutput], None], forced: bool = False,
                  hazard: Optional[Callable[[T.World], Any]] = None, adoption: Sequence[str] = (),
                  reads: Sequence[str] = ()) -> None:
        gen = self.gen
        pre = self.disk()
        before = self._authority(gen)
        mode_before = X4.roster_mode(gen)
        super().code_step(label, action, forced=forced, hazard=hazard, adoption=adoption, reads=reads)
        post = self.disk()
        raced = getattr(self, "_raced", {}) or {}
        self._check_authority(gen, label, before)
        self._check_views(pre, post, raced, forced)
        self._check_stray(pre, post, raced)
        self._check_deleted(gen, pre, post, raced, forced, label, mode_before)

    def _nested_step(self, action: Callable[[T.StepOutput], None], forced: bool, output: T.StepOutput,
                     reads: Sequence[str] = ()) -> None:
        gen = self.gen
        pre = self.disk()
        before = self._authority(gen)
        mode_before = X4.roster_mode(gen)
        super()._nested_step(action, forced, output, reads)
        post = self.disk()
        self._check_authority(gen, self.step_label, before)
        self._check_views(pre, post, {}, forced)
        self._check_stray(pre, post, {})
        self._check_deleted(gen, pre, post, {}, forced, self.step_label, mode_before)

    def _check_authority(self, gen: T.Gen, label: str, before: Dict[str, str]) -> None:
        command = label.split(" -> printed: ", 1)[-1]
        if _EXPLICIT.search(command):
            return
        try:
            after = self._authority(gen)
        except Exception:  # noqa: BLE001 - a dissolved team has nothing stored
            return
        changed = {key: value for key, value in after.items() if value.strip() and value != before.get(key)}
        if not changed:
            return
        foreign = self._foreign_authority(gen)
        for key, value in changed.items():
            if value.strip() in foreign:
                self.fail("CROSS_SESSION_AUTHORITY", "gen {}'s stored {} became {!r} on a step that was none of its "
                          "own imports or adopts ({}); that is text another session's operator stored".format(
                              gen.index, "Rules" if key == "rules" else "instructions for " + key, value[:80], label))

    def _check_views(self, pre: T.Disk, post: T.Disk, raced: Dict[str, List[bytes]], forced: bool) -> None:
        for rel in VIEWS:
            data = pre.get(rel)
            if data is None or data not in self.view_foreign[rel] or post.get(rel) == data:
                continue
            if rel in raced:
                continue
            after = post.get(rel)
            prefixes = tuple(p.encode("utf-8") for p in workdir.MARKER_PREFIXES)
            if len(data) <= workdir.MAX_VIEW_READ_BYTES and data.startswith(prefixes) \
                    and after is not None and after.startswith(prefixes):
                continue  # D1: this is a regenerated local view, never an inheritance source.
            if forced and T.sha(data) in post.inherited_digests:
                continue
            self.fail("VIEW_LOSS", "{} held bytes this team did not write ({} bytes) and this step {} with no "
                      "verified copy under inherited/".format(rel, len(data),
                                                              "deleted it" if post.get(rel) is None else "replaced it"))

    def _check_stray(self, pre: T.Disk, post: T.Disk, raced: Dict[str, List[bytes]]) -> None:
        names = None
        for rel in sorted(set(post.files) - set(pre.files)):
            if rel.startswith(workdir.INHERITED_DIR_NAME + "/") or rel in raced or _RECORD.match(rel):
                continue
            if rel.startswith("members/") and rel.endswith(".md") and rel.count("/") == 1:
                names = names if names is not None else self._allowed_members()
                if rel[len("members/"):-3] in names:
                    continue
            self.fail("STRAY_WRITE", "this step created {} ({} bytes) in the team folder, which is no record the "
                      "mirror writes".format(rel, len(post.files[rel])))

    def _check_deleted(self, gen: T.Gen, pre: T.Disk, post: T.Disk, raced: Dict[str, List[bytes]], forced: bool,
                       label: str, mode_before: Optional[str]) -> None:
        for rel, owner in list(self.deleted_holds.items()):
            if owner != gen.index:
                continue
            if forced or "sync manual" in label or mode_before != "auto" or X4.roster_mode(gen) != "auto":
                self.deleted_holds.pop(rel, None)
                continue
            if pre.get(rel) is None and post.get(rel) is not None and rel not in raced:
                self.fail("DELETED_RECREATED", "{} was held 'deleted' (auto mode: \"never recreates a deleted document "
                          "by itself\") and this step, neither --force nor project sync manual, recreated it".format(rel))
                self.deleted_holds.pop(rel, None)

    def ev_settle(self) -> None:
        """The notifier's settle, on a clock that is never behind a ``since`` the real notifier tick recorded.

        ``ev_daemon_tick`` runs the real daemon, whose scan stamps ``pending`` edits with ``time.time()``, while the base
        ``ev_settle`` scans at the oracle's own clock (1000 s and up): an edit first seen by a tick was never adopted
        by a later settle, so no earlier vocabulary ever saw a tick-then-settle adoption.
        """
        self.clock = max(self.clock, time.time())
        super().ev_settle()

    # -- this team's bytes, not the file it left ---------------------------------------------

    def ev_hardlink_ours(self) -> None:
        ours = [rel for rel in self._ours_now()]
        if not ours or self.folder_ro:
            self.trace[-1] = "hardlink_ours: nothing of this team's is in the folder"
            return
        rel = self.rng.choice(ours)
        self.hardlinks += 1
        outside_dir = self.tmp / "hardlinks"
        if outside_dir not in self.outside_roots:
            self.outside_roots.append(outside_dir)
        outside = outside_dir / "backup-{}-{}".format(self.hardlinks, Path(rel).name)
        data = (self.root / rel).read_bytes()
        self.place_outside(outside, data)
        path = self.root / rel
        path.unlink()
        os.link(os.fspath(outside), os.fspath(path))
        self.trace[-1] = "ln -f: this team's {} becomes a hard link to a backup outside the checkout, same bytes".format(rel)

    def ev_restore_pruned_asset(self) -> None:
        disk = self.disk()
        gen = self.gen.index
        gone = [v for v in self.model.versions.values()
                if v.rel.startswith("canvas-assets/") and gen in v.writers and disk.get(v.rel) is None]
        if not gone or self.folder_ro:
            self.trace[-1] = "restore_pruned_asset: no picture this team wrote has left the folder"
            return
        version = self.rng.choice(sorted(gone, key=lambda v: v.rel))
        self.world_write(version.rel, version.data, "git checkout restores a picture this team wrote and pruned")
        self.trace[-1] = "git checkout restores {}, a picture this team wrote and pruned".format(version.rel)

    # -- the operator and this team's own document ----------------------------------------------

    def _synced_ours(self) -> List[str]:
        return [rel for rel in self._ours_now() if rel == "knowledge.md" or rel.startswith("members/")]

    def ev_operator_revert(self) -> None:
        ours = [rel for rel in self._ours_now() if not rel.startswith("canvas-assets/")]
        if not ours or self.folder_ro:
            self.trace[-1] = "operator_revert: nothing of this team's to edit"
            return
        rel = self.rng.choice(ours)
        original = (self.root / rel).read_bytes()
        # The check below is about the guard's own sentence, "the bytes this team last wrote there", so it is armed
        # only where the guard's record says these are those bytes (an older version this team wrote, which a twin's
        # --force or a checkout can put back, is rightly held ``changed``).
        recorded = DS.load(self.gen.paths).get(os.fspath(self.root / rel)) or {}
        armed = recorded.get("digest") == DS.digest(original) and not recorded.get("claimed")
        shape = self.rng.choice(["rules", "note", "whitespace", "markerless"])
        new = self._edit(rel, original, shape, self.token())
        if new is None or new == original:
            self.trace[-1] = "operator_revert: {} has no {} edit".format(rel, shape)
            return
        self.trace[-1] = "the operator edits this team's {} ({}), a render, then reverts it exactly".format(rel, shape)
        self.world_write(rel, new, "operator edit before a revert")
        self.model.release_unrecoverable(self.disk())
        self.trace.append("render (the edit is held)")
        self.step_label = self.trace[-1]
        self.ev_render()
        if (self.root / rel).read_bytes() != new:
            return  # something adopted or replaced it already; the other checks judged that
        self.trace.append("the operator reverts {} to the bytes this team wrote".format(rel))
        self.world_write(rel, original, "operator revert")
        self.model.release_unrecoverable(self.disk())
        self.trace.append("render")
        self.step_label = self.trace[-1]
        self.ev_render()
        path = os.fspath(self.root / rel)
        for record in DS.held_records(self.gen.paths, self.root) if armed else []:
            entry = DS.load(self.gen.paths).get(path) or {}
            if record["path"] == path and not record.get("stale") and not entry.get("claimed"):
                # A claimed digest (found identical, never written) is the fix pass's accepted cost: held ``found``.
                self.fail("REVERT_STILL_HELD", "{} holds exactly the bytes this team last wrote (reverted by the "
                          "operator) and after a render is still held ({}: {}); entry {}".format(
                              rel, record.get("reason"), record.get("why"),
                              {k: entry.get(k) for k in ("digest", "claimed", "hold")}))

    def ev_edit_then_force(self) -> None:
        ours = self._synced_ours()
        if not ours or self.folder_ro:
            self.trace[-1] = "edit_then_force: no synced document of this team's"
            return
        rel = self.rng.choice(ours)
        new = self._edit(rel, (self.root / rel).read_bytes(), "rules", self.token())
        if new is None:
            self.trace[-1] = "edit_then_force: {} cannot be edited".format(rel)
            return
        self.world_write(rel, new, "operator edit, then --force before it settles")
        self.model.release_unrecoverable(self.disk())
        self.trace[-1] = "the operator edits the Rules/Mission of {}; project render --force before it settles".format(rel)
        self._force(self.trace[-1])
        self.trace.append("notifier settle")
        self.step_label = self.trace[-1]
        self.ev_settle()

    # -- --force and its copy -------------------------------------------------------------------

    def ev_force_copy_hardlink(self) -> None:
        if self.folder_ro or self.inherited_ro:
            self.trace[-1] = "force_copy_hardlink: read-only"
            return
        rel = self._pull_foreign()
        path = self.root / rel
        data = path.read_bytes()
        guard = DS.FolderGuard(self.gen.layout, T.TEAM, self.files())
        try:
            target = guard.copy_target(path, data)
            inherited = self.root / workdir.INHERITED_DIR_NAME
            if inherited.is_symlink() or (inherited.exists() and not inherited.is_dir()):
                raise OSError("inherited/ is not a directory")
            target.parent.mkdir(parents=True, exist_ok=True)
            if os.path.lexists(os.fspath(target)):
                target.unlink()
            os.link(os.fspath(path), os.fspath(target))
        except OSError as err:
            self.trace[-1] = "force_copy_hardlink: cannot link ({})".format(err)
            return
        self.trace[-1] = ("git pull delivers {}; the copy's name under inherited/ is already a hard link to it; "
                          "project render --force".format(rel))
        self._force(self.trace[-1])

    def ev_force_copy_edit(self) -> None:
        if self.folder_ro or self.inherited_ro:
            self.trace[-1] = "force_copy_edit: read-only"
            return
        rel = self._pull_foreign()
        self.trace[-1] = "git pull delivers {}; --force, and a writer edits the verified copy before the replace".format(rel)
        self._force(self.trace[-1], CopyEdit)

    def ev_force_budget_edge(self) -> None:
        if self.folder_ro:
            self.trace[-1] = "force_budget_edge: read-only"
            return
        rel = self.rng.choice(["knowledge.md", "canvas.json", "facts.md",
                               "members/{}.md".format(self.rng.choice(self.gen.members()))])
        size = DS.MAX_COPY_BYTES + self.rng.choice([0, 1])
        base = self.document(rel, "plausible", self.token())
        filler = b" " if rel.endswith(".json") else b"x"
        tail = b"\n" if not rel.endswith(".json") else b""
        data = base + filler * (size - len(base) - len(tail)) + tail
        assert len(data) == size
        self.world_write(rel, data, "a {} of {} bytes".format(rel, size))
        self.model.release_unrecoverable(self.disk())
        self.trace[-1] = "git pull delivers a {} of exactly {} bytes (copy budget {}); project render --force".format(
            rel, size, DS.MAX_COPY_BYTES)
        self._force(self.trace[-1])
        if size == DS.MAX_COPY_BYTES and (self.root / rel).read_bytes() == data and not self.inherited_ro \
                and (self.root / workdir.INHERITED_DIR_NAME).is_dir() is not False:
            held = [r for r in DS.held_records(self.gen.paths, self.root) if r["path"] == os.fspath(self.root / rel)]
            if held and held[0].get("reason") == "too_large":
                self.fail("BUDGET_EDGE", "{} is exactly the 1 MiB copy budget and --force calls it too large to copy "
                          "({})".format(rel, held[0].get("why")))

    def ev_inherited_points_inside(self) -> None:
        if self.folder_ro or self.inherited_ro:
            self.trace[-1] = "inherited_points_inside: read-only"
            return
        inherited = self.root / workdir.INHERITED_DIR_NAME
        # The copies already under inherited/ are what NO LOSS counts on, so they are never moved out of the way: a
        # symlink replaces inherited/ only where there is nothing in it, and otherwise only inherited/members does.
        empty = not os.path.lexists(os.fspath(inherited)) or (
            inherited.is_dir() and not inherited.is_symlink() and not any(inherited.iterdir()))
        ways = [w for w in ("members", "canvas-assets", ".") if w == "." or (self.root / w).is_dir()
                or not os.path.lexists(os.fspath(self.root / w))] if empty else []
        if inherited.is_dir() and not inherited.is_symlink() and not os.path.lexists(os.fspath(inherited / "members")):
            ways.append("inherited/members->members")
        if not ways:
            self.trace[-1] = "inherited_points_inside: inherited/ holds copies and inherited/members exists"
            return
        rel = self._pull_foreign()
        way = self.rng.choice(ways)
        if way == "inherited/members->members":
            os.symlink(os.path.join("..", "members"), os.fspath(inherited / "members"))
        else:
            if os.path.lexists(os.fspath(inherited)):
                inherited.rmdir()
            if way != ".":
                (self.root / way).mkdir(exist_ok=True)
            os.symlink(way, os.fspath(inherited))
        self.trace[-1] = "git pull delivers {}; inherited is a symlink into the team folder ({}); --force".format(rel, way)
        try:
            self._force(self.trace[-1])
        finally:
            if (inherited / "members").is_symlink() and not inherited.is_symlink():
                (inherited / "members").unlink()
            elif inherited.is_symlink():
                inherited.unlink()

    def _stats(self) -> Dict[str, Optional[Tuple[int, int]]]:
        """FOREIGN_REWRITTEN's snapshot, without files reached through a symlinked record directory.

        Such a file is outside the team folder (OUTSIDE_DESTROYED judges it), and a race armed on the step may replace
        the link with a real directory the way git does, after which the mirror rightly writes the picture again into
        the new directory: a new inode at the same name that the mirror did not rewrite in place.
        """
        out = super()._stats()
        for rel in list(out):
            head = rel.split("/", 1)[0]
            if "/" in rel and (self.root / head).is_symlink():
                out.pop(rel)
        return out

    # -- the guarded views ------------------------------------------------------------------------

    def ev_foreign_view(self) -> None:
        if self.folder_ro:
            self.trace[-1] = "foreign_view: read-only"
            return
        rel = self.rng.choice(VIEWS)
        size = self.rng.choice(["small", "past-copy", "past-read"])
        marker = workdir.marker_for(self.root / rel)
        body = "{}\n# a previous team's {}\n\nnotes {}\n".format(marker, rel, self.token()).encode("utf-8")
        if size == "past-copy":
            body += b"x" * (DS.MAX_COPY_BYTES + 4096) + b"\n"
        elif size == "past-read":
            body += b"x" * (workdir.MAX_VIEW_READ_BYTES + 4096) + b"\n"
        self.world_write(rel, body, "a previous team's {}".format(rel))
        then = self.rng.choice(["tick", "force", "draw"])
        self.trace[-1] = "git pull delivers a previous team's {} ({}); then {}".format(rel, size, then)
        self.step_label = self.trace[-1]
        if then == "tick":
            self.ev_daemon_tick()
        elif then == "draw":
            self.ev_draw()
        else:
            self._force(self.trace[-1])

    # -- the twin in another session -----------------------------------------------------------------

    def _make_twin(self, label: str, hazard: Optional[Callable[[T.World], Any]] = None) -> T.Gen:
        twin = T.Gen(self.take_index(), self.project, X4.roster_mode(self.gen), charter.get_rules(self.gen.layout, T.TEAM))
        self.twin = twin
        self.peer = twin
        self.peers.append(twin)

        def create(out: T.StepOutput) -> None:
            code, payload, err = self.run_cli("project", "set", os.fspath(self.project))
            if code != 0:
                self.fail("CREATE_EXIT", err.strip()[:300])
            out.add_render(payload if isinstance(payload, dict) else None)
            self._spend(self.gen.paths, out.records)

        self.trace.append(label)
        with self.as_gen(twin):
            self.code_step(label, create, hazard=hazard)
        return twin

    def ev_twin_force(self) -> None:
        if self.folder_ro:
            self.trace[-1] = "twin_force: read-only"
            return
        twin = self.twin if self.twin is not None and self.twin.project == self.project else None
        if twin is None:
            self.trace[-1] = "twin_force:"
            twin = self._make_twin("a second team of the same name in another session, identical config")
        text = "the other session's rules {}".format(self.token())
        what = self.rng.choice(["rules", "instructions"])
        label = "the other session's operator: {} set, then project render --force (as its hold line says)".format(what)
        self.trace.append(label)

        def run(out: T.StepOutput) -> None:
            if what == "rules":
                self.run_cli("knowledge", "set", text, "--yes")
            else:
                name = self.rng.choice(twin.active_members() or twin.members())
                charter.set_instructions(twin.layout, T.TEAM, T.human(), name, text, None)
            code, payload, err = self.run_cli("project", "render", "--force")
            if isinstance(payload, dict):
                out.add_render(payload)

        with self.as_gen(twin):
            self.code_step(label, run, forced=True)
        self.trace.append("this team's notifier tick and settle")
        self.step_label = self.trace[-1]
        self.ev_daemon_tick()
        self.ev_settle()

    def ev_twin_state_loss(self) -> None:
        """Another session's team writes the folder first; this team is created with its first state write failing."""
        if self.folder_ro:
            self.trace[-1] = "twin_state_loss: read-only"
            return
        mode = X4.roster_mode(self.gen) or "auto"
        self.trace[-1] = "twin_state_loss:"
        twin = self._make_twin("a team of the same name in another session renders the folder first")
        index = self.take_index()
        self.gens.append(T.Gen(index, self.project, mode, charter.get_rules(twin.layout, T.TEAM)))
        label = "this session creates the team again (gen {}, {}); its first document-sync.json write fails".format(
            index, mode)
        self.trace.append(label)

        def create(out: T.StepOutput) -> None:
            code, payload, err = self.run_cli("project", "set", os.fspath(self.project))
            out.add_render(payload if isinstance(payload, dict) else None)
            self._spend(self.gen.paths, out.records)
        self.code_step(label, create, hazard=T._HAZARDS["state-write-fails"])
        self.trace.append("render")
        self.step_label = "render"
        self.ev_render()
        text = "the other session's rules {}".format(self.token())
        label = "the other session's operator sets its rules"
        self.trace.append(label)

        def other(out: T.StepOutput) -> None:
            self.run_cli("knowledge", "set", text, "--yes")
        with self.as_gen(twin):
            self.code_step(label, other)
        self.trace.append("this team's notifier settle")
        self.step_label = self.trace[-1]
        self.ev_settle()

    # -- folders that move with holds open -----------------------------------------------------------

    def ev_move_deleted_hold(self) -> None:
        if self.folder_ro or self.inherited_ro:
            self.trace[-1] = "move_deleted_hold: read-only"
            return
        if X4.roster_mode(self.gen) != "auto":
            self.trace[-1] = "move_deleted_hold: project sync auto"
            self.step_label = self.trace[-1]
            self.code_step(self.trace[-1], lambda out: self.run_cli("project", "sync", "auto") and None)
        ours = self._synced_ours()
        if not ours:
            self.trace.append("move_deleted_hold: no synced document of this team's")
            return
        rel = self.rng.choice(ours)
        self.trace.append("the operator deletes this team's {} (auto mode); notifier tick".format(rel))
        self.step_label = self.trace[-1]
        self.world_remove(rel)
        self.ev_daemon_tick()
        held = [r for r in DS.held_records(self.gen.paths, self.root)
                if r["path"] == os.fspath(self.root / rel) and r.get("reason") == "deleted"]
        if not held:
            return
        self.deleted_holds[rel] = self.gen.index
        self.trace.append("move_folder")
        self.step_label = "move_folder"
        self.ev_move_folder()
        self.trace.append("render")
        self.step_label = "render"
        self.ev_render()

    def _move_to(self, new: Path, label: str) -> None:
        old = self.project
        os.rename(os.fspath(old), os.fspath(new))
        prefix = os.fspath(old) + os.sep
        self.outside = {(os.fspath(new) + os.sep + k[len(prefix):]) if k.startswith(prefix) else k: v
                        for k, v in self.outside.items()}
        model = self.model
        self.models.pop(old, None)
        self.peer = None
        self.twin = None
        self.project = new
        self.models[new] = model
        model.release_unrecoverable(self.disk())
        self.trace.append(label)
        self.step_label = label

        def run(out: T.StepOutput) -> None:
            code, payload, err = self.run_cli("project", "set", os.fspath(new))
            if code != 0:
                self.fail("CREATE_EXIT", err.strip()[:300])
            out.add_render(payload if isinstance(payload, dict) else None)
            self._spend(self.gen.paths, out.records)
        self.code_step(label, run)

    def ev_move_to_quoted(self) -> None:
        if self.folder_ro or self.inherited_ro:
            self.trace[-1] = "move_to_quoted: read-only"
            return
        rel = self._pull_foreign()
        self.trace[-1] = "git pull delivers {}; render (a hold is open)".format(rel)
        self.step_label = self.trace[-1]
        self.ev_render()
        self.counter += 1
        self._move_to(self.tmp / "m it's {}".format(self.counter), "mv to a path with a space and an apostrophe; project set")
        self.trace.append("herdr-synapse project")
        self.step_label = self.trace[-1]
        self.ev_project_status()

    def ev_move_back(self) -> None:
        if self.folder_ro or self.inherited_ro:
            self.trace[-1] = "move_back: read-only"
            return
        home = self.project
        self.counter += 1
        self.trace[-1] = "move_back:"
        self._move_to(self.tmp / "away{}".format(self.counter), "mv away; project set")
        self.trace.append("render")
        self.step_label = "render"
        self.ev_render()
        self._move_to(home, "mv back to where it was; project set")

    # -- the generator --------------------------------------------------------------------------------

    def allowed(self, name: str) -> bool:
        if name in self.X5_WORLD and self.folder_ro:
            return False
        if name in ("move_deleted_hold", "move_to_quoted", "move_back") and (self.folder_ro or self.inherited_ro):
            return False
        return super().allowed(name)

    def dispatch(self, name: str) -> None:
        if name in {n for n, _ in self.X5_EVENTS}:
            getattr(self, "ev_" + name)()
        else:
            super().dispatch(name)


class FolderInvariantFifthVocabularyTests(unittest.TestCase):
    """The same invariant, the fourth vocabulary's checks and the five above, over ``FifthWorld``."""

    def test_no_sequence_of_the_fifth_vocabulary_loses_or_misreports_a_record(self):
        steps = T._steps()
        for seed in _x5_seeds():
            with self.subTest(seed=seed):
                world = FifthWorld(seed, collect=True)
                try:
                    world.run(steps)
                finally:
                    world.close()
                if world.violations:
                    self.fail("{}\n\ndistinct failures:\n  {}".format(world.report(), "\n  ".join(T._distinct(world.violations))))


class ScriptedFifthWorld(FifthWorld):
    """A ``FifthWorld`` driven by an explicit script, at the real ``facts.md`` cap, in a plain path."""

    def __init__(self) -> None:
        super().__init__(seed=0, collect=True)
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.patchers, self.small_facts = [], False
        T._rmtree(self.tmp)
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="orc-x5-")).resolve()
        self.project = self.tmp / "p0"
        self.project.mkdir()

    def label(self, text: str) -> None:
        self.trace.append(text)
        self.step_label = text


class FifthVocabularyFindingScriptTests(unittest.TestCase):
    """What the fifth vocabulary found, each as the shortest script the oracle fails on, plus positive controls."""

    def run_script(self, script: Callable[[ScriptedFifthWorld], None]) -> ScriptedFifthWorld:
        world = ScriptedFifthWorld()
        self.addCleanup(world.close)
        script(world)
        return world

    def assertOraclePasses(self, world: ScriptedFifthWorld) -> None:
        if world.violations:
            self.fail("{}\n\ndistinct failures:\n  {}".format(world.report(), "\n  ".join(T._distinct(world.violations))))

    # -- findings -------------------------------------------------------------------------------

    def _twin(self, w: ScriptedFifthWorld) -> T.Gen:
        return w._make_twin("a second team of the same name in another session, identical config")

    def test_x5_1_the_force_another_sessions_hold_line_prints_does_not_set_this_teams_rules(self):
        """auto mode, two sessions, one folder, one team name -- x4_10 with the other session doing what its own hold
        line tells it to. This team writes knowledge.md; the other session's team finds those bytes identical to its
        own and only *claims* them (the x4_10 fix), so when its operator sets its rules its render holds the file
        ("this team has no record of writing what is there now") and prints ``project render --force``. Run as
        printed, that copies this team's knowledge.md into inherited/ and writes the other session's version; this
        team's notifier then sees an edit of a document this team wrote, and document sync adopts the other session's
        rules as this team's operator Rules. No human in this session touched anything."""
        def script(w: ScriptedFifthWorld) -> None:
            w.new_gen(w.project, mode="auto", rules=None)
            w.label("notifier tick")
            w.ev_daemon_tick()
            twin = self._twin(w)
            w.label("the other session's operator: knowledge set, then its printed project render --force")

            def run(out: T.StepOutput) -> None:
                w.run_cli("knowledge", "set", "the other session's rules", "--yes")
                code, payload, err = w.run_cli("project", "render", "--force")
                out.add_render(payload if isinstance(payload, dict) else None)
            with w.as_gen(twin):
                w.code_step(w.step_label, run, forced=True)
            w.label("this team's notifier settles")
            w.ev_settle()
            self.assertNotEqual(charter.get_rules(w.gen.layout, T.TEAM), "the other session's rules",
                                "another session's rules became this team's operator Rules")
        self.assertOraclePasses(self.run_script(script))

    def test_x5_1_b_the_same_for_a_members_instructions(self):
        """The same route into a member's instructions: the other session sets alpha-worker's Mission and runs
        ``project render --force``; this team's document sync adopts it into this team's stored instructions."""
        def script(w: ScriptedFifthWorld) -> None:
            w.new_gen(w.project, mode="auto", rules=None)
            w.label("notifier tick")
            w.ev_daemon_tick()
            twin = self._twin(w)
            w.label("the other session's operator: alpha-worker's Mission, then project render --force")

            def run(out: T.StepOutput) -> None:
                charter.set_instructions(twin.layout, T.TEAM, T.human(), "alpha-worker", "the other session's mission",
                                         None)
                code, payload, err = w.run_cli("project", "render", "--force")
                out.add_render(payload if isinstance(payload, dict) else None)
            with w.as_gen(twin):
                w.code_step(w.step_label, run, forced=True)
            w.label("this team's notifier settles")
            w.ev_settle()
            self.assertNotIn("the other session's mission",
                             charter.get_instructions(w.gen.layout, T.TEAM, "alpha-worker") or "")
        self.assertOraclePasses(self.run_script(script))

    def test_x5_2_one_failed_state_write_is_not_an_upgrade(self):
        """The upgrade carve-out ("no ``document-sync.json`` yet, a ``mirror.json`` present": a claim counts as this
        team's own) is also what a fresh team looks like after its first ``document-sync.json`` write failed --
        ``mirror.json`` is written separately, and ``FolderGuard.save`` swallows the failure. Another session's team
        of the same name rendered the folder first; this team is created (its first state write fails, so its claims
        are lost), and its next render takes every file identical to its own as written by itself. The other session's
        operator then sets its rules -- a plain ``knowledge set``, no ``--force`` -- and this team's document sync
        adopts them as this team's operator Rules."""
        def script(w: ScriptedFifthWorld) -> None:
            first = T.Gen(w.take_index(), w.project, "auto", None)
            w.peers.append(first)
            w.peer = first
            w.label("a team of the same name in another session renders the folder first")

            def create(out: T.StepOutput) -> None:
                code, payload, err = w.run_cli("project", "set", os.fspath(w.project))
                out.add_render(payload if isinstance(payload, dict) else None)
            with w.as_gen(first):
                w.code_step(w.step_label, create)
            w.gens.append(T.Gen(w.take_index(), w.project, "auto", None))
            w.label("this session creates the team; its first document-sync.json write fails")
            w.code_step(w.step_label, create, hazard=T._HAZARDS["state-write-fails"])
            self.assertFalse(DS.state_path(w.gen.paths).exists())
            w.label("render")
            w.ev_render()
            w.label("the other session's operator sets its rules")
            with w.as_gen(first):
                w.code_step(w.step_label, lambda out: w.run_cli("knowledge", "set", "the other session's rules",
                                                                 "--yes") and None)
            w.label("this team's notifier settles")
            w.ev_settle()
            self.assertNotEqual(charter.get_rules(w.gen.layout, T.TEAM), "the other session's rules")
        self.assertOraclePasses(self.run_script(script))

    def test_x5_3_a_deleted_synced_document_is_not_recreated_because_the_folder_moved(self):
        """auto mode: the operator deletes this team's members/alpha-reviewer.md; the notifier holds it ``deleted``
        ("automatic document sync never recreates a deleted document by itself"). The folder is moved and ``project
        set`` to it: ``follow_move`` drops the entry (no file to compare), so at the new path the document is just
        absent and the render recreates it, though nothing but the folder's name changed."""
        def script(w: ScriptedFifthWorld) -> None:
            w.new_gen(w.project, mode="auto", rules=None)
            w.label("notifier tick")
            w.ev_daemon_tick()
            rel = "members/alpha-reviewer.md"
            w.label("the operator deletes {}; notifier tick".format(rel))
            w.world_remove(rel)
            w.ev_daemon_tick()
            held = [r for r in DS.held_records(w.gen.paths, w.root) if r.get("reason") == "deleted"]
            self.assertEqual([r["path"] for r in held], [os.fspath(w.root / rel)])
            w.deleted_holds[rel] = w.gen.index
            w.label("move_folder")
            w.ev_move_folder()
        self.assertOraclePasses(self.run_script(script))

    @unittest.skipIf(os.getuid() == 0, "root writes into a read-only directory")
    def test_x5_4_b_a_force_whose_replace_fails_says_why_and_does_not_print_itself_again(self):
        """The team folder is not writable but ``inherited/`` is (it was created later, or by another user). ``project
        render --force`` makes and checks the copy, then ``store.atomic_write`` of the record raises ``PermissionError``,
        which ``_replace_foreign`` does not catch: the render files the path under ``skipped`` and ``errors`` (JSON
        only), leaves the hold it had ("it changed after this team last wrote it"), prints ``nothing written; 1 file
        held`` with the same ``project render --force`` as the way to resolve it, and exits 0. Run again, it does the
        same. The text never says the folder is not writable."""
        def script(w: ScriptedFifthWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            w.label("git pull delivers facts.md; render")
            w.world_write("facts.md", w.document("facts.md", "plausible", "PREV"), "pull")
            w.ev_render()
            (w.root / workdir.INHERITED_DIR_NAME).mkdir(exist_ok=True)
            w.label("the team folder becomes read-only; inherited/ stays writable; herdr-synapse project")
            w.set_folder_ro(True)
            os.chmod(os.fspath(w.root / workdir.INHERITED_DIR_NAME), 0o755)
            try:
                w.ev_project_status()
                code, text, err = w.run_cli_text("project", "render", "--force")
                self.assertRegex(text + err, r"(?i)not writable|permission denied|read-only",
                                 "the --force that cannot replace the file never says why")
            finally:
                w.set_folder_ro(False)
        self.assertOraclePasses(self.run_script(script))

    @unittest.skipUnless(X4.KNOWN_RACE, "the residual pre-replace window, widened to the copy; HERDR_SYNAPSE_ORACLE_KNOWN_RACE=1")
    def test_x5_4_a_copy_edited_after_its_check_is_not_the_only_copy_of_what_force_replaces(self):
        """``_replace_foreign`` reads the copy back, then checks the live file, then replaces it. A writer that edits
        the copy (it is a plain file in the checkout) after the read-back and before ``os.replace`` leaves the only
        copy of the replaced bytes changed: the record then says the copy "no longer holds them", truthfully, but the
        bytes are gone. Same window as ``PreReplaceRace``, on the other file."""
        def script(w: ScriptedFifthWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            w.label("git pull delivers a previous team's knowledge.md")
            w.world_write("knowledge.md", w.document("knowledge.md", "plausible", "PREV"), "pull")
            w._force("project render --force; a writer edits the copy before the replace", CopyEdit)
        self.assertOraclePasses(self.run_script(script))

    # -- positive controls --------------------------------------------------------------------------

    def test_x5_5_this_teams_record_hard_linked_to_a_backup_is_replaced_not_written_through(self):
        def script(w: ScriptedFifthWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            w.rng.seed(1)
            w.label("ln -f")
            w.trace.append("x")
            w.ev_hardlink_ours()
            w.label("team activity, render")
            charter.add_finding(w.gen.layout, T.TEAM, T.agent(), "a finding")
            F = __import__("herdr_team.facts", fromlist=["add"])
            F.add(w.gen.paths, {"statement": "a fact", "about": "x", "attribute": "y", "by": "alpha-worker",
                                "by_kind": "claude"}, mode="add")
            charter.set_instructions(w.gen.layout, T.TEAM, T.human(), "alpha-worker", "a mission", None)
            w.ev_render()
        self.assertOraclePasses(self.run_script(script))

    def test_x5_6_force_over_a_record_of_exactly_the_copy_budget_copies_it(self):
        def script(w: ScriptedFifthWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            base = w.document("knowledge.md", "plausible", "EDGE")
            data = base + b"x" * (DS.MAX_COPY_BYTES - len(base) - 1) + b"\n"
            w.label("git pull delivers a knowledge.md of exactly 1 MiB")
            w.world_write("knowledge.md", data, "edge")
            w._force("project render --force")
            self.assertNotEqual((w.root / "knowledge.md").read_bytes(), data)
            self.assertIn(T.sha(data), w.disk().inherited_digests)
        self.assertOraclePasses(self.run_script(script))

    def test_x5_7_force_with_inherited_a_symlink_into_the_folder_refuses(self):
        for way in ("members", ".", "canvas-assets"):
            with self.subTest(way=way):
                def script(w: ScriptedFifthWorld, way: str = way) -> None:
                    w.new_gen(w.project, mode="manual", rules=None)
                    data = w.document("members/alpha-worker.md", "plausible", "PREV")
                    w.label("git pull delivers members/alpha-worker.md; inherited -> {}".format(way))
                    w.world_write("members/alpha-worker.md", data, "pull")
                    inherited = w.root / workdir.INHERITED_DIR_NAME
                    if os.path.lexists(os.fspath(inherited)):
                        inherited.rmdir()
                    (w.root / way).mkdir(exist_ok=True)
                    os.symlink(way, os.fspath(inherited))
                    w._force("project render --force")
                    self.assertEqual((w.root / "members/alpha-worker.md").read_bytes(), data)
                self.assertOraclePasses(self.run_script(script))

    def test_x5_8_views_regenerate_but_foreign_and_oversized_files_survive(self):
        for pad, marked in ((0, True), (DS.MAX_COPY_BYTES + 4096, True),
                            (workdir.MAX_VIEW_READ_BYTES + 4096, True), (0, False)):
            with self.subTest(pad=pad, marked=marked):
                def script(w: ScriptedFifthWorld, pad: int = pad, marked: bool = marked) -> None:
                    w.new_gen(w.project, mode="manual", rules=None)
                    marker = workdir.marker_for(w.root / "board.md") if marked else "# somebody's file"
                    body = "{}\n# a previous team's board\n".format(marker).encode()
                    body += b"x" * pad
                    w.label("a previous team's board.md; notifier tick; --force")
                    w.world_write("board.md", body, "previous board")
                    w.ev_daemon_tick()
                    w._force("project render --force")
                    if marked and len(body) <= workdir.MAX_VIEW_READ_BYTES:
                        self.assertNotEqual((w.root / "board.md").read_bytes(), body)
                    else:
                        self.assertEqual((w.root / "board.md").read_bytes(), body)
                self.assertOraclePasses(self.run_script(script))

    def test_x5_9_a_reverted_edit_is_writable_again(self):
        def script(w: ScriptedFifthWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            w.rng.seed(4)
            w.label("operator_revert")
            w.trace.append("x")
            w.ev_operator_revert()
        self.assertOraclePasses(self.run_script(script))
