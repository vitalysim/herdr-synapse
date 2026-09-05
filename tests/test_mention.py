"""The console and compose ``@`` mention menu: type ``@``, pick a name with the arrows, Tab or Enter."""
from __future__ import annotations

import unittest

from herdr_team import tui_model as tm


MEMBERS = [
    {"name": "human", "kind": "human", "role": "operator", "status": "active"},
    {"name": "red-dev-claude-dev", "kind": "claude", "role": "claude-dev", "agent_status": "working", "status": "active"},
    {"name": "red-dev-codex-reviewer", "kind": "codex", "role": "codex-reviewer", "agent_status": "idle", "status": "active"},
    {"name": "red-dev-tester", "kind": "codex", "role": "codex-reviewer", "agent_status": "idle", "status": "active"},
    {"name": "red-dev-old", "kind": "claude", "role": "scribe", "status": "left"},
]


def header() -> tm.ConsoleHeader:
    return tm.ConsoleHeader(team="red-dev", members=3, view_on=False, nudges="on", toasts="terminal", unread=0, charter_seq=1, charter_headline="Developing red team tools")


def console(text: str = "", cursor: int = None, members=MEMBERS) -> tm.ConsoleModel:
    model = tm.ConsoleModel(team="red-dev", header=header(), members=list(members), width=100, height=24)
    model.input = text
    model.cursor = len(text) if cursor is None else cursor
    return model


def type_keys(model, keys: str):
    intents = []
    for key in keys:
        intents.append(tm.apply_key(model, key))
    return intents


class MentionContextTests(unittest.TestCase):
    def test_at_start_and_after_space_open_the_menu(self):
        self.assertEqual(tm.mention_context("@", 1), (0, 1, "", "@"))
        self.assertEqual(tm.mention_context("@rev", 4), (0, 4, "rev", "@"))
        self.assertEqual(tm.mention_context("hello @re", 9), (6, 9, "re", "@"))

    def test_mid_word_and_no_at_do_not(self):
        self.assertIsNone(tm.mention_context("mail me at foo@bar", 18))
        self.assertIsNone(tm.mention_context("plain text", 10))
        self.assertIsNone(tm.mention_context("@name done", 10))  # cursor after the token, on another word

    def test_cursor_inside_the_token(self):
        self.assertEqual(tm.mention_context("@red-dev rest", 4), (0, 4, "red", "@"))


class MentionCandidateTests(unittest.TestCase):
    def test_empty_prefix_lists_members_roles_all_human_and_skips_left_and_human_rows(self):
        rows = [r["insert"] for r in tm.mention_candidates(MEMBERS, "")]
        self.assertEqual(rows, [
            "red-dev-claude-dev", "red-dev-codex-reviewer", "red-dev-tester",
            "role:claude-dev", "role:codex-reviewer", "all", "human",
        ])

    def test_prefix_beats_substring_and_roles_match_by_role(self):
        rows = [r["insert"] for r in tm.mention_candidates(MEMBERS, "rev")]
        # role:codex-reviewer contains "rev"; both reviewer-role members match through the role
        self.assertIn("red-dev-codex-reviewer", rows)
        self.assertIn("red-dev-tester", rows)
        self.assertIn("role:codex-reviewer", rows)
        self.assertNotIn("red-dev-claude-dev", rows)
        self.assertNotIn("all", rows)
        rows = [r["insert"] for r in tm.mention_candidates(MEMBERS, "al")]
        self.assertEqual(rows[0], "all")  # prefix match first

    def test_labels_show_role_kind_and_status(self):
        row = [r for r in tm.mention_candidates(MEMBERS, "claude")][0]
        self.assertEqual(row["insert"], "red-dev-claude-dev")
        self.assertIn("claude-dev · claude · working", row["label"])
        role_row = [r for r in tm.mention_candidates(MEMBERS, "role:codex")][0]
        self.assertIn("everyone with role codex-reviewer (2)", role_row["label"])

    def test_no_match_closes_the_menu(self):
        self.assertEqual(tm.mention_candidates(MEMBERS, "zzz"), [])


