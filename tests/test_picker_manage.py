"""The team-up picker as a team manager: the tree, and the actions on one member.

`prefix+t` shows every team with its agents under it, and Enter on a member
opens a numbered menu that renames it, changes its goal, sends that goal,
removes it, or jumps to its pane. Every action runs through the CLI while
the popup stays open.
"""

from __future__ import annotations

import unittest

from herdr_team import picker, tui_model
from herdr_team.tui_model import Intent, picker_apply_key
from support import FAKE_MEMBERS, FakeApi, TempState
from test_tui_model import picker_model, type_line

ALPHA = {"alpha": [dict(m) for m in FAKE_MEMBERS]}


def tree(model):
    return [(n.kind, n.key) for n in tui_model.picker_tree(model)]


def managed(model, member="alpha-reviewer", team="alpha"):
    """Open the action menu for one member, the way Enter on its row does."""
    tui_model.focus_node(model, "member:{}/{}".format(team, member))
    picker_apply_key(model, "ENTER")
    return model


class TreeShapeTests(unittest.TestCase):
    def test_teams_come_first_then_the_agents_that_belong_to_none(self):
        model = picker_model(ALPHA, focused=None)
        self.assertEqual(tree(model), [
            ("team", "team:alpha"),
            ("member", "member:alpha/alpha-reviewer"),
            ("member", "member:alpha/alpha-worker"),
            ("section", "section:unassigned"),
            ("agent", "pane:w1:p2"),
            ("agent", "pane:w1:p3"),
            ("agent", "pane:w1:p10"),
        ])

    def test_the_human_member_and_the_tombstones_are_never_listed(self):
        roster = [dict(m) for m in FAKE_MEMBERS]
        self.assertTrue(any(m["kind"] == "human" for m in roster), "the fixture carries the human member")
        roster.append({"name": "alpha-gone", "role": "worker", "kind": "claude", "terminal_id": "term_gone", "status": "left"})
        model = picker_model({"alpha": roster}, focused=None)
        names = [n.label for n in tui_model.picker_tree(model) if n.kind == "member"]
        self.assertEqual(names, ["alpha-reviewer", "alpha-worker"])

    def test_with_no_team_the_tree_is_the_flat_agent_list_it_always_was(self):
        model = picker_model(focused=None)
        self.assertEqual([n.kind for n in tui_model.picker_tree(model)], ["agent"] * 5)

    def test_collapsing_a_team_hides_its_members_but_keeps_the_header(self):
        model = picker_model(ALPHA, focused=None)
        picker_apply_key(model, "ENTER")  # the cursor starts on the team header
        self.assertEqual(model.collapsed, {"alpha"})
        self.assertNotIn(("member", "member:alpha/alpha-reviewer"), tree(model))
        self.assertIn(("team", "team:alpha"), tree(model))
        picker_apply_key(model, "RIGHT")
        self.assertEqual(model.collapsed, set())
        picker_apply_key(model, "LEFT")
        self.assertEqual(model.collapsed, {"alpha"})

    def test_a_member_row_merges_the_roster_the_live_agent_and_who(self):
        model = picker_model(ALPHA, focused=None)
        model.charters = {"alpha": {"seq": 3, "text": "Ship it"}}
        model.who_members = {"alpha": {"alpha-reviewer": {"last_headline": "reviewing", "pending_nudges": 2, "hold": "not_idle", "agent_status": "stale"}}}
        node = [n for n in tui_model.picker_tree(model) if n.key == "member:alpha/alpha-reviewer"][0]
        member = node.member
        self.assertEqual((member["name"], member["role"], member["kind"]), ("alpha-reviewer", "reviewer", "codex"))
        self.assertEqual(member["agent_status"], "idle")  # agent.list wins over a stale who.json
        self.assertEqual((member["last_headline"], member["pending_nudges"], member["hold"]), ("reviewing", 2, "not_idle"))
        self.assertTrue(member["charter_stale"])  # nothing acked charter #3
        line = tui_model.roster_line(member, 100, show_role=True)
        self.assertIn("alpha-reviewer  reviewer  codex  w2:p1  idle", line)

    def test_a_member_whose_agent_is_gone_reads_unknown_not_idle(self):
        model = picker_model(ALPHA, focused=None)
        model.rows = [r for r in model.rows if r.pane_id != "w2:p1"]
        node = [n for n in tui_model.picker_tree(model) if n.key == "member:alpha/alpha-reviewer"][0]
        self.assertEqual(node.member["agent_status"], "unknown")


