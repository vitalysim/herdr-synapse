"""Colour by meaning (canvas v2 foundation): tones resolved per kind and variant, the defaults, legacy
colours, the token file's reader, and ``assets/canvas/tokens.json`` kept fresh."""
from __future__ import annotations

import io
import json
import unittest
from contextlib import redirect_stderr
from unittest import mock

from herdr_team import canvas_theme as T

KINDS = ("box", "ellipse", "diamond", "note", "text", "frame", "arrow", "pen", "path", "svg")


class Resolve(unittest.TestCase):
    def test_every_label_reads_on_its_own_fill_and_on_the_canvas(self):
        canvas = T.base()["canvas"]
        for tone in T.TONES:
            for variant in T.VARIANTS:
                for kind in KINDS:
                    colours = T.resolve(tone, variant, kind)
                    background = colours["fill"] or canvas
                    with self.subTest(tone=tone, variant=variant, kind=kind):
                        self.assertGreaterEqual(T.contrast(colours["text"], background), 4.5)
                        if kind in ("box", "ellipse", "diamond", "note") and variant != "solid":
                            self.assertGreaterEqual(T.contrast(colours["stroke"], canvas), 3.0, "an outline shows in the agent's PNG")

    def test_kinds_use_the_tone_roles_of_the_design(self):
        info = T.roles("info")
        self.assertEqual(T.resolve("info", "soft", "box"), {"stroke": info["stroke"], "fill": info["fill"], "text": info["text"]})
        self.assertEqual(T.resolve("info", "soft", "note")["fill"], info["sticky"])
        self.assertEqual(T.resolve("info", "soft", "frame"), {"stroke": info["zone_stroke"], "fill": info["zone"], "text": info["text"]})
        self.assertEqual(T.resolve("info", "solid", "box"), {"stroke": info["solid"], "fill": info["solid"], "text": info["on_solid"]})
        self.assertEqual(T.resolve("info", "outline", "box")["fill"], T.base()["surface"])
        self.assertEqual(T.resolve("neutral", "soft", "arrow"), {"stroke": T.base()["line"], "fill": None, "text": T.base()["ink_muted"]})
        self.assertEqual(T.resolve("danger", "soft", "text")["stroke"], T.roles("danger")["text"], "free text never takes a stroke colour")

    def test_unknown_names_fall_back_to_neutral_soft(self):
        self.assertEqual(T.resolve("mauve", "sparkly", "box"), T.resolve("neutral", "soft", "box"))

    def test_the_dark_theme_resolves_too(self):
        dark = T.resolve("success", "soft", "box", theme="dark")
        self.assertEqual(dark["fill"], T.roles("success", "dark")["fill"])
        self.assertGreaterEqual(T.contrast(dark["text"], dark["fill"]), 4.5)


class Defaults(unittest.TestCase):
    def test_a_note_is_an_idea_sticky_and_the_rest_is_neutral(self):
        self.assertEqual(T.default_tone("note"), ("idea", "soft"))
        for kind in ("box", "ellipse", "frame", "arrow", "text"):
            self.assertEqual(T.default_tone(kind), ("neutral", "soft"))

    def test_sizes_and_padding_come_from_the_tokens(self):
        tokens = T.tokens()
        self.assertEqual(T.size_min("diamond"), tuple(float(v) for v in tokens["size_min"]["diamond"]))
        self.assertEqual(T.padding("box"), (float(tokens["padding"]["box"]["x"]), float(tokens["padding"]["box"]["y"])))
        self.assertEqual(T.size_min("no-such-kind"), (160.0, 80.0))

    def test_legacy_names_and_hexes_map_to_tones(self):
        self.assertEqual((T.tone_of("blue"), T.tone_of("#1971C2"), T.tone_of("#ffec99"), T.tone_of("#123456"), T.tone_of(None)),
                         ("info", "info", "idea", None, None))

    def test_contrast_is_the_wcag_ratio(self):
        self.assertAlmostEqual(T.contrast("#000000", "#ffffff"), 21.0, places=6)
        self.assertAlmostEqual(T.contrast("#777777", "#777777"), 1.0, places=6)


class TokenFile(unittest.TestCase):
    def test_the_page_asset_is_current(self):
        self.assertEqual(T.main(["--check"]), 0, "run python3 -m herdr_team.canvas_theme --write")
        doc = json.loads(T.ASSET_PATH.read_text(encoding="utf-8"))
        self.assertEqual(doc["v"], T.ASSET_SCHEMA)
        self.assertEqual(set(doc["tones"]), set(T.TONES))
        self.assertEqual(doc["tones"]["warning"]["solid"], T.resolve("warning", "solid", "box"))
        self.assertEqual(doc["kinds"]["note"]["idea"]["soft"], T.resolve("idea", "soft", "note"))
        self.assertEqual(doc["fonts"]["sans"]["family"], "Inter")

    def test_a_stale_asset_fails_the_check(self):
        with mock.patch.object(T, "asset", return_value={"v": 0}), redirect_stderr(io.StringIO()) as err:
            self.assertEqual(T.main(["--check"]), 1)
        self.assertIn("stale", err.getvalue())

    def test_an_unreadable_token_file_draws_in_neutral(self):
        saved = dict(T._CACHE)
        T._CACHE.clear()
        self.addCleanup(lambda: (T._CACHE.clear(), T._CACHE.update(saved)))
        with mock.patch.object(T, "TOKENS_PATH", T.TOKENS_PATH.with_name("no-such-tokens.json")), \
                self.assertLogs("herdr_team.canvas_theme", level="WARNING"):
            colours = T.resolve("info", "soft", "box")
        self.assertEqual(colours["text"], "#1c2024")


if __name__ == "__main__":
    unittest.main()
