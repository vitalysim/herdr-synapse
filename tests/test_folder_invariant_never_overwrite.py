"""The fourth oracle vocabulary: events aimed at the never-overwrite rule itself (owner decision, 2026-10-08).

The first three vocabularies (``tests/test_folder_invariant.py``) were written against the copy-then-overwrite design,
so most of what they generate is a stranger's file arriving. The rule that replaced that design turns on two facts
about one path -- "the bytes this team last wrote there" and "an operator ordered this replacement" -- and this
vocabulary attacks exactly those:

* the bytes are this team's but the file is not as it left it: only the mtime moved (``touch``, a backup restore), or
  the bytes changed while size and mtime were restored (``rsync -a``), or ``git checkout`` restored an *older* version
  this team wrote (a digest it no longer records);
* the operator edits this team's own document, in ``manual`` and in ``auto`` mode, in every shape document sync has to
  tell apart -- an edit of the Rules or a Mission it adopts, a note outside the sections it does not adopt, a
  whitespace-only change, an edit that drops the marker, a deletion;
* ``project render --force`` -- the one replacement of bytes this team did not write -- racing an external writer at
  the look, over a file past the copy budget, and with ``inherited/`` unwritable in each way a checkout can make it so;
* a picture ``canvas.json`` names that this team never wrote (a colleague's identical file landing first, or a pull
  replacing this team's picture with other bytes);
* a second team of the same name in another session whose output is byte-identical to this team's, so both claim it;
* a folder moved, and ``project set`` to it, while holds are open.

Four checks the earlier vocabularies had no use for, each a statement the never-overwrite contract makes:

* FOREIGN_REWRITTEN -- a file whose bytes this team did not write is never rewritten, not even with identical bytes:
  its inode and mtime are what they were (``leaves the file untouched``).
* SILENT_HOLD -- after the notifier's tick, every standing hold has been said to the operator: its path is on the
  board, or a consumer that shows it marked it reported (``reports it once to a consumer that shows it``; "it must
  never freeze silently"). A synced document waiting out document sync's settle window is exempt.
* RESOLVER_INEFFECTIVE -- a printed ``project render --force`` ("write this team's version, copying the file into
  inherited/ first") run as printed leaves no file it was printed for held as ``found`` or ``changed``.
* FALSE_AUTHORSHIP / COPY_SAYS_NOT_OURS -- a hold that says "this team did not write" a file, or a copy line that says
  the file "held bytes this team did not write", names bytes this team never wrote.
* MV_* -- the ``mv`` a hold prints ("move it aside yourself") would run as printed: its source exists, its destination
  does not (``mv`` silently replaces a file there), and its source is inside the team folder.

The residual race the developer named (a write landing between the final ``still_holds`` and ``os.replace``) is
reproduced by ``PreReplaceRace``, armed only with ``HERDR_SYNAPSE_ORACLE_KNOWN_RACE=1``: it is an inherent residual gap,
and a default run that fails on it forever would teach people to ignore this suite.

STATUS ON THE TREE THIS WAS WRITTEN AGAINST (feat/canvas-v2, d13e58c5 plus the uncommitted never-overwrite diff,
2026-10-08). ``FolderInvariantNeverOverwriteTests`` and most of ``NeverOverwriteFindingScriptTests`` FAIL there, on
purpose and not as ``expectedFailure``, for the reason ``tests/test_folder_invariant.py`` gives. No NO_LOSS was found
over 580 widened seeds without the known race; every failure is a silent hold, a printed repair that does not do what
it says, an automatic copy after an edit document sync adopted nothing from, or an authorship sentence that is not
known to be true. ``x4_6``, ``x4_7`` and ``x4_8`` are positive controls and pass.

Widen with ``HERDR_SYNAPSE_ORACLE_X4_SEEDS`` (``200``, ``40-80`` or ``3,17,29``) and ``HERDR_SYNAPSE_ORACLE_STEPS``.
"""

from __future__ import annotations

import os
import random
import re
import shlex
import shutil
import unittest
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Sequence, Set, Tuple
from unittest import mock

import test_folder_invariant as T
from herdr_team import charter, store, workdir
from herdr_team import document_sync as DS

X4_DEFAULT_SEEDS = 20


def _x4_seeds() -> List[int]:
    raw = os.environ.get("HERDR_SYNAPSE_ORACLE_X4_SEEDS", "").strip()
    if not raw:
        return list(range(X4_DEFAULT_SEEDS))
    if "," in raw:
        return [int(part) for part in raw.split(",") if part.strip()]
    if "-" in raw:
        low, high = raw.split("-", 1)
        return list(range(int(low), int(high)))
    return list(range(int(raw)))


KNOWN_RACE = os.environ.get("HERDR_SYNAPSE_ORACLE_KNOWN_RACE", "").strip() == "1"

#: ``mv <path> <path>.aside`` as a hold prints it: the command ends at two spaces, as every command in a line does.
_MV = re.compile(r"(?<![\w-])(mv (?:'[^']*'|\S)+(?: (?:'[^']*'|\S)+)+?)(?=  |$|\.$)")


def _stat_key(path: Path) -> Optional[Tuple[int, int]]:
    try:
        st = os.lstat(os.fspath(path))
    except OSError:
        return None
    return (int(st.st_ino), int(st.st_mtime_ns))


# --------------------------------------------------------------------------
# hazards on --force


