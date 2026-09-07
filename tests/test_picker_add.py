"""The team-up picker: after selecting agents, a numbered target stage adds them to an existing team or creates a new one."""

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
        tui_model.focus_node(model, "pane:w1:p3")
        picker_apply_key(model, " ")
        picker_apply_key(model, "ENTER")
        return model

    def test_the_target_stage_offers_existing_teams_by_number(self):
        model = self.model()
        model.existing_team_sizes = {"alpha": 2}
        self.assertEqual(model.stage, "target")
        lines = tui_model.picker_lines(model, 100, 24)
        self.assertEqual(lines[0], "1 agent selected. What now? (type a number, or ↑/↓ and Enter; Esc back)")
        self.assertEqual(lines[1], "> 1  add it to team alpha  (2 members)")
        self.assertEqual(lines[2], "  2  create a new team")
        picker_apply_key(model, "7")
        self.assertIn("between 1 and 2", model.error)
        self.assertEqual(model.stage, "target")
        picker_apply_key(model, "DOWN")
        self.assertTrue(tui_model.picker_lines(model, 100, 24)[2].startswith("> 2"))
        picker_apply_key(model, "UP")
        picker_apply_key(model, "1")
        self.assertEqual((model.stage, model.mode, model.team_name), ("members", "add", "alpha"))
        self.assertIsNone(model.error)
        self.assertIn("adding 1 agent to team alpha", model.status)
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

    def test_escape_and_the_create_choice_reach_the_name_stage(self):
        model = self.model()
        picker_apply_key(model, "ENTER")  # the highlighted row is "add to team alpha"
        self.assertEqual((model.stage, model.mode), ("members", "add"))
        picker_apply_key(model, "ESC")
        self.assertEqual(model.stage, "target")
        picker_apply_key(model, "2")
        self.assertEqual((model.stage, model.mode), ("name", "create"))
        self.assertTrue(tui_model.picker_lines(model, 100, 24)[0].startswith("New team name"))
        picker_apply_key(model, "ESC")
        self.assertEqual(model.stage, "target")
        picker_apply_key(model, "2")
        type_line(model, "fresh")
        picker_apply_key(model, "ENTER")
        self.assertEqual((model.stage, model.mode, model.team_name), ("charter", "create", "fresh"))
        self.assertEqual(tui_model.create_spec(model)["mode"], "create")
        # typing an existing team's name at the name stage still adds (the same as choosing its row)
        picker_apply_key(model, "ESC")
        type_line(model, "alpha")
        picker_apply_key(model, "ENTER")
        self.assertEqual((model.stage, model.mode, model.team_name), ("members", "add", "alpha"))
        # without any existing team the selection goes straight to the name stage
        fresh = picker_model(focused=None)
        tui_model.focus_node(fresh, "pane:w1:p3")
        picker_apply_key(fresh, " ")
        picker_apply_key(fresh, "ENTER")
        self.assertEqual(fresh.stage, "name")

    def test_input_follows_its_prompt_and_the_cursor_sits_on_it(self):
        model = self.model()
        self.assertIsNone(tui_model.picker_cursor(model, tui_model.picker_lines(model, 100, 24), 100))  # list stage: no cursor
        picker_apply_key(model, "1")
        lines = tui_model.picker_lines(model, 100, 24)
        self.assertEqual(lines[1], "Enter accepts the value shown, Ctrl-U clears it, Esc goes back")
        self.assertEqual(lines[2], "role:")
        self.assertTrue(lines[3].startswith(tui_model.INPUT_PROMPT + "codex-dev"))
        self.assertTrue(lines[4].startswith("adding 1 agent to team alpha"))
        width = tui_model.display_width
        self.assertEqual(tui_model.picker_cursor(model, lines, 100), (3, width(tui_model.INPUT_PROMPT) + width("codex-dev")))
        picker_apply_key(model, "LEFT")
        picker_apply_key(model, "LEFT")
        self.assertEqual(tui_model.picker_cursor(model, tui_model.picker_lines(model, 100, 24), 100), (3, width(tui_model.INPUT_PROMPT) + width("codex-dev") - 2))
        picker_apply_key(model, "END")
        type_line(model, "Bad Role")
        picker_apply_key(model, "ENTER")
        lines = tui_model.picker_lines(model, 100, 24)
        self.assertEqual(lines[2], "role:")
        self.assertTrue(lines[3].startswith(tui_model.INPUT_PROMPT + "Bad Role"))
        self.assertTrue(lines[4].startswith("error: role"))  # the message comes after what was typed
        self.assertEqual(tui_model.picker_cursor(model, lines, 100)[0], 3)
        type_line(model, "tester")
        picker_apply_key(model, "ENTER")
        lines = tui_model.picker_lines(model, 100, 24)
        self.assertEqual(lines[2], "name:")
        self.assertTrue(lines[3].startswith(tui_model.INPUT_PROMPT + "alpha-tester"))
        picker_apply_key(model, "ENTER")
        self.assertEqual(tui_model.picker_lines(model, 100, 24)[2], "brief for alpha-tester (optional):")
        # a short popup keeps the input line and its message on screen
        short = tui_model.picker_lines(model, 100, 3)
        self.assertEqual(len(short), 3)
        self.assertTrue(any(line.startswith(tui_model.INPUT_PROMPT) for line in short))
        self.assertIsNotNone(tui_model.picker_cursor(model, short, 100))

    def test_long_roles_are_accepted_and_the_default_name_still_fits(self):
        model = self.model()
        picker_apply_key(model, "1")
        type_line(model, "opencode-dev-ideation-and-review")  # 32 characters
        picker_apply_key(model, "ENTER")
        self.assertIsNone(model.error)
        self.assertEqual(model.member_field, "name")
        self.assertEqual(model.input, "alpha-ideation-and-review")  # <team>-<role> too long: the generic lead of the role is dropped
        self.assertLessEqual(len(model.input), 32)
        picker_apply_key(model, "ESC")
        type_line(model, "a" * 65)
        picker_apply_key(model, "ENTER")
        self.assertIn("up to 64 characters (yours is 65 characters)", model.error)
        type_line(model, "1st-reviewer")
        picker_apply_key(model, "ENTER")
        self.assertIn("start with a lowercase letter", model.error)
        type_line(model, "ux review")
        picker_apply_key(model, "ENTER")
        self.assertIn("no spaces", model.error)
        type_line(model, "Ideation")  # the picker lowercases what you type
        picker_apply_key(model, "ENTER")
        self.assertIsNone(model.error)
        self.assertEqual(model.member_field, "name")

    def test_confirm_screen_flags_kinds_the_daemon_may_not_type_into(self):
        def confirm(trusted):
            model = self.model()
            model.trusted_kinds = trusted
            picker_apply_key(model, "1")
            for _ in range(3):
                picker_apply_key(model, "ENTER")  # role, name, brief defaults
            self.assertEqual(model.stage, "confirm")
            return "\n".join(tui_model.picker_lines(model, 120, 24))

        self.assertIn("note: codex is not trusted for delivery yet; nothing is typed into it until you run: herdr-synapse kinds trust codex", confirm({"claude"}))
        self.assertNotIn("note:", confirm({"codex", "claude"}))
        self.assertNotIn("note:", confirm(None))
        from support import TempState
        from herdr_team import store
        with TempState() as ts:
            self.assertEqual(picker.trusted_kinds(ts.layout), set())
            store.write_json(ts.session.kinds_json, {"codex": {"trusted": True}, "pi": {"verified": False}})
            self.assertEqual(picker.trusted_kinds(ts.layout), {"codex"})
            self.assertIsNone(picker.trusted_kinds(None))

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
