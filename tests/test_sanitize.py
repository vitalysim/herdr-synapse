"""Sanitizer, name grammar, marker, width, and secret tests (plan BD-06, 5.1, 5.3, 8.3)."""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import unittest

from herdr_team import sanitize
from herdr_team.errors import HerdrTeamError

ESC = "\x1b"
ZWJ = "\u200d"


class Bd06FixtureTests(unittest.TestCase):
    """BD-06: every hostile byte class is stripped, escaped, or made visible."""

    FIXTURES = [
        ("csi color", "a" + ESC + "[31mred" + ESC + "[0m", "ared"),
        ("csi private mode", ESC + "[?25lhidden" + ESC + "[?25h", "hidden"),
        ("csi c1 form", "a\x9b31mb", "ab"),
        ("osc bel", "x" + ESC + "]0;title\x07y", "xy"),
        ("osc st", "x" + ESC + "]8;;http://e.com" + ESC + "\\link" + ESC + "]8;;" + ESC + "\\", "xlink"),
        ("osc c1 form", "x\x9d0;t\x9cy", "xy"),
        ("dcs", "a" + ESC + "Pq#0;2;0;0;0#0~~" + ESC + "\\b", "ab"),
        ("apc", "a" + ESC + "_payload" + ESC + "\\b", "ab"),
        ("pm", "a" + ESC + "^pm" + ESC + "\\b", "ab"),
        ("sos", "a" + ESC + "Xsos" + ESC + "\\b", "ab"),
        ("unterminated osc stops at newline", "a" + ESC + "]swallow\nkept", "a\nkept"),
        ("esc x", "a" + ESC + "cb", "ab"),
        ("esc intermediate final", "a" + ESC + "(Bb", "ab"),
        ("lone esc at end", "ab" + ESC, "ab"),
        ("lone esc before newline", "a" + ESC + "\nb", "a\nb"),
        ("nul", "a\x00b", "ab"),
        ("bell backspace", "a\x07\x08b", "ab"),
        ("vertical tab", "a\x0bb", "ab"),
        ("form feed", "a\x0cb", "ab"),
        ("del", "a\x7fb", "ab"),
        ("nel c1", "a\x85b", "ab"),
        ("other c1", "a\x80\x8f\x9ab", "ab"),
        ("c1 apc introducer swallows its line", "a\x9fpayload\nb", "a\nb"),
        ("tab kept", "a\tb", "a\tb"),
        ("newline kept", "a\nb", "a\nb"),
        ("crlf", "a\r\nb", "a\nb"),
        ("lone cr", "a\rb", "a\nb"),
        ("bidi override", "a\u202eb", "ab"),
        ("bidi isolate", "a\u2066b\u2069", "ab"),
        ("zero width space", "a\u200bb", "ab"),
        ("zero width joiner outside emoji", "a" + ZWJ + "b", "ab"),
        ("bom", "\ufeffa", "a"),
        ("soft hyphen", "a\u00adb", "ab"),
        ("line separator", "a\u2028b", "a\\u2028b"),
        ("paragraph separator", "a\u2029b", "a\\u2029b"),
        ("trailing whitespace", "a  \nb\t\n\n", "a\nb"),
        ("combining run capped", "e" + "\u0301" * 7 + "x", "e" + "\u0301" * 3 + "x"),
        ("combining run of three kept", "e" + "\u0301\u0302\u0303" + "x", "e\u0301\u0302\u0303x"),
        ("emoji zwj family", "\U0001f468" + ZWJ + "\U0001f469" + ZWJ + "\U0001f467", "\U0001f468" + ZWJ + "\U0001f469" + ZWJ + "\U0001f467"),
        ("emoji zwj with variation selector", "\U0001f3f3\ufe0f" + ZWJ + "\U0001f308", "\U0001f3f3\ufe0f" + ZWJ + "\U0001f308"),
        ("emoji zwj with skin tone", "\U0001f468\U0001f3fd" + ZWJ + "\U0001f4bb", "\U0001f468\U0001f3fd" + ZWJ + "\U0001f4bb"),
        ("zwj before non emoji", "\U0001f468" + ZWJ + "x", "\U0001f468x"),
        ("cjk untouched", "漢字 テスト", "漢字 テスト"),
        ("plain", "Diff ready, please review.", "Diff ready, please review."),
    ]

    def test_fixtures(self):
        for name, raw, expected in self.FIXTURES:
            with self.subTest(name):
                self.assertEqual(sanitize.sanitize_text(raw), expected)

    def test_mixed_hostile_text(self):
        raw = "ok" + ESC + "[2J\x00\u202e" + ESC + "]0;x\x07\u200b\x85\x0b\u2028done  "
        self.assertEqual(sanitize.sanitize_text(raw), "ok\\u2028done")

    def test_strip_controls_keeps_format_chars(self):
        # strip_controls alone leaves Cf handling to sanitize_text / headline.
        self.assertEqual(sanitize.strip_controls("a\u200bb\x00c"), "a\u200bbc")

    def test_result_has_no_controls_or_format_chars(self):
        raw = "".join(chr(c) for c in range(0, 0x100)) + "\u200b\u202e\u2028\u2029\ufeff\U000e0041"
        out = sanitize.sanitize_text(raw)
        self.assertNotRegex(out, r"[\x00-\x08\x0b-\x1f\x7f-\x9f\u2028\u2029]")
        import unicodedata
        for ch in out:
            self.assertNotEqual(unicodedata.category(ch), "Cf", repr(ch))


