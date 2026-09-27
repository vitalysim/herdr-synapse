"""The contract between the Python canvas and the built page (canvas v2, items I1, I2, I4 and I6 of
``.local/prd/canvas-v2-architecture.md`` 3.3): the page renders every kind the registry says it must,
draws with the very font files Python measures, ships their licences, and imports the resolved tokens."""
from __future__ import annotations

import hashlib
import json
import unittest

from support import PLUGIN_ROOT

from herdr_team import canvas_fontgen as G
from herdr_team import canvas_kinds as R
from herdr_team import canvas_theme as T

DIST = PLUGIN_ROOT / "web" / "dist"


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class WebContract(unittest.TestCase):
    def test_the_page_knows_every_kind_it_must_render(self):
        doc = json.loads((DIST / "kinds.json").read_text(encoding="utf-8"))
        self.assertEqual(doc["v"], 1)
        self.assertEqual(doc["kinds"], sorted(R.names(page_only=True)), "rebuild web/dist, or add the kind to web/src/canvas/kinds/")
        self.assertIsNone(doc["display_list"], "Phase 0 has no display list yet")

    def test_the_page_draws_with_the_files_python_measures(self):
        shipped = {sha(path) for path in (DIST / "assets").glob("*.ttf")}
        for _key, rel, _weight in G.FACES:
            with self.subTest(font=rel):
                self.assertIn(sha(G.FONTS_DIR / rel), shipped, "web/dist/assets has no byte-identical copy of {}".format(rel))

    def test_the_font_licences_ship_with_the_page(self):
        text = (DIST / "licenses" / "fonts.txt").read_text(encoding="utf-8")
        for name, licence in (("Inter", G.FONTS_DIR / "inter" / "OFL.txt"), ("Geist Mono", G.FONTS_DIR / "geist-mono" / "OFL.txt")):
            with self.subTest(font=name):
                self.assertIn(name, text)
                self.assertIn(licence.read_text(encoding="utf-8").strip().splitlines()[0], text, "the licence text itself")

    def test_the_page_reads_the_resolved_tokens(self):
        self.assertEqual(T.main(["--check"]), 0)
        vite = (PLUGIN_ROOT / "web" / "vite.config.js").read_text(encoding="utf-8")
        self.assertIn("assets/canvas/tokens.json", vite, "the page imports the generated token file, never a copy")


if __name__ == "__main__":
    unittest.main()
