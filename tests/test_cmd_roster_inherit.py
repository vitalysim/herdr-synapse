"""``create --inherit`` and the one-line offer ``create`` prints without it.

``create`` never inherits implicitly: the flag *is* the operator's opt-in, which is what keeps decision 3 ("inheritance
is never automatic") true. Without the flag, a project directory that already carries another team's record gets one
line naming the command, because otherwise the operator has to already know ``knowledge import`` exists to find out
that anything is there.
"""
from __future__ import annotations

import os
import unittest
from pathlib import Path
from unittest import mock

from support import TempState, whiteboard_on
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli

from herdr_team import canvas as C
from herdr_team import canvas_render as R
from herdr_team import charter as CH
from herdr_team import facts as F
from herdr_team import roster as RS
from herdr_team import workdir as WD
from herdr_team.identity import Author

OPERATOR = Author("human", "human", "console", True, pane_id="w7:p1", team="alpha")
MEMBER = C.CanvasAuthor("alpha-worker", "member", "cli", True, agent="claude", team="alpha")
BOARD = [
    {"op": "frame", "id": "flow", "title": "the flow", "at": [0, 0], "w": 700, "h": 400, "intent": "t"},
    {"op": "shape", "kind": "box", "text": "form", "id": "form", "at": [60, 60], "intent": "t"},
    {"op": "shape", "kind": "box", "text": "api", "id": "api", "at": [400, 60], "intent": "t"},
    {"op": "arrow", "from": "form", "to": "api", "label": "posts", "intent": "t"},
]


