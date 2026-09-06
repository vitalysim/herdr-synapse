"""Each team gets its own colour in Herdr's Agents sidebar.

Herdr styles a sidebar cell from a fixed `fg` in the user's config and cannot
colour by a token's value, so the plugin gives each team a numbered slot and
the recommended row carries one differently-coloured cell per slot. A row
drops the tokens that have no value, so exactly one coloured cell renders per
member.
"""

from __future__ import annotations

import unittest

try:
    import tomllib
except ModuleNotFoundError:  # Python 3.9/3.10
    tomllib = None

from herdr_team import cmd_misc, hooks, roster, store
from support import FakeApi, TempState, identity_tokens
from test_daemon import make_daemon


class SlotAssignmentTests(unittest.TestCase):
    def test_the_lowest_free_slot_wins_and_colours_repeat_past_six(self):
        self.assertEqual(roster.free_color_slot([]), 1)
        self.assertEqual(roster.free_color_slot([1, 2]), 3)
        self.assertEqual(roster.free_color_slot([2, 3]), 1)  # a dissolved team frees its colour
        self.assertEqual(roster.free_color_slot([None, "x", 2]), 1)  # junk is not a claim
        full = list(range(1, roster.TEAM_COLOR_SLOTS + 1))
        self.assertEqual(roster.free_color_slot(full, 6), 1)
        self.assertEqual(roster.free_color_slot(full, 7), 2)

    def test_a_slot_is_read_back_only_when_it_is_usable(self):
        self.assertEqual(roster.color_slot_of({"config": {"color_slot": 4}}), 4)
        for bad in ({}, {"config": {}}, {"config": {"color_slot": 0}}, {"config": {"color_slot": 7}},
                    {"config": {"color_slot": True}}, {"config": {"color_slot": "2"}}, {"config": "x"}, None):
            self.assertIsNone(roster.color_slot_of(bad), bad)

    def test_a_team_exposes_its_own_slot(self):
        team = roster.Team(team="alpha", socket="/s", state_dir="/d", created_at="2026-09-06T00:00:00Z", config={"color_slot": 5})
        self.assertEqual(team.color_slot, 5)
        self.assertIsNone(roster.Team(team="b", socket="/s", state_dir="/d", created_at="x").color_slot)


class TokenMapTests(unittest.TestCase):
    def test_one_slot_carries_the_name_and_the_rest_are_cleared(self):
        tokens = roster.color_slot_tokens("red-dev", 3)
        self.assertEqual(sorted(tokens), ["team_c1", "team_c2", "team_c3", "team_c4", "team_c5", "team_c6"])
        self.assertEqual(tokens["team_c3"], "red-dev")
        self.assertEqual([tokens[k] for k in tokens if k != "team_c3"], [None] * 5)

    def test_an_unassigned_or_impossible_slot_clears_every_cell(self):
        for team, slot in (("red-dev", None), ("red-dev", 0), ("red-dev", 9), ("red-dev", True), (None, 3), ("", 3)):
            self.assertEqual(set(roster.color_slot_tokens(team, slot).values()), {None}, (team, slot))

    def test_the_stamp_carries_identity_and_the_colour_together(self):
        member = roster.Member("alpha-reviewer", "reviewer", "codex", "term_r1", pane_id="w2:p1")
        commands = roster.token_commands(member, "alpha", color_slot=2)
        self.assertEqual(len(commands), 1)
        self.assertEqual(commands[0].tokens, identity_tokens("alpha", "reviewer", 2))
        self.assertLessEqual(len(commands[0].tokens), 16)  # Herdr accepts at most 16 keys per report
        self.assertEqual(len([v for v in commands[0].tokens.values() if v is not None]), 3)
        # a member that moves teams loses its old colour in the same patch
        moved = roster.token_commands(member, "beta", color_slot=5)[0].tokens
        self.assertEqual(moved["team_c5"], "beta")
        self.assertIsNone(moved["team_c2"])

    def test_clearing_removes_every_slot_too(self):
        member = roster.Member("alpha-reviewer", "reviewer", "codex", "term_r1", pane_id="w2:p1")
        cleared = roster.token_commands(member, "alpha", clear=True)[0].tokens
        self.assertEqual(cleared, identity_tokens(None, None))
        self.assertEqual(set(cleared.values()), {None})

    def test_the_hook_keeps_its_own_copy_of_the_slot_keys_in_step(self):
        """hooks.py deliberately does not import roster; this is what keeps the two honest."""
        self.assertEqual(list(hooks.COLOR_SLOT_KEYS), roster.color_slot_keys())
        self.assertEqual(hooks._color_slot_tokens("alpha", 2), roster.color_slot_tokens("alpha", 2))
        self.assertEqual(hooks._roster_clear_tokens(), identity_tokens(None, None))
        self.assertEqual(hooks._color_slot_of({"config": {"color_slot": 3}}), 3)
        self.assertIsNone(hooks._color_slot_of({}))


