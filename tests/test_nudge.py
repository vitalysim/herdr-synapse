"""Nudge, briefing, probe, and echo-filter tests (plan 8.3, 9.2)."""

from __future__ import annotations

import unittest

from herdr_team import nudge
from herdr_team.nudge import (
    MAX_BRIEFING_CHARS, MAX_NUDGE_CHARS, NudgeTextError, briefing_lines, build, is_echo, new_nonce,
    nudge_text, probe_line, probe_text,
)

TEAMMATES = [("vuln-hunt-reviewer", "reviewer"), ("vuln-hunt-worker", "worker")]


class NudgeTextTests(unittest.TestCase):
    def test_exact_plural_text(self):
        self.assertEqual(
            nudge_text("reviewer", [41, 42], 17),
            "[herdr-team nudge] 2 new board posts for reviewer (seq 41-42). Run: herdr-team board --new [n17]",
        )

    def test_exact_singular_text(self):
        self.assertEqual(
            nudge_text("reviewer", [41], 17),
            "[herdr-team nudge] 1 new board post for reviewer (seq 41). Run: herdr-team board --new [n17]",
        )

    def test_build_alias_and_ranges(self):
        self.assertEqual(build("reviewer", [42, 41, 42], 3), nudge_text("reviewer", [41, 42], 3))
        self.assertIn("(seq 5-9)", nudge_text("x", [9, 7, 5], 1))
        self.assertIn("3 new board posts", nudge_text("x", [9, 7, 5], 1))

    def test_length_cap(self):
        text = nudge_text("vuln-hunt-reviewer-2", [123456, 123999], 987654)
        self.assertLessEqual(len(text), MAX_NUDGE_CHARS)
        long_name = "a" * 32
        text = nudge_text(long_name, [9999999, 99999999], 999999999)
        self.assertLessEqual(len(text), MAX_NUDGE_CHARS)
        self.assertNotIn("(seq", text, "the seq range is dropped before the cap is exceeded")
        self.assertTrue(text.startswith("[herdr-team nudge] 2 new board posts for " + long_name + ". Run: herdr-team board --new [n"))
        with self.assertRaises(NudgeTextError):
            nudge_text(long_name, [1] * 5, 10 ** 30)

    def test_only_validated_names_and_integers(self):
        for bad in ("Reviewer", "rev iewer", "human", "all", "me", "", "x\n", "-x", "a" * 33, 42):
            with self.subTest(bad):
                with self.assertRaises(NudgeTextError):
                    nudge_text(bad, [1], 1)
        for bad_seq in (["41"], [-1], [True], [1.5], []):
            with self.subTest(bad_seq):
                with self.assertRaises(NudgeTextError):
                    nudge_text("reviewer", bad_seq, 1)
        with self.assertRaises(NudgeTextError):
            nudge_text("reviewer", [1], "17")
        with self.assertRaises(NudgeTextError):
            nudge_text("reviewer", [1], -1)

    def test_nudge_is_an_echo(self):
        self.assertTrue(is_echo(nudge_text("reviewer", [1], 1)))

    def test_new_nonce(self):
        values = {new_nonce() for _ in range(50)}
        self.assertTrue(all(isinstance(v, int) and 0 < v < 1000000 for v in values))
        self.assertGreater(len(values), 1)