class LookRace(T._Hazard):
    """An agent writes a record the instant the guard has looked at it: between the look and the copy."""

    def __init__(self, world: "FourthWorld") -> None:
        super().__init__(world)
        inner = DS.look
        done = {"fired": False}

        def racing(path: Any, *args: Any, **kwargs: Any) -> Any:
            seen = inner(path, *args, **kwargs)
            if done["fired"] or seen.data is None:
                return seen
            rel = world._rel(path)
            if rel is None or not T.durable(rel) or rel.startswith("canvas-assets/"):
                return seen
            done["fired"] = True
            new = world.document(rel, "plausible", world.token() + "-at-the-look") \
                if not rel.startswith("canvas-assets/") else b"raced"
            world.world_write(rel, new, "raced at the look over {}".format(rel))
            world._raced.setdefault(rel, []).append(new)
            return seen

        self.patches.append(mock.patch.object(DS, "look", racing))


class PreReplaceRace(T._Hazard):
    """The residual window the developer named: a write lands after the last ``still_holds`` and before the replace."""

    def __init__(self, world: "FourthWorld") -> None:
        super().__init__(world)
        inner = workdir.still_holds
        done = {"fired": False}

        def racing(path: Any, observed: bytes) -> bool:
            ok = inner(path, observed)
            rel = world._rel(path)
            if ok and not done["fired"] and rel is not None and T.durable(rel) and not rel.startswith("canvas-assets/"):
                done["fired"] = True
                new = world.document(rel, "plausible", world.token() + "-before-the-replace")
                world.world_write(rel, new, "raced before the replace over {}".format(rel))
            return ok

        self.patches.append(mock.patch.object(workdir, "still_holds", racing))


# --------------------------------------------------------------------------
# the world


