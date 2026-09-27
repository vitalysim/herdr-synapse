"""The canvas text engine (canvas v2 foundation): measuring with the bundled fonts' metrics, the estimates
for what they lack, the line breaking rules, the baseline, and the fallback when the metrics are missing."""
from __future__ import annotations

import json
import logging
import tempfile
import unittest
from pathlib import Path

from support import PLUGIN_ROOT

from herdr_team import canvas_text as X

CORPUS = PLUGIN_ROOT / "tests" / "fixtures" / "canvas-text-corpus.json"


def advances(text, key="sans-500"):
    """The sum of ``text``'s advances in em, read straight from the metrics file."""
    doc = json.loads(X.METRICS_PATH.read_text(encoding="utf-8"))
    face = doc["faces"][key]
    table = {}
    for first, widths in face["advances"]:
        for offset, width in enumerate(widths):
            table[first + offset] = width
    return sum(table[ord(ch)] for ch in text) / float(face["units_per_em"])


class Measure(unittest.TestCase):
    def test_a_line_is_its_advances_at_the_size_times_safety(self):
        for text, weight, key in (("Checkout service handles payments", 500, "sans-500"), ("WHY NOT NOW", 400, "sans-400"),
                                  ("Bold headline", 700, "sans-700")):
            with self.subTest(text=text, weight=weight):
                found = X.measure(text, "normal", 20, weight)
                self.assertAlmostEqual(found.width, advances(text, key) * 20 * X.SAFETY, places=6)
                self.assertFalse(found.estimated)

    def test_the_default_weight_is_the_one_the_page_draws(self):
        self.assertEqual(X.DEFAULT_WEIGHT, 500)
        self.assertEqual(X.measure("Label").width, X.measure("Label", "normal", 20, 500).width)
        self.assertGreater(X.measure("Label", weight=500).width, X.measure("Label", weight=400).width, "Medium is the wider face")
        self.assertEqual(X.face("normal", 450).weight, 500, "the nearest weight, the heavier on a tie")

    def test_width_scales_with_the_size(self):
        self.assertAlmostEqual(X.measure("scale me", size=40).width, 2 * X.measure("scale me", size=20).width, places=6)

    def test_code_is_as_wide_as_the_wider_monospace(self):
        found = X.measure("print(hello)", "code", 20)
        self.assertAlmostEqual(found.width, 12 * X.MONO_ADVANCE_EM * 20 * X.SAFETY, places=6)
        self.assertGreaterEqual(X.MONO_ADVANCE_EM, 1200 / 2048.0, "Cascadia Code, the Phase 0 page's code font")
        self.assertEqual(X.face("mono").family, "Geist Mono")

    def test_hand_is_inter_scaled_and_always_an_estimate(self):
        found = X.measure("sketchy", "hand", 20)
        self.assertAlmostEqual(found.width, X.measure("sketchy").width * X.HAND_SCALE, places=6)
        self.assertTrue(found.estimated)

    def test_what_the_font_lacks_is_estimated_by_its_kind(self):
        em = 20 * X.SAFETY
        self.assertAlmostEqual(X.measure("中文", size=20).width, 2 * X.WIDE_EM * em, places=6)
        self.assertAlmostEqual(X.measure("\U0001F680", size=20).width, X.EMOJI_EM * em, places=6)
        self.assertTrue(X.measure("中文").estimated and X.measure("\U0001F680").estimated)
        self.assertAlmostEqual(X.measure("a‍b").width, X.measure("ab").width, places=6, msg="a joiner takes no room")
        self.assertAlmostEqual(X.measure("é").width, X.measure("e").width, places=6, msg="a combining mark takes no room")
        self.assertAlmostEqual(X.measure("a\tb").width, X.measure("a    b").width, places=6, msg="a tab is four spaces")
        self.assertFalse(X.measure("café").estimated, "Inter covers accented Latin")

    def test_the_corpus_widths_are_current(self):
        # The page comparison (item I7) reads this file; a metrics change must regenerate it.
        doc = json.loads(CORPUS.read_text(encoding="utf-8"))
        self.assertGreaterEqual(len(doc["strings"]), 150)
        self.assertEqual(doc["safety"], X.SAFETY)
        for row in doc["strings"]:
            with self.subTest(text=row["text"]):
                self.assertAlmostEqual(row["w400"], X.measure(row["text"], "normal", 20, 400).width, places=2)
                self.assertAlmostEqual(row["w500"], X.measure(row["text"], "normal", 20, 500).width, places=2)
        texts = [row["text"] for row in doc["strings"]]
        for needle in ("->", "<-", "=>", "...", "中文标签没有空格", "https://example.com/some/long/path/to/a/resource.html"):
            self.assertTrue(any(needle in text for text in texts), needle)