class Utf8AndLengthTests(unittest.TestCase):
    def test_bytes_decoded_strictly(self):
        self.assertEqual(sanitize.validate_utf8("héllo".encode("utf-8")), "héllo")
        with self.assertRaises(HerdrTeamError) as ctx:
            sanitize.validate_utf8(b"ok \xff\xfe")
        self.assertEqual(ctx.exception.code, "invalid_utf8")
        self.assertEqual(ctx.exception.exit_code, 1)
        self.assertEqual(ctx.exception.details["position"], 3)

    def test_encoded_surrogate_refused(self):
        with self.assertRaises(HerdrTeamError) as ctx:
            sanitize.validate_utf8(b"\xed\xa0\x80")
        self.assertEqual(ctx.exception.code, "invalid_utf8")

    def test_str_with_lone_surrogate_refused(self):
        with self.assertRaises(HerdrTeamError) as ctx:
            sanitize.sanitize_text("bad \udcff byte")
        self.assertEqual(ctx.exception.code, "invalid_utf8")

    def test_bytes_accepted_by_sanitize_text(self):
        self.assertEqual(sanitize.sanitize_text("a\x1b[1mb".encode("utf-8")), "ab")

    def test_length_checked_after_sanitization(self):
        padded = "x" * 2001 + ESC + "[0m" + "\u200b"
        # 2001 x; the escape and the zero-width space are stripped (plan 6.1): one over the cap.
        with self.assertRaises(HerdrTeamError) as ctx:
            sanitize.sanitize_text(padded)
        self.assertEqual(ctx.exception.code, "text_too_long")
        self.assertEqual(ctx.exception.exit_code, 1)
        self.assertEqual(ctx.exception.details["length"], 2001)
        self.assertEqual(ctx.exception.details["max"], 2000)
        self.assertIn("--spill", ctx.exception.details["hint"])
        self.assertEqual(len(sanitize.sanitize_text("x" * 2000 + ESC + "[0m")), 2000)

    def test_custom_max_len(self):
        self.assertEqual(sanitize.sanitize_text("abc", max_len=3), "abc")
        with self.assertRaises(HerdrTeamError):
            sanitize.sanitize_text("abcd", max_len=3)


class LabelTests(unittest.TestCase):
    def test_none_passes_through(self):
        self.assertIsNone(sanitize.sanitize_label(None))

    def test_valid_labels(self):
        for label in ("v", "vitaly", "v.simonovich_1-2", "A" * 32):
            self.assertEqual(sanitize.sanitize_label(label), label)

    def test_invalid_labels(self):
        for label in ("", "a" * 65, "with space", "sémi", "x\x1b[31m", "a/b", "@me"):
            with self.subTest(label):
                with self.assertRaises(HerdrTeamError) as ctx:
                    sanitize.sanitize_label(label)
                self.assertEqual(ctx.exception.code, "label_invalid")
                self.assertEqual(ctx.exception.exit_code, 1)


