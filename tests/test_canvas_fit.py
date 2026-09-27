"""Fit policies (canvas v2 foundation): the invariant over 10,000 seeded random labels, and what each policy does.

The invariant (``canvas_text.fits``): every line of a fitted label is no wider than the inner box it is drawn
in, and its lines are no taller than that box. It must hold for every policy, any text, any size and any font.
"""
from __future__ import annotations

import math
import random
import unittest

from herdr_team import canvas_text as X
from herdr_team.canvas_kinds import shape as shape_kind

WORDS = ("alpha", "Checkout", "service", "API", "p99", "latency", "a", "of", "Internationalization", "WWWW", "iiii", "résumé",
         "->", "=>", "...", "(draft)", "Q3:", "$1.2M", "gateway", "backpressure", "idempotency", "SELECT", "count(*)")
TOKENS = ("https://example.com/some/long/path/to/a/resource.html", "/opt/homebrew/bin/resvg", "snake_case_identifier_name",
          "kebab-case-identifier-name", "中文标签没有空格也要换行", "日本語のラベル", "한국어 라벨", "\U0001F680", "\U0001F469‍\U0001F4BB",
          "\U0001F1EE\U0001F1F1", "é", "Supercalifragilisticexpialidocious", "x" * 60)
SIZES = (16, 20, 28, 36)
FONTS = ("normal", "normal", "normal", "code", "hand")
POLICIES = ("hug", "shrink", "scale_shape", "clamp", "keep")
INSETS = (None, shape_kind.ellipse_inset, shape_kind.diamond_inset)
SEED = 20260927
LABELS = 10000


def random_label(rng: random.Random) -> str:
    roll = rng.random()
    if roll < 0.03:
        return ""
    if roll < 0.05:
        return " " * rng.randint(1, 8)
    if roll < 0.07:
        return "\n" * rng.randint(1, 3)
    if roll < 0.075:
        return " ".join(rng.choice(WORDS) for _ in range(400))[:2000]
    parts = []
    for _ in range(rng.randint(1, 14)):
        pick = rng.random()
        parts.append(rng.choice(TOKENS) if pick < 0.15 else rng.choice(WORDS))
    joiners = [" "] * 8 + ["\n", "  ", "\t"]
    return "".join(part + rng.choice(joiners) for part in parts).rstrip(" ")


def random_request(rng: random.Random, text: str) -> X.FitRequest:
    min_w = float(rng.choice((1, 20, 40, 80, 160, 200, 320)))
    min_h = float(rng.choice((1, 20, 40, 60, 80, 120)))
    return X.FitRequest(text=text, font=rng.choice(FONTS), size=float(rng.choice(SIZES)), weight=rng.choice((400, 500, 600, 700)),
                        min_w=min_w, min_h=min_h, max_w=float(rng.choice((120, 240, 320, 600))), pad_x=float(rng.choice((0, 4, 12, 16))),
                        pad_y=float(rng.choice((0, 4, 8, 12))), inset=rng.choice(INSETS), max_lines=rng.randint(1, 4),
                        snap=float(rng.choice((0, 20))))


