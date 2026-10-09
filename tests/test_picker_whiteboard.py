"""The whiteboard from the teams view (``prefix+t``, ``d``; 0.21): the menu for each kind of row, a team of
one, ``o`` while the whiteboard is off, what the tree shows, and the popup running the steps."""
from __future__ import annotations

import unittest
from typing import Any
from unittest import mock

from herdr_team import features, picker, roster, tui_model
from herdr_team import picker_whiteboard as W
from herdr_team.tui_model import Intent, picker_apply_key
from support import FAKE_MEMBERS, FakeApi, TempState, fake_agent
from test_cmd_roster import live_api
from test_tui_model import picker_model, type_line

ALPHA = {"alpha": [dict(m) for m in FAKE_MEMBERS]}
ENABLE = ["whiteboard", "enable"]


def model_for(layer: Any = False, watched: Any = (), teams: Any = None) -> tui_model.PickerModel:
    model = picker_model(ALPHA, focused=None)
    model.watch_layer = layer
    model.watched = set(watched)
    model.wb_teams = dict(teams or {})
    model.trusted_kinds = {"claude", "codex"}
    return model


def choices(model: tui_model.PickerModel) -> list:
    return [choice for choice, _label in W.options(model)]


def menu_on(model: tui_model.PickerModel, key: str) -> None:
    tui_model.focus_node(model, key)
    picker_apply_key(model, "d")


class Menu(unittest.TestCase):
    def test_an_agent_in_no_team_while_the_whiteboard_is_off(self):
        model = model_for()
        menu_on(model, "pane:w1:p3")
        self.assertEqual(model.stage, "whiteboard")
        self.assertEqual(choices(model), ["watch", "solo", "open", "layer_on"])
        labels = dict(W.options(model))
        for choice in ("watch", "solo", "open"):
            self.assertIn("turns the whiteboard on first", labels[choice])
        intent = picker_apply_key(model, "ENTER")
        self.assertEqual(intent.kind, "whiteboard_steps")
        self.assertEqual(intent.args["steps"], [ENABLE, ["watch", "w1:p3"]])
        self.assertEqual((intent.args["check_pane"], intent.args["terminal_id"]), ("w1:p3", "term_a"))

    def test_a_member_while_the_whiteboard_is_on(self):
        model = model_for(layer=True, teams={"alpha": {"canvas": True, "viz": True}})
        menu_on(model, "member:alpha/alpha-worker")
        self.assertEqual(choices(model), ["watch", "team_canvas", "team_viz", "open", "layer_off"])
        self.assertFalse(any("first" in label for _choice, label in W.options(model)))
        self.assertEqual(picker_apply_key(model, "2").args["steps"], [["--team", "alpha", "whiteboard", "team", "off"]])
        model = model_for(layer=True, watched={"term_w1"}, teams={"alpha": {"canvas": False, "viz": True}})
        menu_on(model, "member:alpha/alpha-worker")
        self.assertEqual(choices(model), ["unwatch", "team_canvas", "open", "layer_off"])
        self.assertEqual(dict(W.options(model))["team_canvas"], "Turn on the canvas for team alpha (only this team)")
        self.assertEqual(picker_apply_key(model, "1").args["steps"], [["unwatch", "term_w1"]])

    def test_a_team_row_and_no_row_at_all(self):
        model = model_for(layer=True, teams={"alpha": {"canvas": True, "viz": False}})
        menu_on(model, "team:alpha")
        self.assertEqual(choices(model), ["team_canvas", "team_viz", "open", "layer_off"])
        self.assertEqual(picker_apply_key(model, "2").args["steps"], [["--team", "alpha", "whiteboard", "viz", "on"]])
        W.open_menu(model, None)
        self.assertEqual(choices(model), ["open", "layer_off"])
        self.assertEqual(picker_apply_key(model, "1").args["steps"], [["whiteboard", "open"]])

    def test_turning_it_off_asks_first(self):
        model = model_for(layer=True)
        menu_on(model, "pane:w1:p3")
        self.assertIsNone(picker_apply_key(model, str(len(W.options(model)))))
        self.assertEqual(model.stage, "select")
        self.assertIn("y turns it off", model.status)
        self.assertEqual(picker_apply_key(model, "y").args["steps"], [["whiteboard", "disable"]])
        menu_on(model, "pane:w1:p3")
        picker_apply_key(model, str(len(W.options(model))))
        self.assertIsNone(picker_apply_key(model, "n"))
        self.assertEqual(model.status, "cancelled")

    def test_esc_or_d_goes_back_and_numbers_are_checked(self):
        model = model_for()
        for back in ("ESC", "d", "q"):
            menu_on(model, "pane:w1:p3")
            self.assertIsNone(picker_apply_key(model, back))
            self.assertEqual(model.stage, "select")
        menu_on(model, "pane:w1:p3")
        self.assertIsNone(picker_apply_key(model, "9"))
        self.assertIn("between 1 and 4", model.error)
        picker_apply_key(model, "DOWN")
        picker_apply_key(model, "DOWN")
        self.assertEqual(picker_apply_key(model, "ENTER").args["steps"], [ENABLE, ["whiteboard", "open"]])

    def test_a_launching_agent_has_no_watch_or_canvas_yet(self):
        model = model_for(layer=True)
        menu_on(model, "pane:w1:p10")
        self.assertEqual(choices(model), ["open", "layer_off"])