class NameGrammarTests(unittest.TestCase):
    def test_member_names_valid(self):
        for name in ("a", "alpha-reviewer", "vuln-hunt-reviewer-2", "x_1", "a" + "b" * 31):
            self.assertEqual(sanitize.sanitize_name(name), name)

    def test_member_names_invalid_grammar(self):
        for name in ("", "Alpha", "1abc", "-x", "a b", "a" * 33, "ünï", "a.b", None, 42):
            with self.subTest(repr(name)):
                with self.assertRaises(HerdrTeamError) as ctx:
                    sanitize.sanitize_name(name)
                self.assertEqual(ctx.exception.code, "name_invalid")
                self.assertEqual(ctx.exception.exit_code, 1)

    def test_member_names_reserved(self):
        for name in ("human", "all", "me", "none", "system", "team", "claude", "codex", "agy", "qodercli",
                     "cursor-agent", "claude-code", "github-copilot", "muse", "kiro-cli", "antigravity"):
            with self.subTest(name):
                with self.assertRaises(HerdrTeamError) as ctx:
                    sanitize.sanitize_name(name)
                self.assertEqual(ctx.exception.code, "name_reserved")
                self.assertEqual(ctx.exception.details["name"], name)

    def test_team_grammar(self):
        self.assertEqual(sanitize.sanitize_team_name("vuln-hunt"), "vuln-hunt")
        self.assertEqual(sanitize.sanitize_name("a" * 32, "team"), "a" * 32)
        for name in ("a" * 33, "Vuln", "", "1x"):
            with self.assertRaises(HerdrTeamError) as ctx:
                sanitize.sanitize_team_name(name)
            self.assertEqual(ctx.exception.code, "team_name_invalid")
        with self.assertRaises(HerdrTeamError) as ctx:
            sanitize.sanitize_team_name("claude")
        self.assertEqual(ctx.exception.code, "team_name_invalid")
        self.assertEqual(ctx.exception.details["what"], "team")

    def test_role_grammar(self):
        self.assertEqual(sanitize.sanitize_role("reviewer"), "reviewer")
        self.assertEqual(sanitize.sanitize_role("a" * 64), "a" * 64)
        for name in ("a" * 65, "Reviewer", "codex", "human", "qoder"):
            with self.subTest(name):
                with self.assertRaises(HerdrTeamError) as ctx:
                    sanitize.sanitize_role(name)
                self.assertEqual(ctx.exception.code, "role_invalid")

    def test_unknown_class_is_a_programming_error(self):
        with self.assertRaises(ValueError):
            sanitize.sanitize_name("x", "pane")

    def test_error_message_is_control_free(self):
        with self.assertRaises(HerdrTeamError) as ctx:
            sanitize.sanitize_name("bad\x1b[31mname\n")
        self.assertNotIn("\x1b", ctx.exception.message)
        self.assertNotIn("\n", ctx.exception.message)
        self.assertNotIn("\x1b", ctx.exception.details["name"])

    def test_derived_name_fits_cap(self):
        team = "a" * 15
        role = "b" * 14
        derived = "{}-{}".format(team, role)
        self.assertEqual(len(derived), 30)
        self.assertEqual(sanitize.sanitize_name(derived + "-2"), derived + "-2")

    def test_reserved_sets_are_consistent(self):
        self.assertEqual(len(sanitize.KIND_LABELS), 24)
        self.assertTrue(sanitize.KIND_LABELS.isdisjoint(sanitize.RESERVED_NAMES))
        self.assertTrue(sanitize.KIND_LABELS.isdisjoint(sanitize.KIND_ALIASES))
        self.assertEqual(sanitize.RESERVED_WORDS, sanitize.RESERVED_NAMES | sanitize.KIND_LABELS | sanitize.KIND_ALIASES)
        for label in sanitize.KIND_LABELS:
            self.assertRegex(label, sanitize.NAME_RE)


