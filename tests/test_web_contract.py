"""The contract between the Python canvas and the built page (canvas v2, items I1, I2, I4 and I6 of
``.local/prd/canvas-v2-architecture.md`` 3.3): the page renders every kind the registry says it must,
draws with the very font files Python measures, ships their licences, and imports the resolved tokens."""
from __future__ import annotations

import hashlib
import json
import re
import unittest

from support import PLUGIN_ROOT

from herdr_team import canvas_display as D
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
        if doc.get("display_list") is None:
            # web/dist from before the v2 renderer: only its source is checked until the page is rebuilt (phase 1, I4).
            return
        low, high = doc["display_list"]
        self.assertTrue(low <= D.DL_VERSION <= high, "the built page draws the display list the server writes")
        self.assertEqual(doc["slots"], sorted(R.slots()), "every browser-drawn kind has a slot renderer in the build")

    def test_the_v2_renderer_draws_this_display_list_and_every_slot(self):
        # web/src/v2/render/version.js is data only; postbuild copies it into web/dist/kinds.json (J6).
        source = (PLUGIN_ROOT / "web" / "src" / "v2" / "render" / "version.js").read_text(encoding="utf-8")
        supported = re.search(r"DL_SUPPORTED\s*=\s*\[\s*(\d+)\s*,\s*(\d+)\s*\]", source)
        slots = re.search(r"SLOT_KINDS\s*=\s*\[([^\]]*)\]", source)
        self.assertIsNotNone(supported)
        self.assertTrue(int(supported.group(1)) <= D.DL_VERSION <= int(supported.group(2)))
        self.assertEqual(sorted(re.findall(r'"([a-z0-9_-]+)"', slots.group(1))), sorted(R.slots()))

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

    def test_every_tool_is_one_the_page_can_use(self):
        # Phase 2 (6.4): tools carry the op they send (``template``); the page knows every gesture the registry names.
        source = (PLUGIN_ROOT / "web" / "src" / "v2" / "interact" / "toolset.js").read_text(encoding="utf-8")
        names = re.search(r"GESTURE_NAMES\s*=\s*\[([^\]]*)\]", source)
        self.assertIsNotNone(names, "toolset.js lists GESTURE_NAMES")
        page = set(re.findall(r'"([a-z]+)"', names.group(1)))
        self.assertLessEqual(set(R.GESTURES), page, "the page has a handler for every gesture a kind's tool may use")
        from herdr_team import canvas as C

        for tool in T.asset()["tools"]:
            with self.subTest(tool=tool["id"]):
                self.assertIn(tool["gesture"], R.GESTURES)
                if "template" in tool:
                    self.assertIn(tool["template"]["op"], C.OPS, "a template sends an op the canvas takes")
                    for key in tool["template"]:
                        self.assertIn(key, C._FIELDS[tool["template"]["op"]], "{} takes {}".format(tool["template"]["op"], key))
                if tool["gesture"] == "block":
                    self.assertIn("template", tool, "the block gesture sends its template")

    def test_the_page_reads_the_resolved_tokens(self):
        self.assertEqual(T.main(["--check"]), 0)
        vite = (PLUGIN_ROOT / "web" / "vite.config.js").read_text(encoding="utf-8")
        self.assertIn("assets/canvas/tokens.json", vite, "the page imports the generated token file, never a copy")


if __name__ == "__main__":
    unittest.main()