class TreeKeyTests(unittest.TestCase):
    def test_space_and_enter_do_the_right_thing_per_row(self):
        model = picker_model(ALPHA, focused=None)
        # a member row: Space explains, Enter opens the actions
        tui_model.focus_node(model, "member:alpha/alpha-worker")
        picker_apply_key(model, " ")
        self.assertIn("Enter opens its actions", model.error)
        self.assertEqual(picker_apply_key(model, "ENTER"), None)
        self.assertEqual((model.stage, model.action_team, model.action_member), ("actions", "alpha", "alpha-worker"))
        picker_apply_key(model, "ESC")
        self.assertEqual(model.stage, "select")
        # an unassigned row: Space picks it
        tui_model.focus_node(model, "pane:w1:p3")
        picker_apply_key(model, " ")
        self.assertEqual([r.pane_id for r in tui_model.selected_rows(model)], ["w1:p3"])
        # a team row with a selection: Enter adds them to that team
        tui_model.focus_node(model, "team:alpha")
        picker_apply_key(model, "ENTER")
        self.assertEqual((model.stage, model.mode, model.team_name), ("members", "add", "alpha"))

    def test_enter_on_a_lone_agent_picks_it_and_moves_on(self):
        model = picker_model(focused=None)  # no teams, so no target stage
        tui_model.focus_node(model, "pane:w1:p3")
        picker_apply_key(model, "ENTER")
        self.assertEqual([r.pane_id for r in tui_model.selected_rows(model)], ["w1:p3"])
        self.assertEqual(model.stage, "name")

    def test_paste_in_the_tree_never_fires_keys(self):
        model = picker_model(ALPHA, focused=None)
        picker_apply_key(model, "PASTE_START")
        for key in ("ENTER", " ", "a", "q"):
            self.assertIsNone(picker_apply_key(model, key))
        self.assertEqual((model.stage, model.collapsed), ("select", set()))
        picker_apply_key(model, "PASTE_END")
        self.assertEqual(picker_apply_key(model, "q").kind, "quit")

    def test_the_scope_key_keeps_the_cursor_on_the_same_row(self):
        model = picker_model(ALPHA, focused="w1")
        tui_model.focus_node(model, "member:alpha/alpha-worker")
        picker_apply_key(model, "w")
        self.assertEqual(tui_model.node_at(model).key, "member:alpha/alpha-worker")
        self.assertEqual(model.scope_workspace, "w1")


class ScrollTests(unittest.TestCase):
    def test_window_moves_the_least_it_can(self):
        self.assertEqual(tui_model.scroll_window(0, 0, 10, 4), 0)
        self.assertEqual(tui_model.scroll_window(0, 5, 10, 4), 2)
        self.assertEqual(tui_model.scroll_window(5, 1, 10, 4), 1)
        self.assertEqual(tui_model.scroll_window(9, 9, 10, 4), 6)
        self.assertEqual(tui_model.scroll_window(0, 0, 2, 4), 0)  # shorter than the window
        self.assertEqual(tui_model.scroll_window(3, 0, 0, 4), 0)  # empty
        self.assertEqual(tui_model.scroll_window(0, 0, 10, 0), 0)

    def test_a_long_tree_scrolls_and_still_shows_the_error(self):
        roster = [dict(m) for m in FAKE_MEMBERS]
        roster += [{"name": "alpha-{}".format(i), "role": "worker", "kind": "claude", "terminal_id": "t{}".format(i), "status": "active"} for i in range(20)]
        model = picker_model({"alpha": roster}, focused=None)
        model.error = "something went wrong"
        tui_model.focus_node(model, "member:alpha/alpha-19")
        lines = tui_model.picker_lines(model, 90, 12)
        self.assertLessEqual(len(lines), 12)
        self.assertIn("alpha-19", "\n".join(lines))
        self.assertEqual(lines[-1], "error: something went wrong")
        self.assertTrue(any("PgUp/PgDn" in line for line in lines))
        for width, height in ((100, 24), (70, 24), (40, 10), (40, 6)):
            for line in tui_model.picker_lines(model, width, height):
                self.assertLessEqual(tui_model.display_width(line), width)