class FourthWorld(T.ThirdWorld):
    """``ThirdWorld`` plus the never-overwrite vocabulary and its four checks (see the module docstring)."""

    X4_EVENTS: Tuple[Tuple[str, int], ...] = (
        ("touch_ours", 3), ("same_size_swap", 2), ("restore_older", 5), ("operator_edit", 10),
        ("force_race", 3), ("force_oversized", 2), ("force_inherited_blocked", 3),
        ("foreign_asset_named", 3), ("twin_peer", 2), ("move_with_holds", 2),
    )
    EVENTS = T.ThirdWorld.EVENTS + X4_EVENTS
    X4_WORLD = frozenset({"touch_ours", "same_size_swap", "restore_older"})
    WORLD_EVENTS = T.ThirdWorld.WORLD_EVENTS | X4_WORLD

    def __init__(self, seed: int, collect: bool = False) -> None:
        super().__init__(seed, collect)
        self.rng = random.Random(seed * 1299709 + 53)
        self._pre_stats: Dict[str, Optional[Tuple[int, int]]] = {}
        self.mv_checked: Set[Tuple[str, str]] = set()

    # -- stat snapshots for FOREIGN_REWRITTEN --------------------------------

    def _stats(self) -> Dict[str, Optional[Tuple[int, int]]]:
        disk = self.disk()
        return {rel: _stat_key(self.root / rel) for rel in disk.files
                if T.durable(rel) and not rel.startswith(workdir.INHERITED_DIR_NAME + "/")}

    def code_step(self, label: str, action: Callable[[T.StepOutput], None], forced: bool = False,
                  hazard: Optional[Callable[[T.World], Any]] = None, adoption: Sequence[str] = (),
                  reads: Sequence[str] = ()) -> None:
        self._pre_stats = self._stats()
        super().code_step(label, action, forced=forced, hazard=hazard, adoption=adoption, reads=reads)

    def _nested_step(self, action: Callable[[T.StepOutput], None], forced: bool, output: T.StepOutput,
                     reads: Sequence[str] = ()) -> None:
        self._pre_stats = self._stats()
        command = self.step_label.split(" -> printed: ", 1)[-1]
        watched: List[Dict[str, Any]] = []
        if "project render" in command and "--force" in command:
            watched = [r for r in DS.held_records(self.gen.paths, self.root)
                       if not r.get("stale") and r.get("reason") in ("found", "changed")
                       and any("--force" in c for c in r.get("commands") or [])]

        def run(out: T.StepOutput) -> None:
            action(out)
            if not watched or any(r.get("reason") for r in out.renders):
                return  # a render that wrote nothing at all says why (a ``members`` file, no project); judged elsewhere
            after = {r["path"]: r for r in DS.held_records(self.gen.paths, self.root)}
            for record in watched:
                now = after.get(record["path"])
                if now is not None and now.get("reason") in ("found", "changed"):
                    self.fail("RESOLVER_INEFFECTIVE", "{} was held ({}: {}) with  herdr-synapse project render --force  "
                              "printed as the way to write this team's version, and after running it as printed it "
                              "is still held ({}: {})".format(record["path"], record.get("reason"), record.get("why"),
                                                             now.get("reason"), now.get("why")))
        super()._nested_step(run, forced, output, reads)

    # -- the extra claims ------------------------------------------------------

    def _check_claims(self, output: T.StepOutput, pre: T.Disk, post: T.Disk, raced: Dict[str, List[bytes]],
                      written: Dict[str, bytes], gen: int) -> None:
        super()._check_claims(output, pre, post, raced, written, gen)
        # FOREIGN_REWRITTEN: bytes this team did not write, still the same bytes, rewritten anyway.
        for rel, before in self._pre_stats.items():
            data = pre.get(rel)
            if data is None or post.get(rel) != data or data in raced.get(rel, ()) or rel in raced:
                continue
            version = self.model.versions.get((rel, T.sha(data)))
            if version is None or gen in version.writers:
                continue
            now = _stat_key(self.root / rel)
            if before is not None and now is not None and now != before:
                self.fail("FOREIGN_REWRITTEN", "{} holds bytes this team did not write ({}); they are byte-identical "
                          "after this step but the file was rewritten (inode/mtime {} -> {})".format(
                              rel, version.origin, before, now))
        # FALSE_AUTHORSHIP: "this team did not write" said of bytes this team wrote. The sentence itself is judged,
        # as the operator reads it (``describe_inherited``), not the record's reason code: a hold that says only what
        # the code knows ("this team has no record of writing it") is true even of bytes this team wrote and lost
        # the record of, and is not this finding.
        for record in output.records:
            rel = self._rel(record.get("path"))
            if rel is None:
                continue
            line = DS.describe_inherited(record, style="board")
            if record.get("held") and record.get("reason") == "unnamed" and "this team did not write" in line:
                data = post.get(rel)
                said = "no mark names it and this team did not write it"
            elif not record.get("held") and record.get("snapshot") and Path(str(record["snapshot"])).is_file() \
                    and "held bytes this team did not write" in line:
                data = Path(str(record["snapshot"])).read_bytes()
                said = "held bytes this team did not write; they were copied to {}".format(record["snapshot"])
            else:
                continue
            if data is None:
                continue
            version = self.model.versions.get((rel, T.sha(data)))
            if version is not None and gen in version.writers:
                self.fail("FALSE_AUTHORSHIP" if record.get("held") else "COPY_SAYS_NOT_OURS", "{}: the record says \"{}\", but these bytes ({}) were written by this "
                          "team (gen {}; writers {})".format(rel, said, T.sha(data)[:12], gen,
                                                              sorted(map(str, version.writers))))

    def _printed(self, output: T.StepOutput) -> List[str]:
        commands = super()._printed(output)
        texts = list(output.texts)
        for record in output.records:
            texts.append(DS.describe_inherited(record, style="board"))
            texts.append(DS.describe_inherited(record, style="list"))
        for text in texts:
            for match in _MV.finditer(text or ""):
                self._check_mv(match.group(1).rstrip("."))
        return commands

    def _check_mv(self, command: str) -> None:
        key = (command, self.disk().fingerprint())
        if key in self.mv_checked:
            return
        self.mv_checked.add(key)
        try:
            words = shlex.split(command)
        except ValueError as err:
            self.fail("MV_UNPARSEABLE", "a printed mv is not one shell command ({}): {}".format(err, command))
            return
        if len(words) != 3:
            self.fail("MV_UNPARSEABLE", "a printed mv does not have one source and one destination: {}".format(command))
            return
        source, target = Path(words[1]), Path(words[2])
        if not os.path.lexists(os.fspath(source)):
            self.fail("MV_MISSING", "a printed mv names a file that is not there, so it exits 1: {}".format(command))
            return
        if os.path.lexists(os.fspath(target)):
            what = "a directory, so mv moves the file into it" if target.is_dir() and not target.is_symlink() \
                else "a file, which mv silently replaces"
            self.fail("MV_CLOBBER", "a printed mv's destination already exists ({}): {}".format(what, command))
        root = os.path.realpath(os.fspath(self.root))
        parent = os.path.realpath(os.fspath(source.parent))
        if parent != root and not parent.startswith(root + os.sep):
            self.fail("MV_OUTSIDE", "a printed mv moves a file outside the team folder (through a symlink): {}".format(
                command))

    # -- SILENT_HOLD -----------------------------------------------------------

    def ev_daemon_tick(self) -> None:
        super().ev_daemon_tick()
        gen = self.gen
        if self.folder_ro:
            return  # a read-only checkout renders nothing; its holds are older than this tick
        texts = [str(r.get("text") or "") for r in store.BoardStore(gen.ts.team).read()]
        state = DS.load(gen.paths)
        try:
            from herdr_team import roster as _roster

            auto = DS.enabled(_roster.load_team(gen.paths))
        except Exception:  # noqa: BLE001 - the check is about the board, not the roster
            auto = False
        for record in DS.held_records(gen.paths, self.root):
            if record.get("stale"):
                continue
            path = str(record["path"])
            entry = state.get(path) or {}
            if any(path in text for text in texts) or entry.get("reported") == record.get("token"):
                continue
            synced = path == os.fspath(self.files()["knowledge"]) or Path(path).parent.name == "members"
            if auto and synced and (entry.get("pending") or entry.get("error")):
                continue  # waiting out document sync's settle window; its error, if any, is boarded with the path
            self.fail("SILENT_HOLD", "after the notifier's tick {} is held ({}: {}) and no line on the board names it, "
                      "and no consumer marked it reported; `herdr-synapse project` is the only place it shows".format(
                          path, record.get("reason"), record.get("why")))

    # -- the bytes are ours, the file is not as we left it -------------------------

    def _ours_now(self) -> List[str]:
        disk = self.disk()
        out = []
        for rel, data in sorted(disk.files.items()):
            if not T.durable(rel) or rel.startswith(workdir.INHERITED_DIR_NAME + "/"):
                continue
            version = self.model.versions.get((rel, T.sha(data)))
            if version is not None and self.gen.index in version.writers and "world" not in version.writers:
                out.append(rel)
        return out

    def ev_touch_ours(self) -> None:
        touched = 0
        for rel in self._ours_now():
            if self.rng.random() < 0.6:
                stamp = self.rng.uniform(1.0e8, 2.5e9)
                try:
                    os.utime(os.fspath(self.root / rel), (stamp, stamp))
                    touched += 1
                except OSError:
                    pass
        self.trace[-1] = "touch: {} of this team's own records get another mtime, same bytes".format(touched)

    def ev_same_size_swap(self) -> None:
        disk = self.disk()
        candidates = [rel for rel, data in sorted(disk.files.items()) if T.durable(rel) and len(data) > 24
                      and not rel.startswith(workdir.INHERITED_DIR_NAME + "/")]
        if not candidates:
            self.trace[-1] = "same_size_swap: no record to swap"
            return
        rel = self.rng.choice(candidates)
        path = self.root / rel
        data = disk.files[rel]
        st = os.stat(os.fspath(path))
        middle = len(data) // 2
        new = data[:middle] + bytes([data[middle] ^ 0x01]) + data[middle + 1:]
        self.world_write(rel, new, "rsync -a: same size and mtime, other bytes")
        try:
            os.utime(os.fspath(path), ns=(st.st_atime_ns, st.st_mtime_ns))
        except OSError:
            pass
        self.trace[-1] = "rsync -a delivers {} with the same size and mtime and one byte different".format(rel)

    def ev_restore_older(self) -> None:
        disk = self.disk()
        gen = self.gen.index
        candidates = [v for v in self.model.versions.values()
                      if gen in v.writers and disk.get(v.rel) != v.data
                      and not v.rel.startswith(workdir.INHERITED_DIR_NAME + "/")]
        if not candidates:
            self.trace[-1] = "restore_older: this team has no older version anywhere"
            return
        version = self.rng.choice(sorted(candidates, key=lambda v: (v.rel, v.digest)))
        self.world_write(version.rel, version.data, "git checkout restores an older version this team wrote")
        self.trace[-1] = "git checkout restores an older version this team wrote of {} ({})".format(
            version.rel, version.digest[:10])

    # -- the operator edits this team's own document ----------------------------------

    def _edit(self, rel: str, data: bytes, shape: str, token: str) -> Optional[bytes]:
        try:
            text = data.decode("utf-8")
        except ValueError:
            return None
        if rel == "knowledge.md":
            if shape == "rules":
                return text.replace(DS.RULES_HEADING + "\n\n", DS.RULES_HEADING + "\n\nthe operator's edit {}\n".format(
                    token), 1).encode("utf-8")
            if shape == "note":
                lines = text.split("\n")
                return "\n".join(lines[:2] + ["A note the operator typed above the sections: {}".format(token)]
                                 + lines[2:]).encode("utf-8")
            if shape == "whitespace":
                return text.replace("\n" + DS.FINDINGS_HEADING, "\n\n" + DS.FINDINGS_HEADING, 1).encode("utf-8")
        if rel.startswith("members/"):
            if shape == "rules":
                return text.replace("## Mission\n\n", "## Mission\n\nthe operator's edit {} ".format(token), 1).encode()
            if shape == "note":
                lines = text.split("\n")
                return "\n".join(lines[:2] + ["A note the operator typed: {}".format(token)] + lines[2:]).encode()
            if shape == "whitespace":
                return (text.rstrip("\n") + "\n\n\n").encode("utf-8")
        if shape == "markerless":
            return text.split("\n", 1)[-1].encode("utf-8")
        if rel.endswith(".json"):
            return (text.rstrip("\n") + " \n").encode("utf-8")
        return (text.rstrip("\n") + "\n\nthe operator's own line {}\n".format(token)).encode("utf-8")

    def ev_operator_edit(self) -> None:
        mode = self.rng.choice(["auto", "manual"])
        if not self.folder_ro:
            self.trace[-1] = "project sync {} (before the operator's edit)".format(mode)
            self.step_label = self.trace[-1]

            def sync(out: T.StepOutput) -> None:
                code, payload, err = self.run_cli("project", "sync", mode)
                if isinstance(payload, dict):
                    out.records.extend(payload.get("inherited") or [])
            self.code_step(self.step_label, sync)
        ours = [rel for rel in self._ours_now() if not rel.startswith("canvas-assets/")]
        if not ours or self.folder_ro:
            self.trace.append("operator_edit: nothing of this team's is in the folder to edit")
            return
        rel = self.rng.choice(ours)
        shape = self.rng.choice(["rules", "note", "whitespace", "markerless", "delete"])
        token = self.token()
        if shape == "delete":
            self.trace.append("the operator deletes this team's own {} ({} mode)".format(rel, mode))
            self.world_remove(rel)
            return
        new = self._edit(rel, self.disk().get(rel) or b"", shape, token)
        if new is None or new == self.disk().get(rel):
            self.trace.append("operator_edit: {} has no {} edit".format(rel, shape))
            return
        self.trace.append("the operator edits this team's own {} ({}, {} mode)".format(rel, shape, mode))
        self.world_write(rel, new, "operator edit ({}) of this team's {}".format(shape, rel))
        self.model.release_unrecoverable(self.disk())

    # -- --force, the one replacement of bytes this team did not write -----------------

    def _force(self, label: str, hazard: Optional[Callable[[T.World], Any]] = None) -> None:
        self.step_label = label

        def run(out: T.StepOutput) -> None:
            from herdr_team import cmd_knowledge

            code, payload, err = self.run_cli("project", "render", "--force")
            if code != 0:
                self.fail("RENDER_EXIT", "project render --force exited {}: {}".format(code, err[:300]))
            if isinstance(payload, dict):
                out.add_render(payload)
                out.texts.append(cmd_knowledge._render_result_text(payload))
        self.code_step(label, run, forced=True, hazard=hazard)

    def _pull_foreign(self) -> str:
        rel = self.rng.choice(["knowledge.md", "facts.md", "canvas.json",
                               "members/{}.md".format(self.rng.choice(self.gen.members()))])
        self.world_write(rel, self.document(rel, "plausible", self.token()), "pull before --force")
        self.model.release_unrecoverable(self.disk())
        return rel

    def ev_force_race(self) -> None:
        if self.folder_ro:
            self.trace[-1] = "force_race: the checkout is read-only"
            return
        rel = self._pull_foreign()
        kinds = ["look", "copy"] + (["pre-replace"] if KNOWN_RACE else [])
        kind = self.rng.choice(kinds)
        hazard = {"look": LookRace, "copy": T.RaceDocument, "pre-replace": PreReplaceRace}[kind]
        self.trace[-1] = "git pull delivers {}, then project render --force races a writer at the {}".format(rel, kind)
        self._force(self.trace[-1], hazard)

    def ev_force_oversized(self) -> None:
        if self.folder_ro:
            self.trace[-1] = "force_oversized: the checkout is read-only"
            return
        self.ev_pull_oversized()
        self.model.release_unrecoverable(self.disk())
        self.trace[-1] = self.trace[-1] + ", then project render --force"
        self._force(self.trace[-1])

    def ev_force_inherited_blocked(self) -> None:
        if self.folder_ro:
            self.trace[-1] = "force_inherited_blocked: the checkout is read-only"
            return
        rel = self._pull_foreign()
        inherited = self.root / workdir.INHERITED_DIR_NAME
        empty = not os.path.lexists(os.fspath(inherited)) or (inherited.is_dir() and not inherited.is_symlink()
                                                              and not any(inherited.iterdir()))
        ways = ["chmod"] if os.getuid() != 0 else []
        if empty:
            ways += ["file", "symlink"]
        if inherited.is_dir() and not inherited.is_symlink() and not self.inherited_ro \
                and not os.path.lexists(os.fspath(inherited / "members")):
            ways.append("members-file")
        if not ways:
            self.trace[-1] = "force_inherited_blocked: no way to block inherited/ here"
            return
        way = self.rng.choice(ways)
        undo: Callable[[], None]
        if way == "chmod":
            was = self.inherited_ro
            self.set_inherited_ro(True)

            def undo() -> None:
                self.set_inherited_ro(was)
        elif way == "file":
            T._rmtree(inherited)
            inherited.write_bytes(b"")

            def undo() -> None:
                inherited.unlink()
        elif way == "symlink":
            T._rmtree(inherited)
            elsewhere = self.tmp / "elsewhere-{}".format(self.token())
            elsewhere.mkdir()
            self.outside_roots.append(elsewhere)
            os.symlink(os.fspath(elsewhere), os.fspath(inherited))

            def undo() -> None:
                inherited.unlink()
        else:
            (inherited / "members").write_bytes(b"")

            def undo() -> None:
                (inherited / "members").unlink()
        self.trace[-1] = "git pull delivers {}; inherited/ is blocked ({}); project render --force".format(rel, way)
        try:
            self._force(self.trace[-1])
        finally:
            undo()

    # -- a picture canvas.json names that this team never wrote ---------------------------

    def ev_foreign_asset_named(self) -> None:
        gen = self.gen
        if self.folder_ro:
            self.trace[-1] = "foreign_asset_named: the checkout is read-only"
            return
        shape = self.rng.choice(["identical-first", "replaced"])
        if shape == "identical-first":
            # The picture the next draw makes, delivered by a colleague's push before this team mirrors it.
            data = T._png(10 + gen.images + 1, 7 + gen.index)
            name = T.sha(data)[:32] + ".png"
            self.world_write("canvas-assets/" + name, data, "a colleague's identical picture, first")
            self.model.release_unrecoverable(self.disk())
            self.trace[-1] = "a colleague's identical copy of the next picture lands first, then draw 1 picture"
            self.step_label = self.trace[-1]
            self.ev_draw(images=1)
            return
        assets = self.root / "canvas-assets"
        ours = [rel for rel in self._ours_now() if rel.startswith("canvas-assets/")]
        if not ours:
            self.trace[-1] = "foreign_asset_named: this team has no picture in the folder"
            return
        rel = self.rng.choice(ours)
        self.world_write(rel, b"\x89PNG\r\n\x1a\n other bytes under this team's name " + self.token().encode(),
                         "a pull replaces this team's picture")
        self.model.release_unrecoverable(self.disk())
        self.trace[-1] = "a pull replaces this team's {} with other bytes, then draw".format(rel)
        self.step_label = self.trace[-1]
        del assets
        self.ev_draw()

    # -- a second team of the same name whose output is this team's, byte for byte ---------

    def ev_twin_peer(self) -> None:
        if self.peer is not None or self.folder_ro:
            self.trace[-1] = "twin_peer: a second team is already there; the second team renders"
            with self.as_gen(self.peer or self.gen):
                self.ev_render()
            return
        index = self.take_index()
        mine = roster_mode(self.gen)
        rules = charter.get_rules(self.gen.layout, T.TEAM)
        peer = T.Gen(index, self.project, mine, rules)
        self.peer = peer
        self.peers.append(peer)
        label = "a second team of the same name in another session, identical config (gen {}, {})".format(
            index, mine or "default")
        self.trace[-1] = label

        def create(out: T.StepOutput) -> None:
            code, payload, err = self.run_cli("project", "set", os.fspath(self.project))
            if code != 0:
                self.fail("CREATE_EXIT", err.strip()[:300])
            out.add_render(payload if isinstance(payload, dict) else None)
            self._spend(self.gen.paths, out.records)

        with self.as_gen(peer):
            self.code_step(label, create)

    # -- a folder moved while holds are open ------------------------------------------------

    def ev_move_with_holds(self) -> None:
        if self.folder_ro or self.inherited_ro:
            self.trace[-1] = "move_with_holds: the checkout is read-only"
            return
        rel = self._pull_foreign()
        self.trace[-1] = "git pull delivers {}; render (a hold is open)".format(rel)
        self.step_label = self.trace[-1]
        self.ev_render()
        self.trace.append("move_folder")
        self.step_label = "move_folder"
        self.ev_move_folder()
        self.trace.append("herdr-synapse project")
        self.step_label = "herdr-synapse project"
        self.ev_project_status()

    # -- the generator ---------------------------------------------------------------------

    def allowed(self, name: str) -> bool:
        if name in self.X4_WORLD and self.folder_ro:
            return False
        return super().allowed(name)

    def dispatch(self, name: str) -> None:
        if name in {n for n, _ in self.X4_EVENTS}:
            getattr(self, "ev_" + name)()
        else:
            super().dispatch(name)