class KindLabelsMatchBinaryTests(unittest.TestCase):
    """The constant must equal what the installed binary advertises."""

    def test_kind_labels_match_agent_start_help(self):
        binary = os.environ.get("HERDR_BIN_PATH") or shutil.which("herdr")
        if not binary:
            self.skipTest("no herdr binary on PATH")
        try:
            proc = subprocess.run(
                [binary, "agent", "start", "--help"],
                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            self.skipTest("herdr --help unavailable: {}".format(exc))
        text = proc.stdout.decode("utf-8", "replace")
        match = re.search(r"\[possible values: ([^\]]+)\]", text)
        if proc.returncode != 0 or not match:
            self.skipTest("could not parse kind labels from `herdr agent start --help`")
        advertised = frozenset(item.strip() for item in match.group(1).split(","))
        self.assertEqual(advertised, sanitize.KIND_LABELS)


class MarkerTests(unittest.TestCase):
    def test_marker_prefix(self):
        self.assertTrue(sanitize.is_marker_text("[herdr-team nudge] 2 new board posts"))
        self.assertTrue(sanitize.is_marker_text("  \n[herdr-team briefing] You are"))
        self.assertTrue(sanitize.is_marker_text("[herdr-team probe 123]"))
        self.assertTrue(sanitize.is_marker_text("[herdr-team"))

    def test_nonce_anywhere(self):
        self.assertTrue(sanitize.is_marker_text("Run: herdr-synapse board --new [n17]"))
        self.assertTrue(sanitize.is_marker_text("x [n0] y"))
        self.assertFalse(sanitize.is_marker_text("x [n] y"))
        self.assertFalse(sanitize.is_marker_text("x [nx1] y"))
        self.assertFalse(sanitize.is_marker_text("array[n1"))

    def test_plain_text_is_not_a_marker(self):
        self.assertFalse(sanitize.is_marker_text("I saw a [herdr-team nudge] line and read the board"))
        self.assertFalse(sanitize.is_marker_text("Diff ready, please review."))
        self.assertFalse(sanitize.is_marker_text(""))

    def test_echo_rejected_alias(self):
        self.assertIs(sanitize.echo_rejected("[herdr-team x"), True)
        self.assertIs(sanitize.echo_rejected("fine"), False)


class DisplayWidthTests(unittest.TestCase):
    def test_ascii(self):
        self.assertEqual(sanitize.display_width(""), 0)
        self.assertEqual(sanitize.display_width("hello"), 5)

    def test_cjk_is_double_width(self):
        self.assertEqual(sanitize.display_width("漢字"), 4)
        self.assertEqual(sanitize.display_width("テスト"), 6)
        self.assertEqual(sanitize.display_width("ａｂ"), 4)  # fullwidth

    def test_emoji_is_double_width(self):
        self.assertEqual(sanitize.display_width("\U0001f600"), 2)
        self.assertEqual(sanitize.display_width("\U0001f468" + ZWJ + "\U0001f4bb"), 4)
        self.assertEqual(sanitize.display_width("\U0001f3f3\ufe0f"), 2)

    def test_vs16_widens_a_narrow_base(self):
        self.assertEqual(sanitize.display_width("\u2764"), 1)
        self.assertEqual(sanitize.display_width("\u2764\ufe0f"), 2)
        self.assertEqual(sanitize.display_width("\U0001f600\ufe0f"), 2)
        self.assertEqual(sanitize.truncate_columns("\u2764\ufe0fab", 3), "\u2764\ufe0f…")
        self.assertEqual(sanitize.truncate_columns("\u2764\ufe0fa", 3), "\u2764\ufe0fa")
        self.assertEqual(sanitize.truncate_columns("a\u2764\ufe0fb", 3), "a…")
        self.assertEqual(sanitize.truncate_columns("ab\u2764\ufe0fcd", 3), "ab…")

    def test_combining_and_format_are_zero(self):
        self.assertEqual(sanitize.display_width("e\u0301"), 1)
        self.assertEqual(sanitize.display_width("a\u200bb"), 2)
        self.assertEqual(sanitize.display_width("\x1b\x00\t"), 0)

    def test_glyphs_are_single_width(self):
        for glyph in ("→", "✓", "!", "?", "◐", "○", "×", "↪", "·", "…"):
            self.assertEqual(sanitize.display_width(glyph), 1, glyph)


class TruncateTests(unittest.TestCase):
    def test_no_cut_when_fits(self):
        self.assertEqual(sanitize.truncate_columns("abc", 3), "abc")
        self.assertEqual(sanitize.truncate_columns("漢字", 4), "漢字")

    def test_cut_ascii(self):
        self.assertEqual(sanitize.truncate_columns("abcdefgh", 5), "abcd…")
        self.assertEqual(sanitize.display_width(sanitize.truncate_columns("abcdefgh", 5)), 5)

    def test_cut_never_splits_a_wide_char(self):
        out = sanitize.truncate_columns("漢字漢字漢字", 7)
        self.assertEqual(out, "漢字漢…")
        self.assertLessEqual(sanitize.display_width(out), 7)
        out = sanitize.truncate_columns("漢字漢字漢字", 6)
        self.assertEqual(out, "漢字…")

    def test_cut_keeps_combining_marks_with_base(self):
        out = sanitize.truncate_columns("e\u0301e\u0301e\u0301e\u0301", 3)
        self.assertEqual(out, "e\u0301e\u0301…")

    def test_cut_emoji_sequence(self):
        seq = "\U0001f468" + ZWJ + "\U0001f469" + ZWJ + "\U0001f467"
        out = sanitize.truncate_columns(seq + "tail", 3)
        self.assertEqual(out, "\U0001f468…")
        self.assertNotIn(ZWJ + "…", out)

    def test_ascii_ellipsis(self):
        self.assertEqual(sanitize.truncate_columns("abcdefgh", 6, "..."), "abc...")

    def test_degenerate_columns(self):
        self.assertEqual(sanitize.truncate_columns("abc", 0), "")
        self.assertEqual(sanitize.truncate_columns("abc", -1), "")
        self.assertEqual(sanitize.truncate_columns("abcdef", 1), "…")
        self.assertEqual(sanitize.truncate_columns("abcdef", 2, "..."), "ab")

    def test_trim_columns_alias(self):
        self.assertEqual(sanitize.trim_columns("abcdefgh", 5), sanitize.truncate_columns("abcdefgh", 5))

    def test_every_width_up_to_limit(self):
        text = "a漢b字c\U0001f600d"
        for columns in range(0, 12):
            out = sanitize.truncate_columns(text, columns)
            self.assertLessEqual(sanitize.display_width(out), columns, (columns, out))


class HeadlineTests(unittest.TestCase):
    def test_first_non_blank_line(self):
        self.assertEqual(sanitize.headline("\n\n  first  line \nsecond"), "first line")

    def test_controls_and_format_stripped_not_replaced(self):
        self.assertEqual(sanitize.headline("re\x1b[1mview\u200b \u202edone\x00"), "review done")

    def test_tabs_collapse(self):
        self.assertEqual(sanitize.headline("a\t\tb   c"), "a b c")

    def test_default_24_columns(self):
        self.assertEqual(sanitize.headline("x" * 40), "x" * 23 + "…")
        # 11 wide characters (22 columns) plus the ellipsis: 24 would split a glyph.
        self.assertEqual(sanitize.display_width(sanitize.headline("漢" * 20)), 23)
        self.assertEqual(sanitize.headline("漢" * 20), "漢" * 11 + "…")

    def test_empty_and_none(self):
        self.assertEqual(sanitize.headline(""), "")
        self.assertEqual(sanitize.headline(None), "")
        self.assertEqual(sanitize.headline("\n \t\n"), "")

    def test_token_char_cap(self):
        # Many zero-width combining marks keep the column count low but would
        # overflow the 80-char token value.
        text = ("e" + "\u0301" * 3) * 24
        out = sanitize.headline(text)
        self.assertLessEqual(len(out), sanitize.MAX_TOKEN_CHARS)
        self.assertLessEqual(sanitize.display_width(out), 24)


class SecretPatternTests(unittest.TestCase):
    def test_finds_each_shape(self):
        text = "aws AKIAIOSFODNN7EXAMPLE key sk-ant-api03-abcdefghijklmnop gh ghp_abcdefghijklmnopqrstuvwxyz0123456789 pem -----BEGIN RSA PRIVATE KEY-----"
        names = [m["pattern"] for m in sanitize.secret_patterns(text)]
        self.assertEqual(names, ["aws_access_key", "sk_api_key", "github_token", "pem_block"])

    def test_excerpt_is_masked(self):
        matches = sanitize.secret_patterns("AKIAIOSFODNN7EXAMPLE")
        self.assertEqual(matches[0]["excerpt"], "AKIAIO…")
        self.assertEqual((matches[0]["start"], matches[0]["end"]), (0, 20))

    def test_ordinary_text_is_clean(self):
        for text in ("ask-me later", "the task is ready", "AKIA is short", "ghp_short", "BEGIN here"):
            self.assertEqual(sanitize.secret_patterns(text), [], text)


if __name__ == "__main__":
    unittest.main()