class ActionMenuTests(unittest.TestCase):
    def test_the_menu_lists_seven_numbered_actions(self):
        model = managed(picker_model(ALPHA, focused=None))
        lines = tui_model.picker_lines(model, 100, 24)
        self.assertIn("alpha-reviewer · reviewer · codex · w2:p1 · idle", lines[0])
        self.assertTrue(any(line.startswith("> 1  rename it") for line in lines))
        self.assertTrue(any("4  remove it from alpha" in line for line in lines))
        self.assertTrue(any("5  remove it from alpha, keep its Herdr agent name" in line for line in lines))
        self.assertTrue(any("7  show the command that reopens its own session" in line for line in lines))
        picker_apply_key(model, "9")
        self.assertEqual(model.error, "type a number between 1 and 7")
        picker_apply_key(model, "DOWN")
        picker_apply_key(model, "ENTER")
        self.assertEqual(model.stage, "goal")

    def test_actions_refuse_a_member_whose_pane_is_stale(self):
        roster = [dict(m) for m in FAKE_MEMBERS]
        for m in roster:
            if m["name"] == "alpha-reviewer":
                m["status"] = "missing"
        model = managed(picker_model({"alpha": roster}, focused=None))
        picker_apply_key(model, "1")  # rename
        self.assertIn("no live agent", model.error)
        self.assertEqual(model.stage, "actions")
        picker_apply_key(model, "2")  # the goal is a roster edit, so it is still allowed
        self.assertEqual(model.stage, "goal")

    def test_a_member_that_vanished_sends_you_back_to_the_tree(self):
        model = managed(picker_model(ALPHA, focused=None))
        model.rosters = {"alpha": [m for m in model.rosters["alpha"] if m.get("name") != "alpha-reviewer"]}
        picker_apply_key(model, "1")
        self.assertEqual(model.stage, "select")
        self.assertIn("not in alpha any more", model.error)


class RenameStageTests(unittest.TestCase):
    def test_prefilled_validated_and_emitted(self):
        model = managed(picker_model(ALPHA, focused=None))
        picker_apply_key(model, "1")
        self.assertEqual((model.stage, model.input), ("rename", "alpha-reviewer"))
        self.assertIsNone(picker_apply_key(model, "ENTER"))  # unchanged
        self.assertEqual((model.stage, model.status), ("actions", "name unchanged"))
        picker_apply_key(model, "1")
        type_line(model, "Bad Name")
        picker_apply_key(model, "ENTER")
        self.assertIn("lowercase", model.error)
        type_line(model, "alpha-worker")
        picker_apply_key(model, "ENTER")
        self.assertIn("taken", model.error)
        type_line(model, "gem")  # a live agent elsewhere holds this name
        picker_apply_key(model, "ENTER")
        self.assertIn("taken", model.error)
        type_line(model, "alpha-lead")
        intent = picker_apply_key(model, "ENTER")
        self.assertEqual(intent.kind, "member_rename")
        self.assertEqual((intent.args["team"], intent.args["member"], intent.args["new"]), ("alpha", "alpha-reviewer", "alpha-lead"))
        self.assertEqual(intent.args["terminal_id"], "term_r1")
        self.assertEqual(model.stage, "rename")  # the executor owns the transition
        picker_apply_key(model, "ESC")
        self.assertEqual(model.stage, "actions")