class WatchWhileOff(unittest.TestCase):
    def test_o_offers_to_turn_it_on_and_watch(self):
        model = model_for()
        tui_model.focus_node(model, "pane:w1:p3")
        self.assertIsNone(picker_apply_key(model, "o"))
        self.assertIn("Turn it on and watch w1:p3?", model.status)
        self.assertEqual(picker_apply_key(model, "y").args["steps"], [ENABLE, ["watch", "w1:p3"]])
        tui_model.focus_node(model, "pane:w1:p10")
        self.assertIsNone(picker_apply_key(model, "o"))
        self.assertIn("not running", model.error)
        self.assertIsNone(model.pending_action)


class TeamOfOne(unittest.TestCase):
    def test_an_unnamed_agent_gets_a_canvas_with_the_defaults(self):
        model = model_for(layer=True)
        menu_on(model, "pane:w1:p3")
        self.assertIsNone(picker_apply_key(model, "2"))
        self.assertEqual((model.stage, model.input), ("solo_name", "codex-canvas"))
        self.assertIsNotNone(tui_model.picker_cursor(model, tui_model.picker_lines(model, 100, 24), 100))
        picker_apply_key(model, "ENTER")
        self.assertEqual((model.stage, model.input), ("solo_mission", W.SOLO_MISSION))
        intent = picker_apply_key(model, "ENTER")
        name = roster.fit_member_name("codex-canvas", "codex-dev", tui_model.MAX_MEMBER_NAME_CHARS)
        self.assertEqual(intent.args["steps"], [["create", "codex-canvas", "--member", "w1:p3:codex-dev:" + name,
                                                 "--brief", "{}={}".format(name, W.SOLO_MISSION), "--leader", name,
                                                 "--expect-terminal", name + "=term_a"],
                                                ["--team", "codex-canvas", "whiteboard", "team", "on"]])  # only this team's canvas
        self.assertEqual(intent.args["focus"], "team:codex-canvas")
        self.assertIn("has a canvas of its own in team codex-canvas", intent.args["done"])
        self.assertNotIn("not trusted", intent.args["done"])

    def test_a_named_agent_keeps_its_name_and_the_whiteboard_goes_on_first(self):
        model = model_for()
        model.trusted_kinds = {"claude", "codex", "gemini"}  # an untrusted kind asks first: test_canvas_awareness
        menu_on(model, "pane:w1:p2")  # gem, a gemini agent in no team
        picker_apply_key(model, "2")
        self.assertEqual(model.input, "gem-canvas")
        type_line(model, "alpha")
        picker_apply_key(model, "ENTER")
        self.assertIn("team alpha exists", model.error)
        type_line(model, "Sketch Pad!")
        picker_apply_key(model, "ENTER")
        picker_apply_key(model, "CTRL_U")
        picker_apply_key(model, "ENTER")
        self.assertIn("needs a Mission", model.error)
        type_line(model, "Draw   the plan ")
        intent = picker_apply_key(model, "ENTER")
        self.assertEqual(intent.args["steps"], [ENABLE, ["create", "sketch-pad", "--member", "w1:p2:gemini-dev:gem", "--brief", "gem=Draw the plan", "--leader", "gem", "--expect-terminal", "gem=term_c"],
                                                ["--team", "sketch-pad", "whiteboard", "team", "on"]])
        self.assertIn("its briefing tells it once it is idle", intent.args["done"])

    def test_esc_walks_back_keeping_what_was_typed(self):
        model = model_for(layer=True)
        menu_on(model, "pane:w1:p3")
        picker_apply_key(model, "2")
        picker_apply_key(model, "ENTER")
        type_line(model, "my mission")
        picker_apply_key(model, "ESC")
        self.assertEqual((model.stage, model.input), ("solo_name", "codex-canvas"))
        picker_apply_key(model, "ENTER")
        self.assertEqual(model.input, "my mission")
        picker_apply_key(model, "ESC")
        picker_apply_key(model, "ESC")
        self.assertEqual(model.stage, "whiteboard")

    def test_default_names_fit_and_do_not_collide(self):
        model = model_for()
        model.existing_teams = ["alpha", "codex-canvas"]
        model.wb_target = {"agent_kind": "codex", "name": ""}
        self.assertEqual(W.default_solo_team(model), "codex-canvas-2")
        model.wb_target = {"agent_kind": "claude", "name": "a-very-long-agent-name-indeed"}
        name = W.default_solo_team(model)
        self.assertLessEqual(len(name), 15)
        self.assertIsNone(tui_model.validate_team_name_local(name))
        self.assertTrue(name.endswith("-canvas"))


