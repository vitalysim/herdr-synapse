"""How an agent learns its team has a canvas (0.21). Found live on 2026-09-27: an OpenCode agent given a
canvas of its own from ``prefix+t`` never heard of it. The typed briefing now says so, a canvas that
comes on wakes every member, and the teams view asks to trust a kind the notifier cannot type into."""
from __future__ import annotations

import time
import unittest

from herdr_team import daemon as D
from herdr_team import features, nudge, store
from herdr_team import picker_whiteboard as W
from herdr_team.tui_model import picker_apply_key
from support import TempState, whiteboard_on
from test_daemon import make_daemon
from test_picker_whiteboard import ENABLE, menu_on, model_for
from test_tui_model import type_line

CANVAS_LINE = nudge.CANVAS_BRIEFING.format(marker=nudge.MARKER_BRIEFING, cli=nudge.DEFAULT_CLI)


class BriefingText(unittest.TestCase):
    def test_the_canvas_line_comes_last_and_only_when_asked(self):
        plain = nudge.briefing_lines("alpha-worker", "worker", "alpha", "Ship it", [("alpha-reviewer", "reviewer")], "Fix the bug", "herdr-synapse")
        self.assertEqual(len(plain), 2)
        self.assertFalse(any("canvas" in line for line in plain))
        lines = nudge.briefing_lines("alpha-worker", "worker", "alpha", "Ship it", [("alpha-reviewer", "reviewer")], "Fix the bug", "herdr-synapse", canvas=True)
        self.assertEqual(lines[:2], plain)
        self.assertEqual(lines[2], CANVAS_LINE)
        self.assertIn("skill get --reference canvas", CANVAS_LINE)
        self.assertLessEqual(len(CANVAS_LINE), nudge.MAX_BRIEF_LINE_CHARS)
        solo = nudge.briefing_lines("gem", "gemini-dev", "gem-canvas", None, [], None, "herdr-synapse", canvas=True)
        self.assertEqual((len(solo), solo[-1]), (2, CANVAS_LINE))


class Notifier(unittest.TestCase):
    def setUp(self):
        self.ts = TempState()
        self.addCleanup(self.ts.cleanup)
        self.d, self.api, self.clock = make_daemon(self.ts)
        self.d.on_connected()

    def brief(self, member="alpha-reviewer"):
        obj = {"v": 1, "kind": "brief", "member": member, "force": False, "requested_by": {"name": "human"}, "requested_at": "now"}
        store.write_json(self.ts.team.jobs_dir / "{}-brief.json".format(int(time.time() * 1000)), obj)
        self.clock.advance(1)
        self.d.consume_jobs(self.clock() * 1000)
        return self.d.teams["alpha"].pending[member].lines

    def test_the_briefing_says_so_while_the_team_canvas_is_on(self):
        self.assertNotIn(CANVAS_LINE, self.brief())
        features.set_layer(self.ts.session, True, "human", "cli")
        self.assertNotIn(CANVAS_LINE, self.brief(), "the layer alone gives the team no canvas")
        features.set_team(self.ts.team, enabled=True, by="human", via="cli")
        self.d.scan_teams(force=True)
        self.assertEqual(self.brief()[-1], CANVAS_LINE)
        features.set_team(self.ts.team, enabled=False, by="human", via="cli")
        self.d.scan_teams(force=True)
        self.assertNotIn(CANVAS_LINE, self.brief())

    def test_the_line_follows_the_switch_when_the_briefing_is_typed(self):
        # A team of one from prefix+t is created first and its canvas turned on after, so the briefing
        # queued at the join must still carry the line when it is typed.
        team = self.d.teams["alpha"]
        lines = self.brief()
        self.assertNotIn(CANVAS_LINE, lines)
        whiteboard_on(self.ts.session, self.ts.team)
        self.d.scan_teams(force=True)
        self.assertEqual(self.d._canvas_briefing(team, lines)[-1], CANVAS_LINE)
        self.assertEqual(self.d._canvas_briefing(team, lines + [CANVAS_LINE]).count(CANVAS_LINE), 1)
        features.set_team(self.ts.team, enabled=False, by="human", via="cli")
        self.d.scan_teams(force=True)
        self.assertNotIn(CANVAS_LINE, self.d._canvas_briefing(team, lines + [CANVAS_LINE]))

    def pending_after(self, seq):
        self.clock.advance(1)
        self.d.tick()
        return {name: list(p.seqs) for name, p in self.d.teams["alpha"].pending.items() if p.kind == "nudge"}

    def test_a_canvas_that_came_on_wakes_every_member(self):
        whiteboard_on(self.ts.session, self.ts.team)
        seqs = features.announce_layer(self.ts.layout, False, True, "human")
        seq = seqs["alpha"]
        pending = self.pending_after(seq)
        self.assertEqual(sorted(pending), ["alpha-reviewer", "alpha-worker"], self.d.logged)
        self.assertTrue(all(seq in s for s in pending.values()))

    def test_switching_it_off_or_a_viz_change_stays_on_the_board(self):
        whiteboard_on(self.ts.session, self.ts.team)
        seq = features.announce_layer(self.ts.layout, True, False, "human")["alpha"]
        self.assertEqual(self.pending_after(seq), {})
        before = features.TeamSwitch("alpha", True, True, True)
        after = features.TeamSwitch("alpha", True, True, False)
        seq = features.announce(self.ts.layout, self.ts.team, before, after, "human")
        self.assertIsNotNone(seq)
        self.assertEqual(self.pending_after(seq), {})

    def test_came_on_reads_the_recorded_change(self):
        on = {"on": True}
        off = {"on": False}
        self.assertTrue(D.canvas_came_on({"whiteboard": {"before": off, "after": on}}))
        self.assertFalse(D.canvas_came_on({"whiteboard": {"before": on, "after": on}}))
        self.assertFalse(D.canvas_came_on({"whiteboard": {"before": on, "after": off}}))
        self.assertFalse(D.canvas_came_on({"whiteboard": "junk"}))
        self.assertFalse(D.canvas_came_on({}))