class GoalStageTests(unittest.TestCase):
    def test_prefilled_capped_and_clearable(self):
        roster = [dict(m) for m in FAKE_MEMBERS]
        for m in roster:
            if m["name"] == "alpha-reviewer":
                m["brief"] = "Review every patch."
        model = managed(picker_model({"alpha": roster}, focused=None))
        picker_apply_key(model, "2")
        self.assertEqual((model.stage, model.input), ("goal", "Review every patch."))
        self.assertIsNone(picker_apply_key(model, "ENTER"))
        self.assertEqual((model.stage, model.status), ("actions", "goal unchanged"))
        picker_apply_key(model, "2")
        type_line(model, "x" * (tui_model.MAX_BRIEF_TOTAL_CHARS + 1))
        picker_apply_key(model, "ENTER")
        self.assertIn("max 2000", model.error)
        type_line(model, "x" * 400)  # longer than a briefing line, still editable
        intent = picker_apply_key(model, "ENTER")
        self.assertEqual((intent.kind, len(intent.args["text"])), ("member_goal", 400))
        picker_apply_key(model, "CTRL_U")
        intent = picker_apply_key(model, "ENTER")
        self.assertEqual(intent.args["text"], "")  # an empty goal clears it


class RemoveConfirmTests(unittest.TestCase):
    def test_only_y_confirms_and_the_question_names_every_effect(self):
        model = managed(picker_model(ALPHA, focused=None))
        picker_apply_key(model, "4")
        self.assertIsNotNone(model.pending_action)
        self.assertEqual(model.status, "remove alpha-reviewer from alpha? its team tokens, pane label and Herdr agent name are cleared - y removes, n cancels")
        # ENTER is deliberately not yes: Enter opened the menu and picked the action
        self.assertIsNone(picker_apply_key(model, "ENTER"))
        self.assertIsNotNone(model.pending_action)
        for key in ("x", "DOWN", "1"):
            self.assertIsNone(picker_apply_key(model, key))
        self.assertIsNotNone(model.pending_action)
        intent = picker_apply_key(model, "y")
        self.assertEqual((intent.kind, intent.args["keep_name"]), ("member_remove", False))
        self.assertIsNone(model.pending_action)

    def test_n_and_esc_cancel(self):
        for key in ("n", "ESC"):
            model = managed(picker_model(ALPHA, focused=None))
            picker_apply_key(model, "4")
            self.assertIsNone(picker_apply_key(model, key))
            self.assertIsNone(model.pending_action)
            self.assertEqual(model.status, "cancelled")

    def test_the_keep_name_variant_says_so(self):
        model = managed(picker_model(ALPHA, focused=None))
        picker_apply_key(model, "5")
        self.assertIn("its team tokens and pane label are cleared", model.status)
        intent = picker_apply_key(model, "y")
        self.assertTrue(intent.args["keep_name"])


class ActionArgvTests(unittest.TestCase):
    def test_every_action_maps_to_its_cli(self):
        base = {"team": "alpha", "member": "alpha-reviewer", "terminal_id": "term_r1"}
        self.assertEqual(picker.action_args(Intent("member_remove", dict(base, keep_name=False))),
                         ["--team", "alpha", "remove", "alpha", "alpha-reviewer"])
        self.assertEqual(picker.action_args(Intent("member_remove", dict(base, keep_name=True)))[-1], "--keep-name")
        self.assertEqual(picker.action_args(Intent("member_rename", dict(base, new="alpha-lead"))),
                         ["--team", "alpha", "rename", "alpha-reviewer", "alpha-lead"])
        self.assertEqual(picker.action_args(Intent("member_goal", dict(base, text="Do it."))),
                         ["--team", "alpha", "brief", "alpha-reviewer", "--set", "Do it."])
        self.assertEqual(picker.action_args(Intent("member_send_goal", dict(base))),
                         ["--team", "alpha", "brief", "alpha-reviewer"])
        self.assertEqual(picker.action_args(Intent("member_focus", dict(base))),
                         ["--team", "alpha", "focus", "alpha-reviewer"])

    def test_failures_become_one_actionable_line(self):
        rename = Intent("member_rename", {"team": "alpha", "member": "alpha-reviewer", "new": "alpha-lead"})
        send = Intent("member_send_goal", {"team": "alpha", "member": "alpha-reviewer"})
        self.assertIn("only sending it needs the notifier", picker.action_failure_status(send, {"code": "daemon_down", "message": "x"}))
        self.assertIn("herdr-synapse daemon start", picker.action_failure_status(Intent("member_focus", send.args), {"code": "daemon_down", "message": "x"}))
        self.assertEqual(picker.action_failure_status(rename, {"code": "agent_name_taken", "message": "x", "candidates": ["alpha-lead-2"]}),
                         "Herdr already has an agent called alpha-lead; try alpha-lead-2")
        self.assertEqual(picker.action_failure_status(rename, {"code": "name_reserved", "message": "human is reserved"}), "human is reserved")
        self.assertIn("press r to refresh", picker.action_failure_status(rename, {"code": "member_not_found", "message": "x"}))
        self.assertIn("busy", picker.action_failure_status(rename, {"code": "lock_timeout", "message": "x"}))
        self.assertEqual(picker.action_failure_status(rename, {"code": "boom", "message": "it broke"}), "boom: it broke")