class Tree(unittest.TestCase):
    def test_the_header_team_rows_legend_and_detail_line(self):
        model = model_for(layer=True, teams={"alpha": {"canvas": True, "viz": False}})
        lines = tui_model.picker_lines(model, 120, 30)
        self.assertIn("whiteboard on", lines[0])
        self.assertTrue(any("alpha" in line and "canvas (live visuals off)" in line for line in lines), lines)
        self.assertTrue(any("d whiteboard" in line for line in lines))
        tui_model.focus_node(model, "pane:w1:p3")
        self.assertIn("o watches it · d whiteboard", tui_model._tree_detail(model, tui_model.picker_tree(model)))
        tui_model.focus_node(model, "team:alpha")
        self.assertIn("d whiteboard", tui_model._tree_detail(model, tui_model.picker_tree(model)))
        model.wb_teams = {"alpha": {"canvas": False, "viz": True}}
        self.assertFalse(any(" canvas" in line for line in tui_model.picker_lines(model, 120, 30) if "alpha" in line),
                         "a team whose canvas is off says nothing: that is the default")
        model = model_for(layer=False)
        lines = tui_model.picker_lines(model, 120, 30)
        self.assertIn("whiteboard off", lines[0])
        self.assertFalse(any(" canvas" in line for line in lines if "alpha" in line))
        tui_model.focus_node(model, "pane:w1:p3")
        detail = tui_model._tree_detail(model, tui_model.picker_tree(model))
        self.assertIn("d whiteboard", detail)
        self.assertNotIn("o watch", detail)

    def test_every_stage_fits_the_popup(self):
        for layer in (True, False):
            model = model_for(layer=layer)
            menu_on(model, "pane:w1:p3")
            for width, height in ((100, 24), (40, 12), (30, 8)):
                lines = tui_model.picker_lines(model, width, height)
                self.assertLessEqual(len(lines), height)
                self.assertTrue(all(tui_model.display_width(line) <= width for line in lines), (width, lines))
                self.assertIsNone(tui_model.picker_cursor(model, lines, width))
            self.assertIn("Whiteboard: {} for this Herdr session".format("on" if layer else "off"), tui_model.picker_lines(model, 100, 24)[0])
            picker_apply_key(model, "2")
            for stage in ("solo_name", "solo_mission"):
                lines = tui_model.picker_lines(model, 40, 8)
                self.assertTrue(lines[-1].startswith(tui_model.INPUT_PROMPT) or lines[-2].startswith(tui_model.INPUT_PROMPT), lines)
                picker_apply_key(model, "ENTER") if stage == "solo_name" else None


class Messages(unittest.TestCase):
    """Found live on 2026-09-27: a 60-column popup cut "y turns it off, n cancels" off the question."""

    def test_a_long_question_wraps_and_keeps_its_answer_keys(self):
        model = model_for(layer=True)
        menu_on(model, "pane:w1:p3")
        picker_apply_key(model, str(len(W.options(model))))
        for width, height in ((60, 24), (40, 12), (30, 9)):
            lines = tui_model.picker_lines(model, width, height)
            self.assertLessEqual(len(lines), height)
            self.assertTrue(all(tui_model.display_width(line) <= width for line in lines), lines)
            self.assertIn("y turns it off, n cancels", " ".join(line.strip() for line in lines), (width, lines))

    def test_a_long_error_under_a_text_field_stays_with_it(self):
        model = model_for(layer=True)
        menu_on(model, "pane:w1:p3")
        picker_apply_key(model, "2")
        type_line(model, "alpha")
        picker_apply_key(model, "ENTER")
        lines = tui_model.picker_lines(model, 30, 8)
        joined = " ".join(line.strip() for line in lines)
        self.assertIn("a team of one is a new team", joined)
        self.assertTrue(any(line.startswith(tui_model.INPUT_PROMPT) for line in lines))

    def test_wrapped_menu_choices_line_up(self):
        model = model_for()
        menu_on(model, "pane:w1:p3")
        firsts = [line for line in tui_model.picker_lines(model, 58, 28) if line[2:3].isdigit()]
        self.assertEqual(len(firsts), 4, firsts)
        self.assertEqual({line[:2] for line in firsts} - {"> "}, {"  "})