class BriefingTests(unittest.TestCase):
    def test_exact_line(self):
        # A 33-char headline keeps this exact plan-9.2 line under the 400-char cap.
        lines = briefing_lines("vuln-hunt-worker", "worker", "vuln-hunt", "Fix the BFF session-isolation bug", TEAMMATES, None, "herdr-team")
        self.assertEqual(len(lines), 1)
        self.assertEqual(
            lines[0],
            '[herdr-team briefing] You are "vuln-hunt-worker" (worker) in team "vuln-hunt": '
            "Fix the BFF session-isolation bug. Teammates: vuln-hunt-reviewer (reviewer) and human. "
            "This is context, not a task. Run herdr-team --skill once, then herdr-team charter, herdr-team instructions, "
            "herdr-team board --new, herdr-team ack, then continue. Teammates are peers: post to the board, never prompt their panes.",
        )
        self.assertEqual(len(lines[0]), 394)
        self.assertNotIn("\n", lines[0])

    def test_plan_example_headline_is_over_the_cap_and_shortened(self):
        # The plan's own example headline (61 chars) yields a 428-char line: the
        # 400 cap wins, the roster collapses first, then the headline is trimmed.
        headline = "Find and fix the session-isolation bug in the observability BFF"
        line = briefing_lines("vuln-hunt-worker", "worker", "vuln-hunt", headline, TEAMMATES, None, "herdr-team")[0]
        self.assertEqual(len(line), MAX_BRIEFING_CHARS)
        self.assertIn("Teammates: 1 teammate, run herdr-team who.", line)
        self.assertIn(": Find and fix the session-isolation bug in the o…. Teammates:", line)
        self.assertTrue(line.endswith("never prompt their panes."))

    def test_self_and_human_excluded_from_teammates(self):
        roster = [("vuln-hunt-worker", "worker"), ("human", "operator"), ("vuln-hunt-reviewer", "reviewer"), ("vuln-hunt-qa", "qa")]
        line = briefing_lines("vuln-hunt-worker", "worker", "vuln-hunt", "Goal", roster, None, "herdr-team")[0]
        self.assertIn("Teammates: vuln-hunt-reviewer (reviewer), vuln-hunt-qa (qa), and human.", line)
        line = briefing_lines("vuln-hunt-worker", "worker", "vuln-hunt", "Goal", [], None, "herdr-team")[0]
        self.assertIn("Teammates: only human so far.", line)

    def test_long_roster_collapses_to_count_form(self):
        roster = [("vuln-hunt-member{}".format(i), "role{}".format(i)) for i in range(12)]
        line = briefing_lines("vuln-hunt-lead", "lead", "vuln-hunt", "Find and fix the session-isolation bug", roster, None, "herdr-team")[0]
        self.assertLessEqual(len(line), MAX_BRIEFING_CHARS)
        self.assertIn("Teammates: 12 teammates, run herdr-team who.", line)
        self.assertNotIn("vuln-hunt-member1 ", line)
        self.assertIn("Find and fix the session-isolation bug.", line, "the headline survives when the roster collapse is enough")

    def test_headline_trimmed_when_collapse_is_not_enough(self):
        headline = "H" * 120
        roster = [("vuln-hunt-member{}".format(i), "role{}".format(i)) for i in range(30)]
        line = briefing_lines("vuln-hunt-reviewer-2", "reviewer", "vuln-hunt", headline, roster, None, "/Users/v/.local/bin/herdr-team")[0]
        self.assertLessEqual(len(line), MAX_BRIEFING_CHARS)
        self.assertIn("30 teammates, run herdr-team who", line)
        self.assertIn("…", line)
        self.assertNotIn("(CLI:", line, "the CLI hint goes before the headline is trimmed")
        self.assertTrue(line.endswith("never prompt their panes."))

    def test_cli_path_hint(self):
        line = briefing_lines("vuln-hunt-worker", "worker", "vuln-hunt", "Goal", TEAMMATES, None, "/opt/herdr-team/bin/herdr-team")[0]
        self.assertTrue(line.endswith("never prompt their panes. (CLI: /opt/herdr-team/bin/herdr-team)"))
        with self.assertRaises(NudgeTextError):
            briefing_lines("vuln-hunt-worker", "worker", "vuln-hunt", "Goal", TEAMMATES, None, "/path with space/herdr-team")

    def test_no_charter(self):
        line = briefing_lines("vuln-hunt-worker", "worker", "vuln-hunt", None, TEAMMATES, None, "herdr-team")[0]
        self.assertIn('in team "vuln-hunt": no charter yet, ask human. Teammates:', line)
        line = briefing_lines("vuln-hunt-worker", "worker", "vuln-hunt", "  \n ", TEAMMATES, None, "herdr-team")[0]
        self.assertIn("no charter yet, ask human", line)

    def test_headline_is_one_line_and_capped(self):
        line = briefing_lines("vuln-hunt-worker", "worker", "vuln-hunt", "Line one.\nLine two\ttabbed.", TEAMMATES, None, "herdr-team")[0]
        self.assertIn(": Line one. Line two tabbed. Teammates:", line)
        # 120 is the headline's own cap; the 400-char line cap then trims it further
        line = briefing_lines("w", "worker", "t", "x" * 300, [], None, "herdr-team")[0]
        self.assertEqual(len(line), MAX_BRIEFING_CHARS)
        self.assertIn(": " + "x" * 83 + "…. Teammates: only human so far.", line)
        self.assertNotIn("x" * 84, line)
        self.assertEqual(nudge._cut("x" * 300, nudge.MAX_CHARTER_HEADLINE_CHARS), "x" * 119 + "…")

    def test_brief_second_line(self):
        lines = briefing_lines("vuln-hunt-worker", "worker", "vuln-hunt", "Goal", TEAMMATES, "Own the patch.\nNever touch the reviewer's files.", "herdr-team")
        self.assertEqual(len(lines), 2)
        self.assertEqual(lines[1], "[herdr-team briefing] Your brief: Own the patch. Never touch the reviewer's files. Full text: herdr-team me")
        self.assertEqual(len(briefing_lines("vuln-hunt-worker", "worker", "vuln-hunt", "Goal", TEAMMATES, "   ", "herdr-team")), 1)

    def test_brief_cut_to_300(self):
        brief = "B" * 400
        lines = briefing_lines("vuln-hunt-worker", "worker", "vuln-hunt", "Goal", TEAMMATES, brief, "herdr-team")
        self.assertTrue(lines[1].startswith("[herdr-team briefing] Your brief: " + "B" * 299 + "…. Full text: herdr-team me"))
        body = lines[1][len("[herdr-team briefing] Your brief: "):-len(". Full text: herdr-team me")]
        self.assertLessEqual(len(body), 300)

    def test_validation(self):
        with self.assertRaises(NudgeTextError):
            briefing_lines("Bad Name", "worker", "vuln-hunt", "Goal", TEAMMATES, None, "herdr-team")
        with self.assertRaises(NudgeTextError):
            briefing_lines("vuln-hunt-worker", "Worker!", "vuln-hunt", "Goal", TEAMMATES, None, "herdr-team")
        with self.assertRaises(NudgeTextError):
            briefing_lines("vuln-hunt-worker", "worker", "Vuln Hunt", "Goal", TEAMMATES, None, "herdr-team")
        with self.assertRaises(NudgeTextError):
            briefing_lines("vuln-hunt-worker", "worker", "vuln-hunt", "Goal", [("Bad Peer", "x")], None, "herdr-team")

    def test_briefing_lines_are_echoes(self):
        for line in briefing_lines("vuln-hunt-worker", "worker", "vuln-hunt", "Goal", TEAMMATES, "brief", "herdr-team"):
            self.assertTrue(is_echo(line))


class ProbeAndEchoTests(unittest.TestCase):
    def test_probe(self):
        self.assertEqual(probe_text("a1B2"), "[herdr-team probe a1B2]")
        self.assertEqual(probe_line("x"), "[herdr-team probe x]")
        for bad in ("", "a b", "a]", "x" * 33, 7):
            with self.subTest(bad):
                with self.assertRaises(NudgeTextError):
                    probe_text(bad)

    def test_is_echo(self):
        self.assertTrue(is_echo("[herdr-team nudge] 1 new board post"))
        self.assertTrue(is_echo("  [herdr-team briefing] hi"))
        self.assertTrue(is_echo("[herdr-team probe abc]"))
        self.assertTrue(is_echo("done, see [n17] for the nudge"))
        self.assertTrue(is_echo("I got this: [herdr-team nudge] blah"))
        self.assertFalse(is_echo("herdr-team is great"))
        self.assertFalse(is_echo("[n] and [nope]"))
        self.assertFalse(is_echo(""))
        self.assertFalse(is_echo(None))
        self.assertFalse(is_echo("Diff ready, please review."))


if __name__ == "__main__":
    unittest.main()
