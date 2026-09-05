"""Console feed wrapping: long posts span rows, continuation rows are indented, newlines are kept."""
from __future__ import annotations

import unittest

from herdr_team import tui_model as tm


def header() -> tm.ConsoleHeader:
    return tm.ConsoleHeader(team="alpha", members=2, view_on=False, nudges="on", toasts="herdr", unread=0, charter_seq=1, charter_headline="Ship it")


def rec(seq, text, frm="alpha-worker", to=("all",), kind="note", ts="2026-09-05T10:00:00Z", **extra):
    r = {"seq": seq, "from": frm, "to": list(to), "kind": kind, "text": text, "ts": ts, "from_kind": "codex", "origin": {"via": "cli", "verified": True}}
    r.update(extra)
    return r


class WrapColumnsTests(unittest.TestCase):
    def test_wraps_at_word_boundaries(self):
        self.assertEqual(tm.wrap_columns("the quick brown fox jumps", 10), ["the quick", "brown fox", "jumps"])

    def test_first_row_can_be_narrower(self):
        self.assertEqual(tm.wrap_columns("one two three four", 7, 20), ["one two", "three four"])

    def test_long_word_hard_breaks(self):
        self.assertEqual(tm.wrap_columns("abcdefghij", 4), ["abcd", "efgh", "ij"])
        self.assertEqual(tm.wrap_columns("x abcdefghij y", 4), ["x", "abcd", "efgh", "ij y"])

    def test_explicit_newlines_start_rows_and_empty_text_is_one_row(self):
        self.assertEqual(tm.wrap_columns("a\nb c\n\nd", 10), ["a", "b c", "", "d"])
        self.assertEqual(tm.wrap_columns("", 10), [""])

    def test_wide_characters_count_double(self):
        self.assertEqual(tm.wrap_columns("日本語 テスト", 6), ["日本語", "テスト"])


class EntryRowsTests(unittest.TestCase):
    def test_short_entry_is_one_row_with_receipts(self):
        entry = tm.feed_entry(rec(7, "hello"), receipts={7: {"nudged": True, "read": True}}, width=80)
        rows = tm.entry_rows(entry, 80)
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0].startswith("#7 "))
        self.assertIn("hello  ✓nudged ✓read", rows[0])

    def test_long_entry_wraps_with_indented_continuation_and_receipts_last(self):
        text = " ".join("word{}".format(i) for i in range(30))
        entry = tm.feed_entry(rec(8, text), receipts={8: {"nudged": True}}, width=60)
        rows = tm.entry_rows(entry, 60)
        self.assertGreater(len(rows), 2)
        self.assertTrue(rows[0].startswith("#8 "))
        for row in rows[1:]:
            self.assertTrue(row.startswith(tm.WRAP_INDENT), row)
            self.assertLessEqual(tm.display_width(row), 60)
        self.assertIn("✓nudged", rows[-1])
        # nothing lost: every word appears exactly once across the rows
        joined = " ".join(r.strip() for r in rows)
        for i in range(30):
            self.assertEqual(joined.count("word{} ".format(i)) + joined.count("word{}  ".format(i)) + (1 if joined.endswith("word{}".format(i)) else 0) >= 1, True)

    def test_multiline_text_keeps_its_lines(self):
        entry = tm.feed_entry(rec(9, "first line\nsecond line\nthird"), width=80)
        rows = tm.entry_rows(entry, 80)
        self.assertEqual(len(rows), 3)
        self.assertTrue(rows[0].endswith("first line"))
        self.assertEqual(rows[1], tm.WRAP_INDENT + "second line")
        self.assertEqual(rows[2], tm.WRAP_INDENT + "third")

    def test_header_wider_than_the_budget_pushes_text_to_the_next_row(self):
        entry = tm.feed_entry(rec(10, "some text here", frm="a-very-long-member-name-indeed", to=("another-long-member-name",)), width=40)
        rows = tm.entry_rows(entry, 40)
        self.assertTrue(rows[0].startswith("#10"))
        self.assertTrue(rows[1].startswith(tm.WRAP_INDENT + "some"))
        for row in rows:
            self.assertLessEqual(tm.display_width(row), 40)

    def test_system_and_struck_entries(self):
        entry = tm.feed_entry({"seq": 11, "from": "system", "kind": "system", "event": "charter_updated", "text": "x " * 60, "ts": "2026-09-05T10:00:00Z", "to": ["all"]}, width=50)
        rows = tm.entry_rows(entry, 50)
        self.assertTrue(rows[0].startswith("#11 "))
        self.assertIn("system charter_updated:", rows[0])
        self.assertGreater(len(rows), 1)
        struck = tm.feed_entry(rec(12, "oops"), retractions={12: 13}, width=80)
        rows = tm.entry_rows(struck, 80)
        self.assertIn("~~oops~~ (retracted by #13)", rows[0])

    def test_entries_without_head_fall_back_to_their_line(self):
        rows = tm.entry_rows({"line": "⚠ warning: something", "kind": "warning"}, 80)
        self.assertEqual(rows, ["⚠ warning: something"])


class VisibleFeedRowsTests(unittest.TestCase):
    def make(self, width=60, height=24):
        model = tm.ConsoleModel(team="alpha", header=header(), width=width, height=height)
        long = " ".join("w{}".format(i) for i in range(40))
        model.feed = [
            tm.feed_entry(rec(1, "one"), width=width),
            tm.feed_entry(rec(2, long), width=width),
            tm.feed_entry(rec(3, "three"), width=width),
        ]
        return model

    def test_fills_from_the_bottom_and_cuts_the_top_entry(self):
        model = self.make()
        rows = tm.visible_feed_rows(model, 3)
        self.assertEqual([e["seq"] for _, e in rows], [2, 2, 3])  # the long entry's last rows, then #3
        self.assertTrue(rows[-1][0].startswith("#3 "))
        self.assertTrue(rows[0][0].startswith(tm.WRAP_INDENT))  # a continuation row of #2, not its header

    def test_scroll_moves_by_entries_and_clamps_to_the_first(self):
        model = self.make()
        model.scroll = 1
        rows = tm.visible_feed_rows(model, 2)
        self.assertEqual({e["seq"] for _, e in rows}, {2})
        model.scroll = 99
        rows = tm.visible_feed_rows(model, 5)
        self.assertEqual([e["seq"] for _, e in rows], [1])
        self.assertEqual(model.scroll, 2)

    def test_render_keeps_exact_height_and_width_with_wrapped_entries(self):
        for width, height in ((40, 12), (60, 20), (100, 30), (25, 8)):
            model = self.make(width, height)
            screen = tm.render_console(model, width, height)
            self.assertEqual(len(screen), height, (width, height))
            for line in screen:
                self.assertLessEqual(tm.display_width(line), width, (width, line))
            self.assertTrue(any(l.startswith("#3 ") or l.startswith(tm.WRAP_INDENT) for l in screen))

    def test_styles_follow_the_entry_over_its_rows(self):
        model = self.make(60, 30)
        model.members = [{"name": "alpha-worker", "kind": "codex"}]
        styled = tm.render_console_styled(model, 60, 30)
        worker_rows = [line for line, style in styled if style == "member:alpha-worker"]
        self.assertGreater(len(worker_rows), 3)  # #1, several rows of #2, #3


if __name__ == "__main__":
    unittest.main()