class PerTeam(unittest.TestCase):
    """The owner's call on 2026-09-27: turning the whiteboard on is per team, never every team."""

    def test_turning_one_teams_canvas_on_turns_the_layer_on_first_and_nothing_else(self):
        model = model_for(layer=False)
        menu_on(model, "team:alpha")
        labels = dict(W.options(model))
        self.assertIn("(only this team)", labels["team_canvas"])
        self.assertIn("turns the whiteboard on first", labels["team_canvas"])
        self.assertIn("each team's canvas is turned on by itself", labels["layer_on"])
        intent = picker_apply_key(model, str([c for c, _l in W.options(model)].index("team_canvas") + 1))
        self.assertEqual(intent.args["steps"], [ENABLE, ["--team", "alpha", "whiteboard", "team", "on"]])
        model = model_for(layer=True, teams={"alpha": {"canvas": True, "viz": True}})
        menu_on(model, "team:alpha")
        off = picker_apply_key(model, str([c for c, _l in W.options(model)].index("team_canvas") + 1))
        self.assertEqual(off.args["steps"], [["--team", "alpha", "whiteboard", "team", "off"]])


class TeamsViewTrust(unittest.TestCase):
    def solo_until_the_question(self):
        model = model_for()  # trusts claude and codex; gem is a gemini agent in no team
        menu_on(model, "pane:w1:p2")
        picker_apply_key(model, "2")
        picker_apply_key(model, "ENTER")
        type_line(model, "Draw the plan")
        self.assertIsNone(picker_apply_key(model, "ENTER"))
        return model

    def test_an_untrusted_kind_is_named_and_y_trusts_it_first(self):
        model = self.solo_until_the_question()
        self.assertEqual(model.stage, "select")
        self.assertIn("gemini is not trusted for typed delivery", model.status)
        self.assertIn("y trusts gemini and creates the team, n creates it without telling it", model.status)
        steps = picker_apply_key(model, "y").args["steps"]
        self.assertEqual(steps[0], ENABLE)
        self.assertEqual(steps[1][:3], ["kinds", "trust", "gemini"])
        self.assertEqual(steps[2][:2], ["create", "gem-canvas"])
        self.assertIsNone(model.pending_decline)

    def test_n_goes_ahead_without_and_says_what_is_missing(self):
        model = self.solo_until_the_question()
        intent = picker_apply_key(model, "n")
        self.assertEqual([step[0] for step in intent.args["steps"]], ["whiteboard", "create", "--team"])
        self.assertIn("is not told until you run: herdr-synapse kinds trust gemini", intent.args["done"])
        model = self.solo_until_the_question()
        self.assertIsNone(picker_apply_key(model, "ESC"))
        self.assertEqual(model.status, "cancelled")
        self.assertIsNone(model.pending_action)

    def test_a_member_of_an_untrusted_kind_can_be_trusted_from_the_menu(self):
        model = model_for(layer=True)
        model.trusted_kinds = {"claude"}
        menu_on(model, "member:alpha/alpha-reviewer")
        options = [choice for choice, _label in W.options(model)]
        self.assertIn("trust", options)
        intent = picker_apply_key(model, str(options.index("trust") + 1))
        self.assertEqual(intent.args["steps"][0][:3], ["kinds", "trust", "codex"])
        model.trusted_kinds = {"claude", "codex"}
        menu_on(model, "member:alpha/alpha-reviewer")
        self.assertNotIn("trust", [choice for choice, _label in W.options(model)])


if __name__ == "__main__":
    unittest.main()