class InheritOnCreate(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.layout = self.ts.layout
        self.api = live_api()
        self.project = self.ts.tmp / "proj"
        self.project.mkdir()
        patcher = mock.patch.object(R, "find_resvg", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)
        whiteboard_on(self.ts.session, self.ts.team)

    def cli(self, *argv, **overrides):
        return json_out(run_cli(["--json"] + list(argv), env_no_daemon(self.ts, **overrides), self.api))

    def source_team(self) -> Path:
        """``alpha`` with rules, a fact and a four-mark board, mirrored into the project directory, then dissolved."""
        RS.update_team(self.ts.team, lambda doc: doc.config.update(project_dir=os.fspath(self.project)))
        CH.set_rules(self.layout, "alpha", OPERATOR, "DO write tests.", None)
        F.add(self.ts.team, {"statement": "the index fixed the p95", "by": "alpha-worker", "by_kind": "claude"}, "observe")
        result = C.apply_ops(self.layout, self.ts.team, list(BOARD), MEMBER)
        self.assertEqual(result["refused"], [], result["refused"])
        WD.render(self.layout, "alpha")
        WD.render_canvas_snapshot(self.layout, "alpha")
        return Path(WD.paths_for(os.fspath(self.project), "alpha")["root"])

    # --------------------------------------------------------------

    def test_create_inherit_adopts_the_rules_the_facts_and_the_canvas(self):
        folder = self.source_team()
        code, payload, err = self.cli("create", "beta", "--member", "w5:p1:reviewer", "--brief", "reviewer=Review the patch.", "--project", os.fspath(self.project),
                                      "--inherit", os.fspath(folder))
        self.assertEqual(code, 0, err)
        inherited = payload["inherited"]
        self.assertTrue(inherited["rules"]["adopted"])
        self.assertEqual(len(inherited["facts"]["added"]), 1)
        self.assertEqual(inherited["canvas"]["elements"], 4)
        self.assertEqual(CH.get_rules(self.layout, "beta"), "DO write tests.")
        self.assertEqual(len(C.load_scene(self.ts.session.team("beta"))["elements"]), 4)

    def test_inherit_turns_the_canvas_on_because_a_board_needs_somewhere_to_land(self):
        folder = self.source_team()
        from herdr_team import features

        code, payload, err = self.cli("create", "gamma", "--member", "w5:p1:reviewer", "--brief", "reviewer=Review the patch.", "--project", os.fspath(self.project),
                                      "--inherit", os.fspath(folder))
        self.assertEqual(code, 0, err)
        self.assertTrue(features.team_switch(self.ts.session, self.ts.session.team("gamma")).enabled)
        self.assertTrue(payload["inherited"]["canvas"]["imported"])

    def test_inherit_needs_a_project_directory_and_takes_one_from_the_path(self):
        folder = self.source_team()
        code, payload, err = self.cli("create", "delta", "--member", "w5:p1:reviewer", "--brief", "reviewer=Review the patch.", "--inherit", os.fspath(folder))
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["project_dir"], os.path.realpath(self.project), "the knowledge path named its own project")
        code, _payload, err = self.cli("create", "epsilon", "--member", "w5:p3:reviewer", "--brief", "reviewer=Review the patch.", "--inherit", "alpha")
        message = err["message"] if isinstance(err, dict) else err
        self.assertEqual(code, 2, message)
        self.assertIn("--project", message)

    def test_a_bad_inherit_does_not_fail_create(self):
        self.source_team()
        code, payload, err = self.cli("create", "zeta", "--member", "w5:p1:reviewer", "--brief", "reviewer=Review the patch.", "--project", os.fspath(self.project),
                                      "--inherit", os.fspath(self.ts.tmp / "nowhere"))
        self.assertEqual(code, 0, err)
        self.assertTrue(payload["created"])
        self.assertEqual(payload["inherited"]["code"], "path_invalid")
        self.assertIn("knowledge import --from", payload["inherited"]["retry"])

    def test_without_the_flag_create_offers_the_record_already_in_the_folder(self):
        self.source_team()
        code, payload, err = self.cli("create", "eta", "--member", "w5:p1:reviewer", "--brief", "reviewer=Review the patch.", "--project", os.fspath(self.project))
        self.assertEqual(code, 0, err)
        self.assertIsNone(payload["inherited"])
        self.assertEqual(len(C.load_scene(self.ts.session.team("eta"))["elements"]), 0, "nothing is adopted implicitly")
        from herdr_team import cmd_roster

        offer = cmd_roster._inherit_offer(self.project, "eta")
        self.assertIn("alpha's canvas (4 marks)", offer[0])
        self.assertIn("knowledge import --from", offer[1])

    def _previous_alpha_then_recreate(self):
        """A previous ``alpha`` with rules and a fact renders into the folder; its state dir then goes, the folder
        stays -- dissolve-and-recreate, or a colleague's checkout where this team's state never existed."""
        import shutil

        RS.update_team(self.ts.team, lambda doc: doc.config.update(project_dir=os.fspath(self.project)))
        CH.set_rules(self.layout, "alpha", OPERATOR, "DO write tests.", None)
        F.add(self.ts.team, {"statement": "the index fixed the p95", "by": "alpha-worker", "by_kind": "claude"}, "observe")
        WD.render(self.layout, "alpha")
        folder = WD.paths_for(os.fspath(self.project), "alpha")["root"]
        before = {p: p.read_bytes() for p in folder.rglob("*") if p.is_file()}
        shutil.rmtree(Path(self.ts.session.team("alpha").team_json).parent)
        return folder, before

    def test_the_offer_fires_for_a_same_named_teams_facts_still_in_place(self):
        """TRUS-5: the same-name offer counts the previous team's facts where they are, which is where they stay."""
        _folder, _before = self._previous_alpha_then_recreate()
        code, out, err = run_cli(["create", "alpha", "--member", "w5:p1:reviewer",
                                  "--brief", "reviewer=Review the patch.",
                                  "--project", os.fspath(self.project)],
                                 env_no_daemon(self.ts), self.api)
        self.assertEqual(code, 0, err)
        self.assertIn("left exactly as it is", out)
        self.assertIn("1 facts", out)
        self.assertIn("knowledge import --from", out)

    def test_create_project_names_every_held_document_and_adopts_none_of_it(self):
        """DATA-8: ``found_in_place`` names every file the folder already held, the knowledge document included,
        and nothing in it becomes this team's operator authority by itself."""
        folder, before = self._previous_alpha_then_recreate()
        code, payload, err = self.cli("create", "alpha", "--member", "w5:p1:reviewer",
                                      "--brief", "reviewer=Review the patch.",
                                      "--project", os.fspath(self.project))
        self.assertEqual(code, 0, err)
        found = {Path(row["path"]).name: row for row in payload.get("found_in_place") or []}
        self.assertIn("knowledge.md", found, payload.get("found_in_place"))
        self.assertIn("facts.md", found, payload.get("found_in_place"))
        self.assertTrue(all(row["held"] for row in found.values()), found)
        self.assertIsNone(CH.get_rules(self.layout, "alpha"))
        for path, data in before.items():
            if path.name in ("knowledge.md", "facts.md"):
                self.assertEqual(path.read_bytes(), data, path)

    def test_create_project_says_nothing_was_copied_because_nothing_was(self):
        """TRUS-1: every sentence about a found document is true of the disk: it is held, and no copy exists."""
        folder, _before = self._previous_alpha_then_recreate()
        code, out, err = run_cli(["create", "alpha", "--member", "w5:p1:reviewer",
                                  "--brief", "reviewer=Review the patch.",
                                  "--project", os.fspath(self.project)],
                                 env_no_daemon(self.ts), self.api)
        self.assertEqual(code, 0, err)
        self.assertFalse((folder / WD.INHERITED_DIR_NAME).exists())
        for gone in ("copied to", "could not be copied", "copied verbatim"):
            self.assertNotIn(gone, out)


if __name__ == "__main__":
    unittest.main()