class Running(unittest.TestCase):
    def setUp(self) -> None:
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.model = model_for()
        self.api = live_api([fake_agent("w1:p3", "term_a", "codex", None)])

    def intent(self) -> Intent:
        menu_on(self.model, "pane:w1:p3")
        return picker_apply_key(self.model, "ENTER")  # watch, with the whiteboard off

    def test_it_is_a_picker_action(self):
        self.assertIn("whiteboard_steps", picker.ACTION_INTENTS)
        self.assertEqual(picker.ACTION_LABELS["whiteboard_steps"].format(member="x", team=""), "working on the whiteboard")

    def test_the_steps_run_in_order_and_the_popup_stays_open(self):
        intent = self.intent()
        with mock.patch("herdr_team.console.run_cli", return_value=(0, {}, None)) as run:
            self.assertTrue(picker.execute_action(intent, self.model, self.api, self.ts.layout, {}))
        self.assertEqual([c[0][0] for c in run.call_args_list], [ENABLE, ["watch", "w1:p3"]])
        self.assertIn("watching w1:p3", self.model.status)
        self.assertIsNone(self.model.error)
        self.assertEqual(self.model.stage, "select")

    def test_a_refused_step_stops_there_and_says_what_was_done(self):
        intent = self.intent()
        answers = [(0, {}, None), (1, None, {"code": "watch_limit", "message": "m", "max": 20})]
        with mock.patch("herdr_team.console.run_cli", side_effect=answers) as run:
            picker.execute_action(intent, self.model, self.api, self.ts.layout, {})
        self.assertEqual(run.call_count, 2)
        self.assertIn("at most 20", self.model.error)
        self.assertIn("already done: whiteboard enable", self.model.error)
        with mock.patch("herdr_team.console.run_cli", return_value=(1, None, {"code": "author_mismatch", "message": "m"})):
            picker.execute_action(self.intent(), self.model, self.api, self.ts.layout, {})
        self.assertIn("operator's in person", self.model.error)

    def test_open_says_where_the_page_went(self):
        model = model_for(layer=True)
        W.open_menu(model, None)
        intent = picker_apply_key(model, "1")
        with mock.patch("herdr_team.console.run_cli", return_value=(0, {"browser": "opened", "url": "http://127.0.0.1:1/?ticket=t"}, None)):
            picker.execute_action(intent, model, self.api, self.ts.layout, {})
        self.assertEqual(model.status, "opened the whiteboard page in your browser")
        over_ssh = {"browser": "ssh", "url": "http://127.0.0.1:1/?ticket=t", "ssh_hint": "ssh -L 1:127.0.0.1:1 me@box"}
        with mock.patch("herdr_team.console.run_cli", return_value=(0, over_ssh, None)):
            picker.execute_action(intent, model, self.api, self.ts.layout, {})
        self.assertIn("run ssh -L 1:127.0.0.1:1 me@box", model.status)

    def test_a_pane_that_changed_hands_is_left_alone(self):
        api = live_api([fake_agent("w1:p3", "term_other", "codex", None)])
        with mock.patch("herdr_team.console.run_cli") as run:
            picker.execute_action(self.intent(), self.model, api, self.ts.layout, {})
        run.assert_not_called()
        self.assertIn("changed while this was open", self.model.error)

    def test_the_model_reads_each_teams_switches_and_the_page(self):
        self.assertEqual(picker.whiteboard_state(None, {}), ({}, False))
        features.set_team(self.ts.team, enabled=True, viz=False, by="human", via="cli")
        api = FakeApi()
        api.set_response("agent.list", {"type": "agent_list", "agents": [fake_agent("w1:p3", "term_a", "codex", None)]})
        api.set_response("tab.list", {"type": "tab_list", "tabs": []})
        model = picker.build_model(api, {}, self.ts.layout)
        self.assertEqual(model.wb_teams[self.ts.team.name], {"canvas": True, "viz": False})
        self.assertFalse(model.wb_page)
        features.set_team(self.ts.team, enabled=False, by="human", via="cli")
        picker.refresh_rows(model, api, self.ts.layout)
        self.assertFalse(model.wb_teams[self.ts.team.name]["canvas"])


if __name__ == "__main__":
    unittest.main()
