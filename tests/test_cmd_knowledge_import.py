"""``knowledge import``, ``create --inherit`` and the dissolve message: the owner's question, answered end to end.

The question was: "if we dissolve the team and recreate it and point the new team at the same knowledge path, do they
inherit everything the previous team did?" Before this round the answer was no for the canvas, the facts, the board and
the work items, and the dissolve message said only "archived, not deleted" -- which is why the question had to be asked
at all. These tests are the answer: one explicit command, nothing adopted behind the operator's back, and a dissolve
that names what is recoverable and the command that recovers it.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import unittest
from pathlib import Path
from typing import Any, Dict, List, Optional
from unittest import mock

from support import FAKE_MEMBERS, TempState, whiteboard_on

from herdr_team import canvas as C
from herdr_team import canvas_render as R
from herdr_team import charter as CH
from herdr_team import cmd_knowledge as CK
from herdr_team import cmd_roster as CR
from herdr_team import facts as F
from herdr_team import paths as P
from herdr_team import roster as RS
from herdr_team import store
from herdr_team import workdir as WD
from herdr_team.identity import Author

LEAD = C.CanvasAuthor("human", "human", "cli", True, operator=True)
MEMBER = C.CanvasAuthor("alpha-worker", "member", "cli", True, agent="claude", team="alpha")
OPERATOR = Author("human", "human", "console", True, pane_id="w7:p1", team="alpha")

#: Nine marks: a frame, three boxes, two bound arrows, a comment, a chart whose spec is an asset, and a label.
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
    {"op": "shape", "kind": "note", "text": "ship on friday", "at": [1200, 0], "intent": "the plan"},
]


def tree(root: Path) -> Dict[str, str]:
    """Every file under ``root`` by content hash: the archive must be exactly as it was after a read of it."""
    out: Dict[str, str] = {}
    for dirpath, _dirs, files in os.walk(root):
        for name in sorted(files):
            path = Path(dirpath) / name
            out[os.fspath(path.relative_to(root))] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


class InheritRig(unittest.TestCase):
    """One session, one project directory, and a source team with everything a team can leave behind."""

    def setUp(self) -> None:
        self.ts = TempState(members=FAKE_MEMBERS)
        self.addCleanup(self.ts.cleanup)
        self.layout = self.ts.layout
        self.project = self.ts.tmp / "proj"
        self.project.mkdir()
        patcher = mock.patch.object(R, "find_resvg", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)

    def set_project(self, team: P.TeamPaths) -> None:
        RS.update_team(team, lambda doc: doc.config.update(project_dir=os.fspath(self.project)))

    def make_team(self, name: str, members: Optional[List[Dict[str, Any]]] = None) -> P.TeamPaths:
        team = self.ts.session.team(name)
        P.ensure_team_dirs(team)
        store.write_json(team.team_json, {
            "schema": 1, "team": name, "created_at": "2026-10-01T00:00:00Z", "socket": os.fspath(self.ts.socket_path),
            "state_dir": os.fspath(self.ts.state_root), "naming": "prefixed", "revision": 1,
            "config": {"project_dir": os.fspath(self.project)},
            "charter": {"seq": 1, "text": "carry on", "refs": [], "updated_at": "2026-10-01T00:00:00Z", "updated_by": "human"},
            "members": members if members is not None else [dict(m) for m in FAKE_MEMBERS],
        })
        whiteboard_on(self.ts.session, team)
        return team

    def full_source(self, name: str = "alpha") -> P.TeamPaths:
        """A team with rules, two facts, two member documents and the nine-mark board."""
        team = self.ts.session.team(name) if self.ts.session.team(name).team_json.is_file() else self.make_team(name)
        whiteboard_on(self.ts.session, team)
        self.set_project(team)
        WD.render(self.layout, name)
        CH.set_rules(self.layout, name, OPERATOR, "DO write tests. DON'T push to master.", None)
        F.add(team, {"statement": "the login flow posts to /v2/session", "by": "alpha-worker", "by_kind": "claude"}, "observe")
        F.add(team, {"statement": "p95 latency is 240 ms after the index", "by": "alpha-reviewer", "by_kind": "codex"}, "observe")
        CH.set_instructions(self.layout, name, OPERATOR, "alpha-worker", "## Mission\nFix the login flow.\n", None)
        CH.set_instructions(self.layout, name, OPERATOR, "alpha-reviewer", "## Mission\nReview every diff.\n", None)
        result = C.apply_ops(self.layout, team, list(BOARD), MEMBER)
        self.assertEqual(result["refused"], [], result["refused"])
        WD.render(self.layout, name)
        WD.render_canvas_snapshot(self.layout, name)
        return team

    def inherit(self, team_name: str, source_spec: str, **flags: Any) -> Dict[str, Any]:
        args = argparse.Namespace(**dict({"source": source_spec, "no_rules": False, "no_facts": False, "no_canvas": False,
                                          "keep_authors": False, "skip_unknown": False, "skip_missing": False,
                                          "dry_run": False, "yes": False}, **flags))
        source = CK._resolve_source(self.layout, source_spec)
        return CK.inherit(args, self.layout, team_name, OPERATOR, source, CK.inherit_options(args, LEAD))


class TheOwnersQuestion(InheritRig):
    def test_a_recreated_team_on_the_same_knowledge_path_inherits_all_of_it(self):
        source = self.full_source()
        before = {el["id"]: el for el in C.load_scene(source)["elements"]}
        archived = RS.Roster(self.layout, "alpha").dissolve(FakeApiStub())
        archive = Path(archived["archived_to"])
        untouched = tree(archive)

        fresh = self.make_team("alpha")
        self.assertIsNone(CH.get_rules(self.layout, "alpha"))
        self.assertEqual(C.load_scene(fresh)["elements"], [])
        self.assertEqual(F.load(fresh).current(), [])

        result = self.inherit("alpha", "alpha")
        self.assertTrue(result["rules"]["adopted"])
        self.assertEqual(CH.get_rules(self.layout, "alpha"), "DO write tests. DON'T push to master.")

        self.assertEqual(len(result["facts"]["added"]), 2)
        rows = F.load(fresh).current()
        self.assertEqual([row.author for row in rows], ["alpha", "alpha"])
        self.assertEqual([row.author_kind for row in rows], [F.INHERITED_KIND, F.INHERITED_KIND])

        canvas = result["canvas"]
        self.assertTrue(canvas["imported"])
        self.assertEqual((canvas["elements"], canvas["comments"]), (8, 1))
        after = {el["id"]: el for el in C.load_scene(fresh)["elements"]}
        self.assertEqual(sorted(after), sorted(before))
        for eid, el in after.items():
            for key in ("from", "to", "on", "frame"):
                self.assertEqual(el.get(key), before[eid].get(key), "{}.{}".format(eid, key))
                if isinstance(el.get(key), str):
                    self.assertIn(el[key], after, "{}.{} resolves".format(eid, key))
        chart = next(el for el in after.values() if el["type"] == "chart")
        self.assertTrue((C._dir(fresh) / C.ASSETS_DIR / chart["spec_asset"]).is_file(), "the chart draws from a copied asset")

        self.assertEqual(sorted(row["name"] for row in result["members"] if row["mine"]),
                         ["alpha-reviewer", "alpha-worker"])
        self.assertIsNone(CH.get_instructions(self.layout, "alpha", "alpha-worker"),
                          "a member document is offered, never adopted")

        undone = C.apply_ops(self.layout, fresh, [{"op": "undo", "batch": canvas["batch"], "intent": "take it back"}], LEAD)
        self.assertEqual(undone["refused"], [], undone["refused"])
        self.assertEqual(C.load_scene(fresh)["elements"], [])
        self.assertEqual(tree(archive), untouched, "nothing in the source was changed or deleted")

    def test_a_differently_named_team_inherits_the_same_way_from_the_same_path(self):
        self.full_source()
        archived = RS.Roster(self.layout, "alpha").dissolve(FakeApiStub())
        self.make_team("alpha")
        self.make_team("gamma")
        first = self.inherit("alpha", archived["archived_to"])
        second = self.inherit("gamma", archived["archived_to"])
        for result in (first, second):
            self.assertTrue(result["rules"]["adopted"])
            self.assertEqual(len(result["facts"]["added"]), 2)
            self.assertTrue(result["canvas"]["imported"])
        self.assertEqual(len(C.load_scene(self.ts.session.team("gamma"))["elements"]), 9)
        self.assertEqual(CH.get_rules(self.layout, "gamma"), "DO write tests. DON'T push to master.")

    def test_a_differently_named_team_inherits_a_board_that_reads_a_data_file(self):
        """Criterion 2 with the one mark that broke it: a chart whose data is a file under ``artifacts/``.

        A data file is resolved against the *importing* team's own ``artifacts/``, so this board imported into
        ``gamma`` was refused (e) for a CSV in ``alpha/artifacts/`` next door and the canvas step came back with zero
        marks. A team recreated under the same name shares that folder with the team that died, which is the only
        reason the same-name round trip ever passed. The inheritance now brings the file along.
        """
        source = self.full_source()
        csv = WD.paths_for(self.project, "alpha")["artifacts"] / "metrics.csv"
        csv.parent.mkdir(parents=True, exist_ok=True)
        csv.write_text("region,p95\neu,240\nus,310\n", encoding="utf-8")
        drawn = C.apply_ops(self.layout, source, [{"op": "chart", "id": "fromfile", "title": "p95 from the file",
                                                  "type": "bar", "x": "region", "y": "p95", "data": "metrics.csv",
                                                  "at": [0, 2400], "intent": "the latency"}], MEMBER)
        self.assertEqual(drawn["refused"], [], drawn["refused"])
        WD.render(self.layout, "alpha")
        WD.render_canvas_snapshot(self.layout, "alpha")
        archived = RS.Roster(self.layout, "alpha").dissolve(FakeApiStub())
        self.make_team("gamma")

        result = self.inherit("gamma", archived["archived_to"])
        self.assertTrue(result["canvas"]["imported"], result["canvas"])
        self.assertEqual(result["artifacts"]["copied"], ["metrics.csv"], result["artifacts"])
        self.assertTrue((WD.paths_for(self.project, "gamma")["artifacts"] / "metrics.csv").is_file())
        elements = C.load_scene(self.ts.session.team("gamma"))["elements"]
        self.assertEqual(len(elements), 10)
        self.assertIn("metrics.csv", [(el.get("source") or {}).get("data") for el in elements
                                      if isinstance(el.get("source"), dict)])
        self.assertIn("metrics.csv", CK.inherit_text(result))

    def test_the_refusal_for_a_data_file_names_where_the_file_is(self):
        """The direct ``canvas import`` path has no copying step, so its refusal has to name the copy."""
        from herdr_team import canvas_import as IM

        source = self.full_source()
        csv = WD.paths_for(self.project, "alpha")["artifacts"] / "metrics.csv"
        csv.parent.mkdir(parents=True, exist_ok=True)
        csv.write_text("region,p95\neu,240\n", encoding="utf-8")
        C.apply_ops(self.layout, source, [{"op": "chart", "id": "ff", "title": "t", "type": "bar", "x": "region",
                                           "y": "p95", "data": "metrics.csv", "at": [0, 2400], "intent": "t"}], MEMBER)
        WD.render_canvas_snapshot(self.layout, "alpha")
        target = self.make_team("gamma")
        folder = WD.paths_for(self.project, "alpha")["root"]
        out = C.apply_ops(self.layout, target, [{"op": "import", "from": os.fspath(folder), "intent": "inherit"}], LEAD)
        refused = out["refused"][0]
        self.assertEqual(refused["code"], "op_invalid")
        self.assertIn("metrics.csv", refused["message"])
        self.assertIn(os.fspath(csv), refused["message"], "the message names the file it found next door")
        self.assertIn("cp ", refused["message"])
        self.assertEqual(refused["details"]["artifacts_source"],
                         os.fspath(WD.paths_for(self.project, "alpha")["artifacts"]))

    def test_the_knowledge_path_itself_is_a_source(self):
        # The project folder is the only storage that outlives the session, so it must be importable on its own.
        self.full_source()
        RS.Roster(self.layout, "alpha").dissolve(FakeApiStub())
        self.make_team("delta")
        folder = WD.paths_for(os.fspath(self.project), "alpha")["root"]
        result = self.inherit("delta", os.fspath(folder))
        self.assertEqual(result["from"]["kind"], "knowledge_path")
        self.assertTrue(result["rules"]["adopted"])
        self.assertEqual(len(result["facts"]["added"]), 2, "read back from the mirror's facts.md")
        self.assertEqual(result["canvas"]["elements"], 8)
        self.assertEqual(len(C.load_scene(self.ts.session.team("delta"))["elements"]), 9)

    def test_nothing_is_adopted_that_was_not_asked_for(self):
        self.full_source()
        RS.Roster(self.layout, "alpha").dissolve(FakeApiStub())
        fresh = self.make_team("alpha")
        result = self.inherit("alpha", "alpha", no_facts=True, no_canvas=True)
        self.assertTrue(result["rules"]["adopted"])
        self.assertIsNone(result["facts"])
        self.assertIsNone(result["canvas"])
        self.assertEqual(F.load(fresh).current(), [])
        self.assertEqual(C.load_scene(fresh)["elements"], [])

    def test_rules_this_team_already_has_are_not_replaced_without_yes(self):
        self.full_source()
        RS.Roster(self.layout, "alpha").dissolve(FakeApiStub())
        self.make_team("alpha")
        CH.set_rules(self.layout, "alpha", OPERATOR, "DO ship daily.", None)
        result = self.inherit("alpha", "alpha")
        self.assertEqual(result["rules"]["code"], "rules_present")
        self.assertIn("knowledge set", result["rules"]["reason"])
        self.assertEqual(CH.get_rules(self.layout, "alpha"), "DO ship daily.")
        again = self.inherit("alpha", "alpha", yes=True, no_canvas=True, no_facts=True)
        self.assertTrue(again["rules"]["adopted"])

    def test_a_dry_run_adopts_nothing_in_any_step(self):
        self.full_source()
        RS.Roster(self.layout, "alpha").dissolve(FakeApiStub())
        fresh = self.make_team("alpha")
        result = self.inherit("alpha", "alpha", dry_run=True)
        self.assertTrue(result["rules"]["dry_run"] and result["rules"]["would_adopt"])
        self.assertEqual(result["facts"]["would_add"], 2)
        self.assertTrue(result["canvas"]["dry_run"])
        self.assertEqual(result["canvas"]["elements"], 9)
        self.assertIsNone(CH.get_rules(self.layout, "alpha"))
        self.assertEqual(F.load(fresh).current(), [])
        self.assertEqual(C.current_version(fresh), 0)
        self.assertEqual(RS.read_board_records(fresh), [], "a dry run writes no board record either")
        text = CK.inherit_text(result)
        self.assertIn("chars would be adopted", text)
        self.assertIn("2 of 2 current facts would be attributed to alpha", text)

    def test_dry_run_counts_duplicate_source_rows_and_already_held_facts_as_the_real_run_does(self):
        """A plan must count additions, not the source bullets an operator would mistakenly expect to arrive."""
        source = self.full_source()
        folder = WD.paths_for(self.project, "alpha")["root"]
        fact_file = folder / "facts.md"
        fact_file.write_text(fact_file.read_text(encoding="utf-8") + WD.facts_body("alpha", F.mirror_rows(source)),
                             encoding="utf-8")
        target = self.make_team("beta")
        F.add(target, {"statement": F.load(source).current()[0].statement, "by": "beta-worker", "by_kind": "claude"},
              "observe")
        source_before, target_before = tree(folder), tree(target.root)
        plan = self.inherit("beta", os.fspath(folder), dry_run=True, no_rules=True, no_canvas=True)
        self.assertEqual((plan["facts"]["would_add"], plan["facts"]["of"], plan["facts"]["already_held"]), (1, 4, 3))
        self.assertIn("1 of 4 current facts would be attributed to alpha", CK.inherit_text(plan))
        self.assertIn("3 duplicate or already-held rows would not be added", CK.inherit_text(plan))
        self.assertEqual(tree(folder), source_before)
        self.assertEqual(tree(target.root), target_before, "a dry run records nothing, including bookkeeping")
        result = self.inherit("beta", os.fspath(folder), no_rules=True, no_canvas=True)
        self.assertEqual(len(result["facts"]["added"]), plan["facts"]["would_add"])
        self.assertEqual(len(F.load(target).current()), 2)
        self.assertEqual(tree(folder), source_before, "inheriting reads the source in place")

    def test_the_board_is_told_where_what_it_is_looking_at_came_from(self):
        self.full_source()
        RS.Roster(self.layout, "alpha").dissolve(FakeApiStub())
        fresh = self.make_team("alpha")
        self.inherit("alpha", "alpha")
        records = {r.get("event"): r for r in RS.read_board_records(fresh) if r.get("kind") == "system"}
        self.assertIn("canvas_imported", records)
        self.assertIn("not this team's own work", records["canvas_imported"]["text"])
        self.assertEqual(records["canvas_imported"]["to"], ["all"])
        self.assertIn("knowledge_imported", records)
        self.assertEqual(records["knowledge_imported"]["to"], ["human"])
        self.assertEqual(records["knowledge_imported"]["inherited_team"], "alpha")
        self.assertEqual(records["canvas_imported"]["from"], "system", "extra never overwrites the record's own author")

    def test_an_unresolvable_source_names_both_forms(self):
        self.make_team("alpha")
        with self.assertRaises(Exception) as caught:
            self.inherit("alpha", "no-such-team")
        message = getattr(caught.exception, "message", str(caught.exception))
        self.assertIn("--from-archive", message)
        self.assertIn("knowledge.md", message)


class WhatTheOperatorIsTold(InheritRig):
    """Two things the summary used to leave out, both of which changed what the operator believed had happened."""

    def source_with_a_missing_data_file(self) -> str:
        source = self.full_source()
        csv = WD.paths_for(self.project, "alpha")["artifacts"] / "gone.csv"
        csv.parent.mkdir(parents=True, exist_ok=True)
        csv.write_text("region,p95\neu,240\n", encoding="utf-8")
        C.apply_ops(self.layout, source, [{"op": "chart", "id": "ff", "title": "t", "type": "bar", "x": "region",
                                           "y": "p95", "data": "gone.csv", "at": [0, 2400], "intent": "t"}], MEMBER)
        WD.render_canvas_snapshot(self.layout, "alpha")
        archived = RS.Roster(self.layout, "alpha").dissolve(FakeApiStub())
        csv.unlink()  # the data file is gone from the knowledge path as well, so nothing can bring it back
        return archived["archived_to"]

    def test_a_lossy_import_says_what_it_left_out(self):
        """``--skip-missing`` dropped marks and the summary read as a plain success.

        ``canvas import``'s own result text did report the loss, so the two surfaces disagreed about the same import.
        """
        archive = self.source_with_a_missing_data_file()
        self.make_team("gamma")
        result = self.inherit("gamma", archive, skip_missing=True)
        self.assertTrue(result["canvas"]["imported"])
        self.assertTrue(result["canvas"]["skipped"]["missing_artifact"])
        text = CK.inherit_text(result)
        self.assertIn("left out (missing_artifact)", text)
        for eid in result["canvas"]["skipped"]["missing_artifact"]:
            self.assertIn(eid, text)

    def test_the_pictures_of_a_dropped_mark_are_not_copied(self):
        """A mark left out takes its pictures with it: nothing on this canvas names them."""
        archive = self.source_with_a_missing_data_file()
        target = self.make_team("gamma")
        result = self.inherit("gamma", archive, skip_missing=True)
        dropped = set(result["canvas"]["skipped"]["missing_artifact"])
        self.assertTrue(dropped)
        here = {el["id"]: el for el in C.load_scene(target)["elements"]}
        from herdr_team import canvas_import as IM

        named = set(IM.asset_names({"elements": list(here.values())}))
        copied = {path.name for path in (C._dir(target) / C.ASSETS_DIR).iterdir()} if (C._dir(target) / C.ASSETS_DIR).is_dir() else set()
        self.assertEqual(copied - named, set(), "no asset is here that no mark names")

    def test_the_summary_says_who_may_work_on_the_inherited_board(self):
        """The headline of the round: an inherited board the team cannot touch is not inherited in any useful sense.

        Re-attributing every mark to the operator is what makes the import undoable and editable *by her*; it also
        makes every member's edit, move and delete of those marks a proposal. Measured: 12 of 12 attempts by a
        member, a peer, the manager and a delegate came back ``proposed``, reason ``human_made``, and a member
        tidying a 30-mark board hit the 20-proposal limit. Neither the result text nor any document named the one
        setting that changes it.
        """
        self.full_source()
        archived = RS.Roster(self.layout, "alpha").dissolve(FakeApiStub())
        self.make_team("gamma")
        plain = CK.inherit_text(self.inherit("gamma", archived["archived_to"]))
        self.assertIn("canvas settings --human-edits live", plain)
        self.assertIn("--team-can-edit", plain)

    def test_team_can_edit_hands_the_board_to_the_team(self):
        source = self.full_source()
        archived = RS.Roster(self.layout, "alpha").dissolve(FakeApiStub())
        target = self.make_team("gamma")
        result = self.inherit("gamma", archived["archived_to"], team_can_edit=True)
        self.assertTrue(result["canvas"]["imported"])
        self.assertTrue(result["canvas"]["team_can_edit"])
        self.assertIn("the team can work on this board directly", CK.inherit_text(result))
        scene = C.load_scene(target)
        self.assertEqual((scene.get("settings") or {}).get("collab", {}).get("human_edits"), "live")
        worker = C.CanvasAuthor("alpha-worker", "member", "cli", True, agent="claude", team="gamma")
        mark = next(el["id"] for el in scene["elements"] if el["type"] not in ("comment", "frame"))
        out = C.apply_ops(self.layout, target, [{"op": "edit", "id": mark, "text": "tidied", "intent": "tidy"}], worker)
        self.assertEqual(len(out.get("applied") or []), 1, out)
        self.assertEqual(out.get("proposed") or [], [])


class AFolderThatCarriesNothing(InheritRig):
    """A folder that carries no rules or no facts really carries none.

    "That folder carries no rules" used to be confidently wrong, because the team living in the folder had usually
    regenerated a previous team's file from its own empty state. Under the never-overwrite rule a previous team's file
    is either still in place (held) or was replaced by an operator's command after a checked copy, so the plain
    sentence is true and no hint about regenerated files or copies is owed.
    """

    def empty_folder(self, name: str = "alpha") -> Path:
        """A knowledge path whose ``knowledge.md`` and ``facts.md`` are a fresh team's own empty ones."""
        team = self.make_team(name)
        self.set_project(team)
        WD.render(self.layout, name)
        return WD.paths_for(os.fspath(self.project), name)["root"]

    def test_no_rules_at_a_knowledge_path_says_so_plainly(self):
        folder = self.empty_folder()
        self.make_team("delta")
        result = self.inherit("delta", os.fspath(folder))
        self.assertFalse(result["rules"]["adopted"])
        self.assertEqual(result["rules"]["reason"], "that folder carries no rules")
        self.assertIn("  rules    not adopted", CK._inherit_text(result))

    def test_no_facts_at_a_knowledge_path_says_so_plainly(self):
        folder = self.empty_folder()
        self.make_team("delta")
        result = self.inherit("delta", os.fspath(folder))
        self.assertEqual(result["facts"]["added"], [])
        self.assertEqual(result["facts"]["reason"], "that team recorded no current facts")

    def test_a_missing_facts_md_names_the_session_archive_as_conditional(self):
        folder = self.empty_folder()
        (folder / "facts.md").unlink()
        self.make_team("delta")
        reason = self.inherit("delta", os.fspath(folder))["facts"]["reason"]
        self.assertIn("carries no readable facts.md", reason)
        self.assertIn("if the team that wrote it was dissolved in this session", reason)
        self.assertIn("knowledge import --from <that team's name>", reason)

    def test_an_archived_team_with_no_rules_really_had_none(self):
        """The hint is for a knowledge path only: an archive is the team's own record, not a mirror of it."""
        team = self.make_team("alpha")
        self.set_project(team)
        F.add(team, {"statement": "something true", "by": "alpha-worker", "by_kind": "claude"}, "observe")
        WD.render(self.layout, "alpha")
        RS.Roster(self.layout, "alpha").dissolve(FakeApiStub())
        archived = next(self.ts.session.archive_dir.glob("alpha-*"))
        self.make_team("delta")
        result = self.inherit("delta", os.fspath(archived))
        self.assertEqual(result["from"]["kind"], "team_dir")
        self.assertIn("carries no rules", result["rules"]["reason"])
        self.assertNotIn("regenerates it from its own state", result["rules"]["reason"])
        self.assertEqual(len(result["facts"]["added"]), 1, "and the facts still come from its own record")


