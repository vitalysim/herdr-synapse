"""Typing ``/`` in the console opens the command menu, with placeholders.

The console had menus for ``@`` names, ``@@`` files and ``!`` members, but the
commands themselves were discoverable only through ``/help``.
"""

from __future__ import annotations

import unittest

from herdr_team import tui_model


def model(text: str = ""):
    m = tui_model.build_console_model("alpha", {}, [])
    for ch in text:
        tui_model.apply_key(m, ch)
    return m


class SlashMenuTests(unittest.TestCase):
    def test_a_bare_slash_lists_every_command(self):
        rows = tui_model.mention_menu(model("/"))
        self.assertEqual(len(rows), len(tui_model.SLASH_COMMANDS))
        self.assertEqual({"/" + r["insert"] for r in rows}, set(tui_model.SLASH_COMMANDS))

    def test_every_row_carries_its_placeholder(self):
        rows = {r["insert"]: r["label"] for r in tui_model.mention_menu(model("/"))}
        self.assertIn("N", rows["reply"])
        self.assertIn("[path]", rows["export"])
        self.assertIn("name", rows["peek"])
        self.assertIn("note|request", rows["kind"])

    def test_every_row_says_what_it_does(self):
        for row in tui_model.mention_menu(model("/")):
            self.assertGreater(len(row["label"]), len(row["insert"]) + 3, row["insert"])

    def test_every_command_has_an_entry(self):
        """A new command must not ship without telling the operator how to type it."""
        self.assertEqual(set(tui_model.SLASH_USAGE), set(tui_model.SLASH_COMMANDS))

    def test_typing_narrows_the_menu(self):
        rows = tui_model.mention_menu(model("/exp"))
        self.assertEqual([r["insert"] for r in rows], ["export"])

    def test_a_substring_still_finds_it(self):
        self.assertIn("export", [r["insert"] for r in tui_model.mention_menu(model("/port"))])

    def test_a_prefix_match_sorts_first(self):
        rows = [r["insert"] for r in tui_model.mention_menu(model("/u"))]
        self.assertTrue(rows[0].startswith("u"), rows[:3])

    def test_tab_completes_without_the_placeholder(self):
        m = model("/exp")
        tui_model.apply_key(m, "TAB")
        self.assertEqual(m.input, "/export ")
        self.assertEqual(m.cursor, len("/export "))

    def test_enter_runs_a_fully_typed_command(self):
        """The menu is a hint, not a gate: Enter must not need a second press."""
        self.assertEqual(tui_model.apply_key(model("/quit"), "ENTER").kind, "quit")

    def test_arrows_move_the_highlight_and_tab_picks_it(self):
        m = model("/")
        tui_model.apply_key(m, "DOWN")
        second = tui_model.mention_menu(m)[1]["insert"]
        tui_model.apply_key(m, "TAB")
        self.assertEqual(m.input, "/{} ".format(second))

    def test_esc_hides_it(self):
        m = model("/")
        self.assertTrue(tui_model.mention_menu(m))
        tui_model.apply_key(m, "ESC")
        self.assertEqual(tui_model.mention_menu(m), [])

    def test_it_only_opens_at_the_head_of_the_line(self):
        for text in ("hello /foo", "see a/b", "look at ./x", "x"):
            self.assertEqual(tui_model.mention_menu(model(text)), [], text)

    def test_a_file_path_menu_is_unaffected(self):
        """``@@/usr`` is a file completion, not a command menu."""
        m = model("@@")
        self.assertNotEqual(tui_model.mention_sigil(m), "/")

    def test_the_menu_renders_within_the_width(self):
        m = model("/")
        for width in (40, 60, 100):
            for line in tui_model.mention_lines(m, width):
                self.assertLessEqual(tui_model.display_width(line), width, (width, line))

    def test_the_compose_popup_offers_only_the_post_directives(self):
        """The popup parses only these, so offering the rest would mislead."""
        compose = tui_model.ComposeModel()
        compose.input, compose.cursor = "/", 1
        rows = ["/" + r["insert"] for r in tui_model.mention_menu(compose)]
        self.assertEqual(rows, list(tui_model.POST_DIRECTIVES))
        self.assertNotIn("/quit", rows)

    def test_it_can_be_turned_off_entirely(self):
        m = model("/")
        m.slash_menu = False
        self.assertEqual(tui_model.mention_menu(m), [])


if __name__ == "__main__":
    unittest.main()


class SlashMenuFooterTests(unittest.TestCase):
    def test_the_command_menu_footer_does_not_promise_enter(self):
        lines = tui_model.mention_lines(model("/"), 92)
        self.assertIn("Tab picks", lines[-1])
        self.assertNotIn("Enter", lines[-1])

    def test_the_name_menu_footer_still_does(self):
        m = tui_model.build_console_model("alpha", {}, [])
        m.members = [{"name": "alpha-{}".format(i), "kind": "claude", "role": "worker", "status": "active"} for i in range(9)]
        tui_model.apply_key(m, "@")
        lines = tui_model.mention_lines(m, 92)
        self.assertTrue(lines)
        if len(tui_model.mention_menu(m)) > tui_model.MENTION_MENU_ROWS:
            self.assertIn("Tab or Enter picks", lines[-1])

    def test_the_help_names_the_command_menu(self):
        self.assertIn("/ commands", "\n".join(tui_model.help_lines()))
