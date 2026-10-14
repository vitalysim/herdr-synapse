"""New opt-in UI follows the existing wizard, confirmation and command paths."""
from __future__ import annotations

import unittest

from herdr_team import picker, tui_model as T
from test_tui_model import picker_model


class StaffingWizard(unittest.TestCase):
    def model(self):
        model = picker_model()
        model.stage = "confirm"
        model.team_name = "example"
        model.charter = "Implement the change"
        model.project = "/project"
        model.rows[0].selected = True
        model.rows[0].member_name = "example-lead"
        model.rows[0].role = "lead"
        model.rows[0].brief = "Coordinate"
        model.leader_key = T.leader_row_key(model.rows[0])
        return model

    def test_manual_spec_and_argv_do_not_get_autonomous_options(self):
        model = self.model()
        spec = T.create_spec(model)
        self.assertNotIn("staffing", spec)
        self.assertNotIn("--autonomous", picker.create_args(spec))

    def test_opt_in_pool_limit_acceptance_and_disable_keep_manual_selection(self):
        model = self.model()
        before = T.create_spec(model)["members"]
        T.picker_apply_key(model, "a")
        self.assertEqual(model.stage, "staff_pool")
        model.input = "codex:openai,claude:firstParty"
        T.picker_apply_key(model, "ENTER")
        self.assertEqual(model.stage, "staff_limit")
        self.assertEqual(model.input, "4")
        T.picker_apply_key(model, "ENTER")
        model.input = "Tests and review pass"
        T.picker_apply_key(model, "ENTER")
        self.assertEqual(model.stage, "confirm")
        spec = T.create_spec(model)
        self.assertEqual(spec["staffing"]["max_workers"], 4)
        self.assertEqual(spec["members"], before)
        argv = picker.create_args(spec)
        self.assertIn("--autonomous", argv)
        self.assertEqual(argv.count("--allow-provider"), 2)
        T.picker_apply_key(model, "a")
        self.assertNotIn("staffing", T.create_spec(model))
        self.assertEqual(T.create_spec(model)["members"], before)

    def test_existing_multi_member_manual_selection_is_not_removed(self):
        model = self.model()
        model.rows[1].selected = True
        before = T.create_spec(model)["members"]
        T.picker_apply_key(model, "a")
        self.assertEqual(model.stage, "confirm")
        self.assertFalse(model.autonomous)
        self.assertEqual(T.create_spec(model)["members"], before)

    def test_empty_pool_invalid_limit_and_empty_acceptance_stay_in_place(self):
        model = self.model()
        for stage, text in (("staff_pool", ""), ("staff_limit", "0"), ("staff_limit", "not a number"), ("staff_acceptance", "")):
            model.stage, model.input = stage, text
            T.picker_apply_key(model, "ENTER")
            self.assertEqual(model.stage, stage)
            self.assertTrue(model.error)
            self.assertFalse(model.autonomous)

    def test_model_check_requires_confirmation_and_does_not_set_member_model(self):
        model = self.model()
        member = {"name": "example-lead", "kind": "codex", "terminal_id": "t", "cwd": "/project", "profile": "fast"}
        model.rosters = {"example": [member]}
        model.action_team, model.action_member = "example", member["name"]
        model.stage, model.input = "model", "model-a@high"
        self.assertIsNone(T.picker_apply_key(model, "CTRL_T"))
        self.assertEqual(model.pending_action.kind, "member_model_check")
        self.assertIn("charges", model.status)
        accepted = T.picker_apply_key(model, "y")
        self.assertEqual(accepted.kind, "member_model_check")
        argv = picker.action_args(accepted)
        self.assertIn("model-check", argv)
        self.assertIn("--yes", argv)
        self.assertIn("fast", argv)
        self.assertNotIn("model", member)
        model.stage, model.input = "model", "model-a"
        T.picker_apply_key(model, "CTRL_T")
        self.assertIsNone(T.picker_apply_key(model, "n"))
        self.assertIsNone(model.pending_action)

    def test_discovery_is_never_called_by_ui_render_even_with_fifteen_panes(self):
        from unittest import mock
        from herdr_team import model_inventory
        model = self.model()
        for count in (1, 15):
            model.rows = [model.rows[0]] * count
            with mock.patch.object(model_inventory, "discover") as probe:
                T.picker_lines(model, 100, 40)
                probe.assert_not_called()


if __name__ == "__main__":
    unittest.main()
