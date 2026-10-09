"""``canvas import``: the inverse of ``canvas export`` (team inheritance, work item B).

The question behind the whole round was "if I dissolve a team and point a new one at the same knowledge path, do they
inherit everything the previous team did?" For the canvas the answer was no, because nothing could read a board back.
These tests are that answer turned green: the ids and every binding survive, the import is one undoable batch, it is
refused by name for every reason it cannot apply, and a second import of the same board does not double it.
"""
from __future__ import annotations

import json
import os
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

from support import FAKE_MEMBERS, TempState, whiteboard_on

from herdr_team import canvas as C
from herdr_team import canvas_import as IM
from herdr_team import canvas_render as R
from herdr_team import paths as P
from herdr_team import store
from herdr_team.errors import HerdrTeamError

LEAD = C.CanvasAuthor("human", "human", "cli", True, operator=True)
MEMBER = C.CanvasAuthor("alpha-worker", "member", "cli", True, agent="claude", team="alpha")
PEER = C.CanvasAuthor("alpha-reviewer", "member", "cli", True, agent="codex", team="alpha")
MANAGER = C.CanvasAuthor("alpha-worker", "member", "cli", True, agent="claude", manager=True, team="alpha")
DEPUTY = C.CanvasAuthor("alpha-worker", "member", "cli", True, agent="claude", operator=True, team="alpha")
#: The target team's own members: an import is refused to each of them, and the refusal has to name the right repair.
T_MEMBER = C.CanvasAuthor("beta-worker", "member", "cli", True, agent="claude", team="beta")
T_PEER = C.CanvasAuthor("beta-reviewer", "member", "cli", True, agent="codex", team="beta")
T_MANAGER = C.CanvasAuthor("beta-worker", "member", "cli", True, agent="claude", manager=True, team="beta")
T_DEPUTY = C.CanvasAuthor("beta-worker", "member", "cli", True, agent="claude", operator=True, team="beta")

#: A small board with one of everything the import has to carry: a frame, three boxes inside it, two bound arrows,
#: a comment pinned to a box, and a chart whose spec lives in an asset.
BOARD = [
    {"op": "frame", "id": "login", "title": "login flow", "at": [0, 0], "w": 900, "h": 600, "intent": "the flow"},
    {"op": "shape", "kind": "box", "text": "form", "id": "form", "at": [100, 100], "intent": "the form"},
    {"op": "shape", "kind": "box", "text": "api", "id": "api", "at": [500, 100], "intent": "the api"},
    {"op": "shape", "kind": "box", "text": "db", "id": "db", "at": [500, 350], "intent": "the store"},
    {"op": "arrow", "from": "form", "to": "api", "label": "posts", "intent": "the request"},
    {"op": "arrow", "from": "api", "to": "db", "label": "writes", "intent": "the write"},
    {"op": "comment", "at": "form", "text": "needs validation", "intent": "a question"},
    {"op": "chart", "id": "rev", "title": "p95 by region", "type": "bar", "x": "region", "y": "p95", "at": [0, 1200],
     "rows": [{"region": "eu", "p95": 240}, {"region": "us", "p95": 310}], "intent": "the latency"},
]