class Invariant(unittest.TestCase):
    def test_every_policy_fits_ten_thousand_random_labels(self):
        rng = random.Random(SEED)
        counts = {policy: 0 for policy in POLICIES}
        for index in range(LABELS):
            text = random_label(rng)
            request = random_request(rng, text)
            policy = POLICIES[index % len(POLICIES)]
            result = X.fit(policy, request)
            counts[policy] += 1
            context = (index, policy, text[:60], request.size, request.font, request.min_w, request.min_h)
            self.assertTrue(X.fits(result), context)
            self.assertGreaterEqual(result.w, request.min_w, context)
            self.assertLessEqual(result.w, X.MAX_BOX, context)
            if policy != "clamp":
                self.assertGreaterEqual(result.h, request.min_h, context)
            if policy != "clamp" and not result.truncated:
                self.assertEqual("".join("".join(result.lines).split()), "".join(text.split()), context)  # no character lost
        self.assertTrue(all(count >= LABELS // len(POLICIES) for count in counts.values()), counts)

    def test_fitting_is_deterministic(self):
        rng = random.Random(SEED + 1)
        for _ in range(200):
            request = random_request(rng, random_label(rng))
            for policy in POLICIES:
                self.assertEqual(X.fit(policy, request), X.fit(policy, request))


def box(text, **kw):
    kw.setdefault("min_w", 160)
    kw.setdefault("min_h", 80)
    kw.setdefault("max_w", 320)
    kw.setdefault("pad_x", 16)
    kw.setdefault("pad_y", 12)
    kw.setdefault("snap", 20)
    return X.FitRequest(text, **kw)


class Hug(unittest.TestCase):
    def test_a_short_label_keeps_the_minimum_exactly(self):
        result = X.fit("hug", box("API"))
        self.assertEqual((result.w, result.h, result.lines, result.grew), (160, 80, ("API",), False))
        bigger = X.fit("hug", box("API", min_w=333, min_h=97))
        self.assertEqual((bigger.w, bigger.h), (333, 97), "an explicit size larger than the text needs is kept exactly")

    def test_a_long_label_grows_on_the_grid_and_wraps_at_the_maximum(self):
        result = X.fit("hug", box("Internationalization and localization pipeline"))
        self.assertTrue(result.grew)
        self.assertEqual((result.w % 20, result.h % 20), (0, 0))
        self.assertLessEqual(result.w, 320)
        self.assertEqual(len(result.lines), 2)

    def test_two_lines_are_balanced(self):
        # Greedy at 288 leaves one word alone on the second line; the balanced split is narrower.
        text = "Payment reconciliation service for card"
        greedy = X.wrap(text, 320 - 32)
        result = X.fit("hug", box(text))
        self.assertEqual(len(result.lines), 2)
        self.assertLess(max(X.measure(line).width for line in result.lines), max(X.measure(line).width for line in greedy) + 1e-6)
        self.assertGreater(len(result.lines[1].split()), 1, result.lines)

    def test_a_word_wider_than_the_maximum_widens_the_box_rather_than_split(self):
        word = "Supercalifragilisticexpialidocious"
        result = X.fit("hug", box(word, max_w=120, size=28))
        self.assertEqual(result.lines, (word,))
        self.assertGreaterEqual(result.w, X.measure(word, size=28).width + 32)

    def test_a_path_breaks_at_its_separators_instead(self):
        path = "/usr/local/share/herdr-synapse/assets/fonts/inter/Inter-Medium.ttf"
        result = X.fit("hug", box(path))
        self.assertGreater(len(result.lines), 1)
        self.assertLessEqual(result.w, 320)
        self.assertEqual("".join(result.lines), path)


class Shrink(unittest.TestCase):
    def note(self, text, **kw):
        return X.fit("shrink", box(text, min_w=180, min_h=120, max_w=280, pad_x=16, pad_y=16, **kw))

    def test_a_label_that_fits_keeps_its_size(self):
        result = self.note("Price rise in March")
        self.assertEqual((result.w, result.h, result.size, result.shrunk), (180, 120, 20.0, False))

    def test_a_longer_label_steps_the_size_down_by_two(self):
        result = self.note("Ask the designer whether the roof should be flat or pitched")
        self.assertTrue(result.shrunk)
        self.assertLess(result.size, 20)
        self.assertEqual((20 - result.size) % 2, 0)
        self.assertEqual((result.w, result.h), (180, 120), "the paper keeps its size")

    def test_past_the_floor_it_hugs_at_the_floor(self):
        result = self.note("word " * 80)
        self.assertEqual(result.size, 14.0)
        self.assertGreater(result.h, 120)
        self.assertTrue(X.fits(result))


class ScaleShape(unittest.TestCase):
    def test_a_diamond_grows_as_a_whole_until_its_inset_holds_the_label(self):
        result = X.fit("scale_shape", X.FitRequest("Payment approved?", min_w=200, min_h=120, pad_x=4, pad_y=4,
                                                   inset=shape_kind.diamond_inset, snap=20))
        self.assertTrue(result.grew)
        self.assertAlmostEqual(result.w / result.h, 200 / 120.0, delta=0.2)
        ix, iy, iw, ih = result.inner
        self.assertAlmostEqual(ix, result.w / 4 + 4, places=6)
        self.assertAlmostEqual(iw, result.w / 2 - 8, places=6)

    def test_a_long_label_jumps_to_the_area_estimate(self):
        text = "word " * 150
        result = X.fit("scale_shape", X.FitRequest(text, min_w=160, min_h=80, inset=shape_kind.ellipse_inset, pad_x=8, pad_y=8))
        self.assertTrue(X.fits(result))
        self.assertLess(result.w, 3000)


class Clamp(unittest.TestCase):
    def test_keeps_n_lines_and_ends_the_last_with_an_ellipsis(self):
        text = "one two three four five six seven eight nine ten eleven twelve"
        result = X.fit("clamp", X.FitRequest(text, min_w=120, min_h=20, pad_x=8, pad_y=4, max_lines=2))
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.lines), 2)
        self.assertTrue(result.lines[-1].endswith(X.ELLIPSIS))
        self.assertEqual(result.w, 120)


class Registry(unittest.TestCase):
    def test_the_contract_policies_are_registered(self):
        for name in ("hug", "shrink", "scale_shape", "clamp", "keep"):
            self.assertIn(name, X.FIT_POLICIES)
        with self.assertRaises(KeyError):
            X.fit("overflow", box("x"))

    def test_a_new_policy_is_one_register_call(self):
        def fixed(request):
            return X.fit("clamp", X.FitRequest(request.text, min_w=request.min_w, min_h=request.min_h, max_lines=1))

        X.register_fit("test-one-line", fixed)
        self.addCleanup(X.FIT_POLICIES.pop, "test-one-line")
        X.register_fit("test-one-line", fixed)  # the same function again is fine
        self.assertEqual(len(X.fit("test-one-line", box("a b c d e f g h i j k l m n o p q r s t u v w x y z", min_w=40)).lines), 1)
        with self.assertRaises(ValueError):
            X.register_fit("test-one-line", lambda request: fixed(request))

    def test_fit_record_is_what_the_element_stores(self):
        result = X.fit("hug", box("Checkout API"))
        self.assertEqual(X.fit_record(result, (160, 80)), {"policy": "hug", "min": [160, 80], "size": 20, "lines": ["Checkout API"],
                                                          "truncated": False, "estimated": False})
        self.assertEqual(math.ceil(result.w), result.w)


if __name__ == "__main__":
    unittest.main()