class Wrap(unittest.TestCase):
    def test_breaks_at_spaces_and_never_counts_the_break_space(self):
        width = X.measure("alpha beta").width
        self.assertEqual(X.wrap("alpha beta gamma", width), ["alpha beta", "gamma"])
        self.assertEqual(X.wrap("alpha beta gamma", width - 0.01), ["alpha", "beta", "gamma"])

    def test_a_newline_always_breaks_and_indentation_is_kept(self):
        self.assertEqual(X.wrap("one\n\n  two", 500), ["one", "", "  two"])

    def test_a_long_token_breaks_after_a_separator_rightmost_first(self):
        url = "https://example.com/a/very/long/path/to/some/resource.html"
        lines = X.wrap(url, 200)
        self.assertEqual("".join(lines), url)
        self.assertTrue(all(X.measure(line).width <= 200 for line in lines))
        self.assertTrue(all(line[-1] in X.SOFT_BREAKS for line in lines[:-1]), lines)
        self.assertEqual(X.wrap("snake_case_name", X.measure("snake_case_").width), ["snake_case_", "name"])

    def test_east_asian_text_breaks_between_characters(self):
        lines = X.wrap("中文标签没有空格", X.measure("中文标").width)
        self.assertEqual(lines, ["中文标", "签没有", "空格"])

    def test_a_word_breaks_mid_word_only_when_nothing_else_fits(self):
        lines = X.wrap("Internationalization", X.measure("Internation").width)
        self.assertEqual(lines, ["Internation", "alization"])
        self.assertEqual(X.segments("Internationalization"), ["Internationalization"])
        self.assertEqual(X.segments("a/b.c"), ["a/", "b.", "c"])

    def test_a_joined_emoji_is_never_split(self):
        family = "\U0001F469‍\U0001F4BB"
        for line in X.wrap(family * 3, X.measure(family).width):
            self.assertFalse(line.startswith("‍") or line.endswith("‍"), line)

    def test_block_reports_lines_width_and_height(self):
        found = X.block("alpha beta gamma", X.measure("alpha beta").width, size=20)
        self.assertEqual(found.lines, ("alpha beta", "gamma"))
        self.assertAlmostEqual(found.width, X.measure("alpha beta").width, places=6)
        self.assertEqual(found.height, 2 * 1.25 * 20)
        self.assertFalse(found.estimated)


class Baseline(unittest.TestCase):
    def test_the_first_baseline_follows_the_half_leading_model(self):
        face = X.face("normal")
        ascender, descender = face.ascender / face.units_per_em, face.descender / face.units_per_em
        self.assertAlmostEqual(X.baseline(20), (1.25 - (ascender - descender)) * 20 / 2 + ascender * 20, places=6)
        self.assertAlmostEqual(X.baseline(40), 2 * X.baseline(20), places=6)
        self.assertTrue(10 < X.baseline(20) < 25)


class MissingMetrics(unittest.TestCase):
    def tearDown(self):
        X._CACHE.pop(str(self.missing), None)

    def test_a_missing_file_falls_back_to_an_estimate_with_one_warning(self):
        self.missing = Path(tempfile.gettempdir()) / "no-such-font-metrics.json"
        with self.assertLogs("herdr_team.canvas_text", level=logging.WARNING) as logs:
            metrics = X.load(self.missing)
            X.load(self.missing)
        self.assertEqual(len(logs.output), 1, "cached: one warning, not one per call")
        self.assertTrue(metrics.estimated)
        saved = X.METRICS
        X.METRICS = metrics
        try:
            found = X.measure("abc", size=10)
            self.assertAlmostEqual(found.width, 3 * X.FALLBACK_EM * 10 * X.SAFETY, places=6)
            self.assertTrue(found.estimated)
            result = X.fit("hug", X.FitRequest("a label that must still fit", min_w=100, min_h=40, pad_x=8, pad_y=4))
            self.assertTrue(X.fits(result) and result.estimated)
        finally:
            X.METRICS = saved
        self.assertFalse(X.measure("abc").estimated, "the real metrics are back")

    def test_a_corrupt_file_falls_back_too(self):
        with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as fh:
            fh.write("{not json")
        self.missing = Path(fh.name)
        self.addCleanup(self.missing.unlink)
        with self.assertLogs("herdr_team.canvas_text", level=logging.WARNING):
            self.assertTrue(X.load(self.missing).estimated)


if __name__ == "__main__":
    unittest.main()