class ImportRig(unittest.TestCase):
    """One session with a source team and a target team, and no real resvg."""

    def setUp(self) -> None:
        self.ts = TempState(members=FAKE_MEMBERS)
        self.addCleanup(self.ts.cleanup)
        self.layout = self.ts.layout
        self.source = self.ts.team
        whiteboard_on(self.ts.session, self.source)
        self.target = self.make_team("beta")
        patcher = mock.patch.object(R, "find_resvg", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    # the rig ----------------------------------------------------------

    def make_team(self, name: str, members: Optional[List[Dict[str, Any]]] = None) -> P.TeamPaths:
        team = self.ts.session.team(name)
        P.ensure_team_dirs(team)
        store.write_json(team.team_json, {
            "schema": 1, "team": name, "created_at": "2026-10-01T00:00:00Z", "socket": os.fspath(self.ts.socket_path),
            "state_dir": os.fspath(self.ts.state_root), "naming": "prefixed", "revision": 1,
            "charter": {"seq": 1, "text": "carry on", "refs": [], "updated_at": "2026-10-01T00:00:00Z", "updated_by": "human"},
            "members": members if members is not None else [dict(m, name=m["name"].replace("alpha", name)) for m in FAKE_MEMBERS],
        })
        whiteboard_on(self.ts.session, team)
        return team

    def project_dir(self, team: P.TeamPaths) -> Path:
        from herdr_team import roster as _roster

        folder = self.ts.tmp / ("proj-" + team.name)
        folder.mkdir(exist_ok=True)
        _roster.update_team(team, lambda doc: doc.config.update(project_dir=os.fspath(folder)))
        return folder

    def draw(self, ops: List[Dict[str, Any]], author: Any = MEMBER, team: Optional[P.TeamPaths] = None) -> Dict[str, Any]:
        result = C.apply_ops(self.layout, team or self.source, list(ops), author)
        self.assertEqual(result["refused"], [], result["refused"])
        return result

    def scene(self, team: Optional[P.TeamPaths] = None) -> Dict[str, Any]:
        return C.load_scene(team or self.target)

    def write_scene(self, scene: Dict[str, Any], name: str = "scene.json", wrapper: bool = True, assets: bool = True) -> str:
        """``scene`` as an export document, with the source team's assets beside it unless a test wants them missing."""
        path = self.ts.tmp / "export" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        body = {"format": "json", "scene": scene} if wrapper else scene
        path.write_text(json.dumps(body), encoding="utf-8")
        if assets:
            folder = path.parent / "assets"
            folder.mkdir(exist_ok=True)
            for asset in IM.asset_names(scene):
                source = C._dir(self.source) / C.ASSETS_DIR / asset
                if source.is_file():
                    folder.joinpath(asset).write_bytes(source.read_bytes())
        return os.fspath(path)

    def source_document(self, ops: Optional[List[Dict[str, Any]]] = None, assets: bool = True) -> str:
        """Draw ``ops`` on the source team, and hand back the export file with its assets beside it."""
        self.draw(ops if ops is not None else BOARD)
        return self.write_scene(self.scene(self.source), assets=assets)

    def run_import(self, source: str, author: Any = LEAD, team: Optional[P.TeamPaths] = None, **fields: Any) -> Dict[str, Any]:
        op = dict({"op": "import", "from": source, "intent": "inherit the board"}, **fields)
        return C.apply_ops(self.layout, team or self.target, [op], author)

    def imported(self, source: str, **fields: Any) -> Dict[str, Any]:
        result = self.run_import(source, **fields)
        self.assertEqual(result["refused"], [], result["refused"])
        return result["applied"][0]["import"]

    def refusal(self, source: str, author: Any = LEAD, team: Optional[P.TeamPaths] = None, **fields: Any) -> Dict[str, Any]:
        result = self.run_import(source, author=author, team=team, **fields)
        self.assertEqual(result["applied"], [], result["applied"])
        self.assertTrue(result["refused"], "expected a refusal")
        return result["refused"][0]


# --------------------------------------------------------------------------
# the seams other roles call (S1, S2)


class Seams(ImportRig):
    def test_the_payload_is_a_pure_function_of_the_scene(self):
        self.draw(BOARD)
        scene = self.scene(self.source)
        first, second = IM.payload(scene), IM.payload(scene)
        self.assertEqual(first, second)
        self.assertEqual(first["payload"], IM.PAYLOAD)
        self.assertEqual(first["team"], "alpha")
        self.assertEqual(first["scene_version"], scene["version"])
        self.assertEqual(len(first["elements"]), len(scene["elements"]))
        self.assertEqual(first["counters"], {k: int(v) for k, v in scene["counters"].items()})
        self.assertEqual((first["assets_omitted"], first["assets_conflict"]), ([], []))
        for dropped in ("claims", "locks", "homes", "authors", "batches", "proposals", "freezes", "checkpoints", "settings"):
            self.assertNotIn(dropped, first, "{} is this team's live state, never the inherited board's".format(dropped))

    def test_asset_names_are_the_ones_the_carried_elements_name(self):
        self.draw(BOARD)
        scene = self.scene(self.source)
        names = IM.asset_names(scene)
        self.assertTrue(names, "the chart stores its spec as an asset")
        self.assertEqual(names, sorted(set(names)))
        for name in names:
            self.assertTrue((C._dir(self.source) / C.ASSETS_DIR / name).is_file())
        self.assertEqual(IM.asset_names({"elements": []}), [])
        self.assertEqual(IM.asset_names({}), [])

    def test_the_snapshot_listing_writes_nothing_and_reads_the_same_twice(self):
        self.draw(BOARD)
        scene = self.scene(self.source)
        folder = C._dir(self.source)
        before = sorted(p.name for p in folder.iterdir())
        text = C.snapshot_listing(self.source, scene)
        self.assertEqual(text, C.snapshot_listing(self.source, scene))
        self.assertIn("login flow", text)
        self.assertIn("E-1 frame", text)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), before, "a listing is not a read of the canvas")
        self.assertFalse((folder / C.CURSORS_DIR).exists() and any((folder / C.CURSORS_DIR).iterdir()))

    def test_the_snapshot_listing_of_an_empty_scene_says_so(self):
        self.assertIn("the canvas is empty", C.snapshot_listing(self.target, self.scene()))

    def test_the_snapshot_listing_does_not_advise_on_the_layout(self):
        """The mirror records the board; ``canvas check`` advises on it.

        Not a style preference: the layout check searches for a free spot per overlap, so it is superlinear in the
        mark count (measured: 0.018 s at 25 overlapping marks, 8.07 s at 300), and the mirror runs on the notifier
        tick that also delivers nudges and wakes. ``canvas check`` and ``look`` still report problems.
        """
        self.draw([{"op": "shape", "kind": "box", "text": "a", "at": [0, 0], "intent": "t"},
                   {"op": "shape", "kind": "box", "text": "b", "at": [20, 20], "intent": "t"}])
        scene = self.scene(self.source)
        self.assertTrue(C._check.problems(scene["elements"], C.HUMAN, None, claims=()), "the two marks do overlap")
        self.assertNotIn("problems (", C.snapshot_listing(self.source, scene))

    def test_the_snapshot_listing_stays_cheap_as_the_board_grows(self):
        """A load-tolerant ratio, never a wall-clock bound: ten times the marks must not cost ten times per mark.

        The regression guard for the tick cost above. Measured on this machine, overlapping marks, one process: the
        cost per mark was 37x worse at 250 marks than at 25 before the fix and 0.03x after it, so the gate is set at
        8x -- far above any noise a loaded machine can add to the fast case, far below anything superlinear. Each
        measurement is the best of five, which is the estimator that does not move when something else is running.
        """
        import time

        def per_mark(count: int) -> float:
            marks = [{"id": "E-{}".format(i + 1), "type": "box", "x": (i % 10) * 40, "y": (i // 10) * 40,
                      "w": 160, "h": 80, "text": "m{}".format(i), "created_seq": i + 1, "updated_seq": i + 1,
                      "author": "alpha-worker", "author_kind": "member", "style": {"size": 20}, "frame": None}
                     for i in range(count)]
            scene = dict(self.scene(self.source), elements=marks, version=count)
            best = None
            for _ in range(5):
                started = time.perf_counter()
                C.snapshot_listing(self.source, scene)
                taken = time.perf_counter() - started
                best = taken if best is None or taken < best else best
            return (best or 0.0) / count

        per_mark(25)  # warm: the first listing pays the kind-registry import
        small, large = per_mark(25), per_mark(250)
        self.assertLess(large, small * 8.0, "{:.6f} s/mark at 250 marks against {:.6f} at 25".format(large, small))


class RefusalCommands(unittest.TestCase):
    """Every command an import refusal prints must be one that runs.

    Two of them did not: refusals (f) and (g), the dry-run text and two ``canvas --help`` strings all named
    ``canvas clear``, which exits 2 with "invalid choice: 'clear'". The real verb is ``whiteboard clear``. House
    style is that a message names the command that repairs it, so a message naming a command that fails is a defect
    in the message. This reads the module's own text, so a new refusal is covered the day it is written.
    """

    def commands(self):
        import re

        source = Path(IM.__file__).read_text(encoding="utf-8")
        found = set()
        for text in re.findall(r"`\{[a-z_]*\} ([a-z][a-z -]*)", source) + re.findall(r"`herdr-synapse ([a-z][a-z -]*)", source):
            words = [word for word in text.split() if word]
            if words:
                found.add(tuple(words[:2]))
        return sorted(found)

    def test_every_command_named_in_a_refusal_parses(self):
        import argparse

        from herdr_team import cli

        registry = {}
        for command in cli.load_commands():
            for name in (command.name,) + tuple(command.aliases):
                registry[name] = command
        named = self.commands()
        self.assertTrue(named, "the module names commands in its refusals")
        for words in named:
            verb = words[0]
            self.assertIn(verb, registry, "`{}` is not a command".format(" ".join(words)))
            parser = argparse.ArgumentParser(prog=verb)
            registry[verb].add_arguments(parser)
            try:
                parser.parse_args(list(words[1:]))
            except SystemExit:
                self.fail("`herdr-synapse {}` does not parse: a refusal names a command that fails".format(" ".join(words)))


# --------------------------------------------------------------------------
# what the import does (criteria 21-24)


class WhatItDoes(ImportRig):
    def test_it_keeps_the_source_ids_and_every_binding(self):
        source = self.source_document()
        before = {el["id"]: el for el in self.scene(self.source)["elements"]}
        info = self.imported(source)
        after = {el["id"]: el for el in self.scene()["elements"]}
        self.assertEqual(sorted(after), sorted(before))
        self.assertEqual(info["elements"], 7)
        self.assertEqual(info["comments"], 1)
        for eid, el in after.items():
            for key in ("from", "to", "on", "frame"):
                self.assertEqual(el.get(key), before[eid].get(key), "{}.{}".format(eid, key))
                if isinstance(el.get(key), str):
                    self.assertIn(el[key], after, "{}.{} resolves on the board it landed on".format(eid, key))

    def test_it_reproduces_the_board_rather_than_re_arranging_it(self):
        # Measured on the owner's own 25-mark board: without this the graph block was re-laid out and ten arrows
        # re-routed, because every imported mark looks like a member just added to a container.
        source = self.source_document()
        before = {el["id"]: el for el in self.scene(self.source)["elements"]}
        self.imported(source)
        for el in self.scene()["elements"]:
            was = before[el["id"]]
            self.assertEqual([el.get(k) for k in ("x", "y", "w", "h", "points", "z")],
                             [was.get(k) for k in ("x", "y", "w", "h", "points", "z")], el["id"])

    def test_it_restamps_the_sequence_so_if_version_works(self):
        source = self.source_document()
        self.imported(source)
        version = C.look(self.layout, self.target, C.HUMAN, advance=False)["version"]
        for el in self.scene()["elements"]:
            self.assertEqual((el["created_seq"], el["updated_seq"]), (version, version))
        edited = C.apply_ops(self.layout, self.target, [{"op": "edit", "id": "E-2", "text": "the form",
                                                          "if_version": version, "intent": "rename it"}], LEAD)
        self.assertEqual(edited["refused"], [], "a mark is targetable by the version look reports")

    def test_it_seeds_the_counters_so_the_next_mark_collides_with_nothing(self):
        source = self.source_document()
        # a target whose counters survived a clear far above the imported ids
        self.draw([{"op": "shape", "kind": "box", "text": "x", "at": [0, 0], "intent": "t"}], LEAD, team=self.target)
        state = C._load_state(self.target)
        state.counters["E"] = 400
        C._write_scene(self.target, state, 0.0)
        C.clear(self.layout, self.target, C.HUMAN)
        self.assertEqual(C._load_state(self.target).counters["E"], 400)
        self.imported(source)
        drawn = self.draw([{"op": "shape", "kind": "box", "text": "new", "at": [2000, 2000], "intent": "t"}], LEAD, team=self.target)
        self.assertEqual(drawn["applied"][0]["ids"], ["E-401"])

    def test_it_is_one_undoable_batch_and_leaves_what_came_after(self):
        source = self.source_document()
        info = self.imported(source)
        later = self.draw([{"op": "shape", "kind": "box", "text": "mine", "at": [2000, 2000], "intent": "t"}], LEAD,
                          team=self.target)["applied"][0]["ids"][0]
        undone = C.apply_ops(self.layout, self.target, [{"op": "undo", "batch": info["batch"], "intent": "take it back"}], LEAD)
        self.assertEqual(undone["refused"], [], undone["refused"])
        self.assertEqual([el["id"] for el in self.scene()["elements"]], [later])

    def test_the_re_attributed_board_is_fully_editable_by_the_importer(self):
        source = self.source_document()
        self.imported(source)
        for op in ({"op": "edit", "id": "E-2", "text": "the form", "intent": "rename"},
                   {"op": "move", "id": "E-3", "by": [20, 0], "intent": "nudge"},
                   {"op": "refit", "ids": ["E-2", "E-3"], "intent": "size again"},
                   {"op": "delete", "id": "E-4", "intent": "drop it"}):
            with self.subTest(op=op["op"]):
                result = C.apply_ops(self.layout, self.target, [op], LEAD)
                self.assertEqual((result["refused"], result["proposed"]), ([], []), result)
        for el in self.scene()["elements"]:
            self.assertEqual(el["author"], C.HUMAN)
            self.assertEqual(el["imported"]["team"], "alpha")
            self.assertIn(el["imported"]["author"], ("alpha-worker", C.HUMAN))

    def test_the_re_attributed_board_is_the_operators_and_the_result_says_so(self):
        """The half of criterion 24 that was not asked: can the *team* work on what it inherited?

        It could not, and nothing said so. Re-attribution is what makes the import undoable and editable by the
        operator; with the default ``human_edits: propose`` it also turns every edit, move and delete of those marks
        by a member, the manager or a delegate into a proposal (measured: 12 of 12, reason ``human_made``), and a
        member tidying a 30-mark board is hard-stopped at the 20-proposal limit. One line, naming the one knob.
        """
        info = self.imported(self.source_document())
        self.assertFalse(info["team_can_edit"])
        text = IM.result_text(info)
        self.assertIn("canvas settings --human-edits live", text)
        self.assertIn("--team-can-edit", text)
        out = C.apply_ops(self.layout, self.target,
                          [{"op": "edit", "id": "E-2", "text": "theirs", "intent": "tidy"}], T_MEMBER)
        self.assertEqual(len(out["proposed"]), 1, out)
        self.assertEqual(out["proposed"][0]["reason"], "human_made")

    def test_team_can_edit_lets_the_team_work_on_the_board_it_inherited(self):
        info = self.imported(self.source_document(), team_can_edit=True)
        self.assertTrue(info["team_can_edit"])
        self.assertIn("the team can work on this board directly", IM.result_text(info))
        self.assertEqual((self.scene().get("settings") or {}).get("collab", {}).get("human_edits"), "live")
        for op, who in (({"op": "edit", "id": "E-2", "text": "theirs", "intent": "tidy"}, T_MEMBER),
                        ({"op": "move", "id": "E-3", "by": [20, 0], "intent": "tidy"}, T_PEER),
                        ({"op": "delete", "id": "E-4", "intent": "drop it"}, T_MANAGER)):
            with self.subTest(op=op["op"]):
                out = C.apply_ops(self.layout, self.target, [op], who)
                self.assertEqual((out["refused"], out["proposed"]), ([], []), out)

    def test_team_can_edit_is_taken_back_with_the_import(self):
        """One batch, so ``undo`` cannot leave the team editing the operator's marks on a board that is gone."""
        info = self.imported(self.source_document(), team_can_edit=True)
        undone = C.apply_ops(self.layout, self.target, [{"op": "undo", "batch": info["batch"], "intent": "back"}], LEAD)
        self.assertEqual(undone["refused"], [], undone["refused"])
        self.assertEqual(self.scene()["elements"], [])
        self.assertEqual((self.scene().get("settings") or {}).get("collab", {}).get("human_edits"), "propose")

    def test_team_can_edit_with_keep_authors_is_refused_by_name(self):
        found = self.refusal(self.source_document(), team_can_edit=True, keep_authors=True)
        self.assertEqual((found["code"], found["details"]["field"]), ("op_invalid", "team_can_edit"))
        self.assertIn("--keep-authors", found["message"])
        self.assertIn("Pass one of them", found["message"])

    def test_a_dry_run_says_who_will_be_able_to_work_on_it(self):
        from herdr_team.cmd_canvas import dry_run

        source = self.source_document()
        doc = store.read_json(self.target.team_json, default={}) or {}
        plain = IM.dry_run_text(dry_run(self.layout, self.target, doc, LEAD, {"op": "import", "from": source}))
        self.assertIn("canvas settings --human-edits live", plain)
        shared = IM.dry_run_text(dry_run(self.layout, self.target, doc, LEAD,
                                         {"op": "import", "from": source, "team_can_edit": True}))
        self.assertIn("the team can work on this board directly", shared)

    def test_a_member_importing_is_refused_before_any_file_is_read(self):
        missing = os.fspath(self.ts.tmp / "nowhere.json")
        found = self.refusal(missing, author=T_MEMBER)
        self.assertEqual(found["code"], "operator_only")
        self.assertIn("the operator in person", found["message"])


# --------------------------------------------------------------------------
# --keep-authors (criterion 25)


class KeepAuthors(ImportRig):
    def test_it_is_refused_to_a_member_and_to_the_manager_naming_the_repair(self):
        source = self.source_document()
        for author in (T_MEMBER, T_MANAGER, T_DEPUTY):
            with self.subTest(author=author.name, manager=author.manager, operator=author.operator):
                found = self.refusal(source, author=author, keep_authors=True)
                self.assertEqual(found["code"], "operator_only")
                self.assertIn("--keep-authors", found["message"])
                self.assertIn("import it as their own work", found["message"])
                self.assertIn("edit marks or undo the batch with either choice", found["message"])

    def test_the_operator_keeps_the_names_and_can_edit_individually_or_undo(self):
        self.draw([{"op": "shape", "kind": "box", "text": "theirs", "at": [0, 0], "intent": "t"}], LEAD)
        source = self.source_document()
        info = self.imported(source, keep_authors=True)
        authors = {el["id"]: el["author"] for el in self.scene()["elements"]}
        self.assertIn("alpha-worker", authors.values())
        self.assertIn(C.HUMAN, authors.values(), "the operator's old marks map to human")
        self.assertEqual(info["authors"], "keep")
        self.assertIn("the operator can edit marks or undo {}".format(info["batch"]), IM.result_text(info))
        mark = next(el["id"] for el in self.scene()["elements"] if el.get("text") == "form")
        edited = self.draw([{"op": "edit", "id": mark, "text": "operator edit", "intent": "rename"}],
                           LEAD, team=self.target)
        self.assertEqual(len(edited["applied"]), 1)
        self.assertEqual(next(el for el in self.scene()["elements"] if el["id"] == mark)["text"], "operator edit")

    def test_a_system_authored_mark_is_refused(self):
        source = self.source_document()
        document = json.loads(Path(source).read_text(encoding="utf-8"))
        document["scene"]["elements"][1]["author_kind"] = C.KIND_SYSTEM
        Path(source).write_text(json.dumps(document), encoding="utf-8")
        found = self.refusal(source, keep_authors=True)
        self.assertEqual(found["code"], "author_mismatch")
        self.assertIn("import without --keep-authors", found["message"])


# --------------------------------------------------------------------------
# every refusal, by name, with its repair (criterion 26)


class Refusals(ImportRig):
    def file(self, body: Any, name: str = "doc.json") -> str:
        path = self.ts.tmp / name
        path.write_text(json.dumps(body), encoding="utf-8")
        return os.fspath(path)

    def test_a_not_a_scene(self):
        found = self.refusal(self.file({"hello": "world"}))
        self.assertEqual((found["code"], found["details"]["field"]), ("op_invalid", "from"))
        self.assertIn("canvas export --format json", found["message"])

    def test_a_damaged_document_is_told_apart_from_the_wrong_kind_of_file(self):
        """Truncated JSON and invalid UTF-8 used to read as "you passed the wrong kind of file".

        The repair is the opposite one: the right file is broken, so the answer is the other copy -- which matters
        most when the source is a dissolved team's archive that cannot be re-exported. A file that does not even
        begin as a JSON object is still the wrong kind of file.
        """
        whole = json.dumps({"format": "json", "scene": {"v": C.SCHEMA, "team": "alpha", "version": 3,
                                                        "elements": [], "counters": {}}})
        cut = self.ts.tmp / "cut.json"
        cut.write_text(whole[:len(whole) // 2], encoding="utf-8")
        found = self.refusal(os.fspath(cut))
        self.assertIn("is damaged", found["message"])
        self.assertIn("stops mid-JSON", found["message"])
        self.assertIn("whiteboard/archive", found["message"])
        self.assertNotIn("is not a canvas scene", found["message"])

        raw = self.ts.tmp / "bytes.json"
        raw.write_bytes(b'{"team": "\xff\xfe"}')
        found = self.refusal(os.fspath(raw))
        self.assertIn("is damaged", found["message"])
        self.assertIn("not UTF-8", found["message"])

        prose = self.ts.tmp / "notes.md"
        prose.write_text("# these are my notes, not a board\n", encoding="utf-8")
        found = self.refusal(os.fspath(prose))
        self.assertIn("is not a canvas scene", found["message"])

    def test_a_a_directory_with_no_canvas_in_it(self):
        folder = self.ts.tmp / "empty"
        folder.mkdir()
        found = self.refusal(os.fspath(folder))
        self.assertEqual(found["code"], "op_invalid")
        self.assertIn("whiteboard/scene.json", found["message"])

    def test_b_a_newer_schema(self):
        found = self.refusal(self.file({"v": C.SCHEMA + 1, "team": "alpha", "version": 3, "elements": []}))
        self.assertEqual(found["code"], "op_invalid")
        self.assertIn("update herdr-synapse", found["message"])

    def test_c_a_mark_a_newer_build_stored(self):
        self.draw(BOARD)
        scene = self.scene(self.source)
        for el in scene["elements"]:
            if el["type"] == "box":
                el["kv"] = 99
                break
        found = self.refusal(self.write_scene(scene))
        self.assertEqual(found["code"], "op_invalid")
        self.assertIn("newer build", found["message"])
        self.assertIn("update herdr-synapse", found["message"])

    def test_d_an_unknown_kind_is_refused_not_warned(self):
        # An unknown kind lands as an invisible empty rect with no exception, so a warning would leave blank boxes.
        self.draw(BOARD)
        scene = self.scene(self.source)
        scene["elements"][1]["type"] = "quantum"
        source = self.write_scene(scene)
        found = self.refusal(source)
        self.assertEqual(found["code"], "op_invalid")
        self.assertIn("--skip-unknown", found["message"])
        self.assertIn("quantum", found["message"])
        info = self.imported(source, skip_unknown=True)
        self.assertEqual(info["skipped"]["unknown"], ["E-2"])
        self.assertNotIn("E-2", {el["id"] for el in self.scene()["elements"]})

    def test_e_a_dataset_that_is_not_under_artifacts(self):
        self.project_dir(self.target)
        self.draw([{"op": "viz", "id": "v", "title": "t", "libs": ["d3"], "html": "<p>x</p>", "intent": "t"}])
        scene = self.scene(self.source)
        scene["elements"][0]["data_path"] = "missing.csv"
        source = self.write_scene(scene)
        found = self.refusal(source)
        self.assertEqual(found["code"], "op_invalid")
        self.assertIn("missing.csv", found["message"])
        self.assertIn("--skip-missing", found["message"])
        self.assertEqual(self.imported(source, skip_missing=True)["skipped"]["missing_artifact"], ["E-1"])

    def test_f_a_board_over_the_element_cap(self):
        big = {"v": C.SCHEMA, "team": "alpha", "version": 1,
               "elements": [{"id": "E-{}".format(n), "type": "box", "x": n, "y": 0, "w": 10, "h": 10, "text": "",
                             "style": {}, "author": "alpha-worker", "author_kind": "member"}
                            for n in range(1, C.MAX_ELEMENTS + 2)]}
        found = self.refusal(self.write_scene(big, wrapper=False))
        self.assertEqual(found["code"], "canvas_limit")
        self.assertIn("whiteboard clear", found["message"])

    def test_g_a_canvas_that_already_holds_marks(self):
        source = self.source_document()
        self.draw([{"op": "shape", "kind": "box", "text": "mine", "at": [0, 0], "intent": "t"}], LEAD, team=self.target)
        found = self.refusal(source)
        self.assertEqual(found["code"], "canvas_refused")
        self.assertIn("canvas checkpoint", found["message"])
        self.assertIn("whiteboard clear", found["message"])

    def test_h_an_import_by_a_member(self):
        found = self.refusal(self.source_document(), author=T_PEER)
        self.assertEqual(found["code"], "operator_only")
        self.assertIn("a comment or a proposal", found["message"])

    def test_i_a_picture_the_import_does_not_carry(self):
        source = self.source_document(assets=False)
        found = self.refusal(source)
        self.assertEqual(found["code"], "op_invalid")
        self.assertIn("whiteboard/assets/", found["message"])
        self.assertIn("--skip-missing", found["message"])

    def test_j_an_asset_whose_bytes_are_not_what_its_name_says(self):
        source = self.source_document()
        folder = Path(source).parent / "assets"
        name = sorted(p.name for p in folder.iterdir())[0]
        folder.joinpath(name).write_bytes(b'{"not": "the spec that hashes to this name"}')
        found = self.refusal(source)
        self.assertEqual((found["code"], found["details"]["field"]), ("op_invalid", "assets"))
        self.assertIn("do not import it", found["message"])
        self.assertFalse((C._dir(self.target) / C.ASSETS_DIR / name).exists(), "a damaged asset is never copied")

    def test_k_the_same_board_twice(self):
        source = self.source_document()
        info = self.imported(source)
        found = self.refusal(source)
        self.assertEqual(found["code"], "canvas_refused")
        self.assertIn(info["batch"], found["message"])
        self.assertIn("canvas changes --since", found["message"])

    def test_l_if_version(self):
        found = self.refusal(self.source_document(), if_version=1)
        self.assertEqual((found["code"], found["details"]["field"]), ("op_invalid", "if_version"))
        self.assertIn("no if_version", found["message"])

    def test_m_the_mirrors_pointer_form(self):
        pointer = {"//": "generated", "payload": IM.PAYLOAD, "v": C.SCHEMA, "team": "alpha", "scene_version": 90,
                   "elements": None, "element_count": 2400,
                   "too_large": {"bytes": 2 * 1024 * 1024, "cap": 1024 * 1024,
                                 "export": "herdr-synapse canvas export --team alpha --format json --out alpha-canvas.json",
                                 "import": "herdr-synapse canvas import --from alpha-canvas.json"}}
        found = self.refusal(self.file(pointer, "canvas.json"))
        self.assertEqual(found["code"], "op_invalid")
        self.assertIn("is a pointer, not a scene", found["message"])
        self.assertIn("canvas export --team alpha --format json", found["message"])
        self.assertIn("2400", found["message"])

    def test_n_keep_authors_by_someone_who_is_not_the_operator(self):
        found = self.refusal(self.source_document(), author=T_MEMBER, keep_authors=True)
        self.assertEqual(found["code"], "operator_only")
        self.assertIn("Drop --keep-authors", found["message"])

    def test_every_refusal_message_names_a_command_or_the_repair(self):
        # A refusal in this codebase says what is wrong and then how to repair it. This is the whole-table guard.
        self.draw(BOARD)
        scene = self.scene(self.source)
        document = IM.Document(path=Path("x.json"), form="scene", scene=scene, assets_dir=None, digest="d", bytes=1,
                               source={"team": "alpha", "version": 1, "path": "x.json", "form": "scene", "stamp": None})
        report = IM.validate(document, self.scene(), {}, None, {"author": LEAD, "prior": None})
        self.assertTrue(report.refusals)
        for refusal in report.refusals:
            self.assertTrue(refusal.code and refusal.message, refusal)
            self.assertRegex(refusal.message, r"(herdr-synapse |--skip-|--keep-authors|do not import it)")


# --------------------------------------------------------------------------
# --dry-run and idempotence (criteria 27, 28)


class DryRunAndIdempotence(ImportRig):
    def snapshot(self, folder: Path) -> Dict[str, bytes]:
        out: Dict[str, bytes] = {}
        for dirpath, _dirs, files in os.walk(folder):
            for name in sorted(files):
                path = Path(dirpath) / name
                out[os.fspath(path.relative_to(folder))] = path.read_bytes()
        return out

    def test_a_dry_run_writes_nothing_at_all(self):
        source = self.source_document()
        folder = C._dir(self.target)
        folder.mkdir(parents=True, exist_ok=True)
        before = self.snapshot(folder)
        document = IM.read_document(source)
        report = IM.validate(document, self.scene(), {}, None,
                             {"author": LEAD, "prior": IM.prior_import(self.target, document.digest),
                              "target_assets": folder / C.ASSETS_DIR})
        self.assertEqual(report.refusals, [])
        self.assertEqual(len(report.elements), 8)
        self.assertEqual(self.snapshot(folder), before, "no lock, no asset, no event, no scene")
        self.assertEqual(C.current_version(self.target), 0)

    def test_a_dry_run_reports_the_same_refusals_the_op_would_raise(self):
        self.draw(BOARD)
        scene = self.scene(self.source)
        scene["elements"][1]["type"] = "quantum"
        source = self.write_scene(scene)
        self.draw([{"op": "shape", "kind": "box", "text": "mine", "at": [0, 0], "intent": "t"}], LEAD, team=self.target)
        document = IM.read_document(source)
        report = IM.validate(document, self.scene(), {}, None, {"author": LEAD, "prior": None})
        codes = {refusal.letter: refusal.code for refusal in report.refusals}
        self.assertEqual(codes.get("g"), "canvas_refused")
        self.assertEqual(codes.get("d"), "op_invalid")
        self.assertEqual(self.refusal(source)["code"], report.refusals[0].code)
        self.assertIn("refused", IM.dry_run_text(report))

    def test_a_re_import_reports_what_is_already_here_rather_than_doubling_the_board(self):
        self.draw(BOARD)
        scene = self.scene(self.source)
        scene["elements"][1]["type"] = "quantum"
        source = self.write_scene(scene)
        self.imported(source, skip_unknown=True)
        document = IM.read_document(source)
        report = IM.validate(document, self.scene(), {}, None,
                             {"author": LEAD, "prior": IM.prior_import(self.target, document.digest)})
        self.assertEqual(len(report.already), 7)
        self.assertEqual(len(report.fresh), 1, "the mark skip-unknown left out is the only new one")
        self.assertIn("7 already here, 1 new", IM.dry_run_text(report))
        self.assertEqual(len(self.scene()["elements"]), 7, "the board did not double")

    def test_an_import_whose_batch_was_undone_may_be_imported_again(self):
        source = self.source_document()
        info = self.imported(source)
        C.apply_ops(self.layout, self.target, [{"op": "undo", "batch": info["batch"], "intent": "take it back"}], LEAD)
        again = self.imported(source)
        self.assertEqual(again["elements"], info["elements"], "a re-import after an undo is the repair, not a duplicate")


# --------------------------------------------------------------------------
# the three things called "archive" (criteria 29-31)


class Archives(ImportRig):
    def archive(self, team_name: str, stamp: str) -> Path:
        """A dissolved team of ``team_name`` in this session's archive, with a board in it."""
        team = self.make_team(team_name) if not self.ts.session.team(team_name).team_json.is_file() else self.ts.session.team(team_name)
        self.draw([{"op": "shape", "kind": "box", "text": team_name + " " + stamp, "at": [0, 0], "intent": "t"}],
                  LEAD, team=team)
        destination = self.ts.session.archive_dir / "{}-{}".format(team_name, stamp)
        P.ensure_dir(self.ts.session.archive_dir)
        os.rename(team.root, destination)
        return destination

    def test_an_exact_stamp_wins_and_several_are_never_guessed_between(self):
        self.archive("beta", "20260901T093000Z")
        self.archive("beta", "20261001T120000Z")
        found = IM.dissolved(self.ts.session, "beta")
        self.assertEqual([entry["stamp"] for entry in found], ["20261001T120000Z", "20260901T093000Z"])
        exact = IM.resolve_archive(self.ts.session, "beta@20260901T093000Z")
        self.assertEqual(exact["stamp"], "20260901T093000Z")
        with self.assertRaises(HerdrTeamError) as caught:
            IM.resolve_archive(self.ts.session, "beta")
        self.assertIn("was dissolved 2 times", caught.exception.message)
        self.assertLess(caught.exception.message.index("20261001T120000Z"), caught.exception.message.index("20260901T093000Z"),
                        "newest first")
        self.assertEqual(IM.resolve_archive(self.ts.session, "beta", latest=True)["stamp"], "20261001T120000Z")

    def test_a_prune_dump_is_never_offered_as_a_dissolved_team(self):
        pruned = self.ts.session.archive_dir / "beta-prune-20261001T120000Z"
        P.ensure_dir(pruned)
        (pruned / "board.1-9.jsonl").write_text("{}\n", encoding="utf-8")
        self.assertEqual(IM.dissolved(self.ts.session, "beta"), [])
        with self.assertRaises(HerdrTeamError) as caught:
            IM.resolve_archive(self.ts.session, "beta-prune-20261001T120000Z")
        self.assertIn("pruned board dump", caught.exception.message)

    def test_another_teams_name_is_never_matched_by_a_prefix(self):
        self.make_team("alpha-beta")
        self.archive("alpha-beta", "20261001T120000Z")
        self.assertEqual(IM.dissolved(self.ts.session, "alpha"), [])
        self.assertEqual(len(IM.dissolved(self.ts.session, "alpha-beta")), 1)

    def test_a_team_dissolved_elsewhere_says_where_to_look(self):
        with self.assertRaises(HerdrTeamError) as caught:
            IM.resolve_archive(self.ts.session, "gamma")
        self.assertIn("another Herdr session", caught.exception.message)
        self.assertIn("whiteboard/scene.json", caught.exception.message)

    def test_reading_an_archive_leaves_it_byte_identical_and_writes_nothing_into_it(self):
        destination = self.archive("gamma", "20261001T120000Z")
        before = {os.fspath(p.relative_to(destination)): p.read_bytes() for p in destination.rglob("*") if p.is_file()}
        info = self.imported(os.fspath(destination), stamp="20261001T120000Z")
        self.assertEqual(info["elements"], 1)
        after = {os.fspath(p.relative_to(destination)): p.read_bytes() for p in destination.rglob("*") if p.is_file()}
        self.assertEqual(after, before)
        self.assertFalse(self.ts.session.team("gamma").team_json.is_file(), "a dissolved team is not resurrected")

    def test_a_canvas_lost_to_clear_is_recoverable_from_its_own_archive(self):
        self.draw(BOARD)
        version = C.current_version(self.source)
        C.clear(self.layout, self.source, C.HUMAN)
        self.assertEqual(self.scene(self.source)["elements"], [])
        archives = sorted((C._dir(self.source) / C.ARCHIVE_DIR).iterdir())
        self.assertTrue(archives, "clear archives the scene it cleared")
        info = self.imported(os.fspath(archives[-1]))
        self.assertEqual(info["elements"], 7)
        self.assertEqual(info["version"], version)

    def test_the_listing_says_what_each_copy_actually_holds(self):
        self.archive("delta", "20261001T120000Z")
        summary = IM.archive_summary(IM.dissolved(self.ts.session, "delta")[0], "delta")
        self.assertEqual((summary["elements"], summary["comments"], summary["empty"]), (1, 0, False))
        line = IM.archive_line(summary, "delta")
        self.assertIn("canvas import --from-archive delta@20261001T120000Z", line)
        self.assertIn("1 mark", line)
        self.assertIn("2026-10-01 12:00", line)


# --------------------------------------------------------------------------
# the mirror's canvas.json, read back (criterion 16's second half)


class FromTheMirror(ImportRig):
    def test_a_mirrored_canvas_json_imports_whole(self):
        from herdr_team import workdir as WD

        folder = self.project_dir(self.source)
        self.draw(BOARD)
        WD.render(self.layout, "alpha")
        snapshot = WD.render_canvas_snapshot(self.layout, "alpha")
        self.assertIsNone(snapshot.get("reason"), snapshot)
        team_folder = WD.paths_for(os.fspath(folder), "alpha")["root"]
        info = self.imported(os.fspath(team_folder))
        self.assertEqual((info["elements"], info["comments"]), (7, 1))
        self.assertEqual(info["form"], "mirror")
        self.assertEqual(info["assets"], 2, "the chart's spec and data come from canvas-assets/")
        for name in IM.asset_names(self.scene()):
            self.assertTrue((C._dir(self.target) / C.ASSETS_DIR / name).is_file())


# --------------------------------------------------------------------------
# what it reads back as


class ReadsBackAs(ImportRig):
    def test_the_result_text_and_the_change_line(self):
        source = self.source_document()
        result = self.run_import(source)
        text = C.apply_text(result)
        self.assertIn("imported 7 marks and 1 comment from alpha", text)
        self.assertIn("2 pictures copied", text)
        self.assertIn("undo B-1 to take it back", text)
        events = C.changes_since(self.target, 0)["events"]
        self.assertEqual(C.summarize(events[-1]), "imported 7 marks from alpha (v{})".format(
            json.loads(Path(source).read_text(encoding="utf-8"))["scene"]["version"]))

    def test_a_skipped_mark_is_named_in_the_result(self):
        self.draw(BOARD)
        scene = self.scene(self.source)
        scene["elements"][1]["type"] = "quantum"
        info = self.imported(self.write_scene(scene), skip_unknown=True, skip_missing=True)
        self.assertIn("E-2 left out (not drawn by this build)", IM.result_text(info))


# --------------------------------------------------------------------------
# the counts a dissolve can promise (criterion 36's reader half)


class Recoverable(ImportRig):
    def test_it_counts_what_is_actually_in_the_directory(self):
        from herdr_team import facts as _facts

        self.draw(BOARD)
        _facts.add(self.source, {"statement": "the index fixed the p95", "by": "alpha-worker", "by_kind": "claude"}, "observe")
        self.source.rules_md.write_text("DO write tests.\n", encoding="utf-8")
        counts = IM.recoverable(self.source)
        self.assertEqual(counts["canvas_elements"], 7)
        self.assertEqual(counts["canvas_comments"], 1)
        self.assertEqual(counts["canvas_assets"], 2)
        self.assertEqual(counts["facts"], 1)
        self.assertEqual(counts["rules_chars"], len("DO write tests."))

    def test_a_directory_with_nothing_in_it_promises_nothing(self):
        self.assertEqual(IM.recoverable(P.TeamPaths(self.ts.tmp / "gone", "gone")), {})


if __name__ == "__main__":
    unittest.main()
