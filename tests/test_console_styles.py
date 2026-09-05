"""Color coding: every console line carries a style key; members get stable palette slots."""
from __future__ import annotations

import unittest

from herdr_team import tui_model as tm


MEMBERS = [
    {"name": "human", "kind": "human", "role": "operator", "status": "active"},
    {"name": "alpha-reviewer", "kind": "claude", "role": "reviewer", "agent_status": "idle", "status": "active"},
    {"name": "alpha-worker", "kind": "codex", "role": "worker", "agent_status": "working", "status": "active"},
]


def header() -> tm.ConsoleHeader:
    return tm.ConsoleHeader(team="alpha", members=3, view_on=False, nudges="on", toasts="herdr", unread=1, charter_seq=1, charter_headline="Ship it")


def model_with_feed() -> tm.ConsoleModel:
    model = tm.ConsoleModel(team="alpha", header=header(), members=list(MEMBERS), width=100, height=30)
    model.roster_lines = ["? human", "○ alpha-reviewer", "◐ alpha-worker"]
    model.feed = [
        {"seq": 1, "line": "#1 system charter_updated", "kind": "system", "from": "system"},
        {"seq": 2, "line": "#2 human -> all hello", "kind": "note", "from": "human"},
        {"seq": 3, "line": "#3 alpha-reviewer -> all starting", "kind": "note", "from": "alpha-reviewer"},
        {"seq": 4, "line": "#4 alpha-worker -> alpha-reviewer done", "kind": "done", "from": "alpha-worker"},
        {"seq": None, "line": "⚠ warning: alpha-worker tried to post as human", "kind": "warning", "from": "system", "warning": True},
    ]
    return model


class StyleTaggingTests(unittest.TestCase):
    def test_slots_follow_roster_order_and_skip_human(self):
        self.assertEqual(tm.member_color_slot("alpha-reviewer", MEMBERS), 0)
        self.assertEqual(tm.member_color_slot("alpha-worker", MEMBERS), 1)
        self.assertIsNone(tm.member_color_slot("human", MEMBERS))
        self.assertIsNone(tm.member_color_slot("nobody", MEMBERS))

    def test_entry_styles(self):
        self.assertEqual(tm.entry_style({"kind": "system", "from": "system"}), tm.STYLE_SYSTEM)
        self.assertEqual(tm.entry_style({"kind": "note", "from": "human"}), tm.STYLE_HUMAN)
        self.assertEqual(tm.entry_style({"kind": "note", "from": "human@vitaly"}), tm.STYLE_HUMAN)
        self.assertEqual(tm.entry_style({"kind": "note", "from": "alpha-worker"}), "member:alpha-worker")
        self.assertEqual(tm.entry_style({"kind": "note", "from": "alpha-worker", "warning": True}), tm.STYLE_WARNING)

    def test_render_tags_every_region(self):
        model = model_with_feed()
        styled = tm.render_console_styled(model, 100, 30)
        by_line = {line: style for line, style in styled if line}
        self.assertEqual(by_line["? human"], tm.STYLE_HUMAN)
        self.assertEqual(by_line["○ alpha-reviewer"], "member:alpha-reviewer")
        self.assertEqual(by_line["◐ alpha-worker"], "member:alpha-worker")
        self.assertEqual(by_line["#1 system charter_updated"], tm.STYLE_SYSTEM)
        self.assertEqual(by_line["#2 human -> all hello"], tm.STYLE_HUMAN)
        self.assertEqual(by_line["#3 alpha-reviewer -> all starting"], "member:alpha-reviewer")
        self.assertEqual(by_line["#4 alpha-worker -> alpha-reviewer done"], "member:alpha-worker")
        self.assertEqual(by_line["⚠ warning: alpha-worker tried to post as human"], tm.STYLE_WARNING)
        self.assertEqual(styled[-1][1], tm.STYLE_INPUT)
        self.assertEqual(styled[0][1], tm.STYLE_HEADER)
        # the plain renderer is exactly the styled one without styles
        self.assertEqual(tm.render_console(model, 100, 30), [line for line, _ in styled])

    def test_mention_rows_carry_member_colors_and_the_selection(self):
        model = model_with_feed()
        for key in "@":
            tm.apply_key(model, key)
        rows = tm.mention_rows_styled(model, 100)
        self.assertEqual(rows[0][1], tm.STYLE_MENU_SELECTED)
        self.assertEqual(rows[1][1], "member:alpha-worker")
        self.assertTrue(all(style == tm.STYLE_MENU for line, style in rows if " @role:" in line or " @all" in line or " @human" in line))
        tm.apply_key(model, "DOWN")
        rows = tm.mention_rows_styled(model, 100)
        self.assertEqual(rows[0][1], "member:alpha-reviewer")
        self.assertEqual(rows[1][1], tm.STYLE_MENU_SELECTED)


class RuntimeAttrTests(unittest.TestCase):
    def test_style_attr_without_colors_uses_bold_dim_reverse(self):
        import curses

        from herdr_team import console

        self.assertEqual(console.style_attr(tm.STYLE_HUMAN, MEMBERS, False), curses.A_BOLD)
        self.assertEqual(console.style_attr(tm.STYLE_SYSTEM, MEMBERS, False), curses.A_DIM)
        self.assertEqual(console.style_attr(tm.STYLE_MENU_SELECTED, MEMBERS, False), curses.A_REVERSE)
        self.assertEqual(console.style_attr(tm.STYLE_WARNING, MEMBERS, False), curses.A_BOLD)
        self.assertEqual(console.style_attr("member:alpha-worker", MEMBERS, False), 0)
        self.assertEqual(console.style_attr("member:nobody", MEMBERS, True), 0)
        self.assertEqual(console.style_attr(tm.STYLE_PLAIN, MEMBERS, True), 0)

    def test_palette_wraps_and_avoids_red(self):
        from herdr_team import console

        self.assertNotIn("red", console.MEMBER_PALETTE)
        many = [{"name": "m{}".format(i), "kind": "claude"} for i in range(9)]
        self.assertEqual(tm.member_color_slot("m8", many), 8)
        self.assertEqual(console.MEMBER_PALETTE[8 % len(console.MEMBER_PALETTE)], console.MEMBER_PALETTE[2])


if __name__ == "__main__":
    unittest.main()