class DaemonAssignsTheSlotTests(unittest.TestCase):
    def test_a_team_without_a_slot_gets_one_and_keeps_it(self):
        with TempState() as ts:
            d, api, _clock = make_daemon(ts)
            d.on_connected()
            team = d.teams["alpha"]
            self.assertEqual(roster.color_slot_of(team.roster), 1)
            self.assertEqual(store.read_json(ts.team.team_json)["config"]["color_slot"], 1)
            self.assertTrue(any("sidebar colour slot 1 (red)" in line for line in d.logged), d.logged[-4:])
            # a reload does not reassign
            before = len([line for line in d.logged if "sidebar colour slot" in line])
            d._reload_roster(team)
            self.assertEqual(len([line for line in d.logged if "sidebar colour slot" in line]), before)

    def test_the_slot_reaches_the_pane_as_a_token(self):
        with TempState() as ts:
            d, api, _clock = make_daemon(ts)
            d.on_connected()
            d.heartbeat()
            stamps = [p for m, p in api.calls if m == "pane.report_metadata" and p.get("source") == "herdr-team:roster"]
            self.assertTrue(stamps)
            self.assertEqual(stamps[0]["tokens"], identity_tokens("alpha", "reviewer", 1))
            self.assertNotIn("ttl_ms", stamps[0])  # the colour is identity, it must not fade

    def test_clearing_a_pane_clears_the_colour(self):
        with TempState() as ts:
            d, api, _clock = make_daemon(ts)
            d._clear_tokens("w2:p1")
            cleared = [p for m, p in api.calls if m == "pane.report_metadata" and p.get("source") == "herdr-team:roster"]
            self.assertEqual(cleared[0]["tokens"], identity_tokens(None, None))

    def test_a_busy_roster_lock_defers_the_slot_rather_than_failing(self):
        with TempState() as ts:
            d, _api, _clock = make_daemon(ts)
            team_doc = store.read_json(ts.team.team_json)
            self.assertNotIn("color_slot", team_doc.get("config", {}))
            d.on_connected()
            team = d.teams["alpha"]
            team.roster.pop("config", None)  # pretend the slot is still missing
            with store.FileLock(ts.team.team_lock, timeout=0.1):
                d._assign_color_slot(team)  # must not raise
            self.assertTrue(any("colour slot deferred" in line for line in d.logged), d.logged[-4:])


class SidebarSnippetTests(unittest.TestCase):
    def test_the_row_has_one_coloured_cell_per_slot(self):
        snippet = cmd_misc.SIDEBAR_SNIPPET
        for slot, hex_value in enumerate(roster.TEAM_COLOR_HEX, start=1):
            self.assertIn('{{ token = "${}", fg = "{}" }}'.format(roster.color_slot_key(slot), hex_value), snippet)
        self.assertEqual(len(roster.TEAM_COLOR_HEX), roster.TEAM_COLOR_SLOTS)
        self.assertEqual(len(roster.TEAM_COLOR_NAMES), roster.TEAM_COLOR_SLOTS)
        for hex_value in roster.TEAM_COLOR_HEX:
            self.assertRegex(hex_value, r"^#[0-9a-f]{6}$")  # Herdr accepts only #RGB or #RRGGBB

    @unittest.skipIf(tomllib is None, "tomllib needs Python 3.11+")
    def test_the_snippet_is_valid_toml_within_herdrs_limits(self):
        doc = tomllib.loads(cmd_misc.SIDEBAR_SNIPPET)
        agents = doc["ui"]["sidebar"]["agents"]
        for rows in [agents["rows"]] + list(agents["rows_by_agent"].values()):
            self.assertLessEqual(len(rows), 16)
            for row in rows:
                self.assertLessEqual(len(row), 16)
            colour_row = [r for r in rows if isinstance(r[0], dict) and r[0]["token"] == "$team_c1"]
            self.assertEqual(len(colour_row), 1)
            cells = colour_row[0]
            self.assertEqual([c["token"] for c in cells[:6]], ["$" + roster.color_slot_key(i) for i in range(1, 7)])
            self.assertEqual([c["fg"] for c in cells[:6]], list(roster.TEAM_COLOR_HEX))
            self.assertEqual(cells[6], {"token": "$team_role", "dim": True})
            self.assertEqual(cells[7], {"token": "$team_task", "fg": "#89b4fa"})
            for cell in cells:  # Herdr rejects any other key in a styled cell
                self.assertLessEqual(set(cell), {"token", "fg", "bold", "dim"})


class StaleConfigWarningTests(unittest.TestCase):
    def test_doctor_warns_only_about_a_sidebar_block_that_predates_the_colours(self):
        with TempState() as ts:
            config = ts.config_dir / "config.toml"
            config.write_text("[ui.toast]\ndelivery = \"terminal\"\n")
            self.assertFalse(cmd_misc.sidebar_missing_team_colors(ts.config_dir))  # no sidebar at all
            config.write_text('[ui.sidebar.agents]\nrows = [[{ token = "$team_role", dim = true }]]\n')
            self.assertTrue(cmd_misc.sidebar_missing_team_colors(ts.config_dir))
            config.write_text(cmd_misc.SIDEBAR_SNIPPET)
            self.assertFalse(cmd_misc.sidebar_missing_team_colors(ts.config_dir))
        self.assertFalse(cmd_misc.sidebar_missing_team_colors(ts.config_dir / "gone"))


if __name__ == "__main__":
    unittest.main()