class ExecuteActionTests(unittest.TestCase):
    def run_action(self, ts, intent, rc=0, out=None, err=None, stage="select"):
        calls = []

        def fake_run_cli(args, env, timeout=0.0):
            calls.append((list(args), timeout))
            return rc, out, err

        model = picker_model(ALPHA, focused=None)
        model.rosters = picker.session_rosters(ts.layout)
        model.stage = stage
        from herdr_team import console as console_mod
        saved = console_mod.run_cli
        console_mod.run_cli = fake_run_cli
        try:
            keep_open = picker.execute_action(intent, model, FakeApi(ts.socket_path), ts.layout, {})
        finally:
            console_mod.run_cli = saved
        return model, calls, keep_open

    def test_a_successful_remove_refreshes_and_says_what_happened(self):
        with TempState() as ts:
            intent = Intent("member_remove", {"team": "alpha", "member": "alpha-worker", "terminal_id": "term_w1", "keep_name": False})
            model, calls, keep_open = self.run_action(ts, intent, out={"removed": "alpha-worker"})
            self.assertTrue(keep_open)
            self.assertEqual(calls[0][0], ["--team", "alpha", "remove", "alpha", "alpha-worker"])
            self.assertEqual(calls[0][1], picker.ACTION_TIMEOUT_S)
            self.assertEqual(model.status, "alpha-worker removed from alpha")
            self.assertEqual(model.stage, "select")

    def test_a_goal_that_saved_says_it_has_not_reached_the_agent(self):
        with TempState() as ts:
            intent = Intent("member_goal", {"team": "alpha", "member": "alpha-worker", "terminal_id": "term_w1", "text": "Do it."})
            model, _calls, _ = self.run_action(ts, intent, out={"brief": "Do it."})
            self.assertIn("does not reach the agent until you send it", model.status)

    def test_focus_closes_the_popup(self):
        with TempState() as ts:
            intent = Intent("member_focus", {"team": "alpha", "member": "alpha-worker", "terminal_id": "term_w1"})
            _model, _calls, keep_open = self.run_action(ts, intent, out={"job": "j1"})
            self.assertFalse(keep_open)

    def test_an_error_keeps_the_text_stage_so_it_can_be_corrected(self):
        with TempState() as ts:
            intent = Intent("member_rename", {"team": "alpha", "member": "alpha-worker", "terminal_id": "term_w1", "new": "alpha-lead"})
            model, _calls, keep_open = self.run_action(ts, intent, rc=1, err={"code": "agent_name_taken", "message": "taken"}, stage="rename")
            self.assertTrue(keep_open)
            self.assertIn("Herdr already has an agent called alpha-lead", model.error)
            self.assertEqual(model.stage, "rename")  # what was typed stays on screen

    def test_a_member_that_moved_is_never_acted_on(self):
        with TempState() as ts:
            intent = Intent("member_remove", {"team": "alpha", "member": "alpha-worker", "terminal_id": "term_somewhere_else", "keep_name": False})
            model, calls, keep_open = self.run_action(ts, intent)
            self.assertEqual(calls, [])
            self.assertTrue(keep_open)
            self.assertIn("changed while this was open", model.error)


if __name__ == "__main__":
    unittest.main()