def roster_mode(gen: T.Gen) -> Optional[str]:
    from herdr_team import roster as _roster

    return _roster.load_team(gen.paths).config.get("document_sync")


class FolderInvariantNeverOverwriteTests(unittest.TestCase):
    """The same invariant, plus the four checks above, over the fourth vocabulary (``FourthWorld``)."""

    def test_no_sequence_of_the_never_overwrite_vocabulary_loses_or_misreports_a_record(self):
        steps = T._steps()
        for seed in _x4_seeds():
            with self.subTest(seed=seed):
                world = FourthWorld(seed, collect=True)
                try:
                    world.run(steps)
                finally:
                    world.close()
                if world.violations:
                    self.fail("{}\n\ndistinct failures:\n  {}".format(world.report(), "\n  ".join(T._distinct(world.violations))))


class ScriptedFourthWorld(FourthWorld):
    """A ``FourthWorld`` driven by an explicit script, at the real ``facts.md`` cap, in a plain path."""

    def __init__(self) -> None:
        super().__init__(seed=0, collect=True)
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.patchers, self.small_facts = [], False
        T._rmtree(self.tmp)
        import tempfile

        self.tmp = Path(tempfile.mkdtemp(prefix="orc-x4-")).resolve()
        self.project = self.tmp / "p0"
        self.project.mkdir()

    def label(self, text: str) -> None:
        self.trace.append(text)
        self.step_label = text

    def checks(self) -> List[str]:
        return sorted({v["check"] for v in self.violations})