class ConsoleMentionKeyTests(unittest.TestCase):
    def test_typing_at_opens_menu_and_tab_inserts_the_highlighted_name(self):
        model = console()
        type_keys(model, "@")
        self.assertTrue(tm.mention_menu(model))
        lines = tm.render_console(model, 100, 24)
        menu = [l for l in lines if l.startswith(tm.MENTION_MARKER + " @") or l.startswith("  @")]
        self.assertTrue(any(l.startswith(tm.MENTION_MARKER + " @red-dev-claude-dev") for l in menu), menu)
        self.assertIsNone(tm.apply_key(model, "TAB"))
        self.assertEqual(model.input, "@red-dev-claude-dev ")
        self.assertEqual(model.cursor, len(model.input))
        self.assertEqual(tm.mention_menu(model), [])  # cursor is after the space: closed

    def test_arrows_move_the_highlight_and_enter_picks_instead_of_posting(self):
        model = console()
        type_keys(model, "@")
        self.assertIsNone(tm.apply_key(model, "DOWN"))
        self.assertIsNone(tm.apply_key(model, "DOWN"))
        self.assertEqual(model.mention_index, 2)
        self.assertIsNone(tm.apply_key(model, "UP"))
        intent = tm.apply_key(model, "ENTER")
        self.assertIsNone(intent)  # Enter accepted the completion, no post
        self.assertEqual(model.input, "@red-dev-codex-reviewer ")
        self.assertEqual(model.scroll, 0)  # the arrows did not scroll the feed

    def test_typing_filters_and_then_enter_posts_normally(self):
        model = console()
        type_keys(model, "@tes")
        self.assertEqual([r["insert"] for r in tm.mention_menu(model)], ["red-dev-tester"])
        tm.apply_key(model, "TAB")
        type_keys(model, "please review")
        intent = tm.apply_key(model, "ENTER")
        self.assertEqual(intent.kind, "post")
        self.assertEqual(intent.args["to"], ["red-dev-tester"])
        self.assertEqual(intent.args["text"], "please review")

    def test_esc_hides_until_the_input_changes(self):
        model = console()
        type_keys(model, "@")
        self.assertIsNone(tm.apply_key(model, "ESC"))
        self.assertEqual(tm.mention_menu(model), [])
        self.assertEqual(model.input, "@")
        type_keys(model, "r")
        self.assertTrue(tm.mention_menu(model))

    def test_role_and_all_and_human_are_offered(self):
        model = console()
        type_keys(model, "@role")
        inserts = [r["insert"] for r in tm.mention_menu(model)]
        self.assertEqual(inserts, ["role:claude-dev", "role:codex-reviewer"])
        model = console()
        type_keys(model, "@hu")
        tm.apply_key(model, "TAB")
        self.assertEqual(model.input, "@human ")

    def test_menu_scrolls_beyond_six_rows_and_reports_position(self):
        many = [{"name": "m{:02d}".format(i), "kind": "claude", "role": "r", "agent_status": "idle"} for i in range(12)]
        model = console(members=many)
        type_keys(model, "@")
        for _ in range(8):
            tm.apply_key(model, "DOWN")
        lines = tm.mention_lines(model, 100)
        self.assertEqual(len(lines), tm.MENTION_MENU_ROWS + 1)
        self.assertIn("9 of 15", lines[-1])  # 12 members + 1 shared role + all + human
        self.assertTrue(any(l.startswith(tm.MENTION_MARKER + " @m08") for l in lines))

    def test_paste_mode_and_mid_word_at_do_not_open(self):
        model = console()
        type_keys(model, "mail foo@bar")
        self.assertEqual(tm.mention_menu(model), [])
        model = console()
        tm.apply_key(model, "PASTE_START")
        type_keys(model, "@")
        self.assertEqual(tm.mention_menu(model), [])
        tm.apply_key(model, "PASTE_END")
        self.assertTrue(tm.mention_menu(model))

    def test_ascii_marker_when_ascii_only(self):
        model = console()
        model.ascii_only = True
        type_keys(model, "@")
        lines = tm.mention_lines(model, 100, ascii_only=True)
        self.assertTrue(lines[0].startswith("> @"))


class ComposeMentionTests(unittest.TestCase):
    def test_compose_menu_uses_roster_members_and_esc_hides_before_quitting(self):
        model = tm.ComposeModel(default_to=None, team="red-dev", roster_members=list(MEMBERS))
        for key in "@":
            tm.compose_apply_key(model, key)
        self.assertTrue(tm.mention_menu(model))
        lines = tm.compose_lines(model, 100)
        self.assertTrue(any(l.startswith(tm.MENTION_MARKER + " @red-dev-claude-dev") for l in lines), lines)
        self.assertIsNone(tm.compose_apply_key(model, "ESC"))  # hides the menu, does not quit
        self.assertEqual(tm.mention_menu(model), [])
        quit_intent = tm.compose_apply_key(model, "ESC")
        self.assertEqual(quit_intent.kind, "quit")

    def test_compose_tab_then_enter_posts_to_the_picked_member(self):
        model = tm.ComposeModel(default_to=None, team="red-dev", roster_names=["alpha", "beta"])
        for key in "@be":
            tm.compose_apply_key(model, key)
        self.assertIsNone(tm.compose_apply_key(model, "TAB"))
        self.assertEqual(model.input, "@beta ")
        for key in "hi":
            tm.compose_apply_key(model, key)
        intent = tm.compose_apply_key(model, "ENTER")
        self.assertEqual(intent.kind, "post")
        self.assertEqual(intent.args["to"], ["beta"])


if __name__ == "__main__":
    unittest.main()