class ThroughTheCommand(InheritRig):
    """``knowledge import`` as the operator actually types it, through argparse and ``emit``."""

    def cli(self, *argv):
        from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli

        return json_out(run_cli(["--json"] + list(argv), env_no_daemon(self.ts), live_api()))

    def test_knowledge_import_from_a_dissolved_team(self):
        self.full_source()
        RS.Roster(self.layout, "alpha").dissolve(FakeApiStub())
        self.make_team("alpha")
        code, payload, err = self.cli("knowledge", "import", "--from", "alpha", "--team", "alpha")
        self.assertEqual(code, 0, err)
        self.assertTrue(payload["rules"]["adopted"])
        self.assertEqual(payload["canvas"]["elements"], 8)
        self.assertEqual(len(C.load_scene(self.ts.session.team("alpha"))["elements"]), 9)

    def test_the_source_may_be_positional_and_a_dry_run_changes_nothing(self):
        self.full_source()
        RS.Roster(self.layout, "alpha").dissolve(FakeApiStub())
        fresh = self.make_team("alpha")
        code, payload, err = self.cli("knowledge", "import", "alpha", "--team", "alpha", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertTrue(payload["canvas"]["dry_run"])
        self.assertIsNone(CH.get_rules(self.layout, "alpha"))
        self.assertEqual(C.current_version(fresh), 0)

    def test_every_step_refused_is_the_only_time_it_exits_refused(self):
        self.full_source()
        RS.Roster(self.layout, "alpha").dissolve(FakeApiStub())
        self.make_team("alpha")
        CH.set_rules(self.layout, "alpha", OPERATOR, "DO ship daily.", None)
        code, _payload, err = self.cli("knowledge", "import", "--from", "alpha", "--team", "alpha",
                                        "--no-facts", "--no-canvas")
        self.assertEqual(code, 1, err)
        code, payload, err = self.cli("knowledge", "import", "--from", "alpha", "--team", "alpha", "--no-rules")
        self.assertEqual(code, 0, err)
        self.assertTrue(payload["canvas"]["imported"])

    def test_an_unknown_source_is_refused_by_name(self):
        self.make_team("alpha")
        code, _payload, err = self.cli("knowledge", "import", "--from", "no-such-team", "--team", "alpha")
        self.assertEqual((code, err["code"]), (1, "path_invalid"))
        self.assertIn("--from-archive", err["message"])


class DissolveSaysWhatIsRecoverable(InheritRig):
    def test_it_names_every_non_zero_count_and_both_commands(self):
        self.full_source()
        result = RS.Roster(self.layout, "alpha").dissolve(FakeApiStub())
        counts = result["recoverable"]
        self.assertEqual((counts["canvas_elements"], counts["canvas_comments"], counts["canvas_assets"]), (8, 1, 2))
        self.assertEqual(counts["facts"], 2)
        self.assertEqual(counts["rules_chars"], len("DO write tests. DON'T push to master."))
        lines = CR._recoverable_lines(counts, "alpha")
        text = "\n".join(lines)
        self.assertIn("8 canvas marks (1 comment, 2 pictures)", text)
        self.assertIn("2 current facts", text)
        self.assertIn("the operator's rules", text)
        self.assertIn("herdr-synapse knowledge import --from alpha", text)
        self.assertIn("herdr-synapse canvas import --from-archive alpha", text)
        self.assertIn("nothing was deleted", text)

    def test_a_team_that_drew_nothing_is_not_told_about_a_canvas(self):
        self.make_team("beta")
        result = RS.Roster(self.layout, "beta").dissolve(FakeApiStub())
        text = "\n".join(CR._recoverable_lines(result["recoverable"], "beta"))
        self.assertNotIn("canvas mark", text)

    def test_a_count_that_cannot_be_read_omits_its_line_and_never_fails_the_dissolve(self):
        self.full_source()
        with mock.patch.object(C, "load_scene", side_effect=OSError("no")):
            result = RS.Roster(self.layout, "alpha").dissolve(FakeApiStub())
        self.assertNotIn("canvas_elements", result["recoverable"])
        self.assertEqual(result["recoverable"]["facts"], 2)
        self.assertTrue(Path(result["archived_to"]).is_dir())

    def test_the_question_names_what_goes_into_the_archive(self):
        self.full_source()
        counts = RS.recoverable_from(self.ts.session.team("alpha").root, "alpha")
        self.assertEqual(CR._recoverable_phrase(counts), "8 canvas marks, 2 current facts, {} board posts".format(counts["posts"]))


class TheOfferWithoutTheFlag(InheritRig):
    def test_a_knowledge_path_that_carries_another_teams_record_says_so_in_one_line(self):
        self.full_source()
        lines = CR._inherit_offer(self.project, "beta")
        self.assertEqual(len(lines), 2)
        self.assertIn("alpha's canvas (9 marks)", lines[0])
        self.assertIn("2 facts", lines[0])
        self.assertIn("knowledge import --from", lines[1])

    def test_an_empty_knowledge_path_offers_nothing(self):
        self.assertEqual(CR._inherit_offer(self.project, "beta"), [])
        self.assertEqual(CR._inherit_offer(None, "beta"), [])

    def test_a_same_named_team_is_offered_the_record_in_the_folder_carrying_its_own_name(self):
        """The headline scenario, and the one the filter used to silence.

        "If I dissolve the team and create a new one pointing at the same knowledge path" is almost always the same
        name, so the directory holding the previous team's record carries *this* team's name -- and listing only
        directories whose name differs left that operator with no report and no hint at all. The wording is hedged
        because a dissolved team cannot be identified from its leftovers.
        """
        self.full_source()
        lines = CR._inherit_offer(self.project, "alpha")
        self.assertEqual(len(lines), 2, lines)
        self.assertIn("a previous team of this name", lines[0])
        self.assertIn("knowledge import --from", lines[1])


class FakeApiStub:
    """``dissolve`` only clears tokens and labels; nothing here needs a server to answer."""

    def request(self, method: str, params: Any = None) -> Dict[str, Any]:
        return {}


if __name__ == "__main__":
    unittest.main()