class NeverOverwriteFindingScriptTests(unittest.TestCase):
    """What the fourth vocabulary found, each as the shortest script the oracle fails on (see the module docstring)."""

    def run_script(self, script: Callable[[ScriptedFourthWorld], None]) -> ScriptedFourthWorld:
        world = ScriptedFourthWorld()
        self.addCleanup(world.close)
        script(world)
        return world

    def assertOraclePasses(self, world: ScriptedFourthWorld) -> None:
        if world.violations:
            self.fail("{}\n\ndistinct failures:\n  {}".format(world.report(), "\n  ".join(T._distinct(world.violations))))

    def test_x4_1_a_markerless_edit_of_this_teams_member_document_is_said_on_the_board(self):
        """manual mode: the operator rewrites this team's members/alpha-worker.md without the marker line. The render
        holds it ``changed`` with ``quiet`` (kind EDITABLE), ``is_ours`` is false so it is not ``awaiting_adopt``, and
        the notifier skips any path in ``holds``: no line on the board ever names it, and the team's own document is
        never written again. Only ``herdr-synapse project`` lists it."""
        def script(w: ScriptedFourthWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            w.label("notifier tick")
            w.ev_daemon_tick()
            rel = "members/alpha-worker.md"
            w.label("the operator rewrites members/alpha-worker.md without the marker")
            w.world_write(rel, (w.root / rel).read_bytes().split(b"\n", 1)[1], "operator edit, marker dropped")
            w.label("notifier tick")
            w.ev_daemon_tick()
        self.assertOraclePasses(self.run_script(script))

    def test_x4_1_b_a_merge_conflict_in_this_teams_member_document_is_said_on_the_board(self):
        """The same silence for the commonest way it happens: ``git pull`` leaves conflict markers in this team's
        members/alpha-worker.md (manual mode). Its first line is ``<<<<<<< HEAD``, so it is not ``is_ours`` and the
        notifier never mentions it, and this team's own document is not written again until someone runs
        ``herdr-synapse project`` and reads the list."""
        def script(w: ScriptedFourthWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            w.label("notifier tick")
            w.ev_daemon_tick()
            rel = "members/alpha-worker.md"
            w.label("git pull leaves a merge conflict in members/alpha-worker.md")
            w.world_write(rel, w.document(rel, "conflict", "MERGE"), "merge conflict")
            w.label("notifier tick")
            w.ev_daemon_tick()
        self.assertOraclePasses(self.run_script(script))

    def test_x4_2_force_writes_a_synced_document_the_operator_deleted(self):
        """auto mode: the operator (or a checkout) deletes this team's knowledge.md. ``keep_missing`` holds it
        ``changed`` ("it was deleted after this team last wrote it") on every render, ``--force`` included, so the
        ``project render --force`` that ``herdr-synapse project`` prints for it ("write this team's version") exits 0
        and writes nothing; knowledge.md is never regenerated while the team stays in auto mode."""
        def script(w: ScriptedFourthWorld) -> None:
            w.new_gen(w.project, mode="auto", rules=None)
            w.label("notifier tick")
            w.ev_daemon_tick()
            w.label("rm knowledge.md")
            w.world_remove("knowledge.md")
            w.label("notifier tick")
            w.ev_daemon_tick()
            w.label("herdr-synapse project (its printed --force is run)")
            w.ev_project_status()
        self.assertOraclePasses(self.run_script(script))

    def test_x4_3_an_edit_document_sync_adopts_nothing_from_is_not_copied_by_itself(self):
        """auto mode: the operator adds a note above the sections of this team's knowledge.md. Its Rules and Findings are
        what this team wrote, so nothing is adopted -- but ``scan`` still records ``release`` for those bytes, and the
        next plain render copies the file into inherited/ and writes this team's version over it: a copy and an
        overwrite no operator act ordered, and the note leaves the committed file for a git-ignored one."""
        def script(w: ScriptedFourthWorld) -> None:
            w.new_gen(w.project, mode="auto", rules="the operator's rules")
            w.label("notifier tick")
            w.ev_daemon_tick()
            new = w._edit("knowledge.md", (w.root / "knowledge.md").read_bytes(), "note", "NOTE")
            w.label("the operator adds a note above the sections")
            w.world_write("knowledge.md", new, "operator note")
            w.label("notifier settle")
            w.ev_settle()
            w.label("render")
            w.ev_render()
        self.assertOraclePasses(self.run_script(script))

    def test_x4_4_a_picture_this_team_wrote_is_not_said_to_be_someone_elses_after_the_folder_moves(self):
        """``document-sync.json`` is keyed by absolute path and ``create`` never claims a file already there, so after
        ``mv`` + ``project set`` every picture this team mirrored is "a picture ... that this team did not write" and is
        never pruned."""
        def script(w: ScriptedFourthWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            w.label("draw 2 pictures")
            w.ev_draw(images=2)
            w.label("move_folder")
            w.ev_move_folder()
            w.label("canvas undo (the pictures leave the board)")
            w.ev_undo()
        self.assertOraclePasses(self.run_script(script))

    def test_x4_4_b_a_picture_whose_record_was_lost_is_not_said_to_be_someone_elses(self):
        """The same sentence after the state write fails (``document-sync.json`` cannot be saved during a draw): the
        guard rightly no longer claims the picture -- the safe direction ``FolderGuard.save`` documents -- but the line
        then says this team "did not write" it, when all it knows is that it has no record of writing it."""
        def script(w: ScriptedFourthWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            w.label("state-write-fails during draw-images")
            w.run_named("draw-images", T._HAZARDS["state-write-fails"])
            w.label("canvas undo (the picture leaves the board)")
            w.ev_undo()
        self.assertOraclePasses(self.run_script(script))

    def test_x4_5_the_printed_move_aside_never_replaces_an_earlier_aside(self):
        """A knowledge.md past the copy budget is held with ``mv <knowledge.md> <knowledge.md.aside>``. Run it, and the
        next oversized knowledge.md a pull brings is held with the same ``mv``, which silently replaces the first
        ``.aside`` -- the bytes the operator moved aside on the line's own instruction."""
        def script(w: ScriptedFourthWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            pad = DS.MAX_COPY_BYTES + 4096
            w.label("git pull delivers a knowledge.md past the copy budget; render")
            w.world_write("knowledge.md", w.document("knowledge.md", "plausible", "ONE") + b"\n" + b"x" * pad,
                          "oversized ONE")
            w.ev_render()
            w.label("the operator runs the printed mv")
            os.rename(os.fspath(w.root / "knowledge.md"), os.fspath(w.root / "knowledge.md.aside"))
            w.model.release_unrecoverable(w.disk())
            w.label("render")
            w.ev_render()
            w.label("git pull delivers another knowledge.md past the copy budget; render")
            w.world_write("knowledge.md", w.document("knowledge.md", "plausible", "TWO") + b"\n" + b"x" * pad,
                          "oversized TWO")
            w.ev_render()
            w.label("herdr-synapse project (lists the hold with its mv)")
            w.ev_project_status()
        self.assertOraclePasses(self.run_script(script))

    def test_x4_5_b_a_second_file_past_the_copy_budget_at_the_same_path_is_said_again(self):
        """A hold with no digest (``too_large``, ``unreadable``, ``symlinked``, and the ``moved`` the developer named)
        has the token ``hold:<reason>:``, so once one was boarded, a *different* file held for the same reason at the
        same path is never boarded: the operator who moved the first aside is never told a second one arrived."""
        def script(w: ScriptedFourthWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            pad = DS.MAX_COPY_BYTES + 4096
            path = os.fspath(w.root / "knowledge.md")
            w.label("git pull delivers a knowledge.md past the copy budget; notifier tick")
            w.world_write("knowledge.md", w.document("knowledge.md", "plausible", "ONE") + b"\n" + b"x" * pad,
                          "oversized ONE")
            w.ev_daemon_tick()
            w.label("the operator moves it aside; notifier tick")
            os.rename(path, path + ".aside")
            w.model.release_unrecoverable(w.disk())
            w.ev_daemon_tick()
            w.label("git pull delivers another knowledge.md past the copy budget; notifier tick")
            w.world_write("knowledge.md", w.document("knowledge.md", "plausible", "TWO") + b"\n" + b"x" * pad,
                          "oversized TWO")
            w.ev_daemon_tick()
            said = [r for r in store.BoardStore(w.gen.ts.team).read()
                    if path in str(r.get("text") or "") and "1 MiB" in str(r.get("text") or "")]
            self.assertGreaterEqual(len(said), 2, "the second oversized knowledge.md was never boarded")
        self.assertOraclePasses(self.run_script(script))

    def test_x4_5_c_a_held_picture_that_is_gone_is_not_listed_with_a_move_that_exits_1(self):
        """A picture past the copy budget lands in canvas-assets/ and the prune holds it (``too_large``, no digest). A
        checkout then deletes it. ``held_records`` re-checks a hold against the disk only when it has a digest, and the
        prune never meets a file that is not there, so ``herdr-synapse project`` keeps listing it as "left exactly as
        it is" with ``mv <it> <it>.aside``, which exits 1."""
        def script(w: ScriptedFourthWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            w.label("draw")
            w.ev_draw()
            data = b"\x89PNG\r\n\x1a\n a very large picture " + b"\x00" * (DS.MAX_COPY_BYTES + 4096)
            rel = "canvas-assets/" + T.sha(data)[:32] + ".png"
            w.label("git pull delivers a picture past the copy budget; draw (the prune holds it)")
            w.world_write(rel, data, "oversized picture")
            w.ev_draw()
            w.label("git checkout of a branch without it; draw")
            w.world_remove(rel)
            w.ev_draw()
            w.label("herdr-synapse project")
            w.ev_project_status()
        self.assertOraclePasses(self.run_script(script))

    def test_x4_6_force_over_a_file_past_the_copy_budget_changes_nothing_and_says_why(self):
        """Positive control for the developer's claim: ``--force`` over a canvas.json past the copy budget refuses."""
        def script(w: ScriptedFourthWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            w.label("draw")
            w.ev_draw()
            big = w.document("canvas.json", "plausible", "BIG") + b" " * (DS.MAX_COPY_BYTES + 4096)
            w.label("git pull delivers a canvas.json past the copy budget")
            w.world_write("canvas.json", big, "oversized canvas.json")
            w._force("project render --force")
            self.assertEqual((w.root / "canvas.json").read_bytes(), big)
        self.assertOraclePasses(self.run_script(script))

    def test_x4_7_force_with_inherited_unwritable_changes_nothing(self):
        """Positive control: ``inherited/`` is a file, ``--force`` over a previous team's knowledge.md refuses."""
        def script(w: ScriptedFourthWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            data = w.document("knowledge.md", "plausible", "PREV")
            w.label("git pull delivers a previous team's knowledge.md; inherited/ is a file")
            w.world_write("knowledge.md", data, "pull")
            T._rmtree(w.root / workdir.INHERITED_DIR_NAME)
            (w.root / workdir.INHERITED_DIR_NAME).write_bytes(b"")
            w._force("project render --force")
            self.assertEqual((w.root / "knowledge.md").read_bytes(), data)
        self.assertOraclePasses(self.run_script(script))

    def test_x4_8_force_racing_a_writer_at_the_look_keeps_the_writers_bytes(self):
        """Positive control: a writer lands between the guard's look and its copy; ``--force`` refuses (``moved``)."""
        def script(w: ScriptedFourthWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            w.label("git pull delivers a previous team's knowledge.md")
            w.world_write("knowledge.md", w.document("knowledge.md", "plausible", "PREV"), "pull")
            w._force("project render --force, racing a writer at the look", LookRace)
        self.assertOraclePasses(self.run_script(script))

    def test_x4_9_a_restored_older_version_of_this_teams_own_file_is_not_called_someone_elses(self):
        """An mtime-only change of this team's own file changes nothing, and an older version this team wrote that
        ``git checkout`` restores is held ``changed`` ("it changed after this team last wrote it", true) in manual mode.
        But the ``project render --force`` that hold prints copies it and then says the file "held bytes this team did
        not write" -- this team wrote them, one render earlier. Only the last digest is recorded, so the copy line
        cannot know; it should say what it knows ("bytes other than the ones this team last wrote")."""
        def script(w: ScriptedFourthWorld) -> None:
            w.new_gen(w.project, mode="manual", rules=None)
            w.label("render")
            w.ev_render()
            older = (w.root / "knowledge.md").read_bytes()
            w.label("team activity: a finding")
            charter.add_finding(w.gen.layout, T.TEAM, T.agent(), "a finding")
            w.ev_render()
            w.label("touch")
            w.ev_touch_ours()
            w.ev_render()
            w.label("git checkout restores the older knowledge.md")
            w.world_write("knowledge.md", older, "git checkout")
            w.label("notifier tick")
            w.ev_daemon_tick()
            self.assertEqual((w.root / "knowledge.md").read_bytes(), older)
        self.assertOraclePasses(self.run_script(script))

    def test_x4_10_another_sessions_team_of_the_same_name_does_not_set_this_teams_rules(self):
        """auto mode, two sessions, one folder, one team name. This team writes knowledge.md; the other session's team
        finds those exact bytes, which are what it would write too, and claims them (``FolderGuard.write``'s
        ``unchanged`` branch records its digest). Its operator then sets *its* rules, and its render replaces "its own"
        bytes with no copy; this team's document sync sees an edit of a document this team wrote and adopts the other
        session's rules as this team's operator Rules. No human in this session touched anything."""
        def script(w: ScriptedFourthWorld) -> None:
            w.new_gen(w.project, mode="auto", rules=None)
            w.label("notifier tick")
            w.ev_daemon_tick()
            other = T.Gen(w.take_index(), w.project, "manual", None)
            w.peers.append(other)
            w.peer = other
            w.label("another session's team of the same name: project set, then its operator sets its rules")
            with w.as_gen(other):
                w.run_cli("project", "set", os.fspath(w.project))
                w.run_cli("knowledge", "set", "the other session's rules", "--yes")
            w.label("this team's notifier settles")
            w.ev_settle()
            self.assertNotEqual(charter.get_rules(w.gen.layout, T.TEAM), "the other session's rules",
                                "another session's rules became this team's operator Rules")
        self.run_script(script)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
