"""The team-up picker adds agents to an existing team when its name is typed at the name stage."""

from __future__ import annotations

import contextlib
import io
import json
import unittest

from herdr_team import console, picker, tui_model
from herdr_team.tui_model import picker_apply_key
from support import FAKE_MEMBERS
from test_tui_model import picker_model, type_line


class PickerAddModeTests(unittest.TestCase):
    def model(self):
        model = picker_model(rosters={"alpha": [dict(m) for m in FAKE_MEMBERS]}, focused=None)
        model.cursor = [r.pane_id for r in tui_model.visible_rows(model)].index("w1:p3")
        picker_apply_key(model, " ")
        picker_apply_key(model, "ENTER")
        return model

    def test_existing_team_name_switches_to_add_mode(self):
        model = self.model()
        self.assertEqual(model.stage, "name")
        self.assertTrue(any("existing: alpha" in line for line in tui_model.picker_lines(model, 100, 24)))
        type_line(model, "alpha")
        picker_apply_key(model, "ENTER")
        self.assertEqual((model.stage, model.mode, model.team_name), ("members", "add", "alpha"))
        self.assertIsNone(model.error)
        self.assertIn("adding to team alpha", model.status)
        self.assertIn("(adding to team alpha)", tui_model.picker_lines(model, 100, 24)[0])
        picker_apply_key(model, "ENTER")  # default role
        self.assertTrue(model.input.startswith("alpha-"))  # default name <team>-<role>
        picker_apply_key(model, "ENTER")
        type_line(model, "Review every patch.")
        picker_apply_key(model, "ENTER")
        self.assertEqual(model.stage, "confirm")
        joined = "\n".join(tui_model.picker_lines(model, 100, 24))
        self.assertIn("Add 1 agent to team alpha? (Enter adds, Esc back)", joined)
        self.assertIn("charter: kept", joined)
        intent = picker_apply_key(model, "ENTER")
        self.assertEqual(intent.kind, "create")
        spec = intent.args
        self.assertEqual((spec["mode"], spec["team"], spec["charter"]), ("add", "alpha", None))
        self.assertEqual(len(spec["members"]), 1)
        member = spec["members"][0]
        self.assertEqual(member["target"], "w1:p3")
        self.assertEqual(picker.add_args(spec, member), ["add", "alpha", "w1:p3", "--role", member["role"], "--as", member["name"], "--brief", "Review every patch."])
        self.assertEqual(picker.add_args(spec, dict(member, brief=None))[-2:], ["--as", member["name"]])

    def test_escape_from_the_first_member_returns_to_the_name_stage(self):
        model = self.model()
        type_line(model, "alpha")
        picker_apply_key(model, "ENTER")
        picker_apply_key(model, "ESC")
        self.assertEqual((model.stage, model.input), ("name", "alpha"))
        type_line(model, "fresh")
        picker_apply_key(model, "ENTER")
        self.assertEqual((model.stage, model.mode, model.team_name), ("charter", "create", "fresh"))
        self.assertEqual(tui_model.create_spec(model)["mode"], "create")

    def test_execute_add_runs_one_add_per_member_and_stops_at_the_first_refusal(self):
        calls = []

        def fake_run_cli(args, env, timeout=0.0):
            calls.append(list(args))
            if args[2] == "w9:p9":
                return 1, None, {"code": "member_claimed", "message": "taken"}
            return 0, {"team": "alpha", "member": {"name": args[args.index("--as") + 1]}, "briefing_job": "j1"}, None

        spec = {"team": "alpha", "mode": "add", "members": [
            {"target": "w1:p3", "role": "tester", "name": "alpha-tester", "brief": None},
            {"target": "w9:p9", "role": "x", "name": "alpha-x", "brief": "b"},
        ]}
        original = console.run_cli
        console.run_cli = fake_run_cli
        out, err = io.StringIO(), io.StringIO()
        try:
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = picker.execute_create(spec, {})
            self.assertEqual(rc, 1)
            self.assertEqual(calls, [
                ["add", "alpha", "w1:p3", "--role", "tester", "--as", "alpha-tester"],
                ["add", "alpha", "w9:p9", "--role", "x", "--as", "alpha-x", "--brief", "b"],
            ])
            error = json.loads(err.getvalue())
            self.assertEqual((error["code"], error["added_before_failure"]), ("member_claimed", ["alpha-tester"]))
            calls.clear()
            out, err = io.StringIO(), io.StringIO()
            spec["members"] = spec["members"][:1]
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = picker.execute_create(spec, {})
            self.assertEqual(rc, 0)
            payload = json.loads(out.getvalue())
            self.assertEqual((payload["team"], payload["mode"], payload["added"][0]["member"]["name"]), ("alpha", "add", "alpha-tester"))
            self.assertEqual(len(calls), 1)
        finally:
            console.run_cli = original


if __name__ == "__main__":
    unittest.main()
