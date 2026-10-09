"""The ``canvas import`` command surface, and the one lie in the command it is the inverse of.

``canvas export --format json --region R`` parsed the region and exported the whole board anyway (the region reached
only the ``md`` and render branches). A payload that says it is a region and is not would be imported as the whole
board, which is why that fix belongs in this round and in this file.
"""
from __future__ import annotations

import json
import os

from support import FAKE_AGENTS, whiteboard_on
from test_canvas import OPERATOR, CanvasRig
from test_cmd_roster import env_no_daemon, json_out, live_api, run_cli

from herdr_team import canvas as C
from herdr_team import canvas_import as IM
from herdr_team import paths as P
from herdr_team import store

BOARD = [
    {"op": "frame", "id": "flow", "title": "the flow", "at": [0, 0], "w": 700, "h": 400, "intent": "t"},
    {"op": "shape", "kind": "box", "text": "form", "id": "form", "at": [60, 60], "intent": "t"},
    {"op": "shape", "kind": "box", "text": "api", "id": "api", "at": [400, 60], "intent": "t"},
    {"op": "arrow", "from": "form", "to": "api", "label": "posts", "intent": "t"},
]


class ImportCli(CanvasRig):
    """The source board lives on ``alpha``; the import runs against a second team in the same session."""

    def setUp(self):
        super().setUp()
        self.api = live_api(list(FAKE_AGENTS))
        self.target = self.make_team("beta")

    def cli(self, argv, pane=None):
        overrides = {"HERDR_PANE_ID": pane} if pane else {}
        return json_out(run_cli(["--json"] + list(argv), env_no_daemon(self.ts, **overrides), self.api))

    def worker(self, *argv):
        return self.cli(argv, pane="w2:p2")

    def human(self, *argv):
        return self.cli(argv)

    def make_team(self, name):
        team = self.ts.session.team(name)
        P.ensure_team_dirs(team)
        store.write_json(team.team_json, {
            "schema": 1, "team": name, "created_at": "2026-10-01T00:00:00Z", "socket": os.fspath(self.ts.socket_path),
            "state_dir": os.fspath(self.ts.state_root), "naming": "prefixed", "revision": 1,
            "charter": {"seq": 1, "text": "carry on", "refs": [], "updated_at": "2026-10-01T00:00:00Z", "updated_by": "human"},
            "members": [{"name": "human", "role": "operator", "kind": "human", "terminal_id": None, "status": "active"}],
        })
        whiteboard_on(self.ts.session, team)
        return team

    def source_file(self):
        self.apply(BOARD, OPERATOR)
        path = self.ts.tmp / "board.json"
        path.write_text(json.dumps({"format": "json", "scene": C.load_scene(self.team)}), encoding="utf-8")
        return os.fspath(path)

    # --------------------------------------------------------------

    def test_the_import_command_applies_one_batch_and_prints_what_came_in(self):
        source = self.source_file()
        code, payload, err = self.human("canvas", "import", "--from", source, "--team", "beta")
        self.assertEqual(code, 0, err)
        self.assertEqual(payload["applied"][0]["import"]["elements"], 4)
        self.assertEqual(len(C.load_scene(self.target)["elements"]), 4)

    def test_a_dry_run_writes_nothing_and_says_what_it_would_do(self):
        source = self.source_file()
        code, payload, err = self.human("canvas", "import", "--from", source, "--team", "beta", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertEqual((payload["dry_run"], payload["would_apply"], payload["elements"]), (True, True, 4))
        self.assertEqual(payload["refusals"], [])
        self.assertEqual(C.current_version(self.target), 0, "a dry run leaves the canvas at v0")
        self.assertEqual(len(C.load_scene(self.target)["elements"]), 0)

    def test_a_dry_run_carries_the_refusals_the_real_run_would_raise(self):
        source = self.source_file()
        self.human("canvas", "import", "--from", source, "--team", "beta")
        code, payload, err = self.human("canvas", "import", "--from", source, "--team", "beta", "--dry-run")
        self.assertEqual(code, 0, err)
        self.assertFalse(payload["would_apply"])
        self.assertEqual([r["refusal"] for r in payload["refusals"]], ["k", "g"])
        self.assertEqual([r["code"] for r in payload["refusals"]], ["canvas_refused", "canvas_refused"])

    def test_the_two_sources_are_exclusive_and_list_needs_an_archive(self):
        source = self.source_file()
        for argv, field in ((["canvas", "import", "--team", "beta"], "import --from"),
                            (["canvas", "import", "--from", source, "--list", "--team", "beta"], "--list goes with")):
            code, _payload, err = self.human(*argv)
            self.assertEqual(code, 2, err)
            self.assertIn(field, err["message"] if isinstance(err, dict) else err)

    def test_list_says_what_is_recoverable_from_each_dissolved_copy(self):
        self.apply(BOARD, OPERATOR)
        destination = self.ts.session.archive_dir / "alpha-20261001T120000Z"
        P.ensure_dir(self.ts.session.archive_dir)
        os.rename(self.team.root, destination)
        code, payload, err = self.human("canvas", "import", "--from-archive", "alpha", "--list", "--team", "beta")
        self.assertEqual(code, 0, err)
        self.assertEqual(len(payload["dissolved"]), 1)
        self.assertEqual(payload["dissolved"][0]["elements"], 4)

    def test_list_with_nothing_in_the_archive_says_where_else_to_look(self):
        code, payload, err = self.human("canvas", "import", "--from-archive", "gamma", "--list", "--team", "beta")
        self.assertEqual((code, payload["dissolved"]), (0, []), err)

    def test_from_archive_imports_a_dissolved_team(self):
        self.apply(BOARD, OPERATOR)
        destination = self.ts.session.archive_dir / "alpha-20261001T120000Z"
        P.ensure_dir(self.ts.session.archive_dir)
        os.rename(self.team.root, destination)
        code, payload, err = self.human("canvas", "import", "--from-archive", "alpha", "--team", "beta")
        self.assertEqual(code, 0, err)
        info = payload["applied"][0]["import"]
        self.assertEqual((info["elements"], info["stamp"]), (4, "20261001T120000Z"))
        self.assertIn("dissolved 2026-10-01 12:00", IM.result_text(info))

    def test_export_region_with_format_json_is_refused_by_name(self):
        self.apply(BOARD, OPERATOR)
        code, _payload, err = self.human("canvas", "export", "--format", "json", "--region", "c0r0:c20r20", "--team", "alpha")
        message = err["message"] if isinstance(err, dict) else err
        self.assertEqual(code, 2, message)
        self.assertIn("--region is not available with --format json", message)
        self.assertIn("canvas export --format png --region", message)
        self.assertIn("canvas export --format json", message)

    def test_export_still_exports_a_region_as_a_picture_and_the_whole_scene_as_json(self):
        self.apply(BOARD, OPERATOR)
        code, payload, err = self.human("canvas", "export", "--format", "md", "--region", "c0r0:c20r20", "--team", "alpha")
        self.assertEqual(code, 0, err)
        self.assertIn("region", payload["text"])
        code, payload, err = self.human("canvas", "export", "--format", "json", "--team", "alpha")
        self.assertEqual((code, len(payload["scene"]["elements"])), (0, 4), err)

    def test_import_is_in_the_help_and_is_the_operators(self):
        from herdr_team import cmd_canvas

        self.assertIn("import", dict(cmd_canvas._SPECS))
        self.assertIn("the operator", dict(cmd_canvas._SPECS)["import"])
        code, _payload, err = self.worker("canvas", "import", "--from", self.source_file())
        self.assertEqual((code, err["refused"][0]["code"]), (1, "operator_only"))
